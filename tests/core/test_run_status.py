import pytest

from measurement_software.core.config import QualityThresholds, RunStatusConfig
from measurement_software.core.run_status import RunStatusTracker, SampleQuality, classify_sample
from measurement_software.modems.modem import CellSample


def good_lte_sample(**overrides) -> CellSample:
    """An LTE sample comfortably clearing every default threshold, unless overridden."""
    values = {"rat": "LTE", "rsrp": -80.0, "rsrq": -8.0, "sinr": 12.0} | overrides
    return CellSample(**values)


class TestClassifySample:
    def test_good_when_every_metric_clears_its_threshold(self):
        assert classify_sample(good_lte_sample(), RunStatusConfig()) == SampleQuality.GOOD

    def test_good_at_the_threshold_itself(self):
        sample = good_lte_sample(rsrp=-100.0, rsrq=-11.0, sinr=0.0)
        assert classify_sample(sample, RunStatusConfig()) == SampleQuality.GOOD

    @pytest.mark.parametrize("metric,value", [("rsrp", -110.0), ("rsrq", -14.0), ("sinr", -3.0)])
    def test_bad_when_any_single_metric_falls_short(self, metric, value):
        sample = good_lte_sample(**{metric: value})
        assert classify_sample(sample, RunStatusConfig()) == SampleQuality.BAD

    @pytest.mark.parametrize("metric", ["rsrp", "rsrq", "sinr"])
    def test_invalid_when_a_metric_is_missing(self, metric):
        sample = good_lte_sample(**{metric: None})
        assert classify_sample(sample, RunStatusConfig()) == SampleQuality.INVALID

    @pytest.mark.parametrize("metric,value", [("rsrp", -200.0), ("rsrp", 10.0), ("sinr", 99.0)])
    def test_invalid_when_a_metric_is_outside_its_legal_range(self, metric, value):
        sample = good_lte_sample(**{metric: value})
        assert classify_sample(sample, RunStatusConfig()) == SampleQuality.INVALID

    def test_legal_ranges_are_per_rat(self):
        # -150 dBm RSRP is impossible for LTE but a legal (if weak) NR reading.
        assert classify_sample(good_lte_sample(rsrp=-150.0), RunStatusConfig()) == SampleQuality.INVALID
        nr_sample = good_lte_sample(rat="NR5G-SA", rsrp=-150.0)
        assert classify_sample(nr_sample, RunStatusConfig()) == SampleQuality.BAD

    @pytest.mark.parametrize("rat", ["NR5G-SA", "NR5G-NSA"])
    def test_new_radio_samples_use_the_nr_thresholds(self, rat):
        config = RunStatusConfig(
            lte=QualityThresholds(min_rsrp=-100.0),
            nr=QualityThresholds(min_rsrp=-70.0),
        )
        sample = good_lte_sample(rat=rat, rsrp=-80.0)

        assert classify_sample(sample, config) == SampleQuality.BAD
        assert classify_sample(good_lte_sample(rsrp=-80.0), config) == SampleQuality.GOOD


class TestRunStatusTracker:
    def test_starts_out_empty_and_healthy(self):
        status = RunStatusTracker(RunStatusConfig()).status()

        assert (status.datapoints_total, status.good, status.bad, status.invalid) == (0, 0, 0, 0)
        assert status.pipeline_broken is False

    def test_counts_accumulate_across_captures(self):
        tracker = RunStatusTracker(RunStatusConfig())

        tracker.record_capture([good_lte_sample(), good_lte_sample(rsrp=-120.0)])
        tracker.record_capture([good_lte_sample(sinr=None)])

        status = tracker.status()
        assert (status.datapoints_total, status.good, status.bad, status.invalid) == (3, 1, 1, 1)

    def test_payload_matches_the_agreed_heartbeat_body(self):
        tracker = RunStatusTracker(RunStatusConfig())
        tracker.record_capture([good_lte_sample()])

        assert tracker.status().as_payload() == {
            "since_run_start": {"datapoints_total": 1, "good": 1, "bad": 0, "invalid": 0},
            "pipeline_broken": False,
        }

    def test_pipeline_breaks_after_enough_consecutive_empty_captures(self):
        tracker = RunStatusTracker(RunStatusConfig(empty_captures_until_pipeline_broken=3))

        for _ in range(2):
            tracker.record_capture([])
        assert tracker.status().pipeline_broken is False

        tracker.record_capture([])
        assert tracker.status().pipeline_broken is True

    def test_a_successful_capture_clears_the_empty_streak(self):
        tracker = RunStatusTracker(RunStatusConfig(empty_captures_until_pipeline_broken=2))

        tracker.record_capture([])
        tracker.record_capture([good_lte_sample()])
        tracker.record_capture([])

        assert tracker.status().pipeline_broken is False

    def test_an_empty_capture_does_not_count_as_a_datapoint(self):
        tracker = RunStatusTracker(RunStatusConfig())

        tracker.record_capture([])

        assert tracker.status().datapoints_total == 0
