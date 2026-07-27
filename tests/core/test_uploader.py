import json
import subprocess
from pathlib import Path

import pytest

from measurement_software.core.collector import Datapoint
from measurement_software.core.config import UploaderConfig
from measurement_software.core.uploader import Uploader
from measurement_software.gnss.gnss_receiver import GNSSFix, Position
from measurement_software.modems.modem import CellSample


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


class FakeScp:
    """Fakes `subprocess.run` for the scp call, recording every invocation.

    `fail_filenames` lets a test make specific uploads fail by source filename
    while others succeed, to exercise multi-file batches.
    """

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", fail_filenames: set[str] = frozenset()):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.fail_filenames = fail_filenames
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        filename = Path(cmd[-2]).name
        returncode = 1 if filename in self.fail_filenames else self.returncode
        return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=self.stdout, stderr=self.stderr)


def make_config(tmp_path: Path, **overrides) -> UploaderConfig:
    defaults = dict(
        upload_dir=str(tmp_path / "uploads"),
        upload_user="pi",
        upload_host="example.org",
        upload_port=2000,
        remote_dir="/remote/dir/",
        debug_upload=False,
    )
    defaults.update(overrides)
    return UploaderConfig(**defaults)


def make_datapoint() -> Datapoint:
    fix = GNSSFix(position=Position(latitude=1.0, longitude=2.0, altitude=3.0), num_satellites=7)
    return Datapoint(timestamp="2026-07-27T00:00:00Z", fix=fix, cell_sample=CellSample(rat="LTE"))


@pytest.fixture
def interfaces(monkeypatch) -> FakeInterfaces:
    fake = FakeInterfaces()
    monkeypatch.setattr("measurement_software.core.uploader.subprocess.check_output", fake.check_output)
    return fake


@pytest.fixture
def scp(monkeypatch) -> FakeScp:
    fake = FakeScp()
    monkeypatch.setattr("measurement_software.core.uploader.subprocess.run", fake.run)
    return fake


class TestSaveDatapoints:
    def test_skips_and_creates_nothing_when_no_datapoints(self, tmp_path):
        config = make_config(tmp_path)
        uploader = Uploader(config)

        uploader.save_datapoints([])

        assert not Path(config.upload_dir).exists()

    def test_creates_upload_dir_and_writes_json(self, tmp_path):
        config = make_config(tmp_path)
        uploader = Uploader(config)
        dp = make_datapoint()

        uploader.save_datapoints([dp])

        upload_dir = Path(config.upload_dir)
        assert upload_dir.is_dir()
        files = list(upload_dir.glob("*.json"))
        assert len(files) == 1
        assert files[0].name.startswith("gps_5g_") and files[0].name.endswith(".json")

        content = json.loads(files[0].read_text())
        assert len(content) == 1
        assert content[0]["timestamp"] == dp.timestamp
        assert content[0]["cell_sample"]["rat"] == "LTE"
        assert content[0]["fix"]["position"]["latitude"] == 1.0
        assert content[0]["fix"]["num_satellites"] == 7


class TestUploadPendingFiles:
    def test_skips_upload_when_no_network(self, tmp_path, interfaces, scp):
        config = make_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config).upload_pending_files()

        assert scp.calls == []
        assert pending.exists()

    def test_uploads_all_pending_files_in_sorted_order_and_deletes_on_success(self, tmp_path, interfaces, scp):
        interfaces.up_ifaces = {"wlan0"}
        config = make_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        first = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        second = Path(config.upload_dir) / "gps_5g_20260102_000000.json"
        second.write_text("[]")
        first.write_text("[]")

        Uploader(config).upload_pending_files()

        assert [Path(c[-2]).name for c in scp.calls] == [first.name, second.name]
        assert not first.exists()
        assert not second.exists()

    def test_keeps_file_and_does_not_raise_when_scp_fails(self, tmp_path, interfaces, scp):
        interfaces.up_ifaces = {"wlan0"}
        scp.returncode = 1
        scp.stderr = "scp: connection refused"
        config = make_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config).upload_pending_files()

        assert len(scp.calls) == 1
        assert pending.exists()

    def test_continues_to_remaining_files_after_one_upload_fails(self, tmp_path, interfaces, scp):
        interfaces.up_ifaces = {"wlan0"}
        config = make_config(tmp_path)
        Path(config.upload_dir).mkdir(parents=True)
        failing = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        succeeding = Path(config.upload_dir) / "gps_5g_20260102_000000.json"
        failing.write_text("[]")
        succeeding.write_text("[]")
        scp.fail_filenames = {failing.name}

        Uploader(config).upload_pending_files()

        # Both files must be attempted, in sorted order, regardless of the first failing.
        assert [Path(c[-2]).name for c in scp.calls] == [failing.name, succeeding.name]
        assert failing.exists()
        assert not succeeding.exists()

    def test_scp_command_uses_configured_user_host_port_and_remote_dir(self, tmp_path, interfaces, scp):
        interfaces.up_ifaces = {"wlan0"}
        config = make_config(
            tmp_path, upload_user="operator", upload_host="collector.example", upload_port=2222,
            remote_dir="/data/incoming/",
        )
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config).upload_pending_files()

        [cmd] = scp.calls
        assert cmd[0] == "scp"
        assert cmd[cmd.index("-P") + 1] == "2222"
        assert cmd[-1] == f"operator@collector.example:/data/incoming/{pending.name}"

    def test_debug_upload_adds_verbose_and_bind_address_when_wwan0_up(self, tmp_path, interfaces, scp):
        interfaces.up_ifaces = {"wlan0", "wwan0"}
        interfaces.ips = {"wwan0": "10.1.2.3"}
        config = make_config(tmp_path, debug_upload=True)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config).upload_pending_files()

        [cmd] = scp.calls
        assert "-vv" in cmd
        assert "-o" in cmd
        assert cmd[cmd.index("-o") + 1] == "BindAddress=10.1.2.3"

    def test_debug_upload_false_never_adds_verbose_or_bind_address(self, tmp_path, interfaces, scp):
        # wwan0 is up and has an IP, but debug_upload=False must suppress both flags.
        interfaces.up_ifaces = {"wlan0", "wwan0"}
        interfaces.ips = {"wwan0": "10.1.2.3"}
        config = make_config(tmp_path, debug_upload=False)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config).upload_pending_files()

        [cmd] = scp.calls
        assert "-vv" not in cmd
        assert "-o" not in cmd

    def test_debug_upload_true_but_wwan0_without_ip_skips_bind_address(self, tmp_path, interfaces, scp):
        interfaces.up_ifaces = {"wlan0", "wwan0"}
        interfaces.ips = {}  # wwan0 up but no IPv4 address yet
        config = make_config(tmp_path, debug_upload=True)
        Path(config.upload_dir).mkdir(parents=True)
        pending = Path(config.upload_dir) / "gps_5g_20260101_000000.json"
        pending.write_text("[]")

        Uploader(config).upload_pending_files()

        [cmd] = scp.calls
        assert "-vv" in cmd
        assert "-o" not in cmd


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
