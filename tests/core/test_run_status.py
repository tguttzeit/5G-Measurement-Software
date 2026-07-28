import pytest

from measurement_software.core.config import QualityThresholds, RunStatusConfig
from measurement_software.core.run_status import SampleQuality, classify_sample
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
