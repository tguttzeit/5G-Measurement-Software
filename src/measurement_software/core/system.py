import logging
import re
import subprocess
import time

from measurement_software.core.config import ModemConfig
from measurement_software.modems import create_modem

logger = logging.getLogger(__name__)


def setup_gpio(pin: int) -> None:
    """Configures the shutdown pin as a low output (Raspberry Pi only)."""
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)


def signal_completion(pin: int) -> None:
    """Drives the shutdown pin high to signal that the measurement run finished."""
    import RPi.GPIO as GPIO
    GPIO.output(pin, GPIO.HIGH)


def cleanup_gpio() -> None:
    """Releases the GPIO pins."""
    import RPi.GPIO as GPIO
    GPIO.cleanup()


def log_ip_addrs(interfaces: tuple[str, ...] = ("wlan0", "wwan0")) -> None:
    """Logs the current IPv4 address, or its absence, for each given network interface."""
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
    """Powers down the modem if possible, then shuts down the system."""
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