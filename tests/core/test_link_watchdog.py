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
    def __init__(self, monkeypatch, radio_reset_possible: bool = True, on_link_up=None, **config):
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
        defaults = dict(restart_after_s=180.0, max_restart_interval_s=180.0, radio_reset_after_restarts=2)
        defaults.update(config)
        self.watchdog = LinkWatchdog(
            LinkWatchdogConfig(**defaults), QuectelCmConfig(binary="/usr/bin/quectel-CM"),
            request_radio_reset, has_ipv4=lambda iface: self.has_ipv4(iface), clock=self.clock,
            on_link_up=on_link_up, sleep=self.sleep,
        )
        self.link_up_at: float | None = None
        self.queried_interfaces: list[str] = []
        self.sleeps: list[float] = []
        self.initial_process = FakeProcess("quectel-CM #0")
        self.watchdog._process = self.initial_process

    def check_at(self, t: float) -> None:
        self.clock.now = t
        self.watchdog.check()

    def has_ipv4(self, interface: str) -> bool:
        self.queried_interfaces.append(interface)
        if self.link_up_at is not None:
            return self.clock.now >= self.link_up_at
        return self.link_up

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.now += seconds


class TestWaitForLink:
    def test_returns_at_once_and_quietly_when_the_link_is_already_up(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        h = Harness(monkeypatch)

        assert h.watchdog.wait_for_link(30.0) is True
        assert h.sleeps == []
        assert caplog.text == ""

    def test_polls_the_watched_interface_until_it_has_an_address(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        h = Harness(monkeypatch, interface="wwan1")
        h.link_up_at = 1.2

        assert h.watchdog.wait_for_link(30.0) is True
        assert h.clock.now == pytest.approx(1.5)
        assert set(h.queried_interfaces) == {"wwan1"}
        assert "wwan1 is up after 2 s" in caplog.text

    def test_gives_up_after_the_timeout(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        h = Harness(monkeypatch)
        h.link_up = False

        assert h.watchdog.wait_for_link(30.0) is False
        assert h.clock.now == pytest.approx(30.0)
        assert "no IPv4 address after 30 s" in caplog.text

    def test_waits_even_when_the_watchdog_is_disabled(self, monkeypatch):
        h = Harness(monkeypatch, enabled=False)
        h.link_up_at = 1.0

        assert h.watchdog.wait_for_link(30.0) is True


class TestOnLinkUp:
    def test_runs_once_when_the_link_is_first_seen_up(self, monkeypatch):
        calls = []
        h = Harness(monkeypatch, on_link_up=lambda: calls.append(1) or True)

        h.check_at(0)
        h.check_at(30)

        assert calls == [1]

    def test_waits_for_the_link_when_it_starts_down(self, monkeypatch):
        calls = []
        h = Harness(monkeypatch, on_link_up=lambda: calls.append(1) or True)
        h.link_up = False
        h.check_at(0)
        h.check_at(30)
        assert calls == []

        h.link_up = True
        h.check_at(60)

        assert calls == [1]

    def test_a_failure_is_retried_on_the_next_link_up_not_on_the_next_check(self, monkeypatch):
        results = [False, True]
        calls = []
        h = Harness(monkeypatch, on_link_up=lambda: calls.append(1) or results.pop(0))

        h.check_at(0)
        h.check_at(30)
        assert calls == [1]

        h.link_up = False
        h.check_at(60)
        h.link_up = True
        h.check_at(90)
        h.link_up = False
        h.check_at(120)
        h.link_up = True
        h.check_at(150)

        assert calls == [1, 1]

    def test_is_not_run_again_after_it_succeeded(self, monkeypatch):
        calls = []
        h = Harness(monkeypatch, on_link_up=lambda: calls.append(1) or True)
        h.check_at(0)
        h.link_up = False
        h.check_at(30)
        h.link_up = True
        h.check_at(60)

        assert calls == [1]

    def test_a_raising_callback_is_left_to_the_thread_loop_and_not_retried_on_the_next_check(self, monkeypatch):
        calls = []

        def failing():
            calls.append(1)
            raise RuntimeError("boom")

        h = Harness(monkeypatch, on_link_up=failing)

        with pytest.raises(RuntimeError):
            h.check_at(0)
        h.check_at(30)

        assert calls == [1]


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

    def test_reports_the_quectel_cm_it_currently_supervises(self, monkeypatch):
        h = Harness(monkeypatch)
        assert h.watchdog.process is h.initial_process
        h.link_up = False
        h.check_at(0)

        h.check_at(180)

        assert h.watchdog.process is h.started[0]

    def test_a_quectel_cm_ignoring_terminate_is_killed(self, monkeypatch):
        h = Harness(monkeypatch)
        h.initial_process = h.watchdog._process = FakeProcess("quectel-CM #0", ignores_terminate=True)
        h.link_up = False
        h.check_at(0)

        h.check_at(180)

        assert h.initial_process.calls == ["terminate", "kill"]
        assert len(h.started) == 1


class TestBackoff:
    """Where no link can be established at all, recovery steps come further and further apart."""

    def step_times(self, h: Harness, until: float) -> list[float]:
        times = []
        for t in range(0, int(until) + 1, 30):
            before = len(h.started) + h.radio_resets
            h.check_at(t)
            if len(h.started) + h.radio_resets > before:
                times.append(t)
        return times

    def test_the_interval_doubles_with_every_step_up_to_the_maximum(self, monkeypatch):
        h = Harness(monkeypatch, max_restart_interval_s=720.0)
        h.link_up = False

        assert self.step_times(h, 3000) == [180, 540, 1260, 1980, 2700]

    def test_radio_resets_count_as_steps_too(self, monkeypatch):
        h = Harness(monkeypatch, max_restart_interval_s=10_000.0, radio_reset_after_restarts=1)
        h.link_up = False

        assert self.step_times(h, 2700) == [180, 540, 1260, 2700]
        assert (len(h.started), h.radio_resets) == (2, 2)

    def test_the_interval_starts_over_after_the_link_was_up(self, monkeypatch):
        h = Harness(monkeypatch, max_restart_interval_s=10_000.0)
        h.link_up = False
        assert self.step_times(h, 600) == [180, 540]

        h.link_up = True
        h.check_at(630)
        h.link_up = False
        h.check_at(660)
        h.check_at(810)
        assert len(h.started) == 2
        h.check_at(840)
        assert len(h.started) == 3

    def test_never_gives_up(self, monkeypatch):
        h = Harness(monkeypatch, max_restart_interval_s=360.0)
        h.link_up = False

        steps = self.step_times(h, 50_000)

        assert len(steps) > 100
        assert {b - a for a, b in zip(steps[1:], steps[2:])} == {360}


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
