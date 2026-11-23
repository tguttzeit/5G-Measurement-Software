from datetime import UTC, datetime

import pytest

from measurement_software.core import first_fix_wait
from measurement_software.core.collector import RemoteRebootRequested, RemoteShutdownRequested, StopMeasuringRequested
from measurement_software.core.first_fix_wait import FirstFixWaiter
from measurement_software.core.run_phase import RunPhase, RunPhaseState
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, Position
from measurement_software.status.backend_status import GpsFixStatusTracker

FIX = GNSSFix(position=Position(latitude=48.0, longitude=11.0), num_satellites=8)
GNSS_DATETIME = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


class FakeGNSSReceiver(GNSSReceiver):
    """Returns one scripted fix per read_fix() call (None once the script runs out), likewise
    one scripted datetime per read_datetime() call, and records open/close calls."""

    def __init__(self, fixes, datetimes=None, diagnostics=None):
        self._fixes = list(fixes)
        self._datetimes = list(datetimes or [])
        self._diagnostics = list(diagnostics or [])
        self.opened = 0
        self.closed = 0
        self.fix_reads = 0
        self.datetime_reads = 0

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def read_fix(self):
        self.fix_reads += 1
        return self._fixes.pop(0) if self._fixes else None

    def read_datetime(self):
        self.datetime_reads += 1
        return self._datetimes.pop(0) if self._datetimes else None

    def read_gnss_diagnostic(self):
        return self._diagnostics.pop(0) if self._diagnostics else None


class FakeClockSync:
    def __init__(self):
        self.synced = False
        self.attempts = []

    def try_sync(self, gnss_datetime):
        self.attempts.append(gnss_datetime)
        self.synced = True


class FakeClock:
    """Advances one second per sleep-free loop iteration via time.time() calls."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        self.now += 1.0
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(first_fix_wait.time, "time", fake.time)
    return fake


def make_waiter(receiver, run_phase=None, clock_sync=None, gps_fix_status=None, wait_log_interval=1000.0):
    return FirstFixWaiter(
        receiver,
        wait_log_interval,
        run_phase or RunPhase(),
        clock_sync or FakeClockSync(),
        gps_fix_status or GpsFixStatusTracker(),
    )


def no_commands():
    pass


class TestWait:
    def test_returns_the_first_fix_and_reports_waiting_for_gps_fix(self, clock):
        run_phase = RunPhase()
        phases = []
        receiver = FakeGNSSReceiver([None, None, FIX])
        waiter = make_waiter(receiver, run_phase=run_phase)

        def check():
            phases.append(run_phase.get())

        fix = waiter.wait(check, timeout_s=None)

        assert fix == FIX
        assert phases == [RunPhaseState.WAITING_FOR_GPS_FIX] * 3
        assert receiver.fix_reads == 3

    def test_opens_the_receiver_for_the_wait_and_always_closes_it_again(self, clock):
        receiver = FakeGNSSReceiver([None, FIX])

        make_waiter(receiver).wait(no_commands, timeout_s=None)

        assert (receiver.opened, receiver.closed) == (1, 1)

    def test_closes_the_receiver_when_a_command_ends_the_wait(self, clock):
        receiver = FakeGNSSReceiver([])

        def stop():
            raise StopMeasuringRequested()

        with pytest.raises(StopMeasuringRequested):
            make_waiter(receiver).wait(stop, timeout_s=None)

        assert (receiver.opened, receiver.closed) == (1, 1)

    def test_records_every_fix_into_the_shared_gps_fix_status(self, clock):
        gps_fix_status = GpsFixStatusTracker()
        waiter = make_waiter(FakeGNSSReceiver([FIX]), gps_fix_status=gps_fix_status)

        waiter.wait(no_commands, timeout_s=None)

        assert gps_fix_status.status().has_fix is True

    def test_records_a_gnss_diagnostic_into_the_shared_gps_fix_status(self, clock):
        gps_fix_status = GpsFixStatusTracker()
        waiter = make_waiter(FakeGNSSReceiver([None, FIX], diagnostics=["ANTENNA SHORT"]),
                             gps_fix_status=gps_fix_status)

        waiter.wait(no_commands, timeout_s=None)

        assert gps_fix_status.status().diagnostic is not None

    def test_logs_the_wait_once_per_wait_log_interval(self, clock, caplog):
        receiver = FakeGNSSReceiver([None] * 5 + [FIX])
        waiter = make_waiter(receiver, wait_log_interval=2.5)

        with caplog.at_level("INFO"):
            waiter.wait(no_commands, timeout_s=None)

        waiting = [r for r in caplog.records if "Waiting for first GPS fix" in r.getMessage()]
        assert 1 < len(waiting) < 6


class TestTimeout:
    def test_returns_none_once_the_timeout_has_passed(self, clock):
        receiver = FakeGNSSReceiver([])

        fix = make_waiter(receiver).wait(no_commands, timeout_s=2.5)

        assert fix is None
        assert (receiver.opened, receiver.closed) == (1, 1)

    def test_a_fix_inside_the_timeout_wins(self, clock):
        receiver = FakeGNSSReceiver([None, FIX])

        assert make_waiter(receiver).wait(no_commands, timeout_s=100) == FIX

    def test_without_a_timeout_it_keeps_waiting_however_long_it_takes(self, clock):
        receiver = FakeGNSSReceiver([None] * 5000 + [FIX])

        assert make_waiter(receiver).wait(no_commands, timeout_s=None) == FIX


class TestCommandsAreCheckedOnEveryIteration:
    @pytest.mark.parametrize(
        "exception",
        [RemoteShutdownRequested("cmd-1"), RemoteRebootRequested("cmd-2"), StopMeasuringRequested()],
    )
    def test_a_command_raising_from_the_check_ends_the_wait(self, clock, exception):
        receiver = FakeGNSSReceiver([])
        checks = []

        def check():
            checks.append(1)
            if len(checks) == 4:
                raise exception

        with pytest.raises(type(exception)):
            make_waiter(receiver).wait(check, timeout_s=None)

        assert len(checks) == 4
        assert receiver.fix_reads == 3

    def test_a_pending_command_wins_over_an_already_available_fix(self, clock):
        receiver = FakeGNSSReceiver([FIX])

        def stop():
            raise StopMeasuringRequested()

        with pytest.raises(StopMeasuringRequested):
            make_waiter(receiver).wait(stop, timeout_s=None)

        assert receiver.fix_reads == 0


class TestClockSync:
    def test_syncs_the_clock_from_the_gnss_datetime_once_a_fix_exists(self, clock):
        clock_sync = FakeClockSync()
        receiver = FakeGNSSReceiver([None, FIX], datetimes=[GNSS_DATETIME])

        make_waiter(receiver, clock_sync=clock_sync).wait(no_commands, timeout_s=None)

        assert clock_sync.attempts == [GNSS_DATETIME]

    def test_does_not_try_to_sync_before_there_is_a_fix(self, clock):
        clock_sync = FakeClockSync()
        receiver = FakeGNSSReceiver([None, None], datetimes=[GNSS_DATETIME])

        make_waiter(receiver, clock_sync=clock_sync).wait(no_commands, timeout_s=1.5)

        assert clock_sync.attempts == []
        assert receiver.datetime_reads == 0

    def test_keeps_reading_lines_for_a_datetime_until_one_arrives(self, clock):
        clock_sync = FakeClockSync()
        receiver = FakeGNSSReceiver([FIX], datetimes=[None, None, None, GNSS_DATETIME])

        make_waiter(receiver, clock_sync=clock_sync).wait(no_commands, timeout_s=None)

        assert clock_sync.attempts == [GNSS_DATETIME]
        assert receiver.datetime_reads == 4

    def test_gives_up_after_a_bounded_number_of_lines_without_a_datetime(self, clock):
        clock_sync = FakeClockSync()
        receiver = FakeGNSSReceiver([FIX])

        fix = make_waiter(receiver, clock_sync=clock_sync).wait(no_commands, timeout_s=None)

        assert fix == FIX
        assert clock_sync.attempts == []
        assert receiver.datetime_reads == first_fix_wait.MAX_READS_FOR_CLOCK_SYNC

    def test_does_not_read_for_a_datetime_once_the_clock_is_synced(self, clock):
        clock_sync = FakeClockSync()
        clock_sync.synced = True
        receiver = FakeGNSSReceiver([FIX], datetimes=[GNSS_DATETIME])

        make_waiter(receiver, clock_sync=clock_sync).wait(no_commands, timeout_s=None)

        assert receiver.datetime_reads == 0
        assert clock_sync.attempts == []
