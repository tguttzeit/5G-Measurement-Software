import logging
from pathlib import Path

import pytest

from measurement_software.core.config import StorageConfig
from measurement_software.core.storage_status import StorageStatusReporter


class FakeDiskUsage:
    def __init__(self, free: int):
        self.free = free


@pytest.fixture
def disk_usage(monkeypatch):
    """Fakes shutil.disk_usage so tests control free space without touching a real disk."""
    fake = FakeDiskUsage(free=10_000_000_000)
    monkeypatch.setattr(
        "measurement_software.core.storage_status.shutil.disk_usage",
        lambda path: fake,
    )
    return fake


def make_reporter(tmp_path: Path, **overrides) -> StorageStatusReporter:
    return StorageStatusReporter(tmp_path / "uploads", StorageConfig(**overrides))


class TestStatus:
    def test_creates_upload_dir_if_missing(self, tmp_path, disk_usage):
        upload_dir = tmp_path / "uploads"
        assert not upload_dir.exists()

        make_reporter(tmp_path).status()

        assert upload_dir.is_dir()

    def test_counts_only_pending_json_files(self, tmp_path, disk_usage):
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()
        (upload_dir / "gps_5g_20260101_000000.json").write_text("[]")
        (upload_dir / "gps_5g_20260102_000000.json").write_text("[]")
        (upload_dir / "not_a_datapoint_file.txt").write_text("noise")

        status = make_reporter(tmp_path).status()

        assert status.pending_files == 2

    def test_reports_zero_pending_files_when_backlog_is_empty(self, tmp_path, disk_usage):
        status = make_reporter(tmp_path).status()

        assert status.pending_files == 0

    def test_reports_free_disk_space(self, tmp_path, disk_usage):
        disk_usage.free = 1_234_567

        status = make_reporter(tmp_path).status()

        assert status.disk_free_bytes == 1_234_567


class TestLowSpaceWarning:
    def test_warns_when_free_space_is_below_threshold(self, tmp_path, disk_usage, caplog):
        disk_usage.free = 100_000_000
        reporter = make_reporter(tmp_path, low_free_space_warning_bytes=500_000_000)

        with caplog.at_level(logging.WARNING):
            reporter.status()

        assert "low disk space" in caplog.text.lower()

    def test_does_not_warn_when_free_space_is_above_threshold(self, tmp_path, disk_usage, caplog):
        disk_usage.free = 1_000_000_000
        reporter = make_reporter(tmp_path, low_free_space_warning_bytes=500_000_000)

        with caplog.at_level(logging.WARNING):
            reporter.status()

        assert caplog.text == ""

    def test_does_not_warn_when_free_space_exactly_meets_threshold(self, tmp_path, disk_usage, caplog):
        disk_usage.free = 500_000_000
        reporter = make_reporter(tmp_path, low_free_space_warning_bytes=500_000_000)

        with caplog.at_level(logging.WARNING):
            reporter.status()

        assert caplog.text == ""

    def test_warns_again_on_every_call_while_space_stays_low(self, tmp_path, disk_usage, caplog):
        disk_usage.free = 100_000_000
        reporter = make_reporter(tmp_path, low_free_space_warning_bytes=500_000_000)

        with caplog.at_level(logging.WARNING):
            reporter.status()
            reporter.status()

        assert caplog.text.lower().count("low disk space") == 2
