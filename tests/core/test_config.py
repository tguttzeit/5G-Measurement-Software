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
    assert config.storage.low_free_space_warning_bytes == 500_000_000
    assert config.logging.level == "INFO"
    assert config.system.running_on_pi is True
    assert config.system.network_interfaces == ("wlan0", "wwan0")


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


def test_load_config_reads_storage_section(tmp_path):
    content = REQUIRED_SECTIONS + """
[storage]
low_free_space_warning_bytes = 100_000_000
"""
    config = load_config(write_config(tmp_path, content))

    assert config.storage.low_free_space_warning_bytes == 100_000_000


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