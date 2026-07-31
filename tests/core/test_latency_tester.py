import json
import logging
import time
from pathlib import Path

import pytest

from measurement_software.core.config import DeviceConfig, LatencyTestConfig
from measurement_software.core.flent_runner import FlentSummary
from measurement_software.core.latency_result import LATENCY_FILE_PREFIX, LatencyResult
from measurement_software.core.latency_tester import LatencyTester
from measurement_software.core.movement_tracker import MovementTracker
from measurement_software.core.run_log import RunLog
from measurement_software.gnss.gnss_receiver import GNSSFix, Position


class FakeClock:
    """Controls `time.time()` as seen by the latency tester."""

    def __init__(self):
        self.now = 0.0

    def time(self) -> float:
        return self.now


class FakeFlentRunner:
    """Stands in for the flent subprocess, recording every test it was asked to run."""

    def __init__(self, summaries: list[FlentSummary | None] | None = None):
        self.summaries = summaries
        self.runs: list[tuple[str, int]] = []
        self.on_run = None

    def run(self, test: str, length_s: int) -> FlentSummary | None:
        self.runs.append((test, length_s))
        if self.on_run is not None:
            self.on_run()
        if self.summaries is None:
            return FlentSummary(rtt_ms=22.0, rtt_p99_ms=61.0)
        return self.summaries[min(len(self.runs), len(self.summaries)) - 1]


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake_clock = FakeClock()
    monkeypatch.setattr("measurement_software.core.latency_tester.time.time", fake_clock.time)
    return fake_clock


@pytest.fixture
def flent(monkeypatch) -> FakeFlentRunner:
    fake = FakeFlentRunner()
    monkeypatch.setattr("measurement_software.core.latency_tester.FlentRunner", lambda config: fake)
    return fake


@pytest.fixture
def device() -> DeviceConfig:
    return DeviceConfig(device_id="test-device", mission_type="ground")


@pytest.fixture
def run_log(tmp_path) -> RunLog[LatencyResult]:
    return RunLog[LatencyResult](tmp_path / "queue", prefix=LATENCY_FILE_PREFIX)


@pytest.fixture
def movement() -> MovementTracker:
    return MovementTracker()


def config(**overrides) -> LatencyTestConfig:
    defaults = dict(
        enabled=True,
        host="testserver.example.org",
        baseline_test="ping",
        baseline_interval_s=60.0,
        baseline_length_s=10,
        load_test="rrul",
        load_interval_s=900.0,
        load_length_s=60,
        poll_interval_s=5.0,
        movement_window_s=30.0,
    )
    return LatencyTestConfig(**(defaults | overrides))


def fix(latitude: float = 1.0) -> GNSSFix:
    return GNSSFix(position=Position(latitude=latitude, longitude=2.0), num_satellites=8)


def read_results(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestStartStop:
    def test_disabled_runs_no_tests_and_writes_no_log(self, clock, flent, movement, run_log, device, caplog):
        tester = LatencyTester(config(enabled=False), movement, run_log, device)

        with caplog.at_level(logging.INFO):
            tester.start()
            tester.stop()

        assert tester.log_path is None
        assert flent.runs == []
        assert "disabled" in caplog.text

    def test_enabled_without_a_host_stays_idle_and_says_so(self, clock, flent, movement, run_log, device, caplog):
        tester = LatencyTester(config(host=""), movement, run_log, device)

        with caplog.at_level(logging.ERROR):
            tester.start()
            tester.stop()

        assert tester.log_path is None
        assert flent.runs == []
        assert "no host is configured" in caplog.text

    def test_stop_without_start_is_a_no_op(self, clock, flent, movement, run_log, device):
        LatencyTester(config(), movement, run_log, device).stop()

    def test_opens_a_log_of_its_own_that_the_uploader_ignores_until_finalized(
            self, clock, flent, movement, run_log, device):
        tester = LatencyTester(config(), movement, run_log, device)

        tester.start()
        tester.stop()

        assert tester.log_path.name.startswith(LATENCY_FILE_PREFIX)
        assert list(tester.log_path.parent.glob("*.json")) == []

    def test_runs_due_tests_on_its_own_thread(self, flent, movement, run_log, device):
        movement.record_movement(fix())
        tester = LatencyTester(config(poll_interval_s=0.01), movement, run_log, device)

        tester.start()
        try:
            deadline = time.monotonic() + 5.0
            while not flent.runs:
                if time.monotonic() > deadline:
                    pytest.fail("No latency test was run by the background thread")
                time.sleep(0.01)
        finally:
            tester.stop()

        assert flent.runs[0] == ("ping", 10)


class TestMovementGating:
    def test_runs_nothing_while_the_vehicle_has_never_moved(self, clock, flent, movement, run_log, device):
        tester = LatencyTester(config(), movement, run_log, device)
        tester.start()

        tester._run_due_tests()

        assert flent.runs == []

    def test_runs_nothing_while_the_vehicle_stands_still(self, clock, flent, movement, run_log, device):
        movement.record_movement(fix())
        tester = LatencyTester(config(movement_window_s=30.0), movement, run_log, device)
        tester.start()
        clock.now = 31.0

        tester._run_due_tests()

        assert flent.runs == []

    def test_a_test_skipped_while_parked_runs_as_soon_as_the_vehicle_moves_again(
            self, clock, flent, movement, run_log, device):
        tester = LatencyTester(config(), movement, run_log, device)
        tester.start()
        tester._run_due_tests()

        movement.record_movement(fix())
        tester._run_due_tests()

        # Still due rather than consumed, so no interval was skipped by standing still.
        assert flent.runs == [("ping", 10), ("rrul", 60)]


class TestCadences:
    def test_runs_both_tests_when_both_are_due(self, clock, flent, movement, run_log, device):
        movement.record_movement(fix())
        tester = LatencyTester(config(), movement, run_log, device)
        tester.start()

        tester._run_due_tests()

        assert flent.runs == [("ping", 10), ("rrul", 60)]

    def test_holds_each_test_back_for_its_own_interval(self, clock, flent, movement, run_log, device):
        movement.record_movement(fix())
        tester = LatencyTester(config(baseline_interval_s=60.0, load_interval_s=900.0), movement, run_log, device)
        tester.start()
        tester._run_due_tests()

        clock.now = 60.0
        movement.record_movement(fix())
        tester._run_due_tests()

        # The cheap baseline comes round again long before the load test does.
        assert flent.runs == [("ping", 10), ("rrul", 60), ("ping", 10)]

    def test_the_next_run_is_measured_from_the_end_of_the_last_one(self, clock, flent, movement, run_log, device):
        def keep_driving_for_30s() -> None:
            clock.now += 30.0
            movement.record_movement(fix())

        movement.record_movement(fix())
        tester = LatencyTester(config(baseline_interval_s=60.0, load_interval_s=10_000.0), movement, run_log, device)
        tester.start()

        # Two tests occupy the thread for 30 s each, so measuring the baseline's next run from
        # when it started rather than from when it finished would leave it due again at once.
        flent.on_run = keep_driving_for_30s
        tester._run_due_tests()
        flent.on_run = None
        tester._run_due_tests()

        assert flent.runs == [("ping", 10), ("rrul", 60)]

    def test_a_failed_test_waits_for_its_next_interval_rather_than_retrying_at_once(
            self, clock, flent, movement, run_log, device):
        movement.record_movement(fix())
        flent.summaries = [None]
        tester = LatencyTester(config(load_interval_s=10_000.0), movement, run_log, device)
        tester.start()
        tester._run_due_tests()

        clock.now = 1.0
        tester._run_due_tests()

        assert flent.runs == [("ping", 10), ("rrul", 60)]


class TestResults:
    def test_records_a_baseline_result_over_the_stretch_the_vehicle_covered(
            self, clock, flent, movement, run_log, device):
        flent.summaries = [FlentSummary(rtt_ms=22.0, rtt_p99_ms=61.0)]
        movement.record_movement(fix(latitude=1.0))
        tester = LatencyTester(config(load_interval_s=10_000.0), movement, run_log, device)
        tester.start()
        flent.on_run = lambda: movement.record_movement(fix(latitude=2.0))

        tester._run_due_tests()
        tester.stop()

        [result] = [r for r in read_results(tester.log_path) if r["test_type"] == "baseline"]
        assert result["flent_test"] == "ping"
        assert result["baseline_rtt_ms"] == 22.0
        assert result["rtt_under_load_ms"] is None
        assert result["rtt_p99_ms"] == 61.0
        assert result["start_fix"]["position"]["latitude"] == 1.0
        assert result["end_fix"]["position"]["latitude"] == 2.0
        assert result["start_timestamp"].endswith("Z")
        assert result["end_timestamp"].endswith("Z")
        assert result["device_id"] == "test-device"
        assert result["mission_type"] == "ground"

    def test_records_a_load_result_as_latency_under_load(self, clock, flent, movement, run_log, device):
        flent.summaries = [
            FlentSummary(rtt_ms=22.0),
            FlentSummary(rtt_ms=140.0, rtt_p99_ms=450.0, download_mbits_s=48.0, upload_mbits_s=9.0),
        ]
        movement.record_movement(fix())
        tester = LatencyTester(config(), movement, run_log, device)
        tester.start()

        tester._run_due_tests()
        tester.stop()

        [result] = [r for r in read_results(tester.log_path) if r["test_type"] == "under_load"]
        assert result["flent_test"] == "rrul"
        assert result["baseline_rtt_ms"] is None
        assert result["rtt_under_load_ms"] == 140.0
        assert result["download_mbits_s"] == 48.0
        assert result["upload_mbits_s"] == 9.0

    def test_records_nothing_for_a_test_that_produced_no_numbers(self, clock, flent, movement, run_log, device):
        flent.summaries = [None]
        movement.record_movement(fix())
        tester = LatencyTester(config(), movement, run_log, device)
        tester.start()

        tester._run_due_tests()
        tester.stop()

        assert read_results(tester.log_path) == []

    def test_every_result_is_on_disk_before_the_log_is_closed(self, clock, flent, movement, run_log, device):
        movement.record_movement(fix())
        tester = LatencyTester(config(), movement, run_log, device)
        tester.start()

        tester._run_due_tests()

        # A power cut mid-run gets no chance to flush, so a finished test must already be readable.
        assert [r["test_type"] for r in read_results(tester.log_path)] == ["baseline", "under_load"]
