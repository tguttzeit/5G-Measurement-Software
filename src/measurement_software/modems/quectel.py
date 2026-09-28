import glob
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

import serial

from measurement_software.core.config import ModemConfig
from measurement_software.core.util import read_with_retry
from measurement_software.modems.modem import (
    Modem,
    CellSample,
    ModemConnectionError,
    SimPinNotConfiguredError,
    SimPinRejectedError,
    SimPukRequiredError,
    SimStatusUnknownError,
    SimUnlockError,
)
from enum import StrEnum

# udev's stable name for the AT command interface (USB interface 2) of any Quectel module. Unlike
# /dev/ttyUSBn it doesn't depend on enumeration order, which shifts e.g. when the modem drops off
# USB and re-enumerates while its old port is still held open.
AT_PORT_BY_ID_PATTERN = "/dev/serial/by-id/usb-Quectel_*-if02-port0"

# AT+CFUN can take several seconds to answer while the radio switches.
_RADIO_SWITCH_WAIT_S = 5.0

class QuectelMode(StrEnum):
    """Which AT command the modem is queried with: the active serving cell or an NR5G-SA scan."""

    SERVING_CELL = "serving_cell"
    SA_SCAN = "sa_scan"

class SimStatus(StrEnum):
    """The SIM lock state as reported by AT+CPIN?."""

    READY = "ready"
    SIM_PIN = "sim_pin"
    SIM_PUK = "sim_puk"
    UNKNOWN = "unknown"

class Quectel(Modem):
    """Modem implementation for Quectel modules, driven over a serial AT command interface."""

    def __init__(self, config: ModemConfig):
        self._logger = logging.getLogger(__name__)
        self._port = config.port
        self._baud_rate = config.baud_rate
        self._timeout = config.timeout
        self._mode = QuectelMode(config.mode)
        self._retries = config.retries
        self._retry_delay_s = config.retry_delay_s
        self._sim_pin_env_var = config.sim_pin_env_var
        self._sim_unlock_poll_attempts = config.sim_unlock_poll_attempts
        self._sim_unlock_poll_interval = config.sim_unlock_poll_interval
        self._serial: serial.Serial | None = None

    def open(self) -> None:
        """Opens the serial connection and ensures the SIM is unlocked before returning.

        Connecting and confirming the SIM is usable is one atomic step: if unlock_sim() raises,
        the serial connection it can't use is closed again rather than left open. The port is
        looked up anew on every open, so reopening after a USB re-enumeration finds the modem
        wherever it reappeared.
        """
        port = self._resolve_port()
        with self._connection_errors():
            self._serial = serial.Serial(port, self._baud_rate, timeout=self._timeout)
        self._logger.info("Modem AT port opened: %s", port)
        try:
            with self._connection_errors():
                self.unlock_sim()
        except (SimUnlockError, ModemConnectionError):
            self.close()
            raise

    def close(self) -> None:
        """Closes the connection; a port that already vanished from the bus is only logged."""
        if self._serial is None:
            return
        try:
            self._serial.close()
        except OSError as e:
            self._logger.warning("Closing the modem port failed (already gone?): %s", e)
        finally:
            self._serial = None

    def query_cell_info(self) -> list[CellSample]:
        """Queries the modem using the configured mode and parses the response into CellSamples."""
        with self._connection_errors():
            if self._mode == QuectelMode.SA_SCAN:
                return self._query_sa_scan()
            return self._query_serving_cell()

    def _resolve_port(self) -> str:
        """Returns the configured port, or else the one Quectel AT interface found by-id."""
        if self._port is not None:
            return self._port
        candidates = sorted(glob.glob(AT_PORT_BY_ID_PATTERN))
        if not candidates:
            raise ModemConnectionError(
                f"No Quectel AT port found ({AT_PORT_BY_ID_PATTERN}) - is the modem connected? "
                "Set modem.port to use a fixed port instead."
            )
        if len(candidates) > 1:
            raise ModemConnectionError(
                f"Several Quectel AT ports found ({', '.join(candidates)}) - "
                "set modem.port to choose one."
            )
        return candidates[0]

    @contextmanager
    def _connection_errors(self) -> Iterator[None]:
        """Turns serial-level I/O failures into the modem-neutral ModemConnectionError."""
        try:
            yield
        except OSError as e:  # serial.SerialException is an OSError too
            raise ModemConnectionError(str(e)) from e

    def reset_radio(self) -> None:
        """Turns the radio off and on again (AT+CFUN=0, then AT+CFUN=1)."""
        with self._connection_errors():
            for command in (b"AT+CFUN=0\r", b"AT+CFUN=1\r"):
                self._connection.write(command)
                time.sleep(_RADIO_SWITCH_WAIT_S)
                response = self._connection.read_all().decode(errors="ignore")
                if "OK" not in response:
                    self._logger.warning("%s got no OK: %r", command.decode().strip(), response.strip())

    def power_down(self) -> None:
        self._connection.write(b'AT+QPOWD=1\r')
        time.sleep(5)

    def unlock_sim(self) -> None:
        """Unlocks the SIM if it's PIN-locked, entering the PIN from the configured env var at
        most once.

        Never retries a rejected PIN and never attempts PUK entry - see SimUnlockError subclasses
        for the possible failure modes.
        """
        status = self._query_sim_status()
        if status == SimStatus.READY:
            self._logger.info("SIM already unlocked.")
            return
        if status == SimStatus.SIM_PUK:
            raise SimPukRequiredError("SIM requires a PUK; refusing to attempt PUK entry.")
        if status != SimStatus.SIM_PIN:
            raise SimStatusUnknownError(f"Could not determine SIM lock status (got {status!r}).")

        pin = os.environ.get(self._sim_pin_env_var)
        if not pin:
            raise SimPinNotConfiguredError(
                f"SIM is PIN-locked but {self._sim_pin_env_var} is not set."
            )

        response = self._send_pin(pin)
        if "+CME ERROR" in response:
            raise SimPinRejectedError("Modem rejected the SIM PIN; it will not be retried automatically.")
        if "OK" not in response:
            raise SimStatusUnknownError("No clear response after sending the SIM PIN.")

        if not self._poll_until_ready():
            raise SimStatusUnknownError("SIM did not reach READY after the PIN was accepted.")

        self._logger.info("SIM unlocked.")

    def _send_pin(self, pin: str) -> str:
        self._connection.write(f'AT+CPIN="{pin}"\r'.encode())
        time.sleep(0.5)
        return self._connection.read_all().decode(errors="ignore")

    def _poll_until_ready(self) -> bool:
        for _ in range(self._sim_unlock_poll_attempts):
            if self._query_sim_status() == SimStatus.READY:
                return True
            time.sleep(self._sim_unlock_poll_interval)
        return False

    def _query_sim_status(self) -> SimStatus:
        self._connection.write(b'AT+CPIN?\r')
        time.sleep(0.5)
        resp = self._connection.read_all().decode(errors="ignore")
        return self._parse_cpin_response(resp)

    @staticmethod
    def _parse_cpin_response(response: str) -> SimStatus:
        if "READY" in response:
            return SimStatus.READY
        if "PUK" in response:
            return SimStatus.SIM_PUK
        if "SIM PIN" in response:
            return SimStatus.SIM_PIN
        return SimStatus.UNKNOWN

    @property
    def _connection(self) -> serial.Serial:
        if self._serial is None:
            raise RuntimeError("Modem is not open. Call open() before using it.")
        return self._serial

    def _query_serving_cell(self) -> list[CellSample]:
        self._connection.write(b'AT+QENG="servingcell"\r')
        time.sleep(0.5)
        resp = self._read_response()
        return self._parse_qeng_response(resp)

    def _query_sa_scan(self) -> list[CellSample]:
        self._logger.info("Running AT+QSCAN=1,1 (NR5G-SA-only scan)")
        self._connection.write(b'AT+QSCAN=1,1\r')
        time.sleep(7)
        resp = self._read_response()
        return self._parse_qscan_response(resp)

    def _read_response(self) -> str:
        """Reads the AT command response, retrying a transient short/empty read."""
        raw = read_with_retry(
            self._connection.read_all,
            is_transient=lambda b: not b,
            retries=self._retries,
            delay_s=self._retry_delay_s,
            logger=self._logger,
        )
        return raw.decode(errors="ignore")

    def _parse_qeng_response(self, response: str) -> list[CellSample]:
        """Parses an AT+QENG="servingcell" response into CellSamples.

        Real modules report each cell as one line starting with the report type and connection
        state ahead of the RAT itself, e.g. `+QENG: "servingcell","NOCONN","LTE",...` - those two
        leading fields are dropped here so the RAT lines up at parts[0] the same way for every
        RAT-specific parser below. A bare `"servingcell"` line with nothing else on it (seen on
        some firmware as a header echo with no data following) has nothing left to parse and is
        skipped.
        """
        results: list[CellSample] = []
        for line in response.splitlines():
            if not line.startswith("+QENG:"):
                continue

            payload = line[len("+QENG:") :].strip()
            parts = [Quectel._clean(p) for p in payload.split(",")]
            if parts[0] == "servingcell":
                if len(parts) < 3:
                    continue
                parts = parts[2:]
            rat_token = parts[0]

            if rat_token == "LTE":
                results.append(Quectel._parse_lte(parts))
            elif rat_token in ("NR5G-SA", "NR5G-NSA"):
                results.append(Quectel._parse_nr5g(parts, rat_token))
            else:
                continue

        if not results:
            pass
            self._logger.warning("No QENG measurements found!")
        return results

    def _parse_qscan_response(self, response: str) -> list[CellSample]:
        """Parses an AT+QSCAN=1,1 response into CellSamples, keeping only NR5G-SA results."""
        results: list[CellSample] = []
        for line in response.splitlines():
            if not line.startswith("+QSCAN:"):
                continue

            parts = [Quectel._clean(p) for p in line[len("+QSCAN:"):].strip().split(",")]
            if not parts or parts[0] != "NR5G-SA":
                continue

            try:
                results.append(CellSample(
                    rat=parts[0],
                    mcc=Quectel._to_int(parts[1]),
                    mnc=Quectel._to_int(parts[2]),
                    tac=Quectel._to_int(parts[3]),
                    pci=Quectel._to_int(parts[4]),
                    channel=Quectel._to_int(parts[6]),
                    band=Quectel._to_int(parts[7]),
                    rsrp=Quectel._to_float(parts[9]),
                    rsrq=Quectel._to_float(parts[10]),
                    sinr=Quectel._to_float(parts[11]),
                ))
            except IndexError as e:
                self._logger.warning("Error parsing QSCAN line: %s", e)
                continue

        if not results:
            self._logger.warning("No NR5G-SA cells found via QSCAN")
        return results

    @staticmethod
    def _parse_lte(parts: list[str | None]) -> CellSample:
        """Builds a CellSample from a parsed LTE +QENG data line."""
        return CellSample(
            rat="LTE",
            mcc=Quectel._to_int(parts[2]),
            mnc=Quectel._to_int(parts[3]),
            cell_id=Quectel._to_hex_int(parts[4]),
            pci=Quectel._to_int(parts[5]),
            tac=Quectel._to_hex_int(parts[10]),
            rsrp=Quectel._to_float(parts[11]),
            rsrq=Quectel._to_float(parts[12]),
            rssi=Quectel._to_float(parts[13]),
            sinr=Quectel._to_float(parts[14]),
            band=Quectel._to_int(parts[7]),
            channel=Quectel._to_int(parts[6]),
            vendor_specific_extras={
                "duplex_mode": parts[1],
                "cqi": Quectel._to_int(parts[15]),
                "tx_power": Quectel._to_int(parts[16]),
                "srxlev": Quectel._to_int(parts[17]),
            },
        )

    @staticmethod
    def _parse_nr5g(parts: list[str | None], rat: str) -> CellSample:
        """Builds a CellSample from a parsed NR5G-SA/NSA +QENG data line."""
        return CellSample(
            rat=rat,
            mcc=Quectel._to_int(parts[1]),
            mnc=Quectel._to_int(parts[2]),
            tac=Quectel._to_int(parts[3]),
            rsrp=Quectel._to_float(parts[4]),
            sinr=Quectel._to_float(parts[5]),
            rsrq=Quectel._to_float(parts[6]),
            channel=Quectel._to_int(parts[7]),
            band=Quectel._to_int(parts[8]),
            vendor_specific_extras={"scs_index": Quectel._to_int(parts[9])},
        )

    @staticmethod
    def _to_int(val: str | None) -> int | None:
        if val is None:
            return None
        try:
            return int(val, 16) if val.startswith("0x") else int(val)
        except ValueError:
            return None

    @staticmethod
    def _to_hex_int(val: str | None) -> int | None:
        """Parses a bare (no "0x" prefix) hex field - AT+QENG reports CellID/TAC this way."""
        if val is None:
            return None
        try:
            return int(val, 16)
        except ValueError:
            return None

    @staticmethod
    def _to_float(val: str | None) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    @staticmethod
    def _clean(val: str | None) -> str | None:
        if val is None:
            return None
        val = val.strip().strip('"')
        return None if val in ("", "-") else val
