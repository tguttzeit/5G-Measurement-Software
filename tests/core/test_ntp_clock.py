import socket
import struct
from datetime import datetime, timezone

import pytest

from measurement_software.core import ntp_clock
from measurement_software.core.ntp_clock import NtpClockSync, SntpError, query_sntp

SERVER_IP = "192.0.2.10"
UNIX_TIME = datetime(2027, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def build_reply(request: bytes, *, unix_time: datetime = UNIX_TIME, leap=0, mode=4, stratum=2,
                originate: bytes | None = None) -> bytes:
    packet = bytearray(48)
    packet[0] = (leap << 6) | (4 << 3) | mode
    packet[1] = stratum
    packet[24:32] = originate if originate is not None else request[40:48]
    packet[40:44] = struct.pack(">I", int(unix_time.timestamp()) + 2_208_988_800 & 0xFFFFFFFF)
    return bytes(packet)


class FakeSocket:
    """Replays scripted (reply_builder, source) entries after a request was sent."""

    instances: list["FakeSocket"] = []
    script: list = []
    timeout_error = False

    def __init__(self, *args, **kwargs):
        self.sent: list[tuple[bytes, tuple]] = []
        self.timeout = None
        FakeSocket.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendto(self, data, address):
        self.sent.append((data, address))

    def recvfrom(self, size):
        if not FakeSocket.script:
            raise socket.timeout()
        build, source = FakeSocket.script.pop(0)
        return build(self.sent[0][0]), source


@pytest.fixture(autouse=True)
def fake_network(monkeypatch):
    FakeSocket.instances = []
    FakeSocket.script = []
    monkeypatch.setattr(ntp_clock.socket, "socket", FakeSocket)
    monkeypatch.setattr(
        ntp_clock.socket, "getaddrinfo",
        lambda host, port, family, type_: [(family, type_, 0, "", (SERVER_IP, port))],
    )


def reply_from_server(**kwargs):
    return (lambda request: build_reply(request, **kwargs)), (SERVER_IP, 123)


class TestQuerySntp:
    def test_returns_the_servers_time(self):
        FakeSocket.script = [reply_from_server()]

        assert query_sntp("ntp.example", 3.0) == UNIX_TIME

    def test_sends_a_client_request_with_a_random_transmit_timestamp_to_the_resolved_ip(self):
        FakeSocket.script = [reply_from_server()]

        query_sntp("ntp.example", 3.0)

        data, address = FakeSocket.instances[0].sent[0]
        assert address == (SERVER_IP, 123)
        assert len(data) == 48 and data[0] == 0x23
        assert data[40:48] != bytes(8)
        assert 0 < FakeSocket.instances[0].timeout <= 3.0

    def test_ignores_a_reply_from_another_address(self):
        def build(request):
            return build_reply(request, unix_time=datetime(2030, 1, 1, tzinfo=timezone.utc))

        FakeSocket.script = [(build, ("198.51.100.99", 123)), reply_from_server()]

        assert query_sntp("ntp.example", 3.0) == UNIX_TIME

    def test_ignores_a_reply_from_another_port(self):
        FakeSocket.script = [((lambda request: build_reply(request)), (SERVER_IP, 4000))]

        with pytest.raises(SntpError):
            query_sntp("ntp.example", 3.0)

    def test_ignores_a_reply_that_does_not_echo_the_transmit_timestamp(self):
        FakeSocket.script = [reply_from_server(originate=b"\x01" * 8)]

        with pytest.raises(SntpError):
            query_sntp("ntp.example", 3.0)

    def test_ignores_a_truncated_reply(self):
        FakeSocket.script = [((lambda request: request[:20]), (SERVER_IP, 123))]

        with pytest.raises(SntpError):
            query_sntp("ntp.example", 3.0)

    def test_raises_when_nothing_arrives_in_time(self):
        with pytest.raises(SntpError, match="no valid reply"):
            query_sntp("ntp.example", 3.0)

    @pytest.mark.parametrize("kwargs", [
        {"stratum": 0}, {"stratum": 16}, {"leap": 3}, {"mode": 3},
    ])
    def test_rejects_an_unsynchronised_or_non_server_reply(self, kwargs):
        FakeSocket.script = [reply_from_server(**kwargs)]

        with pytest.raises(SntpError):
            query_sntp("ntp.example", 3.0)

    def test_rejects_a_time_before_the_plausibility_floor(self):
        FakeSocket.script = [reply_from_server(unix_time=datetime(2020, 1, 1, tzinfo=timezone.utc))]

        with pytest.raises(SntpError, match="implausibly old"):
            query_sntp("ntp.example", 3.0)

    def test_reads_a_time_past_the_end_of_ntp_era_0(self):
        year_2040 = datetime(2040, 1, 1, tzinfo=timezone.utc)
        FakeSocket.script = [reply_from_server(unix_time=year_2040)]

        assert query_sntp("ntp.example", 3.0) == year_2040


class TestNtpClockSync:
    def test_sets_the_clock_from_the_servers_time(self):
        set_calls = []
        sync = NtpClockSync("ntp.example", 3.0, query=lambda host, timeout: UNIX_TIME,
                            set_clock=lambda dt, source: set_calls.append((dt, source)))

        assert sync.sync() is True
        assert set_calls == [(UNIX_TIME, "NTP ntp.example")]

    @pytest.mark.parametrize("error", [SntpError("bad"), OSError("unreachable")])
    def test_leaves_the_clock_alone_when_the_query_fails(self, error):
        def failing_query(host, timeout):
            raise error

        set_calls = []
        sync = NtpClockSync("ntp.example", 3.0, query=failing_query,
                            set_clock=lambda dt, source: set_calls.append(dt))

        assert sync.sync() is False
        assert set_calls == []

    def test_is_disabled_without_a_server(self):
        assert NtpClockSync("", 3.0).enabled is False
        assert NtpClockSync("ntp.example", 3.0).enabled is True
