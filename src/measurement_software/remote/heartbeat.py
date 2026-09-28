from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import urllib.request
from http.client import HTTPException
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from measurement_software.core.config import BackendConfig, HeartbeatConfig
from measurement_software.core.config_sources import sanitize_overrides, write_mode_override, write_overrides_file
from measurement_software.core.run_phase import RunPhase, WaitingLoop
from measurement_software.core.system import FanController
from measurement_software.core.uploader import Uploader
from measurement_software.core.util import build_backend_url
from measurement_software.remote.gps_fix_test import GpsFixTestRunner
from measurement_software.status.backend_status import (
    ConfigStatusReporter,
    GpsFixStatusTracker,
    RunStatus,
    RunStatusTracker,
    StorageStatus,
    StorageStatusReporter,
)

if TYPE_CHECKING:
    from measurement_software.core.collector import Collector

SIGNATURE_PREFIX = "hmac-sha256:"


class HeartbeatSender:
    """Posts the run's status to the backend on a background thread while a run is going on.

    Reports liveness and whether the run is worth letting finish, so a broken run can be
    noticed while the vehicle is still out rather than after the data is uploaded. Also carries
    the upload backlog's size and free disk space, for the same reason: free visibility, no
    on-device policy attached (see decision record 0009).
    """

    def __init__(self, config: HeartbeatConfig, backend: BackendConfig, run_status: RunStatusTracker,
                 storage_status: StorageStatusReporter, config_status: ConfigStatusReporter | None = None,
                 run_phase: RunPhase | None = None, gps_fix_status: GpsFixStatusTracker | None = None):
        self._logger = logging.getLogger(__name__)
        self._config = config
        self._backend = backend
        self._url = build_backend_url(backend.url, config.path)
        self._run_status = run_status
        self._storage_status = storage_status
        self._config_status = config_status
        self._run_phase = run_phase
        self._gps_fix_status = gps_fix_status
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._remote_commands = None

    def set_remote_commands(self, remote_commands) -> None:
        """Wires in the collaborator that verifies/applies commands carried on each response.

        Set post-construction from main.py, since the dispatcher itself needs the Collector
        instance this HeartbeatSender is passed into - constructing both up front would be
        circular. Left unset (the default) by anything that only wants to POST heartbeats and
        never process remote commands, e.g. the selftest backend check.
        """
        self._remote_commands = remote_commands

    def start(self) -> None:
        """Starts reporting, unless the heartbeat is disabled or misconfigured."""
        if not self.is_usable():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the reporting thread to stop and waits for it to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def is_usable(self) -> bool:
        """Reports whether this run should send heartbeats, saying loudly why if it shouldn't."""
        if not self._config.enabled:
            self._logger.info("Backend heartbeat is disabled.")
            return False
        if not self._backend.url.startswith("https://"):
            self._logger.error(
                "Backend heartbeat needs an https:// url but is configured with %r - "
                "staying silent for this run.", self._backend.url,
            )
            return False
        return True

    def _run(self, stop_event: threading.Event) -> None:
        """Reports the run's status immediately, then once per configured interval."""
        while True:
            self.send_once(self._run_status.status(), self._storage_status.status())
            if stop_event.wait(self._config.interval_s):
                return

    def send_once(self, run_status: RunStatus, storage_status: StorageStatus) -> bool:
        """Posts one status report, returning whether it was accepted.

        A failed send is treated as a skipped tick rather than an error by the recurring
        background loop: a backend that is unreachable — a dead spot, a server outage — must
        never take a measurement run down with it. The return value exists for callers (e.g.
        the selftest heartbeat check) that need to know whether this one send succeeded.
        """
        payload = run_status.as_payload()
        payload["storage"] = storage_status.as_payload()
        if self._config_status is not None:
            payload["current_config"] = self._config_status.as_payload()
        if self._run_phase is not None:
            payload["run_phase"] = self._run_phase.get()
        if self._gps_fix_status is not None:
            payload["gps_fix"] = self._gps_fix_status.status().as_payload()
        if self._config.device_id:
            payload["device_id"] = self._config.device_id

        headers = {"Content-Type": "application/json"}
        if self._config.device_id:
            headers["X-Device-ID"] = self._config.device_id
        if self._backend.device_key:
            headers["X-Device-Key"] = self._backend.device_key

        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_s) as response:
                self._logger.debug("Heartbeat accepted (HTTP %s)", response.status)
                if self._remote_commands is not None:
                    self._process_remote_response(response)
                return True
        except (OSError, HTTPException) as e:
            self._logger.warning("Heartbeat could not be sent: %s", e)
            return False

    def _process_remote_response(self, response) -> None:
        """Parses the response body and hands it to the remote command dispatcher.

        A response that isn't valid JSON is logged and ignored rather than raised - a malformed
        body must never take the heartbeat loop (or the run) down with it.
        """
        try:
            body = json.loads(response.read().decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._logger.warning("Could not parse heartbeat response body: %s", e)
            return
        self._remote_commands.handle_response(body)


class RemoteCommandDispatcher:
    """Verifies and applies the config overrides / one-shot commands a heartbeat response carries.

    Runs on HeartbeatSender's background thread. Config overrides are only ever written to disk
    here - they take effect from the next `load_config()` call (the next boot), never mid-run, so
    no in-memory config is touched.
    One-shot commands are dispatched immediately via the factory-dict below; `shutdown-now` and
    `sim-reset-now` hand off to `Collector`, which applies them on its own thread rather than
    this one, since the modem's serial connection isn't thread-safe. `mode-waiting-now`/
    `mode-auto-now` persist a next-boot device mode the same way config overrides do, but to
    their own file (mode isn't part of the backend's numeric config_overrides schema).
    `start-measuring-now` and `run-selftest-now` hand off to `WaitingLoop` the same way
    `shutdown-now`/`sim-reset-now` hand off to `Collector` - both only meaningful while the
    device is in Waiting Mode. `run-gps-fix-test-now`/`stop-gps-fix-test-now` hand off to their
    own `GpsFixTestRunner` collaborator instead, since that run keeps going on its own background
    thread for up to `gps_fix_test.timeout_s` rather than being handled synchronously like
    `run-selftest-now` - also only meaningful in Waiting Mode.
    """

    def __init__(
        self, hmac_secret: str, overrides_path: Path, mode_override_path: Path,
        uploader: Uploader, fan_controller: FanController, collector: Collector,
        waiting_loop: WaitingLoop | None = None, gps_fix_test_runner: GpsFixTestRunner | None = None,
    ):
        self._logger = logging.getLogger(__name__)
        self._hmac_secret = hmac_secret
        self._overrides_path = overrides_path
        self._mode_override_path = mode_override_path
        self._uploader = uploader
        self._fan_controller = fan_controller
        self._collector = collector
        self._waiting_loop = waiting_loop
        self._gps_fix_test_runner = gps_fix_test_runner
        self._executed_command_ids: set = set()

    def handle_response(self, body: dict) -> None:
        """Verifies and applies whatever a heartbeat response carries, discarding it on any doubt.

        A response with nothing queued has no `config_overrides`/`commands` keys at all, per the
        backend contract - nothing to verify or apply in that case.
        """
        config_overrides = body.get("config_overrides")
        commands = body.get("commands")
        if not config_overrides and not commands:
            return

        if not self._verify_signature(body):
            self._logger.error("Heartbeat response signature invalid or missing - discarding commands.")
            return

        if config_overrides:
            self._apply_config_overrides(config_overrides)
        if commands:
            self._dispatch_commands(commands)

    def _verify_signature(self, body: dict) -> bool:
        if not self._hmac_secret:
            self._logger.error("heartbeat.hmac_secret is not configured - remote commands disabled.")
            return False

        signature = body.get("signature")
        if not isinstance(signature, str):
            return False

        unsigned = {k: v for k, v in body.items() if k != "signature"}
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), default=str)
        digest = hmac.new(self._hmac_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        expected = f"{SIGNATURE_PREFIX}{digest}"
        return hmac.compare_digest(expected, signature)

    def _apply_config_overrides(self, raw: dict) -> None:
        if not isinstance(raw, dict):
            self._logger.warning("Ignoring malformed config_overrides payload: %r", raw)
            return
        sanitized = sanitize_overrides(raw)
        if not sanitized:
            return
        try:
            write_overrides_file(self._overrides_path, sanitized)
        except OSError as e:
            self._logger.error(
                "Could not write config overrides to %s: %s: %s",
                self._overrides_path, type(e).__name__, e,
            )
            return
        self._logger.info(
            "Applied config overrides (take effect next boot): %s", sorted(sanitized)
        )

    def _dispatch_commands(self, commands: list) -> None:
        if not isinstance(commands, list):
            self._logger.warning("Ignoring malformed commands payload: %r", commands)
            return

        for entry in commands:
            self._dispatch_one(entry)

    def _dispatch_one(self, entry: object) -> None:
        if not isinstance(entry, dict) or "id" not in entry or "command" not in entry:
            self._logger.warning("Ignoring malformed one-shot command entry: %r", entry)
            return

        command_id = entry["id"]
        if command_id in self._executed_command_ids:
            self._logger.debug("Skipping already-executed command id=%r (redelivered).", command_id)
            return

        handler = _ONE_SHOT_HANDLERS.get(entry["command"])
        if handler is None:
            self._logger.warning("Ignoring unknown one-shot command %r.", entry["command"])
            return

        try:
            handler(self, entry)
        except Exception:
            self._logger.exception(
                "One-shot command %r (id=%r) failed.", entry["command"], command_id
            )
            return

        self._executed_command_ids.add(command_id)
        self._logger.info("Executed one-shot command %r (id=%r).", entry["command"], command_id)

    def _handle_force_upload(self, entry: dict) -> None:
        self._uploader.upload_pending_files()

    def _handle_shutdown(self, entry: dict) -> None:
        self._collector.request_shutdown()

    def _handle_sim_reset(self, entry: dict) -> None:
        self._collector.request_sim_reset()

    def _handle_fan_override(self, entry: dict) -> None:
        speed = entry.get("speed")
        if not isinstance(speed, int) or isinstance(speed, bool) or not (0 <= speed <= 100):
            self._logger.warning("Ignoring fan-override-now with invalid speed %r.", speed)
            return
        # This hardware only supports on/off fan control (no PWM), so any nonzero requested
        # speed simply forces the fan fully on.
        self._fan_controller.override(speed > 0)

    def _handle_fan_auto(self, entry: dict) -> None:
        self._fan_controller.release_override()

    def _handle_mode_waiting(self, entry: dict) -> None:
        write_mode_override(self._mode_override_path, "waiting")
        self._logger.info("Persisted device mode 'waiting' (takes effect next boot).")

    def _handle_mode_auto(self, entry: dict) -> None:
        write_mode_override(self._mode_override_path, "auto")
        self._logger.info("Persisted device mode 'auto' (takes effect next boot).")

    def _handle_start_measuring(self, entry: dict) -> None:
        if self._waiting_loop is None:
            self._logger.warning("Ignoring start-measuring-now - device is not in Waiting Mode.")
            return
        self._waiting_loop.request_start_measuring()

    def _handle_run_selftest(self, entry: dict) -> None:
        if self._waiting_loop is None:
            self._logger.warning("Ignoring run-selftest-now - device is not in Waiting Mode.")
            return
        self._waiting_loop.request_run_selftest()

    def _handle_run_gps_fix_test(self, entry: dict) -> None:
        if self._gps_fix_test_runner is None:
            self._logger.warning("Ignoring run-gps-fix-test-now - device is not in Waiting Mode.")
            return
        self._gps_fix_test_runner.start()

    def _handle_stop_gps_fix_test(self, entry: dict) -> None:
        if self._gps_fix_test_runner is None:
            self._logger.warning("Ignoring stop-gps-fix-test-now - device is not in Waiting Mode.")
            return
        self._gps_fix_test_runner.stop()


_ONE_SHOT_HANDLERS: dict[str, Callable[["RemoteCommandDispatcher", dict], None]] = {
    "force-upload-now": RemoteCommandDispatcher._handle_force_upload,
    "shutdown-now": RemoteCommandDispatcher._handle_shutdown,
    "sim-reset-now": RemoteCommandDispatcher._handle_sim_reset,
    "fan-override-now": RemoteCommandDispatcher._handle_fan_override,
    "fan-auto-now": RemoteCommandDispatcher._handle_fan_auto,
    "mode-waiting-now": RemoteCommandDispatcher._handle_mode_waiting,
    "mode-auto-now": RemoteCommandDispatcher._handle_mode_auto,
    "start-measuring-now": RemoteCommandDispatcher._handle_start_measuring,
    "run-selftest-now": RemoteCommandDispatcher._handle_run_selftest,
    "run-gps-fix-test-now": RemoteCommandDispatcher._handle_run_gps_fix_test,
    "stop-gps-fix-test-now": RemoteCommandDispatcher._handle_stop_gps_fix_test,
}
