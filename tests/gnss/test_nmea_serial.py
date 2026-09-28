import logging
from datetime import UTC, datetime

import pytest
import serial

from measurement_software.core.config import GnssConfig
from measurement_software.gnss.gnss_receiver import GNSSFix, Position
from measurement_software.gnss.nmea_serial import NMEASerial


def _with_checksum(body: str) -> str:
    """Appends a correct NMEA checksum (XOR of body bytes) to a sentence body."""
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


def zda_sentence(
    talker: str = "GPZDA",
    time: str = "201530.00",
    day: str = "04",
    month: str = "07",
    year: str = "2026",
) -> str:
    """Builds a ZDA sentence with a correct NMEA checksum."""
    return _with_checksum(f"{talker},{time},{day},{month},{year},00,00")


def gga_sentence(
    talker: str = "GPGGA",
    time: str = "123519",
    lat: str = "4807.038",
    lat_dir: str = "N",
    lon: str = "01131.000",
    lon_dir: str = "E",
    gps_qual: str = "1",
    num_sats: str = "08",
    hdop: str = "0.9",
    altitude: str = "545.4",
    geoid_sep: str = "46.9",
) -> str:
    """Builds a GGA sentence with a correct NMEA checksum (XOR of body bytes)."""
    body = f"{talker},{time},{lat},{lat_dir},{lon},{lon_dir},{gps_qual},{num_sats},{hdop},{altitude},M,{geoid_sep},M,,"
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


def gsv_sentence(
    talker: str = "GPGSV",
    num_messages: str = "1",
    msg_num: str = "1",
    num_sv_in_view: str = "11",
) -> str:
    """Builds a single-message GSV sentence (no satellite-detail fields) with a correct checksum."""
    body = f"{talker},{num_messages},{msg_num},{num_sv_in_view}"
    return _with_checksum(body)


def make_config(**overrides) -> GnssConfig:
    defaults = dict(type="quectel", port="/dev/ttyUSB3", baud_rate=9600, timeout=1.0)
    defaults.update(overrides)
    return GnssConfig(**defaults)


class FakeSerial:
    """`readline_sequence`, if set, scripts successive `readline()` calls (an entry that is an
    Exception is raised instead of returned) — otherwise every call just returns `line`.
    """

    def __init__(self, *args, line: bytes = b"", **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.line = line
        self.readline_sequence: list[bytes | Exception] | None = None
        self.reset_called = False
        self.closed = False

    def readline(self) -> bytes:
        if self.readline_sequence is not None:
            item = self.readline_sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.line

    def reset_input_buffer(self) -> None:
        self.reset_called = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("measurement_software.core.util.time.sleep", lambda seconds: None)


@pytest.fixture
def fake_serials(monkeypatch) -> list[FakeSerial]:
    """Patches serial.Serial; each call appends a new FakeSerial to the returned list."""
    created: list[FakeSerial] = []

    def factory(*args, **kwargs):
        fake = FakeSerial(*args, **kwargs)
        created.append(fake)
        return fake

    monkeypatch.setattr("measurement_software.gnss.nmea_serial.serial.Serial", factory)
    return created


class TestConnectionLifecycle:
    def test_read_fix_before_open_raises(self):
        gnss = NMEASerial(make_config())
        with pytest.raises(RuntimeError, match="not open"):
            gnss.read_fix()

    def test_open_constructs_serial_with_configured_parameters_and_resets_buffer(self, fake_serials):
        gnss = NMEASerial(make_config(port="/dev/ttyUSB9", baud_rate=4800, timeout=2.0))
        gnss.open()

        assert len(fake_serials) == 1
        assert fake_serials[0].args == ("/dev/ttyUSB9", 4800)
        assert fake_serials[0].kwargs == {"timeout": 2.0}
        assert fake_serials[0].reset_called is True

    def test_close_closes_connection_and_blocks_further_use(self, fake_serials):
        gnss = NMEASerial(make_config())
        gnss.open()
        gnss.close()

        assert fake_serials[0].closed is True
        with pytest.raises(RuntimeError, match="not open"):
            gnss.read_fix()

    def test_close_without_open_is_a_noop(self):
        gnss = NMEASerial(make_config())
        gnss.close()  # must not raise


class TestReadFix:
    def _gnss(self, fake_serials: list[FakeSerial], line: str) -> NMEASerial:
        gnss = NMEASerial(make_config())
        gnss.open()
        fake_serials[0].line = line.encode()
        return gnss

    def test_returns_none_for_non_gga_sentence(self, fake_serials):
        gnss = self._gnss(fake_serials, "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A")
        assert gnss.read_fix() is None

    def test_returns_none_for_unparseable_garbage(self, fake_serials):
        gnss = self._gnss(fake_serials, "not an nmea sentence at all")
        assert gnss.read_fix() is None

    def test_decodes_invalid_utf8_without_crashing(self, fake_serials):
        gnss = self._gnss(fake_serials, "")
        fake_serials[0].line = b"\xff\xfe not a valid gga line"
        assert gnss.read_fix() is None

    def test_returns_none_when_gps_qual_is_zero(self, fake_serials):
        gnss = self._gnss(fake_serials, gga_sentence(gps_qual="0"))
        assert gnss.read_fix() is None

    def test_returns_none_for_a_gga_line_with_a_bad_checksum(self, fake_serials):
        """A corrupted-on-the-wire GGA sentence (bit error, or a reopen cutting a line off
        mid-transmission) must not crash the caller - see read_datetime()'s equivalent test."""
        gnss = self._gnss(fake_serials, "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*00")
        assert gnss.read_fix() is None

    def test_returns_none_for_a_truncated_gga_line(self, fake_serials):
        """A line cut off mid-transmission (e.g. by the receiver being reopened) fails to parse
        structurally - must not crash the caller."""
        gnss = self._gnss(fake_serials, "$GPGGA,123519,4807")
        assert gnss.read_fix() is None

    def test_returns_none_for_a_non_numeric_satellite_count(self, fake_serials):
        """A structurally valid sentence whose num_sats field can't convert to int must not crash."""
        gnss = self._gnss(fake_serials, gga_sentence(num_sats="XX"))
        assert gnss.read_fix() is None

    def test_a_corrupted_line_does_not_prevent_reading_the_next_valid_one(self, fake_serials):
        """Regression for issue #38: a single bad line among otherwise-valid ones must not take
        down the whole run - the caller can just keep reading."""
        gnss = self._gnss(fake_serials, "")
        fake_serials[0].readline_sequence = [
            b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*00",  # bad checksum
            gga_sentence().encode(),
        ]

        assert gnss.read_fix() is None
        fix = gnss.read_fix()
        assert fix is not None
        assert fix.num_satellites == 8

    def test_returns_fix_for_gpgga(self, fake_serials):
        gnss = self._gnss(fake_serials, gga_sentence(talker="GPGGA"))

        fix = gnss.read_fix()

        assert fix == GNSSFix(
            position=Position(latitude=48.1173, longitude=11.516666666666667, altitude=545.4),
            num_satellites=8,
        )

    def test_returns_fix_for_gngga(self, fake_serials):
        gnss = self._gnss(fake_serials, gga_sentence(talker="GNGGA"))

        fix = gnss.read_fix()

        assert fix is not None
        assert fix.num_satellites == 8

    def test_warns_on_low_satellite_count(self, fake_serials, caplog):
        caplog.set_level(logging.WARNING)
        gnss = self._gnss(fake_serials, gga_sentence(num_sats="03"))

        fix = gnss.read_fix()

        assert fix is not None
        assert fix.num_satellites == 3
        assert "Low satellite count" in caplog.text

    def test_no_warning_when_satellite_count_sufficient(self, fake_serials, caplog):
        caplog.set_level(logging.WARNING)
        gnss = self._gnss(fake_serials, gga_sentence(num_sats="05"))

        gnss.read_fix()

        assert "Low satellite count" not in caplog.text


class TestReadSatellitesInView:
    def _gnss(self, fake_serials: list[FakeSerial], line: str) -> NMEASerial:
        gnss = NMEASerial(make_config())
        gnss.open()
        fake_serials[0].line = line.encode()
        return gnss

    def test_returns_none_for_non_gsv_sentence(self, fake_serials):
        gnss = self._gnss(fake_serials, gga_sentence())
        assert gnss.read_satellites_in_view() is None

    def test_returns_count_for_gpgsv(self, fake_serials):
        gnss = self._gnss(fake_serials, gsv_sentence(talker="GPGSV", num_sv_in_view="11"))
        assert gnss.read_satellites_in_view() == 11

    def test_returns_count_for_gngsv(self, fake_serials):
        gnss = self._gnss(fake_serials, gsv_sentence(talker="GNGSV", num_sv_in_view="7"))
        assert gnss.read_satellites_in_view() == 7

    def test_available_even_without_a_fix(self, fake_serials):
        """The whole point: GSV reports satellites in view regardless of fix status, unlike
        GGA's satellite count (see TestReadFix.test_returns_none_when_gps_qual_is_zero)."""
        gnss = self._gnss(fake_serials, gsv_sentence(num_sv_in_view="4"))
        assert gnss.read_satellites_in_view() == 4

    def test_returns_none_for_a_gsv_line_with_a_bad_checksum(self, fake_serials):
        """A corrupted-on-the-wire GSV sentence must not crash the caller."""
        gnss = self._gnss(fake_serials, "$GPGSV,1,1,11*00")
        assert gnss.read_satellites_in_view() is None

    def test_returns_none_for_a_non_numeric_satellite_count(self, fake_serials):
        gnss = self._gnss(fake_serials, gsv_sentence(num_sv_in_view="XX"))
        assert gnss.read_satellites_in_view() is None


class TestReadFixRetry:
    def test_recovers_from_a_transient_empty_read(self, fake_serials):
        gnss = NMEASerial(make_config(retries=3))
        gnss.open()
        fake_serials[0].readline_sequence = [b"", gga_sentence().encode()]

        fix = gnss.read_fix()

        assert fix is not None
        assert fix.num_satellites == 8

    def test_recovers_from_a_transient_timeout_exception(self, fake_serials):
        gnss = NMEASerial(make_config(retries=3))
        gnss.open()
        fake_serials[0].readline_sequence = [
            serial.SerialTimeoutException("write timed out"),
            gga_sentence().encode(),
        ]

        fix = gnss.read_fix()

        assert fix is not None

    def test_device_gone_propagates_immediately_without_exhausting_retries(self, fake_serials):
        gnss = NMEASerial(make_config(retries=3))
        gnss.open()
        fake_serials[0].readline_sequence = [serial.SerialException("device disconnected")]

        with pytest.raises(serial.SerialException):
            gnss.read_fix()

    def test_a_non_gga_line_is_not_retried(self, fake_serials):
        """A line that just isn't GGA is a normal NMEA stream condition, not a transient failure."""
        gnss = NMEASerial(make_config(retries=3))
        gnss.open()
        fake_serials[0].readline_sequence = [
            b"$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        ]

        fix = gnss.read_fix()

        assert fix is None
        assert fake_serials[0].readline_sequence == []


class TestReadDatetime:
    def _gnss(self, fake_serials: list[FakeSerial], line: str) -> NMEASerial:
        gnss = NMEASerial(make_config())
        gnss.open()
        fake_serials[0].line = line.encode()
        return gnss

    def test_returns_none_for_non_zda_sentence(self, fake_serials):
        gnss = self._gnss(fake_serials, gga_sentence())
        assert gnss.read_datetime() is None

    def test_returns_none_for_unparseable_garbage(self, fake_serials):
        gnss = self._gnss(fake_serials, "not an nmea sentence at all")
        assert gnss.read_datetime() is None

    def test_returns_datetime_for_gpzda(self, fake_serials):
        gnss = self._gnss(fake_serials, zda_sentence(talker="GPZDA", time="201530.00", day="04", month="07", year="2026"))

        result = gnss.read_datetime()

        assert result == datetime(2026, 7, 4, 20, 15, 30, tzinfo=UTC)

    def test_returns_datetime_for_gnzda(self, fake_serials):
        gnss = self._gnss(fake_serials, zda_sentence(talker="GNZDA"))

        result = gnss.read_datetime()

        assert result is not None
        assert result.tzinfo == UTC

    def test_returns_none_for_zda_with_empty_date_time_fields(self, fake_serials):
        """A ZDA sentence with empty fields is real behavior before a receiver has a time lock -
        it parses structurally but carries nothing usable, and must not crash."""
        gnss = self._gnss(fake_serials, _with_checksum("GPZDA,,,,,,"))

        assert gnss.read_datetime() is None

    def test_returns_none_for_a_zda_line_with_a_bad_checksum(self, fake_serials):
        """A corrupted-on-the-wire ZDA sentence must not crash the caller."""
        gnss = self._gnss(fake_serials, "$GPZDA,201530.00,04,07,2026,00,00*00")

        assert gnss.read_datetime() is None