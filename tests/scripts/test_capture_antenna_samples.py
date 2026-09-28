import json
from pathlib import Path

from capture_antenna_samples import AntennaSample, capture, save
from measurement_software.modems.modem import CellSample, Modem


class FakeClock:
    """Controls the time_fn/sleep_fn seen by capture()."""

    def __init__(self):
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeModem(Modem):
    """Yields a scripted sequence of query_cell_info() results, one per call.

    An entry that is an Exception instance is raised instead of returned, to simulate a
    transient query failure.
    """

    def __init__(self, results: list[list[CellSample] | Exception]):
        self._results = results
        self._index = 0
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def unlock_sim(self) -> None:
        pass

    def power_down(self) -> None:
        pass

    def reset_radio(self) -> None:
        pass

    def query_cell_info(self) -> list[CellSample]:
        result = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        if isinstance(result, Exception):
            raise result
        return result


LTE_SAMPLE = CellSample(rat="LTE", rsrp=-90.0, rsrq=-10.0, sinr=5.0)


class TestCapture:
    def test_queries_once_per_interval_until_duration_elapses(self):
        clock = FakeClock()
        modem = FakeModem([[LTE_SAMPLE]] * 5)
        samples = capture(
            modem, "external", duration_s=10.0, interval_s=2.0,
            time_fn=clock.time, sleep_fn=clock.sleep,
        )
        assert len(samples) == 5

    def test_tags_each_sample_with_label_and_timestamp(self):
        clock = FakeClock()
        modem = FakeModem([[LTE_SAMPLE]])
        samples = capture(
            modem, "board", duration_s=1.0, interval_s=2.0,
            time_fn=clock.time, sleep_fn=clock.sleep,
        )
        assert len(samples) == 1
        assert samples[0].label == "board"
        assert samples[0].cell_sample == LTE_SAMPLE
        assert samples[0].timestamp.endswith("Z")

    def test_pairs_multiple_cell_samples_from_one_query_under_one_timestamp(self):
        clock = FakeClock()
        nr_sample = CellSample(rat="NR5G-NSA", rsrp=-95.0)
        modem = FakeModem([[LTE_SAMPLE, nr_sample]])
        samples = capture(
            modem, "external", duration_s=1.0, interval_s=2.0,
            time_fn=clock.time, sleep_fn=clock.sleep,
        )
        assert len(samples) == 2
        assert samples[0].timestamp == samples[1].timestamp

    def test_skips_a_failed_query_and_keeps_going(self):
        clock = FakeClock()
        modem = FakeModem([RuntimeError("boom"), [LTE_SAMPLE]])
        samples = capture(
            modem, "external", duration_s=10.0, interval_s=2.0,
            time_fn=clock.time, sleep_fn=clock.sleep,
        )
        assert len(samples) == 4  # first of the 5 iterations raised and was skipped, the rest succeeded

    def test_closes_modem_after_a_normal_run(self):
        clock = FakeClock()
        modem = FakeModem([[LTE_SAMPLE]] * 3)
        capture(modem, "external", duration_s=6.0, interval_s=2.0, time_fn=clock.time, sleep_fn=clock.sleep)
        assert modem.opened
        assert modem.closed

    def test_keyboard_interrupt_keeps_partial_samples_and_still_closes_modem(self):
        clock = FakeClock()

        class InterruptingModem(FakeModem):
            def query_cell_info(self) -> list[CellSample]:
                if self._index == 2:
                    raise KeyboardInterrupt()
                return super().query_cell_info()

        modem = InterruptingModem([[LTE_SAMPLE]] * 10)
        samples = capture(
            modem, "external", duration_s=100.0, interval_s=2.0,
            time_fn=clock.time, sleep_fn=clock.sleep,
        )
        assert len(samples) == 2
        assert modem.closed


class TestSave:
    def test_writes_samples_as_json_and_creates_parent_dirs(self, tmp_path: Path):
        output_path = tmp_path / "nested" / "external.json"
        samples = [AntennaSample(timestamp="2026-01-01T00:00:00.000000Z", label="external", cell_sample=LTE_SAMPLE)]

        save(samples, output_path)

        with open(output_path) as f:
            written = json.load(f)
        assert written == [
            {
                "timestamp": "2026-01-01T00:00:00.000000Z",
                "label": "external",
                "cell_sample": {
                    "rat": "LTE", "mcc": None, "mnc": None, "cell_id": None, "pci": None, "tac": None,
                    "rsrp": -90.0, "rsrq": -10.0, "rssi": None, "sinr": 5.0, "band": None, "channel": None,
                    "vendor_specific_extras": None,
                },
            }
        ]

    def test_writes_empty_list_for_no_samples(self, tmp_path: Path):
        output_path = tmp_path / "empty.json"
        save([], output_path)
        with open(output_path) as f:
            assert json.load(f) == []
