import logging

import pynmea2
import serial

from measurement_software.core.config import GnssConfig
from measurement_software.core.serial_retry import read_with_retry
from measurement_software.gnss.gnss_receiver import GNSSReceiver, GNSSFix, Position


class NMEASerial(GNSSReceiver):
    """GNSSReceiver implementation that reads GGA sentences from a serial NMEA stream."""

    def __init__(self, config: GnssConfig):
        self._logger = logging.getLogger(__name__)
        self._port = config.port
        self._baud_rate = config.baud_rate
        self._timeout = config.timeout
        self._retries = config.retries
        self._retry_delay_s = config.retry_delay_s
        self._serial = None  # type: ignore[var-annotated]

    def open(self) -> None:
        self._serial = serial.Serial(self._port, self._baud_rate, timeout=self._timeout)
        self._serial.reset_input_buffer()

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def read_fix(self) -> GNSSFix | None:
        """Reads one line and returns its GGA fix, or None if it isn't a valid GGA fix."""
        line = self._read_line()
        if not line.startswith(("$GNGGA", "$GPGGA")):
            return None
        msg = pynmea2.parse(line)

        if msg.gps_qual == 0:
            return None

        if int(msg.num_sats) < 5:
            self._logger.warning("Low satellite count (%s) - low measurement precision", msg.num_sats)

        position = Position(latitude=msg.latitude, longitude=msg.longitude, altitude=msg.altitude)
        return GNSSFix(position=position, num_satellites=int(msg.num_sats))

    def _read_line(self) -> str:
        """Reads one line, retrying a transient short/empty read (a pure timeout with nothing at all).

        A non-empty line that just isn't a GGA sentence is a normal, expected NMEA stream
        condition, not a failure — that's handled by read_fix() itself, not retried here.
        """
        raw = read_with_retry(
            self._connection.readline,
            is_transient=lambda b: not b,
            retries=self._retries,
            delay_s=self._retry_delay_s,
            logger=self._logger,
        )
        return raw.decode(errors="replace").strip()

    @property
    def _connection(self) -> serial.Serial:
        if self._serial is None:
            raise RuntimeError("GNSS receiver is not open. Call open() before using it.")
        return self._serial