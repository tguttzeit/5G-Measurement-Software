from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.request
from http.client import HTTPException
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from measurement_software.core.config import BackendConfig, HeartbeatConfig
from measurement_software.core.config_sources import (
    promote_overrides,
    sanitize_overrides,
    write_overrides_file,
)
from measurement_software.core.run_phase import RunPhase, WaitingLoop
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


class CommandNotApplicable(Exception):
    """Raised by a one-shot command handler when its effect genuinely does not apply right now
    (e.g. stop-measuring-now with no run active, start-measuring-now outside Waiting Mode) -
    distinct from a failure. Logged quietly at info level, acked as "not_applicable" rather than
    "success" or "failed", and never marked executed, so a later redelivery (device state may
    have changed by then) can simply try again."""


class HeartbeatSender:
    """Posts the run's status to the backend on a background thread while a run is going on.

    Reports liveness and whether the run is worth letting finish, so a broken run can be
    noticed while the vehicle is still out rather than after the data is uploaded. Also carries
    the upload backlog's size and free disk space, for the same reason: free visibility, no
    on-device policy attached.
    """

    def __init__(self, config: HeartbeatConfig, backend: BackendConfig, run_status: RunStatusTracker,
                 storage_status: StorageStatusReporter, config_status: ConfigStatusReporter | None = None,
                 run_phase: RunPhase | None = None, gps_fix_status: GpsFixStatusTracker | None = None,
                 critical_error: dict | None = None):
        self._logger = logging.getLogger(__name__)
        self._config = config
        self._backend = backend
        self._url = build_backend_url(backend.url, config.path)
        self._run_status = run_status
        self._storage_status = storage_status
        self._config_status = config_status
        self._run_phase = run_phase
        self._gps_fix_status = gps_fix_status
        self._critical_error = critical_error
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._remote_commands = None
        self._last_delivery_log_at: float | None = None
        self._delivered_since_log = 0
        self._failed_since_delivery = 0

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

    def send_pending_acks_now(self) -> None:
        """Sends one heartbeat immediately, only if a wired dispatcher has an ack queued.

        For a caller that's about to stop() this sender and can't wait for the next scheduled
        tick to flush a just-recorded ack - e.g. Collector.collect() ending a run via
        stop-measuring-now, whose success ack would otherwise sit queued on a background
        thread that's about to be stopped (possibly followed by the process exiting entirely,
        confirmed live during the issue #50 field test). A no-op when there's nothing pending,
        so it's safe to call unconditionally.
        """
        if self._remote_commands is not None and self._remote_commands.pending_acks():
            self.send_once(self._run_status.status(), self._storage_status.status())

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

    def send_once(
        self, run_status: RunStatus, storage_status: StorageStatus,
        extra_executed_commands: list[dict] | None = None,
    ) -> bool:
        """Posts one status report, returning whether it was accepted.

        A failed send is treated as a skipped tick rather than an error by the recurring
        background loop: a backend that is unreachable — a dead spot, a server outage — must
        never take a measurement run down with it. The return value exists for callers (e.g.
        the selftest heartbeat check) that need to know whether this one send succeeded.

        `extra_executed_commands` lets a caller with no RemoteCommandDispatcher of its own (the
        unified shutdown/reboot sequence, which sends one last heartbeat after the dispatcher
        that originally received the command is already gone) still ack a specific command id.
        Combined with whatever the dispatcher itself has queued, if one is wired.
        """
        nonce = secrets.token_hex(16)
        payload = run_status.as_payload()
        payload["nonce"] = nonce
        payload["storage"] = storage_status.as_payload()
        if self._config_status is not None:
            payload["current_config"] = self._config_status.as_payload()
        if self._run_phase is not None:
            payload["run_phase"] = self._run_phase.get()
        if self._gps_fix_status is not None:
            payload["gps_fix"] = self._gps_fix_status.status().as_payload()
        if self._critical_error is not None:
            payload["critical_error"] = self._critical_error
        acks = list(extra_executed_commands or [])
        if self._remote_commands is not None:
            acks.extend(self._remote_commands.pending_acks())
        if acks:
            payload["executed_commands"] = acks
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
                self._log_delivery(response.status)
                if self._remote_commands is not None:
                    self._remote_commands.clear_acks()
                    self._process_remote_response(response, nonce)
                return True
        except (OSError, HTTPException) as e:
            self._failed_since_delivery += 1
            self._logger.warning("Heartbeat could not be sent: %s", e)
            return False

    def _log_delivery(self, status: int) -> None:
        """Logs the first delivery, the first after failures, and otherwise a summary per
        success_log_interval_s - often enough to confirm from the device's own log that the
        backend was reached, without a line every interval_s."""
        now = time.monotonic()
        self._delivered_since_log += 1
        if self._failed_since_delivery:
            self._logger.info(
                "Heartbeat delivered again (HTTP %s) after %d failed attempt(s).", status, self._failed_since_delivery,
            )
        elif self._last_delivery_log_at is None:
            self._logger.info("First heartbeat delivered (HTTP %s).", status)
        elif now - self._last_delivery_log_at >= self._config.success_log_interval_s:
            self._logger.info(
                "%d heartbeat(s) delivered in the last %.0f s.", self._delivered_since_log, now - self._last_delivery_log_at,
            )
        else:
            return
        self._last_delivery_log_at = now
        self._delivered_since_log = 0
        self._failed_since_delivery = 0

    def _process_remote_response(self, response, nonce: str) -> None:
        """Parses the response body and hands it to the remote command dispatcher.

        A response that isn't valid JSON is logged and ignored rather than raised - a malformed
        body must never take the heartbeat loop (or the run) down with it.
        """
        try:
            body = json.loads(response.read().decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._logger.warning("Could not parse heartbeat response body: %s", e)
            return
        self._remote_commands.handle_response(body, nonce)


class RemoteCommandDispatcher:
    """Verifies and applies the config overrides / one-shot commands a heartbeat response carries.

    Runs on HeartbeatSender's background thread. Config overrides are only ever written to disk
    here - they take effect from the next `load_config()` call (the next boot), never mid-run, so
    no in-memory config is touched.
    One-shot commands are dispatched immediately via the factory-dict below; `shutdown-now`,
    `reboot-now`, and `stop-measuring-now` hand off to `Collector`, which applies them on its own
    thread rather than this one (checked from every phase, not just an active measuring session -
    see `Collector.check_remote_shutdown_or_reboot()`). `sim-reset-now` also hands off to
    `Collector`, but `Collector.request_sim_reset()` itself decides whether to act immediately on
    this thread (no run active yet, so nothing else is touching the modem) or defer to the
    collection thread (a run is active, and the modem's serial connection isn't thread-safe).
    `stop-measuring-now` also persists a next-boot "waiting" mode override, same as
    `mode-waiting-now` below, per the device ending up in Waiting Mode once the run it interrupted
    finishes its normal finalize/upload/shutdown sequence. `mode-waiting-now`/
    `mode-auto-now` persist the next-boot device mode as the `system_mode` config override.
    A response carrying overrides or commands is only accepted if it echoes the nonce of the
    request it answers (see `handle_response`), so a captured signed response can't be replayed.
    `start-measuring-now` and `run-selftest-now` hand off to `WaitingLoop`, only meaningful while
    the device is in Waiting Mode. `run-gps-fix-test-now`/`stop-gps-fix-test-now` hand off to their
    own `GpsFixTestRunner` collaborator instead, since that run keeps going on its own background
    thread for up to `gps_fix_test.timeout_s` rather than being handled synchronously like
    `run-selftest-now` - also only meaningful in Waiting Mode. `promote-overrides-now` bakes
    whatever is currently set in config_overrides.toml into config.toml as the new baseline (see
    core/config_sources.py::promote_overrides) - takes effect from the next load_config() call,
    same as every other config_overrides.toml change.
    """

    def __init__(
        self, hmac_secret: str, overrides_path: Path,
        uploader: Uploader, collector: Collector,
        waiting_loop: WaitingLoop | None = None, gps_fix_test_runner: GpsFixTestRunner | None = None,
        config_path: Path | None = None,
    ):
        self._logger = logging.getLogger(__name__)
        self._hmac_secret = hmac_secret
        self._overrides_path = overrides_path
        self._uploader = uploader
        self._collector = collector
        self._waiting_loop = waiting_loop
        self._gps_fix_test_runner = gps_fix_test_runner
        self._config_path = config_path
        self._executed_command_ids: set = set()
        self._command_outcomes: dict = {}
        self._pending_acks: list[dict] = []

    def handle_response(self, body: dict, expected_nonce: str) -> None:
        """Verifies and applies whatever a heartbeat response carries, discarding it on any doubt.

        A response with nothing queued has no `config_overrides`/`commands` keys at all, per the
        backend contract - nothing to verify or apply in that case. `expected_nonce` is the one
        sent with the request this response answers; the response must echo it inside its signed
        body, which ties it to that single request.
        """
        config_overrides = body.get("config_overrides")
        commands = body.get("commands")
        if not config_overrides and not commands:
            return

        if not self._verify_signature(body):
            self._logger.error("Heartbeat response signature invalid or missing - discarding commands.")
            return
        if not self._echoes_nonce(body, expected_nonce):
            self._logger.error("Heartbeat response nonce missing or mismatched - discarding commands.")
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

    @staticmethod
    def _echoes_nonce(body: dict, expected_nonce: str) -> bool:
        nonce = body.get("nonce")
        return isinstance(nonce, str) and hmac.compare_digest(nonce.encode(), expected_nonce.encode())

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
            # Re-queue the remembered outcome - the backend is still redelivering this id, which
            # means it never saw (or never persisted) the ack from when this was first executed.
            outcome = self._command_outcomes.get(command_id)
            if outcome is not None:
                self._pending_acks.append(outcome)
            return

        handler = _ONE_SHOT_HANDLERS.get(entry["command"])
        if handler is None:
            self._logger.warning("Ignoring unknown one-shot command %r.", entry["command"])
            return

        try:
            detail = handler(self, entry)
        except CommandNotApplicable as e:
            self._logger.info(
                "One-shot command %r (id=%r) not applicable: %s", entry["command"], command_id, e,
            )
            self._record_outcome(command_id, entry["command"], "not_applicable", error=str(e))
            return
        except Exception as e:
            self._logger.exception(
                "One-shot command %r (id=%r) failed.", entry["command"], command_id
            )
            self._record_outcome(command_id, entry["command"], "failed", error=str(e))
            return

        if entry["command"] in _SELF_ACKING_COMMANDS:
            # shutdown-now/reboot-now ack themselves on their own schedule, not via the generic
            # success path below: shutdown-now's only ever ack is the "received" the unified
            # shutdown sequence's terminal heartbeat sends on the way out (core/system.py::
            # shutdown()); reboot-now's "success" only comes from seed_reboot_success_ack() once
            # the next boot proves the reboot actually happened. Recording a generic "success"
            # here - right as the command is merely received, before either has actually
            # happened - would race those real acks on this dispatcher's own next heartbeat tick.
            self._logger.info("Received one-shot command %r (id=%r).", entry["command"], command_id)
            return

        self._executed_command_ids.add(command_id)
        self._record_outcome(command_id, entry["command"], "success", error=detail)
        self._logger.info("Executed one-shot command %r (id=%r).", entry["command"], command_id)

    def _record_outcome(
        self, command_id: object, command_name: str, status: str, error: str | None = None,
    ) -> None:
        """Remembers a command's outcome and queues it for the next heartbeat's ack.

        Kept for the dispatcher's whole lifetime (not just until sent) so a redelivered command
        - the backend still hasn't cleared it, meaning it likely never saw this outcome - can be
        re-acked without re-running its handler. `error` doubles as an optional human-readable
        detail on a "success" outcome too (e.g. force-upload-now's "Nothing to upload") - there's
        no separate field for that in the ack schema.
        """
        outcome = {"id": command_id, "command": command_name, "status": status, "error": error}
        self._command_outcomes[command_id] = outcome
        self._pending_acks.append(outcome)

    def seed_reboot_success_ack(self, command_id: object) -> None:
        """Called once at startup with a reboot-now id the previous boot's reboot() persisted
        (see config_sources.py::write_pending_reboot_ack) - the device being back up and
        running this is itself the proof that command actually succeeded, which the "received"
        ack sent on the way out couldn't promise.

        Marks the id executed, not just acked: reboot-now's queue entry may not be cleared on
        the backend yet by the time this dispatcher first sees it, and a fresh process has no
        memory of already having rebooted for it - without this, that redelivery would read as
        a brand new reboot-now and reboot the device again, forever.
        """
        self._executed_command_ids.add(command_id)
        self._record_outcome(command_id, "reboot-now", "success")

    def pending_acks(self) -> list[dict]:
        """The command outcomes queued to go out on the next heartbeat, oldest first."""
        return list(self._pending_acks)

    def clear_acks(self) -> None:
        """Drops every currently-queued ack - called only once they've actually been sent."""
        self._pending_acks.clear()

    def _handle_force_upload(self, entry: dict) -> str | None:
        uploaded = self._uploader.upload_pending_files()
        return None if uploaded else "Nothing to upload"

    def _handle_shutdown(self, entry: dict) -> None:
        self._collector.request_shutdown(entry["id"])

    def _handle_reboot(self, entry: dict) -> None:
        self._collector.request_reboot(entry["id"])

    def _handle_stop_measuring(self, entry: dict) -> None:
        if not self._collector.is_collecting:
            raise CommandNotApplicable("device is not actively measuring - nothing to stop")
        write_overrides_file(self._overrides_path, {"system_mode": "waiting"})
        self._collector.request_stop_measuring()

    def _handle_sim_reset(self, entry: dict) -> None:
        self._collector.request_sim_reset()

    def _handle_mode_waiting(self, entry: dict) -> None:
        write_overrides_file(self._overrides_path, {"system_mode": "waiting"})
        self._logger.info("Persisted device mode 'waiting' (takes effect next boot).")

    def _handle_mode_auto(self, entry: dict) -> None:
        write_overrides_file(self._overrides_path, {"system_mode": "auto"})
        self._logger.info("Persisted device mode 'auto' (takes effect next boot).")

    def _handle_start_measuring(self, entry: dict) -> None:
        if self._waiting_loop is None:
            raise CommandNotApplicable("device is not in Waiting Mode")
        self._waiting_loop.request_start_measuring()

    def _handle_run_selftest(self, entry: dict) -> None:
        if self._waiting_loop is None:
            raise CommandNotApplicable("device is not in Waiting Mode")
        self._waiting_loop.request_run_selftest()

    def _handle_run_gps_fix_test(self, entry: dict) -> None:
        if self._gps_fix_test_runner is None:
            raise CommandNotApplicable("device is not in Waiting Mode")
        self._gps_fix_test_runner.start()

    def _handle_stop_gps_fix_test(self, entry: dict) -> None:
        if self._gps_fix_test_runner is None:
            raise CommandNotApplicable("device is not in Waiting Mode")
        self._gps_fix_test_runner.stop()

    def _handle_promote_overrides(self, entry: dict) -> None:
        if self._config_path is None:
            raise CommandNotApplicable("no config_path was configured")
        promoted = promote_overrides(self._config_path)
        if not promoted:
            self._logger.info("promote-overrides-now: nothing to promote.")


# Commands whose real ack comes from somewhere other than _dispatch_one()'s generic success
# path - see the check in _dispatch_one() for why.
_SELF_ACKING_COMMANDS = frozenset({"shutdown-now", "reboot-now"})

_ONE_SHOT_HANDLERS: dict[str, Callable[["RemoteCommandDispatcher", dict], str | None]] = {
    "force-upload-now": RemoteCommandDispatcher._handle_force_upload,
    "shutdown-now": RemoteCommandDispatcher._handle_shutdown,
    "reboot-now": RemoteCommandDispatcher._handle_reboot,
    "stop-measuring-now": RemoteCommandDispatcher._handle_stop_measuring,
    "sim-reset-now": RemoteCommandDispatcher._handle_sim_reset,
    "mode-waiting-now": RemoteCommandDispatcher._handle_mode_waiting,
    "mode-auto-now": RemoteCommandDispatcher._handle_mode_auto,
    "start-measuring-now": RemoteCommandDispatcher._handle_start_measuring,
    "run-selftest-now": RemoteCommandDispatcher._handle_run_selftest,
    "run-gps-fix-test-now": RemoteCommandDispatcher._handle_run_gps_fix_test,
    "stop-gps-fix-test-now": RemoteCommandDispatcher._handle_stop_gps_fix_test,
    "promote-overrides-now": RemoteCommandDispatcher._handle_promote_overrides,
}
