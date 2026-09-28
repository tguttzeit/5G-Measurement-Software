import json
import logging
import urllib.request
from http.client import HTTPException

from measurement_software.core.config import SelftestConfig
from measurement_software.selftests.selftest_results import CheckStatus

_SUBMIT_TIMEOUT_S = 10.0


class SelftestResultsReporter:
    """Posts selftest check results to the backend's `POST /api/testing/results`.

    Reuses the heartbeat device credentials for authentication - no separate credential is
    introduced for this feature. `is_testing` is always sent as true: every submission from
    this reporter is definitionally test data, never a real measurement.
    """

    def __init__(self, config: SelftestConfig, device_id: str, device_key: str):
        self._logger = logging.getLogger(__name__)
        self._url = config.results_url
        self._device_id = device_id
        self._device_key = device_key

    def is_usable(self) -> bool:
        """Reports whether results should be submitted, saying loudly why if they shouldn't."""
        if not self._url:
            self._logger.info("No selftest.results_url configured - reporting locally only.")
            return False
        if not self._url.startswith("https://"):
            self._logger.error(
                "selftest.results_url needs an https:// url but is configured with %r - "
                "reporting locally only.", self._url,
            )
            return False
        return True

    def submit(
        self, test_type: str, status: CheckStatus, *,
        duration_ms: float | None = None, error_message: str | None = None, details: dict | None = None,
    ) -> None:
        """Posts one check's result, treating a failed send as a skipped submission, not an error.

        A backend that is unreachable must never take a manual selftest run down with it - the
        check's own pass/fail is still reported to the console either way.
        """
        if not self.is_usable():
            return

        payload = {
            "device_id": self._device_id,
            "test_type": test_type,
            "status": status.value,
            "duration_ms": round(duration_ms) if duration_ms is not None else None,
            "error_message": error_message,
            "details": details,
            "is_testing": True,
        }
        headers = {"Content-Type": "application/json", "X-Device-ID": self._device_id}
        if self._device_key:
            headers["X-Device-Key"] = self._device_key

        request = urllib.request.Request(
            self._url, data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_SUBMIT_TIMEOUT_S) as response:
                self._logger.debug("Selftest result submitted (HTTP %s)", response.status)
        except (OSError, HTTPException) as e:
            self._logger.warning("Could not submit selftest result to backend: %s", e)
