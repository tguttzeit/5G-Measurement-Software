import json
import logging
import threading
import urllib.request
from http.client import HTTPException

from measurement_software.core.config import HeartbeatConfig
from measurement_software.core.run_status import RunStatus, RunStatusTracker


class HeartbeatSender:
    """Posts the run's status to the backend on a background thread while a run is going on.

    Reports liveness and whether the run is worth letting finish, so a broken run can be
    noticed while the vehicle is still out rather than after the data is uploaded.
    """

    def __init__(self, config: HeartbeatConfig, run_status: RunStatusTracker):
        self._logger = logging.getLogger(__name__)
        self._config = config
        self._run_status = run_status
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Starts reporting, unless the heartbeat is disabled or misconfigured."""
        if not self._is_usable():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the reporting thread to stop and waits for it to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _is_usable(self) -> bool:
        """Reports whether this run should send heartbeats, saying loudly why if it shouldn't."""
        if not self._config.enabled:
            self._logger.info("Backend heartbeat is disabled.")
            return False
        if not self._config.url.startswith("https://"):
            self._logger.error(
                "Backend heartbeat needs an https:// url but is configured with %r - "
                "staying silent for this run.", self._config.url,
            )
            return False
        return True

    def _run(self, stop_event: threading.Event) -> None:
        """Reports the run's status immediately, then once per configured interval."""
        while True:
            self._send(self._run_status.status())
            if stop_event.wait(self._config.interval_s):
                return

    def _send(self, status: RunStatus) -> None:
        """Posts one status report, treating a failed send as a skipped tick rather than an error.

        A backend that is unreachable — a dead spot, a server outage — must never take a
        measurement run down with it.
        """
        request = urllib.request.Request(
            self._config.url,
            data=json.dumps(status.as_payload()).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_s) as response:
                self._logger.debug("Heartbeat accepted (HTTP %s)", response.status)
        except (OSError, HTTPException) as e:
            self._logger.warning("Heartbeat could not be sent: %s", e)
