from typing import Callable

from measurement_software.core.config import CollectorConfig
from measurement_software.core.run_status import RunStatus

# Each entry names a testing/non-default config flag and how to tell it's active. Adding a
# future testing flag (e.g. a debug sampling mode) is one more entry here, not a change to
# how the display renders them.
_SPECIAL_CONFIG_FLAGS: list[tuple[str, Callable[[CollectorConfig], bool]]] = [
    ("GPS disabled", lambda collector_config: not collector_config.gps_enabled),
]

def active_special_config_flags(collector_config: CollectorConfig) -> list[str]:
    """Returns the labels of every testing/non-default config flag currently active."""
    return [label for label, is_active in _SPECIAL_CONFIG_FLAGS if is_active(collector_config)]

def build_status_message(phase: str, run_status: RunStatus, special_config_flags: list[str]) -> str:
    """Renders the display's content: lifecycle phase, data-quality summary, and special config.

    Reuses RunStatus's own cumulative counters - the same ones the backend heartbeat reports -
    rather than computing quality separately, so the display and the heartbeat always agree.
    """
    lines = [
        phase,
        f"OK {run_status.good}  Bad {run_status.bad}  Inv {run_status.invalid}",
        f"Total {run_status.datapoints_total}",
    ]
    if run_status.pipeline_broken:
        lines.append("PIPELINE BROKEN")
    lines.append(", ".join(special_config_flags) if special_config_flags else "No special config")
    return "\n".join(lines)
