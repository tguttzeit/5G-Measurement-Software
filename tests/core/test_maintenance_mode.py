import logging
import threading

import pytest

from measurement_software.core import maintenance_mode
from measurement_software.core.config import (
    AppConfig,
    BackendConfig,
    CollectorConfig,
    DeviceConfig,
    DisplayConfig,
    GnssConfig,
    GpsFixTestConfig,
    HeartbeatConfig,
    LatencyTestConfig,
    LoggingConfig,
    ModemConfig,
    MovementGateConfig,
    QuectelCmConfig,
    RunStatusConfig,
    SelftestConfig,
    StorageConfig,
    SystemConfig,
    UploaderConfig,
)
from measurement_software.core.run_phase import RunPhaseState
from measurement_software.modems.modem import CellSample, Modem

DETAILS = {"fingerprint": "ModemConnectionError@quectel:_connection_errors", "same_error_count": 1}


def make_config(tmp_path, running_on_pi: bool) -> AppConfig:
    return AppConfig(
        modem=ModemConfig(type="quectel", baud_rate=115200, timeout=1.0),
        gnss_receiver=GnssConfig(type="nmea_serial", port="/dev/ttyUSB1", baud_rate=9600, timeout=1.0),
        device=DeviceConfig(), collector=CollectorConfig(), movement_gate=MovementGateConfig(),
        quectel_cm=QuectelCmConfig(binary="/usr/bin/quectel-CM"), run_status=RunStatusConfig(),
        backend=BackendConfig(), heartbeat=HeartbeatConfig(enabled=True, interval_s=60), selftest=SelftestConfig(),
        gps_fix_test=GpsFixTestConfig(), storage=StorageConfig(), display=DisplayConfig(enabled=False),
        latency_test=LatencyTestConfig(enabled=False),
        uploader=UploaderConfig(upload_dir=str(tmp_path)),
        logging=LoggingConfig(), system=SystemConfig(running_on_pi=running_on_pi),
    )


class FakeHeartbeat:
    instances: list["FakeHeartbeat"] = []

    def __init__(self, config, backend, run_status, storage_status, run_phase=None, critical_error=None):
        self.run_phase = run_phase
        self.critical_error = critical_error
        self.sends = 0
        FakeHeartbeat.instances.append(self)

    def is_usable(self):
        return True

    def send_once(self, run_status, storage_status):
        events.append("send_once")
        self.sends += 1
        if self.sends == 1:
            raise RuntimeError("backend exploded")
        return True


class RecordingModem(Modem):
    def __init__(self):
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def unlock_sim(self) -> None:
        pass

    def query_cell_info(self) -> list[CellSample]:
        return []

    def reset_radio(self) -> None:
        self.calls.append("reset_radio")

    def power_down(self) -> None:
        pass


class FakeWatchdog:
    def __init__(self, config, quectel_cm_config, request_radio_reset):
        self.request_radio_reset = request_radio_reset
        self.started_with = None
        self.process = None
        self.stopped = False

    def start(self, process):
        self.started_with = process
        self.process = process

    def wait_for_link(self, timeout_s):
        events.append(("wait_for_link", timeout_s))
        return True

    def stop(self):
        self.stopped = True


events: list = []


@pytest.fixture
def fakes(monkeypatch):
    FakeHeartbeat.instances = []
    watchdogs = []

    def make_watchdog(*args, **kwargs):
        watchdogs.append(FakeWatchdog(*args, **kwargs))
        return watchdogs[-1]

    started = []
    monkeypatch.setattr(maintenance_mode, "HeartbeatSender", FakeHeartbeat)
    monkeypatch.setattr(maintenance_mode, "LinkWatchdog", make_watchdog)
    monkeypatch.setattr(maintenance_mode, "start_quectel_cm", lambda config: started.append(config) or "process")
    monkeypatch.setattr(maintenance_mode, "create_modem", lambda config: modem)
    monkeypatch.setattr(maintenance_mode, "stop_quectel_cm", lambda process: stopped_processes.append(process))
    modem = RecordingModem()
    stopped_processes.clear()
    events.clear()
    return started, watchdogs


stopped_processes: list = []


class StopAfter:
    """A stop event that reports itself set after `n` waits, so the loop runs a fixed number of ticks."""

    def __init__(self, n: int):
        self.remaining = n
        self.timeouts = []

    def wait(self, timeout=None) -> bool:
        self.timeouts.append(timeout)
        self.remaining -= 1
        return self.remaining < 0


class Terminated(BaseException):
    """Stands in for main.py's TerminationRequested, which SIGTERM raises in the blocking wait."""


class TerminatedWhileWaiting:
    def wait(self, timeout=None) -> bool:
        raise Terminated()


def test_keeps_the_link_up_and_reports_the_critical_error(tmp_path, fakes, caplog):
    started, watchdogs = fakes
    caplog.set_level(logging.CRITICAL)

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=True), DETAILS, StopAfter(2))

    assert len(started) == 1
    assert watchdogs[0].started_with == "process"
    [heartbeat] = FakeHeartbeat.instances
    assert heartbeat.run_phase.get() == RunPhaseState.CRITICAL_ERROR
    assert heartbeat.critical_error["fingerprint"] == DETAILS["fingerprint"]
    assert "SSH" in heartbeat.critical_error["action"]
    # The backend rejects the whole heartbeat for an action longer than 64 characters.
    assert len(heartbeat.critical_error["action"]) <= 64
    assert "MAINTENANCE MODE" in caplog.text


def test_retries_the_notification_until_delivered_then_stops_sending(tmp_path, fakes):
    stop = StopAfter(5)

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=False), DETAILS, stop)

    [heartbeat] = FakeHeartbeat.instances
    assert heartbeat.sends == 2
    assert stop.timeouts[:2] == [60, None]


def test_skips_the_notification_when_the_heartbeat_is_unusable(tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(FakeHeartbeat, "is_usable", lambda self: False)

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=False), DETAILS, StopAfter(0))

    assert FakeHeartbeat.instances[0].sends == 0


def test_does_not_start_quectel_cm_off_the_pi(tmp_path, fakes):
    started, watchdogs = fakes

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=False), DETAILS, StopAfter(0))

    assert started == []
    assert watchdogs == []


def test_a_failing_link_start_does_not_stop_the_heartbeat(tmp_path, fakes, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)

    def failing_start(config):
        raise FileNotFoundError("quectel-CM missing")

    monkeypatch.setattr(maintenance_mode, "start_quectel_cm", failing_start)

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=True), DETAILS, StopAfter(1))

    assert FakeHeartbeat.instances[0].sends == 2
    assert "quectel-CM missing" in caplog.text


def test_returns_once_stopped(tmp_path, fakes):
    stop = threading.Event()
    stop.set()

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=False), DETAILS, stop)

    assert FakeHeartbeat.instances[0].sends == 1


def test_takes_over_a_quectel_cm_that_is_already_running(tmp_path, fakes):
    started, watchdogs = fakes
    running = object()

    maintenance_mode.run_maintenance_mode(
        make_config(tmp_path, running_on_pi=True), DETAILS, StopAfter(0), quectel_cm_process=running,
    )

    assert started == []
    assert watchdogs[0].started_with is running


def test_the_watchdog_resets_the_radio_through_the_at_port_only_for_the_reset(tmp_path, fakes, monkeypatch):
    _, watchdogs = fakes
    modem = RecordingModem()
    monkeypatch.setattr(maintenance_mode, "create_modem", lambda config: modem)

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=True), DETAILS, StopAfter(0))
    assert modem.calls == []

    assert watchdogs[0].request_radio_reset() is True
    assert modem.calls == ["open", "reset_radio", "close"]


def test_a_modem_that_cannot_be_created_still_leaves_the_link_supervised(tmp_path, fakes, monkeypatch, caplog):
    _, watchdogs = fakes
    caplog.set_level(logging.ERROR)

    def failing_create(config):
        raise ValueError("unknown modem type")

    monkeypatch.setattr(maintenance_mode, "create_modem", failing_create)

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=True), DETAILS, StopAfter(0))

    assert watchdogs[0].started_with == "process"
    assert watchdogs[0].request_radio_reset() is False
    assert "unknown modem type" in caplog.text


class TestWaitingForTheLink:
    def test_notifies_the_backend_only_once_the_link_is_up(self, tmp_path, fakes):
        maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=True), DETAILS, StopAfter(1))

        assert events[:2] == [("wait_for_link", maintenance_mode.LINK_UP_WAIT_S), "send_once"]

    def test_does_not_wait_off_the_pi(self, tmp_path, fakes):
        maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=False), DETAILS, StopAfter(1))

        assert events[0] == "send_once"


class TestTakingTheLinkDown:
    """systemd signals only the app on SIGTERM and kills what is left afterwards, so maintenance
    mode stops the quectel-CM it supervises itself."""

    def test_on_sigterm(self, tmp_path, fakes):
        _, watchdogs = fakes

        with pytest.raises(Terminated):
            maintenance_mode.run_maintenance_mode(
                make_config(tmp_path, running_on_pi=True), DETAILS, TerminatedWhileWaiting(),
            )

        assert watchdogs[0].stopped
        assert stopped_processes == ["process"]

    def test_stops_the_quectel_cm_the_watchdog_last_restarted(self, tmp_path, fakes, monkeypatch):
        _, watchdogs = fakes
        original_stop = FakeWatchdog.stop

        def stop_after_a_restart(self):
            original_stop(self)
            self.process = "restarted"

        monkeypatch.setattr(FakeWatchdog, "stop", stop_after_a_restart)

        with pytest.raises(Terminated):
            maintenance_mode.run_maintenance_mode(
                make_config(tmp_path, running_on_pi=True), DETAILS, TerminatedWhileWaiting(),
            )

        assert stopped_processes == ["restarted"]

    def test_a_failure_taking_it_down_is_only_logged(self, tmp_path, fakes, monkeypatch, caplog):
        caplog.set_level(logging.ERROR)
        monkeypatch.setattr(maintenance_mode, "stop_quectel_cm", lambda process: (_ for _ in ()).throw(OSError("gone")))

        with pytest.raises(Terminated):
            maintenance_mode.run_maintenance_mode(
                make_config(tmp_path, running_on_pi=True), DETAILS, TerminatedWhileWaiting(),
            )

        assert "gone" in caplog.text

    def test_there_is_none_to_take_down_off_the_pi(self, tmp_path, fakes):
        with pytest.raises(Terminated):
            maintenance_mode.run_maintenance_mode(
                make_config(tmp_path, running_on_pi=False), DETAILS, TerminatedWhileWaiting(),
            )

        assert stopped_processes == []

    def test_for_the_emergency_link_on_sigterm(self, tmp_path, fakes):
        _, watchdogs = fakes
        config_path = tmp_path / "config.toml"
        config_path.write_text("[system]\nrunning_on_pi = true\n")

        with pytest.raises(Terminated):
            maintenance_mode.run_emergency_link(config_path, TerminatedWhileWaiting())

        assert watchdogs[0].stopped
        assert stopped_processes == ["process"]


class TestEmergencyLink:
    """Maintenance mode for a device whose configuration can't be loaded at all."""

    def write_config(self, tmp_path, running_on_pi: bool = True):
        path = tmp_path / "config.toml"
        path.write_text(
            f"[system]\nrunning_on_pi = {str(running_on_pi).lower()}\n"
            '[quectel_cm]\nbinary = "/opt/quectel-CM"\nargs = ["-s", "apn.example"]\n'
        )
        return path

    def test_brings_up_and_supervises_the_link_from_the_link_settings_alone(self, tmp_path, fakes, caplog):
        started, watchdogs = fakes
        caplog.set_level(logging.CRITICAL)

        maintenance_mode.run_emergency_link(self.write_config(tmp_path), StopAfter(0))

        assert [(c.binary, c.args) for c in started] == [("/opt/quectel-CM", ("-s", "apn.example"))]
        assert watchdogs[0].started_with == "process"
        assert watchdogs[0].request_radio_reset() is False
        assert FakeHeartbeat.instances == []
        assert "MAINTENANCE MODE" in caplog.text
        assert "reboot" in caplog.text

    def test_does_not_start_quectel_cm_off_the_pi(self, tmp_path, fakes):
        started, watchdogs = fakes

        maintenance_mode.run_emergency_link(self.write_config(tmp_path, running_on_pi=False), StopAfter(0))

        assert started == []
        assert watchdogs == []

    def test_a_failing_link_start_still_blocks(self, tmp_path, fakes, monkeypatch, caplog):
        caplog.set_level(logging.ERROR)
        monkeypatch.setattr(maintenance_mode, "start_quectel_cm", lambda config: (_ for _ in ()).throw(OSError("no sudo")))
        stop = StopAfter(0)

        maintenance_mode.run_emergency_link(self.write_config(tmp_path), stop)

        assert stop.timeouts == [None]
        assert "no sudo" in caplog.text
