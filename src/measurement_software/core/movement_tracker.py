import threading
import time
from dataclasses import dataclass

from measurement_software.gnss.gnss_receiver import GNSSFix


@dataclass(frozen=True)
class Movement:
    """Where the vehicle was when it last moved, and when that was."""

    fix: GNSSFix
    at: float


class MovementTracker:
    """The collection loop's record of the vehicle's last movement, readable from other threads.

    Anything that needs to know where the vehicle is has to read it from here rather than from
    the GNSS receiver: the collection loop owns that serial port, and a second reader would be
    taking sentences away from it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._latest: Movement | None = None

    def record_movement(self, fix: GNSSFix) -> None:
        """Records that the vehicle has just moved, and where it moved to."""
        with self._lock:
            self._latest = Movement(fix=fix, at=time.time())

    def latest(self) -> Movement | None:
        """The most recent movement, or None if the vehicle has not moved yet this run."""
        with self._lock:
            return self._latest

    def moved_within(self, window_s: float) -> Movement | None:
        """The most recent movement if it happened within the window, or None if the vehicle sits still.

        The window is what turns a series of discrete movements into a "currently moving"
        answer: the collection loop only reports movement when the vehicle clears its distance
        threshold, so between two such reports it is moving without saying so.
        """
        movement = self.latest()
        if movement is None or (time.time() - movement.at) > window_s:
            return None
        return movement
