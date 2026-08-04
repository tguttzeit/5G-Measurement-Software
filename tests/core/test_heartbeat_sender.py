import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from measurement_software.core.config import HeartbeatConfig, RunStatusConfig, StorageConfig
from measurement_software.core.heartbeat_sender import HeartbeatSender
from measurement_software.core.run_status import RunStatusTracker
from measurement_software.core.storage_status import StorageStatusReporter
from measurement_software.modems.modem import CellSample

URL = "https://backend.example.org/heartbeat"


class FakeDiskUsage:
    def __init__(self, free: int):
        self.free = free


@pytest.fixture(autouse=True)
def disk_usage(monkeypatch) -> FakeDiskUsage:
    """Fakes shutil.disk_usage so the storage stat in the heartbeat payload is deterministic."""
    fake = FakeDiskUsage(free=10_000_000_000)
    monkeypatch.setattr("measurement_software.core.storage_status.shutil.disk_usage", lambda path: fake)
    return fake


def storage_status_for(tmp_path: Path) -> StorageStatusReporter:
    return StorageStatusReporter(tmp_path / "uploads", StorageConfig())


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
    def test_posts_the_current_run_status_right_away(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, url=URL, interval_s=3600), tracker, storage_status_for(tmp_path),
        )

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
            "storage": {"pending_files": 0, "disk_free_bytes": 10_000_000_000},
        }
        assert backend.timeouts[0] == 10.0

    def test_posts_device_id_in_payload(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, url=URL, interval_s=3600, device_id="pi-north-01"),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        payload = backend.payloads()[0]
        assert payload["device_id"] == "pi-north-01"

    def test_posts_device_key_in_header(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, url=URL, interval_s=3600, device_key="test-api-key-123"),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        request = backend.requests[0]
        assert request.headers["X-device-key"] == "test-api-key-123"

    def test_posts_both_device_id_and_device_key(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(
                enabled=True, url=URL, interval_s=3600,
                device_id="pi-north-01", device_key="test-api-key-123"
            ),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        request = backend.requests[0]
        payload = backend.payloads()[0]
        assert payload["device_id"] == "pi-north-01"
        assert request.headers["X-device-key"] == "test-api-key-123"

    def test_keeps_reporting_on_the_configured_interval(self, backend, tmp_path):
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, url=URL, interval_s=0.01), tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 3)
        sender.stop()

        assert backend.request_count() >= 3

    def test_each_report_reflects_the_run_so_far(self, backend, tmp_path):
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, url=URL, interval_s=0.01), tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        tracker.record_capture([CellSample(rat="LTE", rsrp=-130.0, rsrq=-15.0, sinr=-5.0)])
        wait_for_requests(backend, backend.request_count() + 2)
        sender.stop()

        assert backend.payloads()[0]["since_run_start"]["datapoints_total"] == 0
        assert backend.payloads()[-1]["since_run_start"] == {
            "datapoints_total": 1, "good": 0, "bad": 1, "invalid": 0,
        }

    def test_each_report_reflects_the_current_backlog(self, backend, tmp_path):
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, url=URL, interval_s=0.01), tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        (upload_dir / "gps_5g_20260101_000000.json").write_text("[]")
        wait_for_requests(backend, backend.request_count() + 2)
        sender.stop()

        assert backend.payloads()[0]["storage"]["pending_files"] == 0
        assert backend.payloads()[-1]["storage"]["pending_files"] == 1

    def test_survives_a_backend_it_cannot_reach(self, backend, tmp_path):
        backend.fail_first = 2
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, url=URL, interval_s=0.01), tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 4)
        sender.stop()

        # The failed sends must not have killed the reporting thread.
        assert backend.request_count() >= 4

    def test_stays_silent_when_disabled(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=False, url=URL), RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        sender.start()
        sender.stop()

        assert backend.request_count() == 0

    def test_refuses_a_url_that_is_not_https(self, backend, caplog, tmp_path):
        config = HeartbeatConfig(enabled=True, url="http://backend.example.org/heartbeat")
        sender = HeartbeatSender(config, RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path))

        with caplog.at_level(logging.ERROR):
            sender.start()
            sender.stop()

        assert backend.request_count() == 0
        assert "https" in caplog.text

    def test_stopping_without_starting_is_harmless(self, backend, tmp_path):
        HeartbeatSender(
            HeartbeatConfig(enabled=True, url=URL), RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        ).stop()

        assert backend.request_count() == 0
