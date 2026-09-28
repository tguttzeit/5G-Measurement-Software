import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from measurement_software.core.collector import Collector, RemoteRebootRequested, RemoteShutdownRequested
from measurement_software.core.config import CollectorConfig, DeviceConfig, RunStatusConfig
from measurement_software.status.backend_status import GpsFixStatusTracker
from measurement_software.remote.heartbeat import HeartbeatSender
from measurement_software.core.run_log import RunLog
from measurement_software.status.backend_status import RunStatusTracker
from measurement_software.gnss.gnss_receiver import GNSSReceiver, GNSSFix, Position
from measurement_software.modems.modem import CellSample, Modem, ModemConnectionError, SimPinRejectedError


def assert_valid_utc_timestamp(timestamp: str) -> None:
    """Asserts that the timestamp is a valid ISO 8601 UTC timestamp that can be parsed."""
    # Should be parseable by fromisoformat
    parsed = datetime.fromisoformat(timestamp)
    # Should be timezone-aware and in UTC
    assert parsed.tzinfo == timezone.utc
    # Should end with Z (RFC 3339 format)
    assert timestamp.endswith("Z")


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

    `diagnostics`, if given, similarly scripts successive read_gnss_diagnostic() calls, then
    None forever.
    """

    def __init__(
        self, fixes: list[GNSSFix | None], clock: FakeClock, seconds_per_read: float = 1.0,
        diagnostics: list[str | None] | None = None,
    ):
        self._fixes = fixes
        self._index = 0
        self._clock = clock
        self._seconds_per_read = seconds_per_read
        self._diagnostics = diagnostics if diagnostics is not None else []
        self._diagnostic_index = 0
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

    def read_gnss_diagnostic(self) -> str | None:
        if self._diagnostic_index < len(self._diagnostics):
            value = self._diagnostics[self._diagnostic_index]
            self._diagnostic_index += 1
            return value
        return None

    def read_datetime(self):
        return None


class FakeModem(Modem):
    """`reopen_errors`, if given, scripts every open() after the first: an Exception entry is
    raised instead of opening; once the list is used up, open() succeeds."""

    def __init__(
        self,
        samples: list[CellSample] | None = None,
        raise_on_call: int | None = None,
        raise_exc: Exception | None = None,
        reopen_errors: list[Exception | None] | None = None,
    ):
        self._samples = samples if samples is not None else [CellSample(rat="LTE")]
        self._raise_on_call = raise_on_call
        self._raise_exc = raise_exc if raise_exc is not None else RuntimeError("modem failure")
        self._reopen_errors = reopen_errors if reopen_errors is not None else []
        self.opened = False
        self.closed = False
        self.query_count = 0
        self.open_count = 0
        self.close_count = 0

    def open(self) -> None:
        self.open_count += 1
        if self.open_count > 1 and self._reopen_errors:
            error = self._reopen_errors.pop(0)
            if error is not None:
                raise error
        self.opened = True

    def close(self) -> None:
        self.close_count += 1
        self.closed = True

    def unlock_sim(self) -> None:
        pass

    def query_cell_info(self) -> list[CellSample]:
        self.query_count += 1
        if self._raise_on_call == self.query_count:
            raise self._raise_exc
        return self._samples

    def reset_radio(self) -> None:
        self.radio_resets = getattr(self, "radio_resets", 0) + 1

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
def device() -> DeviceConfig:
    return DeviceConfig(device_id="test-device", mission_type="ground")


@pytest.fixture
def heartbeat() -> MagicMock:
    """Stands in for the backend heartbeat, which otherwise spawns a thread and posts to a server."""
    return MagicMock(spec=HeartbeatSender)


@pytest.fixture
def run_log(tmp_path) -> RunLog:
    return RunLog(tmp_path / "queue")


def read_datapoints(run_log_path: Path) -> list[dict]:
    """Reads back what the run actually persisted, rather than what it held in memory."""
    return [json.loads(line) for line in run_log_path.read_text().splitlines()]


def positions(run_log_path: Path) -> list[Position]:
    return [
        Position(
            latitude=dp["fix"]["position"]["latitude"],
            longitude=dp["fix"]["position"]["longitude"],
            altitude=dp["fix"]["position"]["altitude"],
        )
        for dp in read_datapoints(run_log_path)
    ]


@pytest.fixture
def keep_alive_mock(monkeypatch) -> MagicMock:
    mock_cls = MagicMock()
    monkeypatch.setattr("measurement_software.core.collector.KeepModemAliveSender", mock_cls)
    return mock_cls.return_value


class TestRunId:
    def test_every_collector_generates_its_own_uuid4(self, keep_alive_mock, run_status, heartbeat, run_log, device):
        gnss = FakeGNSSReceiver(fixes=[], clock=None)
        first = Collector(FakeModem(), gnss, CollectorConfig(), run_status, heartbeat, run_log, device)
        second = Collector(FakeModem(), gnss, CollectorConfig(), run_status, heartbeat, run_log, device)

        assert uuid.UUID(first.run_id).version == 4
        assert first.run_id != second.run_id

    def test_uses_the_run_id_it_is_given(self, keep_alive_mock, run_status, heartbeat, run_log, device):
        gnss = FakeGNSSReceiver(fixes=[], clock=None)
        collector = Collector(FakeModem(), gnss, CollectorConfig(), run_status, heartbeat, run_log, device,
                              run_id="given-run-id")

        assert collector.run_id == "given-run-id"

    def test_run_id_stays_the_same_for_the_whole_run(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fixes = [
            GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=8),
            GNSSFix(position=Position(latitude=NORTH_1KM, longitude=0.0), num_satellites=8),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        config = CollectorConfig(position_threshold=15, max_idle_time=3.0, max_wait_for_first_fix=1000,
                                 wait_log_interval=1000)
        collector = Collector(FakeModem(samples=[CellSample(rat="LTE")]), gnss, config, run_status, heartbeat,
                              run_log, device)

        recorded = read_datapoints(collector.collect())

        assert len(recorded) == 2
        assert {dp["run_id"] for dp in recorded} == {collector.run_id}


class TestCollect:
    def test_records_nothing_when_no_first_fix(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        modem = FakeModem()
        config = CollectorConfig(max_wait_for_first_fix=2.5, wait_log_interval=100)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        run_log_path = collector.collect()

        assert read_datapoints(run_log_path) == []
        assert modem.query_count == 0
        assert modem.opened and modem.closed
        assert gnss.opened and gnss.closed
        keep_alive_mock.start.assert_called_once()
        keep_alive_mock.stop.assert_called_once()
        heartbeat.start.assert_called_once()
        heartbeat.stop.assert_called_once()

    def test_filters_small_movements_and_stops_after_idle_timeout(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
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
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        run_log_path = collector.collect()

        # Only the two genuine movements (A, then B) should have produced datapoints;
        # the nearby fix and the repeated B fixes while idle are filtered out.
        assert positions(run_log_path) == [pos_a, pos_b]
        assert modem.query_count == 2
        keep_alive_mock.start.assert_called_once()
        keep_alive_mock.stop.assert_called_once()
        heartbeat.start.assert_called_once()
        heartbeat.stop.assert_called_once()

    def test_zero_max_idle_time_never_ends_the_run_on_idle(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=8)
        stop_after_s = 100_000

        class StoppingGNSSReceiver(FakeGNSSReceiver):
            def read_fix(self):
                if clock.time() > stop_after_s:
                    collector.request_stop_measuring()
                return super().read_fix()

        gnss = StoppingGNSSReceiver(fixes=[fix_a], clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(max_idle_time=0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        run_log_path = collector.collect()

        assert positions(run_log_path) == [pos_a]
        assert clock.time() > stop_after_s

    def test_datapoints_carry_modem_samples_and_fix(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=6)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        samples = [CellSample(rat="LTE"), CellSample(rat="NR5G-SA")]
        modem = FakeModem(samples=samples)
        config = CollectorConfig(max_idle_time=1.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        recorded = read_datapoints(collector.collect())

        assert len(recorded) == 2
        assert [dp["cell_sample"]["rat"] for dp in recorded] == [s.rat for s in samples]
        assert all(dp["fix"]["num_satellites"] == fix_a.num_satellites for dp in recorded)
        assert all(assert_valid_utc_timestamp(dp["timestamp"]) or True for dp in recorded)
        assert all(dp["device_id"] == "test-device" for dp in recorded)
        assert all(dp["mission_type"] == "ground" for dp in recorded)
        assert all(dp["run_id"] == collector.run_id for dp in recorded)

    def test_continues_past_intermittent_lost_fixes(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
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
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        run_log_path = collector.collect()

        # The lost-fix reads must not produce datapoints or break the collection;
        # only the two genuine movements (A, then B) should be captured.
        assert positions(run_log_path) == [pos_a, pos_b]
        assert modem.query_count == 2

    def test_records_every_capture_in_the_run_status(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fixes = [
            GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=8),
            GNSSFix(position=Position(latitude=NORTH_5M, longitude=0.0), num_satellites=8),
            GNSSFix(position=Position(latitude=NORTH_1KM, longitude=0.0), num_satellites=8),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0)])
        config = CollectorConfig(max_idle_time=3.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        collector.collect()

        # Only the two genuine movements are captured, so only those get classified.
        status = run_status.status()
        assert (status.datapoints_total, status.valid, status.invalid) == (2, 2, 0)
        assert status.pipeline_broken is False

    def test_run_status_reports_a_broken_pipeline_when_the_modem_yields_nothing(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        moving_fixes = [
            GNSSFix(position=Position(latitude=NORTH_1KM * step, longitude=0.0), num_satellites=8)
            for step in range(4)
        ]
        gnss = FakeGNSSReceiver(fixes=moving_fixes, clock=clock)
        modem = FakeModem(samples=[])
        config = CollectorConfig(max_idle_time=3.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        assert read_datapoints(collector.collect()) == []

        status = run_status.status()
        assert status.datapoints_total == 0
        assert status.pipeline_broken is True

    def test_gps_disabled_captures_on_poll_interval_ignoring_movement_threshold(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
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
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        run_log_path = collector.collect()

        # Idle time (unaffected by GPS-disabled captures) bounds the run: 4 captures 2s apart
        # (t=0,2,4,6) before idle_duration (6s) clears max_idle_time (5s).
        assert positions(run_log_path) == [pos_a, pos_a_nearby, pos_a, pos_a_nearby]
        assert all(dp["fix"]["placeholder"] for dp in read_datapoints(run_log_path))
        assert modem.query_count == 4

    def test_gps_disabled_logs_a_startup_warning(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device, caplog):
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        modem = FakeModem()
        config = CollectorConfig(gps_enabled=False, max_wait_for_first_fix=0, wait_log_interval=100)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        with caplog.at_level("WARNING"):
            collector.collect()

        assert any("GPS disabled" in record.message for record in caplog.records)

    def test_cleans_up_and_propagates_error_when_modem_raises(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=8)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        modem = FakeModem(raise_on_call=1)
        config = CollectorConfig(max_idle_time=1.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        with pytest.raises(RuntimeError, match="modem failure"):
            collector.collect()

        # The finally block must still run cleanup even though the loop body raised.
        assert modem.opened and modem.closed
        assert gnss.opened and gnss.closed
        keep_alive_mock.start.assert_called_once()
        keep_alive_mock.stop.assert_called_once()
        heartbeat.start.assert_called_once()
        heartbeat.stop.assert_called_once()


class TestRemoteCommands:
    def test_request_shutdown_stops_the_run_and_propagates(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=8)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(max_idle_time=1000, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector.request_shutdown(command_id=7)

        with pytest.raises(RemoteShutdownRequested) as exc_info:
            collector.collect()

        assert exc_info.value.command_id == 7
        # Cleanup still runs, same as any other exception unwinding collect().
        assert modem.opened and modem.closed
        assert gnss.opened and gnss.closed
        heartbeat.start.assert_called_once()
        heartbeat.stop.assert_called_once()

    def test_request_shutdown_is_honored_while_still_waiting_for_a_first_fix(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        modem = FakeModem()
        config = CollectorConfig(max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector.request_shutdown()

        with pytest.raises(RemoteShutdownRequested):
            collector.collect()

    def test_request_sim_reset_reconnects_the_modem_immediately_when_not_collecting(
            self, clock, run_status, heartbeat, run_log, device):
        modem = FakeModem()
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        config = CollectorConfig()
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        collector.request_sim_reset()

        # Acted on right away, from this thread - collect() was never even called, so nothing
        # else could possibly be using the modem at this point.
        assert modem.opened and modem.closed

    def test_request_sim_reset_calls_on_sim_reset_before_reconnecting_when_not_collecting(
            self, clock, run_status, heartbeat, run_log, device):
        modem = FakeModem()
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        config = CollectorConfig()
        calls = []
        collector = Collector(
            modem, gnss, config, run_status, heartbeat, run_log, device,
            on_sim_reset=lambda: calls.append(("on_sim_reset", modem.opened, modem.closed)),
        )

        collector.request_sim_reset()

        # Fired before the reconnect itself, so a caller showing a display message sees it
        # before the modem is actually touched.
        assert calls == [("on_sim_reset", False, False)]

    def test_request_sim_reset_defers_to_the_collection_thread_while_a_run_is_active(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)
        fixes = [GNSSFix(position=pos_a, num_satellites=8), GNSSFix(position=pos_b, num_satellites=8)]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(max_idle_time=1000, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector._collecting = True  # simulates a request arriving while collect() is running
        collector.request_sim_reset()

        # Not acted on yet - deferred to the collection thread's own next loop iteration.
        assert not modem.opened and not modem.closed

        run_log_path = collector.collect()

        # The modem was closed and reopened once for the reset, on top of its normal open/close.
        assert modem.opened and modem.closed
        assert positions(run_log_path) == [pos_a, pos_b]

    def test_sim_reset_calls_on_sim_reset_when_deferred_to_the_collection_thread(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)
        fixes = [GNSSFix(position=pos_a, num_satellites=8), GNSSFix(position=pos_b, num_satellites=8)]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(max_idle_time=1000, max_wait_for_first_fix=1000, wait_log_interval=1000)
        calls = []
        collector = Collector(
            modem, gnss, config, run_status, heartbeat, run_log, device,
            on_sim_reset=lambda: calls.append("on_sim_reset"),
        )
        collector._collecting = True
        collector.request_sim_reset()

        assert calls == []  # not fired yet - the reset itself hasn't run on this thread

        collector.collect()

        assert calls == ["on_sim_reset"]

    def test_sim_reset_does_not_stop_the_run(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=8)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(max_idle_time=1.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector._collecting = True
        collector.request_sim_reset()

        run_log_path = collector.collect()

        assert positions(run_log_path) == [pos_a]

    def test_request_reboot_stops_the_run_and_propagates(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=8)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(max_idle_time=1000, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector.request_reboot(command_id=8)

        with pytest.raises(RemoteRebootRequested) as exc_info:
            collector.collect()

        assert exc_info.value.command_id == 8
        # Cleanup still runs, same as any other exception unwinding collect().
        assert modem.opened and modem.closed
        assert gnss.opened and gnss.closed

    def test_request_reboot_is_honored_while_still_waiting_for_a_first_fix(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        modem = FakeModem()
        config = CollectorConfig(max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector.request_reboot()

        with pytest.raises(RemoteRebootRequested):
            collector.collect()

    def test_request_stop_measuring_ends_the_run_without_raising(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=8)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(max_idle_time=1000, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector.request_stop_measuring()

        run_log_path = collector.collect()

        # The run ends like a normal idle timeout - no exception, cleanup still runs.
        assert run_log_path is not None
        assert modem.opened and modem.closed
        assert gnss.opened and gnss.closed

    def test_request_stop_measuring_is_honored_while_still_waiting_for_a_first_fix(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        modem = FakeModem()
        config = CollectorConfig(max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector.request_stop_measuring()

        run_log_path = collector.collect()

        assert run_log_path is not None

    def test_stop_measuring_flushes_its_own_pending_ack_before_the_heartbeat_thread_stops(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        """Confirmed live (issue #50 field test): without this, the run ends and the process can
        exit well before the ack queued for stop-measuring-now ever reaches its next scheduled
        heartbeat tick - the backend then redelivers the command to a fresh process with nothing
        left to stop, which can only ack `not_applicable` instead of the `success` that's
        actually true."""
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        modem = FakeModem()
        config = CollectorConfig(max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)
        collector.request_stop_measuring()

        collector.collect()

        heartbeat.send_pending_acks_now.assert_called_once()

    def test_an_ordinary_idle_timeout_does_not_flush_any_ack(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fix_a = GNSSFix(position=pos_a, num_satellites=8)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(max_idle_time=1.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        collector.collect()

        heartbeat.send_pending_acks_now.assert_not_called()


class TestMovementRecord:
    def test_reports_nothing_before_the_first_capture(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        gnss = FakeGNSSReceiver(fixes=[], clock=clock)
        config = CollectorConfig(max_wait_for_first_fix=2.5, wait_log_interval=100)
        collector = Collector(FakeModem(), gnss, config, run_status, heartbeat, run_log, device)

        collector.collect()

        assert collector.movement.latest() is None

    def test_publishes_the_fix_of_every_genuine_movement(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_a_nearby = Position(latitude=NORTH_5M, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)
        fixes = [
            GNSSFix(position=pos_a, num_satellites=8),
            GNSSFix(position=pos_a_nearby, num_satellites=8),
            GNSSFix(position=pos_b, num_satellites=8),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        config = CollectorConfig(max_idle_time=3.0, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(FakeModem(), gnss, config, run_status, heartbeat, run_log, device)

        collector.collect()

        # The nearby fix is below the movement threshold, so B is the last movement published.
        assert collector.movement.latest().fix.position == pos_b

    def test_a_gps_disabled_run_reads_as_continuously_moving(self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        fixes = [GNSSFix(position=pos_a, num_satellites=0, placeholder=True)]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock, seconds_per_read=0.0)
        config = CollectorConfig(
            gps_enabled=False,
            gps_disabled_poll_interval_s=1.0,
            max_idle_time=2.0,
            max_wait_for_first_fix=1000,
            wait_log_interval=1000,
        )
        collector = Collector(FakeModem(), gnss, config, run_status, heartbeat, run_log, device)

        collector.collect()

        assert collector.movement.latest().fix.placeholder is True


class TestDurability:
    def test_each_capture_reaches_disk_before_the_next_one_is_taken(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        # A power cut mid-run gets no chance to flush anything, so every capture must
        # already be on disk by the time the following capture happens.
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)
        captured_after_first: list[dict] = []

        class RecordingModem(FakeModem):
            def query_cell_info(self) -> list[CellSample]:
                if self.query_count == 1:
                    captured_after_first.extend(read_datapoints(run_log.path))
                return super().query_cell_info()

        fixes = [GNSSFix(position=pos_a, num_satellites=8), GNSSFix(position=pos_b, num_satellites=8)]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        config = CollectorConfig(max_idle_time=3.0, max_wait_for_first_fix=1000, wait_log_interval=1000)

        Collector(RecordingModem(), gnss, config, run_status, heartbeat, run_log, device).collect()

        assert len(captured_after_first) == 1

    def test_captures_taken_before_a_crash_survive_on_disk(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)
        pos_c = Position(latitude=2 * NORTH_1KM, longitude=0.0)

        fixes = [
            GNSSFix(position=pos_a, num_satellites=8),
            GNSSFix(position=pos_b, num_satellites=8),
            GNSSFix(position=pos_c, num_satellites=8),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(raise_on_call=3, raise_exc=RuntimeError("power lost"))
        config = CollectorConfig(max_idle_time=1000, max_wait_for_first_fix=1000, wait_log_interval=1000)

        with pytest.raises(RuntimeError, match="power lost"):
            Collector(modem, gnss, config, run_status, heartbeat, run_log, device).collect()

        assert positions(run_log.path) == [pos_a, pos_b]


class TestModemReconnect:
    """A modem that drops off USB mid-run (seen in the field after a USB over-current) must not
    end the run: the capture is skipped, the modem reopened, and collection continues."""

    @staticmethod
    def three_moving_fixes() -> list[GNSSFix]:
        return [
            GNSSFix(position=Position(latitude=i * NORTH_1KM, longitude=0.0), num_satellites=8)
            for i in range(3)
        ]

    @staticmethod
    def config(**overrides) -> CollectorConfig:
        defaults = dict(max_idle_time=1000, max_wait_for_first_fix=1000, wait_log_interval=1000,
                        modem_reconnect_attempts=3, modem_reconnect_delay_s=4.0)
        defaults.update(overrides)
        return CollectorConfig(**defaults)

    def test_connection_loss_skips_that_capture_reconnects_and_keeps_collecting(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device, caplog):
        caplog.set_level("WARNING")
        fixes = self.three_moving_fixes()
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(raise_on_call=2, raise_exc=ModemConnectionError("write failed: [Errno 5]"))
        collector = Collector(modem, gnss, self.config(max_idle_time=3.0), run_status, heartbeat, run_log, device)

        collector.collect()

        assert positions(run_log.path) == [fixes[0].position, fixes[2].position]
        assert modem.open_count == 2
        assert "Errno 5" in caplog.text

    def test_waits_between_reopen_attempts_until_the_modem_is_back(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fixes = self.three_moving_fixes()
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(
            raise_on_call=2, raise_exc=ModemConnectionError("gone"),
            reopen_errors=[ModemConnectionError("no port yet"), None],
        )
        collector = Collector(modem, gnss, self.config(max_idle_time=30.0), run_status, heartbeat, run_log, device)
        before_reconnect = clock.now

        collector.collect()

        assert modem.open_count == 3
        assert positions(run_log.path) == [fixes[0].position, fixes[2].position]
        assert clock.now - before_reconnect >= 2 * 4.0

    def test_gives_up_after_the_configured_attempts_and_propagates(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fixes = self.three_moving_fixes()
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(
            raise_on_call=2, raise_exc=ModemConnectionError("gone"),
            reopen_errors=[ModemConnectionError(f"still gone {i}") for i in range(3)],
        )
        collector = Collector(modem, gnss, self.config(), run_status, heartbeat, run_log, device)

        with pytest.raises(ModemConnectionError, match="still gone 2"):
            collector.collect()

        assert modem.open_count == 1 + 3
        assert positions(run_log.path) == [fixes[0].position]
        heartbeat.stop.assert_called_once()

    def test_sim_problem_on_reopen_is_not_retried(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fixes = self.three_moving_fixes()
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        modem = FakeModem(
            raise_on_call=2, raise_exc=ModemConnectionError("gone"),
            reopen_errors=[SimPinRejectedError("PIN rejected")],
        )
        collector = Collector(modem, gnss, self.config(), run_status, heartbeat, run_log, device)

        with pytest.raises(SimPinRejectedError):
            collector.collect()

        assert modem.open_count == 2

    def test_other_modem_errors_still_propagate_without_reconnecting(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        gnss = FakeGNSSReceiver(fixes=self.three_moving_fixes(), clock=clock)
        modem = FakeModem(raise_on_call=1, raise_exc=ValueError("unexpected"))
        collector = Collector(modem, gnss, self.config(), run_status, heartbeat, run_log, device)

        with pytest.raises(ValueError):
            collector.collect()

        assert modem.open_count == 1


class TestRadioReset:
    def test_is_refused_while_no_run_is_collecting(self, keep_alive_mock, run_status, heartbeat, run_log, device):
        modem = FakeModem()
        collector = Collector(modem, FakeGNSSReceiver(fixes=[], clock=None), CollectorConfig(),
                              run_status, heartbeat, run_log, device)

        assert collector.request_radio_reset() is False
        assert getattr(modem, "radio_resets", 0) == 0

    def test_is_applied_by_the_collection_thread_while_collecting(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fix = GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=8)
        accepted = []

        class RequestingGnss(FakeGNSSReceiver):
            def read_fix(self):
                if not accepted:
                    accepted.append(collector.request_radio_reset())
                return super().read_fix()

        modem = FakeModem()
        collector = Collector(modem, RequestingGnss(fixes=[fix], clock=clock),
                              CollectorConfig(max_idle_time=3.0, max_wait_for_first_fix=1000, wait_log_interval=1000),
                              run_status, heartbeat, run_log, device)

        collector.collect()

        assert accepted == [True]
        assert modem.radio_resets == 1

    def test_a_connection_loss_during_the_reset_reconnects_the_modem(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fix = GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=8)

        class FailingResetModem(FakeModem):
            def reset_radio(self):
                raise ModemConnectionError("gone during reset")

        modem = FailingResetModem()

        class RequestingGnss(FakeGNSSReceiver):
            def read_fix(self):
                if modem.open_count == 1 and not getattr(self, "requested", False):
                    self.requested = collector.request_radio_reset()
                return super().read_fix()

        collector = Collector(modem, RequestingGnss(fixes=[fix], clock=clock),
                              CollectorConfig(max_idle_time=3.0, max_wait_for_first_fix=1000,
                                              wait_log_interval=1000, modem_reconnect_delay_s=1.0),
                              run_status, heartbeat, run_log, device)

        collector.collect()

        assert modem.open_count == 2


class TestGpsFixStatus:
    def test_records_the_first_fix_into_the_shared_gps_fix_status(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fix_a = GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=7)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        config = CollectorConfig(max_idle_time=0.5, max_wait_for_first_fix=1000, wait_log_interval=1000)
        gps_fix_status = GpsFixStatusTracker()
        collector = Collector(
            FakeModem(), gnss, config, run_status, heartbeat, run_log, device,
            gps_fix_status=gps_fix_status,
        )

        collector.collect()

        status = gps_fix_status.status()
        assert status.has_fix is True
        assert status.num_satellites == 7

    def test_records_the_latest_of_several_fixes(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)
        fixes = [
            GNSSFix(position=pos_a, num_satellites=5),
            GNSSFix(position=pos_b, num_satellites=11),
        ]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock)
        config = CollectorConfig(max_idle_time=1.5, max_wait_for_first_fix=1000, wait_log_interval=1000)
        gps_fix_status = GpsFixStatusTracker()
        collector = Collector(
            FakeModem(), gnss, config, run_status, heartbeat, run_log, device,
            gps_fix_status=gps_fix_status,
        )

        collector.collect()

        assert gps_fix_status.status().num_satellites == 11

    def test_is_optional_and_defaults_to_not_recording_anything(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fix_a = GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=7)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        config = CollectorConfig(max_idle_time=0.5, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(FakeModem(), gnss, config, run_status, heartbeat, run_log, device)

        # Must not raise even with no gps_fix_status wired.
        collector.collect()


class TestGnssDiagnostic:
    def test_records_a_diagnostic_into_the_shared_gps_fix_status(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fix_a = GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=7)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock, diagnostics=["ANTENNA SHORT"])
        config = CollectorConfig(max_idle_time=0.5, max_wait_for_first_fix=1000, wait_log_interval=1000)
        gps_fix_status = GpsFixStatusTracker()
        collector = Collector(
            FakeModem(), gnss, config, run_status, heartbeat, run_log, device,
            gps_fix_status=gps_fix_status,
        )

        collector.collect()

        assert gps_fix_status.status().diagnostic == "Antenna short circuit"

    def test_leaves_diagnostic_unset_when_none_is_ever_read(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fix_a = GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=7)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock)
        config = CollectorConfig(max_idle_time=0.5, max_wait_for_first_fix=1000, wait_log_interval=1000)
        gps_fix_status = GpsFixStatusTracker()
        collector = Collector(
            FakeModem(), gnss, config, run_status, heartbeat, run_log, device,
            gps_fix_status=gps_fix_status,
        )

        collector.collect()

        assert gps_fix_status.status().diagnostic is None

    def test_is_optional_and_defaults_to_not_recording_anything(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        fix_a = GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=7)
        gnss = FakeGNSSReceiver(fixes=[fix_a], clock=clock, diagnostics=["ANTENNA SHORT"])
        config = CollectorConfig(max_idle_time=0.5, max_wait_for_first_fix=1000, wait_log_interval=1000)
        collector = Collector(FakeModem(), gnss, config, run_status, heartbeat, run_log, device)

        # Must not raise even with no gps_fix_status wired.
        collector.collect()


class TestTimeCaptureGate:
    def test_captures_on_a_fixed_cadence_regardless_of_distance_moved(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        # All fixes sit at the same spot, well below any distance threshold - only the
        # time-based gate would still capture each one.
        pos_a = Position(latitude=0.0, longitude=0.0)
        fixes = [GNSSFix(position=pos_a, num_satellites=8) for _ in range(4)]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock, seconds_per_read=2.0)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(
            capture_gate="time",
            capture_interval_s=2.0,
            # Large enough to cover 4 captures 2s apart, small enough to end the run afterwards
            # (no genuine movement ever happens here, so idle time only grows from t=3s on).
            max_idle_time=5.0,
            max_wait_for_first_fix=1000,
            wait_log_interval=1000,
        )
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        run_log_path = collector.collect()

        assert modem.query_count == 4
        assert positions(run_log_path) == [pos_a, pos_a, pos_a, pos_a]

    def test_does_not_capture_again_before_the_interval_elapses(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        pos_a = Position(latitude=0.0, longitude=0.0)
        pos_b = Position(latitude=NORTH_1KM, longitude=0.0)
        fixes = [GNSSFix(position=pos_a, num_satellites=8), GNSSFix(position=pos_b, num_satellites=8)]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock, seconds_per_read=1.0)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(
            capture_gate="time",
            capture_interval_s=10.0,
            # Bounds the run once settled at pos_b with no further movement - well short of
            # the 10s capture interval, so it can't produce a second capture either way.
            max_idle_time=3.0,
            max_wait_for_first_fix=1000,
            wait_log_interval=1000,
        )
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        run_log_path = collector.collect()

        # Only the first fix (t=1s) is captured; the second (t=2s) is still inside the interval.
        assert modem.query_count == 1
        assert positions(run_log_path) == [pos_a]

    def test_idle_timeout_still_fires_when_the_vehicle_genuinely_does_not_move(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        # Vehicle parked the whole time: every fix sits at the same spot. A time-based gate
        # still samples on cadence, but that must never reset the idle clock - idle detection
        # stays tied exclusively to genuine (distance-based) movement, so the run still has to end once max_idle_time elapses.
        pos_a = Position(latitude=0.0, longitude=0.0)
        fixes = [GNSSFix(position=pos_a, num_satellites=8) for _ in range(10)]
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock, seconds_per_read=1.0)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(
            capture_gate="time",
            capture_interval_s=1.0,
            max_idle_time=3.0,
            max_wait_for_first_fix=1000,
            wait_log_interval=1000,
        )
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        run_log_path = collector.collect()

        # Despite capturing every second, the run still ends once genuinely idle for
        # max_idle_time - it does not run through all 10 scripted fixes.
        assert modem.query_count < 10
        assert positions(run_log_path) == [pos_a] * modem.query_count

    def test_idle_clock_resets_on_genuine_movement_even_though_capture_is_time_gated(
            self, clock, keep_alive_mock, run_status, heartbeat, run_log, device):
        # The vehicle moves well past the idle timeout's worth of readings, then stops for good.
        moving_fixes = [
            GNSSFix(position=Position(latitude=NORTH_1KM * step, longitude=0.0), num_satellites=8)
            for step in range(5)
        ]
        parked_fix = moving_fixes[-1]
        fixes = moving_fixes + [parked_fix] * 5
        gnss = FakeGNSSReceiver(fixes=fixes, clock=clock, seconds_per_read=1.0)
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        config = CollectorConfig(
            capture_gate="time",
            capture_interval_s=1.0,
            max_idle_time=3.0,
            max_wait_for_first_fix=1000,
            wait_log_interval=1000,
        )
        collector = Collector(modem, gnss, config, run_status, heartbeat, run_log, device)

        collector.collect()

        # The idle clock kept resetting while genuinely moving, so the run outlasted a single
        # max_idle_time window, then ended once it actually stopped.
        assert modem.query_count > 3
        assert collector.movement.latest().fix.position == parked_fix.position
