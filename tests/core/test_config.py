import logging
import socket
from pathlib import Path

import pytest

from measurement_software.core.config import load_config

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
    assert config.uploader.path == "/upload"
    assert config.backend.url == ""
    assert config.backend.device_key == ""
    assert config.heartbeat.path == "/heartbeat"
    assert config.device.device_id == socket.gethostname()
    assert config.device.mission_type == "ground"
    assert config.collector.max_idle_time == 200
    assert config.collector.gps_enabled is True
    assert config.collector.gps_disabled_poll_interval_s == 5.0
    assert config.movement_gate.enabled is True
    assert config.movement_gate.poll_interval_s == 45.0
    assert config.movement_gate.movement_threshold == 15.0
    assert config.movement_gate.confirmations_required == 2
    assert config.movement_gate.home_latitude is None
    assert config.movement_gate.home_longitude is None
    assert config.movement_gate.home_departure_threshold == 100.0
    assert config.quectel_cm.binary == "/add/your/quectel-CM/path/here"
    assert config.quectel_cm.args == ()
    assert config.run_status.empty_captures_until_pipeline_broken == 3
    assert config.heartbeat.enabled is False
    assert config.heartbeat.hmac_secret == ""
    assert config.selftest.results_url == ""
    assert config.gps_fix_test.status_url == ""
    assert config.gps_fix_test.report_interval_s == 2.0
    assert config.gps_fix_test.timeout_s == 300.0
    assert config.storage.low_free_space_warning_bytes == 500_000_000
    assert config.display.enabled is False
    assert config.display.type == "ssd1306"
    assert config.display.i2c_port == 1
    assert config.display.i2c_address == 0x3C
    assert config.display.refresh_interval_s == 2.0
    assert config.latency_test.enabled is False
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


def test_load_config_raises_on_shutdown_and_display_i2c_pin_conflict(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
shutdown_gpio = 2

[display]
enabled = true
"""
    with pytest.raises(ValueError, match="GPIO pin conflict"):
        load_config(write_config(tmp_path, content))


def test_load_config_does_not_raise_on_display_i2c_pin_conflict_when_display_disabled(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
shutdown_gpio = 2

[display]
enabled = false
"""
    config = load_config(write_config(tmp_path, content))

    assert config.system.shutdown_gpio == 2
    assert config.display.enabled is False


def required_sections_with_gnss_port(port: str) -> str:
    return """
[modem]
type = "quectel"
port = "/dev/ttyUSB2"
baud_rate = 115200
timeout = 1.0

[gnss_receiver]
type = "quectel"
port = \"""" + port + """\"
baud_rate = 9600
timeout = 1.0

[uploader]
upload_dir = "/data/uploads"
"""


def test_load_config_merges_sibling_config_overrides_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config_overrides.toml").write_text('collector_position_threshold = 30.0\nidle_threshold_s = 600\n')

    config = load_config(config_path)

    assert config.collector.position_threshold == 30.0
    assert config.collector.max_idle_time == 600


def test_load_config_without_overrides_file_uses_defaults(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.collector.position_threshold == 15
    assert config.collector.max_idle_time == 200


def test_load_config_raises_on_shutdown_and_gnss_onboard_uart_pin_conflict(tmp_path):
    content = required_sections_with_gnss_port("/dev/serial0") + """
[system]
shutdown_gpio = 14
"""
    with pytest.raises(ValueError, match="GPIO pin conflict"):
        load_config(write_config(tmp_path, content))


def test_load_config_does_not_raise_on_gnss_uart_pin_conflict_for_usb_adapter_port(tmp_path):
    content = required_sections_with_gnss_port("/dev/ttyUSB3") + """
[system]
shutdown_gpio = 14
"""
    config = load_config(write_config(tmp_path, content))

    assert config.system.shutdown_gpio == 14
    assert config.gnss_receiver.port == "/dev/ttyUSB3"


def test_load_config_defaults_system_mode_to_auto(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.system.mode == "auto"


def test_load_config_reads_system_mode_waiting(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
mode = "waiting"
"""
    config = load_config(write_config(tmp_path, content))

    assert config.system.mode == "waiting"


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
    assert config.link_watchdog.radio_reset_after_restarts == 2
    assert config.heartbeat.success_log_interval_s == 600.0


@pytest.mark.parametrize(("section", "setting", "message"), [
    ("system", "crash_same_error_limit = 0", "system.crash_same_error_limit"),
    ("system", "crash_reboot_limit = 0", "system.crash_reboot_limit"),
    ("link_watchdog", "restart_after_s = 0", "link_watchdog.restart_after_s"),
    ("link_watchdog", "check_interval_s = -1", "link_watchdog.check_interval_s"),
    ("link_watchdog", "radio_reset_after_restarts = -1", "link_watchdog.radio_reset_after_restarts"),
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
    (tmp_path / "config_overrides.toml").write_text('system_mode = "waiting"\n')

    assert load_config(config_path).system.mode == "waiting"


def test_load_config_overrides_file_mode_takes_precedence_over_config_toml(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
mode = "waiting"
"""
    config_path = write_config(tmp_path, content)
    (tmp_path / "config_overrides.toml").write_text('system_mode = "auto"\n')

    assert load_config(config_path).system.mode == "auto"


def test_load_config_ignores_an_invalid_mode_in_the_overrides_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config_overrides.toml").write_text('system_mode = "bogus"\n')

    assert load_config(config_path).system.mode == "auto"


def test_load_config_no_longer_reads_a_mode_override_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "mode_override.toml").write_text('mode = "waiting"\n')

    assert load_config(config_path).system.mode == "auto"


def test_load_config_ignores_system_mode_in_config_local_toml(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "config.local.toml").write_text('[system]\nmode = "waiting"\n')

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

    def test_shutdown_gpio_override_colliding_with_the_display_pins_is_dropped(self, tmp_path, caplog):
        base = REQUIRED_SECTIONS + "\n[display]\nenabled = true\n"

        with caplog.at_level(logging.ERROR):
            config = self._load(tmp_path, "system_shutdown_gpio = 2\n", base)

        assert config.system.shutdown_gpio == 17
        assert "GPIO pin conflict" in caplog.text

    def test_display_override_colliding_with_the_shutdown_pin_is_dropped(self, tmp_path):
        base = REQUIRED_SECTIONS + "\n[system]\nshutdown_gpio = 3\n"

        config = self._load(tmp_path, "display_enabled = true\n", base)

        assert config.display.enabled is False
        assert config.system.shutdown_gpio == 3

    def test_a_valid_override_survives_next_to_a_dropped_one(self, tmp_path):
        base = REQUIRED_SECTIONS + "\n[display]\nenabled = true\n"

        config = self._load(tmp_path, "system_shutdown_gpio = 2\nidle_threshold_s = 600\n", base)

        assert config.system.shutdown_gpio == 17
        assert config.collector.max_idle_time == 600

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
        base = REQUIRED_SECTIONS + "\n[system]\nshutdown_gpio = 2\n\n[display]\nenabled = true\n"

        with pytest.raises(ValueError, match="GPIO pin conflict"):
            self._load(tmp_path, "idle_threshold_s = 600\n", base)

    def test_a_corrupt_overrides_file_does_not_break_boot(self, tmp_path):
        config = self._load(tmp_path, "this is = = not toml\n")

        assert config.collector.max_idle_time == 200

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
        load_config(write_config(tmp_path, REQUIRED_SECTIONS))

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

[quectel_cm]
binary = "/usr/local/bin/quectel-CM"
""")

    with caplog.at_level(logging.WARNING):
        load_config(config_path)

    assert caplog.text == ""

class TestMissionProfiles:
    DRONE_PROFILE = """
[profiles.drone]
movement_gate_enabled = false
idle_threshold_s = 0
"""

    def drone_config(self, tmp_path, extra=""):
        content = REQUIRED_SECTIONS + '\n[device]\nmission_type = "drone"\n' + self.DRONE_PROFILE + extra
        return write_config(tmp_path, content)

    def test_profile_of_the_configured_mission_type_is_applied(self, tmp_path):
        config = load_config(self.drone_config(tmp_path))

        assert config.movement_gate.enabled is False
        assert config.collector.max_idle_time == 0

    def test_profile_of_another_mission_type_is_not_applied(self, tmp_path):
        content = REQUIRED_SECTIONS + self.DRONE_PROFILE
        config = load_config(write_config(tmp_path, content))

        assert config.movement_gate.enabled is True
        assert config.collector.max_idle_time == 200

    def test_empty_ground_profile_changes_nothing(self, tmp_path):
        config = load_config(write_config(tmp_path, REQUIRED_SECTIONS + "\n[profiles.ground]\n"))

        assert config.movement_gate.enabled is True

    def test_no_profiles_section_is_fine(self, tmp_path):
        config = load_config(write_config(tmp_path, REQUIRED_SECTIONS + '\n[device]\nmission_type = "drone"\n'))

        assert config.movement_gate.enabled is True

    def test_profile_wins_over_config_toml(self, tmp_path):
        extra = "\n[movement_gate]\nenabled = true\n[collector]\nmax_idle_time = 500\n"
        config = load_config(self.drone_config(tmp_path, extra))

        assert config.movement_gate.enabled is False
        assert config.collector.max_idle_time == 0

    def test_config_local_toml_wins_over_profile(self, tmp_path):
        config_path = self.drone_config(tmp_path)
        (tmp_path / "config.local.toml").write_text('[device]\ndevice_id = "uav-1"\n')

        config = load_config(config_path)

        assert config.device.device_id == "uav-1"
        assert config.movement_gate.enabled is False

    def test_overrides_win_over_profile(self, tmp_path):
        config_path = self.drone_config(tmp_path)
        (tmp_path / "config_overrides.toml").write_text("movement_gate_enabled = true\nidle_threshold_s = 90\n")

        config = load_config(config_path)

        assert config.movement_gate.enabled is True
        assert config.collector.max_idle_time == 90

    def test_mission_type_set_by_override_selects_that_profile(self, tmp_path):
        content = REQUIRED_SECTIONS + self.DRONE_PROFILE
        config_path = write_config(tmp_path, content)
        (tmp_path / "config_overrides.toml").write_text('device_mission_type = "drone"\n')

        config = load_config(config_path)

        assert config.device.mission_type == "drone"
        assert config.movement_gate.enabled is False

    def test_mission_type_override_back_to_ground_drops_the_drone_profile(self, tmp_path):
        config_path = self.drone_config(tmp_path)
        (tmp_path / "config_overrides.toml").write_text('device_mission_type = "ground"\n')

        config = load_config(config_path)

        assert config.movement_gate.enabled is True
        assert config.collector.max_idle_time == 200

    def test_unknown_profile_field_raises(self, tmp_path):
        content = REQUIRED_SECTIONS + "\n[profiles.drone]\nmovement_gate_enable = false\n"

        with pytest.raises(ValueError, match="movement_gate_enable"):
            load_config(write_config(tmp_path, content))

    def test_invalid_profile_value_raises(self, tmp_path):
        content = REQUIRED_SECTIONS + '\n[profiles.drone]\nmovement_gate_enabled = "no"\n'

        with pytest.raises(ValueError, match="movement_gate_enabled"):
            load_config(write_config(tmp_path, content))

    def test_out_of_range_profile_value_raises(self, tmp_path):
        content = REQUIRED_SECTIONS + "\n[profiles.drone]\nidle_threshold_s = -1\n"

        with pytest.raises(ValueError, match="idle_threshold_s"):
            load_config(write_config(tmp_path, content))

    def test_invalid_profile_raises_even_when_it_is_not_the_active_one(self, tmp_path):
        content = REQUIRED_SECTIONS + "\n[profiles.drone]\nbogus = 1\n"

        with pytest.raises(ValueError, match="bogus"):
            load_config(write_config(tmp_path, content))

    @pytest.mark.parametrize("field, value", [
        ("backend_url", '"https://x"'),
        ("backend_device_key", '"k"'),
        ("heartbeat_hmac_secret", '"s"'),
        ("heartbeat_device_id", '"d"'),
        ("device_device_id", '"d"'),
        ("quectel_cm_binary", '"/bin/x"'),
    ])
    def test_profile_cannot_set_config_local_only_fields(self, tmp_path, field, value):
        content = REQUIRED_SECTIONS + f"\n[profiles.drone]\n{field} = {value}\n"

        with pytest.raises(ValueError, match=field):
            load_config(write_config(tmp_path, content))

    def test_profile_cannot_set_mission_type(self, tmp_path):
        content = REQUIRED_SECTIONS + '\n[profiles.drone]\ndevice_mission_type = "ground"\n'

        with pytest.raises(ValueError, match="device_mission_type"):
            load_config(write_config(tmp_path, content))

    def test_unknown_profile_name_raises(self, tmp_path):
        content = REQUIRED_SECTIONS + "\n[profiles.boat]\nmovement_gate_enabled = false\n"

        with pytest.raises(ValueError, match="boat"):
            load_config(write_config(tmp_path, content))

    def test_profile_that_breaks_a_config_check_raises(self, tmp_path):
        content = REQUIRED_SECTIONS + "\n[profiles.ground]\nsystem_mode = \"waiting\"\nmovement_gate_home_latitude = 1.0\n"

        with pytest.raises(ValueError, match="home_latitude"):
            load_config(write_config(tmp_path, content))

    def test_logs_only_the_profile_name(self, tmp_path, caplog):
        with caplog.at_level(logging.INFO):
            load_config(self.drone_config(tmp_path))

        messages = [r.getMessage() for r in caplog.records if "profile" in r.getMessage()]
        assert any("drone" in m for m in messages)
        assert not any("movement_gate_enabled" in m or "False" in m for m in messages)


def test_shipped_config_toml_defines_an_empty_ground_and_the_drone_profile():
    import tomllib
    from measurement_software.core.config_sources import parse_profiles

    with open(Path(__file__).parents[2] / "config.toml", "rb") as f:
        profiles = parse_profiles(tomllib.load(f)["profiles"])

    assert profiles["ground"].movement_gate_enabled is None
    assert profiles["drone"].movement_gate_enabled is False
    assert profiles["drone"].idle_threshold_s == 0
