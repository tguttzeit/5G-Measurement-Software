import logging

from measurement_software.core.config import DisplayConfig
from measurement_software.displays.display import Display


class SSD1306Display(Display):
    """Drives a physical SSD1306 OLED over I2C via the luma.oled/luma.core driver libraries.

    Those libraries are imported only inside these methods (same deferred-import pattern as
    RPi.GPIO in core/system.py), since they depend on Pi-specific I2C access and aren't
    installed on dev machines - only on a Pi with the display attached, via
    `uv pip install luma.oled`.
    """

    def __init__(self, config: DisplayConfig):
        self._logger = logging.getLogger(__name__)
        self._config = config
        self._device = None

    def open(self) -> None:
        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306

        serial = i2c(port=self._config.i2c_port, address=self._config.i2c_address)
        self._device = ssd1306(serial)

    def close(self) -> None:
        if self._device is not None:
            self._device.cleanup()
            self._device = None

    def show(self, message: str) -> None:
        from luma.core.render import canvas

        if self._device is None:
            self._logger.warning("show() called before open() - ignoring")
            return

        with canvas(self._device) as draw:
            for line_number, line in enumerate(message.splitlines()):
                draw.text((0, line_number * 10), line, fill="white")
