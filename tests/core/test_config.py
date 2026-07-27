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
    assert config.logging.level == "INFO"
    assert config.system.running_on_pi is True
    assert config.system.network_interfaces == ("wlan0", "wwan0")


def test_load_config_overrides_defaults_when_optional_sections_present(tmp_path):
    content = REQUIRED_SECTIONS + """
[collector]
max_idle_time = 42
position_threshold = 5

[logging]
level = "DEBUG"

[system]
running_on_pi = false
"""
    config = load_config(write_config(tmp_path, content))

    assert config.collector.max_idle_time == 42
    assert config.collector.position_threshold == 5
    assert config.logging.level == "DEBUG"
    assert config.system.running_on_pi is False