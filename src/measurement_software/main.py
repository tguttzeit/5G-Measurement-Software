import logging
import signal
import sys
from pathlib import Path

from measurement_software.core.collector import Collector, RemoteShutdownRequested
from measurement_software.core.config import AppConfig, load_config
from measurement_software.core.config_sources import MODE_OVERRIDE_FILENAME, OVERRIDES_FILENAME
from measurement_software.core.datapoint import Datapoint
from measurement_software.core.movement_gate import MovementGate
from measurement_software.core.run_log import RunLog, discard_stale_temp_files, finalize, recover_unfinalized
from measurement_software.core.run_phase import RunPhase, RunPhaseState, WaitingLoop
from measurement_software.core.system import (
    ClockSync,
    FanController,
    cleanup_gpio,
    log_ip_addrs,
    perform_shutdown,
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

def main() -> AppConfig:
    """Loads configuration, runs one measurement collection cycle, and uploads the results."""
    config_path = Path(__file__).resolve().parent.parent.parent / "config.toml"
    config = load_config(config_path)
    setup_logging(config.logging)
    signal.signal(signal.SIGINT, handle_sigint)
    if config.system.running_on_pi:
        setup_gpio(config.system.shutdown_gpio)

    try:
        fan_controller = FanController(config.fan)
        fan_controller.start()
        try:
            _run(config, fan_controller, config_path)
        finally:
            fan_controller.stop()
    except RemoteShutdownRequested:
        logger.critical("Remote shutdown-now command received - shutting down.")
        shutdown(config, "remote_shutdown_command")
        raise
    except SimUnlockError:
        logger.critical("SIM unlock failed - shutting down.", exc_info=True)
        shutdown(config, "sim_unlock_failed")
        raise
    except Exception:
        logger.critical("Unhandled exception reached main()'s top level - shutting down.", exc_info=True)
        shutdown(config, "unhandled_exception")
        raise

    return config

def _run(config: AppConfig, fan_controller: FanController, config_path: Path) -> None:
    """Runs one measurement collection cycle.

    Uploads pending data at the waiting/ACTIVE_MEASURING transitions (right after the connection
    opens and again right before it next closes), rather than at fixed start/end points.
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
    config_status = ConfigStatusReporter(config, fan_controller)
    run_phase = RunPhase()
    gps_fix_status = GpsFixStatusTracker()
    heartbeat = HeartbeatSender(
        config.heartbeat, config.backend, run_status, storage_status, config_status,
        run_phase, gps_fix_status,
    )
    collector = Collector(
        modem, gnss_receiver, config.collector, run_status, heartbeat,
        RunLog[Datapoint](upload_dir), config.device, gps_fix_status,
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
    heartbeat.set_remote_commands(RemoteCommandDispatcher(
        hmac_secret=config.heartbeat.hmac_secret,
        overrides_path=config_path.parent / OVERRIDES_FILENAME,
        mode_override_path=config_path.parent / MODE_OVERRIDE_FILENAME,
        uploader=uploader,
        fan_controller=fan_controller,
        collector=collector,
        waiting_loop=waiting_loop,
        gps_fix_test_runner=gps_fix_test_runner,
    ))
    latency_tester = LatencyTester(
        config.latency_test,
        collector.movement,
        RunLog[LatencyResult](upload_dir, LATENCY_FILE_PREFIX),
        config.device,
    )

    display = create_display(config.display)
    status_display = StatusDisplayUpdater(
        display, run_phase, gps_fix_status, run_status, config.collector, config.display
    )
    movement_gate = MovementGate(gnss_receiver, config.movement_gate, run_phase, ClockSync(), gps_fix_status)

    display.open()
    status_display.start()
    try:
        run_phase.set(RunPhaseState.RECOVERING_PENDING_DATA)
        discard_stale_temp_files(upload_dir)
        recover_unfinalized(upload_dir)
        storage_status.status()
        log_ip_addrs(config.system.network_interfaces)

        if waiting_loop is not None:
            _wait_for_remote_command(config, heartbeat, waiting_loop, gps_fix_test_runner, run_phase)
        else:
            _run_auto_mode(config, heartbeat, movement_gate)

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
    finally:
        status_display.stop()
        display.close()

def _run_auto_mode(config: AppConfig, heartbeat: HeartbeatSender, movement_gate: MovementGate) -> None:
    """Auto Mode: opens the cellular link and starts heartbeating immediately, the same way
    Waiting Mode already does - but unlike Waiting Mode, automatically transitions into active
    measuring once MovementGate confirms sustained vehicle movement, with no manual command
    needed. See decision record 0021, which revises 0020's battery-efficiency framing for Auto
    Mode: staying invisible until movement is confirmed traded away visibility the maintainer
    wants to have from the moment the device boots.

    Stops the heartbeat before returning so Collector.collect() can start its own without
    double-starting the background thread - same reasoning as Waiting Mode's own transition.
    """
    if config.system.running_on_pi:
        start_quectel_cm(config.quectel_cm)
    heartbeat.start()
    try:
        if config.collector.gps_enabled:
            movement_gate.wait_for_movement()
        else:
            logger.warning(
                "GPS disabled (config.collector.gps_enabled=false) - skipping the movement "
                "gate and starting to measure immediately. Testing mode only."
            )
    finally:
        heartbeat.stop()

def _wait_for_remote_command(
    config: AppConfig, heartbeat: HeartbeatSender, waiting_loop: WaitingLoop,
    gps_fix_test_runner: GpsFixTestRunner | None, run_phase: RunPhase,
) -> None:
    """Waiting Mode: opens the cellular link and starts heartbeating immediately, then blocks
    until an explicit remote command (currently only start-measuring-now) says what to do next.

    Movement is not observed at all in this mode - see decision record 0020. Stops any
    in-progress run-gps-fix-test-now run before returning, since it holds its own GNSS
    connection open on a background thread - `Collector.collect()` opens its own GNSSReceiver on
    the same serial port, so the diagnostic's connection has to be closed first. Stops the
    heartbeat before returning so `Collector.collect()` can start its own without double-starting
    the background thread.
    """
    if config.system.running_on_pi:
        start_quectel_cm(config.quectel_cm)
    run_phase.set(RunPhaseState.WAITING_MODE_IDLE)
    heartbeat.start()
    try:
        waiting_loop.wait(on_run_selftest=lambda: _run_selftest_now(config))
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
    except Exception as e:
        logger.critical("Critical error: %s", e)
