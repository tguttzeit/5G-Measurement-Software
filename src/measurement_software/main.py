import logging
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from measurement_software.core.collector import (
    Collector,
    RemoteRebootRequested,
    RemoteShutdownRequested,
    StopMeasuringRequested,
)
from measurement_software.core.config import AppConfig, load_config
from measurement_software.core.crash_policy import CrashAction, CrashPolicy, error_fingerprint
from measurement_software.core.config_sources import (
    LEGACY_MODE_OVERRIDE_FILENAME,
    LOCAL_CONFIG_FILENAME,
    MANUAL_NEXT_BOOT_FILENAME,
    OVERRIDES_FILENAME,
    PENDING_REBOOT_ACK_FILENAME,
    PENDING_SHUTDOWN_ACK_FILENAME,
    migrate_legacy_mode_override,
    consume_manual_next_boot_marker,
    discard_manual_next_boot_marker,
    manual_next_boot_marker_present,
    migrate_legacy_system_mode,
    read_and_clear_pending_command_ack,
    save_last_good_link_settings,
)
from measurement_software.core.datapoint import Datapoint
from measurement_software.core.first_fix_wait import FirstFixWaiter
from measurement_software.core.link_watchdog import LINK_UP_WAIT_S, LinkWatchdog
from measurement_software.core.maintenance_mode import run_emergency_link, run_maintenance_mode
from measurement_software.core.modem_access import ModemAccess
from measurement_software.core.movement_gate import MovementGate
from measurement_software.core.ntp_clock import NtpClockSync
from measurement_software.core.run_log import RunLog, discard_stale_temp_files, finalize, recover_unfinalized
from measurement_software.core.run_phase import RunPhase, RunPhaseState, ManualModeLoop
from measurement_software.core.system import (
    ClockSync,
    finalize_and_upload_pending,
    log_ip_addrs,
    reboot,
    send_stopping_heartbeat,
    shutdown,
    start_quectel_cm,
    stop_quectel_cm,
)
from measurement_software.core.uploader import Uploader
from measurement_software.core.util import setup_logging
from measurement_software.displays import create_display
from measurement_software.gnss import create_gnss_receiver
from measurement_software.gnss.gnss_receiver import GNSSFix
from measurement_software.gnss.null_gnss_receiver import NullGNSSReceiver
from measurement_software.latency.tester import LATENCY_FILE_PREFIX, LatencyResult, LatencyTester
from measurement_software.modems import create_modem
from measurement_software.modems.modem import SimUnlockError
from measurement_software.remote.gps_fix_test import GpsFixTestRunner
from measurement_software.remote.heartbeat import HeartbeatSender, RemoteCommandDispatcher
from measurement_software.selftests.selftest_backend import SelftestResultsReporter
from measurement_software.selftests.selftest_checks import available_checks
from measurement_software.selftests.selftest_runner import run_checks
from measurement_software.status.backend_status import (
    ConfigStatusReporter,
    GpsFixStatusTracker,
    RunStatusTracker,
    StorageStatusReporter,
)
from measurement_software.status.display_status import StatusDisplayUpdater

logger = logging.getLogger(__name__)

def handle_sigint(signum, frame):
    """Exits immediately on Ctrl-C without triggering a shutdown."""
    logger.info("SIGINT received - user abort, no shutdown.")
    sys.exit(0)

class TerminationRequested(BaseException):
    """Raised in the main thread when SIGTERM arrives (`systemctl stop`, a system shutdown).

    A BaseException, like KeyboardInterrupt, so no `except Exception` along the way swallows it
    and it is never mistaken for a crash.
    """

def handle_sigterm(signum, frame):
    raise TerminationRequested()

def main() -> AppConfig:
    """Loads configuration, runs one measurement collection cycle, and uploads the results."""
    boot = _Boot(Path(__file__).resolve().parent.parent.parent / "config.toml")
    try:
        _boot(boot)
    except Exception as e:
        _handle_boot_error(boot, e)
        sys.exit(0)
    config, config_path, crash_policy = boot.config, boot.config_path, boot.crash_policy
    manual_next_boot_path, pending_command_acks = boot.manual_next_boot_path, boot.pending_command_acks

    cellular_link = _CellularLink()
    try:
        _run(
            config, config_path, pending_command_acks, manual_next_boot_path,
            crash_policy, cellular_link,
        )
    except TerminationRequested:
        logger.warning("SIGTERM received - finalizing and uploading this run's data; no shutdown or reboot.")
        try:
            send_stopping_heartbeat(config)
            finalize_and_upload_pending(config)
        finally:
            _take_link_down(cellular_link)
        raise
    except RemoteShutdownRequested as e:
        logger.critical("Remote shutdown-now command received - shutting down.")
        shutdown(config, "remote_shutdown_command", command_id=e.command_id, config_path=config_path)
        raise
    except RemoteRebootRequested as e:
        logger.critical("Remote reboot-now command received - rebooting.")
        reboot(config, "remote_reboot_command", command_id=e.command_id, config_path=config_path)
        raise
    except SimUnlockError:
        logger.critical("SIM unlock failed - shutting down.", exc_info=True)
        shutdown(config, "sim_unlock_failed")
        raise
    except Exception as e:
        _handle_unhandled_error(config, config_path, crash_policy, e, cellular_link.quectel_cm_process)
        raise

    crash_policy.reset()
    return config

class _Boot:
    """What main() has set up before _run(), and the step it is at - for handling an error there."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.step = ""
        self.logging_ready = False
        self.config: AppConfig | None = None
        self.crash_policy: CrashPolicy | None = None
        self.manual_next_boot_path: Path | None = None
        self.pending_command_acks: dict[str, object] = {}

def _boot(boot: _Boot) -> None:
    """The steps before _run(). Enters maintenance mode and exits if a previous boot decided so."""
    state_dir = boot.config_path.parent
    # Before anything can fail: the emergency link and maintenance mode rely on SIGTERM to take
    # their cellular link down.
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigterm)
    boot.step = "migrate_legacy_mode_override"
    migrate_legacy_mode_override(state_dir / OVERRIDES_FILENAME, state_dir / LEGACY_MODE_OVERRIDE_FILENAME)
    boot.step = "migrate_legacy_system_mode"
    migrate_legacy_system_mode(state_dir / OVERRIDES_FILENAME)
    boot.step = "load_config"
    config = boot.config = load_config(boot.config_path)
    boot.step = "save_last_good_link_settings"
    save_last_good_link_settings(config, boot.config_path)
    boot.step = "setup_logging"
    setup_logging(config.logging)
    boot.logging_ready = True
    boot.step = "crash_policy"
    crash_policy = boot.crash_policy = _crash_policy(config, state_dir)
    crash_policy.log_previous_report()
    if crash_policy.in_maintenance():
        run_maintenance_mode(config, crash_policy.details())
        sys.exit(0)

    boot.step = "manual_next_boot_marker"
    manual_next_boot_path = state_dir / MANUAL_NEXT_BOOT_FILENAME
    if _apply_manual_next_boot_marker(config, manual_next_boot_path):
        boot.manual_next_boot_path = manual_next_boot_path
    boot.step = "read_and_clear_pending_command_ack"
    for command_name, filename in _PENDING_ACK_FILENAMES.items():
        command_id = read_and_clear_pending_command_ack(state_dir / filename)
        if command_id is not None:
            boot.pending_command_acks[command_name] = command_id

_PENDING_ACK_FILENAMES = {"reboot-now": PENDING_REBOOT_ACK_FILENAME, "shutdown-now": PENDING_SHUTDOWN_ACK_FILENAME}

def _crash_policy(config: AppConfig, state_dir: Path) -> CrashPolicy:
    return CrashPolicy(state_dir, config.system.crash_same_error_limit, config.system.crash_reboot_limit)

def _handle_boot_error(boot: _Boot, error: Exception) -> None:
    """Enters maintenance mode for an error before _run(); blocks like maintenance mode.

    Recorded in the crash policy when the config loaded. Not rebooted: an error this early
    usually sits in a file on the device, which a reboot doesn't change, and the service is not
    restarted either - so without this, the device would sit without a cellular link until
    someone is on site. Without a loadable config, only the link itself comes up.
    """
    _log_boot_error(boot, error)
    if boot.config is None:
        run_emergency_link(boot.config_path)
        return
    run_maintenance_mode(boot.config, _record_boot_error(boot, error))

def _log_boot_error(boot: _Boot, error: Exception) -> None:
    """Logs to stderr (the journal) if logging isn't set up yet. Before the config is loaded,
    only the step, the exception type and the files involved are logged: the message of an error
    from load_config() can carry values from config.local.toml, secrets included."""
    if not boot.logging_ready:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    if boot.config is None:
        logger.critical(
            "Boot step %s failed with %s (files: %s) - entering maintenance mode.",
            boot.step, type(error).__name__, ", ".join(_CONFIG_FILENAMES),
        )
    else:
        logger.critical("Boot step %s failed - entering maintenance mode.", boot.step, exc_info=error)

_CONFIG_FILENAMES = ("config.toml", LOCAL_CONFIG_FILENAME, OVERRIDES_FILENAME, LEGACY_MODE_OVERRIDE_FILENAME)

def _record_boot_error(boot: _Boot, error: Exception) -> dict:
    """Counts the error in the crash policy and returns what maintenance mode reports about it."""
    try:
        crash_policy = boot.crash_policy or _crash_policy(boot.config, boot.config_path.parent)
        crash_policy.write_report(crash_policy.record(error), error)
        return crash_policy.details()
    except Exception:
        logger.exception("Could not record the boot error.")
        return {"fingerprint": error_fingerprint(error)}

def _apply_manual_next_boot_marker(config: AppConfig, marker_path: Path) -> bool:
    """Switches this boot to Manual Mode if stop-measuring-now left its one-shot marker.

    Only acts when the configured mode is Auto; with a persistent Manual Mode the marker has
    nothing to add and is dropped. The marker stays on disk here - _run() consumes it once the
    device has actually entered Manual Mode, so a crash earlier in the boot does not lose it.
    Returns whether the marker is what put this boot into Manual Mode.
    """
    if not manual_next_boot_marker_present(marker_path):
        return False
    if config.system.mode != "auto":
        discard_manual_next_boot_marker(marker_path)
        return False
    logger.warning("stop-measuring-now left a one-shot marker - this boot starts in Manual Mode.")
    config.system.mode = "manual"
    return True

def _take_link_down(cellular_link: "_CellularLink") -> None:
    """Stops the quectel-CM this run left running. The service unit has systemd send SIGTERM to
    this process alone, so the link stays up for the last heartbeat and upload; quectel-CM would
    otherwise only be killed by systemd once this process has exited, without taking the link
    down cleanly."""
    if cellular_link.quectel_cm_process is not None:
        stop_quectel_cm(cellular_link.quectel_cm_process)

@dataclass
class _CellularLink:
    """The quectel-CM process _run() left running, kept past _run()'s own link watchdog so that
    maintenance mode can supervise it instead of starting a second one."""

    quectel_cm_process: subprocess.Popen | None = None

def _handle_unhandled_error(
    config: AppConfig, config_path: Path, crash_policy: CrashPolicy, error: Exception,
    quectel_cm_process: subprocess.Popen | None = None,
) -> None:
    """Recovers per CrashPolicy: reboot; a reboot into the one-shot recovery state once the error
    keeps repeating; maintenance mode on the next boot if even that didn't help.

    If the crash can't be recorded, nothing could detect a reboot loop, so this boot goes into
    maintenance mode right away - taking over `quectel_cm_process`, the cellular link this run
    already brought up, if there is one.
    """
    try:
        decision = crash_policy.record(error)
        crash_policy.write_report(decision, error)
    except Exception:
        logger.critical("Unhandled exception reached main()'s top level.", exc_info=error)
        logger.exception("Could not record the crash - entering maintenance mode without a reboot.")
        run_maintenance_mode(
            config, {"fingerprint": error_fingerprint(error)}, quectel_cm_process=quectel_cm_process,
        )
        return

    logger.critical(
        "Unhandled exception reached main()'s top level (%s; same error %d time(s) in a row, "
        "%d reboot(s) in a row before it) - %s.",
        decision.fingerprint, decision.same_error_count, decision.consecutive_reboots,
        _CRASH_ACTION_DESCRIPTIONS[decision.action],
        exc_info=error,
    )
    reboot(config, _CRASH_REBOOT_REASONS[decision.action])

_CRASH_ACTION_DESCRIPTIONS = {
    CrashAction.REBOOT: "rebooting",
    CrashAction.RECOVERY: "rebooting into the recovery state",
    CrashAction.ENTER_MAINTENANCE: "rebooting into maintenance mode",
}
_CRASH_REBOOT_REASONS = {
    CrashAction.REBOOT: "unhandled_exception",
    CrashAction.RECOVERY: "repeated_unhandled_exception",
    CrashAction.ENTER_MAINTENANCE: "unhandled_exception_after_recovery",
}

def _run(
    config: AppConfig, config_path: Path, pending_command_acks: dict[str, object] | None = None,
    manual_next_boot_path: Path | None = None,
    crash_policy: CrashPolicy | None = None, cellular_link: _CellularLink | None = None,
) -> None:
    """Runs one measurement collection cycle.

    Every path into measuring first waits for a GPS fix in WAITING_FOR_GPS_FIX (see
    FirstFixWaiter), so ACTIVE_MEASURING always means a fix exists. Only Auto Mode gives up on
    that wait (`collector.max_wait_for_first_fix`); the cycle then ends without a run.

    The heartbeat starts once the cellular link is up (or LINK_UP_WAIT_S has passed without it)
    and runs until this function ends, so the backend sees every phase in between.

    Uploads pending data at the two points where the connection is already up for another
    reason: entering ACTIVE_MEASURING, and again after finalizing this run's log (or, when
    there was no run, after the wait that ended it).
    `pending_command_acks` maps a command name to the id the previous boot left for this boot to
    ack as actually successful (see config_sources.py::write_pending_command_ack).
    `manual_next_boot_path`, if given, is the one-shot Manual Mode marker that made this boot a
    Manual Mode boot; it is removed once the device has entered Manual Mode.
    `crash_policy`, if given, says whether this is the one-shot recovery boot after the crash
    escalation: it then blocks on remote commands like Manual Mode, with its own run phase.
    `cellular_link`, if given, receives the quectel-CM process still running when this returns.
    """
    recovery = crash_policy is not None and crash_policy.in_recovery()
    manual = config.system.mode == "manual"
    recovery_idle = recovery and not manual
    upload_dir = Path(config.uploader.upload_dir)
    modem = create_modem(config.modem)
    gnss_receiver = (
        create_gnss_receiver(config.gnss_receiver)
        if config.collector.gps_enabled
        else NullGNSSReceiver()
    )
    run_status = RunStatusTracker(config.run_status)
    storage_status = StorageStatusReporter(upload_dir, config.storage)
    uploader = Uploader(config.uploader, config.backend, config.heartbeat.device_id)
    config_status = ConfigStatusReporter(config)
    run_phase = RunPhase()
    gps_fix_status = GpsFixStatusTracker()
    heartbeat = HeartbeatSender(
        config.heartbeat, config.backend, run_status, storage_status, config_status,
        run_phase, gps_fix_status,
    )
    display = create_display(config.display)
    status_display = StatusDisplayUpdater(
        display, run_phase, gps_fix_status, run_status, config.collector, config.display
    )
    collector = Collector(
        modem, gnss_receiver, config.collector, run_status,
        RunLog[Datapoint](upload_dir), config.device, gps_fix_status,
        on_sim_reset=lambda: status_display.show_final("SIM reset"),
        idle_end_enabled=config.system.mode == "auto",
        modem_access=ModemAccess(modem),
    )
    manual_mode_loop = ManualModeLoop() if manual or recovery else None
    gps_fix_test_runner = (
        GpsFixTestRunner(
            config.gnss_receiver, config.gps_fix_test,
            config.heartbeat.device_id, config.backend.device_key,
            gps_fix_status,
        )
        if (manual or recovery) and config.collector.gps_enabled else None
    )
    clock_sync = ClockSync()
    first_fix_waiter = FirstFixWaiter(
        gnss_receiver, config.collector.wait_log_interval, run_phase, clock_sync, gps_fix_status
    )
    remote_commands = RemoteCommandDispatcher(
        hmac_secret=config.heartbeat.hmac_secret,
        overrides_path=config_path.parent / OVERRIDES_FILENAME,
        uploader=uploader,
        collector=collector,
        manual_mode_loop=manual_mode_loop,
        gps_fix_test_runner=gps_fix_test_runner,
        config_path=config_path,
        run_phase=run_phase,
        manual_next_boot_path=config_path.parent / MANUAL_NEXT_BOOT_FILENAME,
    )
    for command_name, command_id in (pending_command_acks or {}).items():
        remote_commands.seed_success_ack(command_id, command_name)
    heartbeat.set_remote_commands(remote_commands)
    latency_tester = LatencyTester(
        config.latency_test,
        collector.movement,
        RunLog[LatencyResult](upload_dir, LATENCY_FILE_PREFIX),
        config.device,
        collector.run_id,
    )

    movement_gate = MovementGate(gnss_receiver, config.movement_gate, run_phase, clock_sync, gps_fix_status)
    ntp_clock_sync = NtpClockSync(config.clock.ntp_server, config.clock.ntp_timeout_s)
    link_watchdog = LinkWatchdog(
        config.link_watchdog, config.quectel_cm, collector.request_radio_reset,
        on_link_up=ntp_clock_sync.sync if ntp_clock_sync.enabled else None,
    )

    display.open()
    status_display.start()
    freeze_display = False
    try:
        run_phase.set(RunPhaseState.RECOVERING_PENDING_DATA)
        discard_stale_temp_files(upload_dir)
        recover_unfinalized(upload_dir)
        storage_status.status()
        log_ip_addrs(config.system.network_interfaces)

        if config.system.running_on_pi:
            link_watchdog.start(start_quectel_cm(config.quectel_cm))
            link_watchdog.wait_for_link(LINK_UP_WAIT_S)
        heartbeat.start()
        if manual_mode_loop is not None:
            first_fix = _run_manual_mode(
                config, heartbeat, manual_mode_loop, gps_fix_test_runner, run_phase, collector,
                first_fix_waiter,
                on_entered=(
                    (lambda: consume_manual_next_boot_marker(manual_next_boot_path))
                    if manual_next_boot_path is not None else None
                ),
                recovery_details=crash_policy.details() if recovery_idle else None,
                on_recovery_entered=crash_policy.consume_recovery if recovery else None,
            )
        else:
            first_fix = _run_auto_mode(config, movement_gate, collector, first_fix_waiter)

        run_log_path = None
        if first_fix is not None:
            run_phase.set(RunPhaseState.ACTIVE_MEASURING)
            uploader.upload_pending_files()
            latency_tester.start()
            try:
                run_log_path = collector.collect()
            finally:
                latency_tester.stop()

        run_phase.set(RunPhaseState.FINALIZING)
        if run_log_path is not None:
            finalize(run_log_path)
            if latency_tester.log_path is not None:
                finalize(latency_tester.log_path)
        uploader.upload_pending_files()

        run_phase.set(RunPhaseState.DONE)
    except RemoteShutdownRequested:
        freeze_display = True
        status_display.stop()
        status_display.show_final(RunPhaseState.SHUTTING_DOWN)
        raise
    except RemoteRebootRequested:
        freeze_display = True
        status_display.stop()
        status_display.show_final(RunPhaseState.REBOOTING)
        raise
    finally:
        # Heartbeat first: it joins the thread that dispatches commands, so no
        # run-gps-fix-test-now can start the runner again after it has been stopped below.
        heartbeat.stop()
        if gps_fix_test_runner is not None:
            gps_fix_test_runner.stop()
        link_watchdog.stop()
        if cellular_link is not None:
            cellular_link.quectel_cm_process = link_watchdog.process
        status_display.stop()
        if not freeze_display:
            display.close()

def _run_auto_mode(
    config: AppConfig, movement_gate: MovementGate, collector: Collector,
    first_fix_waiter: FirstFixWaiter,
) -> GNSSFix | None:
    """Auto Mode: heartbeats from the just-opened cellular link on (started by _run(), the same way
    as in Manual Mode) - but unlike Manual Mode, automatically transitions into active
    measuring once a GPS fix exists and MovementGate confirms sustained vehicle movement, with no
    manual command needed. Keeping the modem closed while waiting would save battery, but an
    unattended device that stays invisible until movement is confirmed has no way to report that
    it is running at all.

    Returns the first fix, or None if none arrived within `collector.max_wait_for_first_fix` -
    nothing is measured then. Only shutdown-now/reboot-now are honored while waiting:
    stop-measuring-now is not applicable in Auto Mode, since nothing was started by a command.

    The heartbeat is started and stopped by _run(), so it covers this wait. quectel-CM is
    already started by _run() at this point.
    """
    first_fix = first_fix_waiter.wait(
        collector.check_remote_shutdown_or_reboot, config.collector.max_wait_for_first_fix,
    )
    if first_fix is None:
        return None
    if not config.movement_gate.enabled:
        logger.info("Movement gate disabled - starting to measure immediately.")
    elif config.collector.gps_enabled:
        movement_gate.wait_for_movement(
            first_fix.position, on_check_remote_shutdown=collector.check_remote_shutdown_or_reboot,
        )
    else:
        logger.warning(
            "GPS disabled (config.collector.gps_enabled=false) - skipping the movement "
            "gate and starting to measure immediately. Testing mode only."
        )
    return first_fix

def _run_manual_mode(
    config: AppConfig, heartbeat: HeartbeatSender, manual_mode_loop: ManualModeLoop,
    gps_fix_test_runner: GpsFixTestRunner | None, run_phase: RunPhase, collector: Collector,
    first_fix_waiter: FirstFixWaiter,
    on_entered: Callable[[], None] | None = None,
    recovery_details: dict | None = None, on_recovery_entered: Callable[[], None] | None = None,
) -> GNSSFix:
    """Manual Mode (or the recovery state, which waits the same way): blocks
    until an explicit remote command (currently only start-measuring-now) says what to do next.
    After start-measuring-now it waits for a GPS fix, with no timeout - an operator is present, so
    the device stays reachable until a fix arrives or a command ends the wait. A
    stop-measuring-now during that wait returns to the manual idle instead of measuring.

    Movement is not observed at all in this mode. Stops any in-progress
    run-gps-fix-test-now run before the fix wait, since it holds its own GNSS
    connection open on a background thread - the wait and `Collector.collect()` open their own
    GNSSReceiver on the same serial port, so the diagnostic's connection has to be closed first.
    The heartbeat, started by _run() before this, keeps running through all of it. quectel-CM is
    already started by _run() at this point.

    `recovery_details` (only given when this boot's wait is the recovery state, not a deliberate
    Manual Mode) switches the idle phase to RECOVERY_IDLE and goes out with each heartbeat until
    the wait ends. `on_recovery_entered` runs first, so the recovery applies to this boot only -
    even if the device loses power right after.
    """
    idle_phase = RunPhaseState.RECOVERY_IDLE if recovery_details is not None else RunPhaseState.MANUAL_MODE_IDLE
    if on_recovery_entered is not None:
        on_recovery_entered()
    if recovery_details is not None:
        heartbeat.set_recovery({**recovery_details, "action": _RECOVERY_HINT})
    run_phase.set(idle_phase)
    if on_entered is not None:
        on_entered()
    try:
        while True:
            manual_mode_loop.wait(
                on_run_selftest=lambda: _run_selftest_now(config),
                on_check_remote_shutdown=collector.check_remote_shutdown_or_reboot,
            )
            # The phase leaves the idle first: the dispatcher only starts a GPS fix test while idle.
            run_phase.set(RunPhaseState.WAITING_FOR_GPS_FIX)
            if gps_fix_test_runner is not None:
                gps_fix_test_runner.stop()
            try:
                return first_fix_waiter.wait(collector.check_remote_commands_before_measuring, None)
            except StopMeasuringRequested:
                logger.warning("Start aborted by stop-measuring-now before a GPS fix - back to Manual Mode idle.")
                manual_mode_loop.cancel_start_request()
                run_phase.set(idle_phase)
    finally:
        if recovery_details is not None:
            heartbeat.set_recovery(None)

# Sent as the action of the crash details: at most 64 characters, or the backend rejects the
# whole heartbeat. The boot after this one is a normal one again.
_RECOVERY_HINT = "Recovery boot - idle until start-measuring-now"

def _run_selftest_now(config: AppConfig) -> None:
    """Runs every selftest check, exactly as the CLI path does.

    Triggered by a remote run-selftest-now command while in Manual Mode (see
    RemoteCommandDispatcher/ManualModeLoop). No selftest check has a side effect, so there is
    nothing to exclude. Results are reported to the backend the same way the CLI path already
    does.
    """
    reporter = SelftestResultsReporter(
        config.selftest, device_id=config.heartbeat.device_id, device_key=config.backend.device_key,
    )
    run_checks(config, available_checks(), reporter=reporter)

def _shutdown_after_completed_run(config: AppConfig) -> None:
    """Ends a completed run through the one shutdown path, so it reports SHUTTING_DOWN too."""
    if config.system.running_on_pi:
        shutdown(config, "run_completed")

def _run_as_service() -> None:
    """Runs main() and the shutdown after a completed run. A remote shutdown-now or reboot-now has
    already been carried out and logged by main(), which only re-raises it to end the process."""
    try:
        _shutdown_after_completed_run(main())
    except TerminationRequested:
        logger.info("Stopped by SIGTERM.")
    except (RemoteShutdownRequested, RemoteRebootRequested):
        pass
    except Exception as e:
        logger.critical("Critical error: %s", e)

if __name__ == "__main__":
    _run_as_service()
