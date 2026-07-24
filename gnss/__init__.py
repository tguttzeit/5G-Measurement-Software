from typing import Callable

from core.config import GnssConfig
from gnss.gnss_receiver import GNSSReceiver
from gnss.nmea_serial import NMEASerial

_GNSS_FACTORIES: dict[str, Callable[[GnssConfig], GNSSReceiver]] = {
    "quectel": lambda config: NMEASerial(config=config),
}

def create_gnss_receiver(config: GnssConfig) -> GNSSReceiver:
    try:
        factory = _GNSS_FACTORIES[config.type]
    except KeyError:
        raise ValueError(f"Unknown modem type: {config.type!r}")
    return factory(config)