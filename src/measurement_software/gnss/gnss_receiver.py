from abc import ABC, abstractmethod
from dataclasses import dataclass


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