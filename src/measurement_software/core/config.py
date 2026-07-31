import socket
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
    retries: int = 3
    retry_delay_s: float = 0.75
    sim_pin_env_var: str = "MODEM_SIM_PIN"
    sim_unlock_poll_attempts: int = 10
    sim_unlock_poll_interval: float = 1.0

@dataclass
class GnssConfig:
    """Serial connection settings for the GNSS receiver."""

    type: str
    port: str
    baud_rate: int
    timeout: float
    retries: int = 3
    retry_delay_s: float = 0.75

@dataclass
class DeviceConfig:
    """Identifies this device/vehicle and mission type, carried on every captured datapoint.

    `device_id` defaults to the Pi's hostname, which is already distinct per device in a fleet,
    so a single-device setup needs no configuration at all.
    """

    device_id: str = field(default_factory=socket.gethostname)
    mission_type: str = "ground"

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
class QualityThresholds:
    """Cutoffs a cell measurement has to clear on every metric to count as good.

    The defaults are commonly-cited reference values of the kind drive-test tools use as
    their default tiers. They have not been validated against this project's own
    measurements, and are meant to be retuned per RAT once field data exists.
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

    The URL has to be https. The device initiates every contact and the backend answers,
    which makes the response a path for instructions to reach the device — so the transport
    has to be one where the device can trust who it is talking to.
    """

    enabled: bool = False
    url: str = ""
    interval_s: float = 60.0
    timeout_s: float = 10.0

@dataclass
class StorageConfig:
    """Free-disk-space tripwire threshold for the upload backlog directory.

    Not a retained-file cap or eviction policy - the backlog is expected to stay small (see
    decision record 0009) - just a loud early warning in case that expectation turns out wrong.
    """

    low_free_space_warning_bytes: int = 500_000_000

@dataclass
class FanConfig:
    """Case fan on/off control via GPIO, driven by CPU temperature with hysteresis.

    Disabled by default. Two distinct thresholds avoid rapidly toggling the fan right at a
    single boundary as temperature hovers around it.
    """

    enabled: bool = False
    gpio_pin: int = 27
    temp_on_celsius: float = 70.0
    temp_off_celsius: float = 60.0
    poll_interval_s: float = 5.0

@dataclass
class DisplayConfig:
    """SSD1306 OLED status-display settings, for field diagnostics with no terminal attached.

    Optional and disabled by default: an absent [display] section, or enabled=false, uses a
    no-op display so the app runs unchanged without a screen attached.
    """

    enabled: bool = False
    type: str = "ssd1306"
    i2c_port: int = 1
    i2c_address: int = 0x3C
    refresh_interval_s: float = 2.0

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
    device: DeviceConfig
    collector: CollectorConfig
    run_status: RunStatusConfig
    heartbeat: HeartbeatConfig
    storage: StorageConfig
    fan: FanConfig
    display: DisplayConfig
    uploader: UploaderConfig
    logging: LoggingConfig
    system: SystemConfig

def load_config(path: Path) -> AppConfig:
    """Reads config.toml and builds an AppConfig, applying defaults for omitted optional sections."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    config = AppConfig(
        modem=ModemConfig(**raw["modem"]),
        gnss_receiver=GnssConfig(**raw["gnss_receiver"]),
        device=DeviceConfig(**raw.get("device", {})),
        collector=CollectorConfig(**raw.get("collector", {})),
        run_status=_build_run_status_config(raw.get("run_status", {})),
        heartbeat=HeartbeatConfig(**raw.get("heartbeat", {})),
        storage=StorageConfig(**raw.get("storage", {})),
        fan=FanConfig(**raw.get("fan", {})),
        display=DisplayConfig(**raw.get("display", {})),
        uploader=UploaderConfig(**raw["uploader"]),
        logging=LoggingConfig(**raw.get("logging", {})),
        system=SystemConfig(**raw.get("system", {})),
    )
    _validate_gpio_pins(config)
    return config

def _build_run_status_config(raw: dict) -> RunStatusConfig:
    """Builds a RunStatusConfig, expanding its per-RAT sub-tables into QualityThresholds."""
    per_rat_thresholds = {rat: QualityThresholds(**raw.get(rat, {})) for rat in ("lte", "nr")}
    return RunStatusConfig(**(raw | per_rat_thresholds))

_ONBOARD_UART_PORTS = frozenset({"/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0"})

def _validate_gpio_pins(config: AppConfig) -> None:
    """Raises ValueError if two GPIO consumers are configured to use the same pin."""
    claims: dict[int, str] = {}
    _claim_gpio_pin(claims, config.system.shutdown_gpio, "system.shutdown_gpio")
    if config.fan.enabled:
        _claim_gpio_pin(claims, config.fan.gpio_pin, "fan.gpio_pin")
    if config.display.enabled:
        _claim_gpio_pin(claims, 2, "display (I2C SDA1, fixed)")
        _claim_gpio_pin(claims, 3, "display (I2C SCL1, fixed)")

def _claim_gpio_pin(claims: dict[int, str], pin: int, owner: str) -> None:
    if pin in claims:
        raise ValueError(f"GPIO pin conflict: {owner} and {claims[pin]} both use pin {pin}")
    claims[pin] = owner
