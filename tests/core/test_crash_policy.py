import json
import logging

import pytest

from measurement_software.core.crash_policy import (
    CRASH_REPORT_FILENAME,
    CRASH_STATE_FILENAME,
    CrashAction,
    CrashPolicy,
    error_fingerprint,
)


def raise_from_here(error: Exception) -> Exception:
    """Raises and catches `error`, so it carries a real traceback through this test module."""
    try:
        raise error
    except Exception as e:
        return e


def raise_elsewhere(error: Exception) -> Exception:
    try:
        raise error
    except Exception as e:
        return e


@pytest.fixture
def policy(tmp_path) -> CrashPolicy:
    return CrashPolicy(tmp_path, same_error_limit=3, reboot_limit=5)


class TestErrorFingerprint:
    def test_combines_exception_type_and_raising_function(self):
        fingerprint = error_fingerprint(raise_from_here(ValueError("x")))

        assert fingerprint.startswith("ValueError@")
        assert fingerprint.endswith(":raise_from_here")

    def test_ignores_the_message(self):
        first = error_fingerprint(raise_from_here(OSError("could not open /dev/ttyUSB2")))
        second = error_fingerprint(raise_from_here(OSError("could not open /dev/ttyUSB3")))

        assert first == second

    def test_differs_by_raising_location(self):
        assert error_fingerprint(raise_from_here(ValueError())) != error_fingerprint(raise_elsewhere(ValueError()))

    def test_differs_by_type(self):
        assert error_fingerprint(raise_from_here(ValueError())) != error_fingerprint(raise_from_here(KeyError()))

    def test_error_without_traceback_still_has_a_fingerprint(self):
        assert error_fingerprint(ValueError()) == "ValueError@unknown"


class TestDecision:
    def test_first_crash_reboots(self, policy):
        decision = policy.record(raise_from_here(ValueError()))

        assert decision.action is CrashAction.REBOOT
        assert decision.same_error_count == 1
        assert decision.consecutive_reboots == 0

    def test_same_error_reaching_the_limit_escalates_to_the_recovery_state(self, policy):
        actions = [policy.record(raise_from_here(ValueError())).action for _ in range(3)]

        assert actions == [CrashAction.REBOOT, CrashAction.REBOOT, CrashAction.RECOVERY]

    def test_a_different_error_restarts_the_same_error_count(self, policy):
        policy.record(raise_from_here(ValueError()))
        policy.record(raise_from_here(ValueError()))
        decision = policy.record(raise_elsewhere(ValueError()))

        assert decision.action is CrashAction.REBOOT
        assert decision.same_error_count == 1

    def test_alternating_errors_escalate_once_the_reboot_limit_is_used_up(self, policy):
        errors = [raise_from_here, raise_elsewhere] * 3
        actions = [policy.record(make(ValueError())).action for make in errors]

        assert actions == [CrashAction.REBOOT] * 5 + [CrashAction.RECOVERY]

    def test_counters_survive_a_new_policy_instance_as_after_a_reboot(self, tmp_path):
        CrashPolicy(tmp_path, same_error_limit=3, reboot_limit=5).record(raise_from_here(ValueError()))
        CrashPolicy(tmp_path, same_error_limit=3, reboot_limit=5).record(raise_from_here(ValueError()))

        decision = CrashPolicy(tmp_path, same_error_limit=3, reboot_limit=5).record(raise_from_here(ValueError()))

        assert decision.action is CrashAction.RECOVERY

    def test_any_error_after_escalating_to_the_recovery_state_enters_maintenance(self, policy):
        for _ in range(3):
            policy.record(raise_from_here(ValueError()))
        assert not policy.in_maintenance()

        decision = policy.record(raise_elsewhere(KeyError()))

        assert decision.action is CrashAction.ENTER_MAINTENANCE
        assert policy.in_maintenance()
        assert policy.details()["fingerprint"] == decision.fingerprint

    def test_maintenance_survives_a_new_policy_instance_and_further_errors(self, policy, tmp_path):
        for _ in range(4):
            policy.record(raise_from_here(ValueError()))

        again = CrashPolicy(tmp_path, same_error_limit=3, reboot_limit=5)

        assert again.in_maintenance()
        assert again.record(raise_from_here(ValueError())).action is CrashAction.ENTER_MAINTENANCE

    def test_the_recovery_state_is_reported_until_it_is_consumed(self, policy):
        assert not policy.in_recovery()
        for _ in range(3):
            policy.record(raise_from_here(ValueError()))

        assert policy.in_recovery()
        assert not policy.in_maintenance()

        policy.consume_recovery()

        assert not policy.in_recovery()
        assert not policy.in_maintenance()

    def test_consuming_keeps_the_counters_and_survives_a_new_policy_instance(self, policy, tmp_path):
        for _ in range(3):
            policy.record(raise_from_here(ValueError()))
        details = policy.details()

        policy.consume_recovery()

        again = CrashPolicy(tmp_path, same_error_limit=3, reboot_limit=5)
        assert not again.in_recovery()
        assert again.details() == details
        assert details == {"fingerprint": details["fingerprint"], "same_error_count": 3, "consecutive_reboots": 3}

    def test_consuming_without_a_recovery_state_changes_nothing(self, policy, tmp_path):
        policy.consume_recovery()
        assert not (tmp_path / CRASH_STATE_FILENAME).exists()

        policy.record(raise_from_here(ValueError()))
        before = (tmp_path / CRASH_STATE_FILENAME).read_text()
        policy.consume_recovery()

        assert (tmp_path / CRASH_STATE_FILENAME).read_text() == before

    @pytest.mark.parametrize("error", [ValueError, KeyError])
    def test_a_crash_after_consuming_the_recovery_ends_in_maintenance_not_in_another_recovery(self, policy, error):
        for _ in range(3):
            policy.record(raise_from_here(ValueError()))
        policy.consume_recovery()

        decision = policy.record(raise_from_here(error()))

        assert decision.action is CrashAction.ENTER_MAINTENANCE
        assert policy.in_maintenance()

    def test_a_normal_run_after_consuming_the_recovery_clears_everything(self, policy):
        for _ in range(3):
            policy.record(raise_from_here(ValueError()))
        policy.consume_recovery()

        policy.reset()

        assert policy.record(raise_from_here(ValueError())).action is CrashAction.REBOOT

    def test_a_state_file_with_the_former_waiting_escalation_counts_as_recovery(self, policy, tmp_path):
        (tmp_path / CRASH_STATE_FILENAME).write_text(json.dumps(
            {"fingerprint": "X@y:z", "same_error_count": 3, "consecutive_reboots": 2, "escalation": "waiting"}
        ))

        assert policy.in_recovery()

    def test_a_normal_run_after_escalating_clears_the_escalation(self, policy):
        for _ in range(3):
            policy.record(raise_from_here(ValueError()))

        policy.reset()

        assert policy.record(raise_from_here(ValueError())).action is CrashAction.REBOOT

    def test_no_state_means_no_maintenance(self, policy):
        assert not policy.in_maintenance()

    def test_reset_after_a_normal_run_clears_the_counters(self, policy, tmp_path):
        policy.record(raise_from_here(ValueError()))
        policy.record(raise_from_here(ValueError()))

        policy.reset()
        decision = policy.record(raise_from_here(ValueError()))

        assert decision.same_error_count == 1
        assert decision.consecutive_reboots == 0

    def test_reset_without_state_is_a_noop(self, policy, tmp_path):
        policy.reset()

        assert not (tmp_path / CRASH_STATE_FILENAME).exists()

    def test_corrupt_state_is_treated_as_no_previous_crash(self, policy, tmp_path, caplog):
        caplog.set_level(logging.WARNING)
        (tmp_path / CRASH_STATE_FILENAME).write_text("{not json")

        decision = policy.record(raise_from_here(ValueError()))

        assert decision.action is CrashAction.REBOOT
        assert decision.same_error_count == 1
        assert CRASH_STATE_FILENAME in caplog.text


class TestCrashReport:
    def test_report_holds_decision_and_full_traceback(self, policy, tmp_path):
        error = raise_from_here(ValueError("details here"))
        decision = policy.record(error)

        policy.write_report(decision, error)

        report = json.loads((tmp_path / CRASH_REPORT_FILENAME).read_text())
        assert report["action"] == "reboot"
        assert report["fingerprint"] == decision.fingerprint
        assert report["same_error_count"] == 1
        assert report["consecutive_reboots"] == 0
        assert "raise_from_here" in report["traceback"]
        assert "details here" in report["traceback"]
        assert report["timestamp"].endswith("Z")

    def test_next_boot_logs_the_previous_report_once(self, policy, tmp_path, caplog):
        caplog.set_level(logging.WARNING)
        error = raise_from_here(ValueError("details here"))
        policy.write_report(policy.record(error), error)

        policy.log_previous_report()
        policy.log_previous_report()

        assert caplog.text.count("Previous run ended with an unhandled error") == 1
        assert "details here" in caplog.text
        assert not (tmp_path / CRASH_REPORT_FILENAME).exists()

    def test_no_previous_report_logs_nothing(self, policy, caplog):
        caplog.set_level(logging.WARNING)

        policy.log_previous_report()

        assert caplog.text == ""
