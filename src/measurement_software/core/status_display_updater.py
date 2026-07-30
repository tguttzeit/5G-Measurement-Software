import threading

from measurement_software.core.config import CollectorConfig, DisplayConfig
from measurement_software.core.display_status import active_special_config_flags, build_status_message
from measurement_software.core.run_phase import RunPhase
from measurement_software.core.run_status import RunStatusTracker
from measurement_software.displays.display import Display


class StatusDisplayUpdater:
    """Refreshes the status display on a background thread while a run is going on.

    The data-quality summary changes continuously as measurements come in, so the display
    needs its own refresh cadence independent of the position-triggered collection loop - the
    same reasoning that gives HeartbeatSender and KeepModemAliveSender their own threads.
    """

    def __init__(self, display: Display, run_phase: RunPhase, run_status: RunStatusTracker,
                 collector_config: CollectorConfig, config: DisplayConfig):
        self._display = display
        self._run_phase = run_phase
        self._run_status = run_status
        self._special_config_flags = active_special_config_flags(collector_config)
        self._config = config
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Starts refreshing the display, unless it's disabled."""
        if not self._config.enabled:
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the refresh thread to stop and waits for it to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self, stop_event: threading.Event) -> None:
        """Refreshes the display immediately, then once per configured interval."""
        while True:
            self._refresh()
            if stop_event.wait(self._config.refresh_interval_s):
                return

    def _refresh(self) -> None:
        message = build_status_message(
            self._run_phase.get(), self._run_status.status(), self._special_config_flags
        )
        self._display.show(message)
