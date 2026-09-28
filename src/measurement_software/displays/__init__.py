from typing import Callable

from measurement_software.core.config import DisplayConfig
from measurement_software.displays.display import Display
from measurement_software.displays.null_display import NullDisplay
from measurement_software.displays.ssd1306_display import SSD1306Display

_DISPLAY_FACTORIES: dict[str, Callable[[DisplayConfig], Display]] = {
    "ssd1306": lambda config: SSD1306Display(config=config),
}

def create_display(config: DisplayConfig) -> Display:
    """Builds the Display implementation configured for config.type, or a no-op if disabled."""
    if not config.enabled:
        return NullDisplay()
    try:
        factory = _DISPLAY_FACTORIES[config.type]
    except KeyError:
        raise ValueError(f"Unknown display type: {config.type!r}")
    return factory(config)
