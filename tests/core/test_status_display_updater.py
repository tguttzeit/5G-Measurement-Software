import threading
import time

import pytest

from measurement_software.core.config import CollectorConfig, DisplayConfig, RunStatusConfig
from measurement_software.core.run_phase import RunPhase
from measurement_software.core.run_status import RunStatusTracker
from measurement_software.core.status_display_updater import StatusDisplayUpdater
from measurement_software.modems.modem import CellSample


class FakeDisplay:
    def __init__(self):
        self._lock = threading.Lock()
        self.messages: list[str] = []

    def show(self, message: str) -> None:
        with self._lock:
            self.messages.append(message)

    def message_count(self) -> int:
        with self._lock:
            return len(self.messages)

    def last_message(self) -> str:
        with self._lock:
            return self.messages[-1]


def wait_for_messages(display: FakeDisplay, count: int, timeout: float = 5.0) -> None:
    """Waits for the updater's background thread to have shown `count` messages."""
    deadline = time.monotonic() + timeout
    while display.message_count() < count:
        if time.monotonic() > deadline:
            pytest.fail(f"Only {display.message_count()} of {count} refreshes happened in time")
        time.sleep(0.01)


def make_config(**overrides) -> DisplayConfig:
    defaults = dict(enabled=True, type="ssd1306", i2c_port=1, i2c_address=0x3C, refresh_interval_s=0.01)
    defaults.update(overrides)
    return DisplayConfig(**defaults)


class TestStatusDisplayUpdater:
    def test_refreshes_immediately_and_then_on_the_configured_interval(self):
        display = FakeDisplay()
        updater = StatusDisplayUpdater(
            display, RunPhase("collecting"), RunStatusTracker(RunStatusConfig()),
            CollectorConfig(), make_config(),
        )

        updater.start()
        wait_for_messages(display, 3)
        updater.stop()

        assert display.message_count() >= 3

    def test_reflects_the_current_phase_and_run_status(self):
        display = FakeDisplay()
        run_phase = RunPhase("uploading pending data")
        run_status = RunStatusTracker(RunStatusConfig())
        updater = StatusDisplayUpdater(display, run_phase, run_status, CollectorConfig(), make_config())

        updater.start()
        wait_for_messages(display, 1)
        assert "uploading pending data" in display.last_message()

        run_phase.set("collecting")
        run_status.record_capture([CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0)])
        wait_for_messages(display, display.message_count() + 2)
        updater.stop()

        assert "collecting" in display.last_message()
        assert "OK 1" in display.last_message()

    def test_stays_silent_when_disabled(self):
        display = FakeDisplay()
        updater = StatusDisplayUpdater(
            display, RunPhase(), RunStatusTracker(RunStatusConfig()),
            CollectorConfig(), make_config(enabled=False),
        )

        updater.start()
        updater.stop()

        assert display.message_count() == 0

    def test_includes_active_special_config_flags(self):
        display = FakeDisplay()
        updater = StatusDisplayUpdater(
            display, RunPhase(), RunStatusTracker(RunStatusConfig()),
            CollectorConfig(gps_enabled=False), make_config(),
        )

        updater.start()
        wait_for_messages(display, 1)
        updater.stop()

        assert "GPS disabled" in display.last_message()

    def test_stopping_without_starting_is_harmless(self):
        StatusDisplayUpdater(
            FakeDisplay(), RunPhase(), RunStatusTracker(RunStatusConfig()),
            CollectorConfig(), make_config(),
        ).stop()
