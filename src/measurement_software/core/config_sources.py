import fcntl
import logging
import math
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterator

import tomlkit

from measurement_software.core.config import AppConfig
from measurement_software.core.util import flush_to_disk, fsync_directory

logger = logging.getLogger(__name__)

LEGACY_MODE_OVERRIDE_FILENAME = "mode_override.toml"
PENDING_REBOOT_ACK_FILENAME = "pending_reboot_ack.toml"


def write_pending_reboot_ack(path: Path, command_id: Any) -> None:
    """Persists a reboot-now command's id so the next boot can ack it as actually successful.

    Written right before perform_reboot() cuts this process off for good - there is no later
    heartbeat tick within the same run to send a real success ack from, unlike every other
    one-shot command. The next boot's main() reads this (see read_and_clear_pending_reboot_ack())
    and seeds it into the new RemoteCommandDispatcher, so the very first heartbeat after the
    reboot reports what actually happened, not just the "received" the old process managed to
    send on its way out. Same atomic-write durability pattern as _write_overrides_raw().
    """
    value = f'"{command_id}"' if isinstance(command_id, str) else repr(command_id)
    temp_path = path.with_name(f"{path.name}.tmp")
    with open(temp_path, "w") as f:
        f.write(f"command_id = {value}\n")
        flush_to_disk(f)
    temp_path.replace(path)
    fsync_directory(path.parent)


def read_and_clear_pending_reboot_ack(path: Path) -> Any | None:
    """Reads and removes the pending reboot-now ack left by the previous boot's reboot(), or
    None if none is set. Removed immediately (not just on success) so a corrupt or stale file
    can never be reapplied on a later boot."""
    if not path.exists():
        return None
    with open(path, "rb") as f:
        command_id = tomllib.load(f).get("command_id")
    path.unlink()
    return command_id


OVERRIDES_FILENAME = "config_overrides.toml"

# Key in config_overrides.toml listing the nullable fields that are explicitly overridden back to
# unset. TOML has no null, so "overridden to None" can't be written as a plain `field = value`.
CLEARED_KEY = "cleared"


@dataclass(frozen=True)
class FieldSpec:
    """What a remotely-set override value has to look like to be persisted at all."""

    kind: str
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()


def _float(low: float, high: float) -> FieldSpec:
    return FieldSpec("float", low, high)


def _int(low: int, high: int) -> FieldSpec:
    return FieldSpec("int", low, high)


def _nullable_float(low: float, high: float) -> FieldSpec:
    return FieldSpec("nullable_float", low, high)


def _enum(*choices: str) -> FieldSpec:
    return FieldSpec("enum", choices=choices)


_BOOL = FieldSpec("bool")

# The allow-list itself: a field is remotely settable if and only if it is a key here. Sane bounds
# are defense-in-depth against a buggy or compromised backend - its own schema only enforces
# field name/type, not operational sanity. idle/day-end and the per-feature cadence fields are
# bounded to a sane sub-day range, since several days would never end a run. idle_threshold_s
# additionally accepts 0, meaning "never end on idle"; the cadence fields keep a lower bound of 1,
# since 0 would spin. collector_max_wait_for_first_fix gets a tighter upper bound (1 hour) since it gates
# the very start of a run - an unbounded wait for a fix that never arrives could burn an entire
# field deployment day before anything is captured. movement_gate_confirmations_required gets a
# small integer ceiling - an unbounded value could mean the device never leaves LOW_POWER_WAITING
# at all. The last group (modem/GNSS/GPIO) is validated exactly like the rest; the frontend's
# "do you know what you are doing" confirmation for it is not a device concern.
FIELD_SPECS: dict[str, FieldSpec] = {
    "idle_threshold_s": _float(0, 86400),
    "day_end_threshold_s": _float(0, 86400),
    "movement_gate_enabled": _BOOL,
    "movement_gate_poll_interval_s": _float(1, 86400),
    "latency_test_baseline_interval_s": _float(1, 86400),
    "latency_test_load_interval_s": _float(1, 86400),
    "latency_test_poll_interval_s": _float(1, 86400),
    "heartbeat_interval_s": _float(1, 86400),
    "collector_max_wait_for_first_fix": _float(1, 3600),
    "movement_gate_movement_threshold": _float(0.1, 1000),
    "movement_gate_confirmations_required": _int(1, 10),
    "run_status_empty_captures_until_pipeline_broken": _int(1, 100),
    "collector_capture_gate": _enum("distance", "time"),
    "collector_position_threshold": _float(0.1, 1000),
    "collector_capture_interval_s": _float(1, 86400),
    "collector_wait_log_interval": _float(1, 3600),
    "collector_gps_enabled": _BOOL,
    "collector_gps_disabled_poll_interval_s": _float(1, 86400),
    "movement_gate_home_latitude": _nullable_float(-90, 90),
    "movement_gate_home_longitude": _nullable_float(-180, 180),
    "movement_gate_home_departure_threshold": _float(1, 10000),
    "latency_test_enabled": _BOOL,
    "system_mode": _enum("auto", "waiting"),
    "device_mission_type": _enum("ground", "drone"),
    "display_enabled": _BOOL,
    "display_refresh_interval_s": _float(0.5, 3600),
    "modem_mode": _enum("serving_cell", "sa_scan"),
    "modem_sim_unlock_poll_attempts": _int(1, 100),
    "modem_sim_unlock_poll_interval": _float(0.1, 60),
    "gnss_receiver_retries": _int(0, 20),
    "gnss_receiver_retry_delay_s": _float(0, 60),
    "system_running_on_pi": _BOOL,
    "system_shutdown_gpio": _int(2, 27),
}

ALLOWED_OVERRIDE_FIELDS = tuple(FIELD_SPECS)

NULLABLE_OVERRIDE_FIELDS = frozenset(
    field for field, spec in FIELD_SPECS.items() if spec.kind == "nullable_float"
)

# Where each override field's value lives in both the already-built AppConfig
# (apply_overrides, below) and config.toml itself (promote_overrides, below) - one declarative
# mapping consumed by both, so they can't drift apart as fields are added. Each target is a
# dotted AppConfig/config.toml section path plus the field name within it.
# day_end_threshold_s is deliberately absent - see ConfigOverrides' docstring.
OVERRIDE_FIELD_TARGETS: dict[str, tuple[tuple[str, str], ...]] = {
    "idle_threshold_s": (("collector", "max_idle_time"),),
    "movement_gate_enabled": (("movement_gate", "enabled"),),
    "movement_gate_poll_interval_s": (("movement_gate", "poll_interval_s"),),
    "latency_test_baseline_interval_s": (("latency_test", "baseline_interval_s"),),
    "latency_test_load_interval_s": (("latency_test", "load_interval_s"),),
    "latency_test_poll_interval_s": (("latency_test", "poll_interval_s"),),
    "heartbeat_interval_s": (("heartbeat", "interval_s"),),
    "collector_max_wait_for_first_fix": (("collector", "max_wait_for_first_fix"),),
    "movement_gate_movement_threshold": (("movement_gate", "movement_threshold"),),
    "movement_gate_confirmations_required": (("movement_gate", "confirmations_required"),),
    "run_status_empty_captures_until_pipeline_broken": (
        ("run_status", "empty_captures_until_pipeline_broken"),
    ),
    "collector_capture_gate": (("collector", "capture_gate"),),
    "collector_position_threshold": (("collector", "position_threshold"),),
    "collector_capture_interval_s": (("collector", "capture_interval_s"),),
    "collector_wait_log_interval": (("collector", "wait_log_interval"),),
    "collector_gps_enabled": (("collector", "gps_enabled"),),
    "collector_gps_disabled_poll_interval_s": (("collector", "gps_disabled_poll_interval_s"),),
    "movement_gate_home_latitude": (("movement_gate", "home_latitude"),),
    "movement_gate_home_longitude": (("movement_gate", "home_longitude"),),
    "movement_gate_home_departure_threshold": (("movement_gate", "home_departure_threshold"),),
    "latency_test_enabled": (("latency_test", "enabled"),),
    "system_mode": (("system", "mode"),),
    "device_mission_type": (("device", "mission_type"),),
    "display_enabled": (("display", "enabled"),),
    "display_refresh_interval_s": (("display", "refresh_interval_s"),),
    "modem_mode": (("modem", "mode"),),
    "modem_sim_unlock_poll_attempts": (("modem", "sim_unlock_poll_attempts"),),
    "modem_sim_unlock_poll_interval": (("modem", "sim_unlock_poll_interval"),),
    "gnss_receiver_retries": (("gnss_receiver", "retries"),),
    "gnss_receiver_retry_delay_s": (("gnss_receiver", "retry_delay_s"),),
    "system_running_on_pi": (("system", "running_on_pi"),),
    "system_shutdown_gpio": (("system", "shutdown_gpio"),),
}


class _Cleared:
    """Marks a nullable override field as explicitly overridden back to unset (None).

    Distinct from None, which in ConfigOverrides means "not overridden at all".
    """

    def __repr__(self) -> str:
        return "CLEARED"


CLEARED = _Cleared()


@dataclass
class ConfigOverrides:
    """Remotely-set overrides for the allow-listed subset of config, persisted across boots.

    A field left at None is not overridden; the nullable home-position fields can additionally
    hold CLEARED, meaning "override back to unset".

    `day_end_threshold_s` is part of the backend's fixed allow-list but has no device-side
    behavior to attach to yet - the day-end cutoff is future work. It is still accepted,
    bounds-checked and persisted here so a maintainer can stage it ahead of that work landing,
    but `apply_overrides` below is a deliberate no-op for it.
    """

    idle_threshold_s: float | None = None
    day_end_threshold_s: float | None = None
    movement_gate_enabled: bool | None = None
    movement_gate_poll_interval_s: float | None = None
    latency_test_baseline_interval_s: float | None = None
    latency_test_load_interval_s: float | None = None
    latency_test_poll_interval_s: float | None = None
    heartbeat_interval_s: float | None = None
    collector_max_wait_for_first_fix: float | None = None
    movement_gate_movement_threshold: float | None = None
    movement_gate_confirmations_required: int | None = None
    run_status_empty_captures_until_pipeline_broken: int | None = None
    collector_capture_gate: str | None = None
    collector_position_threshold: float | None = None
    collector_capture_interval_s: float | None = None
    collector_wait_log_interval: float | None = None
    collector_gps_enabled: bool | None = None
    collector_gps_disabled_poll_interval_s: float | None = None
    movement_gate_home_latitude: float | _Cleared | None = None
    movement_gate_home_longitude: float | _Cleared | None = None
    movement_gate_home_departure_threshold: float | None = None
    latency_test_enabled: bool | None = None
    system_mode: str | None = None
    device_mission_type: str | None = None
    display_enabled: bool | None = None
    display_refresh_interval_s: float | None = None
    modem_mode: str | None = None
    modem_sim_unlock_poll_attempts: int | None = None
    modem_sim_unlock_poll_interval: float | None = None
    gnss_receiver_retries: int | None = None
    gnss_receiver_retry_delay_s: float | None = None
    system_running_on_pi: bool | None = None
    system_shutdown_gpio: int | None = None


def _is_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validated_value(spec: FieldSpec, value: Any) -> tuple[bool, Any]:
    """Returns (accepted, normalized_value) for `value` against `spec`.

    Strict by kind: `bool` is an `int` subclass in Python, so bools are matched by exact type
    and never pass as numbers; enums are matched by exact string, no case/whitespace folding.
    """
    if spec.kind == "bool":
        return type(value) is bool, value
    if spec.kind == "enum":
        return type(value) is str and value in spec.choices, value
    if spec.kind == "nullable_float" and value is None:
        return True, None
    if not _is_finite_number(value):
        return False, value
    if spec.kind == "int":
        if isinstance(value, float):
            if not value.is_integer():
                return False, value
            value = int(value)
    elif spec.kind in ("float", "nullable_float"):
        value = float(value)
    return spec.low <= value <= spec.high, value


def sanitize_overrides(raw: dict) -> dict:
    """Filters a mapping of proposed overrides down to allow-listed fields of the right type
    within sane bounds.

    Every rejection is logged and the field is simply dropped rather than raising, so one bad
    field (a typo, a stale allow-list entry) never blocks the rest of an otherwise-valid batch.
    A nullable field accepts None, meaning "override back to unset".
    """
    sanitized: dict[str, Any] = {}
    for field, value in raw.items():
        spec = FIELD_SPECS.get(field)
        if spec is None:
            logger.warning("Ignoring config override for disallowed field %r.", field)
            continue
        accepted, normalized = _validated_value(spec, value)
        if not accepted:
            logger.warning(
                "Ignoring config override %s=%r - not a valid %s%s.", field, value, spec.kind,
                _range_description(spec),
            )
            continue
        sanitized[field] = normalized
    return sanitized


def _range_description(spec: FieldSpec) -> str:
    if spec.kind == "enum":
        return f" (one of {list(spec.choices)})"
    if spec.low is not None:
        return f" within [{spec.low}, {spec.high}]"
    return ""


def read_overrides_file(path: Path) -> dict:
    """Reads the override fields on disk, or an empty mapping if no file exists yet.

    Fields listed under `cleared` come back as None, so the mapping has one value per field and
    None always means "explicitly overridden to unset". An unreadable file is logged and treated
    as empty rather than raised, so a corrupt overrides file can never stop the device booting.
    """
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        logger.error("Ignoring unreadable %s: %s", path, e)
        return {}
    cleared = raw.pop(CLEARED_KEY, [])
    if not isinstance(cleared, list):
        logger.warning("Ignoring malformed %r entry in %s: %r", CLEARED_KEY, path, cleared)
        cleared = []
    for field in cleared:
        if field not in NULLABLE_OVERRIDE_FIELDS:
            logger.warning("Ignoring %r entry for non-nullable field %r.", CLEARED_KEY, field)
            continue
        raw[field] = None
    return raw


def write_overrides_file(path: Path, changes: dict) -> None:
    """Merges `changes` onto whatever overrides are already on disk and writes the result back.

    Each field replaces any existing value for that field; fields not mentioned in `changes` are
    left untouched, so a fresh override for one field never erases another still in effect.
    Locked against a concurrent promote_overrides() run on the same file - see _locked_file().
    """
    with _locked_file(path):
        _write_overrides_raw(path, read_overrides_file(path) | changes)


def _write_overrides_raw(path: Path, fields_to_write: dict) -> None:
    """Writes `fields_to_write` as the complete contents of the overrides file (no merge).

    A None value is written into the `cleared` list. Serialized by tomlkit by type, never by
    interpolating a value into TOML text. Written atomically (temp file + fsync + rename), same
    durability pattern as core/util.py. Caller is responsible for any locking - see _locked_file().
    """
    document = tomlkit.document()
    cleared = []
    for field in ALLOWED_OVERRIDE_FIELDS:
        if field not in fields_to_write:
            continue
        if fields_to_write[field] is None:
            cleared.append(field)
        else:
            document[field] = fields_to_write[field]
    if cleared:
        document[CLEARED_KEY] = cleared
    temp_path = path.with_name(f"{path.name}.tmp")
    with open(temp_path, "w") as f:
        f.write(tomlkit.dumps(document))
        flush_to_disk(f)
    temp_path.replace(path)
    fsync_directory(path.parent)


@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
    """Serializes read-modify-write access to `path` across threads and processes.

    Takes an OS advisory lock (fcntl.flock) on a sibling `<name>.lock` file for the duration of
    the `with` block, so RemoteCommandDispatcher's heartbeat-thread writes to config_overrides.toml
    and a promote_overrides() run (in-process via the promote-overrides-now command, or a separate
    `promote_overrides.py` invocation) can never interleave and silently drop one or the other.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def migrate_legacy_mode_override(overrides_path: Path, legacy_path: Path) -> None:
    """Moves a leftover mode_override.toml's mode into config_overrides.toml, exactly once.

    The mode used to live in its own file; it is now the ordinary `system_mode` override. The
    stored value goes through sanitize_overrides() like any remote value (an invalid or
    unreadable one is dropped, never applied), and an existing `system_mode` in the overrides
    file wins. The legacy file is deleted only after the write has been fsynced, and also when
    its content was unusable, so the migration never repeats. A crash between write and delete
    is harmless: rerunning finds `system_mode` already set and just deletes the file.
    """
    if not legacy_path.exists():
        return
    legacy_mode = _read_legacy_mode(legacy_path)
    migrated = sanitize_overrides({"system_mode": legacy_mode}) if legacy_mode is not None else {}
    if migrated:
        try:
            with _locked_file(overrides_path):
                existing = read_overrides_file(overrides_path)
                if "system_mode" not in existing:
                    _write_overrides_raw(overrides_path, existing | migrated)
        except OSError as e:
            logger.error("Could not migrate %s into %s: %s", legacy_path, overrides_path, e)
            return
        logger.info("Migrated device mode from %s into %s.", legacy_path.name, overrides_path.name)
    legacy_path.unlink()
    fsync_directory(legacy_path.parent)


def _read_legacy_mode(legacy_path: Path) -> Any:
    try:
        with open(legacy_path, "rb") as f:
            return tomllib.load(f).get("mode")
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Dropping unreadable %s: %s", legacy_path, e)
        return None


def load_overrides(path: Path) -> ConfigOverrides:
    """Loads the override file into a ConfigOverrides, defaulting every unset field to None.

    Re-validated through sanitize_overrides() on every read, so a stale or hand-edited file
    (an unknown field, an out-of-range value) loses just that field instead of stopping boot.
    """
    sanitized = sanitize_overrides(read_overrides_file(path))
    return ConfigOverrides(**{
        field: CLEARED if value is None else value for field, value in sanitized.items()
    })


def apply_overrides(config: AppConfig, overrides: ConfigOverrides) -> None:
    """Applies whatever overrides are set onto the already-built AppConfig, in place.

    Driven by OVERRIDE_FIELD_TARGETS above. Values are cast to their field's declared kind so a
    stored int landing on a float field (or the reverse) matches the target's type.
    """
    for override_field in fields(overrides):
        value = getattr(overrides, override_field.name)
        if value is None:
            continue
        value = None if value is CLEARED else _cast(override_field.name, value)
        for section_path, target_field in OVERRIDE_FIELD_TARGETS.get(override_field.name, ()):
            setattr(resolve_section(config, section_path), target_field, value)


def parse_profiles(raw: Any) -> dict[str, ConfigOverrides]:
    """Validates the `[profiles.*]` tables of config.toml and returns each as a ConfigOverrides.

    A profile is a mapping of override field names to values, validated through the same
    FIELD_SPECS as remote overrides - so only allow-listed fields can appear, never the
    identity/secret fields of config.local.toml. Unlike remote input, config.toml is
    repo-controlled, so anything invalid raises ValueError instead of being dropped. Every profile
    is checked, not just the active one, so a typo in the other fails at the first boot too.
    `device_mission_type` is rejected: the mission type selects the profile, so a profile
    setting it would be circular.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"config.toml 'profiles' must be a table, got {type(raw).__name__}.")
    known_profiles = FIELD_SPECS["device_mission_type"].choices
    profiles: dict[str, ConfigOverrides] = {}
    for name, fields_raw in raw.items():
        if name not in known_profiles:
            raise ValueError(f"Unknown profile [profiles.{name}] - expected one of {list(known_profiles)}.")
        if not isinstance(fields_raw, dict):
            raise ValueError(f"[profiles.{name}] must be a table.")
        profiles[name] = ConfigOverrides(**_validated_profile_fields(name, fields_raw))
    return profiles


def _validated_profile_fields(name: str, fields_raw: dict) -> dict[str, Any]:
    validated: dict[str, Any] = {}
    for field, value in fields_raw.items():
        spec = FIELD_SPECS.get(field)
        if spec is None or field == "device_mission_type":
            raise ValueError(f"[profiles.{name}] field {field!r} is not allowed in a profile.")
        accepted, normalized = _validated_value(spec, value)
        if not accepted:
            raise ValueError(
                f"[profiles.{name}] {field} is not a valid {spec.kind}{_range_description(spec)}."
            )
        validated[field] = normalized
    return validated


def _cast(field: str, value: Any) -> Any:
    kind = FIELD_SPECS[field].kind
    if kind in ("float", "nullable_float"):
        return float(value)
    if kind == "int":
        return int(value)
    return value


def resolve_section(config: AppConfig, section_path: str) -> Any:
    """Walks a dotted section path (e.g. "movement_gate") from the AppConfig root."""
    section: Any = config
    for part in section_path.split("."):
        section = getattr(section, part)
    return section


def promote_overrides(config_path: Path) -> dict[str, tuple[Any, Any]]:
    """Bakes every currently-set config_overrides.toml field into config.toml as its new
    baseline, then clears just those fields from config_overrides.toml.

    Targets come from OVERRIDE_FIELD_TARGETS - the same mapping apply_overrides() uses - so the
    two can never drift apart. A set field with no mapped target (currently only
    day_end_threshold_s) is left in place and logged, since there's nowhere in config.toml to
    promote it to. A key that is absent from config.toml (e.g. the commented-out home position)
    is added, and promoting an explicit unset removes the key again. config.toml is edited via
    tomlkit so every comment and unrelated field survives untouched; both files are written
    atomically. No-op (returns {}) if nothing is set, or if nothing set has a target. Returns the
    promoted fields as {field: (old_value, new_value)}.

    Locked around the whole read-promote-remove sequence (see _locked_file()) so a concurrent
    config_overrides.toml write from RemoteCommandDispatcher's heartbeat thread - or from a
    separate process, since this also runs as the standalone promote_overrides.py script -
    can't interleave with it and have its change silently dropped.
    """
    overrides_path = config_path.parent / OVERRIDES_FILENAME
    with _locked_file(overrides_path):
        raw = read_overrides_file(overrides_path)
        promotable = {field: value for field, value in raw.items() if field in OVERRIDE_FIELD_TARGETS}
        for field in raw.keys() - promotable.keys():
            logger.warning("Cannot promote %r - no config.toml target is mapped for it.", field)
        if not promotable:
            return {}

        with open(config_path, encoding="utf-8") as f:
            document = tomlkit.parse(f.read())

        promoted: dict[str, tuple[Any, Any]] = {}
        for field, value in promotable.items():
            if value is not None:
                value = _cast(field, value)
            old_value = None
            for section_path, target_field in OVERRIDE_FIELD_TARGETS[field]:
                table = _resolve_toml_table(document, section_path)
                old_value = table.get(target_field)
                if value is None:
                    table.pop(target_field, None)
                else:
                    table[target_field] = value
            promoted[field] = (old_value, value)

        _write_toml_document_atomically(config_path, document)
        _write_overrides_raw(overrides_path, {
            field: value for field, value in raw.items() if field not in promoted
        })

    for field, (old_value, new_value) in promoted.items():
        logger.info(
            "Promoted override %s into config.toml: %r -> %r (cleared from config_overrides.toml).",
            field, old_value, new_value,
        )
    return promoted


def _resolve_toml_table(document: tomlkit.TOMLDocument, section_path: str) -> Any:
    """Walks a dotted section path from a parsed tomlkit document, creating a missing table."""
    table: Any = document
    for part in section_path.split("."):
        if part not in table:
            table[part] = tomlkit.table()
        table = table[part]
    return table


def _write_toml_document_atomically(path: Path, document: tomlkit.TOMLDocument) -> None:
    """Writes a tomlkit document to `path` atomically (temp file + fsync + rename), preserving
    every comment and unrelated field tomlkit was never asked to touch."""
    temp_path = path.with_name(f"{path.name}.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(document))
        flush_to_disk(f)
    temp_path.replace(path)
    fsync_directory(path.parent)


LOCAL_CONFIG_FILENAME = "config.local.toml"

# Per-device identity/secrets a device operator sets by hand once at setup time - the local,
# static counterpart to config_overrides.toml's remotely-writable allow-list above. Deliberately allow-listed, not fully generic, same reasoning as
# ALLOWED_OVERRIDE_FIELDS: only fields excluded from remote override altogether belong here.
ALLOWED_LOCAL_FIELDS: dict[str, tuple[str, ...]] = {
    "backend": ("url", "device_key"),
    "heartbeat": ("hmac_secret", "device_id"),
    "device": ("device_id",),
    "quectel_cm": ("binary",),
    "selftest": ("results_url",),
    "gps_fix_test": ("status_url",),
    "latency_test": ("host",),
    "uploader": ("upload_dir",),
}


def _normalized_local_field_names() -> frozenset[str]:
    return frozenset(
        f"{section}_{field}" for section, fields in ALLOWED_LOCAL_FIELDS.items() for field in fields
    )


# Enforced at import time, not just documented: config.local.toml's fields and
# config_overrides.toml's allow-list must never overlap, so an identity/secret field can never
# become settable from a (compromised) backend response.
assert _normalized_local_field_names().isdisjoint(ALLOWED_OVERRIDE_FIELDS), (
    "config.local.toml fields must stay disjoint from config_overrides.toml's ALLOWED_OVERRIDE_FIELDS"
)


def load_local_config(path: Path) -> dict:
    """Reads the raw config.local.toml sections on disk, or an empty mapping if none exists."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def apply_local_config(config: AppConfig, raw: dict) -> None:
    """Applies allow-listed identity/secret fields from config.local.toml onto the already-built
    AppConfig, in place.

    Anything outside the allow-list (an unrecognized section, an unrecognized field within an
    allow-listed section) is logged and ignored rather than raising, so a typo never blocks the
    rest of an otherwise-valid file - same behavior as sanitize_overrides() above.
    """
    for section, raw_fields in raw.items():
        allowed_fields = ALLOWED_LOCAL_FIELDS.get(section)
        if allowed_fields is None:
            logger.warning("Ignoring config.local.toml section %r - not allow-listed.", section)
            continue
        if not isinstance(raw_fields, dict):
            logger.warning("Ignoring config.local.toml section %r - not a table.", section)
            continue
        section_config = getattr(config, section)
        for field, value in raw_fields.items():
            if field not in allowed_fields:
                logger.warning(
                    "Ignoring config.local.toml field %s.%s - not allow-listed.", section, field
                )
                continue
            setattr(section_config, field, value)
