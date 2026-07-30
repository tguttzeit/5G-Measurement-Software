from measurement_software.displays.display import Display


class NullDisplay(Display):
    """No-op Display used when no screen is configured, so the app runs without one attached."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def show(self, message: str) -> None:
        pass
