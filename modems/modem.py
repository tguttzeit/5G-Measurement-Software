from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class CellSample:
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
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def query_cell_info(self) -> list[CellSample]: ...

    @abstractmethod
    def power_down(self) -> None: ...