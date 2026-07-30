from abc import ABC, abstractmethod


class Display(ABC):
    """Interface for a small field-status display that can be opened, closed, and shown text on."""

    @abstractmethod
    def open(self) -> None:
        """Opens the connection to the display."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Closes the connection to the display."""
        ...

    @abstractmethod
    def show(self, message: str) -> None:
        """Renders the given text as the display's current content."""
        ...
