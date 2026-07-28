import pytest

from measurement_software.core.config import GnssConfig
from measurement_software.gnss import create_gnss_receiver
from measurement_software.gnss.nmea_serial import NMEASerial


def make_config(**overrides) -> GnssConfig:
    defaults = dict(type="nmea_serial", port="/dev/serial0", baud_rate=9600, timeout=1.0)
    defaults.update(overrides)
    return GnssConfig(**defaults)


def test_creates_nmea_serial_for_nmea_serial_type():
    receiver = create_gnss_receiver(make_config())

    assert isinstance(receiver, NMEASerial)


def test_raises_for_unknown_type():
    with pytest.raises(ValueError, match="Unknown gnss receiver type: 'bogus'"):
        create_gnss_receiver(make_config(type="bogus"))
