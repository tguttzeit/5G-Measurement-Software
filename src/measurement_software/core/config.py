import tomllib
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ModemConfig:
    """Serial connection and query-mode settings for the cellular modem."""

    type: str
    port: str
    baud_rate: int
    timeout: float
    mode: str = "serving_cell"

@dataclass
class GnssConfig:
    """Serial connection settings for the GNSS receiver."""

    type: str
    port: str
    baud_rate: int
    timeout: float

@dataclass
class CollectorConfig:
    """Thresholds controlling when a measurement is captured and when a session ends."""

    position_threshold: float = 15
    max_idle_time: float = 200
    max_wait_for_first_fix: float = 300
    wait_log_interval: float = 5
    keep_alive_host: str = "8.8.8.8"
    keep_alive_port: int = 53
    keep_alive_interval_s: float = 4.0
    gps_enabled: bool = True
    gps_disabled_poll_interval_s: float = 5.0

@dataclass
class UploaderConfig:
    """Local storage and scp destination settings for uploading saved measurements."""

    upload_dir: str
    upload_user: str
    upload_host: str
    upload_port: int = 2000
    remote_dir: str = "/add/your/remote/dir/here"
    debug_upload: bool = True

@dataclass
class LoggingConfig:
    """Log level and rotating-file settings."""

    level: str = "INFO"
    log_file: str = "/add/your/log/file/here"
    max_bytes: int = 5_000_000
    backup_count: int = 3

@dataclass
class SystemConfig:
    """Raspberry Pi hardware settings: GPIO shutdown signalling and network interfaces to report."""

    running_on_pi: bool = True
    shutdown_gpio: int = 17
    network_interfaces: tuple[str, ...] = ("wlan0", "wwan0")

@dataclass
class AppConfig:
    """Top-level application configuration, assembled from config.toml."""

    modem: ModemConfig
    gnss_receiver: GnssConfig
    collector: CollectorConfig
    uploader: UploaderConfig
    logging: LoggingConfig
    system: SystemConfig

def load_config(path: Path) -> AppConfig:
    """Reads config.toml and builds an AppConfig, applying defaults for omitted optional sections."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return AppConfig(
        modem=ModemConfig(**raw["modem"]),
        gnss_receiver=GnssConfig(**raw["gnss_receiver"]),
        collector=CollectorConfig(**raw.get("collector", {})),
        uploader=UploaderConfig(**raw["uploader"]),
        logging=LoggingConfig(**raw.get("logging", {})),
        system=SystemConfig(**raw.get("system", {})),
    )