from dataclasses import dataclass
from enum import StrEnum

from measurement_software.gnss.gnss_receiver import GNSSFix

LATENCY_FILE_PREFIX = "latency_"


class LatencyTestType(StrEnum):
    """Which of the two kinds of latency measurement a result came from.

    BASELINE is the latency of an otherwise idle link; UNDER_LOAD is the latency while the
    test itself saturates that link. They answer different questions and run on their own
    cadences, so a result has to say which one it is.
    """

    BASELINE = "baseline"
    UNDER_LOAD = "under_load"


@dataclass
class LatencyResult:
    """One completed latency test, covering the stretch of road the vehicle drove while it ran.

    Deliberately not part of a Datapoint (see decision record 0004): a datapoint is an instant -
    one fix, one cell reading, taken together - while a latency test spans tens of seconds and,
    since it only runs while the vehicle is moving, real distance. Hence a start and an end fix
    rather than a single position.
    """

    test_type: LatencyTestType
    flent_test: str
    device_id: str
    mission_type: str
    start_timestamp: str
    end_timestamp: str
    start_fix: GNSSFix
    end_fix: GNSSFix
    baseline_rtt_ms: float | None
    rtt_under_load_ms: float | None
    rtt_p99_ms: float | None
    download_mbits_s: float | None
    upload_mbits_s: float | None
