import csv
import logging
import subprocess
import tempfile
from dataclasses import dataclass

from measurement_software.core.config import LatencyTestConfig

STATS_FORMAT = "stats_csv"

RTT_SERIES = ("Ping (ms) avg", "Ping (ms) ICMP")
DOWNLOAD_SERIES = ("TCP download sum", "TCP download avg")
UPLOAD_SERIES = ("TCP upload sum", "TCP upload avg")


@dataclass(frozen=True)
class FlentSummary:
    """The few numbers kept from a flent run, distilled from its per-series statistics.

    flent's own output is one row per traffic flow per test, which is far more than a measurement
    campaign correlating latency with position needs. Round-trip times are taken as median and
    99th percentile, since a mean hides exactly the tail that a loaded link produces; throughput
    is taken as a mean, being a rate over the run.
    """

    rtt_ms: float | None = None
    rtt_p99_ms: float | None = None
    download_mbits_s: float | None = None
    upload_mbits_s: float | None = None


class FlentRunner:
    """Runs a single flent test as a subprocess and distills its statistics into a summary."""

    def __init__(self, config: LatencyTestConfig):
        self._logger = logging.getLogger(__name__)
        self._binary = config.flent_binary
        self._host = config.host
        self._timeout_s = config.timeout_s

    def run(self, test: str, length_s: int) -> FlentSummary | None:
        """Runs one flent test, returning its summary, or None if the test produced no numbers.

        A test that cannot run - flent missing, the server unreachable, the link dying
        mid-test - is a gap in the latency series, never a reason to take the measurement
        run down with it.
        """
        with tempfile.TemporaryDirectory() as data_dir:
            command = self._build_command(test, length_s, data_dir)
            self._logger.info("Starting %s latency test against %s", test, self._host)
            self._logger.debug("flent command: %s", " ".join(command))
            try:
                proc = subprocess.run(
                    command, capture_output=True, text=True, timeout=self._timeout_s
                )
            except FileNotFoundError:
                self._logger.warning("flent binary %r not found - skipping latency test.", self._binary)
                return None
            except subprocess.TimeoutExpired:
                self._logger.warning("%s latency test exceeded %s s - skipping it.", test, self._timeout_s)
                return None

        if proc.returncode != 0:
            self._logger.warning("%s latency test failed (rc=%s)", test, proc.returncode)
            if proc.stderr.strip():
                self._logger.debug("flent stderr:\n%s", proc.stderr.strip())
            return None

        return parse_stats_csv(proc.stdout)

    def _build_command(self, test: str, length_s: int, data_dir: str) -> list[str]:
        """Builds the flent invocation, sending statistics to stdout and raw data to a scratch directory.

        flent always writes its full raw result file somewhere; pointing that at a temporary
        directory keeps it off the device's storage, which the upload backlog already has to share.
        """
        return [
            self._binary,
            test,
            "-H", self._host,
            "-l", str(length_s),
            "-f", STATS_FORMAT,
            "-o", "-",
            "-D", data_dir,
        ]


def parse_stats_csv(output: str) -> FlentSummary | None:
    """Distills flent's stats_csv output into a summary, or None if it holds no usable series."""
    statistics = {row["series"]: row for row in csv.DictReader(output.splitlines()) if row.get("series")}
    if not statistics:
        return None

    return FlentSummary(
        rtt_ms=_statistic(statistics, RTT_SERIES, "median"),
        rtt_p99_ms=_statistic(statistics, RTT_SERIES, "pct99"),
        download_mbits_s=_statistic(statistics, DOWNLOAD_SERIES, "mean"),
        upload_mbits_s=_statistic(statistics, UPLOAD_SERIES, "mean"),
    )


def _statistic(statistics: dict[str, dict], series_names: tuple[str, ...], column: str) -> float | None:
    """Reads one statistic from the first of the given series that flent reported a value for.

    Which series exist depends on the test: a load test reports an average across all its
    ping flows, while a plain ping test only has the individual ones.
    """
    for name in series_names:
        value = _to_float(statistics.get(name, {}).get(column))
        if value is not None:
            return value
    return None


def _to_float(value: str | None) -> float | None:
    """Converts a statistics cell to a number, treating anything unparseable as no measurement.

    flent leaves the statistics columns off entirely for a series that collected no data,
    which is what a test against an unreachable server looks like.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
