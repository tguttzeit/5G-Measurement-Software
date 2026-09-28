import json
import time
import urllib.error
import urllib.request

import pytest

from measurement_software.core.config import GnssConfig, GpsFixTestConfig
from measurement_software.status.backend_status import GpsFixStatusTracker
from measurement_software.remote.gps_fix_test import GpsFixTestRunner
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, Position

GNSS_CONFIG = GnssConfig(type="nmea_serial", port="/dev/ttyUSB3", baud_rate=9600, timeout=1.0)
FIX = GNSSFix(position=Position(latitude=1.0, longitude=2.0), num_satellites=7)


class FakeGNSSReceiver(GNSSReceiver):
    """Records open/close/read_fix calls and unblocks each read_fix() only once signalled, so
    tests can deterministically drive the polling loop one iteration at a time."""

    def __init__(
        self, fixes: list[GNSSFix | None] | None = None,
        sats_in_view: list[int | None] | None = None,
    ):
        self.calls: list[str] = []
        self._fixes = fixes if fixes is not None else [FIX]
        self._index = 0
        self._sats_in_view = sats_in_view if sats_in_view is not None else []
        self._sats_index = 0

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def read_fix(self) -> GNSSFix | None:
        self.calls.append("read_fix")
        fix = self._fixes[min(self._index, len(self._fixes) - 1)]
        self._index += 1
        return fix

    def read_satellites_in_view(self) -> int | None:
        self.calls.append("read_satellites_in_view")
        if not self._sats_in_view:
            return None
        value = self._sats_in_view[min(self._sats_index, len(self._sats_in_view) - 1)]
        self._sats_index += 1
        return value

    def read_datetime(self):
        return None


class FakeResponse:
    def __init__(self, status: int = 204):
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeBackend:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.requests: list[urllib.request.Request] = []

    def urlopen(self, request, timeout=None) -> FakeResponse:
        self.requests.append(request)
        if self.fail:
            raise urllib.error.URLError("network is unreachable")
        return FakeResponse()

    def payloads(self) -> list[dict]:
        return [json.loads(r.data.decode()) for r in self.requests]


@pytest.fixture
def backend(monkeypatch) -> FakeBackend:
    fake = FakeBackend()
    monkeypatch.setattr(
        "measurement_software.remote.gps_fix_test.urllib.request.urlopen", fake.urlopen
    )
    return fake


@pytest.fixture
def gnss(monkeypatch) -> FakeGNSSReceiver:
    receiver = FakeGNSSReceiver()
    monkeypatch.setattr(
        "measurement_software.remote.gps_fix_test.create_gnss_receiver", lambda config: receiver
    )
    return receiver


@pytest.fixture
def gps_fix_status() -> GpsFixStatusTracker:
    return GpsFixStatusTracker()


def wait_until(condition, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


class TestStartStop:
    def test_start_opens_the_gnss_receiver_and_stop_closes_it(self, gnss, backend, gps_fix_status):
        runner = GpsFixTestRunner(
            GNSS_CONFIG, GpsFixTestConfig(status_url="", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: "open" in gnss.calls)
        runner.stop()

        assert gnss.calls[0] == "open"
        assert gnss.calls[-1] == "close"
        assert not runner.is_running()

    def test_a_second_start_while_running_is_a_no_op(self, gnss, backend, caplog, gps_fix_status):
        import logging

        runner = GpsFixTestRunner(
            GNSS_CONFIG, GpsFixTestConfig(status_url="", report_interval_s=0.05, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )
        runner.start()
        wait_until(lambda: "open" in gnss.calls)
        first_thread = runner._thread

        with caplog.at_level(logging.INFO):
            runner.start()

        assert runner._thread is first_thread
        assert "already in progress" in caplog.text
        runner.stop()

    def test_stop_without_a_prior_start_is_safe(self, gnss, backend, gps_fix_status):
        runner = GpsFixTestRunner(
            GNSS_CONFIG, GpsFixTestConfig(status_url="", report_interval_s=0.05, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.stop()  # must not raise or hang

        assert not runner.is_running()

    def test_auto_stops_after_timeout(self, gnss, backend, gps_fix_status):
        runner = GpsFixTestRunner(
            GNSS_CONFIG, GpsFixTestConfig(status_url="", report_interval_s=0.01, timeout_s=0.02),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: not runner.is_running(), timeout_s=2.0)

        assert gnss.calls[-1] == "close"


class TestReporting:
    def test_posts_num_satellites_has_fix_and_elapsed_s(self, gnss, backend, gps_fix_status):
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="https://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="secret-key",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: len(backend.requests) >= 1)
        runner.stop()

        payload = backend.payloads()[0]
        assert payload["device_id"] == "pi-01"
        assert payload["num_satellites"] == 7
        assert payload["has_fix"] is True
        assert isinstance(payload["elapsed_s"], int)
        request = backend.requests[0]
        assert request.full_url == "https://backend.example.org/status"
        assert request.headers["X-device-id"] == "pi-01"
        assert request.headers["X-device-key"] == "secret-key"

    def test_reports_no_fix_as_zero_satellites_and_has_fix_false(self, monkeypatch, backend, gps_fix_status):
        receiver = FakeGNSSReceiver(fixes=[None])
        monkeypatch.setattr(
            "measurement_software.remote.gps_fix_test.create_gnss_receiver", lambda config: receiver
        )
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="https://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: len(backend.requests) >= 1)
        runner.stop()

        payload = backend.payloads()[0]
        assert payload["num_satellites"] == 0
        assert payload["has_fix"] is False

    def test_reports_satellites_in_view_while_no_fix_exists_yet(self, monkeypatch, backend, gps_fix_status):
        """The whole point of the diagnostic: a satellite count has to be visible before a fix
        is achieved, not only after - see GpsFixTestRunner._poll()."""
        receiver = FakeGNSSReceiver(fixes=[None], sats_in_view=[6])
        monkeypatch.setattr(
            "measurement_software.remote.gps_fix_test.create_gnss_receiver", lambda config: receiver
        )
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="https://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: len(backend.requests) >= 1)
        runner.stop()

        payload = backend.payloads()[0]
        assert payload["num_satellites"] == 6
        assert payload["has_fix"] is False

    def test_prefers_a_fix_found_later_in_the_budget_over_an_earlier_gsv_sentence(self, monkeypatch, backend, gps_fix_status):
        """Regression test: a real NMEA burst has several GSV sentences per GGA sentence, so a
        fix sitting a couple of lines further into the read budget must still win over a GSV
        sentence encountered first - otherwise the diagnostic reports "no fix" every cycle it
        happens to read a GSV line before the GGA line comes back around, even while a fix is
        continuously held. See GpsFixTestRunner._poll()."""
        receiver = FakeGNSSReceiver(fixes=[None, FIX], sats_in_view=[6])
        monkeypatch.setattr(
            "measurement_software.remote.gps_fix_test.create_gnss_receiver", lambda config: receiver
        )
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="https://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: len(backend.requests) >= 1)
        runner.stop()

        payload = backend.payloads()[0]
        assert payload["has_fix"] is True
        assert payload["num_satellites"] == 7

    def test_keeps_previous_reading_when_neither_fix_nor_gsv_available_in_a_cycle(self, monkeypatch, backend, gps_fix_status):
        receiver = FakeGNSSReceiver(fixes=[FIX, None], sats_in_view=[])
        monkeypatch.setattr(
            "measurement_software.remote.gps_fix_test.create_gnss_receiver", lambda config: receiver
        )
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="https://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: len(backend.requests) >= 2)
        runner.stop()

        # First cycle reads FIX (7 sats, has_fix). Second cycle's read_fix() returns None and
        # there is no GSV data at all, so the budget runs out - it should keep reporting the
        # last known reading rather than resetting to 0/no-fix.
        payloads = backend.payloads()
        assert payloads[0]["num_satellites"] == 7
        assert payloads[0]["has_fix"] is True
        assert payloads[1]["num_satellites"] == 7
        assert payloads[1]["has_fix"] is True

    def test_does_not_post_when_status_url_is_unconfigured(self, gnss, backend, gps_fix_status):
        runner = GpsFixTestRunner(
            GNSS_CONFIG, GpsFixTestConfig(status_url="", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: "read_fix" in gnss.calls)
        runner.stop()

        assert backend.requests == []

    def test_does_not_post_when_status_url_is_not_https(self, gnss, backend, gps_fix_status):
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="http://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: "read_fix" in gnss.calls)
        runner.stop()

        assert backend.requests == []

    def test_omits_device_key_header_when_not_configured(self, gnss, backend, gps_fix_status):
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="https://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: len(backend.requests) >= 1)
        runner.stop()

        assert "X-device-key" not in backend.requests[0].headers

    def test_survives_a_backend_it_cannot_reach(self, gnss, monkeypatch, gps_fix_status):
        fail_backend = FakeBackend(fail=True)
        monkeypatch.setattr(
            "measurement_software.remote.gps_fix_test.urllib.request.urlopen", fail_backend.urlopen
        )
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="https://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: len(fail_backend.requests) >= 1)
        runner.stop()

        assert not runner.is_running()

    def test_keeps_polling_and_reporting_across_multiple_intervals(self, gnss, backend, gps_fix_status):
        runner = GpsFixTestRunner(
            GNSS_CONFIG,
            GpsFixTestConfig(status_url="https://backend.example.org/status", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: len(backend.requests) >= 3)
        runner.stop()

        assert gnss.calls.count("read_fix") >= 3


class TestSharedGpsFixStatus:
    def test_a_fix_found_by_the_diagnostic_is_recorded_into_the_shared_gps_fix_status(
        self, gnss, backend, gps_fix_status,
    ):
        """The screen and heartbeat both read `gps_fix_status`, not this diagnostic's own reports
        - MovementGate/Collector are the only other things that feed it, and neither runs while
        this diagnostic is meaningful (Waiting Mode idle, per decision record 0020). Without this,
        the screen/heartbeat would show "no fix" the entire time this diagnostic sees one."""
        runner = GpsFixTestRunner(
            GNSS_CONFIG, GpsFixTestConfig(status_url="", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        assert gps_fix_status.status().has_fix is False

        runner.start()
        wait_until(lambda: gps_fix_status.status().has_fix is True)
        runner.stop()

        status = gps_fix_status.status()
        assert status.has_fix is True
        assert status.num_satellites == FIX.num_satellites

    def test_a_gsv_only_reading_does_not_mark_the_shared_status_as_a_fix(
        self, monkeypatch, backend, gps_fix_status,
    ):
        receiver = FakeGNSSReceiver(fixes=[None], sats_in_view=[6])
        monkeypatch.setattr(
            "measurement_software.remote.gps_fix_test.create_gnss_receiver", lambda config: receiver
        )
        runner = GpsFixTestRunner(
            GNSS_CONFIG, GpsFixTestConfig(status_url="", report_interval_s=0.01, timeout_s=10.0),
            device_id="pi-01", device_key="k",
            gps_fix_status=gps_fix_status,
        )

        runner.start()
        wait_until(lambda: "read_satellites_in_view" in receiver.calls)
        runner.stop()

        assert gps_fix_status.status().has_fix is False
