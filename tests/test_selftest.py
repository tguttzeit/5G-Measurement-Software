import types

import pytest

from measurement_software import selftest
from measurement_software.selftests.selftest_results import CheckResult, CheckStatus


class TestParseArgs:
    def test_defaults_to_running_all_checks(self):
        args = selftest.parse_args([])

        assert args.checks == []
        assert args.confirm_side_effects is False

    def test_accepts_specific_check_names(self):
        args = selftest.parse_args(["modem", "gps"])

        assert args.checks == ["modem", "gps"]

    def test_accepts_confirm_side_effects_flag(self):
        args = selftest.parse_args(["uploader", "--confirm-side-effects"])

        assert args.confirm_side_effects is True

    def test_rejects_an_unknown_check_name(self):
        with pytest.raises(SystemExit):
            selftest.parse_args(["not-a-real-check"])


class TestMain:
    def _stub(self, monkeypatch, results: list[CheckResult], recorded_calls: dict):
        fake_config = types.SimpleNamespace(
            logging=object(),
            heartbeat=types.SimpleNamespace(device_id=""),
            backend=types.SimpleNamespace(device_key=""),
            selftest=object(),
        )
        monkeypatch.setattr(selftest, "load_app_config", lambda: recorded_calls.setdefault("config", fake_config))
        monkeypatch.setattr(selftest, "setup_logging", lambda config: recorded_calls.setdefault("logging_setup", True))
        monkeypatch.setattr(selftest, "SelftestResultsReporter", lambda *a, **k: recorded_calls.setdefault("reporter", object()))

        def fake_run_checks(config, names, *, confirm_side_effects, reporter):
            recorded_calls["names"] = names
            recorded_calls["confirm_side_effects"] = confirm_side_effects
            return results

        monkeypatch.setattr(selftest, "run_checks", fake_run_checks)

    def test_runs_all_checks_by_default_and_returns_zero_when_all_pass(self, monkeypatch, capsys):
        recorded: dict = {}
        self._stub(monkeypatch, [
            CheckResult(name="modem", status=CheckStatus.PASSED, duration_ms=5.0),
            CheckResult(name="display", status=CheckStatus.SKIPPED, duration_ms=1.0, error_message="disabled"),
        ], recorded)

        exit_code = selftest.main([])

        assert exit_code == 0
        assert recorded["names"] == selftest.available_checks()
        assert recorded["confirm_side_effects"] is False
        out = capsys.readouterr().out
        assert "[PASS] modem" in out
        assert "[SKIP] display" in out
        assert "disabled" in out

    def test_returns_one_when_any_check_fails(self, monkeypatch):
        recorded: dict = {}
        self._stub(monkeypatch, [
            CheckResult(name="modem", status=CheckStatus.FAILED, duration_ms=5.0, error_message="AT timeout"),
        ], recorded)

        exit_code = selftest.main(["modem"])

        assert exit_code == 1
        assert recorded["names"] == ["modem"]

    def test_passes_confirm_side_effects_through(self, monkeypatch):
        recorded: dict = {}
        self._stub(monkeypatch, [
            CheckResult(name="uploader", status=CheckStatus.PASSED, duration_ms=5.0),
        ], recorded)

        selftest.main(["uploader", "--confirm-side-effects"])

        assert recorded["confirm_side_effects"] is True

    def test_never_calls_perform_shutdown(self, monkeypatch):
        # selftest must never trigger a real system shutdown, regardless of flags passed.
        assert not hasattr(selftest, "perform_shutdown")
