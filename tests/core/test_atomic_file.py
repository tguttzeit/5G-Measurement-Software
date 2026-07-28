import json
import os

import pytest

from measurement_software.core.atomic_file import (
    TEMP_SUFFIX,
    flush_to_disk,
    fsync_directory,
    write_json_atomically,
)


class FsyncRecorder:
    """Records every `os.fsync` the module performs, without suppressing the real call."""

    def __init__(self, monkeypatch):
        self.fds: list[int] = []
        real_fsync = os.fsync

        def recording_fsync(fd: int) -> None:
            self.fds.append(fd)
            real_fsync(fd)

        monkeypatch.setattr("measurement_software.core.atomic_file.os.fsync", recording_fsync)


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

        monkeypatch.setattr("measurement_software.core.atomic_file.json.dump", die_midway)
        target = tmp_path / "run.json"

        with pytest.raises(OSError, match="power lost mid-write"):
            write_json_atomically(target, [{"rat": "LTE"}])

        assert not target.exists()

    def test_previous_content_survives_an_interrupted_rewrite(self, tmp_path, monkeypatch):
        target = tmp_path / "run.json"
        write_json_atomically(target, [{"rat": "LTE"}])

        def die_before_rename(source, destination):
            raise OSError("power lost mid-rename")

        monkeypatch.setattr("measurement_software.core.atomic_file.os.replace", die_before_rename)

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
