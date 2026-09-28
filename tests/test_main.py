import types

import pytest

from measurement_software import main as main_module
from measurement_software.core.config import (
    AppConfig,
    BackendConfig,
    CollectorConfig,
    DeviceConfig,
    DisplayConfig,
    FanConfig,
    GnssConfig,
    HeartbeatConfig,
    LatencyTestConfig,
    LoggingConfig,
    ModemConfig,
    MovementGateConfig,
    QuectelCmConfig,
    RunStatusConfig,
    SelftestConfig,
    GpsFixTestConfig,
    StorageConfig,
    SystemConfig,
    UploaderConfig,
)
from measurement_software.core.collector import RemoteShutdownRequested
from measurement_software.modems.modem import SimPinNotConfiguredError


def make_config(tmp_path, **overrides) -> AppConfig:
    defaults = dict(
        modem=ModemConfig(type="quectel", port="/dev/ttyUSB0", baud_rate=115200, timeout=1.0),
        gnss_receiver=GnssConfig(type="nmea_serial", port="/dev/ttyUSB1", baud_rate=9600, timeout=1.0),
        device=DeviceConfig(),
        collector=CollectorConfig(gps_enabled=False),
        movement_gate=MovementGateConfig(),
        quectel_cm=QuectelCmConfig(),
        run_status=RunStatusConfig(),
        heartbeat=HeartbeatConfig(),
        selftest=SelftestConfig(),
        gps_fix_test=GpsFixTestConfig(),
        storage=StorageConfig(),
        fan=FanConfig(),
        display=DisplayConfig(),
        latency_test=LatencyTestConfig(),
        uploader=UploaderConfig(upload_dir=str(tmp_path), upload_user="user", upload_host="host"),
        logging=LoggingConfig(),
        system=SystemConfig(running_on_pi=False),
        backend=BackendConfig(),
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


class FakeCollector:
    """Stands in for the real Collector at main()-level, so tests can drive main()'s exception
    routing without running an actual GPS-triggered collection session."""

    def __init__(self, modem, gnss_receiver, config, run_status, heartbeat, run_log, device,
                 gps_fix_status=None):
        self._movement = types.SimpleNamespace()
        self._run_log = run_log

    @property
    def movement(self):
        return self._movement

    def collect(self):
        raise NotImplementedError


class SucceedingCollector(FakeCollector):
    def collect(self):
        self._run_log.open()
        self._run_log.close()
        return self._run_log.path


class RaisingCollector(FakeCollector):
    """Raises whatever exception class is set on the `error` class attribute."""

    error: BaseException = RuntimeError("boom")

    def collect(self):
        raise self.error


@pytest.fixture(autouse=True)
def patch_common(monkeypatch):
    monkeypatch.setattr(main_module, "setup_logging", lambda config: None)
    monkeypatch.setattr(main_module, "create_modem", lambda config: object())


class TestMainShutdownRouting:
    def test_successful_run_returns_config_without_shutting_down(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        shutdown_calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: shutdown_calls.append((cfg, reason)))

        result = main_module.main()

        assert result is config
        assert shutdown_calls == []

    def test_unhandled_exception_triggers_unified_shutdown_and_reraises(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RuntimeError("boom")

        monkeypatch.setattr(main_module, "Collector", Failing)
        shutdown_calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: shutdown_calls.append((cfg, reason)))

        with pytest.raises(RuntimeError, match="boom"):
            main_module.main()

        assert shutdown_calls == [(config, "unhandled_exception")]

    def test_remote_shutdown_requested_triggers_shutdown_with_its_own_reason(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RemoteShutdownRequested()

        monkeypatch.setattr(main_module, "Collector", Failing)
        shutdown_calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: shutdown_calls.append((cfg, reason)))

        with pytest.raises(RemoteShutdownRequested):
            main_module.main()

        assert shutdown_calls == [(config, "remote_shutdown_command")]

    def test_sim_unlock_error_triggers_shutdown_with_its_own_reason(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = SimPinNotConfiguredError("SIM is PIN-locked but no PIN is configured")

        monkeypatch.setattr(main_module, "Collector", Failing)
        shutdown_calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: shutdown_calls.append((cfg, reason)))

        with pytest.raises(SimPinNotConfiguredError):
            main_module.main()

        assert shutdown_calls == [(config, "sim_unlock_failed")]

    def test_shutdown_runs_after_fan_controller_has_already_stopped(self, monkeypatch, tmp_path):
        """The unified shutdown's own GPIO cleanup must never race a background controller that's
        still driving GPIO pins, so fan_controller.stop() has to complete before shutdown() runs."""
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RuntimeError("boom")

        monkeypatch.setattr(main_module, "Collector", Failing)

        order = []
        stop_calls = []

        class TrackedFanController:
            def __init__(self, fan_config):
                pass

            def start(self):
                pass

            def stop(self):
                order.append("fan_controller.stop")
                stop_calls.append(True)

        def fake_shutdown(cfg, reason):
            order.append("shutdown")

        monkeypatch.setattr(main_module, "FanController", TrackedFanController)
        monkeypatch.setattr(main_module, "shutdown", fake_shutdown)

        with pytest.raises(RuntimeError):
            main_module.main()

        assert order == ["fan_controller.stop", "shutdown"]


class TestUploadTiming:
    """Upload attempts happen at the waiting/ACTIVE_MEASURING transitions, replacing the old
    fixed start-of-run/end-of-run upload points."""

    def test_first_upload_happens_after_the_connection_opens_not_at_the_start(self, monkeypatch, tmp_path):
        config = make_config(
            tmp_path,
            collector=CollectorConfig(gps_enabled=True),
            system=SystemConfig(running_on_pi=True, shutdown_gpio=17),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "setup_gpio", lambda pin: None)
        monkeypatch.setattr(main_module, "signal_completion", lambda pin: None)

        order = []

        class RecordingUploader:
            def __init__(self, config, backend, device_id=""):
                pass

            def upload_pending_files(self):
                order.append("upload_pending_files")

        class TrackedCollector(SucceedingCollector):
            def collect(self):
                order.append("collector.collect")
                return super().collect()

        class FakeMovementGate:
            def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
                pass

            def wait_for_movement(self):
                order.append("movement_gate.wait_for_movement")

        monkeypatch.setattr(main_module, "Uploader", RecordingUploader)
        monkeypatch.setattr(main_module, "Collector", TrackedCollector)
        monkeypatch.setattr(main_module, "MovementGate", FakeMovementGate)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda config: order.append("start_quectel_cm"))

        main_module.main()

        assert order == [
            "start_quectel_cm",
            "movement_gate.wait_for_movement",
            "upload_pending_files",
            "collector.collect",
            "upload_pending_files",
        ]

    def test_still_uploads_around_collect_when_gps_and_pi_features_are_disabled(self, monkeypatch, tmp_path):
        """Dev/test config (gps_enabled=False, running_on_pi=False) skips the movement gate and
        quectel-CM, but still gets an upload attempt bracketing collect(): once at the
        ACTIVE_MEASURING transition, and again after finalizing."""
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        order = []

        class RecordingUploader:
            def __init__(self, config, backend, device_id=""):
                pass

            def upload_pending_files(self):
                order.append("upload_pending_files")

        class TrackedCollector(SucceedingCollector):
            def collect(self):
                order.append("collector.collect")
                return super().collect()

        monkeypatch.setattr(main_module, "Uploader", RecordingUploader)
        monkeypatch.setattr(main_module, "Collector", TrackedCollector)

        main_module.main()

        assert order == ["upload_pending_files", "collector.collect", "upload_pending_files"]


class TestAutoModeHeartbeatFromBoot:
    """Auto Mode (the default) now opens quectel-CM and starts the heartbeat immediately at
    boot, the same way Waiting Mode already does, but still transitions automatically into
    active measuring once MovementGate confirms movement - see issue #37/decision record 0021."""

    def test_opens_quectel_cm_and_heartbeat_before_waiting_for_movement(self, monkeypatch, tmp_path):
        config = make_config(
            tmp_path,
            collector=CollectorConfig(gps_enabled=True),
            system=SystemConfig(running_on_pi=True, shutdown_gpio=17),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "setup_gpio", lambda pin: None)
        monkeypatch.setattr(main_module, "signal_completion", lambda pin: None)

        order = []

        class RecordingHeartbeat:
            def __init__(self, *a, **kw):
                pass

            def set_remote_commands(self, dispatcher):
                pass

            def start(self):
                order.append("heartbeat.start")

            def stop(self):
                order.append("heartbeat.stop")

        class TrackedCollector(SucceedingCollector):
            def collect(self):
                order.append("collector.collect")
                return super().collect()

        class TrackedMovementGate:
            def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
                pass

            def wait_for_movement(self):
                order.append("movement_gate.wait_for_movement")

        monkeypatch.setattr(main_module, "HeartbeatSender", RecordingHeartbeat)
        monkeypatch.setattr(main_module, "Collector", TrackedCollector)
        monkeypatch.setattr(main_module, "MovementGate", TrackedMovementGate)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda config: order.append("start_quectel_cm"))

        main_module.main()

        assert order == [
            "start_quectel_cm",
            "heartbeat.start",
            "movement_gate.wait_for_movement",
            "heartbeat.stop",
            "collector.collect",
        ]

    def test_starts_heartbeat_even_when_not_running_on_pi(self, monkeypatch, tmp_path):
        """quectel-CM itself is only started when running_on_pi, but the heartbeat still starts
        immediately - a dev/testing run should behave the same way with respect to visibility."""
        config = make_config(tmp_path, collector=CollectorConfig(gps_enabled=True))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        order = []

        class RecordingHeartbeat:
            def __init__(self, *a, **kw):
                pass

            def set_remote_commands(self, dispatcher):
                pass

            def start(self):
                order.append("heartbeat.start")

            def stop(self):
                order.append("heartbeat.stop")

        class TrackedMovementGate:
            def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
                pass

            def wait_for_movement(self):
                order.append("movement_gate.wait_for_movement")

        monkeypatch.setattr(main_module, "HeartbeatSender", RecordingHeartbeat)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "MovementGate", TrackedMovementGate)

        main_module.main()

        assert order == ["heartbeat.start", "movement_gate.wait_for_movement", "heartbeat.stop"]


class TestWaitingMode:
    """system.mode="waiting" skips the movement gate entirely: it opens quectel-CM and the
    heartbeat immediately, blocks in WaitingLoop.wait() until start-measuring-now, then
    transitions into the same measuring flow Auto Mode uses."""

    def test_opens_quectel_cm_and_heartbeat_before_waiting_then_measures(self, monkeypatch, tmp_path):
        config = make_config(
            tmp_path,
            system=SystemConfig(running_on_pi=True, shutdown_gpio=17, mode="waiting"),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "setup_gpio", lambda pin: None)
        monkeypatch.setattr(main_module, "signal_completion", lambda pin: None)

        order = []

        class RecordingHeartbeat:
            def __init__(self, *a, **kw):
                pass

            def set_remote_commands(self, dispatcher):
                pass

            def start(self):
                order.append("heartbeat.start")

            def stop(self):
                order.append("heartbeat.stop")

        class TrackedCollector(SucceedingCollector):
            def collect(self):
                order.append("collector.collect")
                return super().collect()

        monkeypatch.setattr(main_module, "HeartbeatSender", RecordingHeartbeat)
        monkeypatch.setattr(main_module, "Collector", TrackedCollector)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda config: order.append("start_quectel_cm"))

        class ImmediateWaitingLoop:
            def wait(self, on_run_selftest=None):
                order.append("waiting_loop.wait")

            def request_start_measuring(self):
                pass

        monkeypatch.setattr(main_module, "WaitingLoop", lambda: ImmediateWaitingLoop())

        main_module.main()

        assert order == [
            "start_quectel_cm",
            "heartbeat.start",
            "waiting_loop.wait",
            "heartbeat.stop",
            "collector.collect",
        ]

    def test_does_not_touch_movement_gate_in_waiting_mode(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="waiting"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        movement_gate_calls = []

        class TrackedMovementGate:
            def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
                pass

            def wait_for_movement(self):
                movement_gate_calls.append(True)

        class ImmediateWaitingLoop:
            def wait(self, on_run_selftest=None):
                pass

            def request_start_measuring(self):
                pass

        monkeypatch.setattr(main_module, "MovementGate", TrackedMovementGate)
        monkeypatch.setattr(main_module, "WaitingLoop", lambda: ImmediateWaitingLoop())

        main_module.main()

        assert movement_gate_calls == []

    def test_auto_mode_is_unchanged_default(self, tmp_path):
        config = make_config(tmp_path)

        assert config.system.mode == "auto"

    def test_run_selftest_now_callback_runs_every_check_with_no_side_effects(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="waiting"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        run_checks_calls = []

        def fake_run_checks(cfg, names, *, confirm_side_effects, reporter):
            run_checks_calls.append((cfg, names, confirm_side_effects, reporter))
            return []

        monkeypatch.setattr(main_module, "run_checks", fake_run_checks)
        monkeypatch.setattr(main_module, "available_checks", lambda: ["modem", "gps"])
        monkeypatch.setattr(main_module, "SelftestResultsReporter", lambda *a, **kw: "reporter")

        class SelftestTriggeringWaitingLoop:
            def wait(self, on_run_selftest=None):
                on_run_selftest()

            def request_start_measuring(self):
                pass

        monkeypatch.setattr(main_module, "WaitingLoop", lambda: SelftestTriggeringWaitingLoop())

        main_module.main()

        assert len(run_checks_calls) == 1
        cfg, names, confirm_side_effects, reporter = run_checks_calls[0]
        assert cfg is config
        assert names == ["modem", "gps"]
        assert confirm_side_effects is False
        assert reporter == "reporter"

    def test_stops_any_in_progress_gps_fix_test_before_measuring_starts(self, monkeypatch, tmp_path):
        """A running gps-fix-test holds its own GNSS connection open - it has to be stopped
        before Collector.collect() opens its own GNSSReceiver on the same serial port."""
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="waiting"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        order = []

        class ImmediateWaitingLoop:
            def wait(self, on_run_selftest=None):
                order.append("waiting_loop.wait")

            def request_start_measuring(self):
                pass

        class TrackedGpsFixTestRunner:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                pass

            def stop(self):
                order.append("gps_fix_test_runner.stop")

        monkeypatch.setattr(main_module, "WaitingLoop", lambda: ImmediateWaitingLoop())
        monkeypatch.setattr(main_module, "GpsFixTestRunner", TrackedGpsFixTestRunner)

        main_module.main()

        assert order == ["waiting_loop.wait", "gps_fix_test_runner.stop"]
