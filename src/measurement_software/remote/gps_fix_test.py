import json
import logging
import threading
import time
import urllib.request
from http.client import HTTPException

from measurement_software.core.config import GnssConfig, GpsFixTestConfig
from measurement_software.gnss import create_gnss_receiver
from measurement_software.gnss.gnss_receiver import GNSSReceiver
from measurement_software.status.backend_status import GpsFixStatusTracker

_STATUS_POST_TIMEOUT_S = 5.0
# Upper bound on lines read per report cycle while looking for a fix or a satellites-in-view
# count - a real NMEA stream interleaves several sentence types per second, so one read per
# cycle would mostly miss both. Bounded rather than unbounded so a stream with neither sentence
# type can't stall a report cycle indefinitely.
_MAX_READS_PER_REPORT = 10


class GpsFixTestRunner:
    """Live GPS-fix-finding diagnostic: streams satellite count while someone repositions the
    antenna, so a good mounting position can be found without waiting for a full measurement run.

    Only meaningful in Waiting Mode (see RemoteCommandDispatcher/decision record 0020) - opens
    its own `GNSSReceiver` on a background thread (nothing else holds one open in Waiting Mode,
    same reasoning as the self-test's own GNSS use) and repeatedly calls `read_fix()`/
    `read_satellites_in_view()`, posting a small status payload to `config.status_url` roughly
    every `report_interval_s`. Reports satellites-in-view (not just a fix's own satellite count)
    so the count moves while someone is still searching for a fix, not only after one exists.
    Auto-stops
    after `config.timeout_s` if `stop()` is never called, so a forgotten run doesn't keep the
    GNSS connection and status POSTs going indefinitely. `start()`/`stop()` are safe to call from
    any thread; the GNSS connection is always closed cleanly when the polling thread exits,
    whether by timeout or by `stop()`.

    Also records every real fix it finds into the shared `gps_fix_status` tracker (the same one
    `MovementGate`/`Collector` feed - see `status/backend_status.py`), so the OLED screen and
    heartbeat reflect what this diagnostic sees rather than sitting at "no fix" the whole time
    it's running - `_wait_for_remote_command()` never touches `MovementGate` in Waiting Mode
    (movement isn't observed there at all, per decision record 0020), so without this, this
    diagnostic would be the only thing on the device that actually knows a fix exists.
    """

    def __init__(
        self, gnss_config: GnssConfig, config: GpsFixTestConfig, device_id: str, device_key: str,
        gps_fix_status: GpsFixStatusTracker,
    ):
        self._logger = logging.getLogger(__name__)
        self._gnss_config = gnss_config
        self._config = config
        self._device_id = device_id
        self._device_key = device_key
        self._gps_fix_status = gps_fix_status
        self._lock = threading.Lock()
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def is_running(self) -> bool:
        """Reports whether a polling run is currently in progress. Thread-safe."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Starts the polling loop on a background thread, unless one is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._logger.info("run-gps-fix-test-now ignored - a run is already in progress.")
                return
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._thread = threading.Thread(target=self._run, args=(stop_event,), daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Signals the polling thread to stop and waits for it to exit. Safe to call if not running."""
        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join()

    def _run(self, stop_event: threading.Event) -> None:
        gnss = create_gnss_receiver(self._gnss_config)
        start = time.monotonic()
        num_satellites = 0
        has_fix = False
        try:
            gnss.open()
            while True:
                elapsed_s = time.monotonic() - start
                if elapsed_s >= self._config.timeout_s:
                    self._logger.info(
                        "run-gps-fix-test-now auto-stopped after %.0fs (gps_fix_test.timeout_s).",
                        elapsed_s,
                    )
                    return

                has_fix, num_satellites = self._poll(gnss, has_fix, num_satellites)
                self._report(has_fix, num_satellites, elapsed_s)
                if stop_event.wait(self._config.report_interval_s):
                    return
        finally:
            gnss.close()

    def _poll(self, gnss: GNSSReceiver, has_fix: bool, num_satellites: int) -> tuple[bool, int]:
        """Reads lines until a fix turns up or the budget runs out, preferring a fix over any
        satellites-in-view count read from the same budget.

        A real NMEA burst carries several GSV sentences per single GGA sentence, so a fix
        sitting later in the budget must not lose to a GSV sentence encountered first - that
        would report "no fix" even while one is continuously held, only because the diagnostic
        happened to read a GSV line before the GGA line came back around. So read_fix() is
        checked on every line across the whole budget; only once the budget is exhausted without
        a fix does the last satellites-in-view count seen during it get reported instead, so the
        report still moves while positioning the antenna, rather than sitting frozen at 0 until a
        fix suddenly appears. If neither turns up within the budget (e.g. a stretch of
        RMC/VTG-only lines), the previous cycle's values carry over rather than resetting to
        0/no-fix.
        """
        latest_sats_in_view = None
        for _ in range(_MAX_READS_PER_REPORT):
            fix = gnss.read_fix()
            if fix is not None:
                self._gps_fix_status.record_fix(fix)
                return True, fix.num_satellites

            sats_in_view = gnss.read_satellites_in_view()
            if sats_in_view is not None:
                latest_sats_in_view = sats_in_view

        if latest_sats_in_view is not None:
            return False, latest_sats_in_view
        return has_fix, num_satellites

    def _report(self, has_fix: bool, num_satellites: int, elapsed_s: float) -> None:
        self._logger.info(
            "gps-fix-test: num_satellites=%d has_fix=%s elapsed_s=%.0f",
            num_satellites, has_fix, elapsed_s,
        )
        if not self._is_usable():
            return

        payload = {
            "device_id": self._device_id,
            "num_satellites": num_satellites,
            "has_fix": has_fix,
            "elapsed_s": round(elapsed_s),
        }
        headers = {"Content-Type": "application/json", "X-Device-ID": self._device_id}
        if self._device_key:
            headers["X-Device-Key"] = self._device_key

        request = urllib.request.Request(
            self._config.status_url, data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_STATUS_POST_TIMEOUT_S):
                pass
        except (OSError, HTTPException) as e:
            self._logger.warning("Could not post gps-fix-test status to backend: %s", e)

    def _is_usable(self) -> bool:
        if not self._config.status_url:
            return False
        if not self._config.status_url.startswith("https://"):
            self._logger.error(
                "gps_fix_test.status_url needs an https:// url but is configured with %r - "
                "reporting locally only.", self._config.status_url,
            )
            return False
        return True
