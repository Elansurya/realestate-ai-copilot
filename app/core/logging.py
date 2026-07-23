"""
Centralized logging configuration.

Enterprise applications need consistent, structured logging across
all modules instead of ad-hoc `print()` statements. This module
configures Python's standard `logging` library once, at application
startup, and every other module simply calls:

    import logging
    logger = logging.getLogger(__name__)
"""

import logging
import sys

from app.core.config import settings

# Log format: timestamp | level | logger name | message
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """
    Configures the root logger for the entire application.

    Called once during FastAPI startup (see app/main.py). Sends
    all log records to stdout so that container orchestrators
    (Docker, Kubernetes, etc.) can capture logs natively.
    """
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove any pre-existing handlers to avoid duplicate log lines
    # when this function is called more than once (e.g. in tests).
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT))
    root_logger.addHandler(handler)

    # Tame overly-verbose third-party loggers in non-debug environments.
    if not settings.DEBUG:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging configured | level=%s | environment=%s",
        settings.LOG_LEVEL,
        settings.ENVIRONMENT,
    )