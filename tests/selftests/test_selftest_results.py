from measurement_software.selftests.selftest_results import CheckResult, CheckStatus


def test_check_status_values_match_backend_enum():
    assert CheckStatus.RUNNING == "running"
    assert CheckStatus.PASSED == "passed"
    assert CheckStatus.FAILED == "failed"
    assert CheckStatus.SKIPPED == "skipped"


def test_check_result_defaults_error_message_and_details_to_none():
    result = CheckResult(name="modem", status=CheckStatus.PASSED, duration_ms=12.5)

    assert result.error_message is None
    assert result.details is None
