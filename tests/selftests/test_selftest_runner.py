from measurement_software.selftests.selftest_backend import SelftestResultsReporter
from measurement_software.selftests.selftest_results import CheckResult, CheckStatus
from measurement_software.selftests.selftest_runner import run_checks


class FakeReporter(SelftestResultsReporter):
    def __init__(self):
        self.submissions: list[tuple] = []

    def submit(self, test_type, status, *, duration_ms=None, error_message=None, details=None) -> None:
        self.submissions.append((test_type, status, duration_ms, error_message, details))


def fake_run_check_factory(results: dict[str, CheckResult]):
    def fake_run_check(name, config, *, confirm_side_effects):
        return results[name]
    return fake_run_check


class TestRunChecks:
    def test_submits_only_the_final_status_for_backend_supported_checks(self, monkeypatch):
        """No RUNNING row is submitted first: POST /testing/results is create-only, so a RUNNING
        submission could never be resolved to a final status and would just orphan the dashboard."""
        result = CheckResult(name="modem", status=CheckStatus.PASSED, duration_ms=10.0, details={"cell_samples": 1})
        monkeypatch.setattr(
            "measurement_software.selftests.selftest_runner.run_check",
            fake_run_check_factory({"modem": result}),
        )
        reporter = FakeReporter()

        results = run_checks(config=None, names=["modem"], confirm_side_effects=False, reporter=reporter)

        assert results == [result]
        assert reporter.submissions == [
            ("modem", CheckStatus.PASSED, 10.0, None, {"cell_samples": 1}),
        ]

    def test_does_not_submit_for_local_only_checks(self, monkeypatch):
        result = CheckResult(name="fan", status=CheckStatus.PASSED, duration_ms=5.0)
        monkeypatch.setattr(
            "measurement_software.selftests.selftest_runner.run_check",
            fake_run_check_factory({"fan": result}),
        )
        reporter = FakeReporter()

        run_checks(config=None, names=["fan"], confirm_side_effects=False, reporter=reporter)

        assert reporter.submissions == []

    def test_runs_checks_in_the_given_order_and_collects_all_results(self, monkeypatch):
        modem_result = CheckResult(name="modem", status=CheckStatus.PASSED, duration_ms=1.0)
        fan_result = CheckResult(name="fan", status=CheckStatus.FAILED, duration_ms=2.0, error_message="no sysfs")
        monkeypatch.setattr(
            "measurement_software.selftests.selftest_runner.run_check",
            fake_run_check_factory({"modem": modem_result, "fan": fan_result}),
        )
        reporter = FakeReporter()

        results = run_checks(config=None, names=["modem", "fan"], confirm_side_effects=False, reporter=reporter)

        assert results == [modem_result, fan_result]
