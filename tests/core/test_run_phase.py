import pytest

from measurement_software.core.run_phase import RunPhase, RunPhaseState, ManualModeLoop, short_phase_label


class TestRunPhase:
    def test_defaults_to_the_starting_up_state(self):
        assert RunPhase().get() == RunPhaseState.STARTING_UP == "starting_up"

    def test_accepts_the_stopping_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.STOPPING)

        assert phase.get() == "stopping"

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

    def test_accepts_the_manual_mode_idle_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.MANUAL_MODE_IDLE)

        assert phase.get() == "manual_mode_idle"

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

    def test_accepts_the_shutting_down_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.SHUTTING_DOWN)

        assert phase.get() == "shutting_down"

    def test_accepts_the_rebooting_state(self):
        phase = RunPhase()

        phase.set(RunPhaseState.REBOOTING)

        assert phase.get() == "rebooting"


class TestRecoveryIdleState:
    def test_is_reported_as_recovery_idle(self):
        phase = RunPhase()

        phase.set(RunPhaseState.RECOVERY_IDLE)

        assert phase.get() == "recovery_idle"


class TestShortPhaseLabel:
    def test_maps_every_run_phase_state_to_a_short_fixed_label(self):
        expected = {
            RunPhaseState.RECOVERING_PENDING_DATA: "Recovering data",
            RunPhaseState.WAITING_FOR_GPS_FIX: "No GPS fix",
            RunPhaseState.WAITING_FOR_MOVEMENT: "Waiting: move",
            RunPhaseState.MANUAL_MODE_IDLE: "Idle (manual)",
            RunPhaseState.RECOVERY_IDLE: "Idle (recovery)",
            RunPhaseState.ACTIVE_MEASURING: "Measuring",
            RunPhaseState.FINALIZING: "Finalizing",
            RunPhaseState.DONE: "Done",
            RunPhaseState.SHUTTING_DOWN: "SHUTTING DOWN",
            RunPhaseState.REBOOTING: "REBOOTING",
        }

        for state, label in expected.items():
            assert short_phase_label(state) == label

    def test_falls_back_to_the_raw_string_for_a_phase_outside_the_fixed_vocabulary(self):
        assert short_phase_label("starting up") == "starting up"


class TestManualModeLoop:
    def test_wait_returns_immediately_if_already_requested(self, monkeypatch):
        loop = ManualModeLoop()
        loop.request_start_measuring()

        sleep_calls = []
        monkeypatch.setattr(
            "measurement_software.core.run_phase.time.sleep", lambda s: sleep_calls.append(s)
        )

        loop.wait()

        assert sleep_calls == []

    def test_wait_blocks_until_start_measuring_is_requested(self, monkeypatch):
        loop = ManualModeLoop()
        calls = []

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) == 3:
                loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait()

        assert len(calls) == 3

    def test_a_start_request_is_consumed_by_the_wait_it_ends(self, monkeypatch):
        loop = ManualModeLoop()
        loop.request_start_measuring()
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)
        loop.wait()

        loop.wait()

        assert len(sleeps) == 2

    def test_cancel_start_request_makes_the_next_wait_block_again(self, monkeypatch):
        loop = ManualModeLoop()
        loop.request_start_measuring()
        loop.cancel_start_request()
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)
        loop.wait()

        assert len(sleeps) == 2

    def test_request_start_measuring_is_idempotent(self, monkeypatch):
        loop = ManualModeLoop()
        loop.request_start_measuring()
        loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", lambda s: None)
        loop.wait()  # must not hang or raise

    def test_wait_runs_the_selftest_callback_when_requested_then_keeps_waiting(self, monkeypatch):
        loop = ManualModeLoop()
        loop.request_run_selftest()
        selftest_calls = []

        def fake_sleep(seconds):
            loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait(on_run_selftest=lambda: selftest_calls.append(True))

        assert selftest_calls == [True]

    def test_run_selftest_request_does_not_end_the_wait_by_itself(self, monkeypatch):
        loop = ManualModeLoop()
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
        loop = ManualModeLoop()
        loop.request_run_selftest()

        def fake_sleep(seconds):
            loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait()  # must not hang or raise with no on_run_selftest given

    def test_request_run_selftest_is_idempotent(self, monkeypatch):
        loop = ManualModeLoop()
        loop.request_run_selftest()
        loop.request_run_selftest()
        selftest_calls = []

        def fake_sleep(seconds):
            loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait(on_run_selftest=lambda: selftest_calls.append(True))

        assert selftest_calls == [True]

    def test_calls_on_check_remote_shutdown_once_per_iteration(self, monkeypatch):
        loop = ManualModeLoop()
        calls = []

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) == 3:
                loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        shutdown_calls = []
        loop.wait(on_check_remote_shutdown=lambda: shutdown_calls.append(1))

        # One call per iteration, including the final one that observes start_measuring.
        assert len(shutdown_calls) == 4

    def test_propagates_an_exception_from_on_check_remote_shutdown_before_checking_anything_else(
        self, monkeypatch,
    ):
        loop = ManualModeLoop()
        sleep_calls = []
        monkeypatch.setattr(
            "measurement_software.core.run_phase.time.sleep", lambda s: sleep_calls.append(s)
        )

        def on_check_remote_shutdown():
            raise RuntimeError("remote shutdown-now command received")

        with pytest.raises(RuntimeError, match="remote shutdown-now command received"):
            loop.wait(on_check_remote_shutdown=on_check_remote_shutdown)

        assert sleep_calls == []

    def test_defaults_to_no_hook_without_breaking_the_wait(self, monkeypatch):
        loop = ManualModeLoop()

        def fake_sleep(seconds):
            loop.request_start_measuring()

        monkeypatch.setattr("measurement_software.core.run_phase.time.sleep", fake_sleep)

        loop.wait()  # must not hang or raise with no on_check_remote_shutdown given
