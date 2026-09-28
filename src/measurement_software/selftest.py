import argparse
import logging
import sys
from pathlib import Path

from measurement_software.core.config import AppConfig, load_config
from measurement_software.selftests.selftest_results import CheckResult, CheckStatus

# Re-export for backwards compatibility and test access
from measurement_software.core.util import setup_logging as setup_logging
from measurement_software.selftests.selftest_backend import (
    SelftestResultsReporter as SelftestResultsReporter,
)
from measurement_software.selftests.selftest_checks import (
    available_checks as available_checks,
)
from measurement_software.selftests.selftest_runner import (
    run_checks as run_checks,
)

logger = logging.getLogger(__name__)

_STATUS_MARKERS = {
    CheckStatus.PASSED: "PASS",
    CheckStatus.FAILED: "FAIL",
    CheckStatus.SKIPPED: "SKIP",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses selftest CLI arguments: which checks to run, and whether to allow side effects."""
    parser = argparse.ArgumentParser(
        prog="python -m measurement_software.selftest",
        description=(
            "Runs manual hardware-reachability checks against a device's real modem, GNSS "
            "receiver, and other configured hardware, for verifying wiring over SSH. Reads "
            "config.toml the same way the main application does - no separate configuration."
        ),
    )
    parser.add_argument(
        "checks", nargs="*", default=[],
        help="Which checks to run (default: all). One or more of: " + ", ".join(available_checks()),
    )
    parser.add_argument(
        "--confirm-side-effects", action="store_true",
        help=(
            "Also run checks that have a real external side effect (currently: one real "
            "throwaway file uploaded to the configured backend via the uploader check). "
            "Never triggers a real system shutdown - selftest has no shutdown check."
        ),
    )
    args = parser.parse_args(argv)

    valid_checks = {*available_checks(), "all"}
    unknown = [name for name in args.checks if name not in valid_checks]
    if unknown:
        parser.error(f"invalid check(s): {', '.join(unknown)} (choose from {', '.join(sorted(valid_checks))})")

    return args


def load_app_config() -> AppConfig:
    """Loads config.toml the same way main.py does - selftest uses no separate configuration."""
    config_path = Path(__file__).resolve().parent.parent.parent / "config.toml"
    return load_config(config_path)


def main(argv: list[str] | None = None) -> int:
    """Runs the requested selftest checks and prints a pass/fail line per check.

    Returns 0 if every check passed or was skipped, 1 if any check failed.
    """
    args = parse_args(argv)
    config = load_app_config()
    setup_logging(config.logging)

    names = available_checks() if not args.checks or "all" in args.checks else args.checks
    reporter = SelftestResultsReporter(
        config.selftest, device_id=config.heartbeat.device_id, device_key=config.backend.device_key,
    )

    results = run_checks(config, names, confirm_side_effects=args.confirm_side_effects, reporter=reporter)
    for result in results:
        print(_format_result(result))

    return 0 if all(r.status in (CheckStatus.PASSED, CheckStatus.SKIPPED) for r in results) else 1


def _format_result(result: CheckResult) -> str:
    line = f"[{_STATUS_MARKERS.get(result.status, result.status.value.upper())}] {result.name} ({result.duration_ms:.0f} ms)"
    if result.error_message:
        line += f": {result.error_message}"
    return line


if __name__ == "__main__":
    sys.exit(main())
