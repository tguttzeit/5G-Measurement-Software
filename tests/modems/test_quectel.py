import logging

import pytest
import serial

from measurement_software.core.config import ModemConfig
from measurement_software.modems.quectel import Quectel


def make_config(**overrides) -> ModemConfig:
    defaults = dict(type="quectel", port="/dev/ttyUSB2", baud_rate=115200, timeout=1.0, mode="serving_cell")
    defaults.update(overrides)
    return ModemConfig(**defaults)


class FakeSerial:
    """Fakes pyserial's Serial, recording writes and returning a scripted response.

    `read_all_sequence`, if set, scripts successive `read_all()` calls (an entry that is an
    Exception is raised instead of returned) — otherwise every call just returns `response`.
    """

    def __init__(self, *args, response: bytes = b"", **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.response = response
        self.read_all_sequence: list[bytes | Exception] | None = None
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def read_all(self) -> bytes:
        if self.read_all_sequence is not None:
            item = self.read_all_sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("measurement_software.modems.quectel.time.sleep", lambda seconds: None)
    monkeypatch.setattr("measurement_software.core.serial_retry.time.sleep", lambda seconds: None)


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


class TestQueryCellInfoRetry:
    def test_recovers_from_a_transient_empty_read(self, fake_serials):
        modem = Quectel(make_config(mode="serving_cell", retries=3))
        modem.open()
        fake_serials[0].read_all_sequence = [
            b"",
            '+QENG: "LTE","FDD",262,1,12345678,205,1650,3,50,50,8721,-95,-10,-70,15,10,23,3\n'.encode(),
        ]

        samples = modem.query_cell_info()

        assert [s.rat for s in samples] == ["LTE"]

    def test_recovers_from_a_transient_timeout_exception(self, fake_serials):
        modem = Quectel(make_config(mode="serving_cell", retries=3))
        modem.open()
        fake_serials[0].read_all_sequence = [
            serial.SerialTimeoutException("write timed out"),
            '+QENG: "LTE","FDD",262,1,12345678,205,1650,3,50,50,8721,-95,-10,-70,15,10,23,3\n'.encode(),
        ]

        samples = modem.query_cell_info()

        assert [s.rat for s in samples] == ["LTE"]

    def test_device_gone_propagates_immediately_without_exhausting_retries(self, fake_serials):
        modem = Quectel(make_config(mode="serving_cell", retries=3))
        modem.open()
        fake_serials[0].read_all_sequence = [serial.SerialException("device disconnected")]

        with pytest.raises(serial.SerialException):
            modem.query_cell_info()

    def test_exhausting_retries_on_persistent_empty_read_yields_no_samples(self, fake_serials, caplog):
        caplog.set_level(logging.WARNING)
        modem = Quectel(make_config(mode="serving_cell", retries=2))
        modem.open()
        fake_serials[0].read_all_sequence = [b"", b""]

        samples = modem.query_cell_info()

        assert samples == []
        assert "No QENG measurements found" in caplog.text


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