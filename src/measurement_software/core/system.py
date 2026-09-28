import logging
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from measurement_software.core.config import AppConfig, FanConfig, ModemConfig, QuectelCmConfig
from measurement_software.core.run_log import discard_stale_temp_files, recover_unfinalized
from measurement_software.core.uploader import Uploader
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


def start_quectel_cm(config: QuectelCmConfig) -> None:
    """Starts the quectel-CM connection-manager daemon in the background, via sudo.

    Fire-and-forget: quectel-CM runs for the rest of the measuring session managing the
    cellular link, so this does not wait for it to exit.
    """
    logger.info("Starting quectel-CM")
    subprocess.Popen(["sudo", config.binary, *config.args])


def set_system_clock(dt: datetime) -> None:
    """Sets the system clock to the given UTC datetime, via sudo (needs root).

    Not checked for success - same as perform_shutdown()'s own sudo calls - since this project has
    no fallback if it fails.
    """
    formatted = dt.strftime("%Y-%m-%d %H:%M:%S")
    logger.info("Setting system clock to %s UTC (from GNSS)", formatted)
    subprocess.run(["sudo", "date", "-u", "-s", formatted])


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


def shutdown(config: AppConfig, reason: str) -> None:
    """Runs the one shutdown sequence every shutdown trigger uses, regardless of why.

    Finalizes and uploads whatever data exists, logs why, then powers down the modem, signals
    completion, and shuts the system down. The sequence never branches on `reason` - it only
    changes what gets logged.
    """
    logger.critical("Shutting down (%s)", reason)

    upload_dir = Path(config.uploader.upload_dir)
    discard_stale_temp_files(upload_dir)
    recover_unfinalized(upload_dir)
    Uploader(config.uploader, config.backend).upload_pending_files()

    if config.system.running_on_pi:
        signal_completion(config.system.shutdown_gpio)
    perform_shutdown(config.modem)
    if config.system.running_on_pi:
        cleanup_gpio()


class FanController:
    """Drives a case fan on/off via GPIO based on CPU temperature, with two-threshold hysteresis.

    Runs on its own background thread for the whole process lifetime, independent of
    Collector's measurement state — the CPU can overheat regardless of whether a measurement
    session is actively running or idle.
    """

    def __init__(self, config: FanConfig):
        self._logger = logging.getLogger(__name__)
        self._config = config
        self._fan_on = False
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._override_lock = threading.Lock()
        self._override: bool | None = None

    def start(self) -> None:
        """Starts the background fan-control thread, unless fan control is disabled."""
        if not self._config.enabled:
            self._logger.info("Fan control is disabled.")
            return
        setup_fan_gpio(self._config.gpio_pin)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the fan-control thread to stop and waits for it to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def override(self, on: bool) -> None:
        """Forces the fan on/off, overriding automatic hysteresis until reboot or a new override.

        Thread-safe: called from the heartbeat thread on a remote fan-override-now command,
        while the poll loop itself runs on this controller's own thread. Ignored (with a
        warning) when fan control is disabled locally, since the GPIO pin was never set up as an
        output in that case - there is nothing safe to drive.
        """
        if not self._config.enabled:
            self._logger.warning("Fan control is disabled locally - ignoring fan override.")
            return
        with self._override_lock:
            self._override = on
        self._logger.warning(
            "Fan override: forcing %s until reboot or a new override.", "on" if on else "off"
        )

    def release_override(self) -> None:
        """Releases a standing override, if any, handing control back to automatic hysteresis.

        Thread-safe, same locking pattern as `override()`. A no-op if no override is currently
        set - fan control was already running under hysteresis.
        """
        with self._override_lock:
            self._override = None
        self._logger.info("Fan override released - resuming automatic hysteresis control.")

    def current_mode(self) -> str:
        """Returns the fan's current control mode: "auto" (hysteresis), "on", or "off"."""
        with self._override_lock:
            override = self._override
        if override is None:
            return "auto"
        return "on" if override else "off"

    def _run(self, stop_event: threading.Event) -> None:
        """Applies hysteresis on the configured poll interval until stopped."""
        while True:
            self._update()
            if stop_event.wait(self._config.poll_interval_s):
                return

    def _update(self) -> None:
        """Applies a standing override if one is set, otherwise reads the CPU temperature and
        toggles the fan if a threshold is crossed."""
        with self._override_lock:
            override = self._override
        if override is not None:
            if override != self._fan_on:
                self._set_fan(override)
            return

        try:
            temperature = read_cpu_temperature_celsius()
        except OSError as e:
            self._logger.warning("Could not read CPU temperature: %s", e)
            return

        if not self._fan_on and temperature >= self._config.temp_on_celsius:
            self._set_fan(True)
        elif self._fan_on and temperature <= self._config.temp_off_celsius:
            self._set_fan(False)

    def _set_fan(self, on: bool) -> None:
        self._fan_on = on
        set_fan_state(self._config.gpio_pin, on)
        self._logger.info("Fan turned %s (CPU temperature threshold crossed)", "on" if on else "off")


_MIN_CLOCK_SYNC_YEAR = 2020
_MAX_CLOCK_SYNC_YEAR = 2100


class ClockSync:
    """Sets the system clock from GNSS-derived UTC time, exactly once per boot.

    GPS-derived time is the only clock source this app trusts - a Pi without a battery-backed RTC
    can boot with a wrong date, not just a wrong time-of-day. Rejects an implausible date/time
    (e.g. a receiver's power-on default before it has a real time lock, such as the GPS epoch
    start of 1980-01-06) so nothing unvalidated ever reaches the shell.
    """

    def __init__(self):
        self._logger = logging.getLogger(__name__)
        self._synced = False

    @property
    def synced(self) -> bool:
        return self._synced

    def try_sync(self, gnss_datetime: datetime) -> None:
        """Sets the system clock from the given GNSS date/time, unless already synced or implausible."""
        if self._synced:
            return
        if not (_MIN_CLOCK_SYNC_YEAR <= gnss_datetime.year <= _MAX_CLOCK_SYNC_YEAR):
            self._logger.warning("Rejecting implausible GNSS date/time: %s", gnss_datetime)
            return

        set_system_clock(gnss_datetime)
        self._synced = True


class KeepModemAliveSender:
    """Sends a periodic UDP heartbeat on a background thread to keep the modem's link alive."""

    def __init__(self, host: str, port: int, interval_s: float):
        self._logger = logging.getLogger(__name__)
        self._host = host
        self._port = port
        self._interval_s = interval_s
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Starts the background heartbeat thread."""
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the heartbeat thread to stop and waits for it to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self, stop_event: threading.Event) -> None:
        """Sends a small UDP packet every interval_s seconds."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            pkt = b"\x00"  # 1-byte payload
            next_timestamp = time.time()
            while not stop_event.is_set():
                now = time.time()
                if now >= next_timestamp:
                    try:
                        s.sendto(pkt, (self._host, self._port))
                    except OSError:
                        pass  # network temporarily unavailable
                    next_timestamp = now + self._interval_s
                time.sleep(0.2)
