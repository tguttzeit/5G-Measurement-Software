from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

from measurement_software.core.config import CollectorConfig, DisplayConfig
from measurement_software.core.run_phase import RunPhase, short_phase_label
from measurement_software.status.backend_status import (
    GpsFixStatus,
    GpsFixStatusTracker,
    RunStatus,
    RunStatusTracker,
)

if TYPE_CHECKING:
    from measurement_software.displays.display import Display

# The SSD1306 this is built for is 128x64px; at the default font (~6px/char, 10px row height)
# that's ~21 characters per row and ~6 rows before content runs off the physical screen -
# confirmed while scoping issue #39. Any future display with different geometry would need its
# own budget, but there is only the one concrete display driver today.
MAX_LINE_CHARS = 21
MAX_LINES = 6

# Each entry names a testing/non-default config flag and how to tell it's active. Adding a
# future testing flag (e.g. a debug sampling mode) is one more entry here, not a change to
# how the display renders them.
_SPECIAL_CONFIG_FLAGS: list[tuple[str, Callable[[CollectorConfig], bool]]] = [
    ("GPS disabled", lambda collector_config: not collector_config.gps_enabled),
]

def active_special_config_flags(collector_config: CollectorConfig) -> list[str]:
    """Returns the labels of every testing/non-default config flag currently active."""
    return [label for label, is_active in _SPECIAL_CONFIG_FLAGS if is_active(collector_config)]

def format_gps_fix_status(gps_fix_status: GpsFixStatus) -> str:
    """Renders GPS fix status as one short line: whether a fix exists, satellites, and its age."""
    if not gps_fix_status.has_fix:
        return "GPS: no fix"
    details = []
    if gps_fix_status.num_satellites is not None:
        details.append(f"{gps_fix_status.num_satellites}sat")
    if gps_fix_status.fix_age_s is not None:
        details.append(f"{int(gps_fix_status.fix_age_s)}s")
    return " ".join(["GPS: fix", *details]) if details else "GPS: fix"

def build_status_message(
    phase: str, run_status: RunStatus, gps_fix_status: GpsFixStatus, special_config_flags: list[str]
) -> str:
    """Renders the display's content: phase, blocking condition, GPS status, and data quality.

    Lines are ordered by priority, highest first, so `_bound_to_line_budget()` can safely drop
    from the end without ever losing the phase or a broken-pipeline warning - reuses RunStatus's
    own cumulative counters, the same ones the backend heartbeat reports, so the display and the
    heartbeat always agree.
    """
    lines = [short_phase_label(phase)]
    if run_status.pipeline_broken:
        lines.append("PIPELINE BROKEN")
    lines.append(format_gps_fix_status(gps_fix_status))
    lines.append(f"OK {run_status.good}  Bad {run_status.bad}  Inv {run_status.invalid}")
    lines.append(f"Total {run_status.datapoints_total}")
    lines.append(", ".join(special_config_flags) if special_config_flags else "No special config")
    return "\n".join(_bound_to_line_budget(lines))

def _bound_to_line_budget(lines: list[str]) -> list[str]:
    """Keeps at most MAX_LINES lines (dropping lowest-priority ones from the end first) and
    truncates every surviving line to MAX_LINE_CHARS, so nothing can run off the physical screen.
    """
    return [_truncate(line) for line in lines[:MAX_LINES]]

def _truncate(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS]


class StatusDisplayUpdater:
    """Refreshes the status display on a background thread while a run is going on.

    The data-quality summary changes continuously as measurements come in, so the display
    needs its own refresh cadence independent of the position-triggered collection loop - the
    same reasoning that gives HeartbeatSender and KeepModemAliveSender their own threads.
    """

    def __init__(self, display: Display, run_phase: RunPhase, gps_fix_status: GpsFixStatusTracker,
                 run_status: RunStatusTracker, collector_config: CollectorConfig, config: DisplayConfig):
        self._display = display
        self._run_phase = run_phase
        self._gps_fix_status = gps_fix_status
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
            self._run_phase.get(), self._run_status.status(), self._gps_fix_status.status(),
            self._special_config_flags,
        )
        self._display.show(message)
