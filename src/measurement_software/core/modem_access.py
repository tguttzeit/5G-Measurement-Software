import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from measurement_software.modems.modem import Modem, ModemConnectionError, SimUnlockError

logger = logging.getLogger(__name__)


class ModemAccess:
    """The one owner of the modem's serial port, which isn't thread-safe: two threads using it at
    once corrupt each other's AT responses.

    `claim()` holds the port open for a whole collection run; while it does, nothing else touches
    it. Outside a claim, `reset_radio_if_idle()` opens the port just long enough to reset the
    radio. Both take the same lock, so they can never overlap.
    """

    def __init__(self, modem: Modem):
        self._modem = modem
        self._lock = threading.Lock()
        self._claimed = False

    @contextmanager
    def claim(self) -> Iterator[None]:
        """Opens the modem for exclusive use by the caller, closing it again afterwards.

        Waits for a radio reset in progress. Anything else asking for the modem meanwhile is
        refused rather than queued.
        """
        with self._lock:
            self._modem.open()
            self._claimed = True
        try:
            yield
        finally:
            try:
                self._modem.close()
            finally:
                self._claimed = False

    def reset_radio_if_idle(self) -> bool:
        """Resets the modem's radio and returns True - or returns False, doing nothing, if the
        modem is claimed, another reset is running, or the modem can't be reached."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._claimed:
                return False
            return self._reset_radio()
        finally:
            self._lock.release()

    def _reset_radio(self) -> bool:
        logger.warning("Resetting the modem's radio outside a collection run.")
        try:
            self._modem.open()
            try:
                self._modem.reset_radio()
            finally:
                self._modem.close()
        except (ModemConnectionError, SimUnlockError):
            logger.warning("Could not reset the modem's radio.", exc_info=True)
            return False
        return True
