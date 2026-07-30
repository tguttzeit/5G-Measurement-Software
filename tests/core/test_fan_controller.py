import logging
import time

import pytest

from measurement_software.core.config import FanConfig
from measurement_software.core.fan_controller import FanController


class FakeSystem:
    """Fakes the GPIO/temperature functions FanController calls from core.system."""

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
def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    monkeypatch.setattr("measurement_software.core.fan_controller.system.setup_fan_gpio", fake.setup_fan_gpio)
    monkeypatch.setattr("measurement_software.core.fan_controller.system.set_fan_state", fake.set_fan_state)
    monkeypatch.setattr(
        "measurement_software.core.fan_controller.system.read_cpu_temperature_celsius",
        fake.read_cpu_temperature_celsius,
    )
    return fake


def config(**overrides) -> FanConfig:
    defaults = dict(enabled=True, gpio_pin=27, temp_on_celsius=70.0, temp_off_celsius=60.0, poll_interval_s=5.0)
    return FanConfig(**(defaults | overrides))


class TestUpdate:
    def test_turns_fan_on_once_temperature_reaches_on_threshold(self, fake_system):
        controller = FanController(config())
        fake_system.temperature = 70.0

        controller._update()

        assert fake_system.fan_state_calls == [(27, True)]

    def test_leaves_fan_off_below_on_threshold(self, fake_system):
        controller = FanController(config())
        fake_system.temperature = 69.9

        controller._update()

        assert fake_system.fan_state_calls == []

    def test_leaves_fan_on_between_thresholds_once_already_on(self, fake_system):
        controller = FanController(config())
        fake_system.temperature = 70.0
        controller._update()

        fake_system.temperature = 65.0
        controller._update()

        assert fake_system.fan_state_calls == [(27, True)]

    def test_turns_fan_off_once_temperature_reaches_off_threshold(self, fake_system):
        controller = FanController(config())
        fake_system.temperature = 70.0
        controller._update()

        fake_system.temperature = 60.0
        controller._update()

        assert fake_system.fan_state_calls == [(27, True), (27, False)]

    def test_does_not_turn_fan_back_on_immediately_after_turning_off(self, fake_system):
        controller = FanController(config())
        fake_system.temperature = 70.0
        controller._update()
        fake_system.temperature = 60.0
        controller._update()

        fake_system.temperature = 65.0
        controller._update()

        assert fake_system.fan_state_calls == [(27, True), (27, False)]

    def test_logs_warning_and_keeps_state_when_temperature_read_fails(self, fake_system, caplog):
        caplog.set_level(logging.WARNING)
        controller = FanController(config())
        fake_system.read_error = OSError("no such file")

        controller._update()

        assert fake_system.fan_state_calls == []
        assert "Could not read CPU temperature" in caplog.text


class TestStartStop:
    def test_disabled_does_not_touch_gpio_or_spawn_a_thread(self, fake_system):
        controller = FanController(config(enabled=False))

        controller.start()
        controller.stop()

        assert fake_system.setup_calls == []

    def test_enabled_sets_up_gpio_on_start(self, fake_system):
        controller = FanController(config())

        controller.start()
        controller.stop()

        assert fake_system.setup_calls == [27]

    def test_stop_without_start_is_a_no_op(self, fake_system):
        controller = FanController(config())

        controller.stop()

    def test_polls_and_updates_fan_state_on_background_thread(self, fake_system):
        fake_system.temperature = 75.0
        controller = FanController(config(poll_interval_s=0.01))

        controller.start()
        try:
            deadline = time.monotonic() + 5.0
            while not fake_system.fan_state_calls:
                if time.monotonic() > deadline:
                    pytest.fail("Fan was never turned on by the background thread")
                time.sleep(0.01)
        finally:
            controller.stop()

        assert fake_system.fan_state_calls[0] == (27, True)
