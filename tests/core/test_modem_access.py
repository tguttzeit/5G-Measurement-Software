import threading

from measurement_software.core.modem_access import ModemAccess
from measurement_software.modems.modem import CellSample, Modem, ModemConnectionError, SimPinRejectedError


class RecordingModem(Modem):
    def __init__(self, open_error: Exception | None = None, reset_error: Exception | None = None):
        self.calls: list[str] = []
        self._open_error = open_error
        self._reset_error = reset_error

    def open(self) -> None:
        self.calls.append("open")
        if self._open_error is not None:
            raise self._open_error

    def close(self) -> None:
        self.calls.append("close")

    def unlock_sim(self) -> None:
        pass

    def query_cell_info(self) -> list[CellSample]:
        return []

    def reset_radio(self) -> None:
        self.calls.append("reset_radio")
        if self._reset_error is not None:
            raise self._reset_error

    def power_down(self) -> None:
        pass


class TestResetRadioIfIdle:
    def test_opens_resets_and_closes_the_modem_when_nobody_uses_it(self):
        modem = RecordingModem()

        assert ModemAccess(modem).reset_radio_if_idle() is True
        assert modem.calls == ["open", "reset_radio", "close"]

    def test_is_refused_while_a_claim_holds_the_modem(self):
        modem = RecordingModem()
        access = ModemAccess(modem)

        with access.claim():
            assert access.reset_radio_if_idle() is False
        assert modem.calls == ["open", "close"]

    def test_works_again_once_the_claim_ended(self):
        modem = RecordingModem()
        access = ModemAccess(modem)
        with access.claim():
            pass

        assert access.reset_radio_if_idle() is True

    def test_is_refused_while_another_reset_is_running(self):
        access = ModemAccess(RecordingModem())
        inner_results = []

        class ReentrantModem(RecordingModem):
            def reset_radio(self):
                inner_results.append(access.reset_radio_if_idle())

        access = ModemAccess(ReentrantModem())

        assert access.reset_radio_if_idle() is True
        assert inner_results == [False]

    def test_a_connection_error_is_reported_as_not_done_and_the_modem_closed(self):
        modem = RecordingModem(reset_error=ModemConnectionError("gone"))

        assert ModemAccess(modem).reset_radio_if_idle() is False
        assert modem.calls[-1] == "close"

    def test_a_failing_open_is_reported_as_not_done(self):
        modem = RecordingModem(open_error=ModemConnectionError("no port"))

        assert ModemAccess(modem).reset_radio_if_idle() is False
        assert "reset_radio" not in modem.calls

    def test_a_sim_unlock_failure_is_reported_as_not_done(self):
        modem = RecordingModem(open_error=SimPinRejectedError("wrong pin"))

        assert ModemAccess(modem).reset_radio_if_idle() is False


class TestClaim:
    def test_opens_and_closes_the_modem_around_the_block(self):
        modem = RecordingModem()

        with ModemAccess(modem).claim():
            assert modem.calls == ["open"]
        assert modem.calls == ["open", "close"]

    def test_closes_the_modem_when_the_block_raises(self):
        modem = RecordingModem()

        try:
            with ModemAccess(modem).claim():
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        assert modem.calls == ["open", "close"]

    def test_a_failing_open_leaves_the_modem_unclaimed(self):
        modem = RecordingModem(open_error=ModemConnectionError("no port"))
        access = ModemAccess(modem)

        try:
            with access.claim():
                pass
        except ModemConnectionError:
            pass
        modem._open_error = None

        assert access.reset_radio_if_idle() is True

    def test_waits_for_a_running_reset_before_opening(self):
        in_reset = threading.Event()
        release = threading.Event()
        order: list[str] = []

        class SlowModem(RecordingModem):
            def reset_radio(self):
                in_reset.set()
                release.wait(5)
                order.append("reset_done")

            def open(self):
                order.append("open")

        access = ModemAccess(SlowModem())
        resetter = threading.Thread(target=access.reset_radio_if_idle)
        resetter.start()
        assert in_reset.wait(5)

        def claim():
            with access.claim():
                order.append("claimed")

        claimer = threading.Thread(target=claim)
        claimer.start()
        release.set()
        resetter.join(5)
        claimer.join(5)

        assert order == ["open", "reset_done", "open", "claimed"]
