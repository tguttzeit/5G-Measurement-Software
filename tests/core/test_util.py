import json
import logging
import os

import pytest
import serial

from measurement_software.core.util import (
    TEMP_SUFFIX,
    flush_to_disk,
    fsync_directory,
    haversine_distance_m,
    read_with_retry,
    write_json_atomically,
)
from measurement_software.gnss.gnss_receiver import Position


class FsyncRecorder:
    """Records every `os.fsync` the module performs, without suppressing the real call."""

    def __init__(self, monkeypatch):
        self.fds: list[int] = []
        real_fsync = os.fsync

        def recording_fsync(fd: int) -> None:
            self.fds.append(fd)
            real_fsync(fd)

        monkeypatch.setattr("measurement_software.core.util.os.fsync", recording_fsync)


@pytest.fixture
def fsyncs(monkeypatch) -> FsyncRecorder:
    return FsyncRecorder(monkeypatch)


class TestWriteJsonAtomically:
    def test_writes_payload_as_indented_json(self, tmp_path):
        target = tmp_path / "run.json"

        write_json_atomically(target, [{"rat": "LTE"}])

        assert json.loads(target.read_text()) == [{"rat": "LTE"}]
        assert "\n    " in target.read_text()

    def test_leaves_no_temp_file_behind(self, tmp_path):
        target = tmp_path / "run.json"

        write_json_atomically(target, [{"rat": "LTE"}])

        assert list(tmp_path.iterdir()) == [target]

    def test_replaces_an_existing_file(self, tmp_path):
        target = tmp_path / "run.json"
        target.write_text("[]")

        write_json_atomically(target, [{"rat": "NR5G-SA"}])

        assert json.loads(target.read_text()) == [{"rat": "NR5G-SA"}]

    def test_temp_file_is_not_picked_up_by_the_uploader_glob(self, tmp_path):
        # Uploader.upload_pending_files() globs "*.json"; a temp file must never match it.
        target = tmp_path / "run.json"
        temp_name = f"{target.name}{TEMP_SUFFIX}"

        assert temp_name not in [p.name for p in tmp_path.glob("*.json")]
        (tmp_path / temp_name).write_text("partial")
        assert list(tmp_path.glob("*.json")) == []

    def test_fsyncs_both_the_file_and_its_directory(self, tmp_path, fsyncs):
        write_json_atomically(tmp_path / "run.json", [{"rat": "LTE"}])

        assert len(fsyncs.fds) == 2

    def test_target_stays_absent_when_the_write_is_interrupted(self, tmp_path, monkeypatch):
        def die_midway(payload, f, **kwargs):
            f.write('[{"rat": "LT')
            raise OSError("power lost mid-write")

        monkeypatch.setattr("measurement_software.core.util.json.dump", die_midway)
        target = tmp_path / "run.json"

        with pytest.raises(OSError, match="power lost mid-write"):
            write_json_atomically(target, [{"rat": "LTE"}])

        assert not target.exists()

    def test_previous_content_survives_an_interrupted_rewrite(self, tmp_path, monkeypatch):
        target = tmp_path / "run.json"
        write_json_atomically(target, [{"rat": "LTE"}])

        def die_before_rename(source, destination):
            raise OSError("power lost mid-rename")

        monkeypatch.setattr("measurement_software.core.util.os.replace", die_before_rename)

        with pytest.raises(OSError, match="power lost mid-rename"):
            write_json_atomically(target, [{"rat": "NR5G-SA"}])

        assert json.loads(target.read_text()) == [{"rat": "LTE"}]


class TestFlushToDisk:
    def test_fsyncs_the_file(self, tmp_path, fsyncs):
        path = tmp_path / "run.jsonl"
        with open(path, "w") as f:
            f.write("line\n")
            flush_to_disk(f)

            assert fsyncs.fds == [f.fileno()]

    def test_content_is_readable_before_the_file_is_closed(self, tmp_path):
        path = tmp_path / "run.jsonl"
        with open(path, "w") as f:
            f.write("line\n")
            flush_to_disk(f)

            assert path.read_text() == "line\n"


class TestFsyncDirectory:
    def test_fsyncs_the_directory(self, tmp_path, fsyncs):
        fsync_directory(tmp_path)

        assert len(fsyncs.fds) == 1


class TestHaversineDistanceM:
    def test_zero_distance_between_identical_positions(self):
        pos = Position(latitude=48.13, longitude=11.58)

        assert haversine_distance_m(pos, pos) == 0.0

    def test_symmetric(self):
        a = Position(latitude=48.13, longitude=11.58)
        b = Position(latitude=48.14, longitude=11.60)

        assert haversine_distance_m(a, b) == pytest.approx(haversine_distance_m(b, a))

    def test_one_degree_latitude_is_about_111_km(self):
        a = Position(latitude=0.0, longitude=0.0)
        b = Position(latitude=1.0, longitude=0.0)

        assert haversine_distance_m(a, b) == pytest.approx(111194.93, abs=1.0)


def is_empty(result: bytes) -> bool:
    return result == b""


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("measurement_software.core.util.time.sleep", lambda seconds: None)


def scripted(*results):
    """Returns a zero-arg callable that yields each of `results` in order on successive calls.

    An entry that is an Exception instance is raised instead of returned.
    """
    remaining = list(results)

    def read():
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return read


class TestReadWithRetrySuccess:
    def test_returns_result_immediately_when_not_transient(self):
        result = read_with_retry(scripted(b"data"), is_empty)
        assert result == b"data"

    def test_recovers_after_a_short_read_then_succeeds(self):
        result = read_with_retry(scripted(b"", b"data"), is_empty, retries=3)
        assert result == b"data"

    def test_recovers_after_a_timeout_exception_then_succeeds(self):
        result = read_with_retry(
            scripted(serial.SerialTimeoutException("write timed out"), b"data"), is_empty, retries=3
        )
        assert result == b"data"


class TestReadWithRetryExhaustedRetries:
    def test_returns_last_short_read_once_budget_is_exhausted(self):
        result = read_with_retry(scripted(b"", b"", b""), is_empty, retries=3)
        assert result == b""

    def test_raises_last_timeout_once_budget_is_exhausted(self):
        error = serial.SerialTimeoutException("write timed out")
        with pytest.raises(serial.SerialTimeoutException):
            read_with_retry(scripted(error, error, error), is_empty, retries=3)

    def test_stops_after_configured_number_of_attempts(self):
        calls = []

        def read():
            calls.append(1)
            return b""

        read_with_retry(read, is_empty, retries=3)
        assert len(calls) == 3


class TestReadWithRetryFaultPropagatesImmediately:
    def test_serial_exception_is_not_retried(self):
        calls = []

        def read():
            calls.append(1)
            raise serial.SerialException("device disconnected")

        with pytest.raises(serial.SerialException):
            read_with_retry(read, is_empty, retries=3)

        assert len(calls) == 1

    def test_other_exceptions_are_not_caught(self):
        def read():
            raise ValueError("not a serial error")

        with pytest.raises(ValueError):
            read_with_retry(read, is_empty, retries=3)


class TestReadWithRetryLogging:
    def test_logs_warning_for_short_read(self, caplog):
        caplog.set_level(logging.WARNING)
        read_with_retry(scripted(b"", b"data"), is_empty, retries=3)
        assert "Short/empty serial read" in caplog.text

    def test_logs_warning_for_transient_timeout(self, caplog):
        caplog.set_level(logging.WARNING)
        read_with_retry(
            scripted(serial.SerialTimeoutException("timed out"), b"data"), is_empty, retries=3
        )
        assert "Transient serial timeout" in caplog.text
