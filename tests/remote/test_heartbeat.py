import hashlib
import hmac
import json
import logging
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from measurement_software.core.collector import Collector
from measurement_software.core.config import (
    BackendConfig,
    HeartbeatConfig,
    RunStatusConfig,
    StorageConfig,
)
from measurement_software.core.config_sources import read_overrides_file, write_overrides_file
from measurement_software.core.run_phase import RunPhase, RunPhaseState, WaitingLoop
from measurement_software.core.uploader import Uploader
from measurement_software.modems.modem import CellSample
from measurement_software.remote.gps_fix_test import GpsFixTestRunner
from measurement_software.remote.heartbeat import HeartbeatSender, RemoteCommandDispatcher
from measurement_software.status.backend_status import (
    GpsFixStatusTracker,
    RunStatusTracker,
    StorageStatus,
    StorageStatusReporter,
)

SECRET = "test-hmac-secret"


def sign(body: dict, secret: str = SECRET) -> str:
    """Mirrors the backend's own signing algorithm (app/response_signing.py)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    digest = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


NONCE = "request-nonce"


def signed_response(nonce: str | None = NONCE, **fields) -> dict:
    """A correctly signed response body echoing `nonce` (pass None to omit it)."""
    if nonce is not None:
        fields = {**fields, "nonce": nonce}
    return {**fields, "signature": sign(fields)}


class FakeConfigStatus:
    def __init__(self, payload: dict):
        self._payload = payload

    def as_payload(self) -> dict:
        return self._payload

BACKEND_URL = "https://backend.example.org"
URL = "https://backend.example.org/heartbeat"


class FakeDiskUsage:
    def __init__(self, free: int):
        self.free = free


@pytest.fixture(autouse=True)
def disk_usage(monkeypatch) -> FakeDiskUsage:
    """Fakes shutil.disk_usage so the storage stat in the heartbeat payload is deterministic."""
    fake = FakeDiskUsage(free=10_000_000_000)
    monkeypatch.setattr("measurement_software.status.backend_status.shutil.disk_usage", lambda path: fake)
    return fake


def storage_status_for(tmp_path: Path) -> StorageStatusReporter:
    return StorageStatusReporter(tmp_path / "uploads", StorageConfig())


def make_backend_config(**overrides) -> BackendConfig:
    defaults = dict(url=BACKEND_URL, device_key="")
    defaults.update(overrides)
    return BackendConfig(**defaults)


class FakeResponse:
    def __init__(self, status: int = 204, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeBackend:
    """Fakes `urlopen` for the heartbeat POST, recording every request it receives.

    `fail_first` makes that many leading sends raise, simulating a dead spot or an
    outage the run has to survive. `response_body` scripts the body of every successful
    response, for tests exercising the remote-command response-handling path; `respond_with`,
    if set, builds that body from the nonce the request carried, as the real backend does.
    """

    def __init__(self, fail_first: int = 0, response_body: bytes = b""):
        self.fail_first = fail_first
        self.response_body = response_body
        self.respond_with = None
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
        if self.respond_with is not None:
            return FakeResponse(body=self.respond_with(json.loads(request.data.decode())["nonce"]))
        return FakeResponse(body=self.response_body)

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def payloads(self) -> list[dict]:
        with self._lock:
            return [json.loads(request.data.decode()) for request in self.requests]


@pytest.fixture
def backend(monkeypatch) -> FakeBackend:
    fake = FakeBackend()
    monkeypatch.setattr("measurement_software.remote.heartbeat.urllib.request.urlopen", fake.urlopen)
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
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        request = backend.requests[0]
        assert request.full_url == URL
        assert request.method == "POST"
        assert request.headers["Content-type"] == "application/json"
        payload = backend.payloads()[0]
        assert len(payload.pop("nonce")) == 32
        assert payload == {
            "since_run_start": {"datapoints_total": 1, "valid": 1, "invalid": 0},
            "pipeline_broken": False,
            "storage": {"pending_files": 0, "disk_free_bytes": 10_000_000_000},
        }
        assert backend.timeouts[0] == 10.0

    def test_omits_current_config_when_no_config_status_is_wired(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert "current_config" not in backend.payloads()[0]

    def test_posts_the_current_config_when_wired(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        config_status = FakeConfigStatus({"rsrp_threshold": -100.0, "idle_threshold_s": 200})
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            tracker, storage_status_for(tmp_path), config_status,
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert backend.payloads()[0]["current_config"] == {"rsrp_threshold": -100.0, "idle_threshold_s": 200}

    def test_posts_device_id_in_payload(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600, device_id="pi-north-01"),
            make_backend_config(), tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        payload = backend.payloads()[0]
        assert payload["device_id"] == "pi-north-01"

    def test_posts_device_id_as_header(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600, device_id="pi-north-01"),
            make_backend_config(), tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        request = backend.requests[0]
        assert request.headers["X-device-id"] == "pi-north-01"

    def test_no_device_id_header_when_not_configured(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        request = backend.requests[0]
        assert "X-device-id" not in request.headers

    def test_posts_device_key_in_header(self, backend, tmp_path):
        tracker = tracker_with(CellSample(rat="LTE", rsrp=-80.0, rsrq=-8.0, sinr=12.0))
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600),
            make_backend_config(device_key="test-api-key-123"),
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
            HeartbeatConfig(enabled=True, interval_s=3600, device_id="pi-north-01"),
            make_backend_config(device_key="test-api-key-123"),
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
            HeartbeatConfig(enabled=True, interval_s=0.01), make_backend_config(),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 3)
        sender.stop()

        assert backend.request_count() >= 3

    def test_each_report_reflects_the_run_so_far(self, backend, tmp_path):
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=0.01), make_backend_config(),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        tracker.record_capture([CellSample(rat="LTE", rsrp=-130.0, rsrq=-15.0, sinr=-5.0)])
        wait_for_requests(backend, backend.request_count() + 2)
        sender.stop()

        assert backend.payloads()[0]["since_run_start"]["datapoints_total"] == 0
        assert backend.payloads()[-1]["since_run_start"] == {
            "datapoints_total": 1, "valid": 1, "invalid": 0,
        }

    def test_each_report_reflects_the_current_backlog(self, backend, tmp_path):
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()
        tracker = RunStatusTracker(RunStatusConfig())
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=0.01), make_backend_config(),
            tracker, storage_status_for(tmp_path),
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
            HeartbeatConfig(enabled=True, interval_s=0.01), make_backend_config(),
            tracker, storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 4)
        sender.stop()

        # The failed sends must not have killed the reporting thread.
        assert backend.request_count() >= 4

    def test_stays_silent_when_disabled(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=False), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        sender.start()
        sender.stop()

        assert backend.request_count() == 0

    def test_refuses_a_url_that_is_not_https(self, backend, caplog, tmp_path):
        config = HeartbeatConfig(enabled=True)
        sender = HeartbeatSender(
            config, make_backend_config(url="http://backend.example.org"),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        with caplog.at_level(logging.ERROR):
            sender.start()
            sender.stop()

        assert backend.request_count() == 0
        assert "https" in caplog.text

    def test_stopping_without_starting_is_harmless(self, backend, tmp_path):
        HeartbeatSender(
            HeartbeatConfig(enabled=True), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        ).stop()

        assert backend.request_count() == 0


class TestDeliveryLogging:
    """Successful heartbeats are logged at INFO - throttled - so delivery can be confirmed from
    the device's own log, not only from the backend."""

    @pytest.fixture
    def clock(self, monkeypatch):
        clock = types.SimpleNamespace(now=1000.0)
        monkeypatch.setattr("measurement_software.remote.heartbeat.time.monotonic", lambda: clock.now)
        return clock

    @staticmethod
    def make_sender(tmp_path) -> HeartbeatSender:
        return HeartbeatSender(
            HeartbeatConfig(enabled=True, success_log_interval_s=600), make_backend_config(),
            tracker_with(), storage_status_for(tmp_path),
        )

    @staticmethod
    def send(sender: HeartbeatSender) -> bool:
        return sender.send_once(tracker_with().status(), StorageStatus(pending_files=0, disk_free_bytes=1))

    @staticmethod
    def delivery_lines(caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records
                if r.levelno == logging.INFO and "delivered" in r.getMessage()]

    def test_first_delivery_is_logged(self, backend, tmp_path, clock, caplog):
        caplog.set_level(logging.INFO)

        self.send(self.make_sender(tmp_path))

        assert self.delivery_lines(caplog) == ["First heartbeat delivered (HTTP 204)."]

    def test_further_deliveries_are_summarized_once_per_interval(self, backend, tmp_path, clock, caplog):
        caplog.set_level(logging.INFO)
        sender = self.make_sender(tmp_path)
        self.send(sender)
        for _ in range(3):
            clock.now += 150
            self.send(sender)
        assert len(self.delivery_lines(caplog)) == 1

        clock.now += 150
        self.send(sender)

        assert self.delivery_lines(caplog)[-1] == "4 heartbeat(s) delivered in the last 600 s."

    def test_first_delivery_after_failures_is_logged_with_their_count(self, tmp_path, clock, caplog, monkeypatch):
        failing_then_ok = FakeBackend(fail_first=3)
        monkeypatch.setattr("measurement_software.remote.heartbeat.urllib.request.urlopen", failing_then_ok.urlopen)
        caplog.set_level(logging.INFO)
        sender = self.make_sender(tmp_path)

        results = [self.send(sender) for _ in range(4)]

        assert results == [False, False, False, True]
        assert self.delivery_lines(caplog) == ["Heartbeat delivered again (HTTP 204) after 3 failed attempt(s)."]


class TestCriticalError:
    def test_payload_carries_the_critical_error_when_given(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True), make_backend_config(), tracker_with(), storage_status_for(tmp_path),
            critical_error={"fingerprint": "ValueError@x:y", "action": "debug via SSH"},
        )

        sender.send_once(tracker_with().status(), StorageStatus(pending_files=0, disk_free_bytes=1))

        assert backend.payloads()[0]["critical_error"] == {"fingerprint": "ValueError@x:y", "action": "debug via SSH"}

    def test_payload_has_no_critical_error_by_default(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True), make_backend_config(), tracker_with(), storage_status_for(tmp_path),
        )

        sender.send_once(tracker_with().status(), StorageStatus(pending_files=0, disk_free_bytes=1))

        assert "critical_error" not in backend.payloads()[0]


class TestRunPhaseAndGpsFixStatus:
    def test_omits_run_phase_when_not_wired(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert "run_phase" not in backend.payloads()[0]

    def test_includes_the_current_run_phase_when_wired(self, backend, tmp_path):
        run_phase = RunPhase()
        run_phase.set(RunPhaseState.WAITING_FOR_MOVEMENT)
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
            run_phase=run_phase,
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert backend.payloads()[0]["run_phase"] == "waiting_for_movement"

    def test_each_report_reflects_the_latest_run_phase(self, backend, tmp_path):
        run_phase = RunPhase()
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=0.01), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
            run_phase=run_phase,
        )

        sender.start()
        wait_for_requests(backend, 1)
        run_phase.set(RunPhaseState.ACTIVE_MEASURING)
        wait_for_requests(backend, backend.request_count() + 2)
        sender.stop()

        assert backend.payloads()[-1]["run_phase"] == "active_measuring"

    def test_omits_gps_fix_when_not_wired(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert "gps_fix" not in backend.payloads()[0]

    def test_includes_gps_fix_status_when_wired(self, backend, tmp_path):
        gps_fix_status = GpsFixStatusTracker()
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
            gps_fix_status=gps_fix_status,
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert backend.payloads()[0]["gps_fix"] == {
            "has_fix": False, "num_satellites": None, "fix_age_s": None, "diagnostic": None,
        }


class TestHeartbeatSenderRemoteCommandsWiring:
    def test_hands_a_successful_response_body_to_the_remote_command_dispatcher(self, backend, tmp_path):
        backend.response_body = json.dumps({"status": "ok", "signature": "hmac-sha256:abc"}).encode()
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )
        remote_commands = MagicMock()
        sender.set_remote_commands(remote_commands)

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        remote_commands.handle_response.assert_called_once_with(
            {"status": "ok", "signature": "hmac-sha256:abc"}, backend.payloads()[0]["nonce"]
        )

    def test_a_malformed_response_body_does_not_crash_the_sender(self, backend, tmp_path, caplog):
        backend.response_body = b"not json"
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )
        remote_commands = MagicMock()
        sender.set_remote_commands(remote_commands)

        with caplog.at_level(logging.WARNING):
            sender.start()
            wait_for_requests(backend, 1)
            sender.stop()

        remote_commands.handle_response.assert_not_called()
        assert "Could not parse heartbeat response body" in caplog.text

    def test_a_config_override_write_failure_is_not_misreported_as_a_network_failure(
        self, backend, tmp_path, caplog,
    ):
        """A real (not mocked) RemoteCommandDispatcher whose overrides file cannot be written must
        still let the heartbeat itself be reported as sent - the write failure is a distinct,
        separately-logged problem, never folded into "Heartbeat could not be sent"."""
        backend.respond_with = lambda nonce: json.dumps(signed_response(
            nonce=nonce, status="ok", config_overrides={"idle_threshold_s": 300},
        )).encode()
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )
        overrides_path = tmp_path / "missing_dir" / "config_overrides.toml"
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=overrides_path,
            uploader=MagicMock(spec=Uploader),
            collector=MagicMock(spec=Collector),
        )
        sender.set_remote_commands(dispatcher)

        with caplog.at_level(logging.ERROR):
            sender.start()
            wait_for_requests(backend, 1)
            sender.stop()

        assert backend.request_count() == 1
        assert "Heartbeat could not be sent" not in caplog.text
        assert str(overrides_path) in caplog.text

    def test_without_a_dispatcher_set_the_response_body_is_never_read(self, backend, tmp_path):
        backend.response_body = b"not json"
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        # Malformed body is never even parsed when nothing wants it - no crash either way.
        assert backend.request_count() == 1


class TestHeartbeatSenderAcks:
    def test_includes_pending_acks_from_a_wired_dispatcher(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )
        dispatcher = MagicMock()
        dispatcher.pending_acks.return_value = [{"id": 1, "command": "force-upload-now", "status": "success", "error": None}]
        sender.set_remote_commands(dispatcher)

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert backend.payloads()[0]["executed_commands"] == [
            {"id": 1, "command": "force-upload-now", "status": "success", "error": None},
        ]
        dispatcher.clear_acks.assert_called_once()

    def test_does_not_clear_acks_when_the_send_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "measurement_software.remote.heartbeat.urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(OSError("unreachable")),
        )
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )
        dispatcher = MagicMock()
        dispatcher.pending_acks.return_value = [{"id": 1, "command": "force-upload-now", "status": "success", "error": None}]
        sender.set_remote_commands(dispatcher)

        sender.send_once(RunStatusTracker(RunStatusConfig()).status(), storage_status_for(tmp_path).status())

        dispatcher.clear_acks.assert_not_called()

    def test_no_executed_commands_key_when_nothing_is_pending(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert "executed_commands" not in backend.payloads()[0]

    def test_extra_executed_commands_is_sent_without_any_dispatcher_wired(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        sender.send_once(
            RunStatusTracker(RunStatusConfig()).status(), storage_status_for(tmp_path).status(),
            extra_executed_commands=[{"id": 9, "command": "reboot-now", "status": "received", "error": None}],
        )

        assert backend.payloads()[0]["executed_commands"] == [
            {"id": 9, "command": "reboot-now", "status": "received", "error": None},
        ]

    def test_send_pending_acks_now_sends_one_heartbeat_when_something_is_queued(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )
        dispatcher = MagicMock()
        dispatcher.pending_acks.return_value = [
            {"id": 1, "command": "stop-measuring-now", "status": "success", "error": None},
        ]
        sender.set_remote_commands(dispatcher)

        sender.send_pending_acks_now()

        assert backend.request_count() == 1
        assert backend.payloads()[0]["executed_commands"] == [
            {"id": 1, "command": "stop-measuring-now", "status": "success", "error": None},
        ]

    def test_send_pending_acks_now_is_a_no_op_when_nothing_is_queued(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )
        dispatcher = MagicMock()
        dispatcher.pending_acks.return_value = []
        sender.set_remote_commands(dispatcher)

        sender.send_pending_acks_now()

        assert backend.request_count() == 0

    def test_send_pending_acks_now_is_a_no_op_without_any_dispatcher_wired(self, backend, tmp_path):
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

        sender.send_pending_acks_now()

        assert backend.request_count() == 0


@pytest.fixture
def uploader() -> MagicMock:
    return MagicMock(spec=Uploader)


@pytest.fixture
def collector() -> MagicMock:
    return MagicMock(spec=Collector)


@pytest.fixture
def waiting_loop() -> MagicMock:
    return MagicMock(spec=WaitingLoop)


@pytest.fixture
def gps_fix_test_runner() -> MagicMock:
    return MagicMock(spec=GpsFixTestRunner)


def write_minimal_config_toml(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[collector]\n'
        'max_idle_time = 200\n'
        '\n'
        '[heartbeat]\n'
        'interval_s = 60.0\n'
    )
    return path


@pytest.fixture
def dispatcher(
    tmp_path, uploader, collector, waiting_loop, gps_fix_test_runner,
) -> RemoteCommandDispatcher:
    return RemoteCommandDispatcher(
        hmac_secret=SECRET,
        overrides_path=tmp_path / "config_overrides.toml",
        uploader=uploader,
        collector=collector,
        waiting_loop=waiting_loop,
        gps_fix_test_runner=gps_fix_test_runner,
        config_path=write_minimal_config_toml(tmp_path),
    )


class TestSignatureVerification:
    def test_ignores_a_response_with_nothing_queued(self, dispatcher, uploader):
        dispatcher.handle_response({"status": "ok", "signature": "bogus"}, NONCE)

        uploader.upload_pending_files.assert_not_called()

    def test_discards_commands_when_signature_is_missing(self, dispatcher, uploader, caplog):
        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response({"commands": [{"id": 1, "command": "force-upload-now"}]}, NONCE)

        uploader.upload_pending_files.assert_not_called()
        assert "signature invalid" in caplog.text

    def test_discards_commands_when_signature_is_wrong(self, dispatcher, uploader, caplog):
        body = {"commands": [{"id": 1, "command": "force-upload-now"}], "signature": "hmac-sha256:deadbeef"}

        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response(body, NONCE)

        uploader.upload_pending_files.assert_not_called()

    def test_discards_commands_when_secret_is_not_configured(self, tmp_path, uploader, collector):
        unconfigured = RemoteCommandDispatcher(
            hmac_secret="", overrides_path=tmp_path / "config_overrides.toml",
            uploader=uploader, collector=collector,
        )
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])

        unconfigured.handle_response(body, NONCE)

        uploader.upload_pending_files.assert_not_called()

    def test_applies_commands_with_a_valid_signature(self, dispatcher, uploader):
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])

        dispatcher.handle_response(body, NONCE)

        uploader.upload_pending_files.assert_called_once()


class TestReplayProtection:
    def test_a_matching_nonce_is_accepted(self, dispatcher, uploader):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "force-upload-now"}]), NONCE)

        uploader.upload_pending_files.assert_called_once()

    def test_a_correctly_signed_response_with_a_wrong_nonce_is_discarded(self, dispatcher, uploader, caplog):
        body = signed_response(nonce="some-other-nonce", commands=[{"id": 1, "command": "force-upload-now"}])

        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response(body, NONCE)

        uploader.upload_pending_files.assert_not_called()
        assert "nonce" in caplog.text

    def test_a_correctly_signed_response_without_a_nonce_is_discarded(self, dispatcher, uploader):
        body = signed_response(nonce=None, commands=[{"id": 1, "command": "force-upload-now"}])

        dispatcher.handle_response(body, NONCE)

        uploader.upload_pending_files.assert_not_called()

    @pytest.mark.parametrize("nonce", [123, None, ["request-nonce"], {"n": 1}])
    def test_a_non_string_nonce_is_discarded(self, dispatcher, uploader, nonce):
        body = signed_response(nonce=None, commands=[{"id": 1, "command": "force-upload-now"}])
        body = {k: v for k, v in body.items() if k != "signature"} | {"nonce": nonce}
        body["signature"] = sign({k: v for k, v in body.items() if k != "signature"})

        dispatcher.handle_response(body, NONCE)

        uploader.upload_pending_files.assert_not_called()

    def test_a_nonce_added_after_signing_is_rejected_by_the_signature(self, dispatcher, uploader):
        body = signed_response(nonce=None, commands=[{"id": 1, "command": "force-upload-now"}])

        dispatcher.handle_response(body | {"nonce": NONCE}, NONCE)

        uploader.upload_pending_files.assert_not_called()

    def test_a_captured_response_replayed_against_a_later_request_is_discarded(
        self, dispatcher, tmp_path,
    ):
        captured = signed_response(nonce="nonce-of-request-1", config_overrides={"idle_threshold_s": 300})

        dispatcher.handle_response(captured, "nonce-of-request-2")

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {}

    def test_a_wrong_nonce_discards_overrides_too(self, dispatcher, tmp_path):
        body = signed_response(nonce="stale", config_overrides={"idle_threshold_s": 300})

        dispatcher.handle_response(body, NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {}

    def test_a_response_with_nothing_to_apply_needs_no_nonce(self, dispatcher):
        dispatcher.handle_response({"status": "ok"}, NONCE)


class TestHeartbeatSenderNonce:
    def _sender(self, tmp_path) -> HeartbeatSender:
        return HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )

    def test_every_request_carries_a_fresh_nonce(self, backend, tmp_path):
        sender = self._sender(tmp_path)
        run, storage = RunStatusTracker(RunStatusConfig()).status(), storage_status_for(tmp_path).status()

        sender.send_once(run, storage)
        sender.send_once(run, storage)

        first, second = (payload["nonce"] for payload in backend.payloads())
        assert first != second
        assert len(first) == len(second) == 32

    def test_the_dispatcher_is_given_the_nonce_of_the_request_it_answers(self, backend, tmp_path):
        sender = self._sender(tmp_path)
        remote_commands = MagicMock()
        sender.set_remote_commands(remote_commands)
        backend.response_body = json.dumps({"status": "ok"}).encode()
        run, storage = RunStatusTracker(RunStatusConfig()).status(), storage_status_for(tmp_path).status()

        sender.send_once(run, storage)
        sender.send_once(run, storage)

        nonces = [call.args[1] for call in remote_commands.handle_response.call_args_list]
        assert nonces == [payload["nonce"] for payload in backend.payloads()]

    def test_an_end_to_end_exchange_applies_overrides_only_when_the_backend_echoes_the_nonce(
        self, backend, tmp_path,
    ):
        sender = self._sender(tmp_path)
        overrides_path = tmp_path / "config_overrides.toml"
        sender.set_remote_commands(RemoteCommandDispatcher(
            hmac_secret=SECRET, overrides_path=overrides_path,
            uploader=MagicMock(spec=Uploader), collector=MagicMock(spec=Collector),
        ))
        run, storage = RunStatusTracker(RunStatusConfig()).status(), storage_status_for(tmp_path).status()

        backend.response_body = json.dumps(
            signed_response(nonce="replayed", config_overrides={"idle_threshold_s": 300})
        ).encode()
        sender.send_once(run, storage)
        assert read_overrides_file(overrides_path) == {}

        backend.respond_with = lambda nonce: json.dumps(
            signed_response(nonce=nonce, config_overrides={"idle_threshold_s": 300})
        ).encode()
        sender.send_once(run, storage)
        assert read_overrides_file(overrides_path) == {"idle_threshold_s": 300}

    def test_concurrent_sends_do_not_share_a_nonce(self, backend, tmp_path):
        sender = self._sender(tmp_path)
        run, storage = RunStatusTracker(RunStatusConfig()).status(), storage_status_for(tmp_path).status()
        threads = [threading.Thread(target=sender.send_once, args=(run, storage)) for _ in range(8)]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        nonces = [payload["nonce"] for payload in backend.payloads()]
        assert len(nonces) == 8
        assert len(set(nonces)) == 8


class TestConfigOverrides:
    def test_writes_allow_listed_overrides_to_the_overrides_file(self, dispatcher, tmp_path):
        body = signed_response(config_overrides={"heartbeat_interval_s": 30.0, "idle_threshold_s": 300})

        dispatcher.handle_response(body, NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {
            "heartbeat_interval_s": 30.0, "idle_threshold_s": 300,
        }

    def test_writes_typed_overrides_and_an_explicit_unset(self, dispatcher, tmp_path):
        body = signed_response(config_overrides={
            "collector_gps_enabled": False, "collector_capture_gate": "time",
            "movement_gate_home_latitude": None, "gnss_receiver_retries": 4,
        })

        dispatcher.handle_response(body, NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {
            "collector_gps_enabled": False, "collector_capture_gate": "time",
            "movement_gate_home_latitude": None, "gnss_receiver_retries": 4,
        }

    def test_drops_a_wrongly_typed_override_but_keeps_the_valid_ones(self, dispatcher, tmp_path):
        body = signed_response(config_overrides={"collector_gps_enabled": 1, "idle_threshold_s": 300})

        dispatcher.handle_response(body, NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {"idle_threshold_s": 300}

    def test_drops_a_disallowed_field_without_crashing(self, dispatcher, tmp_path):
        body = signed_response(config_overrides={"upload_host": "evil.example.org"})

        dispatcher.handle_response(body, NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {}

    def test_a_write_failure_is_logged_distinctly_and_does_not_propagate(
        self, dispatcher, tmp_path, monkeypatch, caplog,
    ):
        overrides_path = tmp_path / "config_overrides.toml"

        def raise_permission_error(path, changes):
            raise PermissionError("Permission denied")

        monkeypatch.setattr(
            "measurement_software.remote.heartbeat.write_overrides_file", raise_permission_error,
        )
        body = signed_response(config_overrides={"idle_threshold_s": 300})

        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response(body, NONCE)

        assert str(overrides_path) in caplog.text
        assert "Permission denied" in caplog.text


class TestOneShotCommands:
    def test_dispatches_force_upload_now(self, dispatcher, uploader):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "force-upload-now"}]), NONCE)

        uploader.upload_pending_files.assert_called_once()

    def test_dispatches_shutdown_now(self, dispatcher, collector):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "shutdown-now"}]), NONCE)

        collector.request_shutdown.assert_called_once_with(1)

    def test_dispatches_reboot_now(self, dispatcher, collector):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "reboot-now"}]), NONCE)

        collector.request_reboot.assert_called_once_with(1)

    def test_dispatches_stop_measuring_now(self, dispatcher, collector, tmp_path):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "stop-measuring-now"}]), NONCE)

        collector.request_stop_measuring.assert_called_once()
        assert read_overrides_file(tmp_path / "config_overrides.toml") == {"system_mode": "waiting"}

    def test_dispatches_sim_reset_now(self, dispatcher, collector):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "sim-reset-now"}]), NONCE)

        collector.request_sim_reset.assert_called_once()

    def test_dispatches_mode_waiting_now(self, dispatcher, tmp_path):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "mode-waiting-now"}]), NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {"system_mode": "waiting"}

    def test_mode_commands_keep_other_overrides_and_write_no_legacy_file(self, dispatcher, tmp_path):
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 300})

        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "mode-waiting-now"}]), NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {
            "idle_threshold_s": 300, "system_mode": "waiting",
        }
        assert not (tmp_path / "mode_override.toml").exists()

    def test_dispatches_mode_auto_now(self, dispatcher, tmp_path):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "mode-auto-now"}]), NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {"system_mode": "auto"}

    def test_a_later_mode_command_overwrites_an_earlier_one(self, dispatcher, tmp_path):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "mode-waiting-now"}]), NONCE)
        dispatcher.handle_response(signed_response(commands=[{"id": 2, "command": "mode-auto-now"}]), NONCE)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {"system_mode": "auto"}

    def test_dispatches_start_measuring_now_to_the_waiting_loop(self, dispatcher, waiting_loop):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "start-measuring-now"}]), NONCE)

        waiting_loop.request_start_measuring.assert_called_once()

    def test_dispatches_run_selftest_now_to_the_waiting_loop(self, dispatcher, waiting_loop):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "run-selftest-now"}]), NONCE)

        waiting_loop.request_run_selftest.assert_called_once()

    def test_dispatches_run_gps_fix_test_now_to_the_runner(self, dispatcher, gps_fix_test_runner):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "run-gps-fix-test-now"}]), NONCE)

        gps_fix_test_runner.start.assert_called_once()

    def test_dispatches_stop_gps_fix_test_now_to_the_runner(self, dispatcher, gps_fix_test_runner):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "stop-gps-fix-test-now"}]), NONCE)

        gps_fix_test_runner.stop.assert_called_once()

    def test_ignores_run_gps_fix_test_now_when_not_in_waiting_mode(
        self, tmp_path, uploader, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            uploader=uploader,
            collector=collector,
            waiting_loop=None,
            gps_fix_test_runner=None,
        )

        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "run-gps-fix-test-now"}]), NONCE
        )

        assert dispatcher.pending_acks()[0]["status"] == "not_applicable"

    def test_ignores_stop_gps_fix_test_now_when_not_in_waiting_mode(
        self, tmp_path, uploader, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            uploader=uploader,
            collector=collector,
            waiting_loop=None,
            gps_fix_test_runner=None,
        )

        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "stop-gps-fix-test-now"}]), NONCE
        )

        assert dispatcher.pending_acks()[0]["status"] == "not_applicable"

    def test_ignores_run_selftest_now_when_not_in_waiting_mode(
        self, tmp_path, uploader, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            uploader=uploader,
            collector=collector,
            waiting_loop=None,
        )

        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "run-selftest-now"}]), NONCE
        )

        assert dispatcher.pending_acks()[0]["status"] == "not_applicable"

    def test_ignores_start_measuring_now_when_not_in_waiting_mode(
        self, tmp_path, uploader, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            uploader=uploader,
            collector=collector,
            waiting_loop=None,
        )

        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "start-measuring-now"}]), NONCE
        )

        assert dispatcher.pending_acks()[0]["status"] == "not_applicable"

    def test_dedupes_a_redelivered_command_id(self, dispatcher, uploader):
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])

        dispatcher.handle_response(body, NONCE)
        dispatcher.handle_response(body, NONCE)

        uploader.upload_pending_files.assert_called_once()

    def test_ignores_unknown_command(self, dispatcher, uploader, caplog):
        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "self-destruct-now"}]), NONCE)

        assert "unknown one-shot command" in caplog.text

    def test_ignores_malformed_command_entry(self, dispatcher, caplog):
        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(signed_response(commands=[{"command": "force-upload-now"}]), NONCE)

        assert "malformed" in caplog.text

    def test_one_bad_command_does_not_block_the_rest_of_the_batch(self, dispatcher, uploader, collector):
        body = signed_response(commands=[
            {"id": 1, "command": "reboot-now"},
            {"id": 2, "command": "force-upload-now"},
        ])

        dispatcher.handle_response(body, NONCE)

        uploader.upload_pending_files.assert_called_once()

    def test_a_failing_handler_is_caught_and_logged_not_raised(self, dispatcher, uploader, caplog):
        uploader.upload_pending_files.side_effect = RuntimeError("boom")
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])

        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response(body, NONCE)  # must not raise

        assert "failed" in caplog.text

    def test_dispatches_promote_overrides_now(self, dispatcher, tmp_path):
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 500})

        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "promote-overrides-now"}]), NONCE
        )

        assert "max_idle_time = 500" in (tmp_path / "config.toml").read_text()
        assert read_overrides_file(tmp_path / "config_overrides.toml") == {}

    def test_promote_overrides_now_requires_signed_dispatch_like_every_other_command(
        self, dispatcher, tmp_path,
    ):
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 500})

        dispatcher.handle_response(
            {"commands": [{"id": 1, "command": "promote-overrides-now"}], "signature": "hmac-sha256:deadbeef"}, NONCE
        )

        assert "max_idle_time = 200" in (tmp_path / "config.toml").read_text()
        assert read_overrides_file(tmp_path / "config_overrides.toml") == {"idle_threshold_s": 500}

    def test_promote_overrides_now_is_a_no_op_when_nothing_is_set(self, dispatcher, tmp_path):
        original_text = (tmp_path / "config.toml").read_text()

        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "promote-overrides-now"}]), NONCE
        )

        assert (tmp_path / "config.toml").read_text() == original_text

    def test_ignores_promote_overrides_now_without_a_configured_config_path(
        self, tmp_path, uploader, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            uploader=uploader,
            collector=collector,
        )
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 500})

        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "promote-overrides-now"}]), NONCE
        )

        assert dispatcher.pending_acks()[0]["status"] == "not_applicable"
        assert read_overrides_file(tmp_path / "config_overrides.toml") == {"idle_threshold_s": 500}

    def test_a_missing_config_toml_is_caught_and_logged_not_raised(
        self, tmp_path, uploader, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            uploader=uploader,
            collector=collector,
            config_path=tmp_path / "does_not_exist.toml",
        )
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 500})

        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response(
                signed_response(commands=[{"id": 1, "command": "promote-overrides-now"}]), NONCE
            )  # must not raise

        assert "failed" in caplog.text


class TestCommandAcks:
    def test_successful_command_queues_a_success_ack(self, dispatcher, uploader):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "force-upload-now"}]), NONCE)

        assert dispatcher.pending_acks() == [
            {"id": 1, "command": "force-upload-now", "status": "success", "error": None},
        ]

    def test_force_upload_now_with_nothing_pending_queues_a_success_ack_with_a_detail(
        self, dispatcher, uploader,
    ):
        uploader.upload_pending_files.return_value = 0

        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "force-upload-now"}]), NONCE)

        assert dispatcher.pending_acks() == [
            {"id": 1, "command": "force-upload-now", "status": "success", "error": "Nothing to upload"},
        ]

    def test_failed_command_queues_a_failed_ack_with_the_error(self, dispatcher, uploader):
        uploader.upload_pending_files.side_effect = RuntimeError("boom")

        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "force-upload-now"}]), NONCE)

        assert dispatcher.pending_acks() == [
            {"id": 1, "command": "force-upload-now", "status": "failed", "error": "boom"},
        ]

    def test_clear_acks_empties_the_queue(self, dispatcher, uploader):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "force-upload-now"}]), NONCE)

        dispatcher.clear_acks()

        assert dispatcher.pending_acks() == []

    def test_redelivered_command_requeues_its_remembered_ack(self, dispatcher, uploader):
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])
        dispatcher.handle_response(body, NONCE)
        dispatcher.clear_acks()

        dispatcher.handle_response(body, NONCE)  # redelivered - already executed, not re-run

        uploader.upload_pending_files.assert_called_once()
        assert dispatcher.pending_acks() == [
            {"id": 1, "command": "force-upload-now", "status": "success", "error": None},
        ]

    def test_shutdown_now_queues_no_generic_success_ack(self, dispatcher, collector):
        """shutdown-now's only ack is the "received" the unified shutdown sequence's terminal
        heartbeat sends on its way out (core/system.py) - a generic "success" here, before the
        device has even shut down, would race and could reach the backend first."""
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "shutdown-now"}]), NONCE)

        collector.request_shutdown.assert_called_once_with(1)
        assert dispatcher.pending_acks() == []

    def test_reboot_now_queues_no_generic_success_ack(self, dispatcher, collector):
        """reboot-now's "success" only comes from seed_reboot_success_ack() once the next boot
        proves the reboot actually happened - not from here, before it's even been attempted."""
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "reboot-now"}]), NONCE)

        collector.request_reboot.assert_called_once_with(1)
        assert dispatcher.pending_acks() == []

    def test_unknown_command_produces_no_ack(self, dispatcher, caplog):
        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "self-destruct-now"}]), NONCE)

        assert dispatcher.pending_acks() == []

    def test_seed_reboot_success_ack_queues_a_success_ack_for_reboot_now(self, dispatcher):
        dispatcher.seed_reboot_success_ack(52)

        assert dispatcher.pending_acks() == [
            {"id": 52, "command": "reboot-now", "status": "success", "error": None},
        ]

    def test_seed_reboot_success_ack_marks_the_id_executed_so_a_redelivery_does_not_reboot_again(
        self, dispatcher, collector,
    ):
        dispatcher.seed_reboot_success_ack(52)
        dispatcher.clear_acks()

        # The backend may not have cleared its queue yet by the time this (new, post-reboot)
        # process first sees its own reboot-now command id redelivered.
        dispatcher.handle_response(signed_response(commands=[{"id": 52, "command": "reboot-now"}]), NONCE)

        collector.request_reboot.assert_not_called()
        assert dispatcher.pending_acks() == [
            {"id": 52, "command": "reboot-now", "status": "success", "error": None},
        ]
