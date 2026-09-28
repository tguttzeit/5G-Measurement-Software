from measurement_software.displays.null_display import NullDisplay


class TestNullDisplay:
    def test_open_close_and_show_do_nothing_and_never_raise(self):
        display = NullDisplay()

        display.open()
        display.show("anything")
        display.close()
