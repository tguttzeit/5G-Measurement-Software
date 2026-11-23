import logging
import subprocess
import threading
import time
from typing import Callable

from measurement_software.core.config import LinkWatchdogConfig, QuectelCmConfig
from measurement_software.core.system import start_quectel_cm, stop_quectel_cm

logger = logging.getLogger(__name__)

LINK_UP_WAIT_S = 30.0
_LINK_UP_POLL_INTERVAL_S = 0.5


def interface_has_ipv4(interface: str) -> bool:
    """Returns True if the network interface exists and has an IPv4 address."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", interface], stderr=subprocess.DEVNULL
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return " inet " in out


class LinkWatchdog:
    """Brings a cellular link back that stays down, on a background thread.

    quectel-CM doesn't recover on its own from everything: in the field the modem deregistered
    from the network and quectel-CM only kept polling the registration state for 27 minutes,
    while a fresh start registered within seconds. So once `interface` has had no IPv4 address
    for `restart_after_s`, quectel-CM is restarted. After `radio_reset_after_restarts` restarts
    that didn't help, the next step resets the modem's radio instead, through
    `request_radio_reset` - which returns False when that isn't possible right now (see
    Collector.request_radio_reset()); quectel-CM is then restarted as usual.

    While the link stays down, the wait before each further step doubles, up to
    `max_restart_interval_s`, and the watchdog never gives up: where no link can be had at all,
    restarting every few minutes only keeps the modem busy, but the device must still come back
    on its own once there is coverage again. The next outage starts again from `restart_after_s`.

    `on_link_up`, if given, runs on this thread whenever the link is seen up after it was down or
    unseen (including right after boot) until it returns True - e.g. setting the clock, which
    needs the network. After a False it is tried again on the next link-up, not on every check.
    """

    def __init__(
        self,
        config: LinkWatchdogConfig,
        quectel_cm_config: QuectelCmConfig,
        request_radio_reset: Callable[[], bool],
        has_ipv4: Callable[[str], bool] = interface_has_ipv4,
        clock: Callable[[], float] = time.monotonic,
        on_link_up: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._config = config
        self._sleep = sleep
        self._quectel_cm_config = quectel_cm_config
        self._request_radio_reset = request_radio_reset
        self._has_ipv4 = has_ipv4
        self._clock = clock
        self._on_link_up = on_link_up
        self._link_seen_up = False
        self._process: subprocess.Popen | None = None
        self._down_since: float | None = None
        self._last_action_at: float | None = None
        self._restarts = 0
        self._steps_this_outage = 0
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self, quectel_cm_process: subprocess.Popen) -> None:
        """Starts watching the link that `quectel_cm_process` manages."""
        self._process = quectel_cm_process
        if not self._config.enabled:
            logger.info("Link watchdog is disabled.")
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def wait_for_link(self, timeout_s: float) -> bool:
        """Blocks until `interface` has an IPv4 address, for at most `timeout_s`, whether or not
        the watchdog is enabled. Returns whether the link came up.

        For whatever needs the network right after quectel-CM was started: it takes a moment to
        bring the link up, and the first attempt would fail without waiting.
        """
        interface = self._config.interface
        if self._has_ipv4(interface):
            return True
        started_at = self._clock()
        deadline = started_at + timeout_s
        while not self._has_ipv4(interface):
            now = self._clock()
            if now >= deadline:
                logger.warning("%s still has no IPv4 address after %.0f s - not waiting any longer.", interface, timeout_s)
                return False
            self._sleep(min(_LINK_UP_POLL_INTERVAL_S, deadline - now))
        logger.info("%s is up after %.0f s.", interface, self._clock() - started_at)
        return True

    @property
    def process(self) -> subprocess.Popen | None:
        """The quectel-CM process currently supervised - the one last (re)started."""
        return self._process

    def stop(self) -> None:
        """Stops watching; quectel-CM itself keeps running."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def check(self) -> None:
        """Checks the link once and takes the next recovery step if it's due."""
        now = self._clock()
        interface = self._config.interface
        if self._has_ipv4(interface):
            if self._down_since is not None:
                logger.info("%s link is up again after %.0f s.", interface, now - self._down_since)
            self._down_since = None
            self._last_action_at = None
            self._restarts = 0
            self._steps_this_outage = 0
            self._handle_link_up()
            return

        self._link_seen_up = False
        if self._down_since is None:
            self._down_since = now
            logger.warning("%s has no IPv4 address - cellular link down.", interface)
            return

        last_step_at = self._last_action_at if self._last_action_at is not None else self._down_since
        if now - last_step_at < self._interval_before_next_step():
            return
        self._last_action_at = now
        self._steps_this_outage += 1
        down_for = now - self._down_since

        if self._restarts >= self._config.radio_reset_after_restarts:
            if self._request_radio_reset():
                logger.warning(
                    "%s still down after %d quectel-CM restart(s) (%.0f s) - resetting the modem's radio.",
                    interface, self._restarts, down_for,
                )
                self._restarts = 0
                return
            logger.warning("%s still down (%.0f s), but a radio reset isn't possible right now.", interface, down_for)

        self._restarts += 1
        logger.warning("%s down for %.0f s - restarting quectel-CM (restart %d).", interface, down_for, self._restarts)
        self._restart_quectel_cm()

    def _interval_before_next_step(self) -> float:
        doubled = self._config.restart_after_s * 2 ** self._steps_this_outage
        return min(doubled, self._config.max_restart_interval_s)

    def _handle_link_up(self) -> None:
        if self._link_seen_up:
            return
        self._link_seen_up = True
        if self._on_link_up is not None and self._on_link_up():
            self._on_link_up = None

    def _restart_quectel_cm(self) -> None:
        if self._process is not None:
            stop_quectel_cm(self._process)
        self._process = start_quectel_cm(self._quectel_cm_config)

    def _run(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(self._config.check_interval_s):
            try:
                self.check()
            except Exception:
                logger.exception("Link watchdog check failed - trying again next interval.")
