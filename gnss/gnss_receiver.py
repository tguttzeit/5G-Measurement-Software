from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Position:
    latitude: float
    longitude: float

@dataclass
class GNSSFix:
    position: Position
    num_satellites: int

class GNSSReceiver(ABC):
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read_fix(self) -> GNSSFix | None: ...