import logging
import signal
import sys
from pathlib import Path

from measurement_software.core.collector import Collector, RemoteRebootRequested, RemoteShutdownRequested
from measurement_software.core.config import AppConfig, load_config
from measurement_software.core.crash_policy import CrashAction, CrashPolicy, error_fingerprint
from measurement_software.core.config_sources import (
    LEGACY_MODE_OVERRIDE_FILENAME,
    OVERRIDES_FILENAME,
    PENDING_REBOOT_ACK_FILENAME,
    migrate_legacy_mode_override,
    read_and_clear_pending_reboot_ack,
    write_overrides_file,
)
from measurement_software.core.datapoint import Datapoint
from measurement_software.core.link_watchdog import LinkWatchdog
from measurement_software.core.maintenance_mode import run_maintenance_mode
from measurement_software.core.movement_gate import MovementGate
from measurement_software.core.run_log import RunLog, discard_stale_temp_files, finalize, recover_unfinalized
from measurement_software.core.run_phase import RunPhase, RunPhaseState, WaitingLoop
from measurement_software.core.system import (
    ClockSync,
    cleanup_gpio,
    finalize_and_upload_pending,
    log_ip_addrs,
    perform_shutdown,
    reboot,
    setup_gpio,
    shutdown,
    signal_completion,
    start_quectel_cm,
)
from measurement_software.core.uploader import Uploader
from measurement_software.core.util import setup_logging
from measurement_software.displays import create_display
from measurement_software.gnss import create_gnss_receiver
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
    config_path = Path(__file__).resolve().parent.parent.parent / "config.toml"
    migrate_legacy_mode_override(
        config_path.parent / OVERRIDES_FILENAME, config_path.parent / LEGACY_MODE_OVERRIDE_FILENAME
    )
    config = load_config(config_path)
    setup_logging(config.logging)
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigterm)
    crash_policy = CrashPolicy(
        config_path.parent, config.system.crash_same_error_limit, config.system.crash_reboot_limit
    )
    crash_policy.log_previous_report()
    if crash_policy.in_maintenance():
        run_maintenance_mode(config, crash_policy.maintenance_details())
        sys.exit(0)

    if config.system.running_on_pi:
        try:
            setup_gpio(config.system.shutdown_gpio)
        except ImportError:
            logger.error("RPi.GPIO is not available - continuing as if system.running_on_pi were false.")
            config.system.running_on_pi = False

    pending_reboot_ack = read_and_clear_pending_reboot_ack(
        config_path.parent / PENDING_REBOOT_ACK_FILENAME
    )

    try:
        _run(config, config_path, pending_reboot_ack)
    except TerminationRequested:
        logger.warning("SIGTERM received - finalizing and uploading this run's data; no shutdown or reboot.")
        finalize_and_upload_pending(config)
        raise
    except RemoteShutdownRequested as e:
        logger.critical("Remote shutdown-now command received - shutting down.")
        shutdown(config, "remote_shutdown_command", command_id=e.command_id)
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
        _handle_unhandled_error(config, config_path, crash_policy, e)
        raise

    crash_policy.reset()
    return config

def _handle_unhandled_error(
    config: AppConfig, config_path: Path, crash_policy: CrashPolicy, error: Exception,
) -> None:
    """Recovers per CrashPolicy: reboot; Waiting Mode plus reboot once the error keeps repeating;
    maintenance mode on the next boot if even that didn't help.

    If the crash can't be recorded, nothing could detect a reboot loop, so this boot goes into
    maintenance mode right away - with the cellular link this run already brought up.
    """
    try:
        decision = crash_policy.record(error)
        crash_policy.write_report(decision, error)
    except Exception:
        logger.critical("Unhandled exception reached main()'s top level.", exc_info=error)
        logger.exception("Could not record the crash - entering maintenance mode without a reboot.")
        run_maintenance_mode(config, {"fingerprint": error_fingerprint(error)}, start_link=False)
        return

    logger.critical(
        "Unhandled exception reached main()'s top level (%s; same error %d time(s) in a row, "
        "%d reboot(s) in a row before it) - %s.",
        decision.fingerprint, decision.same_error_count, decision.consecutive_reboots,
        _CRASH_ACTION_DESCRIPTIONS[decision.action],
        exc_info=error,
    )
    if decision.action is CrashAction.ESCALATE_TO_WAITING:
        try:
            write_overrides_file(config_path.parent / OVERRIDES_FILENAME, {"system_mode": "waiting"})
        except Exception:
            logger.exception("Could not persist Waiting Mode - rebooting anyway.")
    reboot(config, _CRASH_REBOOT_REASONS[decision.action])

_CRASH_ACTION_DESCRIPTIONS = {
    CrashAction.REBOOT: "rebooting",
    CrashAction.ESCALATE_TO_WAITING: "switching to Waiting Mode and rebooting",
    CrashAction.ENTER_MAINTENANCE: "rebooting into maintenance mode",
}
_CRASH_REBOOT_REASONS = {
    CrashAction.REBOOT: "unhandled_exception",
    CrashAction.ESCALATE_TO_WAITING: "repeated_unhandled_exception",
    CrashAction.ENTER_MAINTENANCE: "unhandled_exception_after_waiting_mode",
}

def _run(config: AppConfig, config_path: Path, pending_reboot_ack: object | None = None) -> None:
    """Runs one measurement collection cycle.

    Uploads pending data at the waiting/ACTIVE_MEASURING transitions (right after the connection
    opens and again right before it next closes), rather than at fixed start/end points.
    `pending_reboot_ack`, if given, is a reboot-now command id the previous boot's reboot() left
    for this boot to ack as actually successful (see config_sources.py::write_pending_reboot_ack).
    """
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
        modem, gnss_receiver, config.collector, run_status, heartbeat,
        RunLog[Datapoint](upload_dir), config.device, gps_fix_status,
        on_sim_reset=lambda: status_display.show_final("SIM reset"),
    )
    waiting_loop = WaitingLoop() if config.system.mode == "waiting" else None
    gps_fix_test_runner = (
        GpsFixTestRunner(
            config.gnss_receiver, config.gps_fix_test,
            config.heartbeat.device_id, config.backend.device_key,
            gps_fix_status,
        )
        if config.system.mode == "waiting" else None
    )
    remote_commands = RemoteCommandDispatcher(
        hmac_secret=config.heartbeat.hmac_secret,
        overrides_path=config_path.parent / OVERRIDES_FILENAME,
        uploader=uploader,
        collector=collector,
        waiting_loop=waiting_loop,
        gps_fix_test_runner=gps_fix_test_runner,
        config_path=config_path,
    )
    if pending_reboot_ack is not None:
        remote_commands.seed_reboot_success_ack(pending_reboot_ack)
    heartbeat.set_remote_commands(remote_commands)
    latency_tester = LatencyTester(
        config.latency_test,
        collector.movement,
        RunLog[LatencyResult](upload_dir, LATENCY_FILE_PREFIX),
        config.device,
        collector.run_id,
    )

    movement_gate = MovementGate(gnss_receiver, config.movement_gate, run_phase, ClockSync(), gps_fix_status)
    link_watchdog = LinkWatchdog(config.link_watchdog, config.quectel_cm, collector.request_radio_reset)

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
        if waiting_loop is not None:
            _wait_for_remote_command(config, heartbeat, waiting_loop, gps_fix_test_runner, run_phase, collector)
        else:
            _run_auto_mode(config, heartbeat, movement_gate, collector)

        run_phase.set(RunPhaseState.ACTIVE_MEASURING)
        uploader.upload_pending_files()
        latency_tester.start()
        try:
            run_log_path = collector.collect()
        finally:
            latency_tester.stop()

        run_phase.set(RunPhaseState.FINALIZING)
        finalize(run_log_path)
        if latency_tester.log_path is not None:
            finalize(latency_tester.log_path)
        uploader.upload_pending_files()

        run_phase.set(RunPhaseState.DONE)
        if config.system.running_on_pi:
            signal_completion(config.system.shutdown_gpio)
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
        link_watchdog.stop()
        status_display.stop()
        if not freeze_display:
            display.close()

def _run_auto_mode(
    config: AppConfig, heartbeat: HeartbeatSender, movement_gate: MovementGate, collector: Collector,
) -> None:
    """Auto Mode: starts heartbeating immediately over the just-opened cellular link, the same way
    Waiting Mode already does - but unlike Waiting Mode, automatically transitions into active
    measuring once MovementGate confirms sustained vehicle movement, with no manual command
    needed. In Auto Mode, staying invisible until movement is confirmed would trade away
    visibility the maintainer wants to have from the moment the device boots.

    Stops the heartbeat before returning so Collector.collect() can start its own without
    double-starting the background thread - same reasoning as Waiting Mode's own transition.
    quectel-CM is already started by _run() at this point.
    """
    heartbeat.start()
    try:
        if not config.movement_gate.enabled:
            logger.info("Movement gate disabled - starting to measure immediately.")
        elif config.collector.gps_enabled:
            movement_gate.wait_for_movement(on_check_remote_shutdown=collector.check_remote_shutdown_or_reboot)
        else:
            logger.warning(
                "GPS disabled (config.collector.gps_enabled=false) - skipping the movement "
                "gate and starting to measure immediately. Testing mode only."
            )
    finally:
        heartbeat.stop()

def _wait_for_remote_command(
    config: AppConfig, heartbeat: HeartbeatSender, waiting_loop: WaitingLoop,
    gps_fix_test_runner: GpsFixTestRunner | None, run_phase: RunPhase, collector: Collector,
) -> None:
    """Waiting Mode: starts heartbeating immediately over the just-opened cellular link, then blocks
    until an explicit remote command (currently only start-measuring-now) says what to do next.

    Movement is not observed at all in this mode. Stops any
    in-progress run-gps-fix-test-now run before returning, since it holds its own GNSS
    connection open on a background thread - `Collector.collect()` opens its own GNSSReceiver on
    the same serial port, so the diagnostic's connection has to be closed first. Stops the
    heartbeat before returning so `Collector.collect()` can start its own without double-starting
    the background thread. quectel-CM is already started by _run() at this point.
    """
    run_phase.set(RunPhaseState.WAITING_MODE_IDLE)
    heartbeat.start()
    try:
        waiting_loop.wait(
            on_run_selftest=lambda: _run_selftest_now(config),
            on_check_remote_shutdown=collector.check_remote_shutdown_or_reboot,
        )
    finally:
        if gps_fix_test_runner is not None:
            gps_fix_test_runner.stop()
        heartbeat.stop()

def _run_selftest_now(config: AppConfig) -> None:
    """Runs every selftest check exactly as the CLI path's own default does (no side effects).

    Triggered by a remote run-selftest-now command while in Waiting Mode (see
    RemoteCommandDispatcher/WaitingLoop). Side-effecting checks (currently just the uploader
    check's real throwaway upload, gated behind --confirm-side-effects on the CLI) are never run
    here - confirm_side_effects is always False, matching the CLI's own default rather than a
    separate allow-list. Results are reported to the backend the same way the CLI path already
    does.
    """
    reporter = SelftestResultsReporter(
        config.selftest, device_id=config.heartbeat.device_id, device_key=config.backend.device_key,
    )
    run_checks(config, available_checks(), confirm_side_effects=False, reporter=reporter)

if __name__ == "__main__":
    try:
        config = main()
        if config.system.running_on_pi:
            perform_shutdown(config.modem)
            cleanup_gpio()
    except TerminationRequested:
        logger.info("Stopped by SIGTERM.")
    except Exception as e:
        logger.critical("Critical error: %s", e)
