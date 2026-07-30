import json
import logging
from dataclasses import asdict
from datetime import datetime, UTC
from pathlib import Path
from typing import TextIO

from measurement_software.core.atomic_file import (
    TEMP_SUFFIX,
    flush_to_disk,
    fsync_directory,
    write_json_atomically,
)
from measurement_software.core.datapoint import Datapoint

RUN_FILE_PREFIX = "gps_5g_"
RUN_LOG_SUFFIX = ".jsonl"
UPLOAD_SUFFIX = ".json"

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


def finalize(path: Path) -> Path | None:
    """Converts a run log into the uploadable JSON array file, returning that file's path.

    The run log is only removed once the converted file is in place, so an interruption
    at any point here leaves the run recoverable from the log it came from.
    """
    datapoints = _read_intact_datapoints(path)
    if not datapoints:
        logger.info("Run log %s holds no usable datapoints - discarding it.", path.name)
        path.unlink()
        return None

    upload_path = path.with_suffix(UPLOAD_SUFFIX)
    write_json_atomically(upload_path, datapoints)
    path.unlink()

    logger.info("%d datapoints finalized into %s", len(datapoints), upload_path.name)
    return upload_path


def recover_unfinalized(directory: Path) -> list[Path]:
    """Finalizes run logs a previous run left behind, returning the upload files recovered from them.

    Must run before the current run opens its own log, so that a live log is never
    mistaken for an abandoned one.
    """
    if not directory.is_dir():
        return []

    recovered = []
    for path in sorted(directory.glob(f"*{RUN_LOG_SUFFIX}")):
        logger.warning("Run log %s was never finalized - recovering it.", path.name)
        upload_path = finalize(path)
        if upload_path is not None:
            recovered.append(upload_path)
    return recovered


def discard_stale_temp_files(directory: Path) -> None:
    """Removes half-written temp files an interrupted finalization left behind.

    Nothing is lost with them: the run log they were being converted from is still the
    record, and recovery converts it again from scratch.
    """
    if not directory.is_dir():
        return

    stale_paths = sorted(directory.glob(f"*{TEMP_SUFFIX}"))
    for path in stale_paths:
        logger.warning("Discarding %s - left over from an interrupted write.", path.name)
        path.unlink()

    if stale_paths:
        fsync_directory(directory)


def _read_intact_datapoints(path: Path) -> list[dict]:
    """Parses every complete line of a run log, skipping any line an interrupted append left partial."""
    datapoints = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            datapoints.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping incomplete line %d of %s - lost to an interrupted write.", number, path.name)
    return datapoints
