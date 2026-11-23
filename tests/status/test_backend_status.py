import logging
from pathlib import Path

import pytest

from measurement_software.core.config import (
    AppConfig,
    BackendConfig,
    CollectorConfig,
    DeviceConfig,
    DisplayConfig,
    GnssConfig,
    GpsFixTestConfig,
    HeartbeatConfig,
    LatencyTestConfig,
    LoggingConfig,
    ModemConfig,
    MovementGateConfig,
    QuectelCmConfig,
    RunStatusConfig,
    SelftestConfig,
    StorageConfig,
    SystemConfig,
    UploaderConfig,
)
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


def valid_lte_sample(**overrides) -> CellSample:
    """An LTE sample well inside every legal range, unless overridden."""
    values = {"rat": "LTE", "rsrp": -80.0, "rsrq": -8.0, "sinr": 12.0} | overrides
    return CellSample(**values)


class TestClassifySample:
    def test_valid_when_every_metric_is_within_its_legal_range(self):
        assert classify_sample(valid_lte_sample()) == SampleQuality.VALID

    @pytest.mark.parametrize("metric,value", [("rsrp", -139.0), ("rsrq", -19.0), ("sinr", -19.0)])
    def test_weak_but_possible_readings_are_still_valid(self, metric, value):
        assert classify_sample(valid_lte_sample(**{metric: value})) == SampleQuality.VALID

    def test_valid_at_the_edges_of_the_legal_range(self):
        sample = valid_lte_sample(rsrp=-140.0, rsrq=2.5, sinr=30.0)
        assert classify_sample(sample) == SampleQuality.VALID

    @pytest.mark.parametrize("metric", ["rsrp", "rsrq", "sinr"])
    def test_invalid_when_a_metric_is_missing(self, metric):
        assert classify_sample(valid_lte_sample(**{metric: None})) == SampleQuality.INVALID

    @pytest.mark.parametrize("metric,value", [("rsrp", -200.0), ("rsrp", 10.0), ("sinr", 99.0)])
    def test_invalid_when_a_metric_is_outside_its_legal_range(self, metric, value):
        assert classify_sample(valid_lte_sample(**{metric: value})) == SampleQuality.INVALID

    def test_legal_ranges_are_per_rat(self):
        # -150 dBm RSRP is impossible for LTE but a legal (if weak) NR reading.
        assert classify_sample(valid_lte_sample(rsrp=-150.0)) == SampleQuality.INVALID
        assert classify_sample(valid_lte_sample(rat="NR5G-SA", rsrp=-150.0)) == SampleQuality.VALID

    @pytest.mark.parametrize("rat", ["NR5G-SA", "NR5G-NSA"])
    def test_new_radio_samples_use_the_nr_ranges(self, rat):
        assert classify_sample(valid_lte_sample(rat=rat, sinr=35.0)) == SampleQuality.VALID
        assert classify_sample(valid_lte_sample(sinr=35.0)) == SampleQuality.INVALID


class TestRunStatusTracker:
    def test_starts_out_empty_and_healthy(self):
        status = RunStatusTracker(RunStatusConfig()).status()

        assert (status.datapoints_total, status.valid, status.invalid) == (0, 0, 0)
        assert status.pipeline_broken is False

    def test_counts_accumulate_across_captures(self):
        tracker = RunStatusTracker(RunStatusConfig())

        tracker.record_capture([valid_lte_sample(), valid_lte_sample(rsrp=-120.0)])
        tracker.record_capture([valid_lte_sample(sinr=None)])

        status = tracker.status()
        assert (status.datapoints_total, status.valid, status.invalid) == (3, 2, 1)

    def test_payload_matches_the_agreed_heartbeat_body(self):
        tracker = RunStatusTracker(RunStatusConfig())
        tracker.record_capture([valid_lte_sample()])

        assert tracker.status().as_payload() == {
            "since_run_start": {"datapoints_total": 1, "valid": 1, "invalid": 0},
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
        tracker.record_capture([valid_lte_sample()])
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
            "has_fix": True, "num_satellites": 4, "fix_age_s": 0.0, "diagnostic": None,
        }

    def test_as_payload_shape_before_any_fix(self):
        assert GpsFixStatusTracker().status().as_payload() == {
            "has_fix": False, "num_satellites": None, "fix_age_s": None, "diagnostic": None,
        }

    def test_reports_none_diagnostic_before_any_has_been_recorded(self):
        assert GpsFixStatusTracker().status().diagnostic is None

    def test_reports_the_friendly_mapped_form_of_a_recorded_diagnostic(self):
        tracker = GpsFixStatusTracker()

        tracker.record_diagnostic("ANTENNA SHORT")

        assert tracker.status().diagnostic == "Antenna short circuit"

    def test_reports_the_generic_fallback_for_an_unknown_antenna_status(self):
        tracker = GpsFixStatusTracker()

        tracker.record_diagnostic("ANTENNA FUTURE CODE")

        assert tracker.status().diagnostic == "Unknown antenna/GNSS diagnostic error"

    def test_antenna_ok_reports_no_diagnostic(self):
        tracker = GpsFixStatusTracker()

        tracker.record_diagnostic("ANTENNA OK")

        assert tracker.status().diagnostic is None

    def test_antenna_ok_clears_an_earlier_fault(self):
        tracker = GpsFixStatusTracker()

        tracker.record_diagnostic("ANTENNA SHORT")
        tracker.record_diagnostic("ANTENNA OK")

        assert tracker.status().diagnostic is None

    def test_unrelated_text_keeps_an_earlier_fault(self):
        tracker = GpsFixStatusTracker()

        tracker.record_diagnostic("ANTENNA SHORT")
        tracker.record_diagnostic("HW UBX-M8030")

        assert tracker.status().diagnostic == "Antenna short circuit"

    def test_diagnostic_is_independent_of_fix_status(self, monkeypatch):
        """A hardware fault explains why no fix can ever be acquired - it must be visible
        even though has_fix stays False the whole time."""
        monkeypatch.setattr("measurement_software.status.backend_status.time.monotonic", lambda: 100.0)
        tracker = GpsFixStatusTracker()

        tracker.record_diagnostic("ANTENNA SHORT")

        status = tracker.status()
        assert status.has_fix is False
        assert status.diagnostic == "Antenna short circuit"

    def test_a_later_recorded_diagnostic_replaces_the_earlier_one(self):
        tracker = GpsFixStatusTracker()

        tracker.record_diagnostic("ANTENNA SHORT")
        tracker.record_diagnostic("ANTENNA FUTURE CODE")

        assert tracker.status().diagnostic == "Unknown antenna/GNSS diagnostic error"


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
        heartbeat=HeartbeatConfig(enabled=False),
        selftest=SelftestConfig(),
        gps_fix_test=GpsFixTestConfig(),
        storage=StorageConfig(),
        display=DisplayConfig(enabled=False),
        latency_test=LatencyTestConfig(enabled=False),
        uploader=UploaderConfig(upload_dir="/data/uploads"),
        logging=LoggingConfig(),
        system=SystemConfig(),
    )
    return AppConfig(**(defaults | overrides))


class TestConfigStatusReporterAsPayload:
    def test_reports_the_live_value_for_every_allow_listed_field(self):
        config = make_app_config(
            run_status=RunStatusConfig(empty_captures_until_pipeline_broken=5),
            collector=CollectorConfig(
                max_idle_time=180, max_wait_for_first_fix=240, capture_gate="time",
                position_threshold=25.0, capture_interval_s=20.0, wait_log_interval=7.0,
                gps_enabled=False, gps_disabled_poll_interval_s=9.0,
            ),
            movement_gate=MovementGateConfig(
                enabled=False, poll_interval_s=90.0, movement_threshold=20.0, confirmations_required=3,
                home_latitude=48.1, home_longitude=11.5, home_departure_threshold=150.0,
            ),
            latency_test=LatencyTestConfig(
                enabled=True, baseline_interval_s=45.0, load_interval_s=800.0, poll_interval_s=15.0,
            ),
            heartbeat=HeartbeatConfig(interval_s=30.0),
            device=DeviceConfig(mission_type="drone"),
            display=DisplayConfig(enabled=True, refresh_interval_s=4.0),
            modem=ModemConfig(
                type="quectel", port="/dev/ttyUSB2", baud_rate=115200, timeout=1.0,
                mode="sa_scan", sim_unlock_poll_attempts=12, sim_unlock_poll_interval=2.0,
            ),
            gnss_receiver=GnssConfig(
                type="quectel", port="/dev/ttyUSB3", baud_rate=9600, timeout=1.0,
                retries=5, retry_delay_s=1.5,
            ),
            system=SystemConfig(running_on_pi=False, mode="manual"),
        )

        payload = ConfigStatusReporter(config).as_payload()

        assert payload == {
            "idle_threshold_s": 180,
            "day_end_threshold_s": None,
            "movement_gate_enabled": False,
            "movement_gate_poll_interval_s": 90.0,
            "latency_test_baseline_interval_s": 45.0,
            "latency_test_load_interval_s": 800.0,
            "latency_test_poll_interval_s": 15.0,
            "heartbeat_interval_s": 30.0,
            "collector_max_wait_for_first_fix": 240,
            "movement_gate_movement_threshold": 20.0,
            "movement_gate_confirmations_required": 3,
            "run_status_empty_captures_until_pipeline_broken": 5,
            "collector_capture_gate": "time",
            "collector_position_threshold": 25.0,
            "collector_capture_interval_s": 20.0,
            "collector_wait_log_interval": 7.0,
            "collector_gps_enabled": False,
            "collector_gps_disabled_poll_interval_s": 9.0,
            "movement_gate_home_latitude": 48.1,
            "movement_gate_home_longitude": 11.5,
            "movement_gate_home_departure_threshold": 150.0,
            "latency_test_enabled": True,
            "system_mode": "manual",
            "device_mission_type": "drone",
            "display_enabled": True,
            "display_refresh_interval_s": 4.0,
            "modem_mode": "sa_scan",
            "modem_sim_unlock_poll_attempts": 12,
            "modem_sim_unlock_poll_interval": 2.0,
            "gnss_receiver_retries": 5,
            "gnss_receiver_retry_delay_s": 1.5,
            "system_running_on_pi": False,
        }

    @pytest.mark.parametrize("mode", ["auto", "manual"])
    def test_reports_the_running_mode_as_system_mode(self, mode):
        config = make_app_config(system=SystemConfig(mode=mode))

        assert ConfigStatusReporter(config).as_payload()["system_mode"] == mode

    def test_does_not_report_a_separate_mode_key(self):
        assert "mode" not in ConfigStatusReporter(make_app_config()).as_payload()

    def test_an_unset_home_position_is_reported_as_null(self):
        payload = ConfigStatusReporter(make_app_config()).as_payload()

        assert payload["movement_gate_home_latitude"] is None
        assert payload["movement_gate_home_longitude"] is None

    def test_reports_only_override_field_keys_and_never_identity_or_secrets(self):
        from measurement_software.core.config_sources import ALLOWED_OVERRIDE_FIELDS

        config = make_app_config(
            heartbeat=HeartbeatConfig(hmac_secret="s3cret", device_id="pi-01"),
            backend=BackendConfig(url="https://real.example.org", device_key="k3y"),
        )

        payload = ConfigStatusReporter(config).as_payload()

        assert set(payload) == set(ALLOWED_OVERRIDE_FIELDS)
        assert "s3cret" not in str(payload) and "k3y" not in str(payload) and "pi-01" not in str(payload)
