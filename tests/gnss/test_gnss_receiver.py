from datetime import datetime

from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, updated_gnss_diagnostic


class MinimalGNSSReceiver(GNSSReceiver):
    """Implements only the abstract methods, to exercise the non-abstract defaults untouched."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read_fix(self) -> GNSSFix | None:
        return None

    def read_datetime(self) -> datetime | None:
        return None


class TestReadGnssDiagnosticDefault:
    def test_returns_none_when_not_overridden(self):
        assert MinimalGNSSReceiver().read_gnss_diagnostic() is None


class TestUpdatedGnssDiagnostic:
    def test_maps_antenna_short_to_a_friendly_message(self):
        assert updated_gnss_diagnostic(None, "ANTENNA SHORT") == "Antenna short circuit"

    def test_maps_antenna_open_to_a_friendly_message(self):
        assert updated_gnss_diagnostic(None, "ANTENNA OPEN") == "Antenna open circuit"

    def test_antenna_ok_is_healthy_and_clears_a_previous_fault(self):
        # Sent every second by the receiver in the box - it used to show up as an unknown error.
        assert updated_gnss_diagnostic("Antenna short circuit", "ANTENNA OK") is None

    def test_falls_back_to_the_generic_message_for_an_unknown_antenna_status(self):
        assert updated_gnss_diagnostic(None, "ANTENNA FUTURE CODE") == "Unknown antenna/GNSS diagnostic error"

    def test_never_returns_the_raw_text_itself_for_an_unknown_message(self):
        raw = "ANTENNA FUTURE CODE"
        assert raw not in updated_gnss_diagnostic(None, raw)

    def test_text_unrelated_to_the_antenna_leaves_the_current_state_alone(self):
        assert updated_gnss_diagnostic(None, "u-blox AG - www.u-blox.com") is None
        assert updated_gnss_diagnostic("Antenna short circuit", "HW UBX-M8030") == "Antenna short circuit"
