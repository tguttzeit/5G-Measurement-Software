"""Captures a timed series of modem cell measurements to a JSON file.

Intended for comparing antenna configurations (e.g. the external antenna vs. the Quectel board's
built-in antennas) against each other: run this once per configuration under test, stationary,
then compare the resulting files in a notebook or script of your choice.

Usage:
    uv run python scripts/capture_antenna_samples.py --label external --output external.json
"""

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from measurement_software.core.config import load_config
from measurement_software.core.util import utc_timestamp
from measurement_software.modems import create_modem
from measurement_software.modems.modem import CellSample, Modem

logger = logging.getLogger("capture_antenna_samples")

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class AntennaSample:
    """One cell measurement captured during an antenna comparison run."""

    timestamp: str
    label: str
    cell_sample: CellSample


def capture(
    modem: Modem,
    label: str,
    duration_s: float,
    interval_s: float,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[AntennaSample]:
    """Opens modem and polls query_cell_info() every interval_s until duration_s elapses.

    A query that raises is logged and skipped rather than aborting the whole run. On
    KeyboardInterrupt, whatever samples were captured so far are returned rather than discarded -
    a run cut short in the field shouldn't lose everything before it. The modem is always closed,
    on every exit path.
    """
    samples: list[AntennaSample] = []
    modem.open()
    try:
        deadline = time_fn() + duration_s
        while time_fn() < deadline:
            loop_start = time_fn()
            try:
                cell_samples = modem.query_cell_info()
            except Exception:
                logger.exception("query_cell_info() failed; skipping this sample.")
                cell_samples = []

            timestamp = utc_timestamp()
            for cell_sample in cell_samples:
                samples.append(AntennaSample(timestamp=timestamp, label=label, cell_sample=cell_sample))
            logger.info("Captured %d cell sample(s) (%d total so far).", len(cell_samples), len(samples))

            sleep_fn(max(0.0, interval_s - (time_fn() - loop_start)))
    except KeyboardInterrupt:
        logger.warning("Interrupted - keeping %d sample(s) captured so far.", len(samples))
    finally:
        modem.close()
    return samples


def save(samples: list[AntennaSample], output_path: Path) -> None:
    """Writes samples as a JSON array, creating output_path's parent directory if needed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump([asdict(sample) for sample in samples], f, indent=2)
    logger.info("Wrote %d sample(s) to %s", len(samples), output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label", required=True,
        help='Name for the antenna configuration under test, e.g. "external" or "board".',
    )
    parser.add_argument("--output", required=True, type=Path, help="Path to write captured samples as JSON.")
    parser.add_argument("--duration", type=float, default=300.0, help="How long to capture, in seconds (default: 300).")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between modem queries (default: 2).")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "config.toml",
        help="Path to config.toml (default: repo root config.toml).",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    config = load_config(args.config)
    modem = create_modem(config.modem)
    samples = capture(modem, args.label, args.duration, args.interval)
    save(samples, args.output)


if __name__ == "__main__":
    main()
