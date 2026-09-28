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

    def read_gnss_diagnostic(self) -> str | None:
        """Returns the latest raw diagnostic/status text, or None if none is currently available.

        Covers free-text diagnostic sentences (e.g. NMEA `$GxTXT`) that explain a hardware fault
        - an antenna short, for instance - independent of fix/satellite status, rather than a
        measurement. Raw and receiver/vendor-specific: pass it through updated_gnss_diagnostic()
        before showing or transmitting it anywhere, never the raw text itself. Not abstract, same
        reasoning as read_satellites_in_view(): implementations with no such signal simply don't
        override this and keep reporting "unavailable".
        """
        return None


# Raw antenna status text confirmed seen in the field, mapped to an operator-friendly message.
# Expected to grow as new raw messages get confirmed; adding one is a table entry, not a new code
# path. "ANTENNA OK" is the healthy status - the receiver in the box sends it every second.
_HEALTHY_ANTENNA_STATUSES = frozenset({"ANTENNA OK"})
_KNOWN_DIAGNOSTIC_MESSAGES: dict[str, str] = {
    "ANTENNA SHORT": "Antenna short circuit",
    "ANTENNA OPEN": "Antenna open circuit",
}
_UNKNOWN_DIAGNOSTIC_MESSAGE = "Unknown antenna/GNSS diagnostic error"
_ANTENNA_STATUS_PREFIX = "ANTENNA"


def updated_gnss_diagnostic(current: str | None, raw: str) -> str | None:
    """Returns the operator-friendly diagnostic to show after reading the raw text `raw`.

    A healthy antenna status clears `current` (None); a known fault maps to its message, and any
    other antenna status to a fixed generic message - never the raw text itself, which has no
    length guarantee. Text that isn't an antenna status at all (e.g. a firmware banner some
    receivers send at power-on) leaves `current` as it is.
    """
    if not raw.startswith(_ANTENNA_STATUS_PREFIX):
        return current
    if raw in _HEALTHY_ANTENNA_STATUSES:
        return None
    return _KNOWN_DIAGNOSTIC_MESSAGES.get(raw, _UNKNOWN_DIAGNOSTIC_MESSAGE)
