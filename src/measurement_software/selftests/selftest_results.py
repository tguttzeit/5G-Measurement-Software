from dataclasses import dataclass
from enum import StrEnum


class CheckStatus(StrEnum):
    """A selftest check's outcome, matching the backend's `POST /api/testing/results` enum."""

    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one selftest check against a real hardware-facing subsystem."""

    name: str
    status: CheckStatus
    duration_ms: float
    error_message: str | None = None
    details: dict | None = None
