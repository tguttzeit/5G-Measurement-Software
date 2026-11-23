import copy
import json
import logging
import socket
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class ModemConfig:
    """Serial connection and query-mode settings for the cellular modem."""

    type: str
    baud_rate: int
    timeout: float
    # None looks the port up by its stable /dev/serial/by-id name on every open.
    port: str | None = None
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
    max_idle_time: float = 1800
    max_wait_for_first_fix: float = 300
    wait_log_interval: float = 5
    keep_alive_host: str = "8.8.8.8"
    keep_alive_port: int = 53
    keep_alive_interval_s: float = 4.0
    gps_enabled: bool = True
    gps_disabled_poll_interval_s: float = 5.0
    capture_gate: str = "distance"
    capture_interval_s: float = 15.0
    modem_reconnect_attempts: int = 10
    modem_reconnect_delay_s: float = 3.0

@dataclass
class MovementGateConfig:
    """Pre-flight wait for confirmed vehicle movement, before Collector.collect() starts.

    Runs GNSS-only, at a low duty cycle - MovementGate itself never touches the modem or
    quectel-CM either way. In Auto Mode both are already open for the heartbeat by the time this
    wait starts, trading away the battery savings of keeping them closed for always-on
    visibility. home_latitude/home_longitude are unset by
    default, which falls back to pure movement-distance detection; set both to additionally
    require departure from a known garage/home position. With enabled = false the wait is
    skipped entirely and measuring starts right away (e.g. for drones, which may hover).
    """

    enabled: bool = True
    poll_interval_s: float = 45.0
    movement_threshold: float = 10.0
    confirmations_required: int = 2
    home_latitude: float | None = None
    home_longitude: float | None = None
    home_departure_threshold: float = 100.0

@dataclass
class QuectelCmConfig:
    """quectel-CM connection-manager daemon, started by this app itself, not by systemd at boot.

    Both Auto Mode and Manual Mode start it themselves right at boot, before anything else
    happens, so the heartbeat has a cellular link the moment the app comes up. Deferring it until
    movement is confirmed would save battery but leave an unattended device invisible while it
    waits.
    """

    binary: str = "/add/your/quectel-CM/path/here"
    args: tuple[str, ...] = ()

@dataclass
class RunStatusConfig:
    """How many consecutive empty captures mean the measurement pipeline is broken."""

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
    """Backend heartbeat path and cadence.

    `hmac_secret` authenticates the *response* to each heartbeat POST - distinct from
    `backend.device_key`, which authenticates the device's own outgoing requests. It verifies
    the signature the backend computes over config overrides / one-shot commands riding on that
    response, so a device that never expects remote commands can simply leave it unset.
    """

    enabled: bool = True
    path: str = "/api/heartbeat"
    interval_s: float = 15.0
    timeout_s: float = 10.0
    success_log_interval_s: float = 600.0
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

    Only meaningful in Manual Mode, the one mode that blocks on remote commands with no run in
    progress - opens its own `GNSSReceiver` and posts a small status payload (num_satellites,
    has_fix, elapsed_s) to `status_url` roughly
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

    Not a retained-file cap or eviction policy - a year of undelivered data would still be a
    small fraction of the card - just a loud early warning in case that expectation turns out
    wrong.
    """

    low_free_space_warning_bytes: int = 500_000_000

@dataclass
class DisplayConfig:
    """SSD1306 OLED status-display settings, for field diagnostics with no terminal attached.

    On by default; with enabled=false a no-op display is used instead, so the app runs unchanged
    on a device without a screen attached.
    """

    enabled: bool = True
    type: str = "ssd1306"
    i2c_port: int = 1
    i2c_address: int = 0x3C
    refresh_interval_s: float = 2.0

@dataclass
class LatencyTestConfig:
    """flent-based latency measurements against a test server, on two independent cadences.

    On by default but inert without a host: the tests need a server-side counterpart
    (netperf for the load test, a host that answers pings for the baseline) that is outside this
    repo's control, so an unconfigured device must never start generating test traffic on its
    own.

    The two cadences are separate because the tests cost very different amounts: a baseline
    ping is nearly free, while a load test deliberately saturates the link for its whole
    length and spends real data and battery.

    `movement_window_s` turns the collection loop's discrete movement reports - one per cleared
    position threshold - into an answer about whether the vehicle is moving right now: it counts
    as moving as long as it cleared that threshold within this many seconds.
    """

    enabled: bool = True
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
    - see `Uploader`.
    """

    upload_dir: str
    path: str = "/api/measurements"
    latency_path: str = "/api/latency-results"
    debug_upload: bool = True
    timeout_s: float = 10.0

@dataclass
class LoggingConfig:
    """Log level and rotating-file settings."""

    level: str = "INFO"
    log_file: str = "/add/your/log/file/here"
    max_bytes: int = 5_000_000
    backup_count: int = 3

@dataclass
class SystemConfig:
    """Raspberry Pi settings: boot mode, crash escalation limits and network interfaces to report.

    `mode` selects the boot behavior. Both modes open the cellular link and start heartbeating
    right at boot; "auto" (default) then transitions into measuring on its own once MovementGate
    confirms movement, while "manual" does nothing further until an explicit remote command says
    what to do. Only takes effect on the next boot: switched remotely via the mode-manual-now/
    mode-auto-now one-shot commands, which persist `system_mode` to `config_overrides.toml`
    rather than mutating this field mid-run.
    """

    running_on_pi: bool = True
    network_interfaces: tuple[str, ...] = ("wlan0", "wwan0")
    mode: str = "auto"
    crash_same_error_limit: int = 3
    crash_reboot_limit: int = 5

@dataclass
class LinkWatchdogConfig:
    """Recovers a cellular link that stays down: restarts quectel-CM after `restart_after_s`
    without an IPv4 address on `interface`, and after `radio_reset_after_restarts` restarts that
    didn't help, also resets the modem's radio. While the link stays down, the wait before each
    further step doubles, up to `max_restart_interval_s`; the next outage starts again from
    `restart_after_s`. Only active with `system.running_on_pi`, since quectel-CM is only started
    then."""

    enabled: bool = True
    interface: str = "wwan0"
    check_interval_s: float = 30.0
    restart_after_s: float = 180.0
    max_restart_interval_s: float = 1800.0
    radio_reset_after_restarts: int = 2

@dataclass
class ClockConfig:
    """Sets the system clock over NTP once the cellular link is up (the Pi has no RTC).

    `ntp_server` is an IPv4 address or hostname queried once per link-up via SNTP until it
    succeeds; empty (default) disables it. Device-specific, so set in config.local.toml.
    """

    ntp_server: str = ""
    ntp_timeout_s: float = 3.0

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
    display: DisplayConfig
    latency_test: LatencyTestConfig
    uploader: UploaderConfig
    logging: LoggingConfig
    system: SystemConfig
    link_watchdog: LinkWatchdogConfig = field(default_factory=LinkWatchdogConfig)
    clock: ClockConfig = field(default_factory=ClockConfig)

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
        run_status=RunStatusConfig(**raw.get("run_status", {})),
        backend=BackendConfig(**raw.get("backend", {})),
        heartbeat=HeartbeatConfig(**raw.get("heartbeat", {})),
        selftest=SelftestConfig(**raw.get("selftest", {})),
        gps_fix_test=GpsFixTestConfig(**raw.get("gps_fix_test", {})),
        storage=StorageConfig(**raw.get("storage", {})),
        display=DisplayConfig(**raw.get("display", {})),
        latency_test=LatencyTestConfig(**raw.get("latency_test", {})),
        uploader=UploaderConfig(**_without_removed_uploader_fields(raw["uploader"])),
        logging=LoggingConfig(**raw.get("logging", {})),
        system=SystemConfig(**_without_removed_system_fields(raw.get("system", {}))),
        link_watchdog=LinkWatchdogConfig(**raw.get("link_watchdog", {})),
        clock=ClockConfig(**raw.get("clock", {})),
    )
    _read_legacy_system_mode_as_manual(config.system)
    _ignore_leftover_profiles(raw)
    _validate_config(config)
    _apply_local_config_file(config, path)
    _apply_config_overrides_file(config, path)
    _warn_on_unset_identity_fields(config)
    return config

VENDORED_QUECTEL_CM_BINARY = Path("vendor") / "quectel-CM" / "quectel-CM"
LAST_GOOD_LINK_SETTINGS_FILENAME = "last_good_link_settings.json"

@dataclass
class LinkSettings:
    """Just what bringing up and supervising the cellular link needs."""

    running_on_pi: bool
    quectel_cm: QuectelCmConfig
    link_watchdog: LinkWatchdogConfig

def load_link_settings(config_path: Path) -> LinkSettings:
    """Reads the link settings leniently, for when load_config() itself failed. Never raises.

    Only `[system] running_on_pi`, `[quectel_cm]` and `[link_watchdog]` are read, from
    config.toml and the fields config.local.toml may set; nothing else in either file is looked
    at, so no secret is touched. Underneath both lie the link settings of the last boot whose
    config loaded (see config_sources.py::save_last_good_link_settings()): a single syntax error
    makes config.toml unreadable as a whole, and with it the APN quectel-CM needs. An unreadable
    file or a field of the wrong type is skipped and the value below it kept, the built-in default
    at the bottom. A link watchdog that fails validation gets its defaults. Without a usable
    quectel-CM binary the copy vendored next to config.toml is used, so the link can come up on a
    device whose configuration is broken. Logs field and file names, never values.
    """
    from measurement_software.core.config_sources import ALLOWED_LOCAL_FIELDS, LOCAL_CONFIG_FILENAME
    link_fields = {
        "system": ("running_on_pi",),
        "quectel_cm": tuple(f.name for f in fields(QuectelCmConfig)),
        "link_watchdog": tuple(f.name for f in fields(LinkWatchdogConfig)),
    }
    last_good_path = config_path.parent / LAST_GOOD_LINK_SETTINGS_FILENAME
    local_path = config_path.parent / LOCAL_CONFIG_FILENAME
    sources = [
        (last_good_path, _read_json_leniently(last_good_path), link_fields),
        (config_path, _read_toml_leniently(config_path), link_fields),
        (local_path, _read_toml_leniently(local_path), ALLOWED_LOCAL_FIELDS),
    ]
    system, quectel_cm, link_watchdog = SystemConfig(), QuectelCmConfig(), LinkWatchdogConfig()
    for path, raw, allowed in sources:
        system = _with_readable_fields(system, "system", raw, allowed, path)
        quectel_cm = _with_readable_fields(quectel_cm, "quectel_cm", raw, allowed, path)
        link_watchdog = _with_readable_fields(link_watchdog, "link_watchdog", raw, allowed, path)
    try:
        _validate_link_watchdog(link_watchdog)
    except ValueError:
        logger.warning("Ignoring [link_watchdog] - its values fail validation; using the defaults.")
        link_watchdog = LinkWatchdogConfig()
    if quectel_cm.binary in ("", _PLACEHOLDER_QUECTEL_CM_BINARY):
        quectel_cm = replace(quectel_cm, binary=str(config_path.parent / VENDORED_QUECTEL_CM_BINARY))
    return LinkSettings(system.running_on_pi, quectel_cm, link_watchdog)

def _read_json_leniently(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        logger.warning("Ignoring %s - it can't be read (%s).", path.name, type(e).__name__)
        return {}
    return raw if isinstance(raw, dict) else {}

def _read_toml_leniently(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        logger.warning("Ignoring %s - it can't be read (%s).", path.name, type(e).__name__)
        return {}

def _with_readable_fields(current, section: str, raw: dict, allowed: dict, path: Path):
    """Returns `current` with the fields of `raw[section]` that `allowed` names and that have the
    right type applied."""
    values = raw.get(section)
    if not isinstance(values, dict):
        return current
    updates = {}
    for config_field in fields(current):
        name = config_field.name
        if name not in values or name not in allowed.get(section, ()):
            continue
        value = _coerced(values[name], getattr(current, name))
        if value is None:
            logger.warning("Ignoring %s.%s in %s - wrong type.", section, name, path.name)
            continue
        updates[name] = value
    return replace(current, **updates)

def _coerced(value: object, default: object) -> object | None:
    """`value` as the type of `default`, or None if it isn't one."""
    if isinstance(default, bool) or isinstance(value, bool):
        return value if isinstance(value, bool) and isinstance(default, bool) else None
    if isinstance(default, float):
        return float(value) if isinstance(value, (int, float)) else None
    if isinstance(default, tuple):
        is_strings = isinstance(value, list) and all(isinstance(item, str) for item in value)
        return tuple(value) if is_strings else None
    return value if isinstance(value, type(default)) else None

_REMOVED_UPLOADER_FIELDS = ("upload_user", "upload_host", "upload_port", "remote_dir")

def _without_removed_uploader_fields(raw_uploader: dict) -> dict:
    """Drops the SCP-only [uploader] fields an old config.toml may still carry, warning once.

    Only the field names are logged, never their values. Not rejected: a config.toml failure
    stops boot before the cellular link is up, over fields that no longer do anything.
    """
    leftovers = [name for name in _REMOVED_UPLOADER_FIELDS if name in raw_uploader]
    if leftovers:
        logger.warning(
            "Ignoring removed [uploader] field(s) in config.toml: %s. The selftest no longer uploads "
            "via scp - delete them.", ", ".join(leftovers),
        )
    return {k: v for k, v in raw_uploader.items() if k not in _REMOVED_UPLOADER_FIELDS}

_REMOVED_SYSTEM_FIELDS = ("shutdown_gpio",)

def _without_removed_system_fields(raw_system: dict) -> dict:
    """Drops settings that no longer exist from the [system] table, with one warning per field.

    A leftover in a device's config.toml must not fail the boot: that would end it before the
    cellular link is up, leaving the device unreachable.
    """
    for name in _REMOVED_SYSTEM_FIELDS:
        if name in raw_system:
            logger.warning("Ignoring system.%s - this setting was removed.", name)
    return {name: value for name, value in raw_system.items() if name not in _REMOVED_SYSTEM_FIELDS}

_PLACEHOLDER_BACKEND_URL = "https://add.your.backend.here"
_PLACEHOLDER_QUECTEL_CM_BINARY = "/add/your/quectel-CM/path/here"

def _warn_on_unset_identity_fields(config: AppConfig) -> None:
    """Logs a warning for each per-device identity/secret field still left unset or at its
    config.toml placeholder - the fields config.local.toml exists to hold.

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

def _ignore_leftover_profiles(raw: dict) -> None:
    """Warns about `[profiles.*]` tables left in an old config.toml; their content is ignored.

    Not rejected: a config.toml failure stops boot before the cellular link is up, leaving the
    device without heartbeat or SSH over something it can safely skip. Only the table names are
    logged, never their values.
    """
    if "profiles" not in raw:
        return
    leftover = raw["profiles"]
    names = [f"[profiles.{name}]" for name in leftover] if isinstance(leftover, dict) else ["profiles"]
    logger.warning(
        "Settings profiles were removed; ignoring %s in config.toml. mission_type is only a label.",
        ", ".join(names),
    )

def _apply_local_config_file(config: AppConfig, config_path: Path) -> None:
    """Merges a sibling config.local.toml onto the just-built config, if one exists.

    Applied before config_overrides.toml: config.toml defaults first, then config.local.toml
    (per-device identity/secrets set by hand at setup time), then config_overrides.toml (remote
    runtime tuning) merged last. Deferred import: core.config_sources
    itself needs AppConfig from this module, so importing it at module level here would be
    circular.
    """
    from measurement_software.core.config_sources import LOCAL_CONFIG_FILENAME, apply_local_config, load_local_config
    local_path = config_path.parent / LOCAL_CONFIG_FILENAME
    apply_local_config(config, load_local_config(local_path))

def _apply_config_overrides_file(config: AppConfig, config_path: Path) -> None:
    """Merges a sibling config_overrides.toml onto the just-built config, if one exists.

    The checks in _validate_config() run again on the result, since an override can break what
    config.toml itself satisfied (e.g. a home position with only one coordinate set). A
    violation never stops boot - the device may have no one at it to fix a bad remote value -
    so the override fields in the violated check's scope are dropped and logged, leaving the
    config.toml/config.local.toml values, which already passed. Those alone still raise.

    Deferred import: core.config_sources itself needs AppConfig from this module, so importing
    it at module level here would be circular.
    """
    from measurement_software.core.config_sources import OVERRIDES_FILENAME, apply_overrides, load_overrides
    overrides = load_overrides(config_path.parent / OVERRIDES_FILENAME)
    baseline = copy.deepcopy(config)
    while True:
        candidate = copy.deepcopy(baseline)
        apply_overrides(candidate, overrides)
        dropped = _drop_violating_overrides(candidate, overrides)
        if not dropped:
            break
    config.__dict__.update(candidate.__dict__)

def _drop_violating_overrides(candidate: AppConfig, overrides: object) -> bool:
    """Clears from `overrides` the fields scoped to the first check `candidate` fails, returning
    whether it found one."""
    for check, override_fields in _CHECKS_WITH_OVERRIDE_SCOPE:
        try:
            check(candidate)
        except ValueError as e:
            offending = [name for name in override_fields if getattr(overrides, name) is not None]
            logger.error("Dropping config overrides %s - they break a config check: %s", offending, e)
            for name in offending:
                setattr(overrides, name, None)
            return True
    return False

def _build_quectel_cm_config(raw: dict) -> QuectelCmConfig:
    """Builds a QuectelCmConfig, normalizing TOML's `args` array into a tuple."""
    if "args" in raw:
        raw = raw | {"args": tuple(raw["args"])}
    return QuectelCmConfig(**raw)

def _validate_movement_gate_home_position(config: MovementGateConfig) -> None:
    """Raises ValueError if only one of home_latitude/home_longitude is set."""
    has_lat = config.home_latitude is not None
    has_lon = config.home_longitude is not None
    if has_lat != has_lon:
        raise ValueError(
            "movement_gate.home_latitude and home_longitude must both be set, or both left unset."
        )

_VALID_SYSTEM_MODES = ("auto", "manual")

# Manual Mode's name before it was renamed. Devices, their config files and backends from before
# the rename still carry it, so exactly this one value is read as "manual"; anything else is still
# rejected.
LEGACY_MANUAL_SYSTEM_MODE = "waiting"

def _read_legacy_system_mode_as_manual(config: SystemConfig) -> None:
    """Maps a config.toml `mode` still holding Manual Mode's old name to "manual", with a warning.

    Not rejected like other invalid modes: a config.toml failure stops boot before the cellular
    link is up, leaving the device without heartbeat or SSH over something it can safely read.
    """
    if config.mode != LEGACY_MANUAL_SYSTEM_MODE:
        return
    logger.warning(
        "config.toml system.mode %r is Manual Mode's old name - reading it as 'manual'.",
        LEGACY_MANUAL_SYSTEM_MODE,
    )
    config.mode = "manual"

def _validate_system_mode(config: AppConfig) -> None:
    """Raises ValueError if system.mode is unrecognized."""
    if config.system.mode not in _VALID_SYSTEM_MODES:
        raise ValueError(
            f"system.mode must be one of {_VALID_SYSTEM_MODES}, got {config.system.mode!r}."
        )

def _validate_modem_reconnect(config: CollectorConfig) -> None:
    """Raises ValueError unless at least one reconnect attempt with a non-negative delay is set."""
    if config.modem_reconnect_attempts < 1:
        raise ValueError(
            f"collector.modem_reconnect_attempts must be at least 1, got {config.modem_reconnect_attempts}."
        )
    if config.modem_reconnect_delay_s < 0:
        raise ValueError(
            f"collector.modem_reconnect_delay_s must not be negative, got {config.modem_reconnect_delay_s}."
        )

def _validate_crash_limits(config: SystemConfig) -> None:
    """Raises ValueError unless both crash limits allow at least one occurrence."""
    for name in ("crash_same_error_limit", "crash_reboot_limit"):
        if getattr(config, name) < 1:
            raise ValueError(f"system.{name} must be at least 1, got {getattr(config, name)}.")

def _validate_link_watchdog(config: LinkWatchdogConfig) -> None:
    """Raises ValueError for non-positive intervals, a maximum interval below the first one, or
    a negative restart count."""
    for name in ("check_interval_s", "restart_after_s"):
        if getattr(config, name) <= 0:
            raise ValueError(f"link_watchdog.{name} must be positive, got {getattr(config, name)}.")
    if config.max_restart_interval_s < config.restart_after_s:
        raise ValueError(
            f"link_watchdog.max_restart_interval_s must be at least restart_after_s "
            f"({config.restart_after_s}), got {config.max_restart_interval_s}."
        )
    if config.radio_reset_after_restarts < 0:
        raise ValueError(
            f"link_watchdog.radio_reset_after_restarts must not be negative, got {config.radio_reset_after_restarts}."
        )

def _validate_clock(config: ClockConfig) -> None:
    """Raises ValueError for a non-positive NTP timeout."""
    if config.ntp_timeout_s <= 0:
        raise ValueError(f"clock.ntp_timeout_s must be positive, got {config.ntp_timeout_s}.")

def _validate_config(config: AppConfig) -> None:
    for check, _ in _CHECKS_WITH_OVERRIDE_SCOPE:
        check(config)

# Each check with the override fields that can influence its outcome: if a check fails after
# overrides were applied, exactly these are dropped. Scoped this widely (not per offending field)
# because a check can span several fields - the home position's latitude and longitude only make
# sense as a pair.
_CHECKS_WITH_OVERRIDE_SCOPE = (
    (lambda config: _validate_movement_gate_home_position(config.movement_gate),
     ("movement_gate_home_latitude", "movement_gate_home_longitude")),
    (_validate_system_mode, ("system_mode",)),
    (lambda config: _validate_modem_reconnect(config.collector), ()),
    (lambda config: _validate_crash_limits(config.system), ()),
    (lambda config: _validate_link_watchdog(config.link_watchdog), ()),
    (lambda config: _validate_clock(config.clock), ()),
)
