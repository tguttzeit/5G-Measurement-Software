import json
import os
from pathlib import Path

import pytest

from measurement_software.core.util import TEMP_SUFFIX, write_json_atomically
from measurement_software.core.datapoint import Datapoint
from measurement_software.core.run_log import (
    RUN_FILE_PREFIX,
    RUN_LOG_SUFFIX,
    RunLog,
    discard_stale_temp_files,
    finalize,
    recover_unfinalized,
)
from measurement_software.gnss.gnss_receiver import GNSSFix, Position
from measurement_software.modems.modem import CellSample


def make_datapoint(rat: str = "LTE", latitude: float = 1.0) -> Datapoint:
    fix = GNSSFix(position=Position(latitude=latitude, longitude=2.0, altitude=3.0), num_satellites=7)
    return Datapoint(
        timestamp="2026-07-27T00:00:00Z",
        device_id="test-device",
        mission_type="ground",
        fix=fix,
        cell_sample=CellSample(rat=rat),
    )


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


class TestSeparateLogs:
    def test_a_prefix_gives_a_second_kind_of_measurement_its_own_log_and_upload_file(self, tmp_path):
        other = RunLog(tmp_path, prefix="latency_")
        other.open()
        other.append([make_datapoint()])
        other.close()

        assert other.path.name.startswith("latency_")
        assert finalize(other.path).name.startswith("latency_")

    def test_logs_of_either_kind_are_recovered_after_a_run_that_never_finished(self, tmp_path):
        write_run_log(tmp_path, [1.0])
        other = RunLog(tmp_path, prefix="latency_")
        other.open()
        other.append([make_datapoint(latitude=2.0)])
        other.close()

        recovered = recover_unfinalized(tmp_path)

        assert len(recovered) == 2
        assert any(p.name.startswith(RUN_FILE_PREFIX) for p in recovered)
        assert any(p.name.startswith("latency_") for p in recovered)
        assert list(tmp_path.glob(f"*{RUN_LOG_SUFFIX}")) == []


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
        monkeypatch.setattr("measurement_software.core.util.os.fsync", recording_fsync)

        run_log.append([make_datapoint()])
        run_log.append([make_datapoint()])

        assert len(synced_fds) == 2

    def test_an_empty_append_costs_no_fsync(self, run_log, monkeypatch):
        run_log.open()
        synced_fds: list[int] = []
        monkeypatch.setattr("measurement_software.core.util.os.fsync", synced_fds.append)

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


def write_run_log(
    directory: Path, latitudes: list[float], trailing_partial: bool = False, name: str | None = None
) -> Path:
    """Writes a run log as RunLog would have left it, optionally cut short mid-line by a power loss."""
    log = RunLog(directory)
    log.open()
    for latitude in latitudes:
        log.append([make_datapoint(latitude=latitude)])
    log.close()

    path = log.path if name is None else log.path.rename(directory / name)
    if trailing_partial:
        with open(path, "a") as f:
            f.write('{"timestamp": "2026-07-27T00:00:00Z", "fix": {"position": {"lat')
    return path


class TestFinalize:
    def test_converts_the_log_into_a_json_array_file(self, tmp_path):
        log_path = write_run_log(tmp_path, [1.0, 2.0])

        upload_path = finalize(log_path)

        assert upload_path == log_path.with_suffix(".json")
        content = json.loads(upload_path.read_text())
        assert [dp["fix"]["position"]["latitude"] for dp in content] == [1.0, 2.0]
        assert content[0]["cell_sample"]["rat"] == "LTE"

    def test_removes_the_run_log_once_the_converted_file_exists(self, tmp_path):
        log_path = write_run_log(tmp_path, [1.0])

        finalize(log_path)

        assert not log_path.exists()

    def test_converted_file_is_the_only_thing_left_for_the_uploader(self, tmp_path):
        log_path = write_run_log(tmp_path, [1.0])

        finalize(log_path)

        assert [p.name for p in tmp_path.iterdir()] == [log_path.with_suffix(".json").name]

    def test_keeps_every_complete_line_when_the_last_one_was_cut_short(self, tmp_path):
        log_path = write_run_log(tmp_path, [1.0, 2.0], trailing_partial=True)

        upload_path = finalize(log_path)

        content = json.loads(upload_path.read_text())
        assert [dp["fix"]["position"]["latitude"] for dp in content] == [1.0, 2.0]

    def test_discards_a_log_holding_nothing_but_a_partial_line(self, tmp_path):
        log_path = write_run_log(tmp_path, [], trailing_partial=True)

        assert finalize(log_path) is None
        assert not log_path.exists()
        assert list(tmp_path.glob("*.json")) == []

    def test_ignores_a_blank_line_without_reporting_it_as_damage(self, tmp_path, caplog):
        log_path = write_run_log(tmp_path, [1.0])
        with open(log_path, "a") as f:
            f.write("\n")

        upload_path = finalize(log_path)

        assert len(json.loads(upload_path.read_text())) == 1
        assert "incomplete" not in caplog.text

    def test_discards_an_empty_log_without_writing_an_empty_upload_file(self, tmp_path):
        log_path = write_run_log(tmp_path, [])

        assert finalize(log_path) is None
        assert not log_path.exists()
        assert list(tmp_path.glob("*.json")) == []


class TestFinalizeInterrupted:
    def test_leaves_no_upload_file_when_the_conversion_is_cut_short(self, tmp_path, monkeypatch):
        log_path = write_run_log(tmp_path, [1.0, 2.0])

        def die_midway(payload, f, **kwargs):
            f.write('[{"timestamp": "2026-07')
            raise OSError("power lost mid-write")

        monkeypatch.setattr("measurement_software.core.util.json.dump", die_midway)

        with pytest.raises(OSError, match="power lost mid-write"):
            finalize(log_path)

        assert list(tmp_path.glob("*.json")) == []

    def test_the_run_survives_an_interrupted_conversion_and_finalizes_on_the_next_try(self, tmp_path, monkeypatch):
        log_path = write_run_log(tmp_path, [1.0, 2.0])

        def die_before_rename(source, destination):
            raise OSError("power lost mid-rename")

        monkeypatch.setattr("measurement_software.core.util.os.replace", die_before_rename)
        with pytest.raises(OSError, match="power lost mid-rename"):
            finalize(log_path)

        assert log_path.exists()
        monkeypatch.undo()

        upload_path = finalize(log_path)

        content = json.loads(upload_path.read_text())
        assert [dp["fix"]["position"]["latitude"] for dp in content] == [1.0, 2.0]

    def test_finalizing_again_after_a_cut_between_rename_and_cleanup_is_harmless(self, tmp_path):
        # A power cut can land after the upload file is in place but before the run log
        # is removed, so the next run finds both and must simply redo the conversion.
        log_path = write_run_log(tmp_path, [1.0, 2.0])
        upload_path = log_path.with_suffix(".json")
        write_json_atomically(upload_path, [{"stale": True}])

        assert finalize(log_path) == upload_path

        content = json.loads(upload_path.read_text())
        assert [dp["fix"]["position"]["latitude"] for dp in content] == [1.0, 2.0]
        assert not log_path.exists()


class TestRecoverUnfinalized:
    def test_finalizes_a_log_left_behind_by_a_run_that_never_finished(self, tmp_path):
        log_path = write_run_log(tmp_path, [1.0, 2.0], trailing_partial=True)

        [upload_path] = recover_unfinalized(tmp_path)

        content = json.loads(upload_path.read_text())
        assert [dp["fix"]["position"]["latitude"] for dp in content] == [1.0, 2.0]
        assert not log_path.exists()

    def test_recovers_every_leftover_log_in_chronological_order(self, tmp_path):
        write_run_log(tmp_path, [2.0], name=f"{RUN_FILE_PREFIX}20260102_000000{RUN_LOG_SUFFIX}")
        write_run_log(tmp_path, [1.0], name=f"{RUN_FILE_PREFIX}20260101_000000{RUN_LOG_SUFFIX}")

        recovered = recover_unfinalized(tmp_path)

        assert [p.name for p in recovered] == [
            f"{RUN_FILE_PREFIX}20260101_000000.json",
            f"{RUN_FILE_PREFIX}20260102_000000.json",
        ]
        assert list(tmp_path.glob(f"*{RUN_LOG_SUFFIX}")) == []

    def test_reports_nothing_when_the_previous_run_finalized_cleanly(self, tmp_path):
        already_uploaded = tmp_path / f"{RUN_FILE_PREFIX}20260101_000000.json"
        write_json_atomically(already_uploaded, [{"rat": "LTE"}])

        assert recover_unfinalized(tmp_path) == []
        assert already_uploaded.exists()

    def test_reports_nothing_when_the_upload_directory_does_not_exist_yet(self, tmp_path):
        assert recover_unfinalized(tmp_path / "missing") == []

    def test_a_log_holding_only_a_partial_line_is_discarded_rather_than_recovered(self, tmp_path):
        log_path = write_run_log(tmp_path, [], trailing_partial=True)

        assert recover_unfinalized(tmp_path) == []
        assert not log_path.exists()


class TestDiscardStaleTempFiles:
    def test_removes_a_temp_file_an_interrupted_finalization_left_behind(self, tmp_path):
        stale = tmp_path / f"{RUN_FILE_PREFIX}20260101_000000.json{TEMP_SUFFIX}"
        stale.write_text('[{"timestamp": "2026-07')

        discard_stale_temp_files(tmp_path)

        assert not stale.exists()

    def test_leaves_run_logs_and_finished_upload_files_alone(self, tmp_path):
        log_path = write_run_log(tmp_path, [1.0], name=f"{RUN_FILE_PREFIX}20260101_000000{RUN_LOG_SUFFIX}")
        upload_path = tmp_path / f"{RUN_FILE_PREFIX}20260102_000000.json"
        write_json_atomically(upload_path, [{"rat": "LTE"}])

        discard_stale_temp_files(tmp_path)

        assert log_path.exists()
        assert upload_path.exists()

    def test_does_nothing_when_the_upload_directory_does_not_exist_yet(self, tmp_path):
        discard_stale_temp_files(tmp_path / "missing")
