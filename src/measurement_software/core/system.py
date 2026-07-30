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


def setup_fan_gpio(pin: int) -> None:
    """Configures the fan control pin as a low (off) output (Raspberry Pi only)."""
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)


def set_fan_state(pin: int, on: bool) -> None:
    """Drives the fan control pin high (on) or low (off)."""
    import RPi.GPIO as GPIO
    GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)


_CPU_TEMPERATURE_PATH = "/sys/class/thermal/thermal_zone0/temp"


def read_cpu_temperature_celsius() -> float:
    """Reads the current CPU temperature from sysfs, in degrees Celsius."""
    with open(_CPU_TEMPERATURE_PATH) as f:
        return int(f.read().strip()) / 1000


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