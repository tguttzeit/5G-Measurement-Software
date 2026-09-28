import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from measurement_software.core.config import BackendConfig, UploaderConfig
from measurement_software.core.uploader import (
    Uploader,
    _NoRedirectHandler,
    _Wwan0BoundHTTPSHandler,
)

BACKEND_URL = "https://backend.example.org"


class FakeInterfaces:
    """Fakes the two `ip` invocations Uploader shells out to.

    `up_ifaces` controls `_is_interface_up`; `ips` controls `_get_interface_ip`.
    An iface missing from `ips` simulates `ip -4 -o addr show dev <iface>`
    failing (e.g. no IPv4 address assigned).
    """

    def __init__(self, up_ifaces: set[str] = frozenset(), ips: dict[str, str] | None = None):
        self.up_ifaces = up_ifaces
        self.ips = ips or {}

    def check_output(self, cmd: list[str], **kwargs) -> bytes:
        iface = cmd[-1]
        if cmd[1] == "-4":  # ip -4 -o addr show dev <iface>
            if iface not in self.ips:
                raise subprocess.CalledProcessError(1, cmd)
            return f"3: {iface}    inet {self.ips[iface]}/24 brd 10.0.0.255 scope global {iface}".encode()

        # ip addr show <iface>
        if iface in self.up_ifaces:
            return f"2: {iface}    inet 10.0.0.5/24 brd 10.0.0.255 scope global {iface}".encode()
        return f"2: {iface}: <BROADCAST,MULTICAST> mtu 1500 state DOWN\n    link/ether aa:bb:cc:dd:ee:ff".encode()


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeOpener:
    """Fakes the `OpenerDirector` returned by `urllib.request.build_opener`.

    `fail_filenames` (matched against the uploaded file's own contents, since the request body
    is the file's bytes) lets a test make specific uploads fail while others succeed.
    """

    def __init__(self, status: int = 200, fail_bodies: set[bytes] = frozenset()):
        self.status = status
        self.fail_bodies = fail_bodies
        self.calls: list[tuple[urllib.request.Request, float | None]] = []

    def open(self, request: urllib.request.Request, timeout=None) -> FakeResponse:
        self.calls.append((request, timeout))
        if request.data in self.fail_bodies:
            raise urllib.error.HTTPError(request.full_url, 500, "Internal Server Error", {}, None)
        return FakeResponse(self.status)


class FakeBuildOpener:
    """Fakes `urllib.request.build_opener`, recording the handlers it was built with."""

    def __init__(self, opener: FakeOpener):
        self._opener = opener
        self.handler_calls: list[tuple] = []

    def __call__(self, *handlers) -> FakeOpener:
        self.handler_calls.append(handlers)
        return self._opener


def make_uploader_config(tmp_path: Path, **overrides) -> UploaderConfig:
    defaults = dict(
        upload_dir=str(tmp_path / "uploads"),
        path="/upload",
        latency_path="/upload/latency",
        debug_upload=False,
        timeout_s=10.0,
    )
    defaults.update(overrides)
    return UploaderConfig(**defaults)


def make_backend_config(**overrides) -> BackendConfig:
    defaults = dict(url=BACKEND_URL, device_key="")
    defaults.update(overrides)
    return BackendConfig(**defaults)


@pytest.fixture
def interfaces(monkeypatch) -> FakeInterfaces:
    fake = FakeInterfaces()
    monkeypatch.setattr("measurement_software.core.uploader.subprocess.check_output", fake.check_output)
    return fake


@pytest.fixture
def opener(monkeypatch) -> FakeOpener:
    fake_opener = FakeOpener()
    monkeypatch.setattr(
        "measurement_software.core.uploader.urllib.request.build_opener",
        FakeBuildOpener(fake_opener),
    )
    return fake_opener


def build_opener_fake(monkeypatch) -> FakeBuildOpener:
    fake_opener = FakeOpener()
    fake_build_opener = FakeBuildOpener(fake_opener)
    monkeypatch.setattr("measurement_software.core.uploader.urllib.request.build_opener", fake_build_opener)
    return fake_build_opener


class TestUploadPendingFiles:
    def test_skips_upload_when_no_network(self, tmp_path, interfaces, opener):
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config()).upload_pending_files()

        assert opener.calls == []
        assert pending.exists()

    def test_skips_upload_when_url_is_not_https(self, tmp_path, interfaces, opener, caplog):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config(url="http://backend.example.org")).upload_pending_files()

        assert opener.calls == []
        assert pending.exists()
        assert "https" in caplog.text

    def test_never_uploads_an_in_progress_run_log_or_a_stale_temp_file(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        finalized = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        run_log = Path(config.upload_dir) / "gps_5g_20260102_000000.jsonl"
        stale_temp = Path(config.upload_dir) / "gps_5g_20260103_000000.json.tmp"
        for path in (finalized, run_log, stale_temp):
            path.write_text("[]")

        Uploader(config, make_backend_config()).upload_pending_files()

        assert len(opener.calls) == 1
        assert run_log.exists()
        assert stale_temp.exists()

    def test_uploads_all_pending_files_in_sorted_order_and_deletes_on_success(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        first = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        second = Path(config.upload_dir) / "gps_5g_20260102_000000.json"
        second.write_text("[]")
        first.write_text('["second"]')

        Uploader(config, make_backend_config()).upload_pending_files()

        assert [r.full_url for r, _ in opener.calls] == ["https://backend.example.org/upload"] * 2
        assert not first.exists()
        assert not second.exists()

    def test_keeps_file_and_does_not_raise_when_upload_fails(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        opener.fail_bodies = {b"[]"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config()).upload_pending_files()

        assert len(opener.calls) == 1
        assert pending.exists()

    def test_continues_to_remaining_files_after_one_upload_fails(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        failing = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        succeeding = Path(config.upload_dir) / "gps_5g_20260102_000000.json"
        failing.write_text('["fail"]')
        succeeding.write_text('["ok"]')
        opener.fail_bodies = {b'["fail"]'}

        Uploader(config, make_backend_config()).upload_pending_files()

        # Both files must be attempted regardless of the first failing.
        assert len(opener.calls) == 2
        assert failing.exists()
        assert not succeeding.exists()

    def test_posts_file_contents_as_body(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text('[{"foo": "bar"}]')

        Uploader(config, make_backend_config()).upload_pending_files()

        [(request, timeout)] = opener.calls
        assert request.method == "POST"
        assert request.data == b'[{"foo": "bar"}]'
        assert request.headers["Content-type"] == "application/json"
        assert timeout == 10.0

    def test_upload_url_derived_from_backend_host_and_upload_path(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path, path="/measurements/upload")
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(
            config, make_backend_config(url="https://collector.example:8443")
        ).upload_pending_files()

        [(request, _)] = opener.calls
        assert request.full_url == "https://collector.example:8443/measurements/upload"

    def test_a_gps_file_still_goes_to_the_measurement_path_as_one_request(
        self, tmp_path, interfaces, opener,
    ):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path, path="/measurements/upload")
        Path(config.upload_dir).mkdir(parents=True)
        gps = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        gps.write_text('[{"a": 1}, {"a": 2}]')

        Uploader(
            config, make_backend_config(url="https://collector.example:8443")
        ).upload_pending_files()

        [(request, _)] = opener.calls
        assert request.full_url == "https://collector.example:8443/measurements/upload"
        assert request.data == b'[{"a": 1}, {"a": 2}]'
        assert not gps.exists()


class TestLatencyUpload:
    """The backend's latency-results endpoint takes one record per request, unlike the
    measurement endpoint's whole-file batch - confirmed live: an array 422s ("should be a valid
    dictionary"), a single object 201s. See `Uploader._upload_latency_file`."""

    def test_posts_each_record_individually_to_the_latency_path_and_deletes_on_full_success(
        self, tmp_path, interfaces, opener,
    ):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path, latency_path="/latency-results")
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "latency_20260101_000000.json"
        pending.write_text(json.dumps([{"test_type": "baseline"}, {"test_type": "under_load"}]))

        Uploader(
            config, make_backend_config(url="https://collector.example:8443")
        ).upload_pending_files()

        assert [r.full_url for r, _ in opener.calls] == ["https://collector.example:8443/latency-results"] * 2
        assert [json.loads(r.data) for r, _ in opener.calls] == [
            {"test_type": "baseline"}, {"test_type": "under_load"},
        ]
        assert not pending.exists()

    def test_a_partial_failure_keeps_only_the_still_pending_records_not_the_whole_file(
        self, tmp_path, interfaces, opener,
    ):
        interfaces.up_ifaces = {"wlan0"}
        opener.fail_bodies = {json.dumps({"test_type": "baseline"}).encode()}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "latency_20260101_000000.json"
        pending.write_text(json.dumps([{"test_type": "baseline"}, {"test_type": "under_load"}]))

        Uploader(config, make_backend_config()).upload_pending_files()

        assert len(opener.calls) == 2
        assert pending.exists()
        assert json.loads(pending.read_text()) == [{"test_type": "baseline"}]

    def test_a_full_failure_leaves_the_file_unchanged(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        opener.fail_bodies = {
            json.dumps({"test_type": "baseline"}).encode(), json.dumps({"test_type": "under_load"}).encode(),
        }
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "latency_20260101_000000.json"
        original = json.dumps([{"test_type": "baseline"}, {"test_type": "under_load"}])
        pending.write_text(original)

        Uploader(config, make_backend_config()).upload_pending_files()

        assert json.loads(pending.read_text()) == json.loads(original)

    def test_logs_and_skips_a_file_that_is_not_valid_json(self, tmp_path, interfaces, opener, caplog):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "latency_20260101_000000.json"
        pending.write_text("not valid json")

        Uploader(config, make_backend_config()).upload_pending_files()

        assert opener.calls == []
        assert pending.exists()
        assert "Could not read" in caplog.text

    def test_device_key_sent_as_header(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config(device_key="test-api-key-123")).upload_pending_files()

        [(request, _)] = opener.calls
        assert request.headers["X-device-key"] == "test-api-key-123"

    def test_no_device_key_header_when_not_configured(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config(device_key="")).upload_pending_files()

        [(request, _)] = opener.calls
        assert "X-device-key" not in request.headers

    def test_device_id_sent_as_header(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config(), device_id="pi-north-01").upload_pending_files()

        [(request, _)] = opener.calls
        assert request.headers["X-device-id"] == "pi-north-01"

    def test_no_device_id_header_when_not_configured(self, tmp_path, interfaces, opener):
        interfaces.up_ifaces = {"wlan0"}
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config()).upload_pending_files()

        [(request, _)] = opener.calls
        assert "X-device-id" not in request.headers

    def test_every_request_uses_a_redirect_refusing_opener(self, tmp_path, interfaces, monkeypatch):
        interfaces.up_ifaces = {"wlan0"}
        fake_build_opener = build_opener_fake(monkeypatch)
        config = make_uploader_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config()).upload_pending_files()

        [handlers] = fake_build_opener.handler_calls
        assert any(isinstance(h, _NoRedirectHandler) for h in handlers)

    def test_debug_upload_binds_to_wwan0_when_it_has_an_ip(self, tmp_path, interfaces, monkeypatch):
        interfaces.up_ifaces = {"wlan0", "wwan0"}
        interfaces.ips = {"wwan0": "10.1.2.3"}
        fake_build_opener = build_opener_fake(monkeypatch)
        config = make_uploader_config(tmp_path, debug_upload=True)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config()).upload_pending_files()

        [handlers] = fake_build_opener.handler_calls
        [bound_handler] = [h for h in handlers if isinstance(h, _Wwan0BoundHTTPSHandler)]
        assert bound_handler._bind_ip == "10.1.2.3"

    def test_debug_upload_false_never_binds_to_wwan0(self, tmp_path, interfaces, monkeypatch):
        interfaces.up_ifaces = {"wlan0", "wwan0"}
        interfaces.ips = {"wwan0": "10.1.2.3"}
        fake_build_opener = build_opener_fake(monkeypatch)
        config = make_uploader_config(tmp_path, debug_upload=False)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config()).upload_pending_files()

        [handlers] = fake_build_opener.handler_calls
        assert not any(isinstance(h, _Wwan0BoundHTTPSHandler) for h in handlers)

    def test_debug_upload_true_but_wwan0_without_ip_skips_bind(self, tmp_path, interfaces, monkeypatch):
        interfaces.up_ifaces = {"wlan0", "wwan0"}
        interfaces.ips = {}  # wwan0 up but no IPv4 address yet
        fake_build_opener = build_opener_fake(monkeypatch)
        config = make_uploader_config(tmp_path, debug_upload=True)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config, make_backend_config()).upload_pending_files()

        [handlers] = fake_build_opener.handler_calls
        assert not any(isinstance(h, _Wwan0BoundHTTPSHandler) for h in handlers)


class TestNoRedirectHandler:
    def test_redirect_request_refuses_to_build_a_redirect(self):
        handler = _NoRedirectHandler()

        result = handler.redirect_request(
            req=urllib.request.Request("https://backend.example.org/upload"),
            fp=None, code=302, msg="Found",
            headers={}, newurl="https://attacker.example/collect",
        )

        assert result is None


class TestInterfaceHelpers:
    def test_is_interface_up_true_when_inet_present(self, interfaces):
        interfaces.up_ifaces = {"wlan0"}
        assert Uploader._is_interface_up("wlan0") is True

    def test_is_interface_up_false_when_no_inet(self, interfaces):
        assert Uploader._is_interface_up("wlan0") is False

    def test_is_interface_up_false_when_ip_command_missing(self, monkeypatch):
        def raise_missing(cmd, **kwargs):
            raise FileNotFoundError("ip: command not found")

        monkeypatch.setattr("measurement_software.core.uploader.subprocess.check_output", raise_missing)

        assert Uploader._is_interface_up("wlan0") is False

    def test_get_interface_ip_returns_parsed_address(self, interfaces):
        interfaces.ips = {"wwan0": "192.168.1.42"}
        assert Uploader._get_interface_ip("wwan0") == "192.168.1.42"

    def test_get_interface_ip_none_when_command_fails(self, interfaces):
        assert Uploader._get_interface_ip("wwan0") is None

    def test_get_interface_ip_none_when_ip_command_missing(self, monkeypatch):
        def raise_missing(cmd, **kwargs):
            raise FileNotFoundError("ip: command not found")

        monkeypatch.setattr("measurement_software.core.uploader.subprocess.check_output", raise_missing)

        assert Uploader._get_interface_ip("wwan0") is None
