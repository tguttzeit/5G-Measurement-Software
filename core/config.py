import tomllib
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class ModemConfig:
    type: str
    port: str
    baud_rate: int
    timeout: float

@dataclass
class GnssConfig:
    type: str
    port: str
    baud_rate: int

@dataclass
class CollectorConfig:
    position_threshold: float = 15
    max_idle_time: float = 200
    max_wait_for_fix: float = 300

@dataclass
class AppConfig:
    modem: ModemConfig
    gnss_receiver: GnssConfig
    collector: CollectorConfig

def load_config(path: Path) -> AppConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return AppConfig(
        modem=ModemConfig(**raw["modem"]),
        gnss_receiver=GnssConfig(**raw["gnss"]),
        collector=CollectorConfig(**raw.get("collector", {})),
    )