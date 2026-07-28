from measurement_software.core.config import CollectorConfig
from measurement_software.core.display_status import active_special_config_flags, build_status_message
from measurement_software.core.run_status import RunStatus


def run_status(**overrides) -> RunStatus:
    defaults = dict(datapoints_total=0, good=0, bad=0, invalid=0, pipeline_broken=False)
    defaults.update(overrides)
    return RunStatus(**defaults)


class TestActiveSpecialConfigFlags:
    def test_flags_gps_disabled(self):
        flags = active_special_config_flags(CollectorConfig(gps_enabled=False))

        assert flags == ["GPS disabled"]

    def test_no_flags_when_config_is_all_default(self):
        flags = active_special_config_flags(CollectorConfig())

        assert flags == []


class TestBuildStatusMessage:
    def test_includes_phase_and_quality_counts(self):
        message = build_status_message(
            "collecting", run_status(datapoints_total=10, good=7, bad=2, invalid=1), []
        )

        assert "collecting" in message
        assert "OK 7" in message
        assert "Bad 2" in message
        assert "Inv 1" in message
        assert "Total 10" in message

    def test_flags_a_broken_pipeline(self):
        message = build_status_message("collecting", run_status(pipeline_broken=True), [])

        assert "PIPELINE BROKEN" in message

    def test_does_not_mention_pipeline_when_healthy(self):
        message = build_status_message("collecting", run_status(pipeline_broken=False), [])

        assert "PIPELINE BROKEN" not in message

    def test_lists_active_special_config_flags(self):
        message = build_status_message("collecting", run_status(), ["GPS disabled"])

        assert "GPS disabled" in message

    def test_shows_no_special_config_when_none_are_active(self):
        message = build_status_message("collecting", run_status(), [])

        assert "No special config" in message
