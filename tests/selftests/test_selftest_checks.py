import subprocess
from pathlib import Path

import pytest

from measurement_software.selftests import selftest_checks as checks
from measurement_software.core.config import load_config
from measurement_software.selftests.selftest_results import CheckStatus
from measurement_software.displays.display import Display
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, Position
from measurement_software.modems.modem import CellSample, Modem

REQUIRED_SECTIONS = """
[modem]
type = "quectel"
port = "/dev/ttyUSB2"
baud_rate = 115200
timeout = 1.0

[gnss_receiver]
type = "nmea_serial"
port = "/dev/serial0"
baud_rate = 9600
timeout = 1.0

[uploader]
upload_dir = "{upload_dir}"
upload_user = "pi"
upload_host = "example.org"
upload_port = 2000
remote_dir = "/remote/dir/"
"""


def make_config(tmp_path: Path, extra: str = ""):
    content = REQUIRED_SECTIONS.format(upload_dir=str(tmp_path / "uploads")) + extra
    path = tmp_path / "config.toml"
    path.write_text(content)
    return load_config(path)


class FakeMonotonic:
    """A fake `time.monotonic` that advances a step per call, so a bounded retry loop (e.g.
    `_wait_for_fix`) exhausts its timeout after a few calls instead of a real wall-clock wait."""

    def __init__(self, step: float = 1.0):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture
def fast_fix_timeout(monkeypatch) -> None:
    monkeypatch.setattr(checks.time, "monotonic", FakeMonotonic())


class FakeModem(Modem):
    def __init__(self, samples: list[CellSample] | None = None, fail_at: str | None = None):
        self.samples = samples if samples is not None else [CellSample(rat="LTE", rsrp=-80.0)]
        self.fail_at = fail_at
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")
        if self.fail_at == "open":
            raise RuntimeError("modem open failed")

    def close(self) -> None:
        self.calls.append("close")

    def unlock_sim(self) -> None:
        pass

    def query_cell_info(self) -> list[CellSample]:
        self.calls.append("query_cell_info")
        if self.fail_at == "query_cell_info":
            raise RuntimeError("AT command failed")
        return self.samples

    def power_down(self) -> None:
        pass


_DEFAULT_FIX = GNSSFix(position=Position(1.0, 2.0), num_satellites=7)


class FakeGNSSReceiver(GNSSReceiver):
    def __init__(self, fix: GNSSFix | None = _DEFAULT_FIX, fail_at: str | None = None):
        self.fix = fix
        self.fail_at = fail_at
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")
        if self.fail_at == "open":
            raise RuntimeError("gnss open failed")

    def close(self) -> None:
        self.calls.append("close")

    def read_fix(self) -> GNSSFix | None:
        self.calls.append("read_fix")
        if self.fail_at == "read_fix":
            raise RuntimeError("serial read failed")
        return self.fix

    def read_datetime(self):
        return None


class FakeDisplay(Display):
    def __init__(self, fail_at: str | None = None):
        self.fail_at = fail_at
        self.calls: list[tuple] = []

    def open(self) -> None:
        self.calls.append(("open",))
        if self.fail_at == "open":
            raise RuntimeError("display open failed")

    def close(self) -> None:
        self.calls.append(("close",))

    def show(self, message: str) -> None:
        self.calls.append(("show", message))


class TestCheckModem:
    def test_passes_and_closes_on_success(self, tmp_path, monkeypatch):
        modem = FakeModem(samples=[CellSample(rat="LTE"), CellSample(rat="NR5G-SA")])
        monkeypatch.setattr(checks, "create_modem", lambda config: modem)

        result = checks.check_modem(make_config(tmp_path))

        assert result.status == CheckStatus.PASSED
        assert result.details == {"cell_samples": 2}
        assert modem.calls == ["open", "query_cell_info", "close"]

    def test_fails_and_still_closes_when_query_raises(self, tmp_path, monkeypatch):
        modem = FakeModem(fail_at="query_cell_info")
        monkeypatch.setattr(checks, "create_modem", lambda config: modem)

        result = checks.check_modem(make_config(tmp_path))

        assert result.status == CheckStatus.FAILED
        assert "AT command failed" in result.error_message
        assert "close" in modem.calls

    def test_fails_when_open_raises(self, tmp_path, monkeypatch):
        modem = FakeModem(fail_at="open")
        monkeypatch.setattr(checks, "create_modem", lambda config: modem)

        result = checks.check_modem(make_config(tmp_path))

        assert result.status == CheckStatus.FAILED
        assert "modem open failed" in result.error_message


class FlakyGNSSReceiver(GNSSReceiver):
    """Returns None (as read_fix() does for any non-GGA line) a few times before a real fix -
    the normal shape of a live NMEA stream, where GGA is only one of several sentence types
    cycled through each second."""

    def __init__(self, misses_before_fix: int, fix: GNSSFix):
        self._misses_left = misses_before_fix
        self._fix = fix
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def read_fix(self) -> GNSSFix | None:
        self.calls.append("read_fix")
        if self._misses_left > 0:
            self._misses_left -= 1
            return None
        return self._fix

    def read_datetime(self):
        return None


class TestCheckGps:
    def test_retries_until_a_fix_arrives_within_the_timeout(self, tmp_path, monkeypatch):
        # Regression test: a single read_fix() call has a real chance of landing on a non-GGA
        # NMEA line even with a genuinely live fix - confirmed live, where check_gps flip-flopped
        # PASS/FAIL against a continuously-available fix. It must retry, not fail on one miss.
        gnss = FlakyGNSSReceiver(misses_before_fix=3, fix=GNSSFix(position=Position(1.0, 2.0), num_satellites=12))
        monkeypatch.setattr(checks, "create_gnss_receiver", lambda config: gnss)

        result = checks.check_gps(make_config(tmp_path))

        assert result.status == CheckStatus.PASSED
        assert result.details == {"num_satellites": 12}
        assert gnss.calls.count("read_fix") == 4

    def test_passes_with_a_fix(self, tmp_path, monkeypatch):
        gnss = FakeGNSSReceiver(fix=GNSSFix(position=Position(1.0, 2.0), num_satellites=9))
        monkeypatch.setattr(checks, "create_gnss_receiver", lambda config: gnss)

        result = checks.check_gps(make_config(tmp_path))

        assert result.status == CheckStatus.PASSED
        assert result.details == {"num_satellites": 9}
        assert gnss.calls == ["open", "read_fix", "close"]

    def test_fails_when_no_fix_available(self, tmp_path, monkeypatch, fast_fix_timeout):
        gnss = FakeGNSSReceiver(fix=None)
        monkeypatch.setattr(checks, "create_gnss_receiver", lambda config: gnss)

        result = checks.check_gps(make_config(tmp_path))

        assert result.status == CheckStatus.FAILED

    def test_fails_and_still_closes_on_read_error(self, tmp_path, monkeypatch):
        gnss = FakeGNSSReceiver(fail_at="read_fix")
        monkeypatch.setattr(checks, "create_gnss_receiver", lambda config: gnss)

        result = checks.check_gps(make_config(tmp_path))

        assert result.status == CheckStatus.FAILED
        assert "close" in gnss.calls


class TestCheckCollector:
    def test_passes_and_does_not_write_any_run_log(self, tmp_path, monkeypatch):
        modem = FakeModem(samples=[CellSample(rat="LTE")])
        gnss = FakeGNSSReceiver(fix=GNSSFix(position=Position(1.0, 2.0), num_satellites=5))
        monkeypatch.setattr(checks, "create_modem", lambda config: modem)
        monkeypatch.setattr(checks, "create_gnss_receiver", lambda config: gnss)

        config = make_config(tmp_path)
        result = checks.check_collector(config)

        assert result.status == CheckStatus.PASSED
        assert result.details == {"num_satellites": 5, "cell_samples": 1}
        assert not (tmp_path / "uploads").exists() or list((tmp_path / "uploads").glob("*")) == []

    def test_fails_when_no_fix(self, tmp_path, monkeypatch, fast_fix_timeout):
        modem = FakeModem()
        gnss = FakeGNSSReceiver(fix=None)
        monkeypatch.setattr(checks, "create_modem", lambda config: modem)
        monkeypatch.setattr(checks, "create_gnss_receiver", lambda config: gnss)

        result = checks.check_collector(make_config(tmp_path))

        assert result.status == CheckStatus.FAILED

    def test_closes_both_even_when_modem_query_fails(self, tmp_path, monkeypatch):
        modem = FakeModem(fail_at="query_cell_info")
        gnss = FakeGNSSReceiver()
        monkeypatch.setattr(checks, "create_modem", lambda config: modem)
        monkeypatch.setattr(checks, "create_gnss_receiver", lambda config: gnss)

        result = checks.check_collector(make_config(tmp_path))

        assert result.status == CheckStatus.FAILED
        assert "close" in modem.calls
        assert "close" in gnss.calls


class TestCheckUploader:
    def test_fails_when_no_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(checks.Uploader, "_is_interface_up", staticmethod(lambda iface: False))

        result = checks.check_uploader(make_config(tmp_path), confirm_side_effects=False)

        assert result.status == CheckStatus.FAILED

    def test_passes_without_uploading_when_not_confirmed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(checks.Uploader, "_is_interface_up", staticmethod(lambda iface: iface == "wlan0"))
        calls = []
        monkeypatch.setattr(checks.subprocess, "run", lambda *a, **k: calls.append(a))

        result = checks.check_uploader(make_config(tmp_path), confirm_side_effects=False)

        assert result.status == CheckStatus.PASSED
        assert result.details == {"network_reachable": True, "real_upload_attempted": False}
        assert calls == []

    def test_uploads_a_throwaway_probe_when_confirmed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(checks.Uploader, "_is_interface_up", staticmethod(lambda iface: iface == "wlan0"))
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            probe_path = Path(cmd[-2])
            recorded["probe_existed_during_call"] = probe_path.exists()
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(checks.subprocess, "run", fake_run)

        result = checks.check_uploader(make_config(tmp_path), confirm_side_effects=True)

        assert result.status == CheckStatus.PASSED
        assert result.details["real_upload_attempted"] is True
        assert recorded["probe_existed_during_call"] is True
        assert "selftest_probe_" in recorded["cmd"][-2]
        assert recorded["cmd"][-1].endswith("/remote/dir/" + Path(recorded["cmd"][-2]).name)
        # The temp file must never be left behind, and must never live inside upload_dir.
        assert not Path(recorded["cmd"][-2]).exists()
        assert list((tmp_path / "uploads").glob("*")) == [] if (tmp_path / "uploads").exists() else True

    def test_reports_failure_when_scp_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(checks.Uploader, "_is_interface_up", staticmethod(lambda iface: iface == "wlan0"))
        monkeypatch.setattr(
            checks.subprocess, "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(args=cmd, returncode=1),
        )

        result = checks.check_uploader(make_config(tmp_path), confirm_side_effects=True)

        assert result.status == CheckStatus.FAILED


class TestCheckHeartbeat:
    def test_skipped_when_disabled(self, tmp_path):
        result = checks.check_heartbeat(make_config(tmp_path))

        assert result.status == CheckStatus.SKIPPED

    def test_fails_when_url_not_https(self, tmp_path):
        config = make_config(tmp_path, extra="""
[heartbeat]
enabled = true

[backend]
url = "http://backend.example.org"
""")
        result = checks.check_heartbeat(config)

        assert result.status == CheckStatus.FAILED

    def test_passes_when_send_succeeds(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, extra="""
[heartbeat]
enabled = true

[backend]
url = "https://backend.example.org"
""")
        monkeypatch.setattr(checks.HeartbeatSender, "send_once", lambda self, *a: True)

        result = checks.check_heartbeat(config)

        assert result.status == CheckStatus.PASSED

    def test_fails_when_send_fails(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, extra="""
[heartbeat]
enabled = true

[backend]
url = "https://backend.example.org"
""")
        monkeypatch.setattr(checks.HeartbeatSender, "send_once", lambda self, *a: False)

        result = checks.check_heartbeat(config)

        assert result.status == CheckStatus.FAILED


class TestCheckKeepModemAlive:
    def test_passes_after_starting_and_stopping(self, tmp_path, monkeypatch):
        started = []
        stopped = []
        monkeypatch.setattr(checks.KeepModemAliveSender, "start", lambda self: started.append(True))
        monkeypatch.setattr(checks.KeepModemAliveSender, "stop", lambda self: stopped.append(True))
        monkeypatch.setattr(checks.time, "sleep", lambda s: None)

        result = checks.check_keep_modem_alive(make_config(tmp_path))

        assert result.status == CheckStatus.PASSED
        assert started == [True]
        assert stopped == [True]


class TestCheckFan:
    def test_passes_with_temperature_reading_when_fan_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(checks.system, "read_cpu_temperature_celsius", lambda: 42.5)

        result = checks.check_fan(make_config(tmp_path))

        assert result.status == CheckStatus.PASSED
        assert result.details == {"cpu_temperature_celsius": 42.5}

    def test_fails_when_temperature_read_fails(self, tmp_path, monkeypatch):
        def raise_os_error():
            raise OSError("no such file")

        monkeypatch.setattr(checks.system, "read_cpu_temperature_celsius", raise_os_error)

        result = checks.check_fan(make_config(tmp_path))

        assert result.status == CheckStatus.FAILED

    def test_toggles_gpio_when_fan_enabled(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, extra="""
[fan]
enabled = true
gpio_pin = 22
""")
        monkeypatch.setattr(checks.system, "read_cpu_temperature_celsius", lambda: 50.0)
        calls = []
        monkeypatch.setattr(checks.system, "setup_fan_gpio", lambda pin: calls.append(("setup", pin)))
        monkeypatch.setattr(checks.system, "set_fan_state", lambda pin, on: calls.append(("set", pin, on)))
        monkeypatch.setattr(checks.time, "sleep", lambda s: None)

        result = checks.check_fan(config)

        assert result.status == CheckStatus.PASSED
        assert result.details["gpio_toggled"] is True
        assert calls == [("setup", 22), ("set", 22, True), ("set", 22, False)]


class TestCheckDisplay:
    def test_skipped_when_disabled(self, tmp_path):
        result = checks.check_display(make_config(tmp_path))

        assert result.status == CheckStatus.SKIPPED

    def test_passes_and_shows_a_message(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, extra="""
[display]
enabled = true
""")
        display = FakeDisplay()
        monkeypatch.setattr(checks, "create_display", lambda config: display)

        result = checks.check_display(config)

        assert result.status == CheckStatus.PASSED
        assert display.calls[0] == ("open",)
        assert display.calls[1][0] == "show"
        assert display.calls[-1] == ("close",)

    def test_fails_and_still_closes_when_show_fails(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, extra="""
[display]
enabled = true
""")
        display = FakeDisplay(fail_at="open")
        monkeypatch.setattr(checks, "create_display", lambda config: display)

        result = checks.check_display(config)

        assert result.status == CheckStatus.FAILED
        assert ("close",) in display.calls


class TestCheckLatencyTester:
    def test_skipped_when_disabled(self, tmp_path):
        result = checks.check_latency_tester(make_config(tmp_path))

        assert result.status == CheckStatus.SKIPPED

    def test_fails_when_host_missing(self, tmp_path):
        config = make_config(tmp_path, extra="""
[latency_test]
enabled = true
""")
        result = checks.check_latency_tester(config)

        assert result.status == CheckStatus.FAILED
        assert "host" in result.error_message

    def test_passes_when_flent_reports_a_version(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, extra="""
[latency_test]
enabled = true
host = "latency.example.org"
""")
        monkeypatch.setattr(
            checks.subprocess, "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(args=cmd, returncode=0, stdout="flent 3.0\n"),
        )

        result = checks.check_latency_tester(config)

        assert result.status == CheckStatus.PASSED
        assert result.details["flent_version"] == "flent 3.0"

    def test_fails_when_flent_binary_missing(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, extra="""
[latency_test]
enabled = true
host = "latency.example.org"
""")

        def raise_missing(cmd, **kwargs):
            raise FileNotFoundError()

        monkeypatch.setattr(checks.subprocess, "run", raise_missing)

        result = checks.check_latency_tester(config)

        assert result.status == CheckStatus.FAILED
        assert "not found" in result.error_message


class TestDispatch:
    def test_available_checks_lists_all_nine_subsystems(self):
        assert set(checks.available_checks()) == {
            "modem", "gps", "collector", "uploader", "heartbeat",
            "keep_modem_alive", "fan", "display", "latency_tester",
        }

    def test_run_check_dispatches_by_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(checks.system, "read_cpu_temperature_celsius", lambda: 30.0)

        result = checks.run_check("fan", make_config(tmp_path), confirm_side_effects=False)

        assert result.name == "fan"
        assert result.status == CheckStatus.PASSED

    def test_run_check_raises_on_unknown_name(self, tmp_path):
        with pytest.raises(ValueError):
            checks.run_check("nonexistent", make_config(tmp_path), confirm_side_effects=False)

    def test_backend_test_types_match_the_five_backend_supported_checks(self):
        assert checks.BACKEND_TEST_TYPES == {"modem", "gps", "collector", "uploader", "heartbeat"}
