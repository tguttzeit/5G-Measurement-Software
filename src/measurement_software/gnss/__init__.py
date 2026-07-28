from typing import Callable

from measurement_software.core.config import GnssConfig
from measurement_software.gnss.gnss_receiver import GNSSReceiver
from measurement_software.gnss.nmea_serial import NMEASerial

_GNSS_FACTORIES: dict[str, Callable[[GnssConfig], GNSSReceiver]] = {
    "nmea_serial": lambda config: NMEASerial(config=config),
}

def create_gnss_receiver(config: GnssConfig) -> GNSSReceiver:
    """Builds the GNSSReceiver implementation configured for config.type."""
    try:
        factory = _GNSS_FACTORIES[config.type]
    except KeyError:
        raise ValueError(f"Unknown modem type: {config.type!r}")
    return factory(config)