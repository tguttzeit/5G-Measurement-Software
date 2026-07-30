import logging
import sys
import types

import pytest

from measurement_software.core.config import DisplayConfig
from measurement_software.displays.ssd1306_display import SSD1306Display


class FakeSerial:
    def __init__(self, port, address):
        self.port = port
        self.address = address


class FakeDevice:
    def __init__(self, serial):
        self.serial = serial
        self.cleanup_calls = 0

    def cleanup(self):
        self.cleanup_calls += 1


class FakeDraw:
    def __init__(self):
        self.text_calls: list[tuple] = []

    def text(self, xy, text, fill=None):
        self.text_calls.append((xy, text, fill))


class FakeCanvas:
    """Fakes luma.core.render.canvas: a context manager yielding a FakeDraw for the device."""

    instances: list["FakeCanvas"] = []

    def __init__(self, device):
        self.device = device
        self.draw = FakeDraw()
        FakeCanvas.instances.append(self)

    def __enter__(self):
        return self.draw

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def fake_luma(monkeypatch):
    """Injects fake luma.core/luma.oled modules so SSD1306Display's deferred imports resolve."""
    FakeCanvas.instances.clear()

    luma_core_interface_serial = types.ModuleType("luma.core.interface.serial")
    luma_core_interface_serial.i2c = FakeSerial
    luma_core_render = types.ModuleType("luma.core.render")
    luma_core_render.canvas = FakeCanvas
    luma_oled_device = types.ModuleType("luma.oled.device")
    luma_oled_device.ssd1306 = FakeDevice

    fake_modules = {
        "luma": types.ModuleType("luma"),
        "luma.core": types.ModuleType("luma.core"),
        "luma.core.interface": types.ModuleType("luma.core.interface"),
        "luma.core.interface.serial": luma_core_interface_serial,
        "luma.core.render": luma_core_render,
        "luma.oled": types.ModuleType("luma.oled"),
        "luma.oled.device": luma_oled_device,
    }
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def make_config(**overrides) -> DisplayConfig:
    defaults = dict(enabled=True, type="ssd1306", i2c_port=1, i2c_address=0x3C, refresh_interval_s=2.0)
    defaults.update(overrides)
    return DisplayConfig(**defaults)


class TestSSD1306Display:
    def test_open_then_show_draws_on_a_device_built_from_the_configured_i2c_address(self, fake_luma):
        display = SSD1306Display(make_config(i2c_port=3, i2c_address=0x3D))
        display.open()

        display.show("hello")

        [canvas] = FakeCanvas.instances
        assert canvas.device.serial.port == 3
        assert canvas.device.serial.address == 0x3D

    def test_show_draws_each_line_of_the_message_stacked_vertically(self, fake_luma):
        display = SSD1306Display(make_config())
        display.open()

        display.show("State: collecting\nOK 1  Bad 0  Inv 0")

        [canvas] = FakeCanvas.instances
        assert canvas.draw.text_calls == [
            ((0, 0), "State: collecting", "white"),
            ((0, 10), "OK 1  Bad 0  Inv 0", "white"),
        ]

    def test_show_before_open_logs_a_warning_instead_of_raising(self, fake_luma, caplog):
        display = SSD1306Display(make_config())

        with caplog.at_level(logging.WARNING):
            display.show("too early")

        assert "before open" in caplog.text
        assert FakeCanvas.instances == []

    def test_close_cleans_up_the_device_and_show_after_close_warns_again(self, fake_luma, caplog):
        display = SSD1306Display(make_config())
        display.open()
        display.show("hello")
        [canvas] = FakeCanvas.instances
        device = canvas.device

        display.close()

        assert device.cleanup_calls == 1

        with caplog.at_level(logging.WARNING):
            display.show("after close")
        assert "before open" in caplog.text

    def test_close_before_open_is_harmless(self, fake_luma):
        SSD1306Display(make_config()).close()
