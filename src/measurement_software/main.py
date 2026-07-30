import logging
import signal
import sys
from pathlib import Path

from measurement_software.core.collector import Collector
from measurement_software.core.config import load_config, AppConfig
from measurement_software.core.datapoint import Datapoint
from measurement_software.core.fan_controller import FanController
from measurement_software.core.heartbeat_sender import HeartbeatSender
from measurement_software.core.latency_result import LATENCY_FILE_PREFIX, LatencyResult
from measurement_software.core.latency_tester import LatencyTester
from measurement_software.core.logging_setup import setup_logging
from measurement_software.core.run_log import RunLog, discard_stale_temp_files, finalize, recover_unfinalized
from measurement_software.core.run_phase import RunPhase
from measurement_software.core.run_status import RunStatusTracker
from measurement_software.core.status_display_updater import StatusDisplayUpdater
from measurement_software.core.storage_status import StorageStatusReporter
from measurement_software.core.uploader import Uploader
from measurement_software.displays import create_display
from measurement_software.gnss import create_gnss_receiver
from measurement_software.gnss.null_gnss_receiver import NullGNSSReceiver
from measurement_software.modems import create_modem
from measurement_software.core.system import setup_gpio, signal_completion, cleanup_gpio, perform_shutdown, log_ip_addrs

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

    fan_controller = FanController(config.fan)
    fan_controller.start()

    try:
        upload_dir = Path(config.uploader.upload_dir)
        modem = create_modem(config.modem)
        gnss_receiver = (
            create_gnss_receiver(config.gnss_receiver)
            if config.collector.gps_enabled
            else NullGNSSReceiver()
        )
        run_status = RunStatusTracker(config.run_status)
        storage_status = StorageStatusReporter(upload_dir, config.storage)
        heartbeat = HeartbeatSender(config.heartbeat, run_status, storage_status)
        collector = Collector(
            modem, gnss_receiver, config.collector, run_status, heartbeat,
            RunLog[Datapoint](upload_dir), config.device,
        )
        latency_tester = LatencyTester(
            config.latency_test,
            collector.movement,
            RunLog[LatencyResult](upload_dir, LATENCY_FILE_PREFIX),
            config.device,
        )
        uploader = Uploader(config.uploader)

        display = create_display(config.display)
        run_phase = RunPhase()
        status_display = StatusDisplayUpdater(display, run_phase, run_status, config.collector, config.display)

        display.open()
        status_display.start()
        try:
            run_phase.set("uploading pending data")
            discard_stale_temp_files(upload_dir)
            recover_unfinalized(upload_dir)
            uploader.upload_pending_files()
            storage_status.status()
            log_ip_addrs(config.system.network_interfaces)

            run_phase.set("collecting")
            latency_tester.start()
            try:
                run_log_path = collector.collect()
            finally:
                latency_tester.stop()

            run_phase.set("finalizing and uploading")
            finalize(run_log_path)
            if latency_tester.log_path is not None:
                finalize(latency_tester.log_path)
            uploader.upload_pending_files()

            run_phase.set("done")
            if config.system.running_on_pi:
                signal_completion(config.system.shutdown_gpio)
        finally:
            status_display.stop()
            display.close()
    finally:
        fan_controller.stop()

    return config

if __name__ == "__main__":
    try:
        config = main()
        if config.system.running_on_pi:
            perform_shutdown(config.modem)
            cleanup_gpio()
    except Exception as e:
        logger.critical("Critical error: %s", e)
