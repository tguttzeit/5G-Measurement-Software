from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class GNSSFix:
    lat: float
    lon: float
    num_satellites: int

class GNSSReceiver(ABC):
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read_fix(self) -> GNSSFix | None: ...