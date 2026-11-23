import logging
import os
import socket
import struct
import time
from datetime import datetime, timezone
from typing import Callable

from measurement_software.core.system import set_system_clock

logger = logging.getLogger(__name__)

NTP_PORT = 123
_PACKET_SIZE = 48
_NTP_TO_UNIX_EPOCH_S = 2_208_988_800
_ERA_ROLLOVER_S = 2**32
_CLIENT_REQUEST_HEADER = 0x23  # leap indicator 0, version 4, mode 3 (client)
_SERVER_MODE = 4
_LEAP_UNSYNCHRONISED = 3
_MAX_STRATUM = 15

# A clock earlier than this is wrong by definition: nothing this software does predates it.
MIN_PLAUSIBLE_TIME = datetime(2026, 11, 1, tzinfo=timezone.utc)


class SntpError(Exception):
    """The time server's answer was missing, malformed, not from the server asked, or implausible."""


def query_sntp(host: str, timeout_s: float, port: int = NTP_PORT) -> datetime:
    """Asks `host` for the current UTC time over SNTP (RFC 4330) and returns it.

    Accepts only a reply from the address that was asked which echoes this request's random
    transmit timestamp, so a spoofed or stale packet is ignored. The device's own clock is never
    used - it is what's wrong. Raises SntpError (or OSError for network failures).
    """
    server_ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    nonce = os.urandom(8)
    request = bytearray(_PACKET_SIZE)
    request[0] = _CLIENT_REQUEST_HEADER
    request[40:48] = nonce

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(bytes(request), (server_ip, port))
        deadline = time.monotonic() + timeout_s
        while True:
            sock.settimeout(max(deadline - time.monotonic(), 0.001))
            try:
                reply, (source_ip, source_port) = sock.recvfrom(512)
            except socket.timeout:
                raise SntpError(f"no valid reply from {server_ip} within {timeout_s} s") from None
            if (source_ip, source_port) == (server_ip, port) and _is_reply_to(reply, nonce):
                return _parse_time(reply)


def _is_reply_to(packet: bytes, nonce: bytes) -> bool:
    return len(packet) >= _PACKET_SIZE and packet[24:32] == nonce


def _parse_time(packet: bytes) -> datetime:
    leap_indicator = packet[0] >> 6
    mode = packet[0] & 0b111
    stratum = packet[1]
    if mode != _SERVER_MODE:
        raise SntpError(f"reply is not in server mode (mode {mode})")
    if leap_indicator == _LEAP_UNSYNCHRONISED:
        raise SntpError("server reports its clock as unsynchronised")
    if not 1 <= stratum <= _MAX_STRATUM:
        raise SntpError(f"server stratum {stratum} is unusable")

    seconds = struct.unpack(">I", packet[40:44])[0]
    if not seconds & 0x80000000:
        seconds += _ERA_ROLLOVER_S  # past NTP era 0's end in 2036
    server_time = datetime.fromtimestamp(seconds - _NTP_TO_UNIX_EPOCH_S, timezone.utc)
    if server_time < MIN_PLAUSIBLE_TIME:
        raise SntpError(f"server time {server_time} is implausibly old")
    return server_time


class NtpClockSync:
    """Sets the system clock from an NTP server; meant to be tried whenever the link comes up."""

    def __init__(
        self,
        server: str,
        timeout_s: float,
        query: Callable[[str, float], datetime] = query_sntp,
        set_clock: Callable[..., None] = set_system_clock,
    ):
        self._server = server
        self._timeout_s = timeout_s
        self._query = query
        self._set_clock = set_clock

    @property
    def enabled(self) -> bool:
        return bool(self._server)

    def sync(self) -> bool:
        """Returns True once the clock was set, False if the query failed (nothing is changed)."""
        try:
            server_time = self._query(self._server, self._timeout_s)
        except (SntpError, OSError) as e:
            logger.warning("NTP clock sync via %s failed: %s", self._server, e)
            return False
        self._set_clock(server_time, source=f"NTP {self._server}")
        return True
