import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path

from measurement_software.core.config import DeviceConfig, LatencyTestConfig
from measurement_software.core.flent_runner import FlentRunner, FlentSummary
from measurement_software.core.latency_result import LatencyResult, LatencyTestType
from measurement_software.core.movement_tracker import Movement, MovementTracker
from measurement_software.core.run_log import RunLog


@dataclass(frozen=True)
class LatencyTest:
    """One kind of latency test: which flent test to run, for how long, and how often."""

    test_type: LatencyTestType
    flent_test: str
    length_s: int
    interval_s: float


@dataclass
class Cadence:
    """A latency test and the moment it next comes due."""

    test: LatencyTest
    due_at: float


class LatencyTester:
    """Runs flent latency tests on their own cadences, on a background thread, while the vehicle moves.

    Both cadences share the one thread on purpose: a load test saturates the link for its whole
    length, and a baseline ping running alongside it would measure that load instead of the idle
    link it is supposed to describe.

    Kept out of the collection loop for the same reason it needs its own thread - a load test
    takes tens of seconds, and the collection loop has to keep capturing measurements throughout.
    """

    def __init__(self, config: LatencyTestConfig, movement: MovementTracker,
                 run_log: RunLog[LatencyResult], device: DeviceConfig):
        self._logger = logging.getLogger(__name__)
        self._config = config
        self._movement = movement
        self._run_log = run_log
        self._device = device
        self._runner = FlentRunner(config)
        self._cadences: list[Cadence] = []
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def log_path(self) -> Path | None:
        """The log this run's results are being written to, or None if no testing is happening."""
        return self._run_log.path

    def start(self) -> None:
        """Starts testing, unless latency testing is disabled or has nowhere to test against."""
        if not self._is_usable():
            return
        self._run_log.open()
        self._cadences = [Cadence(test, time.time()) for test in self._scheduled_tests()]
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the testing thread to stop, waits for the test in flight to finish, and closes the log."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self._run_log.close()

    def _is_usable(self) -> bool:
        """Reports whether this run should run latency tests, saying loudly why if it shouldn't."""
        if not self._config.enabled:
            self._logger.info("Latency testing is disabled.")
            return False
        if not self._config.host:
            self._logger.error(
                "Latency testing is enabled but no host is configured - staying idle for this run."
            )
            return False
        return True

    def _scheduled_tests(self) -> list[LatencyTest]:
        """The two tests to run, cheapest first, so a tick where both come due measures the idle link first."""
        return [
            LatencyTest(
                test_type=LatencyTestType.BASELINE,
                flent_test=self._config.baseline_test,
                length_s=self._config.baseline_length_s,
                interval_s=self._config.baseline_interval_s,
            ),
            LatencyTest(
                test_type=LatencyTestType.UNDER_LOAD,
                flent_test=self._config.load_test,
                length_s=self._config.load_length_s,
                interval_s=self._config.load_interval_s,
            ),
        ]

    def _run(self, stop_event: threading.Event) -> None:
        """Checks for due tests on the configured poll interval until stopped."""
        while not stop_event.wait(self._config.poll_interval_s):
            self._run_due_tests()

    def _run_due_tests(self) -> None:
        for cadence in self._cadences:
            if time.time() >= cadence.due_at:
                self._run_test(cadence)

    def _run_test(self, cadence: Cadence) -> None:
        """Runs one due test, or leaves it due if the vehicle is standing still.

        A test run while the vehicle is parked covers no new route and still spends the data
        and battery a load test costs, so a stationary tick postpones the test rather than
        consuming it (see decision record 0004).
        """
        start = self._movement.moved_within(self._config.movement_window_s)
        if start is None:
            self._logger.debug("Vehicle is not moving - %s latency test stays due.", cadence.test.test_type)
            return

        start_timestamp = utc_timestamp()
        summary = self._runner.run(cadence.test.flent_test, cadence.test.length_s)
        cadence.due_at = time.time() + cadence.test.interval_s
        if summary is None:
            return

        end = self._movement.latest() or start
        self._run_log.append([self._to_result(cadence.test, summary, start, end, start_timestamp)])

    def _to_result(self, test: LatencyTest, summary: FlentSummary, start: Movement, end: Movement,
                   start_timestamp: str) -> LatencyResult:
        """Tags a summary with the stretch of road it was measured over and which test produced it."""
        under_load = test.test_type is LatencyTestType.UNDER_LOAD
        return LatencyResult(
            test_type=test.test_type,
            flent_test=test.flent_test,
            device_id=self._device.device_id,
            mission_type=self._device.mission_type,
            start_timestamp=start_timestamp,
            end_timestamp=utc_timestamp(),
            start_fix=start.fix,
            end_fix=end.fix,
            baseline_rtt_ms=None if under_load else summary.rtt_ms,
            rtt_under_load_ms=summary.rtt_ms if under_load else None,
            rtt_p99_ms=summary.rtt_p99_ms,
            download_mbits_s=summary.download_mbits_s,
            upload_mbits_s=summary.upload_mbits_s,
        )


def utc_timestamp() -> str:
    """The current time in the same shape the collector stamps its datapoints with."""
    return f"{datetime.now(UTC).isoformat()}Z"
