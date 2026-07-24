import logging
import socket
import time
from math import radians, sin, cos, asin, sqrt

import pynmea2
import threading
from datetime import datetime, UTC
from dataclasses import dataclass

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

    def collect(self) -> list[Datapoint]:
        self._modem.open()
        self._gnss_receiver.open()
        time.sleep(1)

        datapoints: list[Datapoint] = []
        last_pos: Position | None = None
        idle_start: float | None

        gps_fix = False
        fix_wait_start = time.time()
        last_wait_log = 0

        self._logger.info("Starting data collection...")

        self._start_keep_alive_thread()

        try:
            while True:
                # GPS-Fix Timeout
                if not gps_fix and (time.time() - fix_wait_start) > self._max_wait_for_first_fix:
                    self._logger.info("No GPS fix in specified interval of %s s. - Aborting.",
                                      self._max_wait_for_first_fix)
                    break

                now = time.time()
                if not gps_fix and (now - last_wait_log) >= self._wait_log_interval:
                    self._logger.info("Waiting for first GPS fix… (for %s s by now)",
                                      int(now - fix_wait_start))
                    last_wait_log = now

                fix = self._gnss_receiver.read_fix()
                if fix is None:
                    continue

                if not gps_fix:
                    gps_fix = True

                if last_pos is not None:
                    dist = Collector.haversine(last_pos, fix.position)
                    if dist < self._position_threshold:
                        self._logger.debug("No relevant movement detected: %.1f m (< %s m (specified threshold))",
                                          dist, self._position_threshold)
                        continue
                    self._logger.info("Enough movement detected: %.1f m", dist)
                last_pos = fix.position
                idle_start = None  # Reset Idle-Timer

                if int(fix.num_satellites) < 5:
                    self._logger.warning("Low satellite count (%s) - low measurement precision")

                timestamp = f"{datetime.now(UTC).isoformat()}Z"
                for modem_data in self._modem.query_cell_info():
                    datapoint = Datapoint(timestamp=timestamp, fix=fix, cell_sample=modem_data)
                    datapoints.append(datapoint)
                    self._logger.debug("Datapoint captured: %s", datapoint)

                # Idle-Timer setzen
                if idle_start is None:
                    idle_start = time.time()

                # Messung beenden bei langem Stillstand
                if gps_fix and idle_start and (time.time() - idle_start) >= self._max_idle_time:
                    self._logger.info("No movement for %s s. Ending data collection.",
                                      self._max_idle_time)
                    break
        finally:
            self._stop_keep_alive_thread()
            self._gnss_receiver.close()
            self._modem.close()
            self._logger.info("Finished data collection")

        return datapoints

    def _start_keep_alive_thread(self,) -> None:
        self._keep_alive_stop_event = threading.Event()
        self._keep_alive_thread = threading.Thread(target=self.keep_link_alive, args=(self._keep_alive_stop_event,),
                                                   daemon=True)
        self._keep_alive_thread.start()

    def keep_link_alive(self, stop_event: threading.Event):
        """Kleines UDP-Paket alle KEEPALIVE_INTERVAL Sekunden senden."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            pkt = b"\x00"   # 1-Byte-Payload
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

    @staticmethod
    def haversine(old_position: Position, new_position: Position) -> float:
        r = 6371000
        d_latitude = radians(new_position.latitude - old_position.latitude)
        d_longitude = radians(new_position.longitude - old_position.longitude)
        a = sin(d_latitude / 2) ** 2
        b = cos(radians(old_position.latitude)) * cos(radians(new_position.latitude)) * sin(d_longitude / 2) ** 2
        return 2 * r * asin(sqrt(a+b))