import logging
import logging.config
from pathlib import Path

from measurement_software.core.config import LoggingConfig


def setup_logging(config: LoggingConfig) -> None:
    """Configures root logging with rotating-file and console handlers per the given config."""
    log_path = Path(config.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_path),
                "maxBytes": config.max_bytes,
                "backupCount": config.backup_count,
                "formatter": "default",
            },
        },
        "root": {
            "level": config.level,
            "handlers": ["console", "file"],
        },
    })