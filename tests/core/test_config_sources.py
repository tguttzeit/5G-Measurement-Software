import copy
import logging
import threading
import time
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
from measurement_software.core.config_sources import (
    CLEARED,
    FIELD_SPECS,
    ConfigOverrides,
    apply_local_config,
    apply_overrides,
    load_overrides,
    migrate_legacy_mode_override,
    load_local_config,
    promote_overrides,
    read_and_clear_pending_reboot_ack,
    read_overrides_file,
    sanitize_overrides,
    write_overrides_file,
    write_pending_reboot_ack,
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
        display=DisplayConfig(),
        latency_test=LatencyTestConfig(),
        uploader=UploaderConfig(upload_dir="/data/uploads"),
        logging=LoggingConfig(),
        system=SystemConfig(),
    )


class TestPendingRebootAck:
    def test_read_returns_none_when_no_file_exists(self, tmp_path):
        assert read_and_clear_pending_reboot_ack(tmp_path / "pending_reboot_ack.toml") is None

    def test_write_then_read_round_trips_an_integer_id(self, tmp_path):
        path = tmp_path / "pending_reboot_ack.toml"
        write_pending_reboot_ack(path, 52)

        assert read_and_clear_pending_reboot_ack(path) == 52

    def test_write_then_read_round_trips_a_string_id(self, tmp_path):
        path = tmp_path / "pending_reboot_ack.toml"
        write_pending_reboot_ack(path, "cmd-52")

        assert read_and_clear_pending_reboot_ack(path) == "cmd-52"

    def test_read_removes_the_file_so_it_is_never_applied_twice(self, tmp_path):
        path = tmp_path / "pending_reboot_ack.toml"
        write_pending_reboot_ack(path, 52)

        read_and_clear_pending_reboot_ack(path)

        assert not path.exists()
        assert read_and_clear_pending_reboot_ack(path) is None


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

    def test_applies_uploader_upload_dir(self):
        config = make_app_config()

        apply_local_config(config, {"uploader": {"upload_dir": "/data/upload_queue"}})

        assert config.uploader.upload_dir == "/data/upload_queue"

    def test_ignores_a_section_not_on_the_allow_list(self, caplog):
        config = make_app_config()

        with caplog.at_level(logging.WARNING):
            apply_local_config(config, {"not_a_section": {"upload_host": "evil.example.org"}})

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
        result = sanitize_overrides({"heartbeat_interval_s": 30.0, "idle_threshold_s": 300})

        assert result == {"heartbeat_interval_s": 30.0, "idle_threshold_s": 300}

    def test_drops_a_field_outside_the_allow_list(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"upload_host": "evil.example.org"})

        assert result == {}
        assert "disallowed field" in caplog.text

    def test_drops_a_value_outside_sane_bounds(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"idle_threshold_s": 500000.0})

        assert result == {}
        assert "within [0, 86400]" in caplog.text

    def test_drops_a_non_numeric_value(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"idle_threshold_s": "forever"})

        assert result == {}
        assert "not a valid float" in caplog.text

    def test_one_bad_field_does_not_block_the_rest_of_the_batch(self):
        result = sanitize_overrides({"idle_threshold_s": 300, "upload_host": "evil.example.org"})

        assert result == {"idle_threshold_s": 300}

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

    def test_accepts_zero_idle_threshold_as_disabled(self):
        assert sanitize_overrides({"idle_threshold_s": 0}) == {"idle_threshold_s": 0}

    def test_still_rejects_zero_for_other_lower_bounded_fields(self):
        assert sanitize_overrides({"movement_gate_poll_interval_s": 0, "heartbeat_interval_s": 0}) == {}

    def test_rejects_negative_idle_threshold(self):
        assert sanitize_overrides({"idle_threshold_s": -1}) == {}

    def test_accepts_movement_gate_enabled_as_a_strict_bool(self):
        assert sanitize_overrides({"movement_gate_enabled": False}) == {"movement_gate_enabled": False}

    @pytest.mark.parametrize("value", [0, 1, "false", "true", None])
    def test_rejects_non_bool_movement_gate_enabled(self, value):
        assert sanitize_overrides({"movement_gate_enabled": value}) == {}

    def test_drops_collector_max_wait_for_first_fix_above_one_hour(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"collector_max_wait_for_first_fix": 7200.0})

        assert result == {}
        assert "within [" in caplog.text

    def test_drops_movement_gate_confirmations_required_above_ceiling(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = sanitize_overrides({"movement_gate_confirmations_required": 11})

        assert result == {}
        assert "within [" in caplog.text


class TestOverridesFile:
    def test_read_returns_empty_dict_when_file_does_not_exist(self, tmp_path):
        assert read_overrides_file(tmp_path / "config_overrides.toml") == {}

    def test_write_then_read_round_trips(self, tmp_path):
        path = tmp_path / "config_overrides.toml"

        write_overrides_file(path, {"heartbeat_interval_s": 30.0, "idle_threshold_s": 300})

        assert read_overrides_file(path) == {"heartbeat_interval_s": 30.0, "idle_threshold_s": 300}

    def test_write_merges_onto_existing_fields_without_erasing_them(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        write_overrides_file(path, {"heartbeat_interval_s": 30.0})

        write_overrides_file(path, {"idle_threshold_s": 300})

        assert read_overrides_file(path) == {"heartbeat_interval_s": 30.0, "idle_threshold_s": 300}

    def test_write_replaces_an_existing_field(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        write_overrides_file(path, {"heartbeat_interval_s": 30.0})

        write_overrides_file(path, {"heartbeat_interval_s": 45.0})

        assert read_overrides_file(path) == {"heartbeat_interval_s": 45.0}


class TestApplyOverrides:
    def test_idle_threshold_overrides_max_idle_time(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(idle_threshold_s=600))

        assert config.collector.max_idle_time == 600

    def test_unset_fields_leave_config_untouched(self):
        config = make_app_config()
        original = copy.deepcopy(config)

        apply_overrides(config, ConfigOverrides())

        assert config == original

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


def _write_minimal_config_toml(tmp_path: Path) -> Path:
    """A config.toml covering every OVERRIDE_FIELD_TARGETS section, hand-commented like the real
    one, so promotion tests can assert on comment preservation and exact target fields."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[modem]\n'
        'type = "quectel"\n'
        'port = "/dev/ttyUSB2"\n'
        'baud_rate = 115200\n'
        'timeout = 1.0\n'
        '\n'
        '[gnss_receiver]\n'
        'type = "quectel"\n'
        'port = "/dev/ttyUSB3"\n'
        'baud_rate = 9600\n'
        'timeout = 1.0\n'
        '\n'
        '# Thresholds controlling when a measurement is captured and when a session ends.\n'
        '[collector]\n'
        'max_idle_time = 200\n'
        'max_wait_for_first_fix = 300\n'
        '\n'
        '[movement_gate]\n'
        'poll_interval_s = 120.0\n'
        'movement_threshold = 15.0\n'
        'confirmations_required = 2\n'
        '\n'
        '[heartbeat]\n'
        'interval_s = 60.0\n'
        '\n'
        '[latency_test]\n'
        'baseline_interval_s = 60.0\n'
        'load_interval_s = 900.0\n'
        'poll_interval_s = 30.0\n'
        '\n'
        '# What the heartbeat reports about run quality.\n'
        '[run_status]\n'
        'empty_captures_until_pipeline_broken = 3\n'
        '\n'
        '[uploader]\n'
        'upload_dir = "/data/uploads"\n'
    )
    return path


class TestPromoteOverrides:
    def test_promotes_a_single_target_field(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 500})

        promoted = promote_overrides(config_path)

        assert promoted == {"idle_threshold_s": (200, 500)}
        assert "max_idle_time = 500" in config_path.read_text()

    def test_preserves_comments_and_unrelated_fields(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 500})

        promote_overrides(config_path)

        text = config_path.read_text()
        assert "# Thresholds controlling when a measurement is captured and when a session ends." in text
        assert "# What the heartbeat reports about run quality." in text
        assert 'upload_dir = "/data/uploads"' in text
        assert "max_wait_for_first_fix = 300" in text

    def test_removes_only_the_promoted_field_from_the_overrides_file(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        overrides_path = tmp_path / "config_overrides.toml"
        write_overrides_file(overrides_path, {"idle_threshold_s": 500, "heartbeat_interval_s": 120.0})

        promoted = promote_overrides(config_path)

        assert set(promoted) == {"idle_threshold_s", "heartbeat_interval_s"}
        assert read_overrides_file(overrides_path) == {}

    def test_leaves_other_still_set_fields_in_the_overrides_file(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        overrides_path = tmp_path / "config_overrides.toml"
        write_overrides_file(overrides_path, {
            "idle_threshold_s": 500, "day_end_threshold_s": 3600,
        })

        promoted = promote_overrides(config_path)

        assert set(promoted) == {"idle_threshold_s"}
        assert read_overrides_file(overrides_path) == {"day_end_threshold_s": 3600}

    def test_a_field_with_no_mapped_target_is_logged_and_left_in_place(self, tmp_path, caplog):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"day_end_threshold_s": 3600})

        with caplog.at_level(logging.WARNING):
            promoted = promote_overrides(config_path)

        assert promoted == {}
        assert "no config.toml target is mapped" in caplog.text

    def test_is_a_no_op_when_the_overrides_file_is_empty(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        original_text = config_path.read_text()

        promoted = promote_overrides(config_path)

        assert promoted == {}
        assert config_path.read_text() == original_text

    def test_is_a_no_op_when_no_overrides_file_exists_at_all(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)

        promoted = promote_overrides(config_path)

        assert promoted == {}

    def test_casts_the_pipeline_broken_field_to_int_in_config_toml(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(
            tmp_path / "config_overrides.toml",
            {"run_status_empty_captures_until_pipeline_broken": 5.0},
        )

        promoted = promote_overrides(config_path)

        assert promoted == {"run_status_empty_captures_until_pipeline_broken": (3, 5)}
        assert "empty_captures_until_pipeline_broken = 5\n" in config_path.read_text()

    def test_returns_old_and_new_values_for_every_promoted_field(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {
            "idle_threshold_s": 500, "movement_gate_movement_threshold": 25.0,
        })

        promoted = promote_overrides(config_path)

        assert promoted == {
            "idle_threshold_s": (200, 500),
            "movement_gate_movement_threshold": (15.0, 25.0),
        }

    def test_a_concurrent_override_write_during_promotion_is_not_lost(self, tmp_path, monkeypatch):
        """Reproduces the race the concurrent-write guard exists for: without it, promote_overrides
        would read config_overrides.toml, sleep (standing in for a slow promotion), and only then
        write back a "remaining fields" snapshot computed from that stale read - silently
        clobbering an override write_overrides_file() made to the same file in the meantime. The
        lock must instead serialize the two, so the concurrent write survives."""
        import measurement_software.core.config_sources as config_sources_module

        config_path = _write_minimal_config_toml(tmp_path)
        overrides_path = tmp_path / "config_overrides.toml"
        write_overrides_file(overrides_path, {"idle_threshold_s": 500})

        real_write = config_sources_module._write_toml_document_atomically
        started_writing_config_toml = threading.Event()

        def slow_write(path, document):
            started_writing_config_toml.set()
            time.sleep(0.2)
            real_write(path, document)

        monkeypatch.setattr(config_sources_module, "_write_toml_document_atomically", slow_write)

        promote_thread = threading.Thread(target=promote_overrides, args=(config_path,))
        promote_thread.start()
        assert started_writing_config_toml.wait(timeout=2), "promotion never reached its write step"

        write_overrides_file(overrides_path, {"heartbeat_interval_s": 120.0})
        promote_thread.join(timeout=2)

        assert read_overrides_file(overrides_path) == {"heartbeat_interval_s": 120.0}

    def test_written_config_toml_is_still_valid_toml_that_load_config_can_read(self, tmp_path):
        from measurement_software.core.config import load_config

        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 500})

        promote_overrides(config_path)
        # config_overrides.toml is now empty, so load_config must reflect the promoted baseline.
        config = load_config(config_path)

        assert config.collector.max_idle_time == 500


class TestSanitizeTypedOverrides:
    def test_every_field_spec_has_a_config_override_attribute(self):
        assert set(FIELD_SPECS) == {f.name for f in __import__("dataclasses").fields(ConfigOverrides)}

    def test_accepts_a_bool_only_as_an_exact_bool(self):
        assert sanitize_overrides({"collector_gps_enabled": False}) == {"collector_gps_enabled": False}

    def test_rejects_ints_and_strings_for_a_bool_field(self):
        for value in (0, 1, "true", None):
            assert sanitize_overrides({"display_enabled": value}) == {}

    def test_rejects_a_bool_for_a_numeric_field(self):
        assert sanitize_overrides({"idle_threshold_s": True}) == {}
        assert sanitize_overrides({"system_shutdown_gpio": True}) == {}

    def test_int_field_accepts_an_integral_float_as_int(self):
        result = sanitize_overrides({"gnss_receiver_retries": 3.0})

        assert result == {"gnss_receiver_retries": 3}
        assert type(result["gnss_receiver_retries"]) is int

    def test_int_field_rejects_a_fractional_float(self):
        assert sanitize_overrides({"gnss_receiver_retries": 2.5}) == {}

    def test_float_field_rejects_nan_and_infinity(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            assert sanitize_overrides({"idle_threshold_s": value}) == {}
            assert sanitize_overrides({"movement_gate_home_latitude": value}) == {}

    def test_an_absurdly_large_integer_is_dropped_instead_of_raising(self):
        assert sanitize_overrides({"idle_threshold_s": 10**400, "gnss_receiver_retries": 10**400}) == {}

    def test_enum_matches_by_exact_string_only(self):
        assert sanitize_overrides({"collector_capture_gate": "time"}) == {"collector_capture_gate": "time"}
        for value in ("Time", " time", "time ", "hourly", 1, None):
            assert sanitize_overrides({"collector_capture_gate": value}) == {}

    def test_enum_values_per_field(self):
        assert sanitize_overrides({"system_mode": "waiting"}) == {"system_mode": "waiting"}
        assert sanitize_overrides({"device_mission_type": "drone"}) == {"device_mission_type": "drone"}
        assert sanitize_overrides({"modem_mode": "sa_scan"}) == {"modem_mode": "sa_scan"}
        assert sanitize_overrides({"system_mode": "bogus"}) == {}

    def test_nullable_float_accepts_none_and_in_range_floats(self):
        assert sanitize_overrides({"movement_gate_home_latitude": None}) == {"movement_gate_home_latitude": None}
        assert sanitize_overrides({"movement_gate_home_latitude": 48.1}) == {"movement_gate_home_latitude": 48.1}

    def test_nullable_float_enforces_its_bounds(self):
        assert sanitize_overrides({"movement_gate_home_latitude": 91.0}) == {}
        assert sanitize_overrides({"movement_gate_home_longitude": -181.0}) == {}

    def test_none_is_rejected_for_a_non_nullable_field(self):
        assert sanitize_overrides({"idle_threshold_s": None}) == {}

    def test_gpio_pin_bounds(self):
        assert sanitize_overrides({"system_shutdown_gpio": 27}) == {"system_shutdown_gpio": 27}
        assert sanitize_overrides({"system_shutdown_gpio": 28}) == {}
        assert sanitize_overrides({"system_shutdown_gpio": 1}) == {}

    def test_removed_quality_threshold_fields_are_disallowed(self):
        for field in ("rsrp_threshold", "rsrq_threshold", "sinr_threshold"):
            assert sanitize_overrides({field: -100.0}) == {}


class TestTypedOverridesFile:
    def test_round_trips_every_kind(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        values = {
            "idle_threshold_s": 300.5, "gnss_receiver_retries": 4, "collector_gps_enabled": False,
            "collector_capture_gate": "time", "movement_gate_home_latitude": 48.1,
        }

        write_overrides_file(path, values)

        assert read_overrides_file(path) == values
        assert "collector_gps_enabled = false" in path.read_text()

    def test_a_none_value_is_stored_in_the_cleared_list(self, tmp_path):
        path = tmp_path / "config_overrides.toml"

        write_overrides_file(path, {"movement_gate_home_latitude": None, "idle_threshold_s": 300})

        assert 'cleared = ["movement_gate_home_latitude"]' in path.read_text()
        assert read_overrides_file(path) == {"movement_gate_home_latitude": None, "idle_threshold_s": 300}

    def test_a_later_value_replaces_a_cleared_field(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        write_overrides_file(path, {"movement_gate_home_latitude": None})

        write_overrides_file(path, {"movement_gate_home_latitude": 48.1})

        assert read_overrides_file(path) == {"movement_gate_home_latitude": 48.1}
        assert "cleared" not in path.read_text()

    def test_cleared_accepts_only_nullable_fields(self, tmp_path, caplog):
        path = tmp_path / "config_overrides.toml"
        path.write_text('cleared = ["movement_gate_home_longitude", "idle_threshold_s", "heartbeat_interval_s"]\n')

        with caplog.at_level(logging.WARNING):
            result = read_overrides_file(path)

        assert result == {"movement_gate_home_longitude": None}
        assert "non-nullable" in caplog.text

    def test_a_malformed_cleared_entry_is_ignored(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        path.write_text('cleared = "movement_gate_home_latitude"\n')

        assert read_overrides_file(path) == {}

    def test_an_unreadable_file_reads_as_empty_instead_of_raising(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        path.write_text("this is = = not toml\n")

        assert read_overrides_file(path) == {}


class TestLoadOverrides:
    def test_drops_unknown_and_invalid_fields_instead_of_raising(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        path.write_text('rsrp_threshold = -90.0\nidle_threshold_s = 99999999\nheartbeat_interval_s = 30\n')

        overrides = load_overrides(path)

        assert overrides == ConfigOverrides(heartbeat_interval_s=30.0)

    def test_a_cleared_field_loads_as_the_cleared_marker(self, tmp_path):
        path = tmp_path / "config_overrides.toml"
        write_overrides_file(path, {"movement_gate_home_latitude": None})

        assert load_overrides(path).movement_gate_home_latitude is CLEARED


class TestApplyTypedOverrides:
    def test_applies_every_new_field_to_its_target(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(
            collector_capture_gate="time", collector_position_threshold=30.0,
            collector_capture_interval_s=20.0, collector_wait_log_interval=10.0,
            collector_gps_enabled=False, collector_gps_disabled_poll_interval_s=8.0,
            movement_gate_home_latitude=48.1, movement_gate_home_longitude=11.5,
            movement_gate_home_departure_threshold=200.0, latency_test_enabled=True,
            system_mode="waiting", device_mission_type="drone", display_enabled=True,
            display_refresh_interval_s=5.0, modem_mode="sa_scan", modem_sim_unlock_poll_attempts=20,
            modem_sim_unlock_poll_interval=2.0, gnss_receiver_retries=5, gnss_receiver_retry_delay_s=1.5,
            system_running_on_pi=False, system_shutdown_gpio=22,
        ))

        assert config.collector.capture_gate == "time"
        assert config.collector.position_threshold == 30.0
        assert config.collector.capture_interval_s == 20.0
        assert config.collector.wait_log_interval == 10.0
        assert config.collector.gps_enabled is False
        assert config.collector.gps_disabled_poll_interval_s == 8.0
        assert (config.movement_gate.home_latitude, config.movement_gate.home_longitude) == (48.1, 11.5)
        assert config.movement_gate.home_departure_threshold == 200.0
        assert config.latency_test.enabled is True
        assert config.system.mode == "waiting"
        assert config.device.mission_type == "drone"
        assert config.display.enabled is True
        assert config.display.refresh_interval_s == 5.0
        assert config.modem.mode == "sa_scan"
        assert config.modem.sim_unlock_poll_attempts == 20
        assert config.modem.sim_unlock_poll_interval == 2.0
        assert config.gnss_receiver.retries == 5
        assert config.gnss_receiver.retry_delay_s == 1.5
        assert config.system.running_on_pi is False
        assert config.system.shutdown_gpio == 22

    def test_cleared_resets_a_nullable_field_to_none(self):
        config = make_app_config()
        config.movement_gate.home_latitude = 48.1

        apply_overrides(config, ConfigOverrides(movement_gate_home_latitude=CLEARED))

        assert config.movement_gate.home_latitude is None

    def test_integer_valued_float_field_is_stored_as_float(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(collector_position_threshold=30))

        assert type(config.collector.position_threshold) is float


class TestMovementGateEnabledOverride:
    def test_applies_to_the_movement_gate_section(self):
        config = make_app_config()

        apply_overrides(config, ConfigOverrides(movement_gate_enabled=False))

        assert config.movement_gate.enabled is False

    def test_is_promoted_into_config_toml(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"movement_gate_enabled": False})

        promoted = promote_overrides(config_path)

        assert promoted == {"movement_gate_enabled": (None, False)}
        assert "enabled = false" in config_path.read_text()

    def test_is_reported_in_the_live_config_payload(self):
        from measurement_software.status.backend_status import ConfigStatusReporter
        config = make_app_config()
        config.movement_gate.enabled = False

        assert ConfigStatusReporter(config).as_payload()["movement_gate_enabled"] is False


class TestPromoteTypedOverrides:
    def test_promotes_a_key_that_is_absent_from_config_toml(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"movement_gate_home_latitude": 48.1})

        promoted = promote_overrides(config_path)

        assert promoted == {"movement_gate_home_latitude": (None, 48.1)}
        assert "home_latitude = 48.1" in config_path.read_text()

    def test_promotes_into_a_section_missing_from_config_toml(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"display_enabled": True})

        promote_overrides(config_path)

        from measurement_software.core.config import load_config
        assert load_config(config_path).display.enabled is True

    def test_promoting_a_cleared_field_removes_the_key(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        config_path.write_text(config_path.read_text().replace(
            "confirmations_required = 2\n", "confirmations_required = 2\nhome_latitude = 48.1\n"
        ))
        overrides_path = tmp_path / "config_overrides.toml"
        write_overrides_file(overrides_path, {"movement_gate_home_latitude": None})

        promoted = promote_overrides(config_path)

        assert promoted == {"movement_gate_home_latitude": (48.1, None)}
        assert "home_latitude" not in config_path.read_text()
        assert read_overrides_file(overrides_path) == {}

    def test_promotes_a_bool_as_valid_toml(self, tmp_path):
        config_path = _write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"collector_gps_enabled": False})

        promote_overrides(config_path)

        assert "gps_enabled = false" in config_path.read_text()


class TestMigrateLegacyMode:
    def _paths(self, tmp_path):
        return tmp_path / "config_overrides.toml", tmp_path / "mode_override.toml"

    def test_is_a_no_op_without_a_legacy_file(self, tmp_path):
        overrides_path, legacy_path = self._paths(tmp_path)

        migrate_legacy_mode_override(overrides_path, legacy_path)

        assert not overrides_path.exists()

    def test_migrates_a_valid_mode_and_removes_the_legacy_file(self, tmp_path):
        overrides_path, legacy_path = self._paths(tmp_path)
        legacy_path.write_text('mode = "waiting"\n')

        migrate_legacy_mode_override(overrides_path, legacy_path)

        assert read_overrides_file(overrides_path) == {"system_mode": "waiting"}
        assert not legacy_path.exists()

    def test_keeps_the_other_overrides_already_on_disk(self, tmp_path):
        overrides_path, legacy_path = self._paths(tmp_path)
        write_overrides_file(overrides_path, {"idle_threshold_s": 300})
        legacy_path.write_text('mode = "waiting"\n')

        migrate_legacy_mode_override(overrides_path, legacy_path)

        assert read_overrides_file(overrides_path) == {"idle_threshold_s": 300, "system_mode": "waiting"}

    def test_an_existing_system_mode_wins(self, tmp_path):
        overrides_path, legacy_path = self._paths(tmp_path)
        write_overrides_file(overrides_path, {"system_mode": "auto"})
        legacy_path.write_text('mode = "waiting"\n')

        migrate_legacy_mode_override(overrides_path, legacy_path)

        assert read_overrides_file(overrides_path) == {"system_mode": "auto"}
        assert not legacy_path.exists()

    def test_an_invalid_mode_is_dropped_and_the_file_removed(self, tmp_path):
        overrides_path, legacy_path = self._paths(tmp_path)
        legacy_path.write_text('mode = "bogus"\n')

        migrate_legacy_mode_override(overrides_path, legacy_path)

        assert not overrides_path.exists()
        assert not legacy_path.exists()

    def test_unreadable_content_is_dropped_and_the_file_removed(self, tmp_path):
        overrides_path, legacy_path = self._paths(tmp_path)
        legacy_path.write_text("not = = toml\n")

        migrate_legacy_mode_override(overrides_path, legacy_path)

        assert not overrides_path.exists()
        assert not legacy_path.exists()

    def test_rerun_after_a_crash_between_write_and_delete_is_idempotent(self, tmp_path):
        overrides_path, legacy_path = self._paths(tmp_path)
        write_overrides_file(overrides_path, {"system_mode": "waiting"})
        legacy_path.write_text('mode = "waiting"\n')

        migrate_legacy_mode_override(overrides_path, legacy_path)
        migrate_legacy_mode_override(overrides_path, legacy_path)

        assert read_overrides_file(overrides_path) == {"system_mode": "waiting"}
        assert not legacy_path.exists()

    def test_the_legacy_file_is_kept_when_the_write_fails(self, tmp_path, monkeypatch):
        import measurement_software.core.config_sources as config_sources_module

        overrides_path, legacy_path = self._paths(tmp_path)
        legacy_path.write_text('mode = "waiting"\n')

        def failing_write(path, fields_to_write):
            raise OSError("disk full")

        monkeypatch.setattr(config_sources_module, "_write_overrides_raw", failing_write)

        migrate_legacy_mode_override(overrides_path, legacy_path)

        assert legacy_path.exists()
