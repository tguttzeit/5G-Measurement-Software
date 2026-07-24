import tomllib
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class ModemConfig:
    type: str
    port: str
    baud_rate: int
    timeout: float

@dataclass
class GnssConfig:
    type: str
    port: str
    baud_rate: int
    timeout: float

@dataclass
class CollectorConfig:
    position_threshold: float = 15
    max_idle_time: float = 200
    max_wait_for_first_fix: float = 300
    wait_log_interval: float = 5
    keep_alive_host: str = "8.8.8.8"
    keep_alive_port: int = 53
    keep_alive_interval_s: float = 4.0

@dataclass
class UploaderConfig:
    upload_dir: str
    upload_user: str
    upload_host: str
    upload_port: int = 2000
    remote_dir: str = "/add/your/remote/dir/here"
    debug_upload: bool = True

@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: str = "/add/your/log/file/here"
    max_bytes: int = 5_000_000
    backup_count: int = 3

@dataclass
class SystemConfig:
    running_on_pi: bool = True
    shutdown_gpio: int = 17
    network_interfaces: tuple[str, ...] = ("wlan0", "wwan0")

@dataclass
class AppConfig:
    modem: ModemConfig
    gnss_receiver: GnssConfig
    collector: CollectorConfig
    uploader: UploaderConfig
    logging: LoggingConfig
    system: SystemConfig

def load_config(path: Path) -> AppConfig:
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