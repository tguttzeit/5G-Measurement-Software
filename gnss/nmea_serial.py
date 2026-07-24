import logging

import pynmea2
import serial

from core.config import GnssConfig
from gnss.gnss_receiver import GNSSReceiver, GNSSFix, Position


class NMEASerial(GNSSReceiver):
    def __init__(self, config: GnssConfig):
        self._logger = logging.getLogger(__name__)
        self._port = config.port
        self._baud_rate = config.baud_rate
        self._timeout = config.timeout
        self._serial = None  # type: ignore[var-annotated]

    def open(self) -> None:
        self._serial = serial.Serial(self._port, self._baud_rate, timeout=self._timeout)
        self._serial.reset_input_buffer()

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def read_fix(self) -> GNSSFix | None:
        line = self._connection.readline().decode(errors="replace").strip()
        if not line.startswith(("$GNGGA", "$GPGGA")):
            return None
        msg = pynmea2.parse(line)

        if msg.gps_qual == 0:
            return None

        if int(msg.num_sats) < 5:
            self._logger.warning("Low satellite count (%s) - low measurement precision", msg.num_sats)

        position = Position(latitude=msg.latitude, longitude=msg.longitude, altitude=msg.altitude)
        return GNSSFix(position=position, num_satellites=int(msg.num_sats))

    @property
    def _connection(self) -> serial.Serial:
        if self._serial is None:
            raise RuntimeError("GNSS receiver is not open. Call open() before using it.")
        return self._serial