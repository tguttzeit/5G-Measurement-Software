import pytest

from measurement_software.core.movement_tracker import MovementTracker
from measurement_software.gnss.gnss_receiver import GNSSFix, Position


class FakeClock:
    """Controls `time.time()` as seen by the movement tracker."""

    def __init__(self):
        self.now = 0.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake_clock = FakeClock()
    monkeypatch.setattr("measurement_software.core.movement_tracker.time.time", fake_clock.time)
    return fake_clock


def fix(latitude: float = 1.0) -> GNSSFix:
    return GNSSFix(position=Position(latitude=latitude, longitude=2.0), num_satellites=8)


class TestLatest:
    def test_reports_nothing_before_the_vehicle_has_moved(self, clock):
        assert MovementTracker().latest() is None

    def test_reports_the_position_and_time_of_the_last_movement(self, clock):
        tracker = MovementTracker()
        clock.now = 120.0

        tracker.record_movement(fix(latitude=52.5))

        movement = tracker.latest()
        assert movement.fix.position.latitude == 52.5
        assert movement.at == 120.0

    def test_a_later_movement_replaces_the_earlier_one(self, clock):
        tracker = MovementTracker()
        tracker.record_movement(fix(latitude=1.0))
        clock.now = 30.0
        tracker.record_movement(fix(latitude=2.0))

        movement = tracker.latest()
        assert movement.fix.position.latitude == 2.0
        assert movement.at == 30.0


class TestMovedWithin:
    def test_reports_nothing_before_the_vehicle_has_moved(self, clock):
        assert MovementTracker().moved_within(30.0) is None

    def test_reports_the_movement_while_it_is_still_inside_the_window(self, clock):
        tracker = MovementTracker()
        tracker.record_movement(fix(latitude=52.5))
        clock.now = 30.0

        movement = tracker.moved_within(30.0)

        assert movement.fix.position.latitude == 52.5

    def test_reports_nothing_once_the_last_movement_falls_outside_the_window(self, clock):
        tracker = MovementTracker()
        tracker.record_movement(fix())
        clock.now = 30.1

        assert tracker.moved_within(30.0) is None

    def test_a_new_movement_makes_a_stopped_vehicle_count_as_moving_again(self, clock):
        tracker = MovementTracker()
        tracker.record_movement(fix())
        clock.now = 100.0
        assert tracker.moved_within(30.0) is None

        tracker.record_movement(fix(latitude=3.0))

        assert tracker.moved_within(30.0).fix.position.latitude == 3.0
