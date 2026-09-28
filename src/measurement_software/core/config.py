import logging
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

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
class MovementGateConfig:
    """Pre-flight wait for confirmed vehicle movement, before Collector.collect() starts.

    Runs GNSS-only, at a low duty cycle - MovementGate itself never touches the modem or
    quectel-CM either way. In Auto Mode (decision record 0021, revising 0005) both are already
    open for the heartbeat by the time this wait starts, trading away the battery savings of
    keeping them closed for always-on visibility. home_latitude/home_longitude are unset by
    default, which falls back to pure movement-distance detection; set both to additionally
    require departure from a known garage/home position.
    """

    poll_interval_s: float = 120.0
    movement_threshold: float = 15.0
    confirmations_required: int = 2
    home_latitude: float | None = None
    home_longitude: float | None = None
    home_departure_threshold: float = 100.0

@dataclass
class QuectelCmConfig:
    """quectel-CM connection-manager daemon, started by this app itself, not by systemd at boot.

    Both Auto Mode and Waiting Mode start it themselves right at boot, before anything else
    happens, so the heartbeat has a cellular link the moment the app comes up (decision records
    0020 and 0021) - only the original battery-efficient design (decision record 0005) deferred
    this until movement was confirmed.
    """

    binary: str = "/add/your/quectel-CM/path/here"
    args: tuple[str, ...] = ()

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
class BackendConfig:
    """Shared HTTPS destination and auth for everything this device sends to the backend server.

    `url` has to be https. The device initiates every contact and the backend answers, which
    makes the response a path for instructions to reach the device — so the transport has to be
    one where the device can trust who it is talking to. `HeartbeatConfig` and `UploaderConfig`
    each append their own path onto `url` rather than configuring a host of their own.
    """

    url: str = ""
    device_key: str = ""

@dataclass
class HeartbeatConfig:
    """Backend heartbeat path and cadence. Disabled unless a section says otherwise.

    `hmac_secret` authenticates the *response* to each heartbeat POST - distinct from
    `backend.device_key`, which authenticates the device's own outgoing requests. It verifies
    the signature the backend computes over config overrides / one-shot commands riding on that
    response, so a device that never expects remote commands can simply leave it unset.
    """

    enabled: bool = False
    path: str = "/heartbeat"
    interval_s: float = 60.0
    timeout_s: float = 10.0
    device_id: str = ""
    hmac_secret: str = ""

@dataclass
class SelftestConfig:
    """Backend destination for `python -m measurement_software.selftest` check results.

    Reuses `backend.device_key` for authentication and `heartbeat.device_id` for device
    identification. Empty by default: an
    unconfigured device runs selftest checks locally only, with no backend submission.
    """

    results_url: str = ""

@dataclass
class GpsFixTestConfig:
    """Live GPS-fix-finding diagnostic run (run-gps-fix-test-now/stop-gps-fix-test-now).

    Only meaningful in Waiting Mode (see decision record 0020) - opens its own `GNSSReceiver`
    and posts a small status payload (num_satellites, has_fix, elapsed_s) to `status_url` roughly
    every `report_interval_s`, reusing `backend.device_key`/`heartbeat.device_id` for auth the
    same way `selftest.results_url` already does. Empty `status_url` by default: an unconfigured
    device still runs the diagnostic locally (logged only), matching that same precedent.
    `timeout_s` auto-stops the run if `stop-gps-fix-test-now` never arrives, so a forgotten run
    doesn't keep the GNSS connection and status POSTs going indefinitely.
    """

    status_url: str = ""
    report_interval_s: float = 2.0
    timeout_s: float = 300.0

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
class LatencyTestConfig:
    """flent-based latency measurements against a test server, on two independent cadences.

    Disabled by default, and inert without a host: the tests need a server-side counterpart
    (netperf/irtt, depending on the test) that is outside this repo's control, so an
    unconfigured device must never start generating test traffic on its own.

    The two cadences are separate because the tests cost very different amounts: a baseline
    ping is nearly free, while a load test deliberately saturates the link for its whole
    length and spends real data and battery.

    `movement_window_s` turns the collection loop's discrete movement reports - one per cleared
    position threshold - into an answer about whether the vehicle is moving right now: it counts
    as moving as long as it cleared that threshold within this many seconds.
    """

    enabled: bool = False
    host: str = ""
    flent_binary: str = "flent"
    baseline_test: str = "ping"
    baseline_interval_s: float = 60.0
    baseline_length_s: int = 10
    load_test: str = "rrul"
    load_interval_s: float = 900.0
    load_length_s: int = 60
    poll_interval_s: float = 30.0
    movement_window_s: float = 30.0
    timeout_s: float = 300.0

@dataclass
class UploaderConfig:
    """Local storage and HTTPS upload paths for uploading saved measurements.

    The upload host is not configured here - see `BackendConfig`. `path` and `latency_path` are
    separate endpoints because they carry incompatible schemas - a GPS/cell measurement datapoint
    (timestamp/fix/cell_sample) is not shaped like a LatencyResult (start_fix/end_fix/rtt figures)
    - see `Uploader`. For selftest SCP uploads, see upload_user/upload_host/upload_port/remote_dir.
    """

    upload_dir: str
    path: str = "/upload"
    latency_path: str = "/upload/latency"
    debug_upload: bool = True
    timeout_s: float = 10.0
    # SCP settings for selftest uploader check only
    upload_user: str = ""
    upload_host: str = ""
    upload_port: int = 22
    remote_dir: str = ""

@dataclass
class LoggingConfig:
    """Log level and rotating-file settings."""

    level: str = "INFO"
    log_file: str = "/add/your/log/file/here"
    max_bytes: int = 5_000_000
    backup_count: int = 3

@dataclass
class SystemConfig:
    """Raspberry Pi hardware settings: GPIO shutdown signalling and network interfaces to report.

    `mode` selects the boot behavior: "auto" (default) waits battery-efficiently for confirmed
    vehicle movement before opening the modem/quectel-CM, unchanged from before this field
    existed. "waiting" opens the cellular link and starts heartbeating immediately at boot, then
    does nothing further until an explicit remote command says what to do - see decision record
    0020. Only takes effect on the next boot: switched remotely via the mode-waiting-now/
    mode-auto-now one-shot commands, which persist to `mode_override.toml` rather than mutating
    this field mid-run.
    """

    running_on_pi: bool = True
    shutdown_gpio: int = 17
    network_interfaces: tuple[str, ...] = ("wlan0", "wwan0")
    mode: str = "auto"

@dataclass
class AppConfig:
    """Top-level application configuration, assembled from config.toml."""

    modem: ModemConfig
    gnss_receiver: GnssConfig
    device: DeviceConfig
    collector: CollectorConfig
    movement_gate: MovementGateConfig
    quectel_cm: QuectelCmConfig
    run_status: RunStatusConfig
    backend: BackendConfig
    heartbeat: HeartbeatConfig
    selftest: SelftestConfig
    gps_fix_test: GpsFixTestConfig
    storage: StorageConfig
    fan: FanConfig
    display: DisplayConfig
    latency_test: LatencyTestConfig
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
        movement_gate=MovementGateConfig(**raw.get("movement_gate", {})),
        quectel_cm=_build_quectel_cm_config(raw.get("quectel_cm", {})),
        run_status=_build_run_status_config(raw.get("run_status", {})),
        backend=BackendConfig(**raw.get("backend", {})),
        heartbeat=HeartbeatConfig(**raw.get("heartbeat", {})),
        selftest=SelftestConfig(**raw.get("selftest", {})),
        gps_fix_test=GpsFixTestConfig(**raw.get("gps_fix_test", {})),
        storage=StorageConfig(**raw.get("storage", {})),
        fan=FanConfig(**raw.get("fan", {})),
        display=DisplayConfig(**raw.get("display", {})),
        latency_test=LatencyTestConfig(**raw.get("latency_test", {})),
        uploader=UploaderConfig(**raw["uploader"]),
        logging=LoggingConfig(**raw.get("logging", {})),
        system=SystemConfig(**raw.get("system", {})),
    )
    _validate_gpio_pins(config)
    _validate_movement_gate_home_position(config.movement_gate)
    _apply_local_config_file(config, path)
    _apply_config_overrides_file(config, path)
    _apply_mode_override_file(config, path)
    _validate_system_mode(config)
    _warn_on_unset_identity_fields(config)
    return config

_PLACEHOLDER_BACKEND_URL = "https://add.your.backend.here"
_PLACEHOLDER_QUECTEL_CM_BINARY = "/add/your/quectel-CM/path/here"

def _warn_on_unset_identity_fields(config: AppConfig) -> None:
    """Logs a warning for each per-device identity/secret field still left unset or at its
    config.toml placeholder - the fields config.local.toml exists to hold (see issue #25).

    Warns rather than raising: unlike the GPIO/home-position ValueErrors above, an unset field
    here degrades gracefully at runtime (failed uploads, 401s, remote commands disabled) rather
    than crashing, so failing startup over it would be a stricter behavior change than the field
    itself already causes. Only runs for `system.running_on_pi` - a local dev checkout with it
    false is expected to be unconfigured. `heartbeat.hmac_secret` is gated on `heartbeat.enabled`
    since it's the one field here with no other consumer; `backend.url`/`device_key`,
    `heartbeat.device_id`, and `quectel_cm.binary` are all used unconditionally (uploads and
    quectel-CM run on every real deployment, heartbeat or not), so those are always checked.
    """
    if not config.system.running_on_pi:
        return
    if config.backend.url in ("", _PLACEHOLDER_BACKEND_URL):
        logger.warning(
            "backend.url is unset or still a placeholder - set it in config.local.toml."
        )
    if not config.backend.device_key:
        logger.warning("backend.device_key is unset - set it in config.local.toml.")
    if not config.heartbeat.device_id:
        logger.warning("heartbeat.device_id is unset - set it in config.local.toml.")
    if config.heartbeat.enabled and not config.heartbeat.hmac_secret:
        logger.warning(
            "heartbeat.hmac_secret is unset while heartbeat.enabled - remote commands/overrides "
            "will be discarded. Set it in config.local.toml."
        )
    if config.quectel_cm.binary in ("", _PLACEHOLDER_QUECTEL_CM_BINARY):
        logger.warning(
            "quectel_cm.binary is unset or still a placeholder - set it in config.local.toml."
        )

def _apply_local_config_file(config: AppConfig, config_path: Path) -> None:
    """Merges a sibling config.local.toml onto the just-built config, if one exists.

    Applied before config_overrides.toml, per decision record 0007: config.toml defaults first,
    then config.local.toml (per-device identity/secrets set by hand at setup time), then
    config_overrides.toml (remote runtime tuning) merged last. Deferred import: core.config_sources
    itself needs AppConfig from this module, so importing it at module level here would be
    circular.
    """
    from measurement_software.core.config_sources import LOCAL_CONFIG_FILENAME, apply_local_config, load_local_config
    local_path = config_path.parent / LOCAL_CONFIG_FILENAME
    apply_local_config(config, load_local_config(local_path))

def _apply_config_overrides_file(config: AppConfig, config_path: Path) -> None:
    """Merges a sibling config_overrides.toml onto the just-built config, if one exists.

    Deferred import: core.config_sources itself needs AppConfig from this module, so importing
    it at module level here would be circular.
    """
    from measurement_software.core.config_sources import OVERRIDES_FILENAME, apply_overrides, load_overrides
    overrides_path = config_path.parent / OVERRIDES_FILENAME
    apply_overrides(config, load_overrides(overrides_path))

def _apply_mode_override_file(config: AppConfig, config_path: Path) -> None:
    """Applies a sibling mode_override.toml's device mode onto the just-built config, if one exists.

    Not part of config_overrides.toml's allow-listed schema (that's fixed to the backend's numeric
    ConfigOverrideCreate schema) - the device mode is a small persisted flag of its own, written by
    the mode-waiting-now/mode-auto-now one-shot commands. Deferred import for the same reason as
    _apply_config_overrides_file above.
    """
    from measurement_software.core.config_sources import MODE_OVERRIDE_FILENAME, read_mode_override
    override_path = config_path.parent / MODE_OVERRIDE_FILENAME
    mode = read_mode_override(override_path)
    if mode is not None:
        config.system.mode = mode

def _build_quectel_cm_config(raw: dict) -> QuectelCmConfig:
    """Builds a QuectelCmConfig, normalizing TOML's `args` array into a tuple."""
    if "args" in raw:
        raw = raw | {"args": tuple(raw["args"])}
    return QuectelCmConfig(**raw)

def _build_run_status_config(raw: dict) -> RunStatusConfig:
    """Builds a RunStatusConfig, expanding its per-RAT sub-tables into QualityThresholds."""
    per_rat_thresholds = {rat: QualityThresholds(**raw.get(rat, {})) for rat in ("lte", "nr")}
    return RunStatusConfig(**(raw | per_rat_thresholds))

def _validate_movement_gate_home_position(config: MovementGateConfig) -> None:
    """Raises ValueError if only one of home_latitude/home_longitude is set."""
    has_lat = config.home_latitude is not None
    has_lon = config.home_longitude is not None
    if has_lat != has_lon:
        raise ValueError(
            "movement_gate.home_latitude and home_longitude must both be set, or both left unset."
        )

_VALID_SYSTEM_MODES = ("auto", "waiting")

def _validate_system_mode(config: AppConfig) -> None:
    """Raises ValueError if system.mode (from config.toml or a mode_override.toml) is unrecognized."""
    if config.system.mode not in _VALID_SYSTEM_MODES:
        raise ValueError(
            f"system.mode must be one of {_VALID_SYSTEM_MODES}, got {config.system.mode!r}."
        )

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
    if config.gnss_receiver.port in _ONBOARD_UART_PORTS:
        _claim_gpio_pin(claims, 14, "gnss_receiver (UART TXD, fixed)")
        _claim_gpio_pin(claims, 15, "gnss_receiver (UART RXD, fixed)")

def _claim_gpio_pin(claims: dict[int, str], pin: int, owner: str) -> None:
    if pin in claims:
        raise ValueError(f"GPIO pin conflict: {owner} and {claims[pin]} both use pin {pin}")
    claims[pin] = owner
