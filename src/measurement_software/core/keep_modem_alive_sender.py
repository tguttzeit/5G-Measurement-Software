import logging
import socket
import threading
import time


class KeepModemAliveSender:
    """Sends a periodic UDP heartbeat on a background thread to keep the modem's link alive."""

    def __init__(self, host: str, port: int, interval_s: float):
        self._logger = logging.getLogger(__name__)
        self._host = host
        self._port = port
        self._interval_s = interval_s
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Starts the background heartbeat thread."""
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the heartbeat thread to stop and waits for it to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self, stop_event: threading.Event) -> None:
        """Kleines UDP-Paket alle interval_s Sekunden senden."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            pkt = b"\x00"  # 1-Byte-Payload
            next_timestamp = time.time()
            while not stop_event.is_set():
                now = time.time()
                if now >= next_timestamp:
                    try:
                        s.sendto(pkt, (self._host, self._port))
                    except OSError:
                        pass  # Netzwerk gerade nicht verfügbar
                    next_timestamp = now + self._interval_s
                time.sleep(0.2)