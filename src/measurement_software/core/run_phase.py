import threading
import time
from enum import StrEnum
from typing import Callable


class RunPhaseState(StrEnum):
    """The small, stable vocabulary of lifecycle stages a boot cycle moves through.

    Usable both for heartbeat/backend consumption and the on-device screen - every stage a run
    visibly goes through gets one of these rather than an ad hoc string.

    STARTING_UP: the process has started but has not begun its own work yet; the initial phase.
    RECOVERING_PENDING_DATA: cleaning up after a previous run that was cut short, before this
    run's own state is decided.
    WAITING_FOR_GPS_FIX: the receiver has not reported a first fix yet (`FirstFixWaiter`). Every
    path into measuring passes through this phase, in Manual Mode as well as Auto Mode, so
    `ACTIVE_MEASURING` always means a fix exists.
    WAITING_FOR_MOVEMENT: Auto Mode with the movement gate: a fix exists, but sustained
    displacement from it hasn't been confirmed yet (`MovementGate`).
    MANUAL_MODE_IDLE: Manual Mode (`system.mode = "manual"`) is blocked on an explicit remote
    command - movement is not observed at all in this mode.
    RECOVERY_IDLE: the one-shot recovery boot after the crash escalation
    (`core/crash_policy.py`): like MANUAL_MODE_IDLE it blocks on an explicit remote command, but
    it is a state, not a mode, and applies to this boot only. The heartbeat carries the crash
    details while it lasts.
    ACTIVE_MEASURING: a `Collector` run is underway; the modem is open and quectel-CM is running.
    FINALIZING: the run log is being finalized and uploaded.
    DONE: the run has finished and, if configured, shutdown is imminent.
    STOPPING: SIGTERM arrived (`systemctl stop`, a system shutdown): the run's data is being
    finalized and uploaded, then the process ends - no shutdown or reboot of the board follows.
    SHUTTING_DOWN: the unified shutdown sequence (`core/system.py::shutdown()`) is running - set
    just long enough to send one last best-effort heartbeat before the board loses power.
    REBOOTING: the analogous reboot sequence (`core/system.py::reboot()`) is running - the board
    stays powered throughout, so this phase (unlike SHUTTING_DOWN) does get observed again once
    the device is back up.
    CRITICAL_ERROR: maintenance mode (`core/maintenance_mode.py`) - unhandled errors kept
    recurring even in the recovery state, so nothing but the cellular link and the heartbeat runs, and
    the device needs debugging over SSH.

    Auto Mode's connection and heartbeat already start at
    RECOVERING_PENDING_DATA rather than only once ACTIVE_MEASURING begins, so
    an operator can tell "still waiting, working fine" from "stuck" before a run even starts.
    """

    STARTING_UP = "starting_up"
    RECOVERING_PENDING_DATA = "recovering_pending_data"
    WAITING_FOR_GPS_FIX = "waiting_for_gps_fix"
    WAITING_FOR_MOVEMENT = "waiting_for_movement"
    MANUAL_MODE_IDLE = "manual_mode_idle"
    RECOVERY_IDLE = "recovery_idle"
    ACTIVE_MEASURING = "active_measuring"
    FINALIZING = "finalizing"
    DONE = "done"
    STOPPING = "stopping"
    SHUTTING_DOWN = "shutting_down"
    REBOOTING = "rebooting"
    CRITICAL_ERROR = "critical_error"


IDLE_PHASES = frozenset({RunPhaseState.MANUAL_MODE_IDLE, RunPhaseState.RECOVERY_IDLE})
"""Phases in which the device blocks on an explicit remote command (`_run_manual_mode()`)."""


class RunPhase:
    """Mutable holder for the run's current lifecycle-stage label, for the status display and
    heartbeat to read.

    Accepts either a RunPhaseState (the named states above) or a plain string label for anything
    not yet covered by that vocabulary - callers set it at each transition. Readers only ever see
    RunPhase.get(), a plain string.
    """

    def __init__(self, initial: str = RunPhaseState.STARTING_UP):
        self._label = initial

    def set(self, label: str) -> None:
        self._label = label

    def get(self) -> str:
        return self._label


_SHORT_LABELS: dict[str, str] = {
    RunPhaseState.STARTING_UP: "Starting up",
    RunPhaseState.RECOVERING_PENDING_DATA: "Recovering data",
    RunPhaseState.WAITING_FOR_GPS_FIX: "No GPS fix",
    RunPhaseState.WAITING_FOR_MOVEMENT: "Waiting: move",
    RunPhaseState.MANUAL_MODE_IDLE: "Idle (manual)",
    RunPhaseState.RECOVERY_IDLE: "Idle (recovery)",
    RunPhaseState.ACTIVE_MEASURING: "Measuring",
    RunPhaseState.FINALIZING: "Finalizing",
    RunPhaseState.DONE: "Done",
    RunPhaseState.STOPPING: "STOPPING",
    RunPhaseState.SHUTTING_DOWN: "SHUTTING DOWN",
    RunPhaseState.REBOOTING: "REBOOTING",
    RunPhaseState.CRITICAL_ERROR: "CRITICAL ERROR",
}


def short_phase_label(phase: str) -> str:
    """Maps a RunPhaseState value to a short, fixed-vocabulary label for the on-device screen.

    Falls back to the phase string itself for anything outside that vocabulary (e.g. an ad hoc
    label set by a caller) - callers that display the result are
    still responsible for bounding that fallback to the screen's width, same as every other line.
    """
    return _SHORT_LABELS.get(phase, phase)


class ManualModeLoop:
    """Blocks a boot in Manual Mode until an explicit remote command says what to do next.

    Manual Mode boots with the cellular link open and the heartbeat already running (see
    main.py), then does nothing further of its own accord - movement is not observed here at
    all. Only explicit heartbeat commands (RemoteCommandDispatcher)
    act here, via the same thread-safe request/flag pattern Collector already uses for
    shutdown-now/sim-reset-now: a flag is set from the heartbeat thread and checked here on the
    thread that actually owns the wait. start-measuring-now ends the wait; run-selftest-now runs
    the given callback and then keeps waiting, since a self-test doesn't leave Manual Mode.
    """

    def __init__(self, poll_interval_s: float = 1.0):
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._start_measuring_requested = False
        self._run_selftest_requested = False

    def request_start_measuring(self) -> None:
        """Asks the Manual Mode loop to end and transition into measuring. Thread-safe."""
        with self._lock:
            self._start_measuring_requested = True

    def cancel_start_request(self) -> None:
        """Forgets a pending start request, so the next wait() blocks again. Thread-safe.

        For a start that was aborted before measuring began (stop-measuring-now while waiting for
        the first GPS fix): a start-measuring-now sent again in the meantime must not restart it.
        """
        with self._lock:
            self._start_measuring_requested = False

    def request_run_selftest(self) -> None:
        """Asks the Manual Mode loop to run a self-test without leaving Manual Mode. Thread-safe."""
        with self._lock:
            self._run_selftest_requested = True

    def wait(
        self, on_run_selftest: Callable[[], None] | None = None,
        on_check_remote_shutdown: Callable[[], None] | None = None,
    ) -> None:
        """Blocks until request_start_measuring() has been called.

        The request is consumed: once this returns, the next wait() blocks until another
        request_start_measuring().

        A pending run_selftest request runs `on_run_selftest` (on this thread, the one that owns
        the wait) and then keeps waiting, rather than ending the wait. `on_check_remote_shutdown`
        (Collector.check_remote_shutdown_or_reboot, wired in by main.py) is called once per
        iteration and is expected to raise to end the wait - Manual Mode is otherwise unaware of
        shutdown-now/reboot-now, which would then only ever reach Collector.collect() if the
        device happened to already be measuring.
        """
        while True:
            if on_check_remote_shutdown is not None:
                on_check_remote_shutdown()
            with self._lock:
                if self._start_measuring_requested:
                    self._start_measuring_requested = False
                    return
                run_selftest = self._run_selftest_requested
                self._run_selftest_requested = False
            if run_selftest and on_run_selftest is not None:
                on_run_selftest()
                continue
            time.sleep(self._poll_interval_s)
