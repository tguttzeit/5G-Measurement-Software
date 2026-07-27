import logging

import pytest

from measurement_software.core.config import GnssConfig
from measurement_software.gnss.gnss_receiver import GNSSFix, Position
from measurement_software.gnss.nmea_serial import NMEASerial


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


def make_config(**overrides) -> GnssConfig:
    defaults = dict(type="quectel", port="/dev/ttyUSB3", baud_rate=9600, timeout=1.0)
    defaults.update(overrides)
    return GnssConfig(**defaults)


class FakeSerial:
    def __init__(self, *args, line: bytes = b"", **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.line = line
        self.reset_called = False
        self.closed = False

    def readline(self) -> bytes:
        return self.line

    def reset_input_buffer(self) -> None:
        self.reset_called = True

    def close(self) -> None:
        self.closed = True


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