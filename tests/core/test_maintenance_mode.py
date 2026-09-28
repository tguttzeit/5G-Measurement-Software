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

DETAILS = {"fingerprint": "ModemConnectionError@quectel:_connection_errors", "same_error_count": 1}


def make_config(tmp_path, running_on_pi: bool) -> AppConfig:
    return AppConfig(
        modem=ModemConfig(type="quectel", baud_rate=115200, timeout=1.0),
        gnss_receiver=GnssConfig(type="nmea_serial", port="/dev/ttyUSB1", baud_rate=9600, timeout=1.0),
        device=DeviceConfig(), collector=CollectorConfig(), movement_gate=MovementGateConfig(),
        quectel_cm=QuectelCmConfig(binary="/usr/bin/quectel-CM"), run_status=RunStatusConfig(),
        backend=BackendConfig(), heartbeat=HeartbeatConfig(enabled=True, interval_s=60), selftest=SelftestConfig(),
        gps_fix_test=GpsFixTestConfig(), storage=StorageConfig(), display=DisplayConfig(enabled=False),
        latency_test=LatencyTestConfig(),
        uploader=UploaderConfig(upload_dir=str(tmp_path), upload_user="user", upload_host="host"),
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
        self.sends += 1
        if self.sends == 1:
            raise RuntimeError("backend exploded")
        return True


class FakeWatchdog:
    def __init__(self, config, quectel_cm_config, request_radio_reset):
        self.request_radio_reset = request_radio_reset
        self.started_with = None

    def start(self, process):
        self.started_with = process

    def stop(self):
        pass


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
    return started, watchdogs


class StopAfter:
    """A stop event that reports itself set after `n` waits, so the loop runs a fixed number of ticks."""

    def __init__(self, n: int):
        self.remaining = n
        self.timeouts = []

    def wait(self, timeout=None) -> bool:
        self.timeouts.append(timeout)
        self.remaining -= 1
        return self.remaining < 0


def test_keeps_the_link_up_and_reports_the_critical_error(tmp_path, fakes, caplog):
    started, watchdogs = fakes
    caplog.set_level(logging.CRITICAL)

    maintenance_mode.run_maintenance_mode(make_config(tmp_path, running_on_pi=True), DETAILS, StopAfter(2))

    assert len(started) == 1
    assert watchdogs[0].started_with == "process"
    assert watchdogs[0].request_radio_reset() is False
    [heartbeat] = FakeHeartbeat.instances
    assert heartbeat.run_phase.get() == RunPhaseState.CRITICAL_ERROR
    assert heartbeat.critical_error["fingerprint"] == DETAILS["fingerprint"]
    assert "SSH" in heartbeat.critical_error["action"]
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
