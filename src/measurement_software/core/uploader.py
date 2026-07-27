import json
import logging
import subprocess
from dataclasses import asdict
from datetime import datetime, UTC
from pathlib import Path

from measurement_software.core.collector import Datapoint
from measurement_software.core.config import UploaderConfig


class Uploader:
    """Saves collected datapoints to disk and uploads pending files to the remote server via scp."""

    def __init__(self, config: UploaderConfig):
        self._logger = logging.getLogger(__name__)
        self._upload_dir = Path(config.upload_dir)
        self._user = config.upload_user
        self._host = config.upload_host
        self._port = config.upload_port
        self._remote_dir = config.remote_dir
        self._debug_upload = config.debug_upload

    def save_datapoints(self, datapoints: list[Datapoint]) -> None:
        """Writes datapoints to a timestamped JSON file in the upload directory."""
        if not datapoints:
            self._logger.info("No datapoints to save - skipping.")
            return

        self._upload_dir.mkdir(parents=True, exist_ok=True)
        filename = f"gps_5g_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
        filepath = self._upload_dir / filename

        with open(filepath, "w") as f:
            json.dump([asdict(dp) for dp in datapoints], f, indent=4)

        self._logger.info("%d datapoints saved to %s", len(datapoints), filename)

    def upload_pending_files(self) -> None:
        """Uploads every pending JSON file via scp if a network interface is up, deleting each on success."""
        if not (self._is_interface_up("wlan0") or self._is_interface_up("wwan0")):
            self._logger.info("No network available, upload postponed.")
            return
        for filepath in sorted(self._upload_dir.glob("*.json")):
            self._upload_file(filepath)

    def _upload_file(self, filepath: Path) -> None:
        """Uploads a single file via scp, deleting it locally only if the transfer succeeds."""
        remote = f"{self._user}@{self._host}:{self._remote_dir}{filepath.name}"
        self._logger.info("Starting upload: %s", filepath.name)

        scp_cmd = ["scp", "-P", str(self._port)]
        if self._debug_upload:
            scp_cmd.append("-vv")
        if self._debug_upload and self._is_interface_up("wwan0"):
            ip = self._get_interface_ip("wwan0")
            if ip is not None:
                scp_cmd += ["-o", f"BindAddress={ip}"]
                self._logger.info("Using wwan0 IP %s", ip)

        scp_cmd += [str(filepath), remote]
        self._logger.debug("SCP command: %s", " ".join(scp_cmd))

        proc = subprocess.run(scp_cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            self._logger.info("Upload successful: %s", filepath.name)
            filepath.unlink()
            return

        self._logger.warning("Upload failed (rc=%s)", proc.returncode)
        if proc.stdout.strip():
            self._logger.debug("SCP stdout:\n%s", proc.stdout.strip())
        if proc.stderr.strip():
            self._logger.debug("SCP stderr:\n%s", proc.stderr.strip())

    @staticmethod
    def _is_interface_up(iface: str) -> bool:
        """Returns True if the given network interface has an IPv4 address assigned."""
        try:
            out = subprocess.check_output(
                ["ip", "addr", "show", iface], stderr=subprocess.DEVNULL
            ).decode()
            return "inet " in out
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    @staticmethod
    def _get_interface_ip(iface: str) -> str | None:
        """Returns the interface's IPv4 address, or None if it has none."""
        try:
            out = subprocess.check_output(
                ["ip", "-4", "-o", "addr", "show", "dev", iface]
            ).decode()
            return out.split()[3].split("/")[0]
        except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
            return None