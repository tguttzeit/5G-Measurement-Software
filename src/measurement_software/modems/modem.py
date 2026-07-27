from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class CellSample:
    """A single cell measurement reported by the modem for one radio access technology."""

    rat: str            # "LTE", "NR5G-NSA", "NR5G-SA"
    mcc: int | None = None
    mnc: int | None = None
    cell_id: int | None = None
    pci: int | None = None
    tac: int | None = None
    rsrp: float | None = None
    rsrq: float | None = None
    rssi: float | None = None
    sinr: float | None = None
    band: int | None = None
    channel: int | None = None
    vendor_specific_extras: dict | None = None


class Modem(ABC):
    """Interface for a cellular modem that can be opened, closed, queried, and powered down."""

    @abstractmethod
    def open(self) -> None:
        """Opens the connection to the modem."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Closes the connection to the modem."""
        ...

    @abstractmethod
    def query_cell_info(self) -> list[CellSample]:
        """Returns the modem's current cell measurements."""
        ...

    @abstractmethod
    def power_down(self) -> None:
        """Sends the modem's power-down command."""
        ...
