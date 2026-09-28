import hashlib
import hmac
import json
import logging
import threading
import time
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
from measurement_software.core.config_sources import read_mode_override, read_overrides_file
from measurement_software.core.run_phase import RunPhase, RunPhaseState, WaitingLoop
from measurement_software.core.system import FanController
from measurement_software.core.uploader import Uploader
from measurement_software.modems.modem import CellSample
from measurement_software.remote.gps_fix_test import GpsFixTestRunner
from measurement_software.remote.heartbeat import HeartbeatSender, RemoteCommandDispatcher
from measurement_software.status.backend_status import GpsFixStatusTracker, RunStatusTracker, StorageStatusReporter

SECRET = "test-hmac-secret"


def sign(body: dict, secret: str = SECRET) -> str:
    """Mirrors the backend's own signing algorithm (app/response_signing.py)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    digest = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def signed_response(**fields) -> dict:
    signature = sign(fields)
    return {**fields, "signature": signature}


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
    response, for tests exercising the remote-command response-handling path.
    """

    def __init__(self, fail_first: int = 0, response_body: bytes = b""):
        self.fail_first = fail_first
        self.response_body = response_body
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
        assert backend.payloads()[0] == {
            "since_run_start": {"datapoints_total": 1, "good": 1, "bad": 0, "invalid": 0},
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
        config_status = FakeConfigStatus({"rsrp_threshold": -100.0, "fan_mode": "auto"})
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            tracker, storage_status_for(tmp_path), config_status,
        )

        sender.start()
        wait_for_requests(backend, 1)
        sender.stop()

        assert backend.payloads()[0]["current_config"] == {"rsrp_threshold": -100.0, "fan_mode": "auto"}

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
            "datapoints_total": 1, "good": 0, "bad": 1, "invalid": 0,
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
            "has_fix": False, "num_satellites": None, "fix_age_s": None,
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
            {"status": "ok", "signature": "hmac-sha256:abc"}
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
        signature = sign({"status": "ok", "config_overrides": {"rsrp_threshold": -100.0}}, SECRET)
        backend.response_body = json.dumps(
            {"status": "ok", "config_overrides": {"rsrp_threshold": -100.0}, "signature": signature}
        ).encode()
        sender = HeartbeatSender(
            HeartbeatConfig(enabled=True, interval_s=3600), make_backend_config(),
            RunStatusTracker(RunStatusConfig()), storage_status_for(tmp_path),
        )
        overrides_path = tmp_path / "missing_dir" / "config_overrides.toml"
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=overrides_path,
            mode_override_path=tmp_path / "mode_override.toml",
            uploader=MagicMock(spec=Uploader),
            fan_controller=MagicMock(spec=FanController),
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


@pytest.fixture
def uploader() -> MagicMock:
    return MagicMock(spec=Uploader)


@pytest.fixture
def fan_controller() -> MagicMock:
    return MagicMock(spec=FanController)


@pytest.fixture
def collector() -> MagicMock:
    return MagicMock(spec=Collector)


@pytest.fixture
def waiting_loop() -> MagicMock:
    return MagicMock(spec=WaitingLoop)


@pytest.fixture
def gps_fix_test_runner() -> MagicMock:
    return MagicMock(spec=GpsFixTestRunner)


@pytest.fixture
def dispatcher(
    tmp_path, uploader, fan_controller, collector, waiting_loop, gps_fix_test_runner,
) -> RemoteCommandDispatcher:
    return RemoteCommandDispatcher(
        hmac_secret=SECRET,
        overrides_path=tmp_path / "config_overrides.toml",
        mode_override_path=tmp_path / "mode_override.toml",
        uploader=uploader,
        fan_controller=fan_controller,
        collector=collector,
        waiting_loop=waiting_loop,
        gps_fix_test_runner=gps_fix_test_runner,
    )


class TestSignatureVerification:
    def test_ignores_a_response_with_nothing_queued(self, dispatcher, uploader):
        dispatcher.handle_response({"status": "ok", "signature": "bogus"})

        uploader.upload_pending_files.assert_not_called()

    def test_discards_commands_when_signature_is_missing(self, dispatcher, uploader, caplog):
        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response({"commands": [{"id": 1, "command": "force-upload-now"}]})

        uploader.upload_pending_files.assert_not_called()
        assert "signature invalid" in caplog.text

    def test_discards_commands_when_signature_is_wrong(self, dispatcher, uploader, caplog):
        body = {"commands": [{"id": 1, "command": "force-upload-now"}], "signature": "hmac-sha256:deadbeef"}

        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response(body)

        uploader.upload_pending_files.assert_not_called()

    def test_discards_commands_when_secret_is_not_configured(self, tmp_path, uploader, fan_controller, collector):
        unconfigured = RemoteCommandDispatcher(
            hmac_secret="", overrides_path=tmp_path / "config_overrides.toml",
            mode_override_path=tmp_path / "mode_override.toml",
            uploader=uploader, fan_controller=fan_controller, collector=collector,
        )
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])

        unconfigured.handle_response(body)

        uploader.upload_pending_files.assert_not_called()

    def test_applies_commands_with_a_valid_signature(self, dispatcher, uploader):
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])

        dispatcher.handle_response(body)

        uploader.upload_pending_files.assert_called_once()


class TestConfigOverrides:
    def test_writes_allow_listed_overrides_to_the_overrides_file(self, dispatcher, tmp_path):
        body = signed_response(config_overrides={"rsrp_threshold": -100.0, "idle_threshold_s": 300})

        dispatcher.handle_response(body)

        assert read_overrides_file(tmp_path / "config_overrides.toml") == {
            "rsrp_threshold": -100.0, "idle_threshold_s": 300,
        }

    def test_drops_a_disallowed_field_without_crashing(self, dispatcher, tmp_path):
        body = signed_response(config_overrides={"upload_host": "evil.example.org"})

        dispatcher.handle_response(body)

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
        body = signed_response(config_overrides={"rsrp_threshold": -100.0})

        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response(body)

        assert str(overrides_path) in caplog.text
        assert "Permission denied" in caplog.text


class TestOneShotCommands:
    def test_dispatches_force_upload_now(self, dispatcher, uploader):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "force-upload-now"}]))

        uploader.upload_pending_files.assert_called_once()

    def test_dispatches_shutdown_now(self, dispatcher, collector):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "shutdown-now"}]))

        collector.request_shutdown.assert_called_once()

    def test_dispatches_sim_reset_now(self, dispatcher, collector):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "sim-reset-now"}]))

        collector.request_sim_reset.assert_called_once()

    def test_dispatches_fan_override_now_on_with_nonzero_speed(self, dispatcher, fan_controller):
        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "fan-override-now", "speed": 75}])
        )

        fan_controller.override.assert_called_once_with(True)

    def test_dispatches_fan_override_now_off_with_zero_speed(self, dispatcher, fan_controller):
        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "fan-override-now", "speed": 0}])
        )

        fan_controller.override.assert_called_once_with(False)

    def test_ignores_fan_override_now_missing_speed(self, dispatcher, fan_controller, caplog):
        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "fan-override-now"}]))

        fan_controller.override.assert_not_called()

    def test_ignores_fan_override_now_out_of_range_speed(self, dispatcher, fan_controller):
        dispatcher.handle_response(
            signed_response(commands=[{"id": 1, "command": "fan-override-now", "speed": 150}])
        )

        fan_controller.override.assert_not_called()

    def test_dispatches_fan_auto_now(self, dispatcher, fan_controller):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "fan-auto-now"}]))

        fan_controller.release_override.assert_called_once()

    def test_dispatches_mode_waiting_now(self, dispatcher, tmp_path):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "mode-waiting-now"}]))

        assert read_mode_override(tmp_path / "mode_override.toml") == "waiting"

    def test_dispatches_mode_auto_now(self, dispatcher, tmp_path):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "mode-auto-now"}]))

        assert read_mode_override(tmp_path / "mode_override.toml") == "auto"

    def test_a_later_mode_command_overwrites_an_earlier_one(self, dispatcher, tmp_path):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "mode-waiting-now"}]))
        dispatcher.handle_response(signed_response(commands=[{"id": 2, "command": "mode-auto-now"}]))

        assert read_mode_override(tmp_path / "mode_override.toml") == "auto"

    def test_dispatches_start_measuring_now_to_the_waiting_loop(self, dispatcher, waiting_loop):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "start-measuring-now"}]))

        waiting_loop.request_start_measuring.assert_called_once()

    def test_dispatches_run_selftest_now_to_the_waiting_loop(self, dispatcher, waiting_loop):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "run-selftest-now"}]))

        waiting_loop.request_run_selftest.assert_called_once()

    def test_dispatches_run_gps_fix_test_now_to_the_runner(self, dispatcher, gps_fix_test_runner):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "run-gps-fix-test-now"}]))

        gps_fix_test_runner.start.assert_called_once()

    def test_dispatches_stop_gps_fix_test_now_to_the_runner(self, dispatcher, gps_fix_test_runner):
        dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "stop-gps-fix-test-now"}]))

        gps_fix_test_runner.stop.assert_called_once()

    def test_ignores_run_gps_fix_test_now_when_not_in_waiting_mode(
        self, tmp_path, uploader, fan_controller, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            mode_override_path=tmp_path / "mode_override.toml",
            uploader=uploader,
            fan_controller=fan_controller,
            collector=collector,
            waiting_loop=None,
            gps_fix_test_runner=None,
        )

        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(
                signed_response(commands=[{"id": 1, "command": "run-gps-fix-test-now"}])
            )

        assert "not in Waiting Mode" in caplog.text

    def test_ignores_stop_gps_fix_test_now_when_not_in_waiting_mode(
        self, tmp_path, uploader, fan_controller, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            mode_override_path=tmp_path / "mode_override.toml",
            uploader=uploader,
            fan_controller=fan_controller,
            collector=collector,
            waiting_loop=None,
            gps_fix_test_runner=None,
        )

        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(
                signed_response(commands=[{"id": 1, "command": "stop-gps-fix-test-now"}])
            )

        assert "not in Waiting Mode" in caplog.text

    def test_ignores_run_selftest_now_when_not_in_waiting_mode(
        self, tmp_path, uploader, fan_controller, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            mode_override_path=tmp_path / "mode_override.toml",
            uploader=uploader,
            fan_controller=fan_controller,
            collector=collector,
            waiting_loop=None,
        )

        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(
                signed_response(commands=[{"id": 1, "command": "run-selftest-now"}])
            )

        assert "not in Waiting Mode" in caplog.text

    def test_ignores_start_measuring_now_when_not_in_waiting_mode(
        self, tmp_path, uploader, fan_controller, collector, caplog,
    ):
        dispatcher = RemoteCommandDispatcher(
            hmac_secret=SECRET,
            overrides_path=tmp_path / "config_overrides.toml",
            mode_override_path=tmp_path / "mode_override.toml",
            uploader=uploader,
            fan_controller=fan_controller,
            collector=collector,
            waiting_loop=None,
        )

        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(
                signed_response(commands=[{"id": 1, "command": "start-measuring-now"}])
            )

        assert "not in Waiting Mode" in caplog.text

    def test_dedupes_a_redelivered_command_id(self, dispatcher, uploader):
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])

        dispatcher.handle_response(body)
        dispatcher.handle_response(body)

        uploader.upload_pending_files.assert_called_once()

    def test_ignores_unknown_command(self, dispatcher, uploader, caplog):
        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(signed_response(commands=[{"id": 1, "command": "reboot-now"}]))

        assert "unknown one-shot command" in caplog.text

    def test_ignores_malformed_command_entry(self, dispatcher, caplog):
        with caplog.at_level(logging.WARNING):
            dispatcher.handle_response(signed_response(commands=[{"command": "force-upload-now"}]))

        assert "malformed" in caplog.text

    def test_one_bad_command_does_not_block_the_rest_of_the_batch(self, dispatcher, uploader, collector):
        body = signed_response(commands=[
            {"id": 1, "command": "reboot-now"},
            {"id": 2, "command": "force-upload-now"},
        ])

        dispatcher.handle_response(body)

        uploader.upload_pending_files.assert_called_once()

    def test_a_failing_handler_is_caught_and_logged_not_raised(self, dispatcher, uploader, caplog):
        uploader.upload_pending_files.side_effect = RuntimeError("boom")
        body = signed_response(commands=[{"id": 1, "command": "force-upload-now"}])

        with caplog.at_level(logging.ERROR):
            dispatcher.handle_response(body)  # must not raise

        assert "failed" in caplog.text
