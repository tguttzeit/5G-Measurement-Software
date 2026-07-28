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
