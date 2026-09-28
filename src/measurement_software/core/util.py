"""Small, generic helpers with no interdependency on each other - durable file writes, URL
building, geo math, timestamp formatting, serial-read retry, and logging setup."""

from __future__ import annotations

import json
import logging
import logging.config
import os
import time
from datetime import datetime, UTC
from math import radians, sin, cos, asin, sqrt
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TextIO, TypeVar
from urllib.parse import urlsplit, urlunsplit

import serial

from measurement_software.core.config import LoggingConfig

if TYPE_CHECKING:
    from measurement_software.gnss.gnss_receiver import Position

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


def build_backend_url(backend_url: str, path: str) -> str:
    """Builds a full backend URL from `BackendConfig.url`'s scheme/host with `path` swapped in."""
    parsed = urlsplit(backend_url)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


_EARTH_RADIUS_M = 6371000


def haversine_distance_m(a: Position, b: Position) -> float:
    """Great-circle distance between two positions, in meters."""
    d_latitude = radians(b.latitude - a.latitude)
    d_longitude = radians(b.longitude - a.longitude)
    x = sin(d_latitude / 2) ** 2
    y = cos(radians(a.latitude)) * cos(radians(b.latitude)) * sin(d_longitude / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(x + y))


def utc_timestamp() -> str:
    """Returns the current UTC timestamp in ISO 8601 format with 'Z' suffix.

    This format is RFC 3339 compliant and can be parsed by standard parsers.
    Example: 2026-07-31T05:30:05.582693Z
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


T = TypeVar("T")

DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY_S = 0.75


def read_with_retry(
    read: Callable[[], T],
    is_transient: Callable[[T], bool],
    *,
    retries: int = DEFAULT_RETRIES,
    delay_s: float = DEFAULT_RETRY_DELAY_S,
    logger: logging.Logger | None = None,
) -> T:
    """Runs `read`, retrying transient serial failures up to `retries` times, `delay_s` apart.

    A `serial.SerialTimeoutException`, or a result `is_transient` flags as a short/empty read
    (e.g. vehicle vibration momentarily jostling the connector), is treated as recoverable and
    retried. Any other `serial.SerialException` means the port itself is gone (device removed,
    permission denied, I/O error on disconnect) — retrying can't fix that, so it propagates
    immediately instead of spending the retry budget on it.
    """
    logger = logger or logging.getLogger(__name__)
    last_timeout: serial.SerialTimeoutException | None = None

    for attempt in range(1, retries + 1):
        try:
            result = read()
        except serial.SerialTimeoutException as e:
            logger.warning("Transient serial timeout (attempt %d/%d): %s", attempt, retries, e)
            last_timeout = e
        except serial.SerialException:
            raise
        else:
            if not is_transient(result):
                return result
            logger.warning("Short/empty serial read (attempt %d/%d)", attempt, retries)
            last_timeout = None

        if attempt < retries:
            time.sleep(delay_s)

    if last_timeout is not None:
        raise last_timeout
    return result


def setup_logging(config: LoggingConfig) -> None:
    """Configures root logging with rotating-file and console handlers per the given config."""
    log_path = Path(config.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_path),
                "maxBytes": config.max_bytes,
                "backupCount": config.backup_count,
                "formatter": "default",
            },
        },
        "root": {
            "level": config.level,
            "handlers": ["console", "file"],
        },
    })
