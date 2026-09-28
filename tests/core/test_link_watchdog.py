import logging
import subprocess
import time

import pytest

from measurement_software.core.config import LinkWatchdogConfig, QuectelCmConfig
from measurement_software.core.link_watchdog import LinkWatchdog


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeProcess:
    """Stands in for the sudo/quectel-CM subprocess.Popen."""

    def __init__(self, name: str, exited_with: int | None = None, ignores_terminate: bool = False):
        self.name = name
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
        if self.returncode is None:
            raise subprocess.TimeoutExpired("quectel-CM", timeout)
        return self.returncode


class Harness:
    def __init__(self, monkeypatch, radio_reset_possible: bool = True, **config):
        self.clock = FakeClock()
        self.link_up = True
        self.started: list[FakeProcess] = []
        self.radio_resets = 0
        self.radio_reset_possible = radio_reset_possible

        def fake_start(quectel_cm_config):
            process = FakeProcess(f"quectel-CM #{len(self.started) + 1}")
            self.started.append(process)
            return process

        def request_radio_reset() -> bool:
            if self.radio_reset_possible:
                self.radio_resets += 1
            return self.radio_reset_possible

        monkeypatch.setattr("measurement_software.core.link_watchdog.start_quectel_cm", fake_start)
        defaults = dict(restart_after_s=180.0, radio_reset_after_restarts=2)
        defaults.update(config)
        self.watchdog = LinkWatchdog(
            LinkWatchdogConfig(**defaults), QuectelCmConfig(binary="/usr/bin/quectel-CM"),
            request_radio_reset, has_ipv4=lambda iface: self.link_up, clock=self.clock,
        )
        self.initial_process = FakeProcess("quectel-CM #0")
        self.watchdog._process = self.initial_process

    def check_at(self, t: float) -> None:
        self.clock.now = t
        self.watchdog.check()


class TestLinkWatchdog:
    def test_does_nothing_while_the_link_is_up(self, monkeypatch):
        h = Harness(monkeypatch)
        for t in range(0, 1000, 30):
            h.check_at(t)

        assert h.started == []
        assert h.initial_process.calls == []

    def test_restarts_quectel_cm_once_the_link_was_down_long_enough(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        h = Harness(monkeypatch)
        h.link_up = False

        h.check_at(0)
        h.check_at(179)
        assert h.started == []

        h.check_at(180)
        assert h.initial_process.calls == ["terminate"]
        assert len(h.started) == 1
        assert "restarting quectel-CM" in caplog.text

    def test_waits_another_period_before_the_next_restart(self, monkeypatch):
        h = Harness(monkeypatch)
        h.link_up = False
        h.check_at(0)
        h.check_at(180)

        h.check_at(300)
        assert len(h.started) == 1
        h.check_at(360)
        assert len(h.started) == 2
        assert h.started[0].calls == ["terminate"]

    def test_resets_the_radio_after_the_configured_number_of_restarts(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        h = Harness(monkeypatch, radio_reset_after_restarts=2)
        h.link_up = False
        h.check_at(0)
        h.check_at(180)
        h.check_at(360)
        assert (len(h.started), h.radio_resets) == (2, 0)

        h.check_at(540)
        assert (len(h.started), h.radio_resets) == (2, 1)
        assert "resetting the modem's radio" in caplog.text

        h.check_at(720)
        assert (len(h.started), h.radio_resets) == (3, 1)

    def test_restarts_quectel_cm_when_a_radio_reset_is_not_possible(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        h = Harness(monkeypatch, radio_reset_possible=False, radio_reset_after_restarts=0)
        h.link_up = False
        h.check_at(0)

        h.check_at(180)

        assert len(h.started) == 1
        assert "radio reset isn't possible" in caplog.text

    def test_link_coming_back_resets_the_escalation_and_is_logged(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        h = Harness(monkeypatch)
        h.link_up = False
        h.check_at(0)
        h.check_at(180)

        h.link_up = True
        h.check_at(200)
        assert "wwan0 link is up again after 200 s" in caplog.text

        h.link_up = False
        h.check_at(1000)
        h.check_at(1179)
        assert len(h.started) == 1
        h.check_at(1180)
        assert len(h.started) == 2

    def test_link_loss_is_logged_once(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        h = Harness(monkeypatch)
        h.link_up = False

        h.check_at(0)
        h.check_at(30)

        assert caplog.text.count("cellular link down") == 1

    def test_a_quectel_cm_that_already_exited_is_just_started_again(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        h = Harness(monkeypatch)
        h.initial_process = h.watchdog._process = FakeProcess("quectel-CM #0", exited_with=0)
        h.link_up = False
        h.check_at(0)

        h.check_at(180)

        assert h.initial_process.calls == []
        assert len(h.started) == 1
        assert "had already exited" in caplog.text

    def test_a_quectel_cm_ignoring_terminate_is_killed(self, monkeypatch):
        h = Harness(monkeypatch)
        h.initial_process = h.watchdog._process = FakeProcess("quectel-CM #0", ignores_terminate=True)
        h.link_up = False
        h.check_at(0)

        h.check_at(180)

        assert h.initial_process.calls == ["terminate", "kill"]
        assert len(h.started) == 1


class TestLifecycle:
    def test_disabled_watchdog_starts_no_thread(self, monkeypatch):
        watchdog = LinkWatchdog(
            LinkWatchdogConfig(enabled=False), QuectelCmConfig(), lambda: False, has_ipv4=lambda iface: True,
        )

        watchdog.start(FakeProcess("quectel-CM"))

        assert watchdog._thread is None
        watchdog.stop()

    def test_stop_without_start_is_a_noop(self):
        LinkWatchdog(LinkWatchdogConfig(), QuectelCmConfig(), lambda: False).stop()

    def test_background_thread_survives_a_failing_check(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR)
        checks = []

        def failing_has_ipv4(iface):
            checks.append(iface)
            raise RuntimeError("ip not found")

        watchdog = LinkWatchdog(
            LinkWatchdogConfig(check_interval_s=0.01), QuectelCmConfig(), lambda: False,
            has_ipv4=failing_has_ipv4,
        )
        watchdog.start(FakeProcess("quectel-CM"))
        try:
            for _ in range(500):
                if len(checks) >= 2:
                    break
                time.sleep(0.01)
        finally:
            watchdog.stop()

        assert len(checks) >= 2
        assert "Link watchdog check failed" in caplog.text


@pytest.mark.parametrize(("output", "expected"), [
    ("3: wwan0    inet 100.83.30.55/28 scope global wwan0\n", True),
    ("", False),
])
def test_interface_has_ipv4_reads_ip_addr_output(monkeypatch, output, expected):
    from measurement_software.core import link_watchdog

    commands = []

    def fake_check_output(cmd, **kwargs):
        commands.append(cmd)
        return output.encode()

    monkeypatch.setattr("measurement_software.core.link_watchdog.subprocess.check_output", fake_check_output)

    assert link_watchdog.interface_has_ipv4("wwan0") is expected
    assert commands == [["ip", "-4", "-o", "addr", "show", "dev", "wwan0"]]


def test_interface_has_ipv4_treats_a_missing_interface_as_down(monkeypatch):
    from measurement_software.core import link_watchdog

    def missing(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("measurement_software.core.link_watchdog.subprocess.check_output", missing)

    assert link_watchdog.interface_has_ipv4("wwan0") is False
