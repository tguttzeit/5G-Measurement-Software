import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from measurement_software.core.config import AppConfig
from measurement_software.core.util import flush_to_disk, fsync_directory
from measurement_software.status.backend_status import LTE_LEGAL_RANGES, NR_LEGAL_RANGES

logger = logging.getLogger(__name__)

MODE_OVERRIDE_FILENAME = "mode_override.toml"


def read_mode_override(path: Path) -> str | None:
    """Reads the persisted next-boot device mode override, or None if none is set."""
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return tomllib.load(f).get("mode")


def write_mode_override(path: Path, mode: str) -> None:
    """Persists the device mode to take effect the next time config is loaded (next boot).

    Written atomically (temp file + fsync + rename), same durability pattern as
    write_overrides_file() below.
    """
    temp_path = path.with_name(f"{path.name}.tmp")
    with open(temp_path, "w") as f:
        f.write(f'mode = "{mode}"\n')
        flush_to_disk(f)
    temp_path.replace(path)
    fsync_directory(path.parent)


OVERRIDES_FILENAME = "config_overrides.toml"

# Field names are the allow-list itself, matching the backend's `ConfigOverrideCreate` schema
# exactly - renegotiating this set is out of scope for this device; a backend-side change would
# have to land first.
ALLOWED_OVERRIDE_FIELDS = (
    "rsrp_threshold",
    "rsrq_threshold",
    "sinr_threshold",
    "idle_threshold_s",
    "day_end_threshold_s",
    "movement_gate_poll_interval_s",
    "latency_test_baseline_interval_s",
    "latency_test_load_interval_s",
    "latency_test_poll_interval_s",
    "heartbeat_interval_s",
    "collector_max_wait_for_first_fix",
    "movement_gate_movement_threshold",
    "movement_gate_confirmations_required",
    "run_status_empty_captures_until_pipeline_broken",
)

# Sane bounds a value must clear before being persisted at all, as defense-in-depth against a
# buggy or compromised backend: the backend's own allow-list only enforces field name/type, not
# operational sanity. RSRP/RSRQ/SINR use the wider of the two RATs' physically-legal reporting
# ranges (status/backend_status.py), since a unified override applies to both RATs at once.
# idle/day-end and the per-feature cadence fields are bounded to a sane sub-day range - a value of
# 0 or several days would either end every run instantly or never. collector_max_wait_for_first_fix
# gets a tighter upper bound (1 hour) since it gates the very start of a run - an unbounded wait
# for a fix that never arrives could burn an entire field deployment day before anything is
# captured. movement_gate_movement_threshold is bounded like a sane on-foot-to-vehicle movement
# distance in meters. movement_gate_confirmations_required gets a small integer ceiling - an
# unbounded value could mean the device never leaves LOW_POWER_WAITING at all.
_BOUNDS: dict[str, tuple[float, float]] = {
    "rsrp_threshold": (min(LTE_LEGAL_RANGES.rsrp[0], NR_LEGAL_RANGES.rsrp[0]),
                       max(LTE_LEGAL_RANGES.rsrp[1], NR_LEGAL_RANGES.rsrp[1])),
    "rsrq_threshold": (min(LTE_LEGAL_RANGES.rsrq[0], NR_LEGAL_RANGES.rsrq[0]),
                       max(LTE_LEGAL_RANGES.rsrq[1], NR_LEGAL_RANGES.rsrq[1])),
    "sinr_threshold": (min(LTE_LEGAL_RANGES.sinr[0], NR_LEGAL_RANGES.sinr[0]),
                       max(LTE_LEGAL_RANGES.sinr[1], NR_LEGAL_RANGES.sinr[1])),
    "idle_threshold_s": (1, 86400),
    "day_end_threshold_s": (0, 86400),
    "movement_gate_poll_interval_s": (1, 86400),
    "latency_test_baseline_interval_s": (1, 86400),
    "latency_test_load_interval_s": (1, 86400),
    "latency_test_poll_interval_s": (1, 86400),
    "heartbeat_interval_s": (1, 86400),
    "collector_max_wait_for_first_fix": (1, 3600),
    "movement_gate_movement_threshold": (0.1, 1000),
    "movement_gate_confirmations_required": (1, 10),
    "run_status_empty_captures_until_pipeline_broken": (1, 100),
}


@dataclass
class ConfigOverrides:
    """Remotely-set overrides for the allow-listed subset of config, persisted across boots.

    `day_end_threshold_s` is part of the backend's fixed allow-list but has no device-side
    behavior to attach to yet - the day-end cutoff is future work. It is still accepted,
    bounds-checked and persisted here so a maintainer can stage it ahead of that work landing,
    but `apply_overrides` below is a deliberate no-op for it.
    """

    rsrp_threshold: float | None = None
    rsrq_threshold: float | None = None
    sinr_threshold: float | None = None
    idle_threshold_s: float | None = None
    day_end_threshold_s: float | None = None
    movement_gate_poll_interval_s: float | None = None
    latency_test_baseline_interval_s: float | None = None
    latency_test_load_interval_s: float | None = None
    latency_test_poll_interval_s: float | None = None
    heartbeat_interval_s: float | None = None
    collector_max_wait_for_first_fix: float | None = None
    movement_gate_movement_threshold: float | None = None
    movement_gate_confirmations_required: int | None = None
    run_status_empty_captures_until_pipeline_broken: float | None = None


def sanitize_overrides(raw: dict) -> dict:
    """Filters a mapping of proposed overrides down to allow-listed fields within sane bounds.

    Every rejection is logged and the field is simply dropped rather than raising, so one bad
    field (a typo, a stale allow-list entry) never blocks the rest of an otherwise-valid batch.
    """
    sanitized: dict[str, float] = {}
    for field, value in raw.items():
        if field not in ALLOWED_OVERRIDE_FIELDS:
            logger.warning("Ignoring config override for disallowed field %r.", field)
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            logger.warning("Ignoring config override %s=%r - not a number.", field, value)
            continue
        low, high = _BOUNDS[field]
        if not (low <= value <= high):
            logger.warning(
                "Ignoring config override %s=%r - outside sane bounds [%s, %s].",
                field, value, low, high,
            )
            continue
        sanitized[field] = value
    return sanitized


def read_overrides_file(path: Path) -> dict:
    """Reads the raw override fields on disk, or an empty mapping if no file exists yet."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def write_overrides_file(path: Path, changes: dict) -> None:
    """Merges `changes` onto whatever overrides are already on disk and writes the result back.

    Each field replaces any existing value for that field; fields not mentioned in `changes` are
    left untouched, so a fresh override for one field never erases another still in effect.
    Written atomically (temp file + fsync + rename), same durability pattern as core/util.py.
    """
    merged = read_overrides_file(path) | changes
    temp_path = path.with_name(f"{path.name}.tmp")
    with open(temp_path, "w") as f:
        for field in ALLOWED_OVERRIDE_FIELDS:
            if field in merged:
                f.write(f"{field} = {merged[field]!r}\n")
        flush_to_disk(f)
    temp_path.replace(path)
    fsync_directory(path.parent)


def load_overrides(path: Path) -> ConfigOverrides:
    """Loads the override file into a ConfigOverrides, defaulting every unset field to None."""
    return ConfigOverrides(**read_overrides_file(path))


def apply_overrides(config: AppConfig, overrides: ConfigOverrides) -> None:
    """Applies whatever overrides are set onto the already-built AppConfig, in place.

    rsrp/rsrq/sinr thresholds have no RAT distinction on the wire, so an override applies
    identically to both `run_status.lte` and `run_status.nr`.
    """
    if overrides.rsrp_threshold is not None:
        config.run_status.lte.min_rsrp = overrides.rsrp_threshold
        config.run_status.nr.min_rsrp = overrides.rsrp_threshold
    if overrides.rsrq_threshold is not None:
        config.run_status.lte.min_rsrq = overrides.rsrq_threshold
        config.run_status.nr.min_rsrq = overrides.rsrq_threshold
    if overrides.sinr_threshold is not None:
        config.run_status.lte.min_sinr = overrides.sinr_threshold
        config.run_status.nr.min_sinr = overrides.sinr_threshold
    if overrides.idle_threshold_s is not None:
        config.collector.max_idle_time = overrides.idle_threshold_s
    if overrides.movement_gate_poll_interval_s is not None:
        config.movement_gate.poll_interval_s = overrides.movement_gate_poll_interval_s
    if overrides.movement_gate_movement_threshold is not None:
        config.movement_gate.movement_threshold = overrides.movement_gate_movement_threshold
    if overrides.movement_gate_confirmations_required is not None:
        config.movement_gate.confirmations_required = overrides.movement_gate_confirmations_required
    if overrides.latency_test_baseline_interval_s is not None:
        config.latency_test.baseline_interval_s = overrides.latency_test_baseline_interval_s
    if overrides.latency_test_load_interval_s is not None:
        config.latency_test.load_interval_s = overrides.latency_test_load_interval_s
    if overrides.latency_test_poll_interval_s is not None:
        config.latency_test.poll_interval_s = overrides.latency_test_poll_interval_s
    if overrides.heartbeat_interval_s is not None:
        config.heartbeat.interval_s = overrides.heartbeat_interval_s
    if overrides.collector_max_wait_for_first_fix is not None:
        config.collector.max_wait_for_first_fix = overrides.collector_max_wait_for_first_fix
    if overrides.run_status_empty_captures_until_pipeline_broken is not None:
        config.run_status.empty_captures_until_pipeline_broken = int(
            overrides.run_status_empty_captures_until_pipeline_broken
        )


LOCAL_CONFIG_FILENAME = "config.local.toml"

# Per-device identity/secrets a device operator sets by hand once at setup time - the local,
# static counterpart to config_overrides.toml's remotely-writable allow-list above (see decision
# record 0007). Deliberately allow-listed, not fully generic, same reasoning as
# ALLOWED_OVERRIDE_FIELDS: only fields excluded from remote override altogether belong here.
ALLOWED_LOCAL_FIELDS: dict[str, tuple[str, ...]] = {
    "backend": ("url", "device_key"),
    "heartbeat": ("hmac_secret", "device_id"),
    "device": ("device_id",),
    "quectel_cm": ("binary",),
    "selftest": ("results_url",),
    "gps_fix_test": ("status_url",),
    "latency_test": ("host",),
    "system": ("mode",),
}


def _normalized_local_field_names() -> frozenset[str]:
    return frozenset(
        f"{section}_{field}" for section, fields in ALLOWED_LOCAL_FIELDS.items() for field in fields
    )


# Enforced at import time, not just documented: config.local.toml's fields and
# config_overrides.toml's allow-list must never overlap, so an identity/secret field can never
# become settable from a (compromised) backend response. See decision record 0007.
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
