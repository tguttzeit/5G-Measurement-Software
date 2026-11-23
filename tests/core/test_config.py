import json
import logging
import socket
from pathlib import Path

import pytest

from measurement_software.core.config import (
    LAST_GOOD_LINK_SETTINGS_FILENAME,
    CollectorConfig,
    LinkWatchdogConfig,
    QuectelCmConfig,
    load_config,
    load_link_settings,
)

REQUIRED_SECTIONS = """
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


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path


def test_load_config_applies_defaults_for_omitted_optional_sections(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.modem.type == "quectel"
    assert config.modem.port == "/dev/ttyUSB2"
    assert config.uploader.path == "/api/measurements"
    assert config.uploader.latency_path == "/api/latency-results"
    assert config.backend.url == ""
    assert config.backend.device_key == ""
    assert config.heartbeat.path == "/api/heartbeat"
    assert config.heartbeat.interval_s == 15.0
    assert config.device.device_id == socket.gethostname()
    assert config.device.mission_type == "ground"
    assert config.collector.max_idle_time == 1800
    assert config.collector.gps_enabled is True
    assert config.collector.gps_disabled_poll_interval_s == 5.0
    assert config.movement_gate.enabled is True
    assert config.movement_gate.poll_interval_s == 45.0
    assert config.movement_gate.movement_threshold == 10.0
    assert config.movement_gate.confirmations_required == 2
    assert config.movement_gate.home_latitude is None
    assert config.movement_gate.home_longitude is None
    assert config.movement_gate.home_departure_threshold == 100.0
    assert config.quectel_cm.binary == "/add/your/quectel-CM/path/here"
    assert config.quectel_cm.args == ()
    assert config.run_status.empty_captures_until_pipeline_broken == 3
    assert config.heartbeat.enabled is True
    assert config.heartbeat.hmac_secret == ""
    assert config.selftest.results_url == ""
    assert config.gps_fix_test.status_url == ""
    assert config.gps_fix_test.report_interval_s == 2.0
    assert config.gps_fix_test.timeout_s == 300.0
    assert config.storage.low_free_space_warning_bytes == 500_000_000
    assert config.display.enabled is True
    assert config.display.type == "ssd1306"
    assert config.display.i2c_port == 1
    assert config.display.i2c_address == 0x3C
    assert config.display.refresh_interval_s == 2.0
    assert config.latency_test.enabled is True
    assert config.latency_test.host == ""
    assert config.latency_test.flent_binary == "flent"
    assert config.latency_test.baseline_test == "ping"
    assert config.latency_test.baseline_interval_s == 60.0
    assert config.latency_test.baseline_length_s == 10
    assert config.latency_test.load_test == "rrul"
    assert config.latency_test.load_interval_s == 900.0
    assert config.latency_test.load_length_s == 60
    assert config.latency_test.poll_interval_s == 30.0
    assert config.latency_test.movement_window_s == 30.0
    assert config.latency_test.timeout_s == 300.0
    assert config.logging.level == "INFO"
    assert config.system.running_on_pi is True
    assert config.system.network_interfaces == ("wlan0", "wwan0")
    assert config.modem.retries == 3
    assert config.modem.retry_delay_s == 0.75
    assert config.gnss_receiver.retries == 3
    assert config.gnss_receiver.retry_delay_s == 0.75


def test_load_config_overrides_serial_retry_settings(tmp_path):
    content = """
[modem]
type = "quectel"
port = "/dev/ttyUSB2"
baud_rate = 115200
timeout = 1.0
retries = 5
retry_delay_s = 1.0

[gnss_receiver]
type = "quectel"
port = "/dev/ttyUSB3"
baud_rate = 9600
timeout = 1.0
retries = 1
retry_delay_s = 0.5

[uploader]
upload_dir = "/data/uploads"
"""
    config = load_config(write_config(tmp_path, content))

    assert config.modem.retries == 5
    assert config.modem.retry_delay_s == 1.0
    assert config.gnss_receiver.retries == 1
    assert config.gnss_receiver.retry_delay_s == 0.5


def test_load_config_overrides_defaults_when_optional_sections_present(tmp_path):
    content = REQUIRED_SECTIONS + """
[device]
device_id = "pi-north-01"
mission_type = "drone"

[collector]
max_idle_time = 42
position_threshold = 5
gps_enabled = false
gps_disabled_poll_interval_s = 2.5

[logging]
level = "DEBUG"

[system]
running_on_pi = false
"""
    config = load_config(write_config(tmp_path, content))

    assert config.device.device_id == "pi-north-01"
    assert config.device.mission_type == "drone"
    assert config.collector.max_idle_time == 42
    assert config.collector.position_threshold == 5
    assert config.collector.gps_enabled is False
    assert config.collector.gps_disabled_poll_interval_s == 2.5
    assert config.logging.level == "DEBUG"
    assert config.system.running_on_pi is False


def test_load_config_reads_gps_fix_test_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[gps_fix_test]
status_url = "https://backend.example.org/api/testing/gps-fix-test-status"
report_interval_s = 1.5
timeout_s = 120.0
"""
    config = load_config(write_config(tmp_path, content))

    assert config.gps_fix_test.status_url == "https://backend.example.org/api/testing/gps-fix-test-status"
    assert config.gps_fix_test.report_interval_s == 1.5
    assert config.gps_fix_test.timeout_s == 120.0


def test_load_config_reads_movement_gate_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[movement_gate]
poll_interval_s = 60.0
movement_threshold = 20.0
confirmations_required = 3
home_latitude = 48.13
home_longitude = 11.58
home_departure_threshold = 250.0
"""
    config = load_config(write_config(tmp_path, content))

    assert config.movement_gate.poll_interval_s == 60.0
    assert config.movement_gate.movement_threshold == 20.0
    assert config.movement_gate.confirmations_required == 3
    assert config.movement_gate.home_latitude == 48.13
    assert config.movement_gate.home_longitude == 11.58
    assert config.movement_gate.home_departure_threshold == 250.0


def test_load_config_raises_when_only_home_latitude_is_set(tmp_path):
    content = REQUIRED_SECTIONS + """
[movement_gate]
home_latitude = 48.13
"""
    with pytest.raises(ValueError, match="home_latitude and home_longitude"):
        load_config(write_config(tmp_path, content))


def test_load_config_raises_when_only_home_longitude_is_set(tmp_path):
    content = REQUIRED_SECTIONS + """
[movement_gate]
home_longitude = 11.58
"""
    with pytest.raises(ValueError, match="home_latitude and home_longitude"):
        load_config(write_config(tmp_path, content))


def test_load_config_reads_quectel_cm_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[quectel_cm]
binary = "/usr/bin/quectel-CM"
args = ["-s", "internet"]
"""
    config = load_config(write_config(tmp_path, content))

    assert config.quectel_cm.binary == "/usr/bin/quectel-CM"
    assert config.quectel_cm.args == ("-s", "internet")


def test_load_config_reads_backend_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[backend]
url = "https://backend.example.org"
device_key = "test-api-key-123"
"""
    config = load_config(write_config(tmp_path, content))

    assert config.backend.url == "https://backend.example.org"
    assert config.backend.device_key == "test-api-key-123"


def test_load_config_reads_heartbeat_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[heartbeat]
enabled = true
path = "/status"
interval_s = 30.0
hmac_secret = "test-hmac-secret"
"""
    config = load_config(write_config(tmp_path, content))

    assert config.heartbeat.enabled is True
    assert config.heartbeat.path == "/status"
    assert config.heartbeat.interval_s == 30.0
    assert config.heartbeat.timeout_s == 10.0
    assert config.heartbeat.hmac_secret == "test-hmac-secret"


def test_load_config_reads_selftest_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[selftest]
results_url = "https://backend.example.org/api/testing/results"
"""
    config = load_config(write_config(tmp_path, content))

    assert config.selftest.results_url == "https://backend.example.org/api/testing/results"


def test_load_config_reads_storage_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[storage]
low_free_space_warning_bytes = 100_000_000
"""
    config = load_config(write_config(tmp_path, content))

    assert config.storage.low_free_space_warning_bytes == 100_000_000


def test_load_config_reads_display_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[display]
enabled = true
i2c_port = 3
i2c_address = 0x3D
refresh_interval_s = 5.0
"""
    config = load_config(write_config(tmp_path, content))

    assert config.display.enabled is True
    assert config.display.type == "ssd1306"
    assert config.display.i2c_port == 3
    assert config.display.i2c_address == 0x3D
    assert config.display.refresh_interval_s == 5.0


def test_load_config_reads_latency_test_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[latency_test]
enabled = true
host = "testserver.example.org"
baseline_interval_s = 30.0
load_test = "rrul_be"
load_length_s = 45
movement_window_s = 10.0
"""
    config = load_config(write_config(tmp_path, content))

    assert config.latency_test.enabled is True
    assert config.latency_test.host == "testserver.example.org"
    assert config.latency_test.baseline_interval_s == 30.0
    assert config.latency_test.load_test == "rrul_be"
    assert config.latency_test.load_length_s == 45
    assert config.latency_test.movement_window_s == 10.0
    # Settings the section leaves out keep their defaults.
    assert config.latency_test.baseline_test == "ping"
    assert config.latency_test.load_interval_s == 900.0


def test_load_config_reads_run_status_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[run_status]
empty_captures_until_pipeline_broken = 5
"""
    config = load_config(write_config(tmp_path, content))

    assert config.run_status.empty_captures_until_pipeline_broken == 5


def test_load_config_merges_sibling_config_overrides_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config_overrides.toml").write_text('collector_position_threshold = 30.0\nidle_threshold_s = 600\n')

    config = load_config(config_path)

    assert config.collector.position_threshold == 30.0
    assert config.collector.max_idle_time == 600


def test_load_config_without_overrides_file_uses_defaults(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.collector.position_threshold == 15
    assert config.collector.max_idle_time == 1800


def test_load_config_ignores_removed_shutdown_gpio_with_a_warning(tmp_path, caplog):
    content = REQUIRED_SECTIONS + """
[system]
shutdown_gpio = 17
running_on_pi = false
"""
    with caplog.at_level(logging.WARNING):
        config = load_config(write_config(tmp_path, content))

    assert config.system.running_on_pi is False
    assert not hasattr(config.system, "shutdown_gpio")
    assert caplog.text.count("system.shutdown_gpio") == 1


def test_load_config_defaults_system_mode_to_auto(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.system.mode == "auto"


def test_load_config_reads_system_mode_manual(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
mode = "manual"
"""
    config = load_config(write_config(tmp_path, content))

    assert config.system.mode == "manual"


def test_load_config_leaves_modem_port_unset_for_by_id_lookup_when_omitted(tmp_path):
    content = REQUIRED_SECTIONS.replace('port = "/dev/ttyUSB2"\n', "", 1)

    config = load_config(write_config(tmp_path, content))

    assert config.modem.port is None


def test_load_config_defaults_modem_reconnect_settings(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.collector.modem_reconnect_attempts == 10
    assert config.collector.modem_reconnect_delay_s == 3.0


@pytest.mark.parametrize("setting", ["modem_reconnect_attempts = 0", "modem_reconnect_delay_s = -1.0"])
def test_load_config_raises_on_invalid_modem_reconnect_settings(tmp_path, setting):
    content = REQUIRED_SECTIONS + f"""
[collector]
{setting}
"""
    with pytest.raises(ValueError, match="collector.modem_reconnect"):
        load_config(write_config(tmp_path, content))


def test_load_config_defaults_crash_limits_and_link_watchdog(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.system.crash_same_error_limit == 3
    assert config.system.crash_reboot_limit == 5
    assert config.link_watchdog.enabled is True
    assert config.link_watchdog.interface == "wwan0"
    assert config.link_watchdog.restart_after_s == 180.0
    assert config.link_watchdog.max_restart_interval_s == 1800.0
    assert config.link_watchdog.radio_reset_after_restarts == 2
    assert config.heartbeat.success_log_interval_s == 600.0


def test_load_config_accepts_a_maximum_interval_equal_to_the_first(tmp_path):
    content = REQUIRED_SECTIONS + """
[link_watchdog]
restart_after_s = 300.0
max_restart_interval_s = 300.0
"""
    assert load_config(write_config(tmp_path, content)).link_watchdog.max_restart_interval_s == 300.0


def test_load_config_defaults_clock_to_no_ntp_server(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.clock.ntp_server == ""
    assert config.clock.ntp_timeout_s == 3.0


def test_load_config_reads_the_clock_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[clock]
ntp_server = "192.0.2.10"
ntp_timeout_s = 5.0
"""
    config = load_config(write_config(tmp_path, content))

    assert config.clock.ntp_server == "192.0.2.10"
    assert config.clock.ntp_timeout_s == 5.0


@pytest.mark.parametrize(("section", "setting", "message"), [
    ("clock", "ntp_timeout_s = 0", "clock.ntp_timeout_s"),
    ("system", "crash_same_error_limit = 0", "system.crash_same_error_limit"),
    ("system", "crash_reboot_limit = 0", "system.crash_reboot_limit"),
    ("link_watchdog", "restart_after_s = 0", "link_watchdog.restart_after_s"),
    ("link_watchdog", "check_interval_s = -1", "link_watchdog.check_interval_s"),
    ("link_watchdog", "radio_reset_after_restarts = -1", "link_watchdog.radio_reset_after_restarts"),
    ("link_watchdog", "max_restart_interval_s = 179.0", "link_watchdog.max_restart_interval_s"),
])
def test_load_config_raises_on_invalid_crash_or_watchdog_settings(tmp_path, section, setting, message):
    content = REQUIRED_SECTIONS + f"""
[{section}]
{setting}
"""
    with pytest.raises(ValueError, match=message):
        load_config(write_config(tmp_path, content))


def test_load_config_raises_on_unrecognized_system_mode(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
mode = "bogus"
"""
    with pytest.raises(ValueError, match="system.mode"):
        load_config(write_config(tmp_path, content))


def test_load_config_applies_system_mode_from_the_overrides_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config_overrides.toml").write_text('system_mode = "manual"\n')

    assert load_config(config_path).system.mode == "manual"


def test_load_config_overrides_file_mode_takes_precedence_over_config_toml(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
mode = "manual"
"""
    config_path = write_config(tmp_path, content)
    (tmp_path / "config_overrides.toml").write_text('system_mode = "auto"\n')

    assert load_config(config_path).system.mode == "auto"


def test_load_config_reads_the_legacy_waiting_mode_from_the_overrides_file_as_manual(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config_overrides.toml").write_text('system_mode = "waiting"\n')

    assert load_config(config_path).system.mode == "manual"


def test_load_config_reads_the_legacy_waiting_mode_in_config_toml_as_manual(tmp_path, caplog):
    content = REQUIRED_SECTIONS + """
[system]
mode = "waiting"
"""
    with caplog.at_level(logging.WARNING):
        config = load_config(write_config(tmp_path, content))

    assert config.system.mode == "manual"
    assert [r.message for r in caplog.records if "waiting" in r.message and r.levelno == logging.WARNING]


def test_load_config_still_rejects_a_case_variant_of_the_legacy_mode_in_config_toml(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
mode = "Waiting"
"""
    with pytest.raises(ValueError, match="system.mode"):
        load_config(write_config(tmp_path, content))


def test_load_config_ignores_an_invalid_mode_in_the_overrides_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config_overrides.toml").write_text('system_mode = "bogus"\n')

    assert load_config(config_path).system.mode == "auto"


def test_load_config_no_longer_reads_a_mode_override_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "mode_override.toml").write_text('mode = "manual"\n')

    assert load_config(config_path).system.mode == "auto"


def test_load_config_ignores_system_mode_in_config_local_toml(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config.local.toml").write_text('[system]\nmode = "manual"\n')

    assert load_config(config_path).system.mode == "auto"


def test_load_config_applies_a_cleared_home_position_override(tmp_path):
    content = REQUIRED_SECTIONS + """
[movement_gate]
home_latitude = 48.1
home_longitude = 11.5
"""
    config_path = write_config(tmp_path, content)
    (tmp_path / "config_overrides.toml").write_text(
        'cleared = ["movement_gate_home_latitude", "movement_gate_home_longitude"]\n'
    )

    config = load_config(config_path)

    assert config.movement_gate.home_latitude is None
    assert config.movement_gate.home_longitude is None


class TestBadOverridesNeverBreakBoot:
    def _load(self, tmp_path, overrides_text, base_text=REQUIRED_SECTIONS):
        config_path = write_config(tmp_path, base_text)
        (tmp_path / "config_overrides.toml").write_text(overrides_text)
        return load_config(config_path)

    def test_a_leftover_shutdown_gpio_override_is_ignored_next_to_a_valid_one(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            config = self._load(tmp_path, "system_shutdown_gpio = 2\nidle_threshold_s = 600\n")

        assert config.collector.max_idle_time == 600
        assert "system_shutdown_gpio" in caplog.text

    def test_a_half_set_home_position_override_is_dropped(self, tmp_path):
        config = self._load(tmp_path, "movement_gate_home_latitude = 48.1\n")

        assert config.movement_gate.home_latitude is None
        assert config.movement_gate.home_longitude is None

    def test_a_complete_home_position_override_is_applied(self, tmp_path):
        config = self._load(
            tmp_path, "movement_gate_home_latitude = 48.1\nmovement_gate_home_longitude = 11.5\n"
        )

        assert (config.movement_gate.home_latitude, config.movement_gate.home_longitude) == (48.1, 11.5)

    def test_clearing_only_one_of_a_configured_home_position_pair_is_dropped(self, tmp_path):
        base = REQUIRED_SECTIONS + "\n[movement_gate]\nhome_latitude = 48.1\nhome_longitude = 11.5\n"

        config = self._load(tmp_path, 'cleared = ["movement_gate_home_latitude"]\n', base)

        assert (config.movement_gate.home_latitude, config.movement_gate.home_longitude) == (48.1, 11.5)

    def test_a_violation_in_config_toml_itself_still_raises(self, tmp_path):
        base = REQUIRED_SECTIONS + "\n[movement_gate]\nhome_latitude = 48.1\n"

        with pytest.raises(ValueError, match="home"):
            self._load(tmp_path, "idle_threshold_s = 600\n", base)

    def test_a_corrupt_overrides_file_does_not_break_boot(self, tmp_path):
        config = self._load(tmp_path, "this is = = not toml\n")

        assert config.collector.max_idle_time == 1800

    def test_a_stale_removed_field_in_the_overrides_file_does_not_break_boot(self, tmp_path):
        config = self._load(tmp_path, "rsrp_threshold = -90.0\nidle_threshold_s = 600\n")

        assert config.collector.max_idle_time == 600


def test_load_config_warns_on_unset_identity_fields_when_running_on_pi(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert "backend.url is unset or still a placeholder" in caplog.text
    assert "backend.device_key is unset" in caplog.text
    assert "heartbeat.device_id is unset" in caplog.text
    assert "quectel_cm.binary is unset or still a placeholder" in caplog.text


def test_load_config_does_not_warn_on_hmac_secret_when_heartbeat_disabled(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        load_config(write_config(tmp_path, REQUIRED_SECTIONS + "\n[heartbeat]\nenabled = false\n"))

    assert "hmac_secret" not in caplog.text


def test_load_config_warns_on_hmac_secret_when_heartbeat_enabled(tmp_path, caplog):
    content = REQUIRED_SECTIONS + """
[heartbeat]
enabled = true
"""
    with caplog.at_level(logging.WARNING):
        load_config(write_config(tmp_path, content))

    assert "heartbeat.hmac_secret is unset while heartbeat.enabled" in caplog.text


def test_load_config_does_not_warn_when_not_running_on_pi(tmp_path, caplog):
    content = REQUIRED_SECTIONS + """
[system]
running_on_pi = false
"""
    with caplog.at_level(logging.WARNING):
        load_config(write_config(tmp_path, content))

    assert caplog.text == ""


def test_load_config_does_not_warn_when_identity_fields_are_set_via_local_config(tmp_path, caplog):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config.local.toml").write_text("""
[backend]
url = "https://real.example.org"
device_key = "real-key"

[heartbeat]
device_id = "pi-01"
hmac_secret = "real-secret"

[quectel_cm]
binary = "/usr/local/bin/quectel-CM"
""")

    with caplog.at_level(logging.WARNING):
        load_config(config_path)

    assert caplog.text == ""

class TestLeftoverProfileTables:
    DRONE_PROFILE = """
[profiles.drone]
movement_gate_enabled = false
idle_threshold_s = 0
"""

    def drone_config(self, tmp_path):
        content = REQUIRED_SECTIONS + '\n[device]\nmission_type = "drone"\n' + self.DRONE_PROFILE
        return write_config(tmp_path, content)

    def test_leftover_profile_table_does_not_fail_boot(self, tmp_path):
        config = load_config(self.drone_config(tmp_path))

        assert config.device.mission_type == "drone"

    def test_leftover_profile_values_are_not_applied(self, tmp_path):
        config = load_config(self.drone_config(tmp_path))

        assert config.movement_gate.enabled is True
        assert config.collector.max_idle_time == 1800

    def test_invalid_leftover_profile_content_does_not_fail_boot(self, tmp_path):
        content = REQUIRED_SECTIONS + '\n[profiles.boat]\nbogus = "x"\n'

        load_config(write_config(tmp_path, content))

    def test_leftover_profiles_that_are_not_tables_do_not_fail_boot(self, tmp_path):
        content = 'profiles = "x"\n' + REQUIRED_SECTIONS

        load_config(write_config(tmp_path, content))

    def test_warns_once_naming_the_tables_but_not_their_values(self, tmp_path, caplog):
        content = (
            REQUIRED_SECTIONS
            + self.DRONE_PROFILE
            + "\n[profiles.ground]\nidle_threshold_s = 4242\n"
        )

        with caplog.at_level(logging.WARNING):
            load_config(write_config(tmp_path, content))

        warnings = [r.getMessage() for r in caplog.records if "profiles" in r.getMessage()]
        assert len(warnings) == 1
        assert "[profiles.drone]" in warnings[0]
        assert "[profiles.ground]" in warnings[0]
        assert "movement_gate_enabled" not in warnings[0]
        assert "4242" not in warnings[0]

    def test_no_warning_without_profile_tables(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            load_config(write_config(tmp_path, REQUIRED_SECTIONS))

        assert not any("profiles" in r.getMessage() for r in caplog.records)

    def test_mission_type_override_still_applies_as_a_label(self, tmp_path):
        from measurement_software.core.config_sources import OVERRIDES_FILENAME

        (tmp_path / OVERRIDES_FILENAME).write_text('device_mission_type = "drone"\n')

        config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

        assert config.device.mission_type == "drone"
        assert config.movement_gate.enabled is True
        assert config.collector.max_idle_time == 1800


def test_shipped_config_toml_has_no_profile_tables():
    import tomllib

    with open(Path(__file__).parents[2] / "config.toml", "rb") as f:
        assert "profiles" not in tomllib.load(f)


def test_shipped_config_toml_keeps_the_collector_max_idle_time_default():
    import tomllib

    with open(Path(__file__).parents[2] / "config.toml", "rb") as f:
        shipped = tomllib.load(f)["collector"]

    assert shipped["max_idle_time"] == CollectorConfig().max_idle_time == 1800


def test_load_config_ignores_the_removed_scp_uploader_fields_with_one_warning_naming_only_the_fields(tmp_path, caplog):
    content = REQUIRED_SECTIONS + """upload_user = "secret-user"
upload_host = "secret-host.example.org"
upload_port = 2222
remote_dir = "/secret/dir/"
"""
    with caplog.at_level(logging.WARNING):
        config = load_config(write_config(tmp_path, content))

    assert config.uploader.upload_dir == "/data/uploads"
    warnings = [r.getMessage() for r in caplog.records if "upload_user" in r.getMessage()]
    assert len(warnings) == 1
    for name in ("upload_user", "upload_host", "upload_port", "remote_dir"):
        assert name in warnings[0]
    for value in ("secret-user", "secret-host.example.org", "2222", "/secret/dir/"):
        assert value not in warnings[0]


def test_load_config_does_not_warn_about_scp_fields_when_none_are_present(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert not [r for r in caplog.records if "upload_user" in r.getMessage()]


class TestLoadLinkSettings:
    """What the emergency link needs when load_config() itself fails: read leniently, never raise."""

    def test_reads_the_link_sections_even_when_the_rest_of_the_config_is_broken(self, tmp_path):
        config_path = write_config(tmp_path, """
[modem]
type = 42
[system]
running_on_pi = false
[quectel_cm]
binary = "/opt/quectel-CM"
args = ["-s", "apn.example"]
[link_watchdog]
interface = "wwan1"
restart_after_s = 60
max_restart_interval_s = 600.0
""")
        settings = load_link_settings(config_path)

        assert settings.running_on_pi is False
        assert settings.quectel_cm == QuectelCmConfig(binary="/opt/quectel-CM", args=("-s", "apn.example"))
        assert settings.link_watchdog == LinkWatchdogConfig(
            interface="wwan1", restart_after_s=60.0, max_restart_interval_s=600.0,
        )

    def test_the_local_file_sets_the_binary(self, tmp_path):
        config_path = write_config(tmp_path, '[quectel_cm]\nbinary = "/add/your/quectel-CM/path/here"\n')
        (tmp_path / "config.local.toml").write_text('[quectel_cm]\nbinary = "/home/pi/quectel-CM"\n')

        assert load_link_settings(config_path).quectel_cm.binary == "/home/pi/quectel-CM"

    def test_a_placeholder_binary_falls_back_to_the_vendored_one(self, tmp_path):
        config_path = write_config(tmp_path, '[quectel_cm]\nbinary = "/add/your/quectel-CM/path/here"\n')

        binary = load_link_settings(config_path).quectel_cm.binary

        assert binary == str(tmp_path / "vendor" / "quectel-CM" / "quectel-CM")

    def test_unreadable_files_leave_the_built_in_defaults(self, tmp_path):
        config_path = write_config(tmp_path, "this is [not toml")
        (tmp_path / "config.local.toml").write_text("neither = [is this")

        settings = load_link_settings(config_path)

        assert settings.running_on_pi is True
        assert settings.quectel_cm.args == ()
        assert settings.quectel_cm.binary == str(tmp_path / "vendor" / "quectel-CM" / "quectel-CM")
        assert settings.link_watchdog == LinkWatchdogConfig()

    def write_last_good(self, tmp_path, payload) -> None:
        (tmp_path / LAST_GOOD_LINK_SETTINGS_FILENAME).write_text(json.dumps(payload))

    LAST_GOOD = {
        "system": {"running_on_pi": True},
        "quectel_cm": {"binary": "/opt/quectel-CM", "args": ["-s", "apn.example"]},
        "link_watchdog": {"interface": "wwan1", "restart_after_s": 60.0},
    }

    def test_an_unreadable_config_falls_back_to_the_last_good_link_settings(self, tmp_path):
        """One syntax error anywhere makes config.toml unreadable as a whole - with it the APN, and
        quectel-CM can't bring up the link without one."""
        config_path = write_config(tmp_path, "this is [not toml")
        self.write_last_good(tmp_path, self.LAST_GOOD)

        settings = load_link_settings(config_path)

        assert settings.quectel_cm == QuectelCmConfig(binary="/opt/quectel-CM", args=("-s", "apn.example"))
        assert settings.link_watchdog == LinkWatchdogConfig(interface="wwan1", restart_after_s=60.0)

    def test_a_missing_config_falls_back_to_the_last_good_link_settings(self, tmp_path):
        self.write_last_good(tmp_path, self.LAST_GOOD)

        assert load_link_settings(tmp_path / "config.toml").quectel_cm.args == ("-s", "apn.example")

    def test_a_readable_config_wins_over_the_last_good_link_settings(self, tmp_path):
        config_path = write_config(tmp_path, '[quectel_cm]\nargs = ["-s", "new.apn"]\n')
        self.write_last_good(tmp_path, self.LAST_GOOD)

        settings = load_link_settings(config_path)

        assert settings.quectel_cm.args == ("-s", "new.apn")
        assert settings.quectel_cm.binary == "/opt/quectel-CM"

    def test_the_local_file_wins_over_the_last_good_link_settings(self, tmp_path):
        self.write_last_good(tmp_path, self.LAST_GOOD)
        (tmp_path / "config.local.toml").write_text('[quectel_cm]\nbinary = "/home/pi/quectel-CM"\n')

        assert load_link_settings(tmp_path / "config.toml").quectel_cm.binary == "/home/pi/quectel-CM"

    def test_an_unreadable_last_good_file_is_ignored(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING)
        (tmp_path / LAST_GOOD_LINK_SETTINGS_FILENAME).write_text("{not json")

        settings = load_link_settings(tmp_path / "config.toml")

        assert settings.quectel_cm.args == ()
        assert LAST_GOOD_LINK_SETTINGS_FILENAME in caplog.text

    def test_a_missing_config_file_leaves_the_built_in_defaults(self, tmp_path):
        settings = load_link_settings(tmp_path / "config.toml")

        assert settings.link_watchdog == LinkWatchdogConfig()

    def test_a_field_of_the_wrong_type_is_ignored_without_logging_its_value(self, tmp_path, caplog):
        config_path = write_config(tmp_path, """
[quectel_cm]
args = "-s secret-apn"
[link_watchdog]
restart_after_s = "soon"
check_interval_s = 10.0
""")
        caplog.set_level(logging.WARNING)

        settings = load_link_settings(config_path)

        assert settings.quectel_cm.args == ()
        assert settings.link_watchdog.restart_after_s == 180.0
        assert settings.link_watchdog.check_interval_s == 10.0
        assert "link_watchdog.restart_after_s" in caplog.text
        assert "soon" not in caplog.text and "secret-apn" not in caplog.text

    def test_link_watchdog_values_that_fail_validation_fall_back_to_the_defaults(self, tmp_path):
        config_path = write_config(tmp_path, "[link_watchdog]\nrestart_after_s = 600.0\nmax_restart_interval_s = 60.0\n")

        assert load_link_settings(config_path).link_watchdog == LinkWatchdogConfig()

    def test_the_local_file_cannot_set_fields_outside_its_allow_list(self, tmp_path):
        config_path = write_config(tmp_path, "")
        (tmp_path / "config.local.toml").write_text(
            '[quectel_cm]\nargs = ["-x"]\n[link_watchdog]\ninterface = "eth0"\n[system]\nrunning_on_pi = false\n'
        )

        settings = load_link_settings(config_path)

        assert settings.quectel_cm.args == ()
        assert settings.link_watchdog.interface == "wwan0"
        assert settings.running_on_pi is True

    def test_reads_nothing_else_from_the_system_section(self, tmp_path, caplog):
        config_path = write_config(tmp_path, '[system]\nmode = 7\nrunning_on_pi = false\n')
        caplog.set_level(logging.WARNING)

        assert load_link_settings(config_path).running_on_pi is False
        assert "system.mode" not in caplog.text
