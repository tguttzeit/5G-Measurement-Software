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
        self._modem = modem
        self._gnss_receiver = gnss_receiver
        self._position_threshold = config.position_threshold
        self._max_idle_time = config.max_idle_time
        self._max_wait_for_fix = config.max_wait_for_fix
        self._wait_log_interval = config.wait_log_interval
        self._keep_alive_host = config.keep_alive_host
        self._keep_alive_port = config.keep_alive_port
        self._keep_alive_interval_s = config.keep_alive_interval_s

    def collect(self) -> list[Datapoint]:
        self._modem.open()
        self._gnss_receiver.open()
        time.sleep(1)

        data_points: list[Datapoint] = []
        last_pos: Position | None = None
        idle_start: float | None = None

        gps_fix = False
        fix_wait_start = time.time()
        last_wait_log = 0


        # log("Starte Datenerfassung")

        # Keep-Alive-Thread starten
        keep_alive_stop_event = threading.Event()
        keep_alive_thread = threading.Thread(target=self.keep_link_alive, args=(keep_alive_stop_event,), daemon=True)
        keep_alive_thread.start()

        try:
            while True:
                # GPS-Fix Timeout
                if not gps_fix and (time.time() - fix_wait_start) > self._max_wait_for_fix:
                    # log(f"Kein GPS-Fix innerhalb von {self._max_wait_for_fix}s - Abbruch.")
                    break

                fix = self._gnss_receiver.read_fix()
                if fix is None:
                    continue

                if not gps_fix:
                    gps_fix = True


                # Bewegungsdetektion
                if last_pos is not None:
                    dist = Collector.haversine(last_pos, fix.position)
                    if dist < self._position_threshold:
                        # log(f"Keine Bewegung erkannt: {dist:.1f} m (<{self._position_threshold} m)")
                        continue  # Keine relevante Bewegung
                last_pos = fix.position
                idle_start = None  # Reset Idle-Timer
                #log(f"Bewegung erkannt: {dist:.1f} m")

                # Warnung bei wenig Satelliten
                if int(fix.num_satellites) < 5:
                    pass
                    # log(f"WARNUNG: Geringe Satellitenzahl ({msg.num_sats}) - Messung ungenau.")

                # 5G-Daten holen (kann mehrere Dicts liefern) -> Timestamp vorher berechnen
                timestamp = datetime.now(UTC).isoformat() + "Z"
                for modem_data in self._modem.query_cell_info():
                    data_points.append(Datapoint(
                        timestamp=timestamp,
                        fix=fix,
                        cell_sample=modem_data
                    ))
                    # log(f"Punkt erfasst: {entry}")

                # Idle-Timer setzen
                if idle_start is None:
                    idle_start = time.time()

                # Messung beenden bei langem Stillstand
                if gps_fix and idle_start and (time.time() - idle_start) >= self._max_idle_time:
                    # log("Keine Bewegung mehr - Datenerfassung beendet.")
                    break
        finally:
            keep_alive_stop_event.set()
            keep_alive_thread.join()
            self._gnss_receiver.close()
            self._modem.close()
            # log("Datenerfassung abgeschlossen")

        return data_points

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

    @staticmethod
    def haversine(old_position: Position, new_position: Position) -> float:
        r = 6371000
        d_latitude = radians(new_position.latitude - old_position.latitude)
        d_longitude = radians(new_position.longitude - old_position.longitude)
        a = sin(d_latitude / 2) ** 2
        b = cos(radians(old_position.latitude)) * cos(radians(new_position.latitude)) * sin(d_longitude / 2) ** 2
        return 2 * r * asin(sqrt(a+b))