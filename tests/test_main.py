import json
import logging
import signal
import types

import pytest

from measurement_software import main as main_module
from measurement_software.core.config import (
    AppConfig,
    BackendConfig,
    ClockConfig,
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
from measurement_software.core.collector import (
    RemoteRebootRequested,
    RemoteShutdownRequested,
    StopMeasuringRequested,
)
from measurement_software.core.crash_policy import CRASH_REPORT_FILENAME, CRASH_STATE_FILENAME, CrashPolicy
from measurement_software.core.config_sources import write_manual_next_boot_marker
from measurement_software.core import maintenance_mode
from measurement_software.core.first_fix_wait import FirstFixWaiter as RealFirstFixWaiter
from measurement_software.core.link_watchdog import LINK_UP_WAIT_S
from measurement_software.core.run_phase import RunPhaseState
from measurement_software.gnss.gnss_receiver import GNSSFix, Position
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
        heartbeat=HeartbeatConfig(enabled=False),
        selftest=SelftestConfig(),
        gps_fix_test=GpsFixTestConfig(),
        storage=StorageConfig(),
        display=DisplayConfig(enabled=False),
        latency_test=LatencyTestConfig(enabled=False),
        uploader=UploaderConfig(upload_dir=str(tmp_path)),
        logging=LoggingConfig(),
        system=SystemConfig(running_on_pi=False),
        backend=BackendConfig(),
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


class FakeCollector:
    """Stands in for the real Collector at main()-level, so tests can drive main()'s exception
    routing without running an actual GPS-triggered collection session."""

    instances: list["FakeCollector"] = []

    def __init__(self, modem, gnss_receiver, config, run_status, run_log, device,
                 gps_fix_status=None, on_sim_reset=None, idle_end_enabled=True, modem_access=None):
        self._movement = types.SimpleNamespace()
        self._run_log = run_log
        self.idle_end_enabled = idle_end_enabled
        FakeCollector.instances.append(self)

    @property
    def run_id(self):
        return "test-run-id"

    @property
    def movement(self):
        return self._movement

    def check_remote_shutdown_or_reboot(self):
        pass

    def check_remote_commands_before_measuring(self):
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


class ImmediateManualModeLoop:
    def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
        pass

    def request_start_measuring(self):
        pass


FIRST_FIX = GNSSFix(position=Position(latitude=48.0, longitude=11.0), num_satellites=8)


class FakeFirstFixWaiter:
    """Stands in for FirstFixWaiter, so tests drive main()'s routing around the fix wait without
    any GNSS hardware. Returns `FIRST_FIX` at once unless `outcomes` scripts successive results:
    a GNSSFix or None is returned, an exception is raised."""

    instances: list["FakeFirstFixWaiter"] = []
    outcomes: list = []

    def __init__(self, gnss_receiver, wait_log_interval_s, run_phase, clock_sync, gps_fix_status):
        self.run_phase = run_phase
        self.calls: list[dict] = []
        FakeFirstFixWaiter.instances.append(self)

    def wait(self, check_remote_commands, timeout_s):
        self.calls.append({"check": check_remote_commands, "timeout_s": timeout_s, "phase": self.run_phase.get()})
        if not FakeFirstFixWaiter.outcomes:
            return FIRST_FIX
        outcome = FakeFirstFixWaiter.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeLinkWatchdog:
    instances: list["FakeLinkWatchdog"] = []

    def __init__(self, config, quectel_cm_config, request_radio_reset, on_link_up=None):
        self.on_link_up = on_link_up
        self.started_with = None
        self.process = None
        self.stopped = False
        FakeLinkWatchdog.instances.append(self)

    def start(self, process):
        self.started_with = process
        self.process = process

    def wait_for_link(self, timeout_s):
        self.waited_for_link = timeout_s
        return True

    def stop(self):
        self.stopped = True


@pytest.fixture
def crash_dir(tmp_path):
    path = tmp_path / "crash"
    path.mkdir()
    return path


saved_link_settings: list = []


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
    # Would write next to the repository's config.toml otherwise.
    saved_link_settings.clear()
    monkeypatch.setattr(main_module, "save_last_good_link_settings",
                        lambda config, config_path: saved_link_settings.append((config, config_path.name)))
    FakeFirstFixWaiter.instances = []
    FakeFirstFixWaiter.outcomes = []
    monkeypatch.setattr(main_module, "FirstFixWaiter", FakeFirstFixWaiter)
    monkeypatch.setattr(main_module, "finalize_and_upload_pending", lambda config: None)
    # Never run the real sequences - they end in `sudo shutdown`. Tests that check them override these.
    monkeypatch.setattr(main_module, "reboot", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_module, "shutdown", lambda *args, **kwargs: None)
    # Maintenance mode blocks for good.
    monkeypatch.setattr(main_module, "run_maintenance_mode", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_module, "run_emergency_link", lambda *args, **kwargs: None)


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
            lambda cfg, reason, command_id=None, config_path=None: shutdown_calls.append(
                (cfg, reason, command_id, config_path.name),
            ),
        )

        with pytest.raises(RemoteShutdownRequested):
            main_module.main()

        assert shutdown_calls == [(config, "remote_shutdown_command", 42, "config.toml")]

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
    """An unhandled error reboots; the same one repeating (or too many reboots in a row) reboots
    into the one-shot recovery state; an error even then reboots into maintenance mode."""

    @pytest.fixture
    def failing_run(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, crash_same_error_limit=3))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        calls = []
        monkeypatch.setattr(main_module, "reboot", lambda cfg, reason: calls.append(("reboot", reason)))
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason: calls.append(("shutdown", reason)))
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

    def test_the_same_error_three_times_in_a_row_reboots_into_the_recovery_state_without_an_override(
            self, monkeypatch, failing_run):
        for _ in range(3):
            self.run_failing(monkeypatch, RuntimeError("boom"))

        assert failing_run == [
            ("reboot", "unhandled_exception"),
            ("reboot", "unhandled_exception"),
            ("reboot", "repeated_unhandled_exception"),
        ]
        assert not any(call[0] == "shutdown" for call in failing_run)

    def test_an_error_after_the_recovery_state_reboots_into_maintenance_mode(
            self, monkeypatch, failing_run):
        for _ in range(3):
            self.run_failing(monkeypatch, RuntimeError("boom"))
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: ImmediateManualModeLoop())

        self.run_failing(monkeypatch, KeyError("another"))
        assert failing_run[-1] == ("reboot", "unhandled_exception_after_recovery")

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
        assert kwargs == {"quectel_cm_process": None}

    def test_an_unrecordable_crash_hands_the_running_quectel_cm_to_maintenance_mode(
            self, monkeypatch, failing_run, tmp_path):
        """The run's own watchdog is already stopped by then; maintenance mode has to supervise
        the link itself, with the quectel-CM the watchdog last (re)started, not a second one."""
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=True))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda quectel_cm_config: "first")

        class RestartingWatchdog(FakeLinkWatchdog):
            def stop(self):
                super().stop()
                self.process = "restarted"

        monkeypatch.setattr(main_module, "LinkWatchdog", RestartingWatchdog)

        class BrokenPolicy(CrashPolicy):
            def record(self, error):
                raise OSError("disk full")

        monkeypatch.setattr(main_module, "CrashPolicy", lambda *args: BrokenPolicy(*args))

        self.run_failing(monkeypatch, RuntimeError("boom"))

        [(action, details, kwargs)] = failing_run
        assert action == "maintenance"
        assert kwargs == {"quectel_cm_process": "restarted"}

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


class TestRecoveryState:
    """After the crash escalation the next boot idles like Manual Mode - in its own run phase and
    reporting the crash details - and consumes the recovery, so the boot after is a normal one."""

    STATE = {"fingerprint": "RuntimeError@x:y", "same_error_count": 3, "consecutive_reboots": 2}

    @pytest.fixture
    def idle(self, monkeypatch, crash_dir):
        """Runs one boot; returns what the wait in the manual loop saw."""
        seen = types.SimpleNamespace(phase=None, recovery=None, escalation=None, heartbeat=None, waited=False)

        class RecordingHeartbeat:
            def __init__(self, config, backend, run_status, storage_status, config_status, run_phase, *a, **kw):
                self.run_phase = run_phase
                self.recovery = None

            def set_remote_commands(self, dispatcher):
                pass

            def set_recovery(self, details):
                self.recovery = details

            def start(self):
                pass

            def stop(self):
                pass

        class RecordingLoop:
            def wait(loop_self, on_run_selftest=None, on_check_remote_shutdown=None):
                seen.waited = True
                seen.phase = seen.heartbeat.run_phase.get()
                seen.recovery = seen.heartbeat.recovery
                seen.escalation = json.loads((crash_dir / CRASH_STATE_FILENAME).read_text())["escalation"]

            def request_start_measuring(self):
                pass

        def make_heartbeat(*args, **kwargs):
            seen.heartbeat = RecordingHeartbeat(*args, **kwargs)
            return seen.heartbeat

        monkeypatch.setattr(main_module, "HeartbeatSender", make_heartbeat)
        monkeypatch.setattr(main_module, "ManualModeLoop", RecordingLoop)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        return seen

    def write_state(self, crash_dir, escalation):
        (crash_dir / CRASH_STATE_FILENAME).write_text(json.dumps({**self.STATE, "escalation": escalation}))

    def test_idles_in_the_recovery_phase_with_the_crash_details_and_consumes_the_state(
            self, monkeypatch, tmp_path, crash_dir, idle):
        monkeypatch.setattr(main_module, "load_config", lambda path: make_config(
            tmp_path, system=SystemConfig(running_on_pi=False)))
        self.write_state(crash_dir, "recovery")

        main_module.main()

        assert idle.waited
        assert idle.phase == "recovery_idle"
        assert idle.recovery["fingerprint"] == "RuntimeError@x:y"
        assert idle.recovery["same_error_count"] == 3
        assert idle.recovery["consecutive_reboots"] == 2
        assert "traceback" not in idle.recovery
        # The backend rejects the whole heartbeat for an action longer than 64 characters.
        assert 0 < len(idle.recovery["action"]) <= 64
        assert idle.escalation == "recovery_consumed"
        assert idle.heartbeat.recovery is None

    def test_does_not_observe_movement_in_the_recovery_state(self, monkeypatch, tmp_path, crash_dir, idle):
        monkeypatch.setattr(main_module, "load_config", lambda path: make_config(
            tmp_path, system=SystemConfig(running_on_pi=False)))
        self.write_state(crash_dir, "recovery")
        gate_calls = []

        class TrackedMovementGate:
            def __init__(self, *args, **kwargs):
                pass

            def wait_for_movement(self, *args, **kwargs):
                gate_calls.append(True)

        monkeypatch.setattr(main_module, "MovementGate", TrackedMovementGate)

        main_module.main()

        assert gate_calls == []

    def test_the_boot_after_the_recovery_is_auto_mode_again(self, monkeypatch, tmp_path, crash_dir, idle):
        monkeypatch.setattr(main_module, "load_config", lambda path: make_config(
            tmp_path, system=SystemConfig(running_on_pi=False), collector=CollectorConfig(gps_enabled=True)))
        self.write_state(crash_dir, "recovery")
        main_module.main()
        idle.waited = False
        gate_calls = []

        class TrackedMovementGate:
            def __init__(self, *args, **kwargs):
                pass

            def wait_for_movement(self, *args, **kwargs):
                gate_calls.append(True)

        monkeypatch.setattr(main_module, "MovementGate", TrackedMovementGate)

        main_module.main()

        assert not idle.waited
        assert gate_calls == [True]

    def test_a_crash_after_the_recovery_ends_in_maintenance_mode_not_in_another_recovery(
            self, monkeypatch, tmp_path, crash_dir, idle):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, crash_same_error_limit=3))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        calls = []
        monkeypatch.setattr(main_module, "reboot", lambda cfg, reason: calls.append(reason))
        monkeypatch.setattr(main_module, "run_maintenance_mode", lambda cfg, details, **kw: calls.append("maintenance"))
        self.write_state(crash_dir, "recovery")

        class Failing(RaisingCollector):
            error = RuntimeError("boom")

        monkeypatch.setattr(main_module, "Collector", Failing)
        with pytest.raises(RuntimeError):
            main_module.main()
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        with pytest.raises(SystemExit):
            main_module.main()

        assert calls == ["unhandled_exception_after_recovery", "maintenance"]

    def test_a_start_command_in_the_recovery_state_leads_to_the_gps_fix_wait(
            self, monkeypatch, tmp_path, crash_dir, idle):
        monkeypatch.setattr(main_module, "load_config", lambda path: make_config(
            tmp_path, system=SystemConfig(running_on_pi=False)))
        self.write_state(crash_dir, "recovery")

        main_module.main()

        assert idle.phase == "recovery_idle"
        assert [call["phase"] for call in FakeFirstFixWaiter.instances[-1].calls] == ["waiting_for_gps_fix"]

    def test_a_deliberate_manual_mode_keeps_its_phase_and_still_consumes_the_recovery(
            self, monkeypatch, tmp_path, crash_dir, idle):
        monkeypatch.setattr(main_module, "load_config", lambda path: make_config(
            tmp_path, system=SystemConfig(running_on_pi=False, mode="manual")))
        self.write_state(crash_dir, "recovery")

        main_module.main()

        assert idle.phase == "manual_mode_idle"
        assert idle.recovery is None
        assert idle.escalation == "recovery_consumed"

    def test_a_boot_without_a_recovery_state_does_not_idle_in_auto_mode(
            self, monkeypatch, tmp_path, crash_dir, idle):
        monkeypatch.setattr(main_module, "load_config", lambda path: make_config(
            tmp_path, system=SystemConfig(running_on_pi=False)))
        self.write_state(crash_dir, "recovery_consumed")

        main_module.main()

        assert not idle.waited


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
        monkeypatch.setattr(main_module, "send_stopping_heartbeat", lambda cfg: calls.append("stopping_heartbeat"))
        monkeypatch.setattr(main_module, "finalize_and_upload_pending", lambda cfg: calls.append("finalize_and_upload"))

        with pytest.raises(main_module.TerminationRequested):
            main_module.main()

        assert calls == ["stopping_heartbeat", "finalize_and_upload"]
        assert not (crash_dir / CRASH_STATE_FILENAME).exists()

    def test_takes_the_link_down_only_after_the_heartbeat_and_the_upload(self, monkeypatch, tmp_path):
        """systemd leaves quectel-CM running on SIGTERM, so the last heartbeat and upload still
        have a link; stopping quectel-CM is then up to this process."""
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=True))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda quectel_cm_config: "first")

        class RestartingWatchdog(FakeLinkWatchdog):
            def stop(self):
                super().stop()
                self.process = "restarted"

        class Terminated(RaisingCollector):
            error = main_module.TerminationRequested()

        monkeypatch.setattr(main_module, "LinkWatchdog", RestartingWatchdog)
        monkeypatch.setattr(main_module, "Collector", Terminated)
        calls = []
        monkeypatch.setattr(main_module, "send_stopping_heartbeat", lambda cfg: calls.append("stopping_heartbeat"))
        monkeypatch.setattr(main_module, "finalize_and_upload_pending", lambda cfg: calls.append("finalize_and_upload"))
        monkeypatch.setattr(main_module, "stop_quectel_cm", lambda process: calls.append(("stop_quectel_cm", process)))

        with pytest.raises(main_module.TerminationRequested):
            main_module.main()

        assert calls == ["stopping_heartbeat", "finalize_and_upload", ("stop_quectel_cm", "restarted")]

    def test_has_no_link_to_take_down_off_the_pi(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        class Terminated(RaisingCollector):
            error = main_module.TerminationRequested()

        monkeypatch.setattr(main_module, "Collector", Terminated)
        stopped = []
        monkeypatch.setattr(main_module, "stop_quectel_cm", stopped.append)

        with pytest.raises(main_module.TerminationRequested):
            main_module.main()

        assert stopped == []

    def test_is_not_swallowed_by_generic_exception_handlers(self):
        assert not issubclass(main_module.TerminationRequested, Exception)


class TestHeartbeatLifetime:
    """One heartbeat covers the whole _run(), so every phase between link-up and the end is reported."""

    def _recording_heartbeat(self, events, phases):
        class RecordingHeartbeat:
            def __init__(self, *a, run_phase=None, **kw):
                self.run_phase = a[5] if run_phase is None and len(a) > 5 else run_phase

            def set_remote_commands(self, dispatcher):
                pass

            def start(self):
                events.append("heartbeat.start")
                phases.append(self.run_phase.get())

            def stop(self):
                events.append("heartbeat.stop")
                phases.append(self.run_phase.get())

        return RecordingHeartbeat

    def test_runs_from_link_up_through_finalizing_and_done(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=True))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        events, phases = [], []

        class TrackedCollector(SucceedingCollector):
            def collect(self):
                events.append("collect")
                return super().collect()

        monkeypatch.setattr(main_module, "HeartbeatSender", self._recording_heartbeat(events, phases))
        monkeypatch.setattr(main_module, "Collector", TrackedCollector)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda cfg: events.append("link_up"))

        main_module.main()

        assert events == ["link_up", "heartbeat.start", "collect", "heartbeat.stop"]
        assert phases == [RunPhaseState.RECOVERING_PENDING_DATA, RunPhaseState.DONE]

    def test_starts_only_once_the_link_is_up(self, monkeypatch, tmp_path):
        """quectel-CM needs a moment to bring the link up; a heartbeat sent right away fails."""
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=True))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        events, phases = [], []

        class WaitingWatchdog(FakeLinkWatchdog):
            def wait_for_link(self, timeout_s):
                events.append(("wait_for_link", timeout_s))
                return False

        monkeypatch.setattr(main_module, "LinkWatchdog", WaitingWatchdog)
        monkeypatch.setattr(main_module, "HeartbeatSender", self._recording_heartbeat(events, phases))
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda cfg: events.append("link_up"))

        main_module.main()

        assert events[:3] == ["link_up", ("wait_for_link", LINK_UP_WAIT_S), "heartbeat.start"]

    def test_does_not_wait_for_a_link_off_the_pi(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        main_module.main()

        [watchdog] = FakeLinkWatchdog.instances
        assert not hasattr(watchdog, "waited_for_link")

    def test_is_stopped_when_the_run_raises(self, monkeypatch, tmp_path, crash_dir):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        events, phases = [], []
        monkeypatch.setattr(main_module, "HeartbeatSender", self._recording_heartbeat(events, phases))
        monkeypatch.setattr(main_module, "Collector", RaisingCollector)
        monkeypatch.setattr(main_module, "reboot", lambda *a, **kw: None)

        with pytest.raises(RuntimeError):
            main_module.main()

        assert events == ["heartbeat.start", "heartbeat.stop"]


class TestRunAsService:
    """What `python -m measurement_software.main` runs around main()."""

    def run_with(self, monkeypatch, outcome):
        def fake_main():
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(main_module, "main", fake_main)
        main_module._run_as_service()

    @pytest.mark.parametrize("error", [RemoteRebootRequested(7), RemoteShutdownRequested(8)])
    def test_a_remote_reboot_or_shutdown_is_not_logged_as_a_critical_error(self, monkeypatch, caplog, error):
        """main() has already run the reboot or shutdown and logged why; it only re-raises to end."""
        caplog.set_level(logging.INFO)

        self.run_with(monkeypatch, error)

        assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]

    def test_sigterm_ends_quietly(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)

        self.run_with(monkeypatch, main_module.TerminationRequested())

        assert "Stopped by SIGTERM." in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]

    def test_any_other_error_is_logged_as_critical(self, monkeypatch, caplog):
        self.run_with(monkeypatch, RuntimeError("boom"))

        assert [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL] == ["Critical error: boom"]

    def test_a_completed_run_goes_on_to_the_shutdown(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        calls = []
        monkeypatch.setattr(main_module, "_shutdown_after_completed_run", calls.append)

        self.run_with(monkeypatch, config)

        assert calls == [config]


class TestShutdownAfterCompletedRun:
    def test_goes_through_the_unified_shutdown_path_on_the_pi(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=True))
        calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason, **kw: calls.append(reason))

        main_module._shutdown_after_completed_run(config)

        assert calls == ["run_completed"]

    def test_does_nothing_off_the_pi(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False))
        calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda *a, **kw: calls.append("shutdown"))

        main_module._shutdown_after_completed_run(config)

        assert calls == []


class TestLinkWatchdogWiring:
    def test_watches_the_started_quectel_cm_and_stops_with_the_run(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=True))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        process = object()
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda quectel_cm_config: process)

        main_module.main()

        [watchdog] = FakeLinkWatchdog.instances
        assert watchdog.started_with is process
        assert watchdog.stopped

    def test_syncs_the_clock_on_link_up_only_with_an_ntp_server_configured(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        for server, expect_hook in (("", False), ("192.0.2.10", True)):
            FakeLinkWatchdog.instances = []
            config = make_config(tmp_path, clock=ClockConfig(ntp_server=server))
            monkeypatch.setattr(main_module, "load_config", lambda path, config=config: config)

            main_module.main()

            [watchdog] = FakeLinkWatchdog.instances
            assert (watchdog.on_link_up is not None) is expect_hook

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
        monkeypatch.setattr(main_module, "shutdown", lambda cfg, reason, **kwargs: None)
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
    """Upload attempts bracket collect(): once on entering ACTIVE_MEASURING, once after
    finalizing - both points where the connection is already up for another reason."""

    def test_first_upload_happens_after_the_movement_wait_not_at_the_start(self, monkeypatch, tmp_path):
        config = make_config(
            tmp_path,
            collector=CollectorConfig(gps_enabled=True),
            system=SystemConfig(running_on_pi=True),
        )
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

        class FakeMovementGate:
            def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
                pass

            def wait_for_movement(self, reference, on_check_remote_shutdown=None):
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

            def wait_for_movement(self, reference, on_check_remote_shutdown=None):
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

    def test_runs_with_the_idle_end(self, monkeypatch, tmp_path, caplog):
        FakeCollector.instances = []

        self._run(monkeypatch, tmp_path, enabled=False, caplog=caplog)

        [collector] = FakeCollector.instances
        assert collector.idle_end_enabled is True


class TestAutoModeHeartbeatFromBoot:
    """Auto Mode (the default) now opens quectel-CM and starts the heartbeat immediately at
    boot, the same way Manual Mode already does, but still transitions automatically into
    active measuring once MovementGate confirms movement."""

    def test_opens_quectel_cm_and_heartbeat_before_waiting_for_movement(self, monkeypatch, tmp_path):
        config = make_config(
            tmp_path,
            collector=CollectorConfig(gps_enabled=True),
            system=SystemConfig(running_on_pi=True),
        )
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

        class TrackedCollector(SucceedingCollector):
            def collect(self):
                order.append("collector.collect")
                return super().collect()

        class TrackedMovementGate:
            def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
                pass

            def wait_for_movement(self, reference, on_check_remote_shutdown=None):
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
            "collector.collect",
            "heartbeat.stop",
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

            def wait_for_movement(self, reference, on_check_remote_shutdown=None):
                order.append("movement_gate.wait_for_movement")

        monkeypatch.setattr(main_module, "HeartbeatSender", RecordingHeartbeat)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "MovementGate", TrackedMovementGate)

        main_module.main()

        assert order == ["heartbeat.start", "movement_gate.wait_for_movement", "heartbeat.stop"]


class TestManualMode:
    """system.mode="manual" skips the movement gate entirely: it opens quectel-CM and the
    heartbeat immediately, blocks in ManualModeLoop.wait() until start-measuring-now, then
    transitions into the same measuring flow Auto Mode uses."""

    def test_opens_quectel_cm_and_heartbeat_before_idling_then_measures(self, monkeypatch, tmp_path):
        config = make_config(
            tmp_path,
            system=SystemConfig(running_on_pi=True, mode="manual"),
        )
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

        class TrackedCollector(SucceedingCollector):
            def collect(self):
                order.append("collector.collect")
                return super().collect()

        monkeypatch.setattr(main_module, "HeartbeatSender", RecordingHeartbeat)
        monkeypatch.setattr(main_module, "Collector", TrackedCollector)
        monkeypatch.setattr(main_module, "start_quectel_cm", lambda config: order.append("start_quectel_cm"))

        class ImmediateManualModeLoop:
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
                order.append("manual_mode_loop.wait")

            def request_start_measuring(self):
                pass

        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: ImmediateManualModeLoop())

        main_module.main()

        assert order == [
            "start_quectel_cm",
            "heartbeat.start",
            "manual_mode_loop.wait",
            "collector.collect",
            "heartbeat.stop",
        ]

    def test_does_not_touch_movement_gate_in_manual_mode(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="manual"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        movement_gate_calls = []

        class TrackedMovementGate:
            def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
                pass

            def wait_for_movement(self, reference, on_check_remote_shutdown=None):
                movement_gate_calls.append(True)

        class ImmediateManualModeLoop:
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
                pass

            def request_start_measuring(self):
                pass

        monkeypatch.setattr(main_module, "MovementGate", TrackedMovementGate)
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: ImmediateManualModeLoop())

        main_module.main()

        assert movement_gate_calls == []

    def test_runs_without_the_idle_end(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="manual"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: ImmediateManualModeLoop())
        FakeCollector.instances = []

        main_module.main()

        [collector] = FakeCollector.instances
        assert collector.idle_end_enabled is False

    def test_auto_mode_is_unchanged_default(self, tmp_path):
        config = make_config(tmp_path)

        assert config.system.mode == "auto"

    def test_run_selftest_now_callback_runs_every_check_with_no_side_effects(self, monkeypatch, tmp_path):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="manual"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        run_checks_calls = []

        def fake_run_checks(cfg, names, *, reporter):
            run_checks_calls.append((cfg, names, reporter))
            return []

        monkeypatch.setattr(main_module, "run_checks", fake_run_checks)
        monkeypatch.setattr(main_module, "available_checks", lambda: ["modem", "gps"])
        monkeypatch.setattr(main_module, "SelftestResultsReporter", lambda *a, **kw: "reporter")

        class SelftestTriggeringManualModeLoop:
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
                on_run_selftest()

            def request_start_measuring(self):
                pass

        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: SelftestTriggeringManualModeLoop())

        main_module.main()

        assert len(run_checks_calls) == 1
        cfg, names, reporter = run_checks_calls[0]
        assert cfg is config
        assert names == ["modem", "gps"]
        assert reporter == "reporter"

    def test_stops_any_in_progress_gps_fix_test_before_the_fix_wait_and_measuring(self, monkeypatch, tmp_path):
        """A running gps-fix-test holds its own GNSS connection open - it has to be stopped
        before the fix wait and Collector.collect() open their own GNSSReceiver on the same
        serial port."""
        config = make_config(
            tmp_path, system=SystemConfig(running_on_pi=False, mode="manual"),
            collector=CollectorConfig(gps_enabled=True),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: config)

        order = []

        class TrackedCollector(SucceedingCollector):
            def collect(self):
                order.append("collector.collect")
                return super().collect()

        class TrackedFirstFixWaiter(FakeFirstFixWaiter):
            def wait(self, check_remote_commands, timeout_s):
                order.append("first_fix_waiter.wait")
                return super().wait(check_remote_commands, timeout_s)

        class ImmediateManualModeLoop:
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
                order.append("manual_mode_loop.wait")

            def request_start_measuring(self):
                pass

        class TrackedGpsFixTestRunner:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                pass

            def stop(self):
                order.append("gps_fix_test_runner.stop")

        monkeypatch.setattr(main_module, "Collector", TrackedCollector)
        monkeypatch.setattr(main_module, "FirstFixWaiter", TrackedFirstFixWaiter)
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: ImmediateManualModeLoop())
        monkeypatch.setattr(main_module, "GpsFixTestRunner", TrackedGpsFixTestRunner)

        main_module.main()

        assert order == [
            "manual_mode_loop.wait",
            "gps_fix_test_runner.stop",
            "first_fix_waiter.wait",
            "collector.collect",
            "gps_fix_test_runner.stop",
        ]


class TrackingManualModeLoop:
    """Returns from every wait() at once; records waits and cancelled start requests."""

    def __init__(self):
        self.waits = 0
        self.cancelled_starts = 0

    def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
        self.waits += 1

    def request_start_measuring(self):
        pass

    def cancel_start_request(self):
        self.cancelled_starts += 1


class RecordingUploader:
    uploads = 0

    def __init__(self, config, backend, device_id=""):
        pass

    def upload_pending_files(self):
        RecordingUploader.uploads += 1


class CollectCountingCollector(SucceedingCollector):
    collects = 0

    def collect(self):
        CollectCountingCollector.collects += 1
        return super().collect()


@pytest.fixture
def counting(monkeypatch):
    CollectCountingCollector.collects = 0
    RecordingUploader.uploads = 0
    monkeypatch.setattr(main_module, "Collector", CollectCountingCollector)
    monkeypatch.setattr(main_module, "Uploader", RecordingUploader)


def _record_gate_waits(monkeypatch):
    waits = []

    class RecordingMovementGate:
        def __init__(self, gnss_receiver, config, run_phase, clock_sync, gps_fix_status):
            pass

        def wait_for_movement(self, reference, on_check_remote_shutdown=None):
            waits.append(reference)

    monkeypatch.setattr(main_module, "MovementGate", RecordingMovementGate)
    return waits


class TestFirstFixWaitOnEveryPath:
    """Every path into measuring waits for a GPS fix first, in its own phase, before the
    movement gate and before Collector.collect() - see FirstFixWaiter."""

    def test_auto_mode_waits_for_the_fix_before_the_gate_and_hands_the_fix_to_it(
        self, monkeypatch, tmp_path, counting,
    ):
        config = make_config(tmp_path, collector=CollectorConfig(gps_enabled=True, max_wait_for_first_fix=123))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        gate_waits = _record_gate_waits(monkeypatch)

        main_module.main()

        [waiter] = FakeFirstFixWaiter.instances
        assert [c["timeout_s"] for c in waiter.calls] == [123]
        assert gate_waits == [FIRST_FIX.position]
        assert CollectCountingCollector.collects == 1

    def test_auto_mode_without_the_movement_gate_still_waits_for_the_fix(self, monkeypatch, tmp_path, counting):
        config = make_config(
            tmp_path, collector=CollectorConfig(gps_enabled=True),
            movement_gate=MovementGateConfig(enabled=False),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        gate_waits = _record_gate_waits(monkeypatch)

        main_module.main()

        [waiter] = FakeFirstFixWaiter.instances
        assert len(waiter.calls) == 1
        assert gate_waits == []
        assert CollectCountingCollector.collects == 1

    def test_manual_mode_waits_for_the_fix_after_the_start_command_without_a_timeout(
        self, monkeypatch, tmp_path, counting,
    ):
        config = make_config(
            tmp_path, system=SystemConfig(running_on_pi=False, mode="manual"),
            collector=CollectorConfig(gps_enabled=True, max_wait_for_first_fix=123),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        manual_mode_loop = TrackingManualModeLoop()
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: manual_mode_loop)
        gate_waits = _record_gate_waits(monkeypatch)

        main_module.main()

        [waiter] = FakeFirstFixWaiter.instances
        assert [c["timeout_s"] for c in waiter.calls] == [None]
        assert manual_mode_loop.waits == 1
        assert gate_waits == []
        assert CollectCountingCollector.collects == 1

    def test_with_gps_disabled_the_real_wait_passes_at_once(self, monkeypatch, tmp_path, counting):
        """NullGNSSReceiver reports a placeholder fix immediately, so the wait costs nothing."""
        config = make_config(tmp_path, collector=CollectorConfig(gps_enabled=False))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "FirstFixWaiter", RealFirstFixWaiter)

        main_module.main()

        assert CollectCountingCollector.collects == 1

    def test_the_wait_reports_its_own_phase_before_measuring_begins(self, monkeypatch, tmp_path, counting):
        """The phase is set by the wait itself; ACTIVE_MEASURING only after it returned a fix."""
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        phases = []

        class PhaseRecordingWaiter(FakeFirstFixWaiter):
            def wait(self, check_remote_commands, timeout_s):
                phases.append(self.run_phase.get())
                return super().wait(check_remote_commands, timeout_s)

        class PhaseRecordingCollector(CollectCountingCollector):
            def collect(self):
                phases.append(PhaseRecordingWaiter.instances[-1].run_phase.get())
                return super().collect()

        monkeypatch.setattr(main_module, "FirstFixWaiter", PhaseRecordingWaiter)
        monkeypatch.setattr(main_module, "Collector", PhaseRecordingCollector)

        main_module.main()

        assert phases == [RunPhaseState.RECOVERING_PENDING_DATA, RunPhaseState.ACTIVE_MEASURING]

    def test_the_wait_checks_shutdown_and_reboot_in_auto_mode_but_also_stop_in_manual_mode(
        self, monkeypatch, tmp_path, counting,
    ):
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: TrackingManualModeLoop())
        checks = {}
        for mode in ("auto", "manual"):
            FakeFirstFixWaiter.instances = []
            config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode=mode))
            monkeypatch.setattr(main_module, "load_config", lambda path, config=config: config)

            main_module.main()

            checks[mode] = FakeFirstFixWaiter.instances[0].calls[0]["check"].__name__

        assert checks == {
            "auto": "check_remote_shutdown_or_reboot",
            "manual": "check_remote_commands_before_measuring",
        }


class TestNoFixInAutoMode:
    def test_a_timeout_ends_the_cycle_without_a_run(self, monkeypatch, tmp_path, counting):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        FakeFirstFixWaiter.outcomes = [None]
        finalized = []
        monkeypatch.setattr(main_module, "finalize", finalized.append)

        returned = main_module.main()

        assert returned is config
        assert CollectCountingCollector.collects == 0
        assert finalized == []

    def test_uploads_only_in_the_finalizing_phase_when_there_is_no_run(self, monkeypatch, tmp_path, counting):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        FakeFirstFixWaiter.outcomes = [None]
        phases_at_upload = []

        class PhaseRecordingUploader(RecordingUploader):
            def upload_pending_files(self):
                phases_at_upload.append(FakeFirstFixWaiter.instances[-1].run_phase.get())

        monkeypatch.setattr(main_module, "Uploader", PhaseRecordingUploader)

        main_module.main()

        assert phases_at_upload == [RunPhaseState.FINALIZING]

    def test_the_cycle_ends_in_done_so_the_device_shuts_down_as_after_any_run(
        self, monkeypatch, tmp_path, counting,
    ):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        FakeFirstFixWaiter.outcomes = [None]

        main_module.main()

        assert FakeFirstFixWaiter.instances[-1].run_phase.get() == RunPhaseState.DONE

    def test_a_timeout_never_starts_the_gate(self, monkeypatch, tmp_path, counting):
        config = make_config(tmp_path, collector=CollectorConfig(gps_enabled=True))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        FakeFirstFixWaiter.outcomes = [None]
        gate_waits = _record_gate_waits(monkeypatch)

        main_module.main()

        assert gate_waits == []


class TestCommandsDuringTheFirstFixWait:
    def test_manual_mode_stop_measuring_returns_to_the_manual_idle_and_waits_for_the_next_start(
        self, monkeypatch, tmp_path, counting,
    ):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="manual"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        manual_mode_loop = TrackingManualModeLoop()
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: manual_mode_loop)
        FakeFirstFixWaiter.outcomes = [StopMeasuringRequested(), FIRST_FIX]
        finalized = []
        monkeypatch.setattr(main_module, "finalize", finalized.append)

        main_module.main()

        # Back in the idle wait after the abort - no shutdown, no finalize, no run yet.
        assert manual_mode_loop.waits == 2
        assert manual_mode_loop.cancelled_starts == 1
        assert CollectCountingCollector.collects == 1
        assert len(finalized) == 1  # only the one run that actually started afterwards

    def test_manual_mode_reports_the_idle_phase_again_after_the_abort(self, monkeypatch, tmp_path, counting):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="manual"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        phases_at_wait = []

        class PhaseRecordingLoop(TrackingManualModeLoop):
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
                phases_at_wait.append(FakeFirstFixWaiter.instances[-1].run_phase.get())
                super().wait(on_run_selftest, on_check_remote_shutdown)

        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: PhaseRecordingLoop())
        FakeFirstFixWaiter.outcomes = [StopMeasuringRequested(), FIRST_FIX]

        main_module.main()

        assert phases_at_wait[1] == RunPhaseState.MANUAL_MODE_IDLE

    def test_manual_mode_stop_measuring_never_shuts_the_device_down(self, monkeypatch, tmp_path, counting):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode="manual"))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: TrackingManualModeLoop())
        FakeFirstFixWaiter.outcomes = [StopMeasuringRequested(), FIRST_FIX]
        shutdown_calls = []
        monkeypatch.setattr(main_module, "shutdown", lambda *a, **kw: shutdown_calls.append(a))

        main_module.main()

        assert shutdown_calls == []

    @pytest.mark.parametrize("mode", ["auto", "manual"])
    def test_shutdown_now_during_the_wait_shuts_down_in_either_mode(
        self, monkeypatch, tmp_path, counting, mode,
    ):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode=mode))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: TrackingManualModeLoop())
        FakeFirstFixWaiter.outcomes = [RemoteShutdownRequested(42)]
        shutdown_calls = []
        monkeypatch.setattr(
            main_module, "shutdown",
            lambda cfg, reason, command_id=None, **kwargs: shutdown_calls.append((reason, command_id)),
        )

        with pytest.raises(RemoteShutdownRequested):
            main_module.main()

        assert shutdown_calls == [("remote_shutdown_command", 42)]
        assert CollectCountingCollector.collects == 0

    @pytest.mark.parametrize("mode", ["auto", "manual"])
    def test_reboot_now_during_the_wait_reboots_in_either_mode(self, monkeypatch, tmp_path, counting, mode):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode=mode))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: TrackingManualModeLoop())
        FakeFirstFixWaiter.outcomes = [RemoteRebootRequested(43)]
        reboot_calls = []
        monkeypatch.setattr(
            main_module, "reboot",
            lambda cfg, reason, command_id=None, config_path=None: reboot_calls.append((reason, command_id)),
        )

        with pytest.raises(RemoteRebootRequested):
            main_module.main()

        assert reboot_calls == [("remote_reboot_command", 43)]
        assert CollectCountingCollector.collects == 0


class CapturingRemoteCommandDispatcher:
    """Records the collaborators main() hands the dispatcher."""

    kwargs: dict = {}

    def __init__(self, **kwargs):
        CapturingRemoteCommandDispatcher.kwargs = kwargs

    def seed_success_ack(self, command_id, command_name):
        pass


class TestDispatcherWiring:
    def _run(self, monkeypatch, tmp_path, mode, gps_enabled):
        config = make_config(
            tmp_path, system=SystemConfig(running_on_pi=False, mode=mode),
            collector=CollectorConfig(gps_enabled=gps_enabled),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "RemoteCommandDispatcher", CapturingRemoteCommandDispatcher)
        monkeypatch.setattr(
            main_module.ManualModeLoop, "wait", lambda self, **kwargs: None,
        )
        monkeypatch.setattr(main_module.MovementGate, "wait_for_movement", lambda self, **kwargs: None)
        main_module.main()
        return CapturingRemoteCommandDispatcher.kwargs

    def test_dispatcher_decides_by_the_run_phase(self, monkeypatch, tmp_path):
        kwargs = self._run(monkeypatch, tmp_path, mode="manual", gps_enabled=True)

        assert isinstance(kwargs["run_phase"], main_module.RunPhase)

    def test_no_gps_fix_test_runner_without_gps(self, monkeypatch, tmp_path):
        kwargs = self._run(monkeypatch, tmp_path, mode="manual", gps_enabled=False)

        assert kwargs["gps_fix_test_runner"] is None

    def test_gps_fix_test_runner_in_manual_mode_with_gps(self, monkeypatch, tmp_path):
        kwargs = self._run(monkeypatch, tmp_path, mode="manual", gps_enabled=True)

        assert isinstance(kwargs["gps_fix_test_runner"], main_module.GpsFixTestRunner)


class SpyRemoteCommandDispatcher:
    """Stands in for the real RemoteCommandDispatcher so tests can observe seed_success_ack()
    calls without constructing the real collaborators it needs."""

    seeded: list = []

    def __init__(self, **kwargs):
        pass

    def seed_success_ack(self, command_id, command_name):
        self.seeded.append((command_name, command_id))


class TestMainPendingRebootAck:
    def test_seeds_the_dispatcher_when_a_pending_reboot_ack_was_persisted(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(
            main_module, "read_and_clear_pending_command_ack",
            lambda path: 52 if path.name == "pending_reboot_ack.toml" else None,
        )
        seeded: list = []
        monkeypatch.setattr(SpyRemoteCommandDispatcher, "seeded", seeded)
        monkeypatch.setattr(main_module, "RemoteCommandDispatcher", SpyRemoteCommandDispatcher)

        main_module.main()

        assert seeded == [("reboot-now", 52)]

    def test_seeds_a_pending_shutdown_ack_too(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(
            main_module, "read_and_clear_pending_command_ack",
            lambda path: 61 if path.name == "pending_shutdown_ack.toml" else None,
        )
        seeded: list = []
        monkeypatch.setattr(SpyRemoteCommandDispatcher, "seeded", seeded)
        monkeypatch.setattr(main_module, "RemoteCommandDispatcher", SpyRemoteCommandDispatcher)

        main_module.main()

        assert seeded == [("shutdown-now", 61)]

    def test_does_not_seed_when_nothing_was_persisted(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)
        monkeypatch.setattr(main_module, "read_and_clear_pending_command_ack", lambda path: None)
        seeded: list = []
        monkeypatch.setattr(SpyRemoteCommandDispatcher, "seeded", seeded)
        monkeypatch.setattr(main_module, "RemoteCommandDispatcher", SpyRemoteCommandDispatcher)

        main_module.main()

        assert seeded == []


class TestErrorsBeforeRun:
    """An error before _run() must still leave the device reachable: the service doesn't restart,
    so without a cellular link nobody could reach it until someone is on site."""

    SECRET = "hmac-secret-from-config-local"

    @pytest.fixture
    def entered(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main_module, "run_maintenance_mode",
                            lambda config, details, **kwargs: calls.append(("maintenance", details)))
        monkeypatch.setattr(main_module, "run_emergency_link",
                            lambda config_path: calls.append(("emergency_link", config_path.name)))
        return calls

    def fail(self, monkeypatch, name, error):
        def raising(*args, **kwargs):
            raise error
        monkeypatch.setattr(main_module, name, raising)

    def test_an_unloadable_config_brings_up_the_emergency_link(self, monkeypatch, entered, caplog):
        self.fail(monkeypatch, "load_config", ValueError(f"bad value {self.SECRET!r}"))
        caplog.set_level(logging.CRITICAL)

        with pytest.raises(SystemExit):
            main_module.main()

        assert entered == [("emergency_link", "config.toml")]
        assert "load_config" in caplog.text
        assert "ValueError" in caplog.text
        assert "config.local.toml" in caplog.text
        assert self.SECRET not in caplog.text

    def test_a_loaded_config_keeps_its_link_settings_for_the_emergency_link(self, monkeypatch, tmp_path):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        main_module.main()

        assert saved_link_settings == [(config, "config.toml")]

    def test_sigterm_is_handled_on_the_emergency_link(self, monkeypatch):
        """Without the handler, SIGTERM would end the process at once, and the emergency link's
        quectel-CM would only be killed by systemd instead of taken down."""
        self.fail(monkeypatch, "load_config", ValueError("broken"))
        handlers = []
        monkeypatch.setattr(main_module, "run_emergency_link",
                            lambda config_path: handlers.append(signal.getsignal(signal.SIGTERM)))

        with pytest.raises(SystemExit):
            main_module.main()

        assert handlers == [main_module.handle_sigterm]

    def test_a_failing_migration_brings_up_the_emergency_link(self, monkeypatch, entered):
        self.fail(monkeypatch, "migrate_legacy_system_mode", OSError("read-only file system"))

        with pytest.raises(SystemExit):
            main_module.main()

        assert entered == [("emergency_link", "config.toml")]

    def test_an_error_after_the_config_loaded_is_recorded_and_enters_maintenance_mode(
            self, monkeypatch, tmp_path, entered, crash_dir, caplog):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        self.fail(monkeypatch, "read_and_clear_pending_command_ack", OSError("disk gone"))
        caplog.set_level(logging.CRITICAL)

        with pytest.raises(SystemExit):
            main_module.main()

        [(action, details)] = entered
        assert action == "maintenance"
        assert details["fingerprint"].startswith("OSError@")
        assert details["same_error_count"] == 1
        assert (crash_dir / CRASH_STATE_FILENAME).exists()
        assert "read_and_clear_pending_command_ack" in caplog.text
        assert "disk gone" in caplog.text

    def test_a_failing_logging_setup_still_enters_maintenance_mode(self, monkeypatch, tmp_path, entered):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        self.fail(monkeypatch, "setup_logging", PermissionError("log dir"))

        with pytest.raises(SystemExit):
            main_module.main()

        assert [call[0] for call in entered] == ["maintenance"]

    def test_a_crash_policy_that_cannot_be_built_still_enters_maintenance_mode(self, monkeypatch, tmp_path, entered):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        self.fail(monkeypatch, "CrashPolicy", OSError("no state dir"))

        with pytest.raises(SystemExit):
            main_module.main()

        [(action, details)] = entered
        assert action == "maintenance"
        assert details == {"fingerprint": details["fingerprint"]}
        assert details["fingerprint"].startswith("OSError@")

    def test_nothing_is_measured_after_an_early_error(self, monkeypatch, tmp_path, entered):
        config = make_config(tmp_path)
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        self.fail(monkeypatch, "read_and_clear_pending_command_ack", OSError("disk gone"))

        class MustNotRun(SucceedingCollector):
            def __init__(self, *args, **kwargs):
                raise AssertionError("an early error must not build the measuring pipeline")

        monkeypatch.setattr(main_module, "Collector", MustNotRun)

        with pytest.raises(SystemExit):
            main_module.main()

    def test_an_unloadable_config_ends_with_a_supervised_link(self, monkeypatch):
        """End to end through the real emergency link, with the repository's own config.toml."""
        self.fail(monkeypatch, "load_config", ValueError("broken"))
        started, watchdogs = [], []

        class RecordingWatchdog:
            def __init__(self, config, quectel_cm_config, request_radio_reset):
                watchdogs.append(self)

            def start(self, process):
                self.process = process

        stopped = types.SimpleNamespace(wait=lambda timeout=None: True)
        monkeypatch.setattr(maintenance_mode, "start_quectel_cm", lambda config: started.append(config) or "process")
        monkeypatch.setattr(maintenance_mode, "LinkWatchdog", RecordingWatchdog)
        monkeypatch.setattr(main_module, "run_emergency_link",
                            lambda config_path: maintenance_mode.run_emergency_link(config_path, stopped))

        with pytest.raises(SystemExit):
            main_module.main()

        [quectel_cm] = started
        assert quectel_cm.binary.endswith("vendor/quectel-CM/quectel-CM")
        assert quectel_cm.args == ("-s", "web.vodafone.de")
        assert watchdogs[0].process == "process"


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

    def test_migrates_a_legacy_system_mode_after_the_mode_override_and_before_loading_config(
        self, monkeypatch, tmp_path,
    ):
        config = make_config(tmp_path)
        order = []
        monkeypatch.setattr(
            main_module, "migrate_legacy_mode_override",
            lambda overrides_path, legacy_path: order.append("migrate_mode_override"),
        )
        monkeypatch.setattr(
            main_module, "migrate_legacy_system_mode",
            lambda overrides_path: order.append(("migrate_system_mode", overrides_path.name)),
        )
        monkeypatch.setattr(main_module, "load_config", lambda path: order.append("load_config") or config)
        monkeypatch.setattr(main_module, "Collector", SucceedingCollector)

        main_module.main()

        assert order == [
            "migrate_mode_override", ("migrate_system_mode", "config_overrides.toml"), "load_config",
        ]


class TestManualNextBootMarker:
    """stop-measuring-now's one-shot marker puts exactly one boot into Manual Mode."""

    @pytest.fixture
    def marker(self, monkeypatch, tmp_path):
        path = tmp_path / "manual_next_boot.json"
        # config_path.parent / <absolute path> is that absolute path: keeps tests off the repo dir.
        monkeypatch.setattr(main_module, "MANUAL_NEXT_BOOT_FILENAME", str(path))
        return path

    def run_main(self, monkeypatch, tmp_path, mode, collector_cls=SucceedingCollector):
        config = make_config(tmp_path, system=SystemConfig(running_on_pi=False, mode=mode))
        monkeypatch.setattr(main_module, "load_config", lambda path: config)
        monkeypatch.setattr(main_module, "Collector", collector_cls)
        waits = []

        class RecordingManualModeLoop(ImmediateManualModeLoop):
            def wait(self, on_run_selftest=None, on_check_remote_shutdown=None):
                waits.append(True)

        monkeypatch.setattr(main_module, "ManualModeLoop", lambda: RecordingManualModeLoop())
        try:
            main_module.main()
        except Exception:
            pass
        return config, waits

    def test_an_auto_mode_boot_with_the_marker_runs_in_manual_mode_and_consumes_it(
        self, monkeypatch, tmp_path, marker,
    ):
        write_manual_next_boot_marker(marker)

        config, waits = self.run_main(monkeypatch, tmp_path, "auto")

        assert config.system.mode == "manual"
        assert waits == [True]
        assert not marker.exists()

    def test_the_boot_after_that_is_back_in_auto_mode(self, monkeypatch, tmp_path, marker):
        write_manual_next_boot_marker(marker)
        self.run_main(monkeypatch, tmp_path, "auto")

        config, waits = self.run_main(monkeypatch, tmp_path, "auto")

        assert config.system.mode == "auto"
        assert waits == []

    def test_a_crash_before_manual_mode_is_entered_keeps_the_marker_for_the_next_boot(
        self, monkeypatch, tmp_path, marker,
    ):
        write_manual_next_boot_marker(marker)
        monkeypatch.setattr(main_module, "recover_unfinalized", lambda d: (_ for _ in ()).throw(RuntimeError("boom")))

        _, waits = self.run_main(monkeypatch, tmp_path, "auto")

        assert waits == []
        assert marker.exists()

    def test_a_crash_between_stop_and_run_end_still_gives_exactly_one_manual_boot(
        self, monkeypatch, tmp_path, marker,
    ):
        # stop-measuring-now left the marker, then the process died before the run ended: no
        # persistent override exists, only the marker.
        write_manual_next_boot_marker(marker)

        first, first_waits = self.run_main(monkeypatch, tmp_path, "auto")
        second, second_waits = self.run_main(monkeypatch, tmp_path, "auto")

        assert (first.system.mode, first_waits) == ("manual", [True])
        assert (second.system.mode, second_waits) == ("auto", [])

    def test_a_persistent_manual_mode_is_untouched_and_the_marker_is_dropped(
        self, monkeypatch, tmp_path, marker,
    ):
        write_manual_next_boot_marker(marker)

        config, waits = self.run_main(monkeypatch, tmp_path, "manual")

        assert config.system.mode == "manual"
        assert waits == [True]
        assert not marker.exists()

    def test_an_invalid_marker_never_switches_to_manual_mode(self, monkeypatch, tmp_path, marker):
        marker.write_text("garbage")

        config, waits = self.run_main(monkeypatch, tmp_path, "auto")

        assert config.system.mode == "auto"
        assert waits == []
        assert not marker.exists()
