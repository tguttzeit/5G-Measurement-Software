import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable

from measurement_software.core.config import AppConfig, LinkWatchdogConfig, QuectelCmConfig, load_link_settings
from measurement_software.core.crash_policy import CRASH_STATE_FILENAME
from measurement_software.core.link_watchdog import LINK_UP_WAIT_S, LinkWatchdog
from measurement_software.core.modem_access import ModemAccess
from measurement_software.core.run_phase import RunPhase, RunPhaseState
from measurement_software.core.system import start_quectel_cm, stop_quectel_cm
from measurement_software.modems import create_modem
from measurement_software.remote.heartbeat import HeartbeatSender
from measurement_software.status.backend_status import RunStatusTracker, StorageStatusReporter

logger = logging.getLogger(__name__)

# What the backend shows as the critical error's action: at most 64 characters, or it rejects
# the whole heartbeat.
_ACTION = "Maintenance mode - debug the device over SSH"


def run_maintenance_mode(
    config: AppConfig, details: dict, stop_event: threading.Event | None = None,
    quectel_cm_process: subprocess.Popen | None = None,
) -> None:
    """Runs nothing but the cellular link, after telling the backend once that the device is in
    a critical state. Blocks until `stop_event` is set, which by default it never is - the
    process only ends via SIGTERM.

    Deliberately touches as little as possible - no GNSS, no display, no measuring, and the
    modem's AT port only for the moment of a radio reset - since whatever kept crashing could be
    in any of them. SSH goes through the cellular link, so the link is always supervised by a
    LinkWatchdog: `quectel_cm_process`, if this process already runs one, is taken over rather
    than started a second time. Whether the device is reachable shows from outside (e.g. a VPN
    dashboard), so only the one notification is sent. Every step is guarded on its own: a step that fails must
    not take the others down with it. On SIGTERM the link is taken down before the process ends.
    """
    stop_event = stop_event or threading.Event()
    logger.critical(
        "MAINTENANCE MODE: unhandled errors kept recurring even in Manual Mode - debug the device "
        "over SSH. Measuring is disabled; only the cellular link is kept up. "
        "To leave: fix the cause, delete %s next to config.toml and reboot.", CRASH_STATE_FILENAME,
    )
    watchdog = _keep_link_up(config, quectel_cm_process)
    try:
        _wait_for_link(watchdog)
        if _notify_backend(config, details, stop_event):
            stop_event.wait()
    finally:
        _take_link_down(watchdog)


def run_emergency_link(config_path: Path, stop_event: threading.Event | None = None) -> None:
    """Maintenance mode for a device whose configuration can't be loaded: keeps up nothing but
    the cellular link, from what load_link_settings() can still read. Blocks like
    run_maintenance_mode().

    Without a configuration there is no backend to notify and no modem to reset; quectel-CM
    restarts are all the watchdog does. Nothing is persisted, so once the configuration is fixed
    over SSH, the next reboot starts normally.
    """
    stop_event = stop_event or threading.Event()
    logger.critical(
        "MAINTENANCE MODE: the configuration next to %s can't be loaded. Measuring is disabled; "
        "only the cellular link is kept up. To leave: fix the configuration over SSH and reboot.",
        config_path.name,
    )
    settings = load_link_settings(config_path)
    watchdog = (
        _supervise_link(settings.link_watchdog, settings.quectel_cm, lambda: False, None)
        if settings.running_on_pi else None
    )
    try:
        stop_event.wait()
    finally:
        _take_link_down(watchdog)


def _keep_link_up(config: AppConfig, quectel_cm_process: subprocess.Popen | None) -> LinkWatchdog | None:
    if not config.system.running_on_pi:
        return None
    return _supervise_link(config.link_watchdog, config.quectel_cm, _radio_reset(config), quectel_cm_process)


def _supervise_link(
    watchdog_config: LinkWatchdogConfig, quectel_cm_config: QuectelCmConfig,
    request_radio_reset: Callable[[], bool], quectel_cm_process: subprocess.Popen | None,
) -> LinkWatchdog | None:
    try:
        watchdog = LinkWatchdog(watchdog_config, quectel_cm_config, request_radio_reset=request_radio_reset)
        watchdog.start(quectel_cm_process or start_quectel_cm(quectel_cm_config))
        return watchdog
    except Exception:
        logger.exception("Could not bring up the cellular link in maintenance mode.")
        return None


def _wait_for_link(watchdog: LinkWatchdog | None) -> None:
    """Gives a just-started quectel-CM the moment it needs, so the notification's first attempt
    doesn't fail only for lack of a link."""
    if watchdog is None:
        return
    try:
        watchdog.wait_for_link(LINK_UP_WAIT_S)
    except Exception:
        logger.exception("Could not wait for the cellular link.")


def _take_link_down(watchdog: LinkWatchdog | None) -> None:
    """Stops the watchdog and the quectel-CM it last (re)started: systemd signals only this process
    and kills whatever is left once it exited, which wouldn't take the link down cleanly."""
    if watchdog is None:
        return
    try:
        watchdog.stop()
        if watchdog.process is not None:
            stop_quectel_cm(watchdog.process)
    except Exception:
        logger.exception("Could not take the cellular link down.")


def _radio_reset(config: AppConfig) -> Callable[[], bool]:
    """Resets the radio through the AT port, opened only for that; a modem that can't even be
    set up leaves the watchdog with quectel-CM restarts alone."""
    try:
        return ModemAccess(create_modem(config.modem)).reset_radio_if_idle
    except Exception:
        logger.exception("Could not set up the modem for radio resets in maintenance mode.")
        return lambda: False


def _notify_backend(config: AppConfig, details: dict, stop_event: threading.Event) -> bool:
    """Sends one critical-error heartbeat, retrying until it's delivered. Returns False if
    stopped before that."""
    try:
        sender = HeartbeatSender(
            config.heartbeat, config.backend,
            RunStatusTracker(config.run_status),
            StorageStatusReporter(Path(config.uploader.upload_dir), config.storage),
            run_phase=RunPhase(RunPhaseState.CRITICAL_ERROR),
            critical_error={**details, "action": _ACTION},
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
