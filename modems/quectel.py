import logging
import time
import serial

from core.config import ModemConfig
from modems.modem import Modem, CellSample
from enum import StrEnum

class QuectelMode(StrEnum):
    SERVING_CELL = "serving_cell"
    SA_SCAN = "sa_scan"

class Quectel(Modem):
    def __init__(self, config: ModemConfig):
        self._logger = logging.getLogger(__name__)
        self._port = config.port
        self._baud_rate = config.baud_rate
        self._timeout = config.timeout
        self._mode = QuectelMode(config.mode)
        self._serial: serial.Serial | None = None

    def open(self) -> None:
        self._serial = serial.Serial(self._port, self._baud_rate, timeout=self._timeout)

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def query_cell_info(self) -> list[CellSample]:
        if self._mode == QuectelMode.SA_SCAN:
            return self._query_sa_scan()
        return self._query_serving_cell()

    def power_down(self) -> None:
        self._connection.write(b'AT+QPOWD=1\r')
        time.sleep(5)

    @property
    def _connection(self) -> serial.Serial:
        if self._serial is None:
            raise RuntimeError("Modem is not open. Call open() before using it.")
        return self._serial

    def _query_serving_cell(self) -> list[CellSample]:
        self._connection.write(b'AT+QENG="servingcell"\r')
        time.sleep(0.5)
        resp = self._connection.read_all().decode(errors="ignore")
        return self._parse_qeng_response(resp)

    def _query_sa_scan(self) -> list[CellSample]:
        self._logger.info("Running AT+QSCAN=1,1 (NR5G-SA-only scan)")
        self._connection.write(b'AT+QSCAN=1,1\r')
        time.sleep(7)
        resp = self._connection.read_all().decode(errors="ignore")
        return self._parse_qscan_response(resp)

    def _parse_qeng_response(self, response: str) -> list[CellSample]:
        results: list[CellSample] = []
        for line in response.splitlines():
            if not line.startswith("+QENG:"):
                continue

            payload = line[len("+QENG:") :].strip()
            if payload.startswith('"servingcell"'):
                continue

            parts = [Quectel._clean(p) for p in payload.split(",")]
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
        return CellSample(
            rat="LTE",
            mcc=Quectel._to_int(parts[2]),
            mnc=Quectel._to_int(parts[3]),
            cell_id=Quectel._to_int(parts[4]),
            pci=Quectel._to_int(parts[5]),
            tac=Quectel._to_int(parts[10]),
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
