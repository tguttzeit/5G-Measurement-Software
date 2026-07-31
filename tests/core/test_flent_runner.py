import logging
import subprocess
from pathlib import Path

import pytest

from measurement_software.core.config import LatencyTestConfig
from measurement_software.core.flent_runner import FlentRunner, parse_stats_csv

HEADER = "filename,title,series,units,datapoints,mean,median,min,max,std_dev,variance,cumul_total,pct99"


def stats_csv(*rows: str) -> str:
    return "\n".join((HEADER, *rows)) + "\n"


def series_row(series: str, units: str, mean: float, median: float, pct99: float) -> str:
    return f"data.flent.gz,2026-07-30T12:00:00,{series},{units},300,{mean},{median},1.0,99.0,2.0,4.0,0.0,{pct99}"


PING_OUTPUT = stats_csv(
    series_row("Ping (ms) ICMP", "ms", 24.5, 22.0, 61.0),
    series_row("Ping (ms) UDP", "ms", 25.5, 23.0, 63.0),
)

RRUL_OUTPUT = stats_csv(
    series_row("Ping (ms) ICMP", "ms", 130.0, 120.0, 410.0),
    series_row("Ping (ms) avg", "ms", 145.0, 140.0, 450.0),
    series_row("TCP download sum", "Mbits/s", 48.0, 47.0, 60.0),
    series_row("TCP download avg", "Mbits/s", 12.0, 11.5, 15.0),
    series_row("TCP upload sum", "Mbits/s", 9.0, 8.5, 12.0),
    series_row("TCP upload avg", "Mbits/s", 2.25, 2.1, 3.0),
)


class FakeSubprocess:
    """Stands in for the flent binary, recording the command and returning canned output."""

    def __init__(self, stdout: str = PING_OUTPUT, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.raises: Exception | None = None
        self.commands: list[list[str]] = []
        self.data_dirs_during_run: list[bool] = []

    def run(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.commands.append(command)
        self.data_dirs_during_run.append(Path(command[command.index("-D") + 1]).is_dir())
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(command, self.returncode, stdout=self.stdout, stderr="boom")


@pytest.fixture
def fake_subprocess(monkeypatch) -> FakeSubprocess:
    fake = FakeSubprocess()
    monkeypatch.setattr("measurement_software.core.flent_runner.subprocess.run", fake.run)
    return fake


def config(**overrides) -> LatencyTestConfig:
    defaults = dict(enabled=True, host="testserver.example.org", timeout_s=120.0)
    return LatencyTestConfig(**(defaults | overrides))


class TestCommand:
    def test_invokes_the_configured_test_host_and_length(self, fake_subprocess):
        FlentRunner(config()).run("rrul", 45)

        [command] = fake_subprocess.commands
        assert command[:2] == ["flent", "rrul"]
        assert command[command.index("-H") + 1] == "testserver.example.org"
        assert command[command.index("-l") + 1] == "45"

    def test_asks_for_machine_readable_statistics_on_stdout(self, fake_subprocess):
        FlentRunner(config()).run("ping", 10)

        [command] = fake_subprocess.commands
        assert command[command.index("-f") + 1] == "stats_csv"
        assert command[command.index("-o") + 1] == "-"

    def test_uses_the_configured_flent_binary(self, fake_subprocess):
        FlentRunner(config(flent_binary="/usr/local/bin/flent")).run("ping", 10)

        assert fake_subprocess.commands[0][0] == "/usr/local/bin/flent"

    def test_keeps_flents_raw_data_file_out_of_the_devices_storage(self, fake_subprocess):
        runner = FlentRunner(config())

        runner.run("ping", 10)

        # flent always writes a raw result file into its data dir, so the runner points it at a
        # scratch directory that exists for the test and is gone once the summary was taken.
        [command] = fake_subprocess.commands
        data_dir = Path(command[command.index("-D") + 1])
        assert fake_subprocess.data_dirs_during_run == [True]
        assert not data_dir.exists()


class TestRun:
    def test_summarizes_a_baseline_ping_run(self, fake_subprocess):
        fake_subprocess.stdout = PING_OUTPUT

        summary = FlentRunner(config()).run("ping", 10)

        assert summary.rtt_ms == 22.0
        assert summary.rtt_p99_ms == 61.0
        assert summary.download_mbits_s is None
        assert summary.upload_mbits_s is None

    def test_summarizes_a_load_run(self, fake_subprocess):
        fake_subprocess.stdout = RRUL_OUTPUT

        summary = FlentRunner(config()).run("rrul", 60)

        # The averaged ping series a load test reports beats the individual ICMP one.
        assert summary.rtt_ms == 140.0
        assert summary.rtt_p99_ms == 450.0
        assert summary.download_mbits_s == 48.0
        assert summary.upload_mbits_s == 9.0

    def test_reports_nothing_when_flent_is_not_installed(self, fake_subprocess, caplog):
        fake_subprocess.raises = FileNotFoundError("flent")

        with caplog.at_level(logging.WARNING):
            assert FlentRunner(config()).run("ping", 10) is None

        assert "not found" in caplog.text

    def test_reports_nothing_when_the_test_runs_past_its_timeout(self, fake_subprocess, caplog):
        fake_subprocess.raises = subprocess.TimeoutExpired(cmd="flent", timeout=120.0)

        with caplog.at_level(logging.WARNING):
            assert FlentRunner(config()).run("rrul", 60) is None

        assert "exceeded" in caplog.text

    def test_reports_nothing_when_flent_exits_with_an_error(self, fake_subprocess, caplog):
        fake_subprocess.returncode = 1

        with caplog.at_level(logging.WARNING):
            assert FlentRunner(config()).run("ping", 10) is None

        assert "failed" in caplog.text

    def test_passes_the_configured_timeout_to_the_subprocess(self, monkeypatch):
        timeouts: list[float] = []

        def record_timeout(command, **kwargs):
            timeouts.append(kwargs["timeout"])
            return subprocess.CompletedProcess(command, 0, stdout=PING_OUTPUT, stderr="")

        monkeypatch.setattr("measurement_software.core.flent_runner.subprocess.run", record_timeout)

        FlentRunner(config(timeout_s=90.0)).run("ping", 10)

        assert timeouts == [90.0]


class TestParseStatsCsv:
    def test_reports_nothing_for_output_holding_no_series(self):
        assert parse_stats_csv(stats_csv()) is None

    def test_reports_nothing_for_empty_output(self):
        assert parse_stats_csv("") is None

    def test_ignores_a_series_flent_collected_no_data_for(self):
        # flent leaves the statistics columns off entirely for a series with no data points,
        # which is what a test against an unreachable server looks like.
        output = stats_csv(
            "data.flent.gz,2026-07-30T12:00:00,Ping (ms) avg,ms,0",
            series_row("Ping (ms) ICMP", "ms", 24.5, 22.0, 61.0),
        )

        summary = parse_stats_csv(output)

        assert summary.rtt_ms == 22.0
        assert summary.rtt_p99_ms == 61.0

    def test_reports_no_round_trip_time_when_no_ping_series_has_data(self):
        summary = parse_stats_csv(stats_csv(series_row("TCP download sum", "Mbits/s", 48.0, 47.0, 60.0)))

        assert summary.rtt_ms is None
        assert summary.rtt_p99_ms is None
        assert summary.download_mbits_s == 48.0

    def test_treats_an_unparseable_value_as_no_measurement(self):
        output = stats_csv("data.flent.gz,2026-07-30T12:00:00,Ping (ms) ICMP,ms,300,nan-ish,,1.0,99.0,2.0,4.0,0.0,")

        summary = parse_stats_csv(output)

        assert summary.rtt_ms is None
        assert summary.rtt_p99_ms is None
