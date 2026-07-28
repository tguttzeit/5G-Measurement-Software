import logging
import signal
import sys
from pathlib import Path

from measurement_software.core.collector import Collector
from measurement_software.core.config import load_config, AppConfig
from measurement_software.core.heartbeat_sender import HeartbeatSender
from measurement_software.core.logging_setup import setup_logging
from measurement_software.core.run_status import RunStatusTracker
from measurement_software.core.uploader import Uploader
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

    modem = create_modem(config.modem)
    gnss_receiver = (
        create_gnss_receiver(config.gnss_receiver)
        if config.collector.gps_enabled
        else NullGNSSReceiver()
    )
    run_status = RunStatusTracker(config.run_status)
    heartbeat = HeartbeatSender(config.heartbeat, run_status)
    collector = Collector(modem, gnss_receiver, config.collector, run_status, heartbeat)
    uploader = Uploader(config.uploader)

    uploader.upload_pending_files()
    log_ip_addrs(config.system.network_interfaces)

    datapoints = collector.collect()
    uploader.save_datapoints(datapoints)
    uploader.upload_pending_files()

    if config.system.running_on_pi:
        signal_completion(config.system.shutdown_gpio)

    return config

if __name__ == "__main__":
    try:
        config = main()
        if config.system.running_on_pi:
            perform_shutdown(config.modem)
            cleanup_gpio()
    except Exception as e:
        logger.critical("Critical error: %s", e)