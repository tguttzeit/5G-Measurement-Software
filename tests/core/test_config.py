from pathlib import Path

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
upload_user = "pi"
upload_host = "example.org"
"""


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path


def test_load_config_applies_defaults_for_omitted_optional_sections(tmp_path):
    config = load_config(write_config(tmp_path, REQUIRED_SECTIONS))

    assert config.modem.type == "quectel"
    assert config.modem.port == "/dev/ttyUSB2"
    assert config.uploader.upload_host == "example.org"
    assert config.collector.max_idle_time == 200
    assert config.collector.gps_enabled is True
    assert config.collector.gps_disabled_poll_interval_s == 5.0
    assert config.run_status.lte.min_rsrp == -100.0
    assert config.run_status.nr.min_sinr == 0.0
    assert config.run_status.empty_captures_until_pipeline_broken == 3
    assert config.heartbeat.enabled is False
    assert config.display.enabled is False
    assert config.display.type == "ssd1306"
    assert config.display.i2c_port == 1
    assert config.display.i2c_address == 0x3C
    assert config.display.refresh_interval_s == 2.0
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
upload_user = "pi"
upload_host = "example.org"
"""
    config = load_config(write_config(tmp_path, content))

    assert config.modem.retries == 5
    assert config.modem.retry_delay_s == 1.0
    assert config.gnss_receiver.retries == 1
    assert config.gnss_receiver.retry_delay_s == 0.5


def test_load_config_overrides_defaults_when_optional_sections_present(tmp_path):
    content = REQUIRED_SECTIONS + """
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

    assert config.collector.max_idle_time == 42
    assert config.collector.position_threshold == 5
    assert config.collector.gps_enabled is False
    assert config.collector.gps_disabled_poll_interval_s == 2.5
    assert config.logging.level == "DEBUG"
    assert config.system.running_on_pi is False


def test_load_config_reads_heartbeat_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[heartbeat]
enabled = true
url = "https://backend.example.org/heartbeat"
interval_s = 30.0
"""
    config = load_config(write_config(tmp_path, content))

    assert config.heartbeat.enabled is True
    assert config.heartbeat.url == "https://backend.example.org/heartbeat"
    assert config.heartbeat.interval_s == 30.0
    assert config.heartbeat.timeout_s == 10.0


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