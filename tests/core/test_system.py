import logging
import subprocess
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from measurement_software.core import system
from measurement_software.core.config_sources import read_and_clear_pending_command_ack
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
from measurement_software.modems.modem import CellSample, Modem


class FakeCheckOutput:
    """Fakes subprocess.check_output for `ip -4 addr show <iface>`, keyed by iface."""

    def __init__(self, outputs: dict[str, bytes] | None = None, errors: dict[str, Exception] | None = None):
        self.outputs = outputs or {}
        self.errors = errors or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs) -> bytes:
        self.calls.append(cmd)
        iface = cmd[-1]
        if iface in self.errors:
            raise self.errors[iface]
        return self.outputs.get(iface, b"")


@pytest.fixture
def check_output(monkeypatch) -> FakeCheckOutput:
    fake = FakeCheckOutput()
    monkeypatch.setattr("measurement_software.core.system.subprocess.check_output", fake)
    return fake


class TestLogIpAddrs:
    def test_logs_ipv4_address_when_present(self, check_output, caplog):
        caplog.set_level(logging.INFO)
        check_output.outputs["wlan0"] = (
            b"2: wlan0    inet 10.0.0.5/24 brd 10.0.0.255 scope global wlan0"
        )

        system.log_ip_addrs(("wlan0",))

        assert "wlan0: 10.0.0.5/24" in caplog.text

    def test_logs_no_ipv4_address_when_absent(self, check_output, caplog):
        caplog.set_level(logging.INFO)
        check_output.outputs["wlan0"] = b"2: wlan0: <BROADCAST> state DOWN\n    link/ether aa:bb:cc:dd:ee:ff"

        system.log_ip_addrs(("wlan0",))

        assert "wlan0: no IPv4 address" in caplog.text

    def test_logs_warning_when_command_fails(self, check_output, caplog):
        caplog.set_level(logging.WARNING)
        check_output.errors["wwan0"] = subprocess.CalledProcessError(1, ["ip"])

        system.log_ip_addrs(("wwan0",))

        assert "wwan0: error querying IP address" in caplog.text

    def test_logs_warning_when_ip_command_missing(self, check_output, caplog):
        caplog.set_level(logging.WARNING)
        check_output.errors["wwan0"] = FileNotFoundError("ip: command not found")

        system.log_ip_addrs(("wwan0",))

        assert "wwan0: error querying IP address" in caplog.text

    def test_queries_each_interface_independently(self, check_output, caplog):
        caplog.set_level(logging.INFO)
        check_output.outputs["wlan0"] = b"inet 10.0.0.5/24"
        check_output.outputs["wwan0"] = b"inet 10.1.0.9/24"

        system.log_ip_addrs(("wlan0", "wwan0"))

        assert [c[-1] for c in check_output.calls] == ["wlan0", "wwan0"]
        assert "wlan0: 10.0.0.5/24" in caplog.text
        assert "wwan0: 10.1.0.9/24" in caplog.text

    def test_default_interfaces_are_wlan0_and_wwan0(self, check_output):
        system.log_ip_addrs()

        assert [c[-1] for c in check_output.calls] == ["wlan0", "wwan0"]


class FakeModem(Modem):
    def __init__(self, fail_at: str | None = None):
        self.fail_at = fail_at
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")
        if self.fail_at == "open":
            raise RuntimeError("open failed")

    def close(self) -> None:
        self.calls.append("close")
        if self.fail_at == "close":
            raise RuntimeError("close failed")

    def unlock_sim(self) -> None:
        self.calls.append("unlock_sim")

    def query_cell_info(self) -> list[CellSample]:
        return []

    def reset_radio(self) -> None:
        self.calls.append("reset_radio")

    def power_down(self) -> None:
        self.calls.append("power_down")
        if self.fail_at == "power_down":
            raise RuntimeError("power_down failed")


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("measurement_software.core.system.time.sleep", lambda seconds: None)


@pytest.fixture
def run(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "measurement_software.core.system.subprocess.run",
        lambda cmd, **kwargs: calls.append(cmd),
    )
    return calls


def modem_config() -> ModemConfig:
    return ModemConfig(type="quectel", port="/dev/ttyUSB0", baud_rate=115200, timeout=1.0)


@pytest.fixture
def popen(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "measurement_software.core.system.subprocess.Popen",
        lambda cmd, **kwargs: calls.append(cmd),
    )
    return calls


class TestStartQuectelCm:
    def test_starts_the_configured_binary_under_sudo(self, popen):
        system.start_quectel_cm(QuectelCmConfig(binary="/usr/bin/quectel-CM"))

        assert popen == [["sudo", "/usr/bin/quectel-CM"]]

    def test_passes_through_configured_args(self, popen):
        system.start_quectel_cm(QuectelCmConfig(binary="/usr/bin/quectel-CM", args=("-s", "internet")))

        assert popen == [["sudo", "/usr/bin/quectel-CM", "-s", "internet"]]

    def test_returns_the_started_process_so_it_can_be_restarted_later(self, monkeypatch):
        process = object()
        monkeypatch.setattr("measurement_software.core.system.subprocess.Popen", lambda cmd, **kwargs: process)

        assert system.start_quectel_cm(QuectelCmConfig(binary="/usr/bin/quectel-CM")) is process

    def test_does_not_wait_for_the_process_to_exit(self, popen):
        # subprocess.Popen (not .run/.check_output) is used precisely so this returns immediately
        # while quectel-CM keeps running in the background - asserting the mock isn't .run/.check_output
        # further protects against a regression back to a blocking call.
        system.start_quectel_cm(QuectelCmConfig(binary="/usr/bin/quectel-CM"))

        assert len(popen) == 1


class FakeQuectelCmProcess:
    """Stands in for the sudo/quectel-CM subprocess.Popen."""

    def __init__(self, exited_with: int | None = None, ignores_terminate: bool = False):
        self.returncode = exited_with
        self.ignores_terminate = ignores_terminate
        self.calls: list[str] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if not self.ignores_terminate:
            self.returncode = -15

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.calls.append(f"wait({timeout})")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("quectel-CM", timeout)
        return self.returncode


class TestStopQuectelCm:
    def test_terminates_it_and_waits_for_it_to_take_the_link_down(self):
        process = FakeQuectelCmProcess()

        system.stop_quectel_cm(process)

        assert process.calls == ["terminate", f"wait({system.QUECTEL_CM_STOP_TIMEOUT_S})"]

    def test_kills_it_if_it_does_not_stop_in_time(self, caplog):
        caplog.set_level(logging.WARNING)
        process = FakeQuectelCmProcess(ignores_terminate=True)

        system.stop_quectel_cm(process)

        assert process.calls == ["terminate", f"wait({system.QUECTEL_CM_STOP_TIMEOUT_S})", "kill", "wait(None)"]
        assert "killing it" in caplog.text

    def test_leaves_one_that_already_exited_alone(self, caplog):
        caplog.set_level(logging.WARNING)
        process = FakeQuectelCmProcess(exited_with=1)

        system.stop_quectel_cm(process)

        assert process.calls == []
        assert "had already exited (exit code 1)" in caplog.text


class TestSetSystemClock:
    def test_sets_clock_via_sudo_date_with_an_argument_list(self, run):
        system.set_system_clock(datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC))

        assert run == [["sudo", "date", "-u", "-s", "2026-07-04 20:15:30"]]

    def test_logs_the_datetime_being_set(self, run, caplog):
        caplog.set_level(logging.INFO)

        system.set_system_clock(datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC))

        assert "2026-07-04 20:15:30" in caplog.text


@pytest.fixture
def log_syncs(monkeypatch, run) -> list[str]:
    """Records sync_log_files() calls into the same order as the subprocess.run fixture's commands."""
    monkeypatch.setattr("measurement_software.core.system.sync_log_files", lambda: run.append(["sync_log_files"]))
    return run


class TestLogSyncBeforePowerCommand:
    def test_shutdown_syncs_the_log_before_shutting_down(self, monkeypatch, no_sleep, log_syncs):
        monkeypatch.setattr("measurement_software.core.system.create_modem", lambda config: FakeModem())

        system.perform_shutdown(modem_config())

        assert log_syncs == [["sync_log_files"], ["sudo", "shutdown", "-h", "now"]]

    def test_reboot_syncs_the_log_before_rebooting(self, monkeypatch, no_sleep, log_syncs):
        monkeypatch.setattr("measurement_software.core.system.create_modem", lambda config: FakeModem())

        system.perform_reboot(modem_config())

        assert log_syncs == [["sync_log_files"], ["sudo", "shutdown", "-r", "now"]]


class TestPerformShutdown:
    def test_powers_down_modem_then_shuts_down(self, monkeypatch, no_sleep, run):
        modem = FakeModem()
        monkeypatch.setattr("measurement_software.core.system.create_modem", lambda config: modem)

        system.perform_shutdown(modem_config())

        assert modem.calls == ["open", "power_down", "close"]
        assert run == [["sudo", "shutdown", "-h", "now"]]

    def test_still_shuts_down_when_modem_power_down_fails(self, monkeypatch, no_sleep, run, caplog):
        caplog.set_level(logging.WARNING)
        modem = FakeModem(fail_at="power_down")
        monkeypatch.setattr("measurement_software.core.system.create_modem", lambda config: modem)

        system.perform_shutdown(modem_config())

        assert "Modem power-down failed" in caplog.text
        assert run == [["sudo", "shutdown", "-h", "now"]]

    def test_still_shuts_down_when_modem_creation_fails(self, monkeypatch, no_sleep, run, caplog):
        caplog.set_level(logging.WARNING)

        def raise_create(config):
            raise RuntimeError("no such modem")

        monkeypatch.setattr("measurement_software.core.system.create_modem", raise_create)

        system.perform_shutdown(modem_config())

        assert "Modem power-down failed" in caplog.text
        assert run == [["sudo", "shutdown", "-h", "now"]]


class TestPerformReboot:
    def test_powers_down_modem_then_reboots(self, monkeypatch, no_sleep, run):
        modem = FakeModem()
        monkeypatch.setattr("measurement_software.core.system.create_modem", lambda config: modem)

        system.perform_reboot(modem_config())

        assert modem.calls == ["open", "power_down", "close"]
        assert run == [["sudo", "shutdown", "-r", "now"]]

    def test_still_reboots_when_modem_power_down_fails(self, monkeypatch, no_sleep, run, caplog):
        caplog.set_level(logging.WARNING)
        modem = FakeModem(fail_at="power_down")
        monkeypatch.setattr("measurement_software.core.system.create_modem", lambda config: modem)

        system.perform_reboot(modem_config())

        assert "Modem power-down failed" in caplog.text
        assert run == [["sudo", "shutdown", "-r", "now"]]

    def test_still_reboots_when_modem_creation_fails(self, monkeypatch, no_sleep, run, caplog):
        caplog.set_level(logging.WARNING)

        def raise_create(config):
            raise RuntimeError("no such modem")

        monkeypatch.setattr("measurement_software.core.system.create_modem", raise_create)

        system.perform_reboot(modem_config())

        assert "Modem power-down failed" in caplog.text
        assert run == [["sudo", "shutdown", "-r", "now"]]


def make_app_config(upload_dir: Path, **overrides) -> AppConfig:
    defaults = dict(
        modem=modem_config(),
        gnss_receiver=GnssConfig(type="nmea_serial", port="/dev/ttyUSB1", baud_rate=9600, timeout=1.0),
        device=DeviceConfig(),
        collector=CollectorConfig(),
        movement_gate=MovementGateConfig(),
        quectel_cm=QuectelCmConfig(),
        run_status=RunStatusConfig(),
        heartbeat=HeartbeatConfig(enabled=False),
        selftest=SelftestConfig(),
        gps_fix_test=GpsFixTestConfig(),
        storage=StorageConfig(),
        display=DisplayConfig(enabled=False),
        latency_test=LatencyTestConfig(enabled=False),
        uploader=UploaderConfig(upload_dir=str(upload_dir)),
        logging=LoggingConfig(),
        system=SystemConfig(),
        backend=BackendConfig(),
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


class FakeUploader:
    def __init__(self, config, backend_config, calls: list):
        self._calls = calls

    def upload_pending_files(self) -> None:
        self._calls.append("upload_pending_files")


@pytest.fixture
def shutdown_steps(monkeypatch) -> list:
    """Patches every step shutdown() delegates to, recording the order they run in."""
    calls: list = []
    monkeypatch.setattr(
        "measurement_software.core.system.discard_stale_temp_files",
        lambda d: calls.append(("discard_stale_temp_files", d)),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.recover_unfinalized",
        lambda d: calls.append(("recover_unfinalized", d)),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.Uploader",
        lambda config, backend_config, device_id: FakeUploader(config, backend_config, calls),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.perform_shutdown",
        lambda modem_cfg: calls.append(("perform_shutdown", modem_cfg)),
    )
    return calls


@pytest.fixture
def reboot_steps(monkeypatch) -> list:
    """Patches every step reboot() delegates to, recording the order they run in."""
    calls: list = []
    monkeypatch.setattr(
        "measurement_software.core.system.discard_stale_temp_files",
        lambda d: calls.append(("discard_stale_temp_files", d)),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.recover_unfinalized",
        lambda d: calls.append(("recover_unfinalized", d)),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.Uploader",
        lambda config, backend_config, device_id: FakeUploader(config, backend_config, calls),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.perform_reboot",
        lambda modem_cfg: calls.append(("perform_reboot", modem_cfg)),
    )
    return calls


class TestReboot:
    def test_running_on_pi_runs_the_full_sequence(self, reboot_steps, tmp_path):
        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=True))

        system.reboot(config, "remote_reboot_command")

        assert reboot_steps == [
            ("discard_stale_temp_files", tmp_path),
            ("recover_unfinalized", tmp_path),
            "upload_pending_files",
            ("perform_reboot", config.modem),
        ]

    def test_not_running_on_pi_still_finalizes_and_reboots(self, reboot_steps, tmp_path):
        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=False))

        system.reboot(config, "remote_reboot_command")

        assert reboot_steps == [
            ("discard_stale_temp_files", tmp_path),
            ("recover_unfinalized", tmp_path),
            "upload_pending_files",
            ("perform_reboot", config.modem),
        ]

    def test_logs_the_given_reason(self, reboot_steps, tmp_path, caplog):
        caplog.set_level(logging.CRITICAL)
        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=False))

        system.reboot(config, "remote_reboot_command")

        assert "remote_reboot_command" in caplog.text


class TestShutdown:
    def test_running_on_pi_runs_the_full_sequence_in_order(self, shutdown_steps, tmp_path):
        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=True))

        system.shutdown(config, "unhandled_exception")

        assert shutdown_steps == [
            ("discard_stale_temp_files", tmp_path),
            ("recover_unfinalized", tmp_path),
            "upload_pending_files",
            ("perform_shutdown", config.modem),
        ]

    def test_not_running_on_pi_still_finalizes_and_powers_down(self, shutdown_steps, tmp_path):
        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "unhandled_exception")

        assert shutdown_steps == [
            ("discard_stale_temp_files", tmp_path),
            ("recover_unfinalized", tmp_path),
            "upload_pending_files",
            ("perform_shutdown", config.modem),
        ]

    def test_logs_the_given_reason(self, shutdown_steps, tmp_path, caplog):
        caplog.set_level(logging.CRITICAL)
        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "sim_unlock_failed")

        assert "sim_unlock_failed" in caplog.text

    def test_finalizes_an_in_progress_run_log_before_uploading(self, tmp_path, monkeypatch):
        """Exercises recover_unfinalized() for real (unpatched) to confirm a run log left open
        by whatever triggered the shutdown actually gets finalized as part of the sequence."""
        monkeypatch.setattr(
            "measurement_software.core.system.perform_shutdown", lambda modem_cfg: None
        )
        monkeypatch.setattr(
            "measurement_software.core.uploader.subprocess.check_output",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
        )
        run_log = tmp_path / "gps_5g_20260101_000000.jsonl"
        run_log.write_text('{"a": 1}\n')

        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=False))
        system.shutdown(config, "unhandled_exception")

        assert not run_log.exists()
        assert (tmp_path / "gps_5g_20260101_000000.json").exists()


class FakeUploadResponse:
    def __init__(self, status: int = 201):
        self.status = status

    def __enter__(self) -> "FakeUploadResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeUploadOpener:
    """Fakes the opener a real `Uploader` POSTs through, recording every request it sent."""

    def __init__(self):
        self.requests: list = []

    def open(self, request, timeout=None) -> FakeUploadResponse:
        self.requests.append(request)
        return FakeUploadResponse()


@pytest.fixture
def upload_opener(monkeypatch) -> FakeUploadOpener:
    """Lets a real `Uploader` run with wlan0 up and every POST accepted, touching no network."""
    fake = FakeUploadOpener()
    monkeypatch.setattr(
        "measurement_software.core.uploader.urllib.request.build_opener", lambda *handlers: fake,
    )
    monkeypatch.setattr(
        "measurement_software.core.uploader.subprocess.check_output",
        lambda *args, **kwargs: b"2: wlan0    inet 10.0.0.5/24 scope global wlan0",
    )
    return fake


class TestFinalizeAndUploadPending:
    def test_sends_the_configured_device_id_as_a_header(self, upload_opener, tmp_path):
        """The terminal paths build their own Uploader; without the device id every upload at
        the end of a shutdown or reboot is rejected by the backend as unauthorized."""
        config = make_app_config(
            tmp_path,
            heartbeat=HeartbeatConfig(device_id="pi-north-01"),
            backend=BackendConfig(url="https://backend.example.org"),
        )
        (tmp_path / "gps_5g_20260101_000000.json").write_text("[]")

        system.finalize_and_upload_pending(config)

        [request] = upload_opener.requests
        assert request.headers["X-device-id"] == "pi-north-01"


class FakeHeartbeatUrlopen:
    """Fakes urllib.request.urlopen for the terminal heartbeat's own HeartbeatSender."""

    def __init__(self, raise_error: Exception | None = None):
        self.raise_error = raise_error
        self.requests: list = []
        self.timeouts: list = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.raise_error is not None:
            raise self.raise_error
        return types.SimpleNamespace(
            status=204, read=lambda: b"", __enter__=lambda s: s, __exit__=lambda *a: None,
        )

    def payloads(self) -> list[dict]:
        import json
        return [json.loads(r.data.decode()) for r in self.requests]


@pytest.fixture
def heartbeat_urlopen(monkeypatch) -> FakeHeartbeatUrlopen:
    fake = FakeHeartbeatUrlopen()
    monkeypatch.setattr("measurement_software.remote.heartbeat.urllib.request.urlopen", fake)
    return fake


@pytest.fixture
def no_op_finalize(monkeypatch):
    """Skips the real finalize/upload step - irrelevant to these tests, and Uploader otherwise
    shells out to `ip addr show` for real."""
    monkeypatch.setattr("measurement_software.core.system.discard_stale_temp_files", lambda d: None)
    monkeypatch.setattr("measurement_software.core.system.recover_unfinalized", lambda d: None)
    monkeypatch.setattr(
        "measurement_software.core.system.Uploader",
        lambda config, backend_config, device_id: types.SimpleNamespace(upload_pending_files=lambda: None),
    )


def app_config_with_heartbeat(tmp_path: Path, **overrides) -> AppConfig:
    return make_app_config(
        tmp_path,
        heartbeat=HeartbeatConfig(enabled=True),
        backend=BackendConfig(url="https://backend.example.org"),
        **overrides,
    )


class TestTerminalHeartbeat:
    def test_shutdown_sends_a_final_heartbeat_with_the_shutting_down_phase(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "unhandled_exception")

        assert heartbeat_urlopen.payloads()[-1]["run_phase"] == "shutting_down"

    def test_reboot_sends_a_final_heartbeat_with_the_rebooting_phase(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.reboot(config, "remote_reboot_command")

        assert heartbeat_urlopen.payloads()[-1]["run_phase"] == "rebooting"

    def test_shutdown_acks_the_triggering_command_id(self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "remote_shutdown_command", command_id=42)

        assert heartbeat_urlopen.payloads()[-1]["executed_commands"] == [
            {"id": 42, "command": "shutdown-now", "status": "received", "error": None},
        ]

    def test_reboot_acks_the_triggering_command_id(self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.reboot(config, "remote_reboot_command", command_id=43)

        assert heartbeat_urlopen.payloads()[-1]["executed_commands"] == [
            {"id": 43, "command": "reboot-now", "status": "received", "error": None},
        ]

    def test_shutdown_without_a_command_id_sends_no_ack(self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "unhandled_exception")

        assert "executed_commands" not in heartbeat_urlopen.payloads()[-1]

    def test_shutdown_proceeds_even_if_the_final_heartbeat_fails(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path, caplog):
        heartbeat_urlopen.raise_error = OSError("unreachable")
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "unhandled_exception")

        assert run == [["sudo", "shutdown", "-h", "now"]]

    def test_reboot_proceeds_even_if_the_final_heartbeat_fails(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        heartbeat_urlopen.raise_error = OSError("unreachable")
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.reboot(config, "remote_reboot_command")

        assert run == [["sudo", "shutdown", "-r", "now"]]

    def test_no_heartbeat_sent_when_heartbeat_is_disabled(self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "unhandled_exception")

        assert heartbeat_urlopen.requests == []


class TestStoppingHeartbeat:
    def test_reports_the_stopping_phase_without_acking_a_command(self, heartbeat_urlopen, tmp_path):
        config = app_config_with_heartbeat(tmp_path)

        system.send_stopping_heartbeat(config)

        [payload] = heartbeat_urlopen.payloads()
        assert payload["run_phase"] == "stopping"
        assert "executed_commands" not in payload

    def test_caps_a_long_timeout_so_an_unreachable_backend_cannot_delay_the_finalize(
            self, heartbeat_urlopen, tmp_path):
        config = app_config_with_heartbeat(tmp_path)
        config.heartbeat.timeout_s = 60.0

        system.send_stopping_heartbeat(config)

        assert heartbeat_urlopen.timeouts == [3.0]
        assert config.heartbeat.timeout_s == 60.0

    def test_keeps_a_timeout_that_is_already_short(self, heartbeat_urlopen, tmp_path):
        config = app_config_with_heartbeat(tmp_path)
        config.heartbeat.timeout_s = 1.0

        system.send_stopping_heartbeat(config)

        assert heartbeat_urlopen.timeouts == [1.0]

    def test_never_raises_when_the_backend_is_unreachable(self, heartbeat_urlopen, tmp_path):
        heartbeat_urlopen.raise_error = OSError("unreachable")

        system.send_stopping_heartbeat(app_config_with_heartbeat(tmp_path))

    def test_sends_nothing_when_the_heartbeat_is_disabled(self, heartbeat_urlopen, tmp_path):
        system.send_stopping_heartbeat(make_app_config(tmp_path))

        assert heartbeat_urlopen.requests == []


class TestRebootPendingAck:
    def test_persists_the_command_id_when_both_are_given(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))
        config_path = tmp_path / "config.toml"

        system.reboot(config, "remote_reboot_command", command_id=52, config_path=config_path)

        assert read_and_clear_pending_command_ack(tmp_path / "pending_reboot_ack.toml") == 52

    def test_writes_nothing_without_a_command_id(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))
        config_path = tmp_path / "config.toml"

        system.reboot(config, "unhandled_exception", config_path=config_path)

        assert not (tmp_path / "pending_reboot_ack.toml").exists()

    def test_writes_nothing_without_a_config_path(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.reboot(config, "remote_reboot_command", command_id=52)

        assert not (tmp_path / "pending_reboot_ack.toml").exists()


@pytest.fixture
def set_clock_calls(monkeypatch) -> list[datetime]:
    calls: list[datetime] = []
    monkeypatch.setattr(
        "measurement_software.core.system.set_system_clock", calls.append
    )
    return calls


class TestClockSyncTrySync:
    def test_not_synced_before_any_call(self):
        assert system.ClockSync().synced is False

    def test_first_valid_datetime_sets_the_clock_and_marks_synced(self, set_clock_calls):
        clock_sync = system.ClockSync()
        dt = datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC)

        clock_sync.try_sync(dt)

        assert set_clock_calls == [dt]
        assert clock_sync.synced is True

    def test_a_second_valid_datetime_does_not_set_the_clock_again(self, set_clock_calls):
        clock_sync = system.ClockSync()
        first = datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC)
        second = datetime(2026, 7, 4, 20, 17, 30, tzinfo=UTC)

        clock_sync.try_sync(first)
        clock_sync.try_sync(second)

        assert set_clock_calls == [first]

    @pytest.mark.parametrize("year", [1980, 1970, 2200, 3000])
    def test_rejects_implausible_year_without_setting_the_clock(self, set_clock_calls, year, caplog):
        clock_sync = system.ClockSync()
        dt = datetime(year, 1, 6, 0, 0, 0, tzinfo=UTC)

        clock_sync.try_sync(dt)

        assert set_clock_calls == []
        assert clock_sync.synced is False

    def test_rejecting_an_implausible_datetime_still_allows_a_later_valid_one(self, set_clock_calls):
        clock_sync = system.ClockSync()
        implausible = datetime(1980, 1, 6, 0, 0, 0, tzinfo=UTC)
        valid = datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC)

        clock_sync.try_sync(implausible)
        clock_sync.try_sync(valid)

        assert set_clock_calls == [valid]
        assert clock_sync.synced is True

class TestShutdownPendingAck:
    """shutdown-now's only ack on the way out is a single "received" heartbeat. If that is lost,
    the backend redelivers the command after the next power-on - the persisted id lets that boot
    ack it as done instead of shutting down again."""

    def test_persists_the_command_id_when_both_are_given(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "remote_shutdown_command", command_id=61, config_path=tmp_path / "config.toml")

        assert read_and_clear_pending_command_ack(tmp_path / "pending_shutdown_ack.toml") == 61

    def test_writes_nothing_for_a_shutdown_no_command_triggered(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "run_completed", config_path=tmp_path / "config.toml")

        assert not (tmp_path / "pending_shutdown_ack.toml").exists()

    def test_writes_nothing_without_a_config_path(
            self, heartbeat_urlopen, no_sleep, run, no_op_finalize, tmp_path):
        config = app_config_with_heartbeat(tmp_path, system=SystemConfig(running_on_pi=False))

        system.shutdown(config, "remote_shutdown_command", command_id=61)

        assert not (tmp_path / "pending_shutdown_ack.toml").exists()
