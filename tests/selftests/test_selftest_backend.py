import json
import urllib.error
import urllib.request

import pytest

from measurement_software.core.config import SelftestConfig
from measurement_software.selftests.selftest_backend import SelftestResultsReporter
from measurement_software.selftests.selftest_results import CheckStatus

URL = "https://backend.example.org/api/testing/results"


class FakeResponse:
    def __init__(self, status: int = 204):
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeBackend:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.requests: list[urllib.request.Request] = []

    def urlopen(self, request, timeout=None) -> FakeResponse:
        self.requests.append(request)
        if self.fail:
            raise urllib.error.URLError("network is unreachable")
        return FakeResponse()

    def payloads(self) -> list[dict]:
        return [json.loads(r.data.decode()) for r in self.requests]


@pytest.fixture
def backend(monkeypatch) -> FakeBackend:
    fake = FakeBackend()
    monkeypatch.setattr("measurement_software.selftests.selftest_backend.urllib.request.urlopen", fake.urlopen)
    return fake


class TestSelftestResultsReporter:
    def test_submits_full_payload_with_is_testing_always_true(self, backend):
        reporter = SelftestResultsReporter(
            SelftestConfig(results_url=URL), device_id="pi-north-01", device_key="secret-key",
        )

        reporter.submit(
            "modem", CheckStatus.PASSED, duration_ms=42.0, error_message=None, details={"cell_samples": 1},
        )

        request = backend.requests[0]
        assert request.full_url == URL
        assert request.method == "POST"
        assert backend.payloads()[0] == {
            "device_id": "pi-north-01",
            "test_type": "modem",
            "status": "passed",
            "duration_ms": 42.0,
            "error_message": None,
            "details": {"cell_samples": 1},
            "is_testing": True,
        }

    def test_rounds_duration_ms_to_an_integer(self, backend):
        """The backend's TestResultCreate.duration_ms is a strict int - a fractional value
        (the norm, since it comes from a wall-clock measurement) gets a 422 otherwise."""
        reporter = SelftestResultsReporter(
            SelftestConfig(results_url=URL), device_id="pi-north-01", device_key="secret-key",
        )

        reporter.submit("modem", CheckStatus.PASSED, duration_ms=1005.663)

        assert backend.payloads()[0]["duration_ms"] == 1006

    def test_omits_duration_ms_when_none(self, backend):
        reporter = SelftestResultsReporter(
            SelftestConfig(results_url=URL), device_id="pi-north-01", device_key="secret-key",
        )

        reporter.submit("modem", CheckStatus.RUNNING, duration_ms=None)

        assert backend.payloads()[0]["duration_ms"] is None

    def test_authenticates_with_device_id_and_device_key_headers(self, backend):
        reporter = SelftestResultsReporter(
            SelftestConfig(results_url=URL), device_id="pi-north-01", device_key="secret-key",
        )

        reporter.submit("gps", CheckStatus.RUNNING)

        request = backend.requests[0]
        assert request.headers["X-device-id"] == "pi-north-01"
        assert request.headers["X-device-key"] == "secret-key"

    def test_does_not_send_when_results_url_is_unconfigured(self, backend):
        reporter = SelftestResultsReporter(SelftestConfig(results_url=""), device_id="pi", device_key="key")

        reporter.submit("modem", CheckStatus.PASSED)

        assert backend.requests == []

    def test_does_not_send_when_results_url_is_not_https(self, backend):
        reporter = SelftestResultsReporter(
            SelftestConfig(results_url="http://backend.example.org/api/testing/results"),
            device_id="pi", device_key="key",
        )

        reporter.submit("modem", CheckStatus.PASSED)

        assert backend.requests == []

    def test_survives_a_backend_it_cannot_reach(self, backend):
        backend.fail = True
        reporter = SelftestResultsReporter(SelftestConfig(results_url=URL), device_id="pi", device_key="key")

        reporter.submit("modem", CheckStatus.FAILED, error_message="serial timeout")

        assert len(backend.requests) == 1

    def test_omits_device_key_header_when_not_configured(self, backend):
        reporter = SelftestResultsReporter(SelftestConfig(results_url=URL), device_id="pi", device_key="")

        reporter.submit("modem", CheckStatus.PASSED)

        assert "X-device-key" not in backend.requests[0].headers
