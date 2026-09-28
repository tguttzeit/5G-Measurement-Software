import logging
import subprocess
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from measurement_software.core import system
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
from measurement_software.modems.modem import CellSample, Modem


@pytest.fixture
def fake_gpio(monkeypatch):
    """Injects a fake RPi.GPIO module so `import RPi.GPIO` resolves without real hardware."""
    calls: list[tuple] = []
    gpio = types.SimpleNamespace(BCM="BCM", OUT="OUT", LOW=0, HIGH=1)
    gpio.setwarnings = lambda flag: calls.append(("setwarnings", flag))
    gpio.setmode = lambda mode: calls.append(("setmode", mode))
    gpio.setup = lambda pin, direction: calls.append(("setup", pin, direction))
    gpio.output = lambda pin, value: calls.append(("output", pin, value))
    gpio.cleanup = lambda: calls.append(("cleanup",))
    gpio.calls = calls

    rpi_module = types.ModuleType("RPi")
    rpi_module.GPIO = gpio
    monkeypatch.setitem(sys.modules, "RPi", rpi_module)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", gpio)
    return gpio


class TestGpioHelpers:
    def test_setup_gpio_configures_pin_as_low_output(self, fake_gpio):
        system.setup_gpio(17)

        assert fake_gpio.calls == [
            ("setwarnings", False),
            ("setmode", "BCM"),
            ("setup", 17, "OUT"),
            ("output", 17, 0),
        ]

    def test_signal_completion_sets_pin_high(self, fake_gpio):
        system.signal_completion(17)

        assert fake_gpio.calls == [("output", 17, 1)]

    def test_cleanup_gpio_calls_cleanup(self, fake_gpio):
        system.cleanup_gpio()

        assert fake_gpio.calls == [("cleanup",)]

    def test_setup_fan_gpio_configures_pin_as_low_output(self, fake_gpio):
        system.setup_fan_gpio(27)

        assert fake_gpio.calls == [
            ("setwarnings", False),
            ("setmode", "BCM"),
            ("setup", 27, "OUT"),
            ("output", 27, 0),
        ]

    def test_set_fan_state_true_drives_pin_high(self, fake_gpio):
        system.set_fan_state(27, True)

        assert fake_gpio.calls == [("output", 27, 1)]

    def test_set_fan_state_false_drives_pin_low(self, fake_gpio):
        system.set_fan_state(27, False)

        assert fake_gpio.calls == [("output", 27, 0)]


class TestReadCpuTemperatureCelsius:
    def test_reads_millidegrees_and_converts_to_celsius(self, monkeypatch, tmp_path):
        temp_file = tmp_path / "temp"
        temp_file.write_text("70000\n")
        monkeypatch.setattr("measurement_software.core.system._CPU_TEMPERATURE_PATH", str(temp_file))

        assert system.read_cpu_temperature_celsius() == 70.0

    def test_raises_when_thermal_zone_file_is_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "measurement_software.core.system._CPU_TEMPERATURE_PATH", str(tmp_path / "missing")
        )

        with pytest.raises(FileNotFoundError):
            system.read_cpu_temperature_celsius()


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

    def test_does_not_wait_for_the_process_to_exit(self, popen):
        # subprocess.Popen (not .run/.check_output) is used precisely so this returns immediately
        # while quectel-CM keeps running in the background - asserting the mock isn't .run/.check_output
        # further protects against a regression back to a blocking call.
        system.start_quectel_cm(QuectelCmConfig(binary="/usr/bin/quectel-CM"))

        assert len(popen) == 1


class TestSetSystemClock:
    def test_sets_clock_via_sudo_date_with_an_argument_list(self, run):
        system.set_system_clock(datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC))

        assert run == [["sudo", "date", "-u", "-s", "2026-07-04 20:15:30"]]

    def test_logs_the_datetime_being_set(self, run, caplog):
        caplog.set_level(logging.INFO)

        system.set_system_clock(datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC))

        assert "2026-07-04 20:15:30" in caplog.text


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


def make_app_config(upload_dir: Path, **overrides) -> AppConfig:
    defaults = dict(
        modem=modem_config(),
        gnss_receiver=GnssConfig(type="nmea_serial", port="/dev/ttyUSB1", baud_rate=9600, timeout=1.0),
        device=DeviceConfig(),
        collector=CollectorConfig(),
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
        uploader=UploaderConfig(upload_dir=str(upload_dir), upload_user="user", upload_host="host"),
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
        lambda config, backend_config: FakeUploader(config, backend_config, calls),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.signal_completion",
        lambda pin: calls.append(("signal_completion", pin)),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.perform_shutdown",
        lambda modem_cfg: calls.append(("perform_shutdown", modem_cfg)),
    )
    monkeypatch.setattr(
        "measurement_software.core.system.cleanup_gpio",
        lambda: calls.append("cleanup_gpio"),
    )
    return calls


class TestShutdown:
    def test_running_on_pi_runs_the_full_sequence_in_order(self, shutdown_steps, tmp_path):
        config = make_app_config(tmp_path, system=SystemConfig(running_on_pi=True, shutdown_gpio=17))

        system.shutdown(config, "unhandled_exception")

        assert shutdown_steps == [
            ("discard_stale_temp_files", tmp_path),
            ("recover_unfinalized", tmp_path),
            "upload_pending_files",
            ("signal_completion", 17),
            ("perform_shutdown", config.modem),
            "cleanup_gpio",
        ]

    def test_not_running_on_pi_skips_gpio_but_still_finalizes_and_powers_down(self, shutdown_steps, tmp_path):
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
            "measurement_software.core.system.signal_completion", lambda pin: None
        )
        monkeypatch.setattr(
            "measurement_software.core.system.perform_shutdown", lambda modem_cfg: None
        )
        monkeypatch.setattr("measurement_software.core.system.cleanup_gpio", lambda: None)
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


class FakeFanHardware:
    """Fakes the GPIO/temperature functions FanController calls in core.system."""

    def __init__(self, temperature: float = 20.0):
        self.temperature = temperature
        self.setup_calls: list[int] = []
        self.fan_state_calls: list[tuple[int, bool]] = []
        self.read_error: Exception | None = None

    def setup_fan_gpio(self, pin: int) -> None:
        self.setup_calls.append(pin)

    def set_fan_state(self, pin: int, on: bool) -> None:
        self.fan_state_calls.append((pin, on))

    def read_cpu_temperature_celsius(self) -> float:
        if self.read_error is not None:
            raise self.read_error
        return self.temperature


@pytest.fixture
def fake_fan_hardware(monkeypatch) -> FakeFanHardware:
    fake = FakeFanHardware()
    monkeypatch.setattr("measurement_software.core.system.setup_fan_gpio", fake.setup_fan_gpio)
    monkeypatch.setattr("measurement_software.core.system.set_fan_state", fake.set_fan_state)
    monkeypatch.setattr(
        "measurement_software.core.system.read_cpu_temperature_celsius",
        fake.read_cpu_temperature_celsius,
    )
    return fake


def fan_config(**overrides) -> FanConfig:
    defaults = dict(enabled=True, gpio_pin=27, temp_on_celsius=70.0, temp_off_celsius=60.0, poll_interval_s=5.0)
    return FanConfig(**(defaults | overrides))


class TestFanControllerUpdate:
    def test_turns_fan_on_once_temperature_reaches_on_threshold(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        fake_fan_hardware.temperature = 70.0

        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, True)]

    def test_leaves_fan_off_below_on_threshold(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        fake_fan_hardware.temperature = 69.9

        controller._update()

        assert fake_fan_hardware.fan_state_calls == []

    def test_leaves_fan_on_between_thresholds_once_already_on(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        fake_fan_hardware.temperature = 70.0
        controller._update()

        fake_fan_hardware.temperature = 65.0
        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, True)]

    def test_turns_fan_off_once_temperature_reaches_off_threshold(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        fake_fan_hardware.temperature = 70.0
        controller._update()

        fake_fan_hardware.temperature = 60.0
        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, True), (27, False)]

    def test_does_not_turn_fan_back_on_immediately_after_turning_off(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        fake_fan_hardware.temperature = 70.0
        controller._update()
        fake_fan_hardware.temperature = 60.0
        controller._update()

        fake_fan_hardware.temperature = 65.0
        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, True), (27, False)]

    def test_logs_warning_and_keeps_state_when_temperature_read_fails(self, fake_fan_hardware, caplog):
        caplog.set_level(logging.WARNING)
        controller = system.FanController(fan_config())
        fake_fan_hardware.read_error = OSError("no such file")

        controller._update()

        assert fake_fan_hardware.fan_state_calls == []
        assert "Could not read CPU temperature" in caplog.text


class TestFanControllerOverride:
    def test_override_on_forces_fan_on_regardless_of_temperature(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        fake_fan_hardware.temperature = 20.0

        controller.override(True)
        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, True)]

    def test_override_off_forces_fan_off_regardless_of_temperature(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        fake_fan_hardware.temperature = 80.0
        controller._update()
        fake_fan_hardware.fan_state_calls.clear()

        controller.override(False)
        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, False)]

    def test_override_persists_across_repeated_updates(self, fake_fan_hardware):
        controller = system.FanController(fan_config())

        controller.override(True)
        controller._update()
        fake_fan_hardware.temperature = 20.0
        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, True)]

    def test_new_override_replaces_the_previous_one(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        controller.override(True)
        controller._update()

        controller.override(False)
        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, True), (27, False)]

    def test_override_is_ignored_when_fan_control_is_disabled_locally(self, fake_fan_hardware, caplog):
        caplog.set_level(logging.WARNING)
        controller = system.FanController(fan_config(enabled=False))

        controller.override(True)
        controller._update()

        assert fake_fan_hardware.fan_state_calls == []
        assert "disabled locally" in caplog.text


class TestFanControllerReleaseOverride:
    def test_release_override_resumes_hysteresis(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        controller.override(True)
        controller._update()
        fake_fan_hardware.temperature = 20.0

        controller.release_override()
        controller._update()

        assert fake_fan_hardware.fan_state_calls == [(27, True), (27, False)]

    def test_release_override_with_no_standing_override_is_a_no_op(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        fake_fan_hardware.temperature = 20.0

        controller.release_override()
        controller._update()

        assert fake_fan_hardware.fan_state_calls == []


class TestFanControllerCurrentMode:
    def test_reports_auto_when_no_override_is_set(self, fake_fan_hardware):
        controller = system.FanController(fan_config())

        assert controller.current_mode() == "auto"

    def test_reports_on_when_overridden_on(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        controller.override(True)

        assert controller.current_mode() == "on"

    def test_reports_off_when_overridden_off(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        controller.override(False)

        assert controller.current_mode() == "off"

    def test_reports_auto_again_after_release(self, fake_fan_hardware):
        controller = system.FanController(fan_config())
        controller.override(True)

        controller.release_override()

        assert controller.current_mode() == "auto"


class TestFanControllerStartStop:
    def test_disabled_does_not_touch_gpio_or_spawn_a_thread(self, fake_fan_hardware):
        controller = system.FanController(fan_config(enabled=False))

        controller.start()
        controller.stop()

        assert fake_fan_hardware.setup_calls == []

    def test_enabled_sets_up_gpio_on_start(self, fake_fan_hardware):
        controller = system.FanController(fan_config())

        controller.start()
        controller.stop()

        assert fake_fan_hardware.setup_calls == [27]

    def test_stop_without_start_is_a_no_op(self, fake_fan_hardware):
        controller = system.FanController(fan_config())

        controller.stop()

    def test_polls_and_updates_fan_state_on_background_thread(self, fake_fan_hardware):
        fake_fan_hardware.temperature = 75.0
        controller = system.FanController(fan_config(poll_interval_s=0.01))

        controller.start()
        try:
            deadline = time.monotonic() + 5.0
            while not fake_fan_hardware.fan_state_calls:
                if time.monotonic() > deadline:
                    pytest.fail("Fan was never turned on by the background thread")
                time.sleep(0.01)
        finally:
            controller.stop()

        assert fake_fan_hardware.fan_state_calls[0] == (27, True)


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