import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from measurement_software.core.config import StorageConfig


@dataclass(frozen=True)
class StorageStatus:
    """Snapshot of the upload backlog and free disk space for the upload directory's filesystem."""

    pending_files: int
    disk_free_bytes: int

    def as_payload(self) -> dict:
        """Renders the status as the JSON body the backend receives."""
        return {"pending_files": self.pending_files, "disk_free_bytes": self.disk_free_bytes}


class StorageStatusReporter:
    """Reads the current upload-backlog size and free disk space, warning loudly if space is low.

    Not a cap or eviction policy (see decision record 0009) - free space is only ever reported
    and warned about here, never acted on.
    """

    def __init__(self, upload_dir: Path, config: StorageConfig):
        self._upload_dir = upload_dir
        self._config = config
        self._logger = logging.getLogger(__name__)

    def status(self) -> StorageStatus:
        """Returns the current backlog/free-space snapshot, logging a warning if space is low."""
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        pending_files = sum(1 for _ in self._upload_dir.glob("*.json"))
        disk_free_bytes = shutil.disk_usage(self._upload_dir).free

        if disk_free_bytes < self._config.low_free_space_warning_bytes:
            self._logger.warning(
                "Low disk space on upload_dir's filesystem: %d bytes free, below the %d byte "
                "warning threshold.", disk_free_bytes, self._config.low_free_space_warning_bytes,
            )

        return StorageStatus(pending_files=pending_files, disk_free_bytes=disk_free_bytes)
