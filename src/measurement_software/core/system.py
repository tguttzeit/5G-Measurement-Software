import logging
import re
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from measurement_software.core.config import AppConfig, ModemConfig, QuectelCmConfig
from measurement_software.core.config_sources import PENDING_REBOOT_ACK_FILENAME, write_pending_reboot_ack
from measurement_software.core.run_log import discard_stale_temp_files, recover_unfinalized
from measurement_software.core.run_phase import RunPhase, RunPhaseState
from measurement_software.core.uploader import Uploader
from measurement_software.core.util import sync_log_files
from measurement_software.modems import create_modem
from measurement_software.remote.heartbeat import HeartbeatSender
from measurement_software.status.backend_status import RunStatusTracker, StorageStatusReporter

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


def start_quectel_cm(config: QuectelCmConfig) -> subprocess.Popen:
    """Starts the quectel-CM connection-manager daemon in the background, via sudo.

    Does not wait for it to exit: quectel-CM runs for the rest of the measuring session managing
    the cellular link. The returned process lets the link watchdog restart it.
    """
    logger.info("Starting quectel-CM")
    return subprocess.Popen(["sudo", config.binary, *config.args])


def set_system_clock(dt: datetime) -> None:
    """Sets the system clock to the given UTC datetime, via sudo (needs root).

    Not checked for success - same as perform_shutdown()'s own sudo calls - since this project has
    no fallback if it fails.
    """
    formatted = dt.strftime("%Y-%m-%d %H:%M:%S")
    logger.info("Setting system clock to %s UTC (from GNSS)", formatted)
    subprocess.run(["sudo", "date", "-u", "-s", formatted])


def _power_down_modem(modem_config: ModemConfig) -> None:
    """Powers down the modem if possible; a failure here is only logged, never raised - neither
    shutdown nor reboot has a fallback if it fails."""
    try:
        modem = create_modem(modem_config)
        modem.open()
        modem.power_down()
        modem.close()
    except Exception as e:
        logger.warning("Modem power-down failed: %s", e)


def perform_shutdown(modem_config: ModemConfig) -> None:
    """Powers down the modem if possible, then shuts down the system."""
    _power_down_modem(modem_config)
    logger.info("Starting system shutdown")
    sync_log_files()
    time.sleep(2)
    subprocess.run(["sudo", "shutdown", "-h", "now"])


def perform_reboot(modem_config: ModemConfig) -> None:
    """Powers down the modem if possible, then reboots the system."""
    _power_down_modem(modem_config)
    logger.info("Starting system reboot")
    sync_log_files()
    time.sleep(2)
    subprocess.run(["sudo", "shutdown", "-r", "now"])


def finalize_and_upload_pending(config: AppConfig) -> None:
    """Finalizes any run log left open and uploads everything pending - best effort, since a
    missing network only leaves the files for the next run."""
    upload_dir = Path(config.uploader.upload_dir)
    discard_stale_temp_files(upload_dir)
    recover_unfinalized(upload_dir)
    Uploader(config.uploader, config.backend, config.heartbeat.device_id).upload_pending_files()


def _send_terminal_heartbeat(
    config: AppConfig, phase: str, command_id: Any | None, command_name: str,
) -> None:
    """Best-effort last heartbeat before the board loses power or restarts.

    Reports `phase` (SHUTTING_DOWN/REBOOTING) so the backend sees why the device is about to go
    quiet, and - if a remote command triggered this - acks that command's id as "received": by
    the time any later heartbeat could report its actual success, shutdown-now's board is off
    and reboot-now's old process no longer exists to send one. Never raises: a network hiccup
    here must never delay or block the actual shutdown/reboot.
    """
    try:
        sender = HeartbeatSender(
            config.heartbeat, config.backend,
            RunStatusTracker(config.run_status), StorageStatusReporter(Path(config.uploader.upload_dir), config.storage),
            run_phase=RunPhase(phase),
        )
        if not sender.is_usable():
            return
        acks = [{"id": command_id, "command": command_name, "status": "received", "error": None}] \
            if command_id is not None else None
        sender.send_once(
            RunStatusTracker(config.run_status).status(),
            StorageStatusReporter(Path(config.uploader.upload_dir), config.storage).status(),
            extra_executed_commands=acks,
        )
    except Exception as e:
        logger.warning("Could not send terminal heartbeat: %s", e)


def shutdown(config: AppConfig, reason: str, command_id: Any | None = None) -> None:
    """Runs the one shutdown sequence every shutdown trigger uses, regardless of why.

    Finalizes and uploads whatever data exists, logs why, sends one last best-effort heartbeat,
    then powers down the modem, signals completion, and shuts the system down. The sequence
    never branches on `reason` - it only changes what gets logged. `command_id` is set only for
    a remote shutdown-now command, never for an unhandled exception or a failed SIM unlock.
    """
    logger.critical("Shutting down (%s)", reason)
    finalize_and_upload_pending(config)
    _send_terminal_heartbeat(config, RunPhaseState.SHUTTING_DOWN, command_id, "shutdown-now")

    if config.system.running_on_pi:
        signal_completion(config.system.shutdown_gpio)
    perform_shutdown(config.modem)
    if config.system.running_on_pi:
        cleanup_gpio()


def reboot(
    config: AppConfig, reason: str, command_id: Any | None = None, config_path: Path | None = None,
) -> None:
    """Runs the one reboot sequence every reboot trigger uses, regardless of why.

    Finalizes and uploads whatever data exists, logs why, sends one last best-effort heartbeat,
    then powers down the modem and reboots. Unlike shutdown(), never touches
    system.shutdown_gpio - that pin signals an external power-management circuit that it's safe
    to cut power, which doesn't apply here: a reboot keeps the board powered throughout.

    If `command_id` and `config_path` are both given (a remote reboot-now, not an unhandled
    exception), persists the command id for the next boot to ack as successful - see
    config_sources.py::write_pending_reboot_ack(). The "received" ack the terminal heartbeat
    just sent only confirms this process got the command, not that the reboot it's about to run
    actually works.
    """
    logger.critical("Rebooting (%s)", reason)
    finalize_and_upload_pending(config)
    _send_terminal_heartbeat(config, RunPhaseState.REBOOTING, command_id, "reboot-now")
    if command_id is not None and config_path is not None:
        write_pending_reboot_ack(config_path.parent / PENDING_REBOOT_ACK_FILENAME, command_id)
    perform_reboot(config.modem)


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
