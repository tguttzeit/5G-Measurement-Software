from typing import Callable

from measurement_software.core.config import ModemConfig
from measurement_software.modems.modem import Modem
from measurement_software.modems.quectel import Quectel

_MODEM_FACTORIES: dict[str, Callable[[ModemConfig], Modem]] = {
    "quectel": lambda cfg: Quectel(config=cfg),
}

def create_modem(config: ModemConfig) -> Modem:
    try:
        factory = _MODEM_FACTORIES[config.type]
    except KeyError:
        raise ValueError(f"Unknown modem type: {config.type!r}")
    return factory(config)