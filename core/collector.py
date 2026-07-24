import logging
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from math import radians, sin, cos, asin, sqrt

from core.config import CollectorConfig
from gnss.gnss_receiver import GNSSReceiver, Position, GNSSFix
from modems.modem import Modem, CellSample


@dataclass
class Datapoint:
    timestamp: str
    fix: GNSSFix
    cell_sample: CellSample


class Collector:
    def __init__(self, modem: Modem, gnss_receiver: GNSSReceiver, config: CollectorConfig):
        self._logger = logging.getLogger(__name__)
        self._modem = modem
        self._gnss_receiver = gnss_receiver

        self._position_threshold = config.position_threshold
        self._max_idle_time = config.max_idle_time
        self._max_wait_for_first_fix = config.max_wait_for_first_fix
        self._wait_log_interval = config.wait_log_interval

        self._keep_alive_host = config.keep_alive_host
        self._keep_alive_port = config.keep_alive_port
        self._keep_alive_interval_s = config.keep_alive_interval_s

        self._last_pos: Position | None = None
        self._last_movement_time: float | None = None

    def collect(self) -> list[Datapoint]:
        self._modem.open()
        self._gnss_receiver.open()
        time.sleep(1)

        self._logger.info("Starting data collection...")

        self._start_keep_alive_thread()

        try:
            first_fix = self._wait_for_first_fix()
            if first_fix is None:
                return []

            datapoints: list[Datapoint] = []
            self._last_pos = None
            self._last_movement_time = None

            for fix in self._iter_fixes(first_fix):
                if not self._has_moved_enough(fix):
                    continue

                datapoints.extend(self._capture_datapoints(fix))

                if self._has_been_idle_too_long():
                    break
        finally:
            self._stop_keep_alive_thread()
            self._gnss_receiver.close()
            self._modem.close()
            self._logger.info("Finished data collection")

        return datapoints

    def _start_keep_alive_thread(self, ) -> None:
        self._keep_alive_stop_event = threading.Event()
        self._keep_alive_thread = threading.Thread(target=self.keep_link_alive, args=(self._keep_alive_stop_event,),
                                                   daemon=True)
        self._keep_alive_thread.start()

    def keep_link_alive(self, stop_event: threading.Event):
        """Kleines UDP-Paket alle KEEPALIVE_INTERVAL Sekunden senden."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            pkt = b"\x00"  # 1-Byte-Payload
            next_timestamp = time.time()
            while not stop_event.is_set():
                now = time.time()
                if now >= next_timestamp:
                    try:
                        s.sendto(pkt, (self._keep_alive_host, self._keep_alive_port))
                    except OSError:
                        pass  # Netzwerk gerade nicht verfügbar
                    next_timestamp = now + self._keep_alive_interval_s
                time.sleep(0.2)

    def _stop_keep_alive_thread(self):
        self._keep_alive_stop_event.set()
        self._keep_alive_thread.join()

    def _wait_for_first_fix(self) -> GNSSFix | None:
        start = time.time()
        last_wait_log = 0.0

        while True:
            elapsed = time.time() - start
            if elapsed > self._max_wait_for_first_fix:
                self._logger.info("No GPS fix in specified interval of %s s. - Aborting.",
                                  self._max_wait_for_first_fix)
                return None

            now = time.time()
            if (now - last_wait_log) >= self._wait_log_interval:
                self._logger.info("Waiting for first GPS fix… (for %s s by now)", int(elapsed))
                last_wait_log = now

            fix = self._gnss_receiver.read_fix()
            if fix is not None:
                return fix

    def _iter_fixes(self, first_fix: GNSSFix):
        yield first_fix
        while True:
            fix = self._gnss_receiver.read_fix()
            if fix is not None:
                yield fix

    def _has_moved_enough(self, fix: GNSSFix) -> bool:
        if self._last_pos is None:
            self._last_pos = fix.position
            return True

        dist = Collector.haversine(self._last_pos, fix.position)
        if dist < self._position_threshold:
            self._logger.debug(
                "No relevant movement detected: %.1f m (< %s m (specified threshold))",
                dist, self._position_threshold,
            )
            return False

        self._logger.info("Enough movement detected: %.1f m", dist)
        self._last_pos = fix.position
        self._last_movement_time = None
        return True

    @staticmethod
    def haversine(old_position: Position, new_position: Position) -> float:
        r = 6371000
        d_latitude = radians(new_position.latitude - old_position.latitude)
        d_longitude = radians(new_position.longitude - old_position.longitude)
        a = sin(d_latitude / 2) ** 2
        b = cos(radians(old_position.latitude)) * cos(radians(new_position.latitude)) * sin(d_longitude / 2) ** 2
        return 2 * r * asin(sqrt(a + b))

    def _capture_datapoints(self, fix: GNSSFix) -> list[Datapoint]:
        timestamp = f"{datetime.now(UTC).isoformat()}Z"
        datapoints: list[Datapoint] = []
        for modem_data in self._modem.query_cell_info():
            datapoint = Datapoint(timestamp=timestamp, fix=fix, cell_sample=modem_data)
            datapoints.append(datapoint)
            self._logger.debug("Datapoint captured: %s", datapoint)
        return datapoints

    def _has_been_idle_too_long(self) -> bool:
        if self._last_movement_time is None:
            self._last_movement_time = time.time()
            return False

        idle_duration = time.time() - self._last_movement_time
        if idle_duration >= self._max_idle_time:
            self._logger.info("No movement for %s s. Ending data collection.", self._max_idle_time)
            return True
        return False
