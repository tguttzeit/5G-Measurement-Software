import logging
from datetime import datetime

import pynmea2
import serial

from measurement_software.core.config import GnssConfig
from measurement_software.core.util import read_with_retry
from measurement_software.gnss.gnss_receiver import GNSSReceiver, GNSSFix, Position


class NMEASerial(GNSSReceiver):
    """GNSSReceiver implementation that reads GGA and ZDA sentences from a serial NMEA stream."""

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
        """Reads one line and returns its GGA fix, or None if it isn't a valid GGA fix.

        A structurally invalid sentence (bad checksum, truncated fields) or one whose fields
        don't parse as expected (e.g. a non-numeric satellite count) is treated as unusable, not
        an error - see read_datetime()'s docstring for why a bad line off the wire must never
        crash the caller.
        """
        line = self._read_line()
        if not line.startswith(("$GNGGA", "$GPGGA")):
            return None

        try:
            msg = pynmea2.parse(line)
            if msg.gps_qual == 0:
                return None

            num_sats = int(msg.num_sats)
            if num_sats < 5:
                self._logger.warning("Low satellite count (%s) - low measurement precision", num_sats)

            position = Position(latitude=msg.latitude, longitude=msg.longitude, altitude=msg.altitude)
            return GNSSFix(position=position, num_satellites=num_sats)
        except (TypeError, ValueError):
            self._logger.warning("Discarding malformed GGA sentence: %s", line)
            return None

    def read_satellites_in_view(self) -> int | None:
        """Reads one line and returns its GSV satellites-in-view count, or None if it isn't GSV.

        GSV's "total number of satellites in view" field is populated regardless of fix status -
        unlike GGA's satellite count (see read_fix()), which is 0 until a fix exists.

        A structurally invalid or malformed-field sentence is treated the same as a non-GSV line
        - see read_fix()'s docstring for why.
        """
        line = self._read_line()
        if not line.startswith(("$GNGSV", "$GPGSV", "$GLGSV", "$GAGSV", "$GBGSV")):
            return None
        try:
            msg = pynmea2.parse(line)
            return int(msg.num_sv_in_view)
        except (TypeError, ValueError):
            self._logger.warning("Discarding malformed GSV sentence: %s", line)
            return None

    def read_datetime(self) -> datetime | None:
        """Reads one line and returns its ZDA UTC date+time, or None if it isn't a usable ZDA sentence.

        Treats a structurally invalid sentence (bad checksum, corrupted field) the same as a ZDA
        with empty date/time fields (real GNSS behavior before it has a time lock) - both parse to
        nothing usable, not an error - since this feeds a system clock change and must never let a
        single bad reading off the wire crash the caller.
        """
        line = self._read_line()
        if not line.startswith(("$GNZDA", "$GPZDA")):
            return None

        try:
            msg = pynmea2.parse(line)
            return msg.datetime
        except (TypeError, ValueError):
            return None

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