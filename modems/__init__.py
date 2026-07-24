from typing import Callable

from core.config import ModemConfig
from modems.modem import Modem
from modems.quectel import Quectel

_MODEM_FACTORIES: dict[str, Callable[[ModemConfig], Modem]] = {
    "quectel": lambda config: Quectel(config=config),
}

def create_modem(config: ModemConfig) -> Modem:
    try:
        factory = _MODEM_FACTORIES[config.type]
    except KeyError:
        raise ValueError(f"Unknown modem type: {config.type!r}")
    return factory(config)