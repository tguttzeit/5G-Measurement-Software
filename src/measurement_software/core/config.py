import tomllib
from dataclasses import dataclass, field
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

@dataclass
class QualityThresholds:
    """Cutoffs a cell measurement has to clear on every metric to count as good.

    Starting values are the reference cutoffs from decision record 0002, expected to be
    retuned per RAT once real field data exists.
    """

    min_rsrp: float = -100.0
    min_rsrq: float = -11.0
    min_sinr: float = 0.0

@dataclass
class RunStatusConfig:
    """Per-RAT quality thresholds and how many empty captures mean the pipeline is broken."""

    lte: QualityThresholds = field(default_factory=QualityThresholds)
    nr: QualityThresholds = field(default_factory=QualityThresholds)
    empty_captures_until_pipeline_broken: int = 3

@dataclass
class HeartbeatConfig:
    """Backend heartbeat destination and cadence. Disabled unless a section says otherwise.

    The URL has to be https: the backend's responses to this channel will carry remote
    config overrides and commands (issue #8), so it needs to be authenticated transport.
    """

    enabled: bool = False
    url: str = ""
    interval_s: float = 60.0
    timeout_s: float = 10.0

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
    run_status: RunStatusConfig
    heartbeat: HeartbeatConfig
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
        run_status=_build_run_status_config(raw.get("run_status", {})),
        heartbeat=HeartbeatConfig(**raw.get("heartbeat", {})),
        uploader=UploaderConfig(**raw["uploader"]),
        logging=LoggingConfig(**raw.get("logging", {})),
        system=SystemConfig(**raw.get("system", {})),
    )

def _build_run_status_config(raw: dict) -> RunStatusConfig:
    """Builds a RunStatusConfig, expanding its per-RAT sub-tables into QualityThresholds."""
    per_rat_thresholds = {rat: QualityThresholds(**raw.get(rat, {})) for rat in ("lte", "nr")}
    return RunStatusConfig(**(raw | per_rat_thresholds))