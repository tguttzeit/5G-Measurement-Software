import json
import logging
import signal
import types

import pytest

from measurement_software import main as main_module
from measurement_software.core.config import (
    AppConfig,
    BackendConfig,
    CollectorConfig,
    DeviceConfig,
    DisplayConfig,
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
from measurement_software.core.collector import RemoteRebootRequested, RemoteShutdownRequested
from measurement_software.core.crash_policy import CRASH_REPORT_FILENAME, CRASH_STATE_FILENAME, CrashPolicy
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
                 gps_fix_status=None, on_sim_reset=None):
        self._movement = types.SimpleNamespace()
        self._run_log = run_log

    @property
    def run_id(self):
        return "test-run-id"

    @property
    def movement(self):
        return self._movement

    def check_remote_shutdown_or_reboot(self):
        pass

    def request_radio_reset(self):
        return False

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


class FakeLinkWatchdog:
    instances: list["FakeLinkWatchdog"] = []

    def __init__(self, config, quectel_cm_config, request_radio_reset):
        self.started_with = None
        self.stopped = False
        FakeLinkWatchdog.instances.append(self)

    def start(self, process):
        self.started_with = process

    def stop(self):
        self.stopped = True


@pytest.fixture
def crash_dir(tmp_path):
    path = tmp_path / "crash"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def patch_common(monkeypatch, crash_dir):
    monkeypatch.setattr(main_module, "setup_logging", lambda config: None)
    monkeypatch.setattr(main_module, "create_modem", lambda config: object())
    # main() keeps crash state next to config.toml - redirect it so tests never touch the repo.
    monkeypatch.setattr(
        main_module, "CrashPolicy",
        lambda state_dir, same_error_limit, reboot_limit: CrashPolicy(crash_dir, same_error_limit, reboot_limit),
    )
    FakeLinkWatchdog.instances = []
    monkeypatch.setattr(main_module, "LinkWatchdog", FakeLinkWatchdog)
    monkeypatch.setattr(main_module, "finalize_and_upload_pending", lambda config: None)
    # Never run the real sequences - they end in `sudo shutdown`. Tests that check them override these.
    monkeypatch.setattr(main_module, "reboot", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_module, "shutdown", lambda *args, **kwargs: None)
    # main() writes overrides next to the real config.toml, and maintenance mode blocks for good.
    monkeypatch.setattr(main_module, "write_overrides_file", lambda path, changes: None)
    monkeypatch.setattr(main_module, "run_maintenance_mode", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    """main() installs process-wide SIGINT/SIGTERM handlers; keep them out of the test process."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


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

    def test_unhandled_exception_reboots_and_reraises(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RuntimeError("boom")

        monkeypatch.setattr(main_module, "Collector", Failing)
        reboot_calls = []
        monkeypatch.setattr(main_module, "reboot", lambda cfg, reason: reboot_calls.append((cfg, reason)))
        shutdown_calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: shutdown_calls.append((cfg, reason)))

        with pytest.raises(RuntimeError, match="boom"):
            main_module.main()

        assert reboot_calls == [(config, "unhandled_exception")]
        assert shutdown_calls == []

    def test_remote_shutdown_requested_triggers_shutdown_with_its_own_reason(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RemoteShutdownRequested(42)

        monkeypatch.setattr(main_module, "Collector", Failing)
        shutdown_calls = []
        monkeypatch.setattr(
            main_module, "shutdown",
            lambda cfg, reason, command_id=None: shutdown_calls.append((cfg, reason, command_id)),
        )

        with pytest.raises(RemoteShutdownRequested):
            main_module.main()

        assert shutdown_calls == [(config, "remote_shutdown_command", 42)]

    def test_remote_reboot_requested_triggers_reboot_with_its_own_reason(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RemoteRebootRequested(43)

        monkeypatch.setattr(main_module, "Collector", Failing)
        reboot_calls = []
        monkeypatch.setattr(
            main_module, "reboot",
            lambda cfg, reason, command_id=None, config_path=None: reboot_calls.append((cfg, reason, command_id)),
        )
        shutdown_calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason, command_id=None: shutdown_calls.append((cfg, reason)))

        with pytest.raises(RemoteRebootRequested):
            main_module.main()

        assert reboot_calls == [(config, "remote_reboot_command", 43)]
        assert shutdown_calls == []

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


class TestUnhandledErrorPolicy:
    """An unhandled error reboots; the same one repeating (or too many reboots in a row) switches
    to Waiting Mode and reboots; an error even then reboots into maintenance mode."""

    @pytest.fixture
    def failing_run(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, crash_same_error_limit=3))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        calls = []
        monkeypatch.setattr(main_module, "reboot", lambda cfg, reason: calls.append(("reboot", reason)))
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: calls.append(("shutdown", reason)))
        monkeypatch.setattr(main_module, "write_overrides_file",
                            lambda path, changes: calls.append(("overrides", path.name, changes)))
        monkeypatch.setattr(main_module, "run_maintenance_mode",
                            lambda config, details, **kwargs: calls.append(("maintenance", details, kwargs)))
        return calls

    def run_failing(self, monkeypatch, error):
        class Failing(RaisingCollector):
            pass

        Failing.error = error
        monkeypatch.setattr(main_module, "Collector", Failing)
        with pytest.raises(type(error)):
            main_module.main()

    def test_the_same_error_three_times_in_a_row_switches_to_waiting_mode_and_reboots(
            self, monkeypatch, failing_run):
        for _ in range(3):
            self.run_failing(monkeypatch, RuntimeError("boom"))

        assert failing_run == [
            ("reboot", "unhandled_exception"),
            ("reboot", "unhandled_exception"),
            ("overrides", "config_overrides.toml", {"system_mode": "waiting"}),
            ("reboot", "repeated_unhandled_exception"),
        ]
        assert not any(call[0] == "shutdown" for call in failing_run)

    def test_an_error_after_switching_to_waiting_mode_reboots_into_maintenance_mode(
            self, monkeypatch, failing_run):
        for _ in range(3):
            self.run_failing(monkeypatch, RuntimeError("boom"))

        self.run_failing(monkeypatch, KeyError("another"))
        assert failing_run[-1] == ("reboot", "unhandled_exception_after_waiting_mode")

        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        with pytest.raises(SystemExit):
            main_module.main()
        action, details, kwargs = failing_run[-1]
        assert action == "maintenance"
        assert details["fingerprint"].startswith("KeyError@")
        assert kwargs == {}

    def test_maintenance_mode_starts_before_anything_else_runs(self, monkeypatch, failing_run, crash_dir):
        (crash_dir / CRASH_STATE_FILENAME).write_text(json.dumps(
            {"fingerprint": "X@y:z", "same_error_count": 1, "consecutive_reboots": 4, "escalation": "maintenance"}
        ))

        class MustNotRun(SucceedingCollector):
            def __init__(self, *args, **kwargs):
                raise AssertionError("maintenance mode must not build the measuring pipeline")

        monkeypatch.setattr(main_module, "Collector", MustNotRun)

        with pytest.raises(SystemExit):
            main_module.main()

        assert [call[0] for call in failing_run] == ["maintenance"]

    def test_an_unrecordable_crash_enters_maintenance_mode_without_a_reboot(self, monkeypatch, failing_run):
        class BrokenPolicy(CrashPolicy):
            def record(self, error):
                raise OSError("disk full")

        monkeypatch.setattr(main_module, "CrashPolicy", lambda *args: BrokenPolicy(*args))

        self.run_failing(monkeypatch, RuntimeError("boom"))

        [(action, details, kwargs)] = failing_run
        assert action == "maintenance"
        assert kwargs == {"start_link": False}

    def test_a_successful_run_in_between_resets_the_count(self, monkeypatch, failing_run, crash_dir):
        self.run_failing(monkeypatch, RuntimeError("boom"))
        self.run_failing(monkeypatch, RuntimeError("boom"))
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        main_module.main()

        self.run_failing(monkeypatch, RuntimeError("boom"))

        assert [call[0] for call in failing_run] == ["reboot", "reboot", "reboot"]

    def test_writes_a_crash_report_and_logs_it_on_the_next_start(self, monkeypatch, failing_run, crash_dir, caplog):
        self.run_failing(monkeypatch, RuntimeError("boom with details"))
        report = json.loads((crash_dir / CRASH_REPORT_FILENAME).read_text())
        assert report["action"] == "reboot"
        assert "boom with details" in report["traceback"]

        caplog.set_level(logging.WARNING)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        main_module.main()

        assert "Previous run ended with an unhandled error" in caplog.text
        assert "boom with details" in caplog.text
        assert not (crash_dir / CRASH_REPORT_FILENAME).exists()

    def test_logs_the_decision_with_its_counters(self, monkeypatch, failing_run, caplog):
        caplog.set_level(logging.CRITICAL)

        self.run_failing(monkeypatch, RuntimeError("boom"))

        assert "same error 1 time(s) in a row" in caplog.text
        assert "rebooting" in caplog.text


class TestSigterm:
    def test_main_installs_a_sigterm_handler_that_raises_termination_requested(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        main_module.main()

        with pytest.raises(main_module.TerminationRequested):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    def test_ends_the_run_cleanly_without_shutdown_reboot_or_crash_count(self, monkeypatch, tmp_path, crash_dir):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Terminated(RaisingCollector):
            error = main_module.TerminationRequested()

        monkeypatch.setattr(main_module, "Collector", Terminated)
        calls = []
        monkeypatch.setattr(main_module, "reboot", lambda *args, **kwargs: calls.append("reboot"))
        monkeypatch.setattr(main_module, "shutdown", lambda *args, **kwargs: calls.append("shutdown"))
        monkeypatch.setattr(main_module, "finalize_and_upload_pending", lambda cfg: calls.append("finalize_and_upload"))

        with pytest.raises(main_module.TerminationRequested):
            main_module.main()

        assert calls == ["finalize_and_upload"]
        assert not (crash_dir / CRASH_STATE_FILENAME).exists()

    def test_is_not_swallowed_by_generic_exception_handlers(self):
        assert not issubclass(main_module.TerminationRequested, Exception)


class TestLinkWatchdogWiring:
    def test_watches_the_started_quectel_cm_and_stops_with_the_run(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=True, shutdown_gpio=17))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "setup_gpio", lambda pin: None)
        monkeypatch.setattr(main_module, "signal_completion", lambda pin: None)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        process = object()
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda quectel_cm_config: process)

        main_module.main()

        [watchdog] = FakeLinkWatchdog.instances
        assert watchdog.started_with is process
        assert watchdog.stopped

    def test_is_not_started_off_the_pi(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        main_module.main()

        [watchdog] = FakeLinkWatchdog.instances
        assert watchdog.started_with is None


class FakeDisplay:
    """Records show()/close() calls, so tests can assert what's left on screen and whether it
    was ever blanked."""

    def __init__(self):
        self.messages: list[str] = []
        self.closed = False

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def show(self, message: str) -> None:
        self.messages.append(message)


class TestDisplayOnRemoteShutdownOrReboot:
    """The on-device screen must still show shutdown-now/reboot-now happening even though the
    periodic refresh thread stops - see StatusDisplayUpdater.show_final()."""

    def test_remote_shutdown_requested_shows_shutting_down_and_never_blanks_the_screen(
            self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RemoteShutdownRequested(42)

        monkeypatch.setattr(main_module, "Collector", Failing)
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason, command_id=None: None)
        display = FakeDisplay()
        monkeypatch.setattr(main_module, "create_display", lambda config: display)

        with pytest.raises(RemoteShutdownRequested):
            main_module.main()

        assert "SHUTTING DOWN" in display.messages[-1]
        assert display.closed is False

    def test_remote_reboot_requested_shows_rebooting_and_never_blanks_the_screen(
            self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RemoteRebootRequested(43)

        monkeypatch.setattr(main_module, "Collector", Failing)
        monkeypatch.setattr(main_module, "reboot", lambda cfg, reason, command_id=None, config_path=None: None)
        display = FakeDisplay()
        monkeypatch.setattr(main_module, "create_display", lambda config: display)

        with pytest.raises(RemoteRebootRequested):
            main_module.main()

        assert "REBOOTING" in display.messages[-1]
        assert display.closed is False

    def test_an_unrelated_failure_still_closes_the_display(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Failing(RaisingCollector):
            error = RuntimeError("boom")

        monkeypatch.setattr(main_module, "Collector", Failing)
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: None)
        display = FakeDisplay()
        monkeypatch.setattr(main_module, "create_display", lambda config: display)

        with pytest.raises(RuntimeError):
            main_module.main()

        assert display.closed is True


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

            def wait_for_movement(self, on_check_remote_shutdown=None):
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


class TestMovementGateDisabled:
    def _run(self, monkeypatch, tmp_path, enabled, caplog):
        config = make_config(
            tmp_path,
            collector=CollectorConfig(gps_enabled=True),
            movement_gate=MovementGateConfig(enabled=enabled),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        waited = []

        class FakeMovementGate:
            def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
                pass

            def wait_for_movement(self, on_check_remote_shutdown=None):
                waited.append(True)

        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "MovementGate", FakeMovementGate)
        with caplog.at_level("INFO"):
            main_module.main()
        return waited

    def test_skips_the_movement_wait_and_logs_the_mode(self, monkeypatch, tmp_path, caplog):
        waited = self._run(monkeypatch, tmp_path, enabled=False, caplog=caplog)

        assert waited == []
        assert "movement gate disabled" in caplog.text.lower()
        assert not [r for r in caplog.records if r.levelname == "WARNING" and "movement gate" in r.getMessage().lower()]

    def test_waits_for_movement_when_enabled(self, monkeypatch, tmp_path, caplog):
        waited = self._run(monkeypatch, tmp_path, enabled=True, caplog=caplog)

        assert waited == [True]


class TestAutoModeHeartbeatFromBoot:
    """Auto Mode (the default) now opens quectel-CM and starts the heartbeat immediately at
    boot, the same way Waiting Mode already does, but still transitions automatically into
    active measuring once MovementGate confirms movement - see issue #37."""

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

            def wait_for_movement(self, on_check_remote_shutdown=None):
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

            def wait_for_movement(self, on_check_remote_shutdown=None):
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
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
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

            def wait_for_movement(self, on_check_remote_shutdown=None):
                movement_gate_calls.append(True)

        class ImmediateWaitingLoop:
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
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
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
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
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
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


class SpyRemoteCommandDispatcher:
    """Stands in for the real RemoteCommandDispatcher so tests can observe seed_reboot_success_ack()
    calls without constructing the real collaborators it needs."""

    seeded: list = []

    def __init__(self, **kwargs):
        pass

    def seed_reboot_success_ack(self, command_id):
        self.seeded.append(command_id)


class TestMainPendingRebootAck:
    def test_seeds_the_dispatcher_when_a_pending_reboot_ack_was_persisted(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "read_and_clear_pending_reboot_ack", lambda path: 52)
        seeded: list = []
        monkeypatch.setattr(SpyRemoteCommandDispatcher, "seeded", seeded)
        monkeypatch.setattr(main_module, "RemoteCommandDispatcher", SpyRemoteCommandDispatcher)

        main_module.main()

        assert seeded == [52]

    def test_does_not_seed_when_nothing_was_persisted(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "read_and_clear_pending_reboot_ack", lambda path: None)
        seeded: list = []
        monkeypatch.setattr(SpyRemoteCommandDispatcher, "seeded", seeded)
        monkeypatch.setattr(main_module, "RemoteCommandDispatcher", SpyRemoteCommandDispatcher)

        main_module.main()

        assert seeded == []


class TestStartupBeforeRun:
    def test_migrates_a_legacy_mode_override_before_loading_config(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        order = []
        monkeypatch.setattr(
            main_module, "migrate_legacy_mode_override",
            lambda overrides_path, legacy_path: order.append(
                ("migrate", overrides_path.name, legacy_path.name)
            ),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: order.append("load_config") or config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        main_module.main()

        assert order == [("migrate", "config_overrides.toml", "mode_override.toml"), "load_config"]

    def test_continues_as_not_on_pi_when_rpi_gpio_cannot_be_imported(self, monkeypatch, tmp_path, caplog):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=True, shutdown_gpio=17))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        shutdown_calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: shutdown_calls.append(reason))
        signalled = []
        monkeypatch.setattr(main_module, "signal_completion", lambda pin: signalled.append(pin))

        def missing_gpio(pin):
            raise ImportError("No module named 'RPi'")

        monkeypatch.setattr(main_module, "setup_gpio", missing_gpio)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda cfg: None)
        monkeypatch.setattr(main_module, "cleanup_gpio", lambda: None)

        with caplog.at_level("ERROR"):
            result = main_module.main()

        assert result.system.running_on_pi is False
        assert shutdown_calls == []
        assert signalled == []
        assert "RPi.GPIO is not available" in caplog.text
