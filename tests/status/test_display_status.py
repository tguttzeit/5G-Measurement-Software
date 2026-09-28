import threading
import time

import pytest

from measurement_software.core.config import CollectorConfig, DisplayConfig, RunStatusConfig
from measurement_software.core.run_phase import RunPhase
from measurement_software.gnss.gnss_receiver import GNSSFix, Position
from measurement_software.modems.modem import CellSample
from measurement_software.status.backend_status import GpsFixStatus, GpsFixStatusTracker, RunStatus, RunStatusTracker
from measurement_software.status.display_status import (
    MAX_LINE_CHARS,
    MAX_LINES,
    StatusDisplayUpdater,
    active_special_config_flags,
    build_status_message,
    format_gps_fix_status,
)


def run_status(**overrides) -> RunStatus:
    defaults = dict(datapoints_total=0, good=0, bad=0, invalid=0, pipeline_broken=False)
    defaults.update(overrides)
    return RunStatus(**defaults)


def gps_fix_status(**overrides) -> GpsFixStatus:
    defaults = dict(has_fix=False, num_satellites=None, fix_age_s=None)
    defaults.update(overrides)
    return GpsFixStatus(**defaults)


class TestActiveSpecialConfigFlags:
    def test_flags_gps_disabled(self):
        flags = active_special_config_flags(CollectorConfig(gps_enabled=False))

        assert flags == ["GPS disabled"]

    def test_no_flags_when_config_is_all_default(self):
        flags = active_special_config_flags(CollectorConfig())

        assert flags == []


class TestFormatGpsFixStatus:
    def test_no_fix_yet(self):
        assert format_gps_fix_status(gps_fix_status()) == "GPS: no fix"

    def test_fix_with_satellites_and_age(self):
        status = gps_fix_status(has_fix=True, num_satellites=7, fix_age_s=4.8)

        assert format_gps_fix_status(status) == "GPS: fix 7sat 4s"

    def test_fix_with_unknown_satellite_count(self):
        status = gps_fix_status(has_fix=True, num_satellites=None, fix_age_s=1.0)

        assert format_gps_fix_status(status) == "GPS: fix 1s"


class TestBuildStatusMessage:
    def test_includes_short_phase_label_and_quality_counts(self):
        message = build_status_message(
            "active_measuring",
            run_status(datapoints_total=10, good=7, bad=2, invalid=1),
            gps_fix_status(),
            [],
        )

        assert "Measuring" in message
        assert "OK 7" in message
        assert "Bad 2" in message
        assert "Inv 1" in message
        assert "Total 10" in message

    def test_falls_back_to_the_raw_phase_string_outside_the_fixed_vocabulary(self):
        message = build_status_message("starting up", run_status(), gps_fix_status(), [])

        assert "starting up" in message

    def test_includes_gps_fix_status(self):
        message = build_status_message(
            "active_measuring", run_status(), gps_fix_status(has_fix=True, num_satellites=5, fix_age_s=2.0), []
        )

        assert "GPS: fix 5sat 2s" in message

    def test_flags_a_broken_pipeline(self):
        message = build_status_message(
            "active_measuring", run_status(pipeline_broken=True), gps_fix_status(), []
        )

        assert "PIPELINE BROKEN" in message

    def test_does_not_mention_pipeline_when_healthy(self):
        message = build_status_message(
            "active_measuring", run_status(pipeline_broken=False), gps_fix_status(), []
        )

        assert "PIPELINE BROKEN" not in message

    def test_pipeline_broken_appears_before_the_quality_counts(self):
        message = build_status_message(
            "active_measuring", run_status(pipeline_broken=True), gps_fix_status(), []
        )

        lines = message.splitlines()
        assert lines.index("PIPELINE BROKEN") < next(
            i for i, line in enumerate(lines) if line.startswith("OK ")
        )

    def test_lists_active_special_config_flags(self):
        message = build_status_message(
            "active_measuring", run_status(), gps_fix_status(), ["GPS disabled"]
        )

        assert "GPS disabled" in message

    def test_shows_no_special_config_when_none_are_active(self):
        message = build_status_message("active_measuring", run_status(), gps_fix_status(), [])

        assert "No special config" in message

    def test_no_line_exceeds_the_display_width(self):
        message = build_status_message(
            "active_measuring",
            run_status(pipeline_broken=True),
            gps_fix_status(),
            ["a very long special config flag that would never fit on one row of the screen"],
        )

        for line in message.splitlines():
            assert len(line) <= MAX_LINE_CHARS

    def test_the_message_never_has_more_lines_than_the_display_can_show(self):
        message = build_status_message(
            "active_measuring",
            run_status(pipeline_broken=True),
            gps_fix_status(has_fix=True, num_satellites=5, fix_age_s=2.0),
            ["flag one", "flag two", "flag three"],
        )

        assert len(message.splitlines()) <= MAX_LINES

    def test_the_phase_line_survives_even_when_every_other_line_is_at_its_longest(self):
        message = build_status_message(
            "active_measuring",
            run_status(pipeline_broken=True, datapoints_total=999999, good=999999, bad=999999, invalid=999999),
            gps_fix_status(has_fix=True, num_satellites=99, fix_age_s=99999.0),
            ["flag one", "flag two", "flag three", "flag four"],
        )

        assert message.splitlines()[0] == "Measuring"

    def test_pipeline_broken_survives_truncation_ahead_of_the_special_config_flags_line(self):
        message = build_status_message(
            "active_measuring",
            run_status(pipeline_broken=True),
            gps_fix_status(has_fix=True, num_satellites=5, fix_age_s=2.0),
            ["flag one", "flag two", "flag three", "flag four", "flag five"],
        )

        assert "PIPELINE BROKEN" in message


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


def make_display_config(**overrides) -> DisplayConfig:
    defaults = dict(enabled=True, type="ssd1306", i2c_port=1, i2c_address=0x3C, refresh_interval_s=0.01)
    defaults.update(overrides)
    return DisplayConfig(**defaults)


class TestStatusDisplayUpdater:
    def test_refreshes_immediately_and_then_on_the_configured_interval(self):
        display = FakeDisplay()
        updater = StatusDisplayUpdater(
            display, RunPhase("collecting"), GpsFixStatusTracker(), RunStatusTracker(RunStatusConfig()),
            CollectorConfig(), make_display_config(),
        )

        updater.start()
        wait_for_messages(display, 3)
        updater.stop()

        assert display.message_count() >= 3

    def test_reflects_the_current_phase_and_run_status(self):
        display = FakeDisplay()
        run_phase = RunPhase("starting up")
        run_status = RunStatusTracker(RunStatusConfig())
        updater = StatusDisplayUpdater(
            display, run_phase, GpsFixStatusTracker(), run_status, CollectorConfig(), make_display_config()
        )

        updater.start()
        wait_for_messages(display, 1)
        assert "starting up" in display.last_message()

        run_phase.set("collecting")
        run_status.record_capture([CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0)])
        wait_for_messages(display, display.message_count() + 2)
        updater.stop()

        assert "collecting" in display.last_message()
        assert "OK 1" in display.last_message()

    def test_stays_silent_when_disabled(self):
        display = FakeDisplay()
        updater = StatusDisplayUpdater(
            display, RunPhase(), GpsFixStatusTracker(), RunStatusTracker(RunStatusConfig()),
            CollectorConfig(), make_display_config(enabled=False),
        )

        updater.start()
        updater.stop()

        assert display.message_count() == 0

    def test_includes_active_special_config_flags(self):
        display = FakeDisplay()
        updater = StatusDisplayUpdater(
            display, RunPhase(), GpsFixStatusTracker(), RunStatusTracker(RunStatusConfig()),
            CollectorConfig(gps_enabled=False), make_display_config(),
        )

        updater.start()
        wait_for_messages(display, 1)
        updater.stop()

        assert "GPS disabled" in display.last_message()

    def test_reflects_the_current_gps_fix_status(self):
        display = FakeDisplay()
        gps_fix_status_tracker = GpsFixStatusTracker()
        updater = StatusDisplayUpdater(
            display, RunPhase(), gps_fix_status_tracker, RunStatusTracker(RunStatusConfig()),
            CollectorConfig(), make_display_config(),
        )

        updater.start()
        wait_for_messages(display, 1)
        assert "GPS: no fix" in display.last_message()

        gps_fix_status_tracker.record_fix(
            GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=6)
        )
        wait_for_messages(display, display.message_count() + 1)
        updater.stop()

        assert "GPS: fix 6sat" in display.last_message()

    def test_stopping_without_starting_is_harmless(self):
        StatusDisplayUpdater(
            FakeDisplay(), RunPhase(), GpsFixStatusTracker(), RunStatusTracker(RunStatusConfig()),
            CollectorConfig(), make_display_config(),
        ).stop()
