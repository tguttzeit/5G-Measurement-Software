import logging
import shutil
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from measurement_software.core.config import AppConfig, RunStatusConfig, StorageConfig
from measurement_software.core.config_sources import (
    ALLOWED_OVERRIDE_FIELDS,
    OVERRIDE_FIELD_TARGETS,
    resolve_section,
)
from measurement_software.gnss.gnss_receiver import GNSSFix, updated_gnss_diagnostic
from measurement_software.modems.modem import CellSample


class SampleQuality(StrEnum):
    """Whether a single cell measurement is a physically possible reading.

    RSRP, RSRQ and SINR each have a range the radio standard allows them to be reported in. If
    any one of them is missing or sits outside that range, the modem handed us a
    null/sentinel/garbage value and the sample is INVALID; otherwise it is VALID. No judgement
    about how strong or clean the signal is happens here - that belongs in analysis of the
    recorded data, not in a cutoff baked into the device.
    """

    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class LegalRanges:
    """Value ranges a measurement has to fall into to be a physically possible reading.

    Spec-derived (3GPP reporting ranges): a value outside these is a null/sentinel/garbage
    reading, not a poor signal.
    """

    rsrp: tuple[float, float]
    rsrq: tuple[float, float]
    sinr: tuple[float, float]


LTE_LEGAL_RANGES = LegalRanges(rsrp=(-140.0, -44.0), rsrq=(-19.5, 2.5), sinr=(-20.0, 30.0))
NR_LEGAL_RANGES = LegalRanges(rsrp=(-156.0, -31.0), rsrq=(-43.0, 20.0), sinr=(-23.0, 40.0))


@dataclass(frozen=True)
class RunStatus:
    """How a run is doing so far: how much of its data is usable, and whether it is producing any.

    `valid` and `invalid` count measurements by their SampleQuality (see there for what each
    means) and always add up to `datapoints_total`. `pipeline_broken` is a separate signal
    and says nothing about quality: it means measurements are not arriving even though the
    device is moving, which is a fault to go fix rather than a poor reading to record.
    """

    datapoints_total: int
    valid: int
    invalid: int
    pipeline_broken: bool

    def as_payload(self) -> dict:
        """Renders the status as the JSON body the backend receives.

        The counts are nested under a name saying what they cover, because they are totals
        for the whole run so far rather than for the interval since the last heartbeat.
        """
        return {
            "since_run_start": {
                "datapoints_total": self.datapoints_total,
                "valid": self.valid,
                "invalid": self.invalid,
            },
            "pipeline_broken": self.pipeline_broken,
        }


class RunStatusTracker:
    """Accumulates a run's data-quality counts and pipeline health as measurements come in.

    Counts are cumulative since the start of the run, so any single heartbeat reflects
    the whole run rather than one interval's worth of it. Reads happen from the heartbeat
    thread while the collector writes, hence the lock.
    """

    def __init__(self, config: RunStatusConfig):
        self._config = config
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(SampleQuality, 0)
        self._consecutive_empty_captures = 0

    def record_capture(self, samples: list[CellSample]) -> None:
        """Records one movement-triggered modem query and classifies whatever it returned."""
        with self._lock:
            for sample in samples:
                self._counts[classify_sample(sample)] += 1
            self._consecutive_empty_captures = 0 if samples else self._consecutive_empty_captures + 1

    def status(self) -> RunStatus:
        """Returns a consistent snapshot of the run so far."""
        with self._lock:
            return RunStatus(
                datapoints_total=sum(self._counts.values()),
                valid=self._counts[SampleQuality.VALID],
                invalid=self._counts[SampleQuality.INVALID],
                pipeline_broken=self._is_pipeline_broken(),
            )

    def _is_pipeline_broken(self) -> bool:
        """True once enough consecutive captures came back empty while the device kept moving.

        One empty modem response is ordinary noise; a streak of them means the vehicle is
        covering ground while nothing is being measured.
        """
        return self._consecutive_empty_captures >= self._config.empty_captures_until_pipeline_broken


def classify_sample(sample: CellSample) -> SampleQuality:
    """Classifies a cell measurement as valid, or invalid if any metric is an impossible reading."""
    legal_ranges = _legal_ranges_for(sample.rat)
    if (_within(sample.rsrp, legal_ranges.rsrp)
            and _within(sample.rsrq, legal_ranges.rsrq)
            and _within(sample.sinr, legal_ranges.sinr)):
        return SampleQuality.VALID
    return SampleQuality.INVALID


def _is_new_radio(rat: str) -> bool:
    return rat.upper().startswith("NR")


def _legal_ranges_for(rat: str) -> LegalRanges:
    return NR_LEGAL_RANGES if _is_new_radio(rat) else LTE_LEGAL_RANGES


def _within(value: float | None, legal_range: tuple[float, float]) -> bool:
    low, high = legal_range
    return value is not None and low <= value <= high


@dataclass(frozen=True)
class GpsFixStatus:
    """Whether a GPS fix has been obtained at all this run, and how stale the latest one is.

    All-`None`/`has_fix=False` before any fix has ever been read this run - not an error state,
    just "nothing to report yet" (e.g. still waiting for the very first fix).

    `diagnostic` is independent of fix status - a receiver can report a hardware fault (e.g. an
    antenna short) explaining why no fix can ever be acquired, regardless of location/sky
    visibility. `None` while no antenna fault is known - before any status was read, or after the
    receiver reported a healthy antenna again (e.g. a loose connector reseated). Always the
    already mapped, operator-friendly text (see updated_gnss_diagnostic()), never raw vendor text.
    """

    has_fix: bool
    num_satellites: int | None
    fix_age_s: float | None
    diagnostic: str | None = None

    def as_payload(self) -> dict:
        """Renders the status as the JSON body the backend receives."""
        return {
            "has_fix": self.has_fix,
            "num_satellites": self.num_satellites,
            "fix_age_s": self.fix_age_s,
            "diagnostic": self.diagnostic,
        }


class GpsFixStatusTracker:
    """Tracks the most recent GPS fix and diagnostic message read this run, for the heartbeat.

    `MovementGate` and `Collector` each read fixes (and, opportunistically, diagnostics) from the
    same `GNSSReceiver` at different stages of a run's lifecycle; sharing one tracker between them
    means the heartbeat always reports the freshest fix/diagnostic regardless of which stage is
    currently doing the reading.

    `record_fix()` is called from the main thread (`MovementGate`/`Collector`) while `status()` is
    read from `HeartbeatSender`'s background thread - the lock keeps a snapshot from ever pairing a
    fresh `_fix_time` with a stale `_num_satellites` (or vice versa), matching `RunStatusTracker`'s
    own lock for the same producer/consumer shape.

    Uses `time.monotonic()`, not `time.time()`, for `_fix_time` - `MovementGate` can trigger
    `ClockSync` to jump the wall clock (e.g. from a bogus near-epoch boot time to the real date) in
    the same polling loop that records fixes, which would otherwise make `fix_age_s` briefly read as
    years old (or negative) right after a clock jump.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._num_satellites: int | None = None
        self._fix_time: float | None = None
        self._diagnostic: str | None = None

    def record_fix(self, fix: GNSSFix) -> None:
        """Records a freshly-read fix as the most recent one seen this run."""
        with self._lock:
            self._num_satellites = fix.num_satellites
            self._fix_time = time.monotonic()

    def record_diagnostic(self, raw: str) -> None:
        """Updates the current diagnostic from a freshly-read raw GNSS diagnostic message.

        Maps it to its friendly form immediately (a healthy antenna status clears it), so status()/as_payload() never have to handle
        - and nothing downstream can ever accidentally show or transmit - the raw vendor text.
        """
        with self._lock:
            self._diagnostic = updated_gnss_diagnostic(self._diagnostic, raw)

    def status(self) -> GpsFixStatus:
        """Returns a consistent snapshot of the most recent fix, or the no-fix-yet state."""
        with self._lock:
            num_satellites = self._num_satellites
            fix_time = self._fix_time
            diagnostic = self._diagnostic
        if fix_time is None:
            return GpsFixStatus(has_fix=False, num_satellites=None, fix_age_s=None, diagnostic=diagnostic)
        return GpsFixStatus(
            has_fix=True,
            num_satellites=num_satellites,
            fix_age_s=time.monotonic() - fix_time,
            diagnostic=diagnostic,
        )


@dataclass(frozen=True)
class StorageStatus:
    """Snapshot of the upload backlog and free disk space for the upload directory's filesystem."""

    pending_files: int
    disk_free_bytes: int

    def as_payload(self) -> dict:
        """Renders the status as the JSON body the backend receives."""
        return {"pending_files": self.pending_files, "disk_free_bytes": self.disk_free_bytes}


class StorageStatusReporter:
    """Reads the current upload-backlog size and free disk space, warning loudly if space is low.

    Not a cap or eviction policy - free space is only ever reported and warned about here, never
    acted on.
    """

    def __init__(self, upload_dir: Path, config: StorageConfig):
        self._upload_dir = upload_dir
        self._config = config
        self._logger = logging.getLogger(__name__)

    def status(self) -> StorageStatus:
        """Returns the current backlog/free-space snapshot, logging a warning if space is low."""
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        pending_files = sum(1 for _ in self._upload_dir.glob("*.json"))
        disk_free_bytes = shutil.disk_usage(self._upload_dir).free

        if disk_free_bytes < self._config.low_free_space_warning_bytes:
            self._logger.warning(
                "Low disk space on upload_dir's filesystem: %d bytes free, below the %d byte "
                "warning threshold.", disk_free_bytes, self._config.low_free_space_warning_bytes,
            )

        return StorageStatus(pending_files=pending_files, disk_free_bytes=disk_free_bytes)


class ConfigStatusReporter:
    """Reports the config values actually in effect on this device right now.

    Keyed by the same field names as the backend's `ConfigOverrideCreate` allow-list (mirrored
    device-side in `core/config_sources.py`'s `ALLOWED_OVERRIDE_FIELDS`), so a maintainer can
    see the live value next to the override they could queue for it. Read straight off the
    already-loaded `AppConfig`, which already reflects any pending overrides
    merged in at boot - nothing here mutates, or is mutated by, a run in progress.
    """

    def __init__(self, config: AppConfig):
        self._config = config

    def as_payload(self) -> dict:
        """Renders the status as the JSON body the backend receives.

        One entry per override field, read from the live AppConfig - including `system_mode`,
        the mode the device is actually running rather than a pending override. Nothing outside
        the override allow-list is ever included, so identity/secret fields can't leak here.
        """
        return {field: self._live_value(field) for field in ALLOWED_OVERRIDE_FIELDS}

    def _live_value(self, field: str):
        targets = OVERRIDE_FIELD_TARGETS.get(field)
        if not targets:
            # day_end_threshold_s has no device-side behavior yet - there is no
            # live value to report for it.
            return None
        section_path, target_field = targets[0]
        return getattr(resolve_section(self._config, section_path), target_field)
