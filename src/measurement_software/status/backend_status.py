import logging
import shutil
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from measurement_software.core.config import AppConfig, QualityThresholds, RunStatusConfig, StorageConfig
from measurement_software.core.system import FanController
from measurement_software.gnss.gnss_receiver import GNSSFix
from measurement_software.modems.modem import CellSample


class SampleQuality(StrEnum):
    """How a single cell measurement was classified, by two checks asked in order.

    First: is the reading physically possible at all? RSRP, RSRQ and SINR each have a range
    the radio standard allows them to be reported in. If any one of them is missing or sits
    outside that range, the modem handed us a null/sentinel/garbage value, and the sample is
    INVALID — no judgement about signal strength has been made yet.

    Second, for a reading that is possible: is it actually good enough to be useful? Every
    one of the three has to clear its configured cutoff for the sample to be GOOD. If even
    one falls short while the others are fine, the sample is BAD — weak signal, a congested
    cell and a noisy link each make a measurement unhelpful on their own, so two good metrics
    do not outvote one poor one.
    """

    GOOD = "good"
    BAD = "bad"
    INVALID = "invalid"


@dataclass(frozen=True)
class LegalRanges:
    """Value ranges a measurement has to fall into to be a physically possible reading.

    Spec-derived (3GPP reporting ranges), unlike the configured quality thresholds:
    a value outside these is a null/sentinel/garbage reading, not a poor signal.
    """

    rsrp: tuple[float, float]
    rsrq: tuple[float, float]
    sinr: tuple[float, float]


LTE_LEGAL_RANGES = LegalRanges(rsrp=(-140.0, -44.0), rsrq=(-19.5, 2.5), sinr=(-20.0, 30.0))
NR_LEGAL_RANGES = LegalRanges(rsrp=(-156.0, -31.0), rsrq=(-43.0, 20.0), sinr=(-23.0, 40.0))


@dataclass(frozen=True)
class RunStatus:
    """How a run is doing so far: how good its data is, and whether it is producing any at all.

    `good`, `bad` and `invalid` count measurements by their SampleQuality (see there for what
    each means) and always add up to `datapoints_total`. `pipeline_broken` is a separate signal
    and says nothing about quality: it means measurements are not arriving even though the
    device is moving, which is a fault to go fix rather than a poor reading to record.
    """

    datapoints_total: int
    good: int
    bad: int
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
                "good": self.good,
                "bad": self.bad,
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
                self._counts[classify_sample(sample, self._config)] += 1
            self._consecutive_empty_captures = 0 if samples else self._consecutive_empty_captures + 1

    def status(self) -> RunStatus:
        """Returns a consistent snapshot of the run so far."""
        with self._lock:
            return RunStatus(
                datapoints_total=sum(self._counts.values()),
                good=self._counts[SampleQuality.GOOD],
                bad=self._counts[SampleQuality.BAD],
                invalid=self._counts[SampleQuality.INVALID],
                pipeline_broken=self._is_pipeline_broken(),
            )

    def _is_pipeline_broken(self) -> bool:
        """True once enough consecutive captures came back empty while the device kept moving.

        One empty modem response is ordinary noise; a streak of them means the vehicle is
        covering ground while nothing is being measured.
        """
        return self._consecutive_empty_captures >= self._config.empty_captures_until_pipeline_broken


def classify_sample(sample: CellSample, config: RunStatusConfig) -> SampleQuality:
    """Classifies a cell measurement as invalid (impossible reading), good, or bad.

    A sample is good only if RSRP, RSRQ and SINR all clear their RAT's configured
    threshold — any one of them falling short makes the sample bad.
    """
    legal_ranges = _legal_ranges_for(sample.rat)
    if not (_within(sample.rsrp, legal_ranges.rsrp)
            and _within(sample.rsrq, legal_ranges.rsrq)
            and _within(sample.sinr, legal_ranges.sinr)):
        return SampleQuality.INVALID

    thresholds = _thresholds_for(sample.rat, config)
    if (sample.rsrp >= thresholds.min_rsrp
            and sample.rsrq >= thresholds.min_rsrq
            and sample.sinr >= thresholds.min_sinr):
        return SampleQuality.GOOD
    return SampleQuality.BAD


def _is_new_radio(rat: str) -> bool:
    return rat.upper().startswith("NR")


def _legal_ranges_for(rat: str) -> LegalRanges:
    return NR_LEGAL_RANGES if _is_new_radio(rat) else LTE_LEGAL_RANGES


def _thresholds_for(rat: str, config: RunStatusConfig) -> QualityThresholds:
    return config.nr if _is_new_radio(rat) else config.lte


def _within(value: float | None, legal_range: tuple[float, float]) -> bool:
    low, high = legal_range
    return value is not None and low <= value <= high


@dataclass(frozen=True)
class GpsFixStatus:
    """Whether a GPS fix has been obtained at all this run, and how stale the latest one is.

    All-`None`/`has_fix=False` before any fix has ever been read this run - not an error state,
    just "nothing to report yet" (e.g. still waiting for the very first fix).
    """

    has_fix: bool
    num_satellites: int | None
    fix_age_s: float | None

    def as_payload(self) -> dict:
        """Renders the status as the JSON body the backend receives."""
        return {
            "has_fix": self.has_fix,
            "num_satellites": self.num_satellites,
            "fix_age_s": self.fix_age_s,
        }


class GpsFixStatusTracker:
    """Tracks the most recent GPS fix read this run, for reporting in the heartbeat.

    `MovementGate` and `Collector` each read fixes from the same `GNSSReceiver` at different
    stages of a run's lifecycle; sharing one tracker between them means the heartbeat always
    reports the freshest fix regardless of which stage is currently doing the reading.

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

    def record_fix(self, fix: GNSSFix) -> None:
        """Records a freshly-read fix as the most recent one seen this run."""
        with self._lock:
            self._num_satellites = fix.num_satellites
            self._fix_time = time.monotonic()

    def status(self) -> GpsFixStatus:
        """Returns a consistent snapshot of the most recent fix, or the no-fix-yet state."""
        with self._lock:
            num_satellites = self._num_satellites
            fix_time = self._fix_time
        if fix_time is None:
            return GpsFixStatus(has_fix=False, num_satellites=None, fix_age_s=None)
        return GpsFixStatus(
            has_fix=True,
            num_satellites=num_satellites,
            fix_age_s=time.monotonic() - fix_time,
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

    Not a cap or eviction policy (see decision record 0009) - free space is only ever reported
    and warned about here, never acted on.
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
    """Reports the config values and fan mode actually in effect on this device right now.

    Keyed by the same field names as the backend's `ConfigOverrideCreate` allow-list (mirrored
    device-side in `core/config_sources.py`'s `ALLOWED_OVERRIDE_FIELDS`), so a maintainer can
    see the live value next to the override they could queue for it. Read straight off the
    already-loaded `AppConfig`, which per decision 0019 already reflects any pending overrides
    merged in at boot - nothing here mutates, or is mutated by, a run in progress.
    """

    def __init__(self, config: AppConfig, fan_controller: FanController):
        self._config = config
        self._fan_controller = fan_controller

    def as_payload(self) -> dict:
        """Renders the status as the JSON body the backend receives."""
        return {
            "rsrp_threshold": self._config.run_status.lte.min_rsrp,
            "rsrq_threshold": self._config.run_status.lte.min_rsrq,
            "sinr_threshold": self._config.run_status.lte.min_sinr,
            "idle_threshold_s": self._config.collector.max_idle_time,
            # day_end_threshold_s has no device-side behavior yet (decision 0019) - there is no
            # live value to report for it.
            "day_end_threshold_s": None,
            "movement_gate_poll_interval_s": self._config.movement_gate.poll_interval_s,
            "latency_test_baseline_interval_s": self._config.latency_test.baseline_interval_s,
            "latency_test_load_interval_s": self._config.latency_test.load_interval_s,
            "latency_test_poll_interval_s": self._config.latency_test.poll_interval_s,
            "heartbeat_interval_s": self._config.heartbeat.interval_s,
            "collector_max_wait_for_first_fix": self._config.collector.max_wait_for_first_fix,
            "movement_gate_movement_threshold": self._config.movement_gate.movement_threshold,
            "movement_gate_confirmations_required": self._config.movement_gate.confirmations_required,
            "run_status_empty_captures_until_pipeline_broken":
                self._config.run_status.empty_captures_until_pipeline_broken,
            "fan_mode": self._fan_controller.current_mode(),
        }
