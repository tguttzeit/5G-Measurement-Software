import time
from abc import ABC, abstractmethod
from typing import Callable

from measurement_software.core.config import CollectorConfig
from measurement_software.core.util import haversine_distance_m
from measurement_software.gnss.gnss_receiver import GNSSFix, Position


class CaptureGate(ABC):
    """Interface deciding whether a GNSS fix should trigger a modem query and captured datapoint.

    Distinct from, and decoupled from, `Collector`'s own genuine-movement tracking: this only answers "should a datapoint be captured for this fix", never whether
    the vehicle has actually moved. Keeping the two separate means a gate that captures on a fixed
    cadence rather than on distance can never affect idle-timeout detection, which has to keep
    reflecting real movement.
    """

    @abstractmethod
    def should_capture(self, fix: GNSSFix) -> bool:
        """Returns True if this fix should be captured as a new datapoint."""
        ...


class DistanceCaptureGate(CaptureGate):
    """Captures whenever the fix has moved at least `position_threshold` meters since the last capture."""

    def __init__(self, position_threshold: float):
        self._position_threshold = position_threshold
        self._last_pos: Position | None = None

    def should_capture(self, fix: GNSSFix) -> bool:
        if self._last_pos is None:
            self._last_pos = fix.position
            return True

        if haversine_distance_m(self._last_pos, fix.position) < self._position_threshold:
            return False

        self._last_pos = fix.position
        return True


class TimeCaptureGate(CaptureGate):
    """Captures at a fixed cadence (`capture_interval_s`), regardless of distance moved."""

    def __init__(self, capture_interval_s: float):
        self._capture_interval_s = capture_interval_s
        self._last_capture_time: float | None = None

    def should_capture(self, fix: GNSSFix) -> bool:
        now = time.time()
        if self._last_capture_time is not None and (now - self._last_capture_time) < self._capture_interval_s:
            return False

        self._last_capture_time = now
        return True


_CAPTURE_GATE_FACTORIES: dict[str, Callable[[CollectorConfig], CaptureGate]] = {
    "distance": lambda config: DistanceCaptureGate(config.position_threshold),
    "time": lambda config: TimeCaptureGate(config.capture_interval_s),
}


def create_capture_gate(config: CollectorConfig) -> CaptureGate:
    """Builds the CaptureGate implementation configured for config.capture_gate."""
    try:
        factory = _CAPTURE_GATE_FACTORIES[config.capture_gate]
    except KeyError:
        raise ValueError(f"Unknown capture gate: {config.capture_gate!r}")
    return factory(config)
