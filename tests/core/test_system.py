import logging
import subprocess
import sys
import types

import pytest

from measurement_software.core import system
from measurement_software.core.config import ModemConfig
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