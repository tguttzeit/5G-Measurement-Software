from measurement_software.core.config import AppConfig
from measurement_software.selftests.selftest_backend import SelftestResultsReporter
from measurement_software.selftests.selftest_checks import BACKEND_TEST_TYPES, run_check
from measurement_software.selftests.selftest_results import CheckResult


def run_checks(
    config: AppConfig, names: list[str], *, confirm_side_effects: bool, reporter: SelftestResultsReporter,
) -> list[CheckResult]:
    """Runs each named check in order, reporting backend-supported ones once they finish.

    Does not submit a RUNNING row first: the backend's `POST /testing/results` is create-only
    (no way to later update a row to its final status), so a RUNNING submission would just be an
    orphaned row with no way to ever resolve it - every manual selftest run would permanently
    clutter the dashboard with a "still running" entry per check.
    """
    results = []
    for name in names:
        result = run_check(name, config, confirm_side_effects=confirm_side_effects)

        if name in BACKEND_TEST_TYPES:
            reporter.submit(
                name, result.status, duration_ms=result.duration_ms,
                error_message=result.error_message, details=result.details,
            )
        results.append(result)
    return results
