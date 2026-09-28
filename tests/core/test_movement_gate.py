from datetime import UTC, datetime

import pytest

from measurement_software.core.config import MovementGateConfig
from measurement_software.status.backend_status import GpsFixStatusTracker
from measurement_software.core.movement_gate import MovementGate
from measurement_software.core.run_phase import RunPhase, RunPhaseState
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, Position

HOME = Position(latitude=0.0, longitude=0.0)
# ~111m north of HOME - clears a 15m movement threshold but not a 200m home-departure threshold.
NEAR_HOME = Position(latitude=0.001, longitude=0.0)
# ~1.1km north of HOME - clears both.
FAR_FROM_HOME = Position(latitude=0.01, longitude=0.0)


def fix(position: Position) -> GNSSFix:
    return GNSSFix(position=position, num_satellites=8)


class FakeGNSSReceiver(GNSSReceiver):
    """Returns one scripted fix per read_fix() call, then None forever; records open/close/read calls.

    `datetimes`, if given, similarly scripts successive read_datetime() calls, then None forever.
    """

    def __init__(self, fixes: list[GNSSFix | None], datetimes: list[datetime | None] | None = None):
        self._fixes = list(fixes)
        self._index = 0
        self._datetimes = list(datetimes) if datetimes is not None else []
        self._datetime_index = 0
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def read_fix(self) -> GNSSFix | None:
        self.calls.append("read")
        if self._index < len(self._fixes):
            value = self._fixes[self._index]
            self._index += 1
            return value
        return None

    def read_datetime(self) -> datetime | None:
        self.calls.append("read_datetime")
        if self._datetime_index < len(self._datetimes):
            value = self._datetimes[self._datetime_index]
            self._datetime_index += 1
            return value
        return None


class BurstingFakeGNSSReceiver(GNSSReceiver):
    """Models a receiver reopened every poll whose read window restarts at the same phase of its
    NMEA report cycle each time - `non_gga_reads_per_poll` non-GGA sentences before the next
    poll's fix becomes readable. Used to reproduce issue #38's "GGA structurally missed" scenario,
    where a per-poll read cap smaller than the burst can never see a fix no matter how many times
    it polls - unlike `FakeGNSSReceiver`, whose scripted reads advance across polls regardless of
    where a poll's cap cuts off.
    """

    def __init__(self, non_gga_reads_per_poll: int, positions: list[Position]):
        self._non_gga_reads_per_poll = non_gga_reads_per_poll
        self._positions = list(positions)
        self._poll_index = 0
        self._reads_this_poll = 0
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")
        self._reads_this_poll = 0

    def close(self) -> None:
        self.calls.append("close")
        self._poll_index += 1

    def read_fix(self) -> GNSSFix | None:
        self.calls.append("read")
        self._reads_this_poll += 1
        if self._reads_this_poll <= self._non_gga_reads_per_poll:
            return None
        position = self._positions[min(self._poll_index, len(self._positions) - 1)]
        return fix(position)

    def read_datetime(self) -> datetime | None:
        self.calls.append("read_datetime")
        return None


class FakeClockSync:
    """Records try_sync() calls; pre-synced by default so unrelated tests need no scripted datetimes."""

    def __init__(self, synced: bool = True):
        self.synced = synced
        self.sync_calls: list[datetime] = []

    def try_sync(self, gnss_datetime: datetime) -> None:
        self.sync_calls.append(gnss_datetime)
        self.synced = True


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("measurement_software.core.movement_gate.time.sleep", sleeps.append)
    return sleeps


def gate(gnss_receiver, run_phase=None, clock_sync=None, gps_fix_status=None, **overrides) -> MovementGate:
    config = MovementGateConfig(**overrides)
    return MovementGate(
        gnss_receiver, config, run_phase or RunPhase(), clock_sync or FakeClockSync(),
        gps_fix_status or GpsFixStatusTracker(),
    )


class TestWaitForMovement:
    def test_sets_run_phase_to_waiting_for_gps_fix_before_any_fix_is_obtained(self, no_sleep):
        run_phase = RunPhase()
        receiver = FakeGNSSReceiver([None])

        with pytest.raises(RuntimeError, match="ran out of scripted fixes"):
            _run_bounded(gate(receiver, run_phase, confirmations_required=2), max_polls=1)

        assert run_phase.get() == RunPhaseState.WAITING_FOR_GPS_FIX.value

    def test_transitions_to_waiting_for_movement_once_a_fix_is_obtained(self, no_sleep):
        run_phase = RunPhase()
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)])

        gate(receiver, run_phase).wait_for_movement()

        assert run_phase.get() == RunPhaseState.WAITING_FOR_MOVEMENT.value

    def test_records_every_fix_read_into_the_shared_gps_fix_status(self, no_sleep):
        gps_fix_status = GpsFixStatusTracker()
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)])

        gate(receiver, gps_fix_status=gps_fix_status).wait_for_movement()

        status = gps_fix_status.status()
        assert status.has_fix is True
        assert status.num_satellites == 8

    def test_returns_once_movement_is_confirmed_for_required_consecutive_polls(self, no_sleep):
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)])

        gate(receiver, confirmations_required=2).wait_for_movement()

        # Poll 1 sets the reference, polls 2 and 3 both confirm - 2 consecutive confirmations.
        assert receiver.calls.count("read") == 3

    def test_does_not_return_after_a_single_confirmation_below_required_count(self, no_sleep):
        receiver = FakeGNSSReceiver([
            fix(HOME), fix(FAR_FROM_HOME), fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME),
        ])

        gate(receiver, confirmations_required=2).wait_for_movement()

        # A confirmation not immediately followed by another resets the streak; the gate only
        # returns once two *consecutive* polls confirm (polls 4 and 5 here).
        assert receiver.calls.count("read") == 5

    def test_a_single_reading_never_returns_when_two_confirmations_are_required(self, no_sleep):
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME)])

        with pytest.raises(RuntimeError, match="ran out of scripted fixes"):
            _run_bounded(gate(receiver, confirmations_required=2), max_polls=3)

    def test_falls_back_to_movement_only_when_home_position_is_unset(self, no_sleep):
        receiver = FakeGNSSReceiver([fix(HOME), fix(NEAR_HOME), fix(NEAR_HOME)])

        gate(receiver, confirmations_required=2, home_latitude=None, home_longitude=None).wait_for_movement()

        assert receiver.calls.count("read") == 3

    def test_requires_home_departure_when_home_position_is_configured(self, no_sleep):
        # NEAR_HOME clears the movement threshold but not home_departure_threshold - never confirms.
        receiver = FakeGNSSReceiver([fix(HOME), fix(NEAR_HOME), fix(NEAR_HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)])

        gate(
            receiver, confirmations_required=2,
            home_latitude=HOME.latitude, home_longitude=HOME.longitude,
            home_departure_threshold=200.0,
        ).wait_for_movement()

        assert receiver.calls.count("read") == 5

    def test_opens_and_closes_the_receiver_once_per_poll(self, no_sleep):
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)])

        gate(receiver, confirmations_required=2).wait_for_movement()

        assert receiver.calls == ["open", "read", "close"] * 3

    def test_finds_a_gga_fix_past_a_realistic_multi_constellation_burst_of_other_sentences(self, no_sleep):
        """Regression for issue #38: a real multi-constellation NMEA receiver (GPS + GLONASS +
        Galileo + BeiDou, say) emits a burst of GLL/GSA/GSV/RMC/VTG sentences - none of them GGA -
        every report cycle, well past the old cap of 10 reads per poll before GGA comes back
        around. Because the receiver is reopened on every poll, the read window restarts at the
        same phase of that cycle each time - so a poll whose cap is smaller than the burst
        structurally can never see a GGA sentence, no matter how many times it polls. Using
        `_run_bounded` (rather than asserting a read count) is the point: the old cap of 10 would
        never confirm movement here even after unboundedly many polls, so bounding polls and
        expecting normal completion is what actually distinguishes the two behaviors.
        """
        receiver = BurstingFakeGNSSReceiver(
            non_gga_reads_per_poll=25, positions=[HOME, FAR_FROM_HOME, FAR_FROM_HOME],
        )

        _run_bounded(gate(receiver, confirmations_required=2), max_polls=3)

    def test_retries_within_a_poll_when_a_read_returns_none(self, no_sleep):
        # First poll: two None reads (e.g. non-GGA sentences) before a usable fix.
        receiver = FakeGNSSReceiver([
            None, None, fix(HOME),
            fix(FAR_FROM_HOME), fix(FAR_FROM_HOME),
        ])

        gate(receiver, confirmations_required=2).wait_for_movement()

        assert receiver.calls == (
            ["open", "read", "read", "read", "close"]
            + ["open", "read", "close"]
            + ["open", "read", "close"]
        )

    def test_sleeps_the_configured_poll_interval_between_polls(self, no_sleep):
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)])

        gate(receiver, confirmations_required=2, poll_interval_s=45.0).wait_for_movement()

        # No sleep after the poll that confirms movement and returns - only between polls.
        assert no_sleep == [45.0, 45.0]

    def test_never_terminates_on_reads_that_all_time_out_within_a_poll(self, no_sleep):
        receiver = FakeGNSSReceiver([None] * 30)

        with pytest.raises(RuntimeError, match="ran out of scripted fixes"):
            _run_bounded(gate(receiver, confirmations_required=2), max_polls=2)


class TestClockSyncIntegration:
    def test_syncs_the_clock_from_the_first_datetime_reading(self, no_sleep):
        dt = datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC)
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)], datetimes=[dt])
        clock_sync = FakeClockSync(synced=False)

        gate(receiver, clock_sync=clock_sync, confirmations_required=2).wait_for_movement()

        assert clock_sync.sync_calls == [dt]

    def test_reads_a_datetime_on_every_poll_until_synced(self, no_sleep):
        # No datetimes scripted - clock_sync never syncs, so every poll keeps trying.
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)])
        clock_sync = FakeClockSync(synced=False)

        gate(receiver, clock_sync=clock_sync, confirmations_required=2).wait_for_movement()

        assert receiver.calls == ["open", "read", "read_datetime", "close"] * 3

    def test_does_not_read_a_datetime_once_already_synced(self, no_sleep):
        receiver = FakeGNSSReceiver([fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)])
        clock_sync = FakeClockSync(synced=True)

        gate(receiver, clock_sync=clock_sync, confirmations_required=2).wait_for_movement()

        assert "read_datetime" not in receiver.calls

    def test_stops_reading_a_datetime_once_synced_mid_wait(self, no_sleep):
        dt = datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC)
        receiver = FakeGNSSReceiver(
            [fix(HOME), fix(FAR_FROM_HOME), fix(FAR_FROM_HOME)], datetimes=[dt, dt],
        )
        clock_sync = FakeClockSync(synced=False)

        gate(receiver, clock_sync=clock_sync, confirmations_required=2).wait_for_movement()

        # Synced on the first poll's datetime read - the second and third polls read no more.
        assert clock_sync.sync_calls == [dt]
        assert receiver.calls == (
            ["open", "read", "read_datetime", "close"]
            + ["open", "read", "close"]
            + ["open", "read", "close"]
        )


def _run_bounded(movement_gate: MovementGate, max_polls: int) -> None:
    """Runs wait_for_movement() but forces a RuntimeError once the fake receiver's script runs
    dry, so a test whose gate should never return does not spin forever on an infinite fake."""
    original_poll = movement_gate._poll_fix
    calls = {"n": 0}

    def bounded_poll():
        calls["n"] += 1
        if calls["n"] > max_polls:
            raise RuntimeError("ran out of scripted fixes")
        return original_poll()

    movement_gate._poll_fix = bounded_poll
    movement_gate.wait_for_movement()
