from measurement_software.core.run_phase import RunPhase, RunPhaseState, WaitingLoop, short_phase_label


class TestRunPhase:
    def test_defaults_to_starting_up(self):
        assert RunPhase().get() == "starting up"

    def test_get_reflects_the_latest_set_label(self):
        phase = RunPhase()

        phase.set("collecting")

        assert phase.get() == "collecting"

    def test_can_be_constructed_with_an_initial_label(self):
        assert RunPhase(initial="custom").get() == "custom"

    def test_accepts_the_recovering_pending_data_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.RECOVERING_PENDING_DATA)

        assert phase.get() == "recovering_pending_data"

    def test_accepts_the_waiting_for_gps_fix_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.WAITING_FOR_GPS_FIX)

        assert phase.get() == "waiting_for_gps_fix"

    def test_accepts_the_waiting_for_movement_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.WAITING_FOR_MOVEMENT)

        assert phase.get() == "waiting_for_movement"

    def test_accepts_the_waiting_mode_idle_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.WAITING_MODE_IDLE)

        assert phase.get() == "waiting_mode_idle"

    def test_accepts_the_active_measuring_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.ACTIVE_MEASURING)

        assert phase.get() == "active_measuring"

    def test_accepts_the_finalizing_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.FINALIZING)

        assert phase.get() == "finalizing"

    def test_accepts_the_done_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.DONE)

        assert phase.get() == "done"


class TestShortPhaseLabel:
    def test_maps_every_run_phase_state_to_a_short_fixed_label(self):
        expected = {
            RunPhaseState.RECOVERING_PENDING_DATA: "Recovering data",
            RunPhaseState.WAITING_FOR_GPS_FIX: "No GPS fix",
            RunPhaseState.WAITING_FOR_MOVEMENT: "Waiting: move",
            RunPhaseState.WAITING_MODE_IDLE: "Idle (waiting)",
            RunPhaseState.ACTIVE_MEASURING: "Measuring",
            RunPhaseState.FINALIZING: "Finalizing",
            RunPhaseState.DONE: "Done",
        }

        for state, label in expected.items():
            assert short_phase_label(state) == label

    def test_falls_back_to_the_raw_string_for_a_phase_outside_the_fixed_vocabulary(self):
        assert short_phase_label("starting up") == "starting up"


class TestWaitingLoop:
    def test_wait_returns_immediately_if_already_requested(self, monkeypatch):
        loop = WaitingLoop()
        loop.request_start_measuring()

        sleep_calls = []
        monkeypatch.setattr(
            "measurement_software.core.run_phase.time.sleep", lambda s: sleep_calls.append(s)
        )

        loop.wait()

        assert sleep_calls == []

    def test_wait_blocks_until_start_measuring_is_requested(self, monkeypatch):
        loop = WaitingLoop()
        calls = []

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) == 3:
                loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait()

        assert len(calls) == 3

    def test_request_start_measuring_is_idempotent(self, monkeypatch):
        loop = WaitingLoop()
        loop.request_start_measuring()
        loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", lambda s: None)
        loop.wait()  # must not hang or raise

    def test_wait_runs_the_selftest_callback_when_requested_then_keeps_waiting(self, monkeypatch):
        loop = WaitingLoop()
        loop.request_run_selftest()
        selftest_calls = []

        def fake_sleep(seconds):
            loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait(on_run_selftest=lambda: selftest_calls.append(True))

        assert selftest_calls == [True]

    def test_run_selftest_request_does_not_end_the_wait_by_itself(self, monkeypatch):
        loop = WaitingLoop()
        loop.request_run_selftest()
        calls = []

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) == 2:
                loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait(on_run_selftest=lambda: None)

        assert len(calls) == 2

    def test_wait_without_a_selftest_callback_ignores_a_pending_selftest_request(self, monkeypatch):
        loop = WaitingLoop()
        loop.request_run_selftest()

        def fake_sleep(seconds):
            loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait()  # must not hang or raise with no on_run_selftest given

    def test_request_run_selftest_is_idempotent(self, monkeypatch):
        loop = WaitingLoop()
        loop.request_run_selftest()
        loop.request_run_selftest()
        selftest_calls = []

        def fake_sleep(seconds):
            loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait(on_run_selftest=lambda: selftest_calls.append(True))

        assert selftest_calls == [True]
