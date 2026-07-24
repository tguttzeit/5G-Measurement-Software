import logging
import signal
import sys
from pathlib import Path

from core.collector import Collector
from core.config import load_config, AppConfig
from core.logging_setup import setup_logging
from core.uploader import Uploader
from gnss import create_gnss_receiver
from modems import create_modem
from core.system import setup_gpio, signal_completion, cleanup_gpio, perform_shutdown, log_ip_addrs

logger = logging.getLogger(__name__)

def handle_sigint(signum, frame):
    logger.info("SIGINT received - user abort, no shutdown.")
    sys.exit(0)

def main() -> AppConfig:
    config = load_config(Path("config.toml"))
    setup_logging(config.logging)
    signal.signal(signal.SIGINT, handle_sigint)
    if config.system.running_on_pi:
        setup_gpio(config.system.shutdown_gpio)

    modem = create_modem(config.modem)
    gnss_receiver = create_gnss_receiver(config.gnss_receiver)
    collector = Collector(modem, gnss_receiver, config.collector)
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