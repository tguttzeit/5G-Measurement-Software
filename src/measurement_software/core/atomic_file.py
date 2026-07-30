import json
import os
from pathlib import Path
from typing import Any, TextIO

TEMP_SUFFIX = ".tmp"


def write_json_atomically(path: Path, payload: Any) -> None:
    """Writes JSON via a temp file in the same directory, so `path` is only ever complete or absent."""
    temp_path = path.with_name(f"{path.name}{TEMP_SUFFIX}")
    with open(temp_path, "w") as f:
        json.dump(payload, f, indent=4)
        flush_to_disk(f)

    os.replace(temp_path, path)
    fsync_directory(path.parent)


def flush_to_disk(f: TextIO) -> None:
    """Returns only once the file's buffered writes have left the OS page cache for the storage device."""
    f.flush()
    os.fsync(f.fileno())


def fsync_directory(directory: Path) -> None:
    """Makes a directory entry change - a file created, renamed or removed - durable across a power cut."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
