from measurement_software.gnss.null_gnss_receiver import NullGNSSReceiver


class TestNullGNSSReceiver:
    def test_open_and_close_are_no_ops(self):
        receiver = NullGNSSReceiver()

        receiver.open()
        receiver.close()

    def test_read_fix_returns_a_flagged_placeholder(self):
        receiver = NullGNSSReceiver()

        fix = receiver.read_fix()

        assert fix is not None
        assert fix.placeholder is True

    def test_read_fix_is_consistent_across_calls(self):
        receiver = NullGNSSReceiver()

        first = receiver.read_fix()
        second = receiver.read_fix()

        assert first == second

    def test_read_datetime_returns_none(self):
        receiver = NullGNSSReceiver()

        assert receiver.read_datetime() is None
