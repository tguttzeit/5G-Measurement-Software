from dataclasses import dataclass

from measurement_software.gnss.gnss_receiver import GNSSFix
from measurement_software.modems.modem import CellSample


@dataclass
class Datapoint:
    """A single cell-info sample paired with the GNSS fix and timestamp it was captured at."""

    timestamp: str
    device_id: str
    mission_type: str
    fix: GNSSFix
    cell_sample: CellSample
