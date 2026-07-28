import json
import os
from pathlib import Path

import pytest

from measurement_software.core.datapoint import Datapoint
from measurement_software.core.run_log import RUN_FILE_PREFIX, RUN_LOG_SUFFIX, RunLog
from measurement_software.gnss.gnss_receiver import GNSSFix, Position
from measurement_software.modems.modem import CellSample


def make_datapoint(rat: str = "LTE", latitude: float = 1.0) -> Datapoint:
    fix = GNSSFix(position=Position(latitude=latitude, longitude=2.0, altitude=3.0), num_satellites=7)
    return Datapoint(timestamp="2026-07-27T00:00:00Z", fix=fix, cell_sample=CellSample(rat=rat))


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def cut_power_mid_append(path: Path, keep_bytes: int) -> None:
    """Simulates a power cut partway through an append by truncating the log's last line."""
    with open(path, "r+") as f:
        f.truncate(keep_bytes)


@pytest.fixture
def run_log(tmp_path) -> RunLog:
    return RunLog(tmp_path / "queue")


class TestOpen:
    def test_creates_the_directory_and_a_timestamped_log_file(self, run_log, tmp_path):
        run_log.open()
        run_log.close()

        assert (tmp_path / "queue").is_dir()
        assert run_log.path.name.startswith(RUN_FILE_PREFIX)
        assert run_log.path.suffix == RUN_LOG_SUFFIX
        assert run_log.path.exists()

    def test_path_is_none_before_the_log_is_opened(self, run_log):
        assert run_log.path is None

    def test_log_file_is_not_picked_up_by_the_uploader_glob(self, run_log):
        # Uploader.upload_pending_files() globs "*.json"; an in-progress run must never match it.
        run_log.open()
        run_log.append([make_datapoint()])
        run_log.close()

        assert list(run_log.path.parent.glob("*.json")) == []


class TestAppend:
    def test_writes_one_json_object_per_datapoint(self, run_log):
        run_log.open()
        run_log.append([make_datapoint(rat="LTE"), make_datapoint(rat="NR5G-SA")])
        run_log.close()

        lines = read_lines(run_log.path)
        assert [line["cell_sample"]["rat"] for line in lines] == ["LTE", "NR5G-SA"]
        assert lines[0]["fix"]["position"]["latitude"] == 1.0
        assert lines[0]["fix"]["num_satellites"] == 7
        assert lines[0]["timestamp"] == "2026-07-27T00:00:00Z"

    def test_appends_across_calls_without_rewriting_earlier_lines(self, run_log):
        run_log.open()
        run_log.append([make_datapoint(latitude=1.0)])
        run_log.append([make_datapoint(latitude=2.0)])
        run_log.close()

        assert [line["fix"]["position"]["latitude"] for line in read_lines(run_log.path)] == [1.0, 2.0]

    def test_writes_nothing_when_there_are_no_datapoints(self, run_log):
        run_log.open()
        run_log.append([])
        run_log.close()

        assert run_log.path.read_text() == ""

    def test_raises_when_the_log_was_never_opened(self, run_log):
        with pytest.raises(RuntimeError, match="must be opened"):
            run_log.append([make_datapoint()])


class TestDurability:
    def test_datapoints_are_on_disk_before_the_log_is_closed(self, run_log):
        # This is the whole point: a run that never gets to close() must still have
        # every already-captured datapoint readable on disk.
        run_log.open()
        run_log.append([make_datapoint(rat="LTE")])

        assert [line["cell_sample"]["rat"] for line in read_lines(run_log.path)] == ["LTE"]

    def test_every_append_is_fsynced(self, run_log, monkeypatch):
        real_fsync = os.fsync
        synced_fds: list[int] = []

        def recording_fsync(fd: int) -> None:
            synced_fds.append(fd)
            real_fsync(fd)

        run_log.open()
        monkeypatch.setattr("measurement_software.core.atomic_file.os.fsync", recording_fsync)

        run_log.append([make_datapoint()])
        run_log.append([make_datapoint()])

        assert len(synced_fds) == 2

    def test_an_empty_append_costs_no_fsync(self, run_log, monkeypatch):
        run_log.open()
        synced_fds: list[int] = []
        monkeypatch.setattr("measurement_software.core.atomic_file.os.fsync", synced_fds.append)

        run_log.append([])

        assert synced_fds == []

    def test_a_power_cut_mid_append_damages_only_the_last_line(self, run_log):
        run_log.open()
        run_log.append([make_datapoint(latitude=1.0)])
        run_log.append([make_datapoint(latitude=2.0)])
        intact_bytes = run_log.path.stat().st_size
        run_log.append([make_datapoint(latitude=3.0)])
        run_log.close()

        cut_power_mid_append(run_log.path, intact_bytes + 20)

        lines = run_log.path.read_text().splitlines()
        assert [json.loads(line)["fix"]["position"]["latitude"] for line in lines[:2]] == [1.0, 2.0]
        with pytest.raises(json.JSONDecodeError):
            json.loads(lines[2])
