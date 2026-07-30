import logging

import pytest

from measurement_software.core.config import ModemConfig
from measurement_software.modems.modem import (
    SimPinNotConfiguredError,
    SimPinRejectedError,
    SimPukRequiredError,
    SimStatusUnknownError,
)
from measurement_software.modems.quectel import Quectel, SimStatus

SIM_PIN_ENV_VAR = "MODEM_SIM_PIN"


def make_config(**overrides) -> ModemConfig:
    defaults = dict(type="quectel", port="/dev/ttyUSB2", baud_rate=115200, timeout=1.0, mode="serving_cell")
    defaults.update(overrides)
    return ModemConfig(**defaults)


class FakeSerial:
    """Fakes pyserial's Serial, recording writes and returning scripted responses.

    `response` is returned for every read_all() call. For interactions with more than one
    write/read round trip, set `responses` instead - each read_all() pops the next entry.
    """

    def __init__(self, *args, response: bytes = b"", **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.response = response
        self.responses: list[bytes] = []
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def read_all(self) -> bytes:
        if self.responses:
            return self.responses.pop(0)
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("measurement_software.modems.quectel.time.sleep", lambda seconds: None)


@pytest.fixture
def fake_serials(monkeypatch) -> list[FakeSerial]:
    """Patches serial.Serial; each call appends a new FakeSerial to the returned list."""
    created: list[FakeSerial] = []

    def factory(*args, **kwargs):
        fake = FakeSerial(*args, **kwargs)
        created.append(fake)
        return fake

    monkeypatch.setattr("measurement_software.modems.quectel.serial.Serial", factory)
    return created


class TestToInt:
    def test_decimal(self):
        assert Quectel._to_int("42") == 42

    def test_negative_decimal(self):
        assert Quectel._to_int("-5") == -5

    def test_hex_with_prefix(self):
        assert Quectel._to_int("0x1A") == 26

    def test_none_returns_none(self):
        assert Quectel._to_int(None) is None

    def test_invalid_returns_none(self):
        assert Quectel._to_int("not-a-number") is None


class TestToFloat:
    def test_valid_float(self):
        assert Quectel._to_float("-12.5") == -12.5

    def test_integer_looking_string(self):
        assert Quectel._to_float("10") == 10.0

    def test_none_returns_none(self):
        assert Quectel._to_float(None) is None

    def test_invalid_returns_none(self):
        assert Quectel._to_float("not-a-number") is None


class TestClean:
    def test_strips_quotes_and_whitespace(self):
        assert Quectel._clean(' "LTE" ') == "LTE"

    def test_plain_value_untouched(self):
        assert Quectel._clean("123") == "123"

    def test_empty_string_is_none(self):
        assert Quectel._clean("") is None

    def test_dash_is_none(self):
        assert Quectel._clean("-") is None

    def test_none_passes_through(self):
        assert Quectel._clean(None) is None


class TestParseLte:
    PARTS = [
        "LTE", "FDD", "262", "1", "12345678", "205", "1650", "3", "50", "50",
        "8721", "-95", "-10", "-70", "15", "10", "23", "3",
    ]

    def test_maps_fields_by_index(self):
        sample = Quectel._parse_lte(self.PARTS)

        assert sample.rat == "LTE"
        assert sample.mcc == 262
        assert sample.mnc == 1
        assert sample.cell_id == 12345678
        assert sample.pci == 205
        assert sample.channel == 1650
        assert sample.band == 3
        assert sample.tac == 8721
        assert sample.rsrp == -95.0
        assert sample.rsrq == -10.0
        assert sample.rssi == -70.0
        assert sample.sinr == 15.0
        assert sample.vendor_specific_extras == {
            "duplex_mode": "FDD", "cqi": 10, "tx_power": 23, "srxlev": 3,
        }

    def test_missing_optional_value_becomes_none(self):
        parts = list(self.PARTS)
        parts[11] = None  # rsrp cleaned to None (e.g. reported as "-")

        sample = Quectel._parse_lte(parts)

        assert sample.rsrp is None


class TestParseNr5g:
    PARTS = ["NR5G-SA", "460", "11", "8901", "-88", "18", "-9", "632448", "78", "1"]

    def test_maps_fields_by_index(self):
        sample = Quectel._parse_nr5g(self.PARTS, "NR5G-SA")

        assert sample.rat == "NR5G-SA"
        assert sample.mcc == 460
        assert sample.mnc == 11
        assert sample.tac == 8901
        assert sample.rsrp == -88.0
        assert sample.sinr == 18.0
        assert sample.rsrq == -9.0
        assert sample.channel == 632448
        assert sample.band == 78
        assert sample.vendor_specific_extras == {"scs_index": 1}

    def test_nsa_rat_label_is_preserved(self):
        sample = Quectel._parse_nr5g(self.PARTS, "NR5G-NSA")
        assert sample.rat == "NR5G-NSA"


class TestParseQengResponse:
    def _modem(self) -> Quectel:
        return Quectel(make_config())

    def test_parses_lte_line_and_skips_header_echo(self):
        response = (
            '+QENG: "servingcell"\n'
            '+QENG: "LTE","FDD",262,1,12345678,205,1650,3,50,50,8721,-95,-10,-70,15,10,23,3\n'
            "\nOK\n"
        )

        results = self._modem()._parse_qeng_response(response)

        assert len(results) == 1
        assert results[0].rat == "LTE"
        assert results[0].cell_id == 12345678

    def test_parses_multiple_rats(self):
        response = (
            '+QENG: "servingcell"\n'
            '+QENG: "LTE","FDD",262,1,12345678,205,1650,3,50,50,8721,-95,-10,-70,15,10,23,3\n'
            '+QENG: "NR5G-NSA",460,0,1234,-92,12,-11,500000,41,0\n'
        )

        results = self._modem()._parse_qeng_response(response)

        assert [r.rat for r in results] == ["LTE", "NR5G-NSA"]

    def test_unknown_rat_is_skipped(self):
        response = '+QENG: "WCDMA",1,2,3,4,5,6,7,8,9\n'

        results = self._modem()._parse_qeng_response(response)

        assert results == []

    def test_non_qeng_lines_are_ignored(self):
        response = 'AT+QENG="servingcell"\nsome other text\n'

        results = self._modem()._parse_qeng_response(response)

        assert results == []

    def test_no_results_logs_warning(self, caplog):
        caplog.set_level(logging.WARNING)

        results = self._modem()._parse_qeng_response('+QENG: "servingcell"\n')

        assert results == []
        assert "No QENG measurements found" in caplog.text


class TestParseQscanResponse:
    def _modem(self) -> Quectel:
        return Quectel(make_config(mode="sa_scan"))

    def test_parses_nr5g_sa_line(self):
        response = '+QSCAN: "NR5G-SA",460,11,8901,205,1,632448,78,1,-88,-9,18\n'

        results = self._modem()._parse_qscan_response(response)

        assert len(results) == 1
        sample = results[0]
        assert sample.rat == "NR5G-SA"
        assert sample.mcc == 460
        assert sample.mnc == 11
        assert sample.tac == 8901
        assert sample.pci == 205
        assert sample.channel == 632448
        assert sample.band == 78
        assert sample.rsrp == -88.0
        assert sample.rsrq == -9.0
        assert sample.sinr == 18.0

    def test_non_nr5g_sa_line_is_skipped(self):
        response = '+QSCAN: "LTE",262,1,8721,205,1,1650,3,1,-95,-10,15\n'

        results = self._modem()._parse_qscan_response(response)

        assert results == []

    def test_short_line_is_skipped_and_logged(self, caplog):
        caplog.set_level(logging.WARNING)
        response = '+QSCAN: "NR5G-SA",460,11\n'  # too few fields -> IndexError internally

        results = self._modem()._parse_qscan_response(response)

        assert results == []
        assert "Error parsing QSCAN line" in caplog.text

    def test_no_results_logs_warning(self, caplog):
        caplog.set_level(logging.WARNING)

        results = self._modem()._parse_qscan_response("\n")

        assert results == []
        assert "No NR5G-SA cells found via QSCAN" in caplog.text


class TestConnectionLifecycle:
    def test_query_before_open_raises(self):
        modem = Quectel(make_config())
        with pytest.raises(RuntimeError, match="not open"):
            modem.query_cell_info()

    def test_open_constructs_serial_with_configured_parameters(self, fake_serials):
        modem = Quectel(make_config(port="/dev/ttyUSB5", baud_rate=9600, timeout=2.5))
        modem.open()

        assert len(fake_serials) == 1
        assert fake_serials[0].args == ("/dev/ttyUSB5", 9600)
        assert fake_serials[0].kwargs == {"timeout": 2.5}

    def test_close_closes_connection_and_blocks_further_use(self, fake_serials):
        modem = Quectel(make_config())
        modem.open()
        modem.close()

        assert fake_serials[0].closed is True
        with pytest.raises(RuntimeError, match="not open"):
            modem.query_cell_info()

    def test_close_without_open_is_a_noop(self):
        modem = Quectel(make_config())
        modem.close()  # must not raise


class TestQueryCellInfo:
    def test_serving_cell_mode_sends_qeng_command(self, fake_serials):
        modem = Quectel(make_config(mode="serving_cell"))
        modem.open()
        fake_serials[0].response = (
            '+QENG: "LTE","FDD",262,1,12345678,205,1650,3,50,50,8721,-95,-10,-70,15,10,23,3\n'
        ).encode()

        samples = modem.query_cell_info()

        assert fake_serials[0].written == [b'AT+QENG="servingcell"\r']
        assert [s.rat for s in samples] == ["LTE"]

    def test_sa_scan_mode_sends_qscan_command(self, fake_serials):
        modem = Quectel(make_config(mode="sa_scan"))
        modem.open()
        fake_serials[0].response = '+QSCAN: "NR5G-SA",460,11,8901,205,1,632448,78,1,-88,-9,18\n'.encode()

        samples = modem.query_cell_info()

        assert fake_serials[0].written == [b"AT+QSCAN=1,1\r"]
        assert [s.rat for s in samples] == ["NR5G-SA"]


class TestParseCpinResponse:
    def test_ready(self):
        assert Quectel._parse_cpin_response("+CPIN: READY\n\nOK\n") == SimStatus.READY

    def test_sim_pin_locked(self):
        assert Quectel._parse_cpin_response("+CPIN: SIM PIN\n\nOK\n") == SimStatus.SIM_PIN

    def test_sim_puk_locked(self):
        assert Quectel._parse_cpin_response("+CPIN: SIM PUK\n\nOK\n") == SimStatus.SIM_PUK

    def test_unrecognized_response_is_unknown(self):
        assert Quectel._parse_cpin_response("garbled\n") == SimStatus.UNKNOWN

    def test_empty_response_is_unknown(self):
        assert Quectel._parse_cpin_response("") == SimStatus.UNKNOWN


class TestQuerySimStatus:
    def test_sends_cpin_query_and_parses_response(self, fake_serials):
        modem = Quectel(make_config())
        modem.open()
        fake_serials[0].response = b"+CPIN: READY\n\nOK\n"

        status = modem._query_sim_status()

        assert fake_serials[0].written == [b"AT+CPIN?\r"]
        assert status == SimStatus.READY


class TestUnlockSim:
    def _open_modem(self, fake_serials) -> Quectel:
        modem = Quectel(make_config())
        modem.open()
        return modem

    def test_already_unlocked_is_a_noop(self, fake_serials, monkeypatch):
        monkeypatch.delenv(SIM_PIN_ENV_VAR, raising=False)
        modem = self._open_modem(fake_serials)
        fake_serials[0].response = b"+CPIN: READY\n\nOK\n"

        modem.unlock_sim()

        assert fake_serials[0].written == [b"AT+CPIN?\r"]

    def test_puk_required_raises_and_never_sends_a_pin(self, fake_serials, monkeypatch):
        monkeypatch.setenv(SIM_PIN_ENV_VAR, "1234")
        modem = self._open_modem(fake_serials)
        fake_serials[0].response = b"+CPIN: SIM PUK\n\nOK\n"

        with pytest.raises(SimPukRequiredError):
            modem.unlock_sim()

        assert fake_serials[0].written == [b"AT+CPIN?\r"]

    def test_unrecognized_status_raises(self, fake_serials):
        modem = self._open_modem(fake_serials)
        fake_serials[0].response = b"garbled\n"

        with pytest.raises(SimStatusUnknownError):
            modem.unlock_sim()

    def test_pin_not_configured_raises_and_does_not_send_a_pin(self, fake_serials, monkeypatch):
        monkeypatch.delenv(SIM_PIN_ENV_VAR, raising=False)
        modem = self._open_modem(fake_serials)
        fake_serials[0].response = b"+CPIN: SIM PIN\n\nOK\n"

        with pytest.raises(SimPinNotConfiguredError):
            modem.unlock_sim()

        assert fake_serials[0].written == [b"AT+CPIN?\r"]

    def test_correct_pin_unlocks(self, fake_serials, monkeypatch):
        monkeypatch.setenv(SIM_PIN_ENV_VAR, "1234")
        modem = self._open_modem(fake_serials)
        fake_serials[0].responses = [
            b"+CPIN: SIM PIN\n\nOK\n",
            b"OK\n",
            b"+CPIN: READY\n\nOK\n",
        ]

        modem.unlock_sim()

        assert fake_serials[0].written == [b"AT+CPIN?\r", b'AT+CPIN="1234"\r', b"AT+CPIN?\r"]

    def test_wrong_pin_is_rejected_and_never_retried(self, fake_serials, monkeypatch):
        monkeypatch.setenv(SIM_PIN_ENV_VAR, "0000")
        modem = self._open_modem(fake_serials)
        fake_serials[0].responses = [
            b"+CPIN: SIM PIN\n\nOK\n",
            b"+CME ERROR: 16\n",
        ]

        with pytest.raises(SimPinRejectedError):
            modem.unlock_sim()

        assert fake_serials[0].written == [b"AT+CPIN?\r", b'AT+CPIN="0000"\r']

    def test_ambiguous_response_after_sending_pin_raises(self, fake_serials, monkeypatch):
        monkeypatch.setenv(SIM_PIN_ENV_VAR, "1234")
        modem = self._open_modem(fake_serials)
        fake_serials[0].responses = [
            b"+CPIN: SIM PIN\n\nOK\n",
            b"",
        ]

        with pytest.raises(SimStatusUnknownError):
            modem.unlock_sim()

        assert fake_serials[0].written == [b"AT+CPIN?\r", b'AT+CPIN="1234"\r']

    def test_never_reaching_ready_raises_without_resending_the_pin(self, fake_serials, monkeypatch):
        monkeypatch.setenv(SIM_PIN_ENV_VAR, "1234")
        poll_attempts = 3
        modem = Quectel(make_config(sim_unlock_poll_attempts=poll_attempts))
        modem.open()
        fake_serials[0].responses = [
            b"+CPIN: SIM PIN\n\nOK\n",
            b"OK\n",
        ] + [b"+CPIN: SIM PIN\n\nOK\n"] * poll_attempts

        with pytest.raises(SimStatusUnknownError):
            modem.unlock_sim()

        pin_sends = [w for w in fake_serials[0].written if w.startswith(b'AT+CPIN="')]
        assert pin_sends == [b'AT+CPIN="1234"\r']

    def test_poll_attempts_are_configurable(self, fake_serials, monkeypatch):
        monkeypatch.setenv(SIM_PIN_ENV_VAR, "1234")
        modem = Quectel(make_config(sim_unlock_poll_attempts=2))
        modem.open()
        fake_serials[0].responses = [
            b"+CPIN: SIM PIN\n\nOK\n",
            b"OK\n",
            b"+CPIN: SIM PIN\n\nOK\n",
            b"+CPIN: READY\n\nOK\n",
        ]

        modem.unlock_sim()

        status_queries = [w for w in fake_serials[0].written if w == b"AT+CPIN?\r"]
        assert len(status_queries) == 3  # initial check + 2 polls

    def test_pin_env_var_name_is_configurable(self, fake_serials, monkeypatch):
        monkeypatch.setenv("ALTERNATE_SIM_PIN_VAR", "5678")
        modem = Quectel(make_config(sim_pin_env_var="ALTERNATE_SIM_PIN_VAR"))
        modem.open()
        fake_serials[0].responses = [
            b"+CPIN: SIM PIN\n\nOK\n",
            b"OK\n",
            b"+CPIN: READY\n\nOK\n",
        ]

        modem.unlock_sim()

        assert b'AT+CPIN="5678"\r' in fake_serials[0].written

    def test_poll_interval_is_configurable(self, fake_serials, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("measurement_software.modems.quectel.time.sleep", sleeps.append)
        monkeypatch.setenv(SIM_PIN_ENV_VAR, "1234")
        modem = Quectel(make_config(sim_unlock_poll_interval=2.5))
        modem.open()
        fake_serials[0].responses = [
            b"+CPIN: SIM PIN\n\nOK\n",
            b"OK\n",
            b"+CPIN: SIM PIN\n\nOK\n",
            b"+CPIN: READY\n\nOK\n",
        ]

        modem.unlock_sim()

        assert 2.5 in sleeps


class TestPowerDown:
    def test_sends_power_down_command(self, fake_serials):
        modem = Quectel(make_config())
        modem.open()

        modem.power_down()

        assert fake_serials[0].written == [b"AT+QPOWD=1\r"]

    def test_raises_when_not_open(self):
        modem = Quectel(make_config())
        with pytest.raises(RuntimeError, match="not open"):
            modem.power_down()


def test_invalid_mode_rejected_at_construction():
    with pytest.raises(ValueError):
        Quectel(make_config(mode="not-a-real-mode"))
