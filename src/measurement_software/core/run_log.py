import json
import logging
from dataclasses import asdict
from datetime import datetime, UTC
from pathlib import Path
from typing import TextIO

from measurement_software.core.atomic_file import flush_to_disk, fsync_directory
from measurement_software.core.datapoint import Datapoint

RUN_FILE_PREFIX = "gps_5g_"
RUN_LOG_SUFFIX = ".jsonl"

logger = logging.getLogger(__name__)


class RunLog:
    """An append-only JSON Lines record of one run's datapoints, made durable after every append.

    One JSON object per line is what makes this survivable: an append interrupted by a
    power cut can only ever leave the last, incomplete line unparseable, while every line
    written before it stays independently valid. A single JSON array has no such property -
    one missing closing bracket makes the whole run unreadable.
    """

    def __init__(self, directory: Path):
        self._directory = Path(directory)
        self._path: Path | None = None
        self._file: TextIO | None = None

    @property
    def path(self) -> Path | None:
        """The file this run is being written to, or None until the log has been opened."""
        return self._path

    def open(self) -> None:
        """Creates this run's log file, making the file itself durable before anything is written to it."""
        self._directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        self._path = self._directory / f"{RUN_FILE_PREFIX}{timestamp}{RUN_LOG_SUFFIX}"
        self._file = open(self._path, "a")
        fsync_directory(self._directory)
        logger.info("Recording this run to %s", self._path.name)

    def append(self, datapoints: list[Datapoint]) -> None:
        """Appends datapoints to the run log, returning only once they are on the storage device."""
        if self._file is None:
            raise RuntimeError("Run log must be opened before datapoints can be appended.")
        if not datapoints:
            return

        for datapoint in datapoints:
            self._file.write(f"{json.dumps(asdict(datapoint))}\n")
        flush_to_disk(self._file)

    def close(self) -> None:
        """Closes the log file, leaving it on disk for finalization."""
        if self._file is not None:
            self._file.close()
            self._file = None
