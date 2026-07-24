import logging
import re
import subprocess
import time

from core.config import ModemConfig
from modems import create_modem

logger = logging.getLogger(__name__)


def setup_gpio(pin: int) -> None:
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)


def signal_completion(pin: int) -> None:
    import RPi.GPIO as GPIO
    GPIO.output(pin, GPIO.HIGH)


def cleanup_gpio() -> None:
    import RPi.GPIO as GPIO
    GPIO.cleanup()


def log_ip_addrs(interfaces: tuple[str, ...] = ("wlan0", "wwan0")) -> None:
    for iface in interfaces:
        try:
            out = subprocess.check_output(
                ["ip", "-4", "addr", "show", iface], stderr=subprocess.DEVNULL
            ).decode()
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", out)
            if match:
                logger.info("%s: %s", iface, match.group(1))
            else:
                logger.info("%s: no IPv4 address", iface)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning("%s: error querying IP address: %s", iface, e)


def perform_shutdown(modem_config: ModemConfig) -> None:
    try:
        modem = create_modem(modem_config)
        modem.open()
        modem.power_down()
        modem.close()
    except Exception as e:
        logger.warning("Modem power-down failed: %s", e)

    logger.info("Starting system shutdown")
    time.sleep(2)
    subprocess.run(["sudo", "shutdown", "-h", "now"])