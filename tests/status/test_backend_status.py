import logging
from pathlib import Path

import pytest

from measurement_software.core.config import (
    AppConfig,
    BackendConfig,
    CollectorConfig,
    DeviceConfig,
    DisplayConfig,
    FanConfig,
    GnssConfig,
    GpsFixTestConfig,
    HeartbeatConfig,
    LatencyTestConfig,
    LoggingConfig,
    ModemConfig,
    MovementGateConfig,
    QualityThresholds,
    QuectelCmConfig,
    RunStatusConfig,
    SelftestConfig,
    StorageConfig,
    SystemConfig,
    UploaderConfig,
)
from measurement_software.core.system import FanController
from measurement_software.gnss.gnss_receiver import GNSSFix, Position
from measurement_software.modems.modem import CellSample
from measurement_software.status.backend_status import (
    ConfigStatusReporter,
    GpsFixStatus,
    GpsFixStatusTracker,
    RunStatusTracker,
    SampleQuality,
    StorageStatusReporter,
    classify_sample,
)


def good_lte_sample(**overrides) -> CellSample:
    """An LTE sample comfortably clearing every default threshold, unless overridden."""
    values = {"rat": "LTE", "rsrp": -80.0, "rsrq": -8.0, "sinr": 12.0} | overrides
    return CellSample(**values)


class TestClassifySample:
    def test_good_when_every_metric_clears_its_threshold(self):
        assert classify_sample(good_lte_sample(), RunStatusConfig()) == SampleQuality.GOOD

    def test_good_at_the_threshold_itself(self):
        sample = good_lte_sample(rsrp=-100.0, rsrq=-11.0, sinr=0.0)
        assert classify_sample(sample, RunStatusConfig()) == SampleQuality.GOOD

    @pytest.mark.parametrize("metric,value", [("rsrp", -110.0), ("rsrq", -14.0), ("sinr", -3.0)])
    def test_bad_when_any_single_metric_falls_short(self, metric, value):
        sample = good_lte_sample(**{metric: value})
        assert classify_sample(sample, RunStatusConfig()) == SampleQuality.BAD

    @pytest.mark.parametrize("metric", ["rsrp", "rsrq", "sinr"])
    def test_invalid_when_a_metric_is_missing(self, metric):
        sample = good_lte_sample(**{metric: None})
        assert classify_sample(sample, RunStatusConfig()) == SampleQuality.INVALID

    @pytest.mark.parametrize("metric,value", [("rsrp", -200.0), ("rsrp", 10.0), ("sinr", 99.0)])
    def test_invalid_when_a_metric_is_outside_its_legal_range(self, metric, value):
        sample = good_lte_sample(**{metric: value})
        assert classify_sample(sample, RunStatusConfig()) == SampleQuality.INVALID

    def test_legal_ranges_are_per_rat(self):
        # -150 dBm RSRP is impossible for LTE but a legal (if weak) NR reading.
        assert classify_sample(good_lte_sample(rsrp=-150.0), RunStatusConfig()) == SampleQuality.INVALID
        nr_sample = good_lte_sample(rat="NR5G-SA", rsrp=-150.0)
        assert classify_sample(nr_sample, RunStatusConfig()) == SampleQuality.BAD

    @pytest.mark.parametrize("rat", ["NR5G-SA", "NR5G-NSA"])
    def test_new_radio_samples_use_the_nr_thresholds(self, rat):
        config = RunStatusConfig(
            lte=QualityThresholds(min_rsrp=-100.0),
            nr=QualityThresholds(min_rsrp=-70.0),
        )
        sample = good_lte_sample(rat=rat, rsrp=-80.0)

        assert classify_sample(sample, config) == SampleQuality.BAD
        assert classify_sample(good_lte_sample(rsrp=-80.0), config) == SampleQuality.GOOD


class TestRunStatusTracker:
    def test_starts_out_empty_and_healthy(self):
        status = RunStatusTracker(RunStatusConfig()).status()

        assert (status.datapoints_total, status.good, status.bad, status.invalid) == (0, 0, 0, 0)
        assert status.pipeline_broken is False

    def test_counts_accumulate_across_captures(self):
        tracker = RunStatusTracker(RunStatusConfig())

        tracker.record_capture([good_lte_sample(), good_lte_sample(rsrp=-120.0)])
        tracker.record_capture([good_lte_sample(sinr=None)])

        status = tracker.status()
        assert (status.datapoints_total, status.good, status.bad, status.invalid) == (3, 1, 1, 1)

    def test_payload_matches_the_agreed_heartbeat_body(self):
        tracker = RunStatusTracker(RunStatusConfig())
        tracker.record_capture([good_lte_sample()])

        assert tracker.status().as_payload() == {
            "since_run_start": {"datapoints_total": 1, "good": 1, "bad": 0, "invalid": 0},
            "pipeline_broken": False,
        }

    def test_pipeline_breaks_after_enough_consecutive_empty_captures(self):
        tracker = RunStatusTracker(RunStatusConfig(empty_captures_until_pipeline_broken=3))

        for _ in range(2):
            tracker.record_capture([])
        assert tracker.status().pipeline_broken is False

        tracker.record_capture([])
        assert tracker.status().pipeline_broken is True

    def test_a_successful_capture_clears_the_empty_streak(self):
        tracker = RunStatusTracker(RunStatusConfig(empty_captures_until_pipeline_broken=2))

        tracker.record_capture([])
        tracker.record_capture([good_lte_sample()])
        tracker.record_capture([])

        assert tracker.status().pipeline_broken is False

    def test_an_empty_capture_does_not_count_as_a_datapoint(self):
        tracker = RunStatusTracker(RunStatusConfig())

        tracker.record_capture([])

        assert tracker.status().datapoints_total == 0


def fix(num_satellites: int) -> GNSSFix:
    return GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=num_satellites)


class TestGpsFixStatusTracker:
    def test_reports_no_fix_before_anything_is_recorded(self):
        assert GpsFixStatusTracker().status() == GpsFixStatus(
            has_fix=False, num_satellites=None, fix_age_s=None,
        )

    def test_reports_the_most_recently_recorded_fixes_satellite_count(self, monkeypatch):
        monkeypatch.setattr("measurement_software.status.backend_status.time.monotonic", lambda: 100.0)
        tracker = GpsFixStatusTracker()

        tracker.record_fix(fix(6))
        tracker.record_fix(fix(9))

        assert tracker.status().num_satellites == 9

    def test_reports_has_fix_true_once_a_fix_has_been_recorded(self, monkeypatch):
        monkeypatch.setattr("measurement_software.status.backend_status.time.monotonic", lambda: 100.0)
        tracker = GpsFixStatusTracker()

        tracker.record_fix(fix(6))

        assert tracker.status().has_fix is True

    def test_reports_fix_age_as_elapsed_time_since_it_was_recorded(self, monkeypatch):
        clock = {"now": 100.0}
        monkeypatch.setattr("measurement_software.status.backend_status.time.monotonic", lambda: clock["now"])
        tracker = GpsFixStatusTracker()

        tracker.record_fix(fix(6))
        clock["now"] = 107.5

        assert tracker.status().fix_age_s == 7.5

    def test_as_payload_shape(self, monkeypatch):
        monkeypatch.setattr("measurement_software.status.backend_status.time.monotonic", lambda: 100.0)
        tracker = GpsFixStatusTracker()

        tracker.record_fix(fix(4))

        assert tracker.status().as_payload() == {
            "has_fix": True, "num_satellites": 4, "fix_age_s": 0.0,
        }

    def test_as_payload_shape_before_any_fix(self):
        assert GpsFixStatusTracker().status().as_payload() == {
            "has_fix": False, "num_satellites": None, "fix_age_s": None,
        }


class FakeDiskUsage:
    def __init__(self, free: int):
        self.free = free


@pytest.fixture
def disk_usage(monkeypatch):
    """Fakes shutil.disk_usage so tests control free space without touching a real disk."""
    fake = FakeDiskUsage(free=10_000_000_000)
    monkeypatch.setattr(
        "measurement_software.status.backend_status.shutil.disk_usage",
        lambda path: fake,
    )
    return fake


def make_storage_reporter(tmp_path: Path, **overrides) -> StorageStatusReporter:
    return StorageStatusReporter(tmp_path / "uploads", StorageConfig(**overrides))


class TestStorageStatusReporterStatus:
    def test_creates_upload_dir_if_missing(self, tmp_path, disk_usage):
        upload_dir = tmp_path / "uploads"
        assert not upload_dir.exists()

        make_storage_reporter(tmp_path).status()

        assert upload_dir.is_dir()

    def test_counts_only_pending_json_files(self, tmp_path, disk_usage):
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()
        (upload_dir / "gps_5g_20260101_000000.json").write_text("[]")
        (upload_dir / "gps_5g_20260102_000000.json").write_text("[]")
        (upload_dir / "not_a_datapoint_file.txt").write_text("noise")

        status = make_storage_reporter(tmp_path).status()

        assert status.pending_files == 2

    def test_reports_zero_pending_files_when_backlog_is_empty(self, tmp_path, disk_usage):
        status = make_storage_reporter(tmp_path).status()

        assert status.pending_files == 0

    def test_reports_free_disk_space(self, tmp_path, disk_usage):
        disk_usage.free = 1_234_567

        status = make_storage_reporter(tmp_path).status()

        assert status.disk_free_bytes == 1_234_567


class TestStorageStatusReporterLowSpaceWarning:
    def test_warns_when_free_space_is_below_threshold(self, tmp_path, disk_usage, caplog):
        disk_usage.free = 100_000_000
        reporter = make_storage_reporter(tmp_path, low_free_space_warning_bytes=500_000_000)

        with caplog.at_level(logging.WARNING):
            reporter.status()

        assert "low disk space" in caplog.text.lower()

    def test_does_not_warn_when_free_space_is_above_threshold(self, tmp_path, disk_usage, caplog):
        disk_usage.free = 1_000_000_000
        reporter = make_storage_reporter(tmp_path, low_free_space_warning_bytes=500_000_000)

        with caplog.at_level(logging.WARNING):
            reporter.status()

        assert caplog.text == ""

    def test_does_not_warn_when_free_space_exactly_meets_threshold(self, tmp_path, disk_usage, caplog):
        disk_usage.free = 500_000_000
        reporter = make_storage_reporter(tmp_path, low_free_space_warning_bytes=500_000_000)

        with caplog.at_level(logging.WARNING):
            reporter.status()

        assert caplog.text == ""

    def test_warns_again_on_every_call_while_space_stays_low(self, tmp_path, disk_usage, caplog):
        disk_usage.free = 100_000_000
        reporter = make_storage_reporter(tmp_path, low_free_space_warning_bytes=500_000_000)

        with caplog.at_level(logging.WARNING):
            reporter.status()
            reporter.status()

        assert caplog.text.lower().count("low disk space") == 2


def make_app_config(**overrides) -> AppConfig:
    defaults = dict(
        modem=ModemConfig(type="quectel", port="/dev/ttyUSB2", baud_rate=115200, timeout=1.0),
        gnss_receiver=GnssConfig(type="quectel", port="/dev/ttyUSB3", baud_rate=9600, timeout=1.0),
        device=DeviceConfig(),
        collector=CollectorConfig(),
        movement_gate=MovementGateConfig(),
        quectel_cm=QuectelCmConfig(),
        run_status=RunStatusConfig(),
        backend=BackendConfig(),
        heartbeat=HeartbeatConfig(),
        selftest=SelftestConfig(),
        gps_fix_test=GpsFixTestConfig(),
        storage=StorageConfig(),
        fan=FanConfig(),
        display=DisplayConfig(),
        latency_test=LatencyTestConfig(),
        uploader=UploaderConfig(upload_dir="/data/uploads"),
        logging=LoggingConfig(),
        system=SystemConfig(),
    )
    return AppConfig(**(defaults | overrides))


class TestConfigStatusReporterAsPayload:
    def test_reports_the_live_value_for_every_allow_listed_field(self):
        config = make_app_config(
            run_status=RunStatusConfig(
                lte=QualityThresholds(min_rsrp=-95.0, min_rsrq=-10.0, min_sinr=3.0),
                empty_captures_until_pipeline_broken=5,
            ),
            collector=CollectorConfig(max_idle_time=180, max_wait_for_first_fix=240),
            movement_gate=MovementGateConfig(
                poll_interval_s=90.0, movement_threshold=20.0, confirmations_required=3,
            ),
            latency_test=LatencyTestConfig(
                baseline_interval_s=45.0, load_interval_s=800.0, poll_interval_s=15.0,
            ),
            heartbeat=HeartbeatConfig(interval_s=30.0),
        )
        reporter = ConfigStatusReporter(config, FanController(FanConfig(enabled=False)))

        payload = reporter.as_payload()

        assert payload == {
            "rsrp_threshold": -95.0,
            "rsrq_threshold": -10.0,
            "sinr_threshold": 3.0,
            "idle_threshold_s": 180,
            "day_end_threshold_s": None,
            "movement_gate_poll_interval_s": 90.0,
            "latency_test_baseline_interval_s": 45.0,
            "latency_test_load_interval_s": 800.0,
            "latency_test_poll_interval_s": 15.0,
            "heartbeat_interval_s": 30.0,
            "collector_max_wait_for_first_fix": 240,
            "movement_gate_movement_threshold": 20.0,
            "movement_gate_confirmations_required": 3,
            "run_status_empty_captures_until_pipeline_broken": 5,
            "fan_mode": "auto",
        }

    def test_reports_the_fan_controllers_current_mode(self):
        config = make_app_config()
        fan_controller = FanController(FanConfig(enabled=True))
        fan_controller.override(True)
        reporter = ConfigStatusReporter(config, fan_controller)

        assert reporter.as_payload()["fan_mode"] == "on"
