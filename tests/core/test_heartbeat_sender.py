import json
import logging
import threading
import time
import urllib.error
import urllib.request

import pytest

from measurement_software.core.config import HeartbeatConfig, RunStatusConfig
from measurement_software.core.heartbeat_sender import HeartbeatSender
from measurement_software.core.run_status import RunStatusTracker
from measurement_software.modems.modem import CellSample

URL = "https://backend.example.org/heartbeat"


class FakeResponse:
    def __init__(self, status: int = 204):
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeBackend:
    """Fakes `urlopen` for the heartbeat POST, recording every request it receives.

    `fail_first` makes that many leading sends raise, simulating a dead spot or an
    outage the run has to survive.
    """

    def __init__(self, fail_first: int = 0):
        self.fail_first = fail_first
        self._lock = threading.Lock()
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def urlopen(self, request, timeout=None) -> FakeResponse:
        with self._lock:
            self.requests.append(request)
            self.timeouts.append(timeout)
            should_fail = len(self.requests) <= self.fail_first
        if should_fail:
            raise urllib.error.URLError("network is unreachable")
        return FakeResponse()

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def payloads(self) -> list[dict]:
        with self._lock:
            return [json.loads(request.data.decode()) for request in self.requests]


@pytest.fixture
def backend(monkeypatch) -> FakeBackend:
    fake = FakeBackend()
    monkeypatch.setattr("measurement_software.core.heartbeat_sender.urllib.request.urlopen", fake.urlopen)
    return fake


def wait_for_requests(backend: FakeBackend, count: int, timeout: float = 5.0) -> None:
    """Waits for the sender's background thread to have posted `count` requests."""
    deadline = time.monotonic() + timeout
    while backend.request_count() < count:
        if time.monotonic() > deadline:
            pytest.fail(f"Only {backend.request_count()} of {count} heartbeats were sent in time")
        time.sleep(0.01)


def tracker_with(*samples: CellSample) -> RunStatusTracker:
    tracker = RunStatusTracker(RunStatusConfig())
    tracker.record_capture(list(samples))
    return tracker


class TestHeartbeatSender:
    def test_posts_the_current_run_status_right_away(self, backend):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(HeartbeatConfig(enabled=True, url=URL, interval_s=3600), tracker)

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        request = backend.requests[0]
        assert request.full_url == URL
        assert request.method == "POST"
        assert request.headers["Content-type"] == "application/json"
        assert backend.payloads()[0] == {
            "since_run_start": {"datapoints_total": 1, "good": 1, "bad": 0, "invalid": 0},
            "pipeline_broken": False,
        }
        assert backend.timeouts[0] == 10.0

    def test_keeps_reporting_on_the_configured_interval(self, backend):
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(HeartbeatConfig(enabled=True, url=URL, interval_s=0.01), tracker)

        sender.start()
        wait_for_requests(backend, 3)
        sender.stop()

        assert backend.request_count() >= 3

    def test_each_report_reflects_the_run_so_far(self, backend):
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(HeartbeatConfig(enabled=True, url=URL, interval_s=0.01), tracker)

        sender.start()
        wait_for_requests(backend, 1)
        tracker.record_capture([CellSample(rat="LTE", rsrp=-130.0, rsrq=-15.0, sinr=-5.0)])
        wait_for_requests(backend, backend.request_count() + 2)
        sender.stop()

        assert backend.payloads()[0]["since_run_start"]["datapoints_total"] == 0
        assert backend.payloads()[-1]["since_run_start"] == {
            "datapoints_total": 1, "good": 0, "bad": 1, "invalid": 0,
        }

    def test_survives_a_backend_it_cannot_reach(self, backend):
        backend.fail_first = 2
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(HeartbeatConfig(enabled=True, url=URL, interval_s=0.01), tracker)

        sender.start()
        wait_for_requests(backend, 4)
        sender.stop()

        # The failed sends must not have killed the reporting thread.
        assert backend.request_count() >= 4

    def test_stays_silent_when_disabled(self, backend):
        sender = HeartbeatSender(HeartbeatConfig(enabled=False, url=URL), RunStatusTracker(RunStatusConfig()))

        sender.start()
        sender.stop()

        assert backend.request_count() == 0

    def test_refuses_a_url_that_is_not_https(self, backend, caplog):
        config = HeartbeatConfig(enabled=True, url="http://backend.example.org/heartbeat")
        sender = HeartbeatSender(config, RunStatusTracker(RunStatusConfig()))

        with caplog.at_level(logging.ERROR):
            sender.start()
            sender.stop()

        assert backend.request_count() == 0
        assert "https" in caplog.text

    def test_stopping_without_starting_is_harmless(self, backend):
        HeartbeatSender(HeartbeatConfig(enabled=True, url=URL), RunStatusTracker(RunStatusConfig())).stop()

        assert backend.request_count() == 0
