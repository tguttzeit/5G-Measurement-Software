import logging

import pynmea2
import serial

from gnss.gnss_receiver import GNSSReceiver, GNSSFix, Position


class NMEASerial(GNSSReceiver):
    def __init__(self, port: str, baud_rate: int = 9600, timeout: float = 1.0):
        self._logger = logging.getLogger(__name__)
        self._port = port
        self._baud_rate = baud_rate
        self._timeout = timeout
        self._serial = None  # type: ignore[var-annotated]

    def open(self) -> None:
        self._serial = serial.Serial(self._port, self._baud_rate, timeout=self._timeout)
        self._serial.reset_input_buffer()

    def close(self) -> None:
        raise NotImplementedError

    def read_fix(self) -> GNSSFix | None:
        line = self._connection.readline().decode(errors="replace").strip()
        if not line.startswith(("$GNGGA", "$GPGGA")):
            return None
        msg = pynmea2.parse(line)
        if msg.gps_qual == 0:
            return None
        position = Position(latitude=msg.latitude, longitude=msg.longitude)
        return GNSSFix(position=position, num_satellites=int(msg.num_sats))

    @property
    def _connection(self) -> serial.Serial:
        if self._serial is None:
            raise RuntimeError("Modem is not open. Call open() before using it.")
        return self._serial