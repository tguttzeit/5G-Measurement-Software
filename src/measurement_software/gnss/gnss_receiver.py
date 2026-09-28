from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Position:
    """A geographic position in decimal degrees, with optional altitude in meters."""

    latitude: float
    longitude: float
    altitude: float | None = None

@dataclass
class GNSSFix:
    """A GPS fix: position plus the number of satellites used to compute it."""

    position: Position
    num_satellites: int
    # True for a dummy fix (e.g. from a GPS-disabled testing run) — position is not real data.
    placeholder: bool = False

class GNSSReceiver(ABC):
    """Interface for a GNSS receiver that can be opened, closed, and polled for fixes."""

    @abstractmethod
    def open(self) -> None:
        """Opens the connection to the receiver."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Closes the connection to the receiver."""
        ...

    @abstractmethod
    def read_fix(self) -> GNSSFix | None:
        """Returns the latest GPS fix, or None if none is currently available."""
        ...

    @abstractmethod
    def read_datetime(self) -> datetime | None:
        """Returns the latest GNSS-derived UTC date and time, or None if none is available."""
        ...

    def read_satellites_in_view(self) -> int | None:
        """Returns the latest satellites-in-view count, or None if none is currently available.

        Distinct from read_fix()'s satellite count, which counts satellites used in a computed
        fix and is therefore 0 whenever there is no fix yet. This instead reflects satellites the
        receiver is tracking regardless of fix status - what an antenna-positioning diagnostic
        needs to show progress before any fix has been achieved. Not abstract: implementations
        with no such signal (e.g. a receiver type that doesn't expose it, or a test fake) can
        simply not override this and keep reporting "unavailable".
        """
        return None