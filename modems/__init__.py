from typing import Callable

from core.config import ModemConfig
from modems.modem import Modem
from modems.quectel import Quectel

_MODEM_FACTORIES: dict[str, Callable[[ModemConfig], Modem]] = {
    "quectel": lambda cfg: Quectel(port=cfg.port, baud_rate=cfg.baud_rate, timeout=cfg.timeout),
}

def create_modem(config: ModemConfig) -> Modem:
    try:
        factory = _MODEM_FACTORIES[config.type]
    except KeyError:
        raise ValueError(f"Unknown modem type: {config.type!r}")
    return factory(config)