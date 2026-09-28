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
    assert config.movement_gate.poll_interval_s == 120.0
    assert config.movement_gate.movement_threshold == 15.0
    assert config.movement_gate.confirmations_required == 2
    assert config.movement_gate.home_latitude is None
    assert config.movement_gate.home_longitude is None
    assert config.movement_gate.home_departure_threshold == 100.0
    assert config.quectel_cm.binary == "/add/your/quectel-CM/path/here"
    assert config.quectel_cm.args == ()
    assert config.run_status.lte.min_rsrp == -100.0
    assert config.run_status.nr.min_sinr == 0.0
    assert config.run_status.empty_captures_until_pipeline_broken == 3
    assert config.heartbeat.enabled is False
    assert config.heartbeat.hmac_secret == ""
    assert config.selftest.results_url == ""
    assert config.gps_fix_test.status_url == ""
    assert config.gps_fix_test.report_interval_s == 2.0
    assert config.gps_fix_test.timeout_s == 300.0
    assert config.storage.low_free_space_warning_bytes == 500_000_000
    assert config.fan.enabled is False
    assert config.fan.gpio_pin == 27
    assert config.fan.temp_on_celsius == 70.0
    assert config.fan.temp_off_celsius == 60.0
    assert config.fan.poll_interval_s == 5.0
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


def test_load_config_reads_fan_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[fan]
enabled = true
gpio_pin = 22
temp_on_celsius = 75.0
temp_off_celsius = 65.0
poll_interval_s = 2.0
"""
    config = load_config(write_config(tmp_path, content))

    assert config.fan.enabled is True
    assert config.fan.gpio_pin == 22
    assert config.fan.temp_on_celsius == 75.0
    assert config.fan.temp_off_celsius == 65.0
    assert config.fan.poll_interval_s == 2.0


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


def test_load_config_reads_per_rat_quality_thresholds(tmp_path):
    content = REQUIRED_SECTIONS + """
[run_status]
empty_captures_until_pipeline_broken = 5

[run_status.nr]
min_rsrp = -95.0
min_sinr = 3.0
"""
    config = load_config(write_config(tmp_path, content))

    assert config.run_status.empty_captures_until_pipeline_broken == 5
    assert config.run_status.nr.min_rsrp == -95.0
    assert config.run_status.nr.min_sinr == 3.0
    # Thresholds not given for a RAT fall back to the defaults, per RAT independently.
    assert config.run_status.nr.min_rsrq == -11.0
    assert config.run_status.lte.min_rsrp == -100.0


def test_load_config_raises_on_shutdown_and_fan_gpio_conflict(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
shutdown_gpio = 22

[fan]
enabled = true
gpio_pin = 22
"""
    with pytest.raises(ValueError, match="GPIO pin conflict"):
        load_config(write_config(tmp_path, content))


def test_load_config_does_not_raise_on_fan_gpio_conflict_when_fan_disabled(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
shutdown_gpio = 22

[fan]
enabled = false
gpio_pin = 22
"""
    config = load_config(write_config(tmp_path, content))

    assert config.system.shutdown_gpio == 22
    assert config.fan.gpio_pin == 22


def test_load_config_raises_on_shutdown_and_display_i2c_pin_conflict(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
shutdown_gpio = 2

[display]
enabled = true
"""
    with pytest.raises(ValueError, match="GPIO pin conflict"):
        load_config(write_config(tmp_path, content))


def test_load_config_raises_on_fan_and_display_i2c_pin_conflict(tmp_path):
    content = REQUIRED_SECTIONS + """
[fan]
enabled = true
gpio_pin = 3

[display]
enabled = true
"""
    with pytest.raises(ValueError, match="GPIO pin conflict"):
        load_config(write_config(tmp_path, content))


def test_load_config_does_not_raise_on_display_i2c_pin_conflict_when_display_disabled(tmp_path):
    content = REQUIRED_SECTIONS + """
[fan]
enabled = true
gpio_pin = 2

[display]
enabled = false
"""
    config = load_config(write_config(tmp_path, content))

    assert config.fan.gpio_pin == 2
    assert config.display.enabled is False


def test_load_config_allows_non_conflicting_fan_and_display_pins(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
shutdown_gpio = 17

[fan]
enabled = true
gpio_pin = 27

[display]
enabled = true
"""
    config = load_config(write_config(tmp_path, content))

    assert config.system.shutdown_gpio == 17
    assert config.fan.gpio_pin == 27
    assert config.display.enabled is True


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
    (tmp_path / "config_overrides.toml").write_text('rsrp_threshold = -90.0\nidle_threshold_s = 600\n')

    config = load_config(config_path)

    assert config.run_status.lte.min_rsrp == -90.0
    assert config.run_status.nr.min_rsrp == -90.0
    assert config.collector.max_idle_time == 600


def test_load_config_without_overrides_file_uses_defaults(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.run_status.lte.min_rsrp == -100.0
    assert config.collector.max_idle_time == 200


def test_load_config_raises_on_shutdown_and_gnss_onboard_uart_pin_conflict(tmp_path):
    content = required_sections_with_gnss_port("/dev/serial0") + """
[system]
shutdown_gpio = 14
"""
    with pytest.raises(ValueError, match="GPIO pin conflict"):
        load_config(write_config(tmp_path, content))


def test_load_config_raises_on_fan_and_gnss_onboard_uart_pin_conflict(tmp_path):
    content = required_sections_with_gnss_port("/dev/ttyAMA0") + """
[fan]
enabled = true
gpio_pin = 15
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


def test_load_config_raises_on_unrecognized_system_mode(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
mode = "bogus"
"""
    with pytest.raises(ValueError, match="system.mode"):
        load_config(write_config(tmp_path, content))


def test_load_config_applies_a_sibling_mode_override_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "mode_override.toml").write_text('mode = "waiting"\n')

    config = load_config(config_path)

    assert config.system.mode == "waiting"


def test_load_config_mode_override_file_takes_precedence_over_config_toml(tmp_path):
    content = REQUIRED_SECTIONS + """
[system]
mode = "waiting"
"""
    config_path = write_config(tmp_path, content)
    (tmp_path / "mode_override.toml").write_text('mode = "auto"\n')

    config = load_config(config_path)

    assert config.system.mode == "auto"


def test_load_config_without_mode_override_file_uses_config_toml_value(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.system.mode == "auto"


def test_load_config_raises_on_unrecognized_mode_from_override_file(tmp_path):
    config_path = write_config(tmp_path, REQUIRED_SECTIONS)
    (tmp_path / "mode_override.toml").write_text('mode = "bogus"\n')

    with pytest.raises(ValueError, match="system.mode"):
        load_config(config_path)


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