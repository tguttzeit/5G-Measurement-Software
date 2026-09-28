import http.client
import json
import logging
import socket
import subprocess
import urllib.request
from pathlib import Path

from measurement_software.core.config import BackendConfig, UploaderConfig
from measurement_software.core.util import build_backend_url, write_json_atomically
from measurement_software.latency.tester import LATENCY_FILE_PREFIX


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow HTTP redirects, so `X-Device-Key` can never be resent to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Wwan0BoundHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection whose outgoing socket is bound to a specific source IP."""

    bind_ip: str = ""

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout, source_address=(self.bind_ip, 0),
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _Wwan0BoundHTTPSHandler(urllib.request.HTTPSHandler):
    """Routes HTTPS requests through `_Wwan0BoundHTTPSConnection`, binding to the given IP."""

    def __init__(self, bind_ip: str):
        super().__init__()
        self._bind_ip = bind_ip

    def https_open(self, req):
        def build_connection(host, **kwargs):
            connection = _Wwan0BoundHTTPSConnection(host, **kwargs)
            connection.bind_ip = self._bind_ip
            return connection

        return self.do_open(build_connection, req, context=self._context)


class Uploader:
    """Uploads finalized measurement files from the upload directory via authenticated HTTPS POST.

    Routes each pending file to one of two endpoints by filename prefix: a `LATENCY_FILE_PREFIX`
    ("latency_") file goes to `config.latency_path`, everything else (GPS/cell measurement
    datapoints) to `config.path`. They're incompatible schemas on the backend - a LatencyResult
    (start_fix/end_fix/rtt figures) doesn't validate as a measurement datapoint
    (timestamp/fix/cell_sample) - so posting both to the same endpoint always fails one of them.
    The two endpoints also differ in shape, not just schema: `config.path` accepts one POST per
    *file* (a whole JSON array of datapoints in one request); `config.latency_path` accepts one
    POST per *record* (a single JSON object each) - confirmed live against the real backend,
    which 422s a latency array with "should be a valid dictionary" but 201s a single object.
    """

    def __init__(self, config: UploaderConfig, backend: BackendConfig, device_id: str = ""):
        self._logger = logging.getLogger(__name__)
        self._upload_dir = Path(config.upload_dir)
        self._backend = backend
        self._device_id = device_id
        self._url = build_backend_url(backend.url, config.path)
        self._latency_url = build_backend_url(backend.url, config.latency_path)
        self._timeout_s = config.timeout_s
        self._debug_upload = config.debug_upload

    def upload_pending_files(self) -> None:
        """Uploads every pending JSON file over HTTPS if usable and a network interface is up."""
        if not self._is_usable():
            return
        if not (self._is_interface_up("wlan0") or self._is_interface_up("wwan0")):
            self._logger.info("No network available, upload postponed.")
            return
        for filepath in sorted(self._upload_dir.glob("*.json")):
            if filepath.name.startswith(LATENCY_FILE_PREFIX):
                self._upload_latency_file(filepath)
            else:
                self._upload_file(filepath, self._url)

    def _is_usable(self) -> bool:
        """Reports whether the backend URL is usable, saying loudly why if it isn't."""
        if not self._backend.url.startswith("https://"):
            self._logger.error(
                "Uploads need an https:// backend url but are configured with %r - "
                "uploads disabled for this run.", self._backend.url,
            )
            return False
        return True

    def _upload_file(self, filepath: Path, url: str) -> None:
        """POSTs a single file's contents to `url`, deleting it locally only if the upload succeeds."""
        self._logger.info("Starting upload: %s", filepath.name)
        if self._post(url, filepath.read_bytes(), filepath.name):
            filepath.unlink()

    def _upload_latency_file(self, filepath: Path) -> None:
        """POSTs each record in a latency result file individually - see `Uploader`'s docstring
        for why, unlike `_upload_file`, this can't just send the file's bytes as one request.

        Records that post successfully are removed from the file as they go (atomically, via
        `write_json_atomically`) so a later retry after a partial failure only resends what's
        still pending - not already-accepted records, which the backend has no way to
        deduplicate since each is just POSTed as a bare object with no submission id.
        """
        try:
            records = json.loads(filepath.read_text())
        except (OSError, json.JSONDecodeError) as e:
            self._logger.warning("Could not read %s: %s", filepath.name, e)
            return

        self._logger.info("Starting upload: %s (%d records)", filepath.name, len(records))
        remaining = [
            record for record in records
            if not self._post(self._latency_url, json.dumps(record).encode(), filepath.name)
        ]

        if not remaining:
            filepath.unlink()
        elif len(remaining) < len(records):
            write_json_atomically(filepath, remaining)

    def _post(self, url: str, body: bytes, filename: str) -> bool:
        """POSTs one request body, returning whether it succeeded."""
        headers = {"Content-Type": "application/json"}
        if self._device_id:
            headers["X-Device-ID"] = self._device_id
        if self._backend.device_key:
            headers["X-Device-Key"] = self._backend.device_key

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = self._build_opener()
        try:
            with opener.open(request, timeout=self._timeout_s) as response:
                self._logger.info("Upload successful (HTTP %s): %s", response.status, filename)
                return True
        except (OSError, http.client.HTTPException) as e:
            self._logger.warning("Upload failed: %s: %s", filename, e)
            return False

    def _build_opener(self) -> urllib.request.OpenerDirector:
        """Builds the opener for one upload: always redirect-safe, wwan0-bound only in debug mode."""
        handlers: list[urllib.request.BaseHandler] = [_NoRedirectHandler()]
        if self._debug_upload and self._is_interface_up("wwan0"):
            bind_ip = self._get_interface_ip("wwan0")
            if bind_ip is not None:
                handlers.append(_Wwan0BoundHTTPSHandler(bind_ip))
                self._logger.info("Using wwan0 IP %s", bind_ip)
        return urllib.request.build_opener(*handlers)

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
