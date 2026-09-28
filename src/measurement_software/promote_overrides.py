import argparse
import logging
import sys
from pathlib import Path

from measurement_software.core.config_sources import promote_overrides

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses promote_overrides CLI arguments - there are none beyond --help.

    Promotes every field currently set in config_overrides.toml in one pass; promoting only
    some of them is a non-goal (see the originating issue).
    """
    parser = argparse.ArgumentParser(
        prog="python -m measurement_software.promote_overrides",
        description=(
            "Bakes every currently-set config_overrides.toml field into config.toml as the new "
            "device baseline, then clears those fields from config_overrides.toml. Reads "
            "config.toml's path the same way the main application does - no separate "
            "configuration. Takes effect from the next load_config() call (the next boot), same "
            "as every other config_overrides.toml change."
        ),
    )
    return parser.parse_args(argv)


def config_toml_path() -> Path:
    """Locates config.toml the same way main.py does - no separate configuration."""
    return Path(__file__).resolve().parent.parent.parent / "config.toml"


def main(argv: list[str] | None = None) -> int:
    """Runs one promotion pass and prints what changed. Always returns 0 - promote_overrides()
    itself already logs anything that couldn't be promoted rather than raising."""
    parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

    promoted = promote_overrides(config_toml_path())
    if not promoted:
        print("Nothing to promote - config_overrides.toml has no fields set.")
        return 0

    for field, (old_value, new_value) in promoted.items():
        print(f"Promoted {field}: {old_value!r} -> {new_value!r} (cleared from config_overrides.toml)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
