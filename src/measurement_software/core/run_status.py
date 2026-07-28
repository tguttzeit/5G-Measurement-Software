import threading
from dataclasses import dataclass
from enum import StrEnum

from measurement_software.core.config import QualityThresholds, RunStatusConfig
from measurement_software.modems.modem import CellSample


class SampleQuality(StrEnum):
    """How a single cell measurement was classified on-device."""

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
    """How a run is doing so far: how good its data is, and whether it is producing any at all."""

    datapoints_total: int
    good: int
    bad: int
    invalid: int
    pipeline_broken: bool

    def as_payload(self) -> dict:
        """Renders the status as the heartbeat body defined in decision record 0001."""
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
        """True once the device has moved repeatedly without the modem yielding anything."""
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
