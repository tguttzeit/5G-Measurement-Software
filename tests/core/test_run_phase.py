from measurement_software.core.run_phase import RunPhase


class TestRunPhase:
    def test_defaults_to_starting_up(self):
        assert RunPhase().get() == "starting up"

    def test_get_reflects_the_latest_set_label(self):
        phase = RunPhase()

        phase.set("collecting")

        assert phase.get() == "collecting"

    def test_can_be_constructed_with_an_initial_label(self):
        assert RunPhase(initial="custom").get() == "custom"
