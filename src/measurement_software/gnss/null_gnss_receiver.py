from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, Position


class NullGNSSReceiver(GNSSReceiver):
    """A GNSSReceiver that never touches real hardware, for testing without GPS attached.

    Always reports the same placeholder fix, flagged via `GNSSFix.placeholder` so it can never be
    mistaken for a genuine position downstream.
    """

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read_fix(self) -> GNSSFix | None:
        return GNSSFix(position=Position(latitude=0.0, longitude=0.0), num_satellites=0, placeholder=True)
