from unittest.mock import MagicMock

import pytest

from measurement_software.core.collector import Collector
from measurement_software.core.config import CollectorConfig, RunStatusConfig
from measurement_software.core.heartbeat_sender import HeartbeatSender
from measurement_software.core.run_status import RunStatusTracker
from measurement_software.gnss.gnss_receiver import GNSSReceiver, GNSSFix, Position
from measurement_software.modems.modem import Modem, CellSample


class FakeClock:
    """Controls `time.time()`/`time.sleep()` as seen by the collector module."""

    def __init__(self):
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeGNSSReceiver(GNSSReceiver):
    """Yields a scripted sequence of fixes, then repeats the last one forever.

    A scripted entry may be `None` to simulate a momentarily lost fix (e.g. a
    non-GGA sentence). Each `read_fix()` call advances the shared clock,
    simulating a GNSS receiver that produces one read per `seconds_per_read`.
    """

    def __init__(self, fixes: list[GNSSFix | None], clock: FakeClock, seconds_per_read: float = 1.0):
        self._fixes = fixes
        self._index = 0
        self._clock = clock
        self._seconds_per_read = seconds_per_read
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def read_fix(self) -> GNSSFix | None:
        self._clock.sleep(self._seconds_per_read)
        if not self._fixes:
            return None
        if self._index < len(self._fixes):
            fix = self._fixes[self._index]
            self._index += 1
            return fix
        return self._fixes[-1]


class FakeModem(Modem):
    def __init__(
        self,
        samples: list[CellSample] | None = None,
        raise_on_call: int | None = None,
        raise_exc: Exception | None = None,
    ):
        self._samples = samples if samples is not None else [CellSample(rat="LTE")]
        self._raise_on_call = raise_on_call
        self._raise_exc = raise_exc if raise_exc is not None else RuntimeError("modem failure")
        self.opened = False
        self.closed = False
        self.query_count = 0

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def query_cell_info(self) -> list[CellSample]:
        self.query_count += 1
        if self._raise_on_call == self.query_count:
            raise self._raise_exc
        return self._samples

    def power_down(self) -> None:
        pass


NORTH_5M = 0.00005  # ~5.6 m of latitude, below the default 15 m movement threshold
NORTH_1KM = 0.01     # ~1112 m of latitude, well above the default movement threshold


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake_clock = FakeClock()
    monkeypatch.setattr("measurement_software.core.collector.time.time", fake_clock.time)
    monkeypatch.setattr("measurement_software.core.collector.time.sleep", fake_clock.sleep)
    return fake_clock


@pytest.fixture
def run_status() -> RunStatusTracker:
    return RunStatusTracker(RunStatusConfig())


@pytest.fixture
def heartbeat() -> MagicMock:
    """Stands in for the backend heartbeat, which otherwise spawns a thread and posts to a server."""
    return MagicMock(spec=HeartbeatSender)


@pytest.fixture
def keep_alive_mock(monkeypatch) -> MagicMock:
    mock_cls = MagicMock()
    monkeypatch.setattr("measurement_software.core.collector.KeepModemAliveSender", mock_cls)
    return mock_cls.return_value


class TestHaversine:
    def test_same_point_is_zero(self):
        pos = Position(latitude=52.5, longitude=13.4)
        assert Collector.haversine(pos, pos) == 0.0

    def test_symmetric(self):
        a = Position(latitude=52.5, longitude=13.4)
        b = Position(latitude=52.6, longitude=13.5)
        assert Collector.haversine(a, b) == pytest.approx(Collector.haversine(b, a))

    def test_known_distance_one_degree_latitude(self):
        a = Position(latitude=0.0, longitude=0.0)
        b = Position(latitude=1.0, longitude=0.0)
        assert Collector.haversine(a, b) == pytest.approx(111194.93, abs=1.0)


class TestCollect:
    def test_returns_empty_list_when_no_first_fix(self, clock, keep_alive_mock, run_status, heartbeat):
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        modem = FakeModem()
        config = CollectorConfig(max_wait_for_first_fix=2.5, wait_log_interval=100)
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        result = collector.collect()

        assert result == []
        assert modem.query_count == 0
        assert modem.opened and modem.closed
        assert gnss.opened and gnss.closed
        keep_alive_mock.start.assert_called_once()
        keep_alive_mock.stop.assert_called_once()
        heartbeat.start.assert_called_once()
        heartbeat.stop.assert_called_once()

    def test_filters_small_movements_and_stops_after_idle_timeout(self, clock, keep_alive_mock, run_status, heartbeat):
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_a_nearby = Position(latitude=NORTH_5M, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)

        fixes = [
            GNSSFix(position=pos_a, num_satellites=8),
            GNSSFix(position=pos_a_nearby, num_satellites=8),
            GNSSFix(position=pos_b, num_satellites=8),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(
            position_threshold=15,
            max_idle_time=3.0,
            max_wait_for_first_fix=1000,
            wait_log_interval=1000,
        )
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        result = collector.collect()

        # Only the two genuine movements (A, then B) should have produced datapoints;
        # the nearby fix and the repeated B fixes while idle are filtered out.
        assert [dp.fix.position for dp in result] == [pos_a, pos_b]
        assert modem.query_count == 2
        keep_alive_mock.start.assert_called_once()
        keep_alive_mock.stop.assert_called_once()
        heartbeat.start.assert_called_once()
        heartbeat.stop.assert_called_once()

    def test_datapoints_carry_modem_samples_and_fix(self, clock, keep_alive_mock, run_status, heartbeat):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=6)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        samples = [CellSample(rat="LTE"), CellSample(rat="NR5G-SA")]
        modem = FakeModem(samples=samples)
        config = CollectorConfig(max_idle_time=1.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        result = collector.collect()

        assert len(result) == 2
        assert [dp.cell_sample for dp in result] == samples
        assert all(dp.fix == fix_a for dp in result)
        assert all(dp.timestamp.endswith("Z") for dp in result)

    def test_continues_past_intermittent_lost_fixes(self, clock, keep_alive_mock, run_status, heartbeat):
        # A momentarily loses its fix (e.g. a non-GGA sentence) twice in a row
        # before GNSS recovers and later reports a genuine movement to B.
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_a_nearby = Position(latitude=NORTH_5M, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)

        fixes = [
            GNSSFix(position=pos_a, num_satellites=8),
            None,
            None,
            GNSSFix(position=pos_a_nearby, num_satellites=8),
            GNSSFix(position=pos_b, num_satellites=8),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(
            position_threshold=15,
            max_idle_time=4.0,
            max_wait_for_first_fix=1000,
            wait_log_interval=1000,
        )
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        result = collector.collect()

        # The lost-fix reads must not produce datapoints or break the collection;
        # only the two genuine movements (A, then B) should be captured.
        assert [dp.fix.position for dp in result] == [pos_a, pos_b]
        assert modem.query_count == 2

    def test_records_every_capture_in_the_run_status(self, clock, keep_alive_mock, run_status, heartbeat):
        fixes = [
            GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=8),
            GNSSFix(position=Position(latitude=NORTH_5M, longitude=0.0), num_satellites=8),
            GNSSFix(position=Position(latitude=NORTH_1KM, longitude=0.0), num_satellites=8),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0)])
        config = CollectorConfig(max_idle_time=3.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        collector.collect()

        # Only the two genuine movements are captured, so only those get classified.
        status = run_status.status()
        assert (status.datapoints_total, status.good, status.bad, status.invalid) == (2, 2, 0, 0)
        assert status.pipeline_broken is False

    def test_run_status_reports_a_broken_pipeline_when_the_modem_yields_nothing(
            self, clock, keep_alive_mock, run_status, heartbeat):
        moving_fixes = [
            GNSSFix(position=Position(latitude=NORTH_1KM * step, longitude=0.0), num_satellites=8)
            for step in range(4)
        ]
        gnss = FakeGNSSReceiver(fixes=moving_fixes, clock=clock)
        modem = FakeModem(samples=[])
        config = CollectorConfig(max_idle_time=3.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        assert collector.collect() == []

        status = run_status.status()
        assert status.datapoints_total == 0
        assert status.pipeline_broken is True

    def test_gps_disabled_captures_on_poll_interval_ignoring_movement_threshold(
            self, clock, keep_alive_mock, run_status, heartbeat):
        # Alternating between the same spot and a nearby one that's below the default movement
        # threshold: real movement-threshold gating would filter the "nearby" reads out entirely,
        # so capturing all of them proves the poll-interval path is used instead.
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_a_nearby = Position(latitude=NORTH_5M, longitude=0.0)
        fixes = [
            GNSSFix(position=pos_a, num_satellites=0, placeholder=True),
            GNSSFix(position=pos_a_nearby, num_satellites=0, placeholder=True),
            GNSSFix(position=pos_a, num_satellites=0, placeholder=True),
            GNSSFix(position=pos_a_nearby, num_satellites=0, placeholder=True),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock, seconds_per_read=0.0)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(
            gps_enabled=False,
            gps_disabled_poll_interval_s=2.0,
            max_idle_time=5.0,
            max_wait_for_first_fix=1000,
            wait_log_interval=1000,
        )
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        result = collector.collect()

        # Idle time (unaffected by GPS-disabled captures) bounds the run: 4 captures 2s apart
        # (t=0,2,4,6) before idle_duration (6s) clears max_idle_time (5s).
        assert [dp.fix.position for dp in result] == [pos_a, pos_a_nearby, pos_a, pos_a_nearby]
        assert all(dp.fix.placeholder for dp in result)
        assert modem.query_count == 4

    def test_gps_disabled_logs_a_startup_warning(self, clock, keep_alive_mock, run_status, heartbeat, caplog):
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        modem = FakeModem()
        config = CollectorConfig(gps_enabled=False, max_wait_for_first_fix=0, wait_log_interval=100)
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        with caplog.at_level("WARNING"):
            collector.collect()

        assert any("GPS disabled" in record.message for record in caplog.records)

    def test_cleans_up_and_propagates_error_when_modem_raises(self, clock, keep_alive_mock, run_status, heartbeat):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=8)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        modem = FakeModem(raise_on_call=1)
        config = CollectorConfig(max_idle_time=1.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat)

        with pytest.raises(RuntimeError, match="modem failure"):
            collector.collect()

        # The finally block must still run cleanup even though the loop body raised.
        assert modem.opened and modem.closed
        assert gnss.opened and gnss.closed
        keep_alive_mock.start.assert_called_once()
        keep_alive_mock.stop.assert_called_once()
        heartbeat.start.assert_called_once()
        heartbeat.stop.assert_called_once()
