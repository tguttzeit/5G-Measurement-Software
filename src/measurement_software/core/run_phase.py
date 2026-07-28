class RunPhase:
    """Mutable holder for the run's current lifecycle-stage label, for the status display to read.

    Today's flow (main.py) is linear, so this just holds whichever phase label that flow is
    currently in - callers set it at each transition. Once #7 introduces a real lifecycle
    state machine, its states become this holder's source instead: callers change what they
    set here, not how the display reads it.
    """

    def __init__(self, initial: str = "starting up"):
        self._label = initial

    def set(self, label: str) -> None:
        self._label = label

    def get(self) -> str:
        return self._label
