import pytest

from measurement_software.core.capture_gate import (
    DistanceCaptureGate,
    TimeCaptureGate,
    create_capture_gate,
)
from measurement_software.core.config import CollectorConfig
from measurement_software.gnss.gnss_receiver import GNSSFix, Position

NORTH_5M = 0.00005  # ~5.6 m of latitude, below the default 15 m movement threshold
NORTH_1KM = 0.01     # ~1112 m of latitude, well above the default movement threshold


def fix_at(latitude: float) -> GNSSFix:
    return GNSSFix(position=Position(latitude=latitude, longitude=0.0), num_satellites=8)


class TestDistanceCaptureGate:
    def test_captures_the_first_fix_unconditionally(self):
        gate = DistanceCaptureGate(position_threshold=15)

        assert gate.should_capture(fix_at(0.0)) is True

    def test_does_not_capture_a_fix_below_the_threshold(self):
        gate = DistanceCaptureGate(position_threshold=15)
        gate.should_capture(fix_at(0.0))

        assert gate.should_capture(fix_at(NORTH_5M)) is False

    def test_captures_a_fix_at_or_above_the_threshold(self):
        gate = DistanceCaptureGate(position_threshold=15)
        gate.should_capture(fix_at(0.0))

        assert gate.should_capture(fix_at(NORTH_1KM)) is True

    def test_tracks_distance_from_the_last_captured_fix_not_the_last_seen_fix(self):
        gate = DistanceCaptureGate(position_threshold=15)
        gate.should_capture(fix_at(0.0))
        assert gate.should_capture(fix_at(NORTH_5M)) is False

        # Still measured from the original capture, not from the rejected nearby fix.
        assert gate.should_capture(fix_at(NORTH_5M)) is False


class TestTimeCaptureGate:
    def test_captures_the_first_fix_unconditionally(self, monkeypatch):
        monkeypatch.setattr("measurement_software.core.capture_gate.time.time", lambda: 100.0)
        gate = TimeCaptureGate(capture_interval_s=10.0)

        assert gate.should_capture(fix_at(0.0)) is True

    def test_does_not_capture_again_before_the_interval_elapses(self, monkeypatch):
        now = [100.0]
        monkeypatch.setattr("measurement_software.core.capture_gate.time.time", lambda: now[0])
        gate = TimeCaptureGate(capture_interval_s=10.0)
        gate.should_capture(fix_at(0.0))

        now[0] += 5.0
        assert gate.should_capture(fix_at(0.0)) is False

    def test_captures_again_once_the_interval_elapses(self, monkeypatch):
        now = [100.0]
        monkeypatch.setattr("measurement_software.core.capture_gate.time.time", lambda: now[0])
        gate = TimeCaptureGate(capture_interval_s=10.0)
        gate.should_capture(fix_at(0.0))

        now[0] += 10.0
        assert gate.should_capture(fix_at(0.0)) is True

    def test_captures_regardless_of_distance_moved(self, monkeypatch):
        now = [100.0]
        monkeypatch.setattr("measurement_software.core.capture_gate.time.time", lambda: now[0])
        gate = TimeCaptureGate(capture_interval_s=10.0)
        gate.should_capture(fix_at(0.0))

        now[0] += 10.0
        # Fix barely moved at all, well below any distance threshold - still captures on cadence.
        assert gate.should_capture(fix_at(NORTH_5M)) is True


class TestCreateCaptureGate:
    def test_builds_a_distance_gate_by_default(self):
        gate = create_capture_gate(CollectorConfig())

        assert isinstance(gate, DistanceCaptureGate)

    def test_builds_a_time_gate_when_configured(self):
        gate = create_capture_gate(CollectorConfig(capture_gate="time"))

        assert isinstance(gate, TimeCaptureGate)

    def test_raises_on_an_unknown_capture_gate_type(self):
        with pytest.raises(ValueError, match="unknown_gate"):
            create_capture_gate(CollectorConfig(capture_gate="unknown_gate"))
