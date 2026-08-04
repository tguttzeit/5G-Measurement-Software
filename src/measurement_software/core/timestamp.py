"""Timestamp utilities for consistent ISO 8601 formatting."""

from datetime import datetime, UTC


def utc_timestamp() -> str:
    """Returns the current UTC timestamp in ISO 8601 format with 'Z' suffix.
    
    This format is RFC 3339 compliant and can be parsed by standard parsers.
    Example: 2026-07-31T05:30:05.582693Z
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
