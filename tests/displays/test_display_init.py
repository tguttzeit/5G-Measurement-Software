import pytest

from measurement_software.core.config import DisplayConfig
from measurement_software.displays import create_display
from measurement_software.displays.null_display import NullDisplay
from measurement_software.displays.ssd1306_display import SSD1306Display


def make_config(**overrides) -> DisplayConfig:
    defaults = dict(enabled=True, type="ssd1306", i2c_port=1, i2c_address=0x3C, refresh_interval_s=2.0)
    defaults.update(overrides)
    return DisplayConfig(**defaults)


def test_creates_ssd1306_for_ssd1306_type():
    display = create_display(make_config())

    assert isinstance(display, SSD1306Display)


def test_returns_null_display_when_disabled_regardless_of_type():
    display = create_display(make_config(enabled=False, type="bogus"))

    assert isinstance(display, NullDisplay)


def test_raises_for_unknown_type_when_enabled():
    with pytest.raises(ValueError, match="Unknown display type: 'bogus'"):
        create_display(make_config(enabled=True, type="bogus"))
