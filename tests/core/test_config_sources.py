import logging
from pathlib import Path

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
    QuectelCmConfig,
    RunStatusConfig,
    SelftestConfig,
    StorageConfig,
    SystemConfig,
    UploaderConfig,
)
from measurement_software.core.config_sources import (
    ConfigOverrides,
    apply_local_config,
    apply_overrides,
    load_local_config,
    read_mode_override,
    read_overrides_file,
    sanitize_overrides,
    write_mode_override,
    write_overrides_file,
)


def make_app_config() -> AppConfig:
    return AppConfig(
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


class TestReadModeOverride:
    def test_returns_none_when_no_file_exists(self, tmp_path):
        assert read_mode_override(tmp_path / "mode_override.toml") is None

    def test_reads_a_persisted_mode(self, tmp_path):
        path = tmp_path / "mode_override.toml"
        path.write_text('mode = "waiting"\n')

        assert read_mode_override(path) == "waiting"


class TestWriteModeOverride:
    def test_persists_the_mode_so_it_can_be_read_back(self, tmp_path):
        path = tmp_path / "mode_override.toml"

        write_mode_override(path, "waiting")

        assert read_mode_override(path) == "waiting"

    def test_overwrites_a_previously_persisted_mode(self, tmp_path):
        path = tmp_path / "mode_override.toml"
        write_mode_override(path, "waiting")

        write_mode_override(path, "auto")

        assert read_mode_override(path) == "auto"


class TestLoadLocalConfig:
    def test_returns_empty_dict_when_file_does_not_exist(self, tmp_path):
        assert load_local_config(tmp_path / "config.local.toml") == {}

    def test_reads_sections_from_disk(self, tmp_path):
        path = tmp_path / "config.local.toml"
        path.write_text('[backend]\nurl = "https://example.org"\n')

        assert load_local_config(path) == {"backend": {"url": "https://example.org"}}


class TestApplyLocalConfig:
    def test_applies_backend_fields(self):
        config = make_app_config()

        apply_local_config(config, {"backend": {"url": "https://real.example.org", "device_key": "k"}})

        assert config.backend.url == "https://real.example.org"
        assert config.backend.device_key == "k"

    def test_applies_heartbeat_fields(self):
        config = make_app_config()

        apply_local_config(config, {"heartbeat": {"hmac_secret": "s", "device_id": "pi-01"}})

        assert config.heartbeat.hmac_secret == "s"
        assert config.heartbeat.device_id == "pi-01"

    def test_applies_gps_fix_test_status_url(self):
        config = make_app_config()

        apply_local_config(config, {"gps_fix_test": {"status_url": "https://real.example.org/gps-fix-test"}})

        assert config.gps_fix_test.status_url == "https://real.example.org/gps-fix-test"

    def test_applies_quectel_cm_binary(self):
        config = make_app_config()

        apply_local_config(config, {"quectel_cm": {"binary": "/usr/local/bin/quectel-CM"}})

        assert config.quectel_cm.binary == "/usr/local/bin/quectel-CM"

    def test_applies_device_device_id(self):
        """Must match [heartbeat] device_id - see docs/device-bringup-faq.md. Left unset,
        device.device_id silently defaults to the Pi's hostname, which the backend then rejects
        on upload as a device-identity mismatch against whatever authenticated the request."""
        config = make_app_config()

        apply_local_config(config, {"device": {"device_id": "5gmu_1"}})

        assert config.device.device_id == "5gmu_1"

    def test_unset_fields_leave_config_untouched(self):
        config = make_app_config()

        apply_local_config(config, {})

        assert config.backend.url == ""
        assert config.quectel_cm.binary == "/add/your/quectel-CM/path/here"

    def test_ignores_a_section_not_on_the_allow_list(self, caplog):
        config = make_app_config()

        with caplog.at_level(logging.WARNING):
            apply_local_config(config, {"uploader": {"upload_host": "evil.example.org"}})

        assert config.uploader.upload_host == ""
        assert "not allow-listed" in caplog.text

    def test_ignores_a_field_not_on_the_allow_list_within_an_allowed_section(self, caplog):
        config = make_app_config()

        with caplog.at_level(logging.WARNING):
            apply_local_config(config, {"heartbeat": {"enabled": True}})

        assert config.heartbeat.enabled is False
        assert "not allow-listed" in caplog.text

    def test_one_bad_field_does_not_block_the_rest_of_the_batch(self):
        config = make_app_config()

        apply_local_config(config, {"heartbeat": {"enabled": True, "device_id": "pi-01"}})

        assert config.heartbeat.enabled is False
        assert config.heartbeat.device_id == "pi-01"


class TestLoadConfigMergesLocalConfigFile:
    def _write_config(self, tmp_path: Path) -> Path:
        content = """
[modem]
type = "quectel"
port = "/dev/ttyUSB2"
baud_rate = 115200
timeout = 1.0

[gnss_receiver]
type = "quectel"
port = "/dev/ttyUSB3"
baud_rate = 9600
timeout = 1.0

[uploader]
upload_dir = "/data/uploads"
"""
        path = tmp_path / "config.toml"
        path.write_text(content)
        return path

    def test_merges_sibling_config_local_file(self, tmp_path):
        from measurement_software.core.config import load_config

        config_path = self._write_config(tmp_path)
        (tmp_path / "config.local.toml").write_text(
            '[backend]\nurl = "https://real.example.org"\ndevice_key = "real-key"\n'
        )

        config = load_config(config_path)

        assert config.backend.url == "https://real.example.org"
        assert config.backend.device_key == "real-key"

    def test_without_local_file_keeps_config_toml_placeholder_values(self, tmp_path):
        from measurement_software.core.config import load_config

        config = load_config(self._write_config(tmp_path))

        assert config.backend.url == ""
        assert config.quectel_cm.binary == "/add/your/quectel-CM/path/here"

    def test_config_overrides_file_still_wins_over_config_local_toml_for_shared_precedence(self, tmp_path):
        """config.local.toml and config_overrides.toml allow-list disjoint fields, but the load
        order itself (local before overrides) should still hold if that ever changes."""
        from measurement_software.core.config import load_config

        config_path = self._write_config(tmp_path)
        (tmp_path / "config.local.toml").write_text('[backend]\nurl = "https://local.example.org"\n')
        (tmp_path / "config_overrides.toml").write_text("idle_threshold_s = 600\n")

        config = load_config(config_path)

        assert config.backend.url == "https://local.example.org"
        assert config.collector.max_idle_time == 600


class TestSanitizeOverrides:
    def test_keeps_allow_listed_fields_within_bounds(self):
        result = sanitize_overrides({"rsrp_threshold": -100.0, "idle_threshold_s": 300})

        assert result == {"rsrp_threshold": -100.0, "idle_threshold_s": 300}

    def test_drops_a_field_outside_the_allow_list(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"upload_host": "evil.example.org"})

        assert result == {}
        assert "disallowed field" in caplog.text

    def test_drops_a_value_outside_sane_bounds(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"rsrp_threshold": 500.0})

        assert result == {}
        assert "outside sane bounds" in caplog.text

    def test_drops_a_non_numeric_value(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"idle_threshold_s": "forever"})

        assert result == {}
        assert "not a number" in caplog.text

    def test_one_bad_field_does_not_block_the_rest_of_the_batch(self):
        result = sanitize_overrides({"rsrp_threshold": -100.0, "upload_host": "evil.example.org"})

        assert result == {"rsrp_threshold": -100.0}

    def test_accepts_a_field_with_no_device_side_effect_yet(self):
        """day_end_threshold_s is allow-listed but not wired to behavior yet."""
        result = sanitize_overrides({"day_end_threshold_s": 3600})

        assert result == {"day_end_threshold_s": 3600}

    def test_rejects_dropped_test_interval_field(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"test_interval_s": 300})

        assert result == {}
        assert "disallowed field" in caplog.text

    def test_keeps_new_per_feature_cadence_fields_within_bounds(self):
        result = sanitize_overrides({
            "movement_gate_poll_interval_s": 60.0,
            "latency_test_baseline_interval_s": 900.0,
            "latency_test_load_interval_s": 1800.0,
            "latency_test_poll_interval_s": 30.0,
            "heartbeat_interval_s": 120.0,
            "collector_max_wait_for_first_fix": 600.0,
            "movement_gate_movement_threshold": 20.0,
            "movement_gate_confirmations_required": 3,
            "run_status_empty_captures_until_pipeline_broken": 5,
        })

        assert result == {
            "movement_gate_poll_interval_s": 60.0,
            "latency_test_baseline_interval_s": 900.0,
            "latency_test_load_interval_s": 1800.0,
            "latency_test_poll_interval_s": 30.0,
            "heartbeat_interval_s": 120.0,
            "collector_max_wait_for_first_fix": 600.0,
            "movement_gate_movement_threshold": 20.0,
            "movement_gate_confirmations_required": 3,
            "run_status_empty_captures_until_pipeline_broken": 5,
        }

    def test_drops_collector_max_wait_for_first_fix_above_one_hour(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"collector_max_wait_for_first_fix": 7200.0})

        assert result == {}
        assert "outside sane bounds" in caplog.text

    def test_drops_movement_gate_confirmations_required_above_ceiling(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"movement_gate_confirmations_required": 11})

        assert result == {}
        assert "outside sane bounds" in caplog.text


class TestOverridesFile:
    def test_read_returns_empty_dict_when_file_does_not_exist(self, tmp_path):
        assert read_overrides_file(tmp_path / "config_overrides.toml") == {}

    def test_write_then_read_round_trips(self, tmp_path):
        path = tmp_path / "config_overrides.toml"

        write_overrides_file(path, {"rsrp_threshold": -100.0, "idle_threshold_s": 300})

        assert read_overrides_file(path) == {"rsrp_threshold": -100.0, "idle_threshold_s": 300}

    def test_write_merges_onto_existing_fields_without_erasing_them(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        write_overrides_file(path, {"rsrp_threshold": -100.0})

        write_overrides_file(path, {"idle_threshold_s": 300})

        assert read_overrides_file(path) == {"rsrp_threshold": -100.0, "idle_threshold_s": 300}

    def test_write_replaces_an_existing_field(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        write_overrides_file(path, {"rsrp_threshold": -100.0})

        write_overrides_file(path, {"rsrp_threshold": -95.0})

        assert read_overrides_file(path) == {"rsrp_threshold": -95.0}


class TestApplyOverrides:
    def test_unified_threshold_applies_to_both_rats(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(rsrp_threshold=-90.0))

        assert config.run_status.lte.min_rsrp == -90.0
        assert config.run_status.nr.min_rsrp == -90.0

    def test_idle_threshold_overrides_max_idle_time(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(idle_threshold_s=600))

        assert config.collector.max_idle_time == 600

    def test_unset_fields_leave_config_untouched(self):
        config = make_app_config()
        original_rsrp = config.run_status.lte.min_rsrp

        apply_overrides(config, ConfigOverrides())

        assert config.run_status.lte.min_rsrp == original_rsrp

    def test_fields_with_no_device_side_effect_do_not_raise(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(day_end_threshold_s=3600))

    def test_movement_gate_overrides_apply(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(
            movement_gate_poll_interval_s=60.0,
            movement_gate_movement_threshold=20.0,
            movement_gate_confirmations_required=3,
        ))

        assert config.movement_gate.poll_interval_s == 60.0
        assert config.movement_gate.movement_threshold == 20.0
        assert config.movement_gate.confirmations_required == 3

    def test_latency_test_overrides_apply(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(
            latency_test_baseline_interval_s=900.0,
            latency_test_load_interval_s=1800.0,
            latency_test_poll_interval_s=30.0,
        ))

        assert config.latency_test.baseline_interval_s == 900.0
        assert config.latency_test.load_interval_s == 1800.0
        assert config.latency_test.poll_interval_s == 30.0

    def test_heartbeat_interval_override_applies(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(heartbeat_interval_s=120.0))

        assert config.heartbeat.interval_s == 120.0

    def test_collector_max_wait_for_first_fix_override_applies(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(collector_max_wait_for_first_fix=600.0))

        assert config.collector.max_wait_for_first_fix == 600.0

    def test_run_status_empty_captures_until_pipeline_broken_override_applies_as_int(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(run_status_empty_captures_until_pipeline_broken=5.0))

        assert config.run_status.empty_captures_until_pipeline_broken == 5
