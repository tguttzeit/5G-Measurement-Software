import logging
import threading
from pathlib import Path

from measurement_software.core.config import AppConfig
from measurement_software.core.crash_policy import CRASH_STATE_FILENAME
from measurement_software.core.link_watchdog import LinkWatchdog
from measurement_software.core.run_phase import RunPhase, RunPhaseState
from measurement_software.core.system import start_quectel_cm
from measurement_software.remote.heartbeat import HeartbeatSender
from measurement_software.status.backend_status import RunStatusTracker, StorageStatusReporter

logger = logging.getLogger(__name__)

_DEBUG_HINT = "Unhandled errors kept recurring even in Waiting Mode - debug the device over SSH."


def run_maintenance_mode(
    config: AppConfig, details: dict, stop_event: threading.Event | None = None, start_link: bool = True,
) -> None:
    """Runs nothing but the cellular link, after telling the backend once that the device is in
    a critical state. Blocks until `stop_event` is set, which by default it never is - the
    process only ends via SIGTERM.

    Deliberately touches as little as possible - no modem AT port, no GNSS, no display, no
    measuring - since whatever kept crashing could be in any of them. The link stays up so the
    device remains reachable over SSH; whether it is, Tailscale already shows, so only the one
    notification is sent. Every step is guarded on its own: a step that fails must not take the
    others down with it. `start_link=False` is for a process whose quectel-CM is already running.
    """
    stop_event = stop_event or threading.Event()
    logger.critical(
        "MAINTENANCE MODE: %s Measuring is disabled; only the cellular link is kept up. "
        "To leave: fix the cause, delete %s next to config.toml and reboot.", _DEBUG_HINT, CRASH_STATE_FILENAME,
    )
    if start_link:
        _keep_link_up(config)
    if _notify_backend(config, details, stop_event):
        stop_event.wait()


def _keep_link_up(config: AppConfig) -> None:
    if not config.system.running_on_pi:
        return
    try:
        watchdog = LinkWatchdog(config.link_watchdog, config.quectel_cm, request_radio_reset=lambda: False)
        watchdog.start(start_quectel_cm(config.quectel_cm))
    except Exception:
        logger.exception("Could not bring up the cellular link in maintenance mode.")


def _notify_backend(config: AppConfig, details: dict, stop_event: threading.Event) -> bool:
    """Sends one critical-error heartbeat, retrying until it's delivered. Returns False if
    stopped before that."""
    try:
        sender = HeartbeatSender(
            config.heartbeat, config.backend,
            RunStatusTracker(config.run_status),
            StorageStatusReporter(Path(config.uploader.upload_dir), config.storage),
            run_phase=RunPhase(RunPhaseState.CRITICAL_ERROR),
            critical_error={**details, "action": _DEBUG_HINT},
        )
        if not sender.is_usable():
            return True
    except Exception:
        logger.exception("Could not set up the critical-error notification.")
        return True

    while True:
        try:
            if sender.send_once(
                RunStatusTracker(config.run_status).status(),
                StorageStatusReporter(Path(config.uploader.upload_dir), config.storage).status(),
            ):
                logger.info("Backend notified of the critical error.")
                return True
        except Exception:
            logger.exception("Critical-error notification failed.")
        if stop_event.wait(config.heartbeat.interval_s):
            return False
