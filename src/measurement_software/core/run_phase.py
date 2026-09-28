import threading
import time
from enum import StrEnum
from typing import Callable


class RunPhaseState(StrEnum):
    """The small, stable vocabulary of lifecycle stages a boot cycle moves through.

    Usable both for heartbeat/backend consumption and (see issue #37) the on-device screen -
    every stage a run visibly goes through gets one of these rather than an ad hoc string.

    RECOVERING_PENDING_DATA: cleaning up after a previous run that was cut short, before this
    run's own state is decided.
    WAITING_FOR_GPS_FIX: `MovementGate` has no fix yet at all - no reference position exists to
    even judge movement against.
    WAITING_FOR_MOVEMENT: `MovementGate` has a fix, but sustained displacement from it hasn't
    been confirmed yet.
    WAITING_MODE_IDLE: Waiting Mode (`system.mode = "waiting"`) is blocked on an explicit remote
    command - movement is not observed at all in this mode (decision record 0020).
    ACTIVE_MEASURING: a `Collector` run is underway; the modem is open and quectel-CM is running.
    FINALIZING: the run log is being finalized and uploaded.
    DONE: the run has finished and, if configured, shutdown is imminent.

    See decision record 0021 for why Auto Mode's connection/heartbeat now starts at
    RECOVERING_PENDING_DATA/WAITING_FOR_GPS_FIX rather than only once ACTIVE_MEASURING begins.
    """

    RECOVERING_PENDING_DATA = "recovering_pending_data"
    WAITING_FOR_GPS_FIX = "waiting_for_gps_fix"
    WAITING_FOR_MOVEMENT = "waiting_for_movement"
    WAITING_MODE_IDLE = "waiting_mode_idle"
    ACTIVE_MEASURING = "active_measuring"
    FINALIZING = "finalizing"
    DONE = "done"


class RunPhase:
    """Mutable holder for the run's current lifecycle-stage label, for the status display and
    heartbeat to read.

    Accepts either a RunPhaseState (the named states above) or a plain string label for anything
    not yet covered by that vocabulary - callers set it at each transition. Readers only ever see
    RunPhase.get(), a plain string.
    """

    def __init__(self, initial: str = "starting up"):
        self._label = initial

    def set(self, label: str) -> None:
        self._label = label

    def get(self) -> str:
        return self._label


_SHORT_LABELS: dict[str, str] = {
    RunPhaseState.RECOVERING_PENDING_DATA: "Recovering data",
    RunPhaseState.WAITING_FOR_GPS_FIX: "No GPS fix",
    RunPhaseState.WAITING_FOR_MOVEMENT: "Waiting: move",
    RunPhaseState.WAITING_MODE_IDLE: "Idle (waiting)",
    RunPhaseState.ACTIVE_MEASURING: "Measuring",
    RunPhaseState.FINALIZING: "Finalizing",
    RunPhaseState.DONE: "Done",
}


def short_phase_label(phase: str) -> str:
    """Maps a RunPhaseState value to a short, fixed-vocabulary label for the on-device screen.

    Falls back to the phase string itself for anything outside that vocabulary (e.g. RunPhase's
    "starting up" default, before main.py ever calls set()) - callers that display the result are
    still responsible for bounding that fallback to the screen's width, same as every other line.
    """
    return _SHORT_LABELS.get(phase, phase)


class WaitingLoop:
    """Blocks a boot in Waiting Mode until an explicit remote command says what to do next.

    Waiting Mode boots with the cellular link open and the heartbeat already running (see
    main.py), then does nothing further of its own accord - movement is not observed here at
    all, per decision record 0020. Only explicit heartbeat commands (RemoteCommandDispatcher)
    act here, via the same thread-safe request/flag pattern Collector already uses for
    shutdown-now/sim-reset-now: a flag is set from the heartbeat thread and checked here on the
    thread that actually owns the wait. start-measuring-now ends the wait; run-selftest-now runs
    the given callback and then keeps waiting, since a self-test doesn't leave Waiting Mode.
    """

    def __init__(self, poll_interval_s: float = 1.0):
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._start_measuring_requested = False
        self._run_selftest_requested = False

    def request_start_measuring(self) -> None:
        """Asks the waiting loop to end and transition into measuring. Thread-safe."""
        with self._lock:
            self._start_measuring_requested = True

    def request_run_selftest(self) -> None:
        """Asks the waiting loop to run a self-test without leaving Waiting Mode. Thread-safe."""
        with self._lock:
            self._run_selftest_requested = True

    def wait(self, on_run_selftest: Callable[[], None] | None = None) -> None:
        """Blocks until request_start_measuring() has been called.

        A pending run_selftest request runs `on_run_selftest` (on this thread, the one that owns
        the wait) and then keeps waiting, rather than ending the wait.
        """
        while True:
            with self._lock:
                if self._start_measuring_requested:
                    return
                run_selftest = self._run_selftest_requested
                self._run_selftest_requested = False
            if run_selftest and on_run_selftest is not None:
                on_run_selftest()
                continue
            time.sleep(self._poll_interval_s)
