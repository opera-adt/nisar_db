"""Shared logging configuration for nisar_db command-line entry points."""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def configure_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    """Apply the package-wide log format and return a named logger.

    Parameters
    ----------
    name : str
        Logger name (typically the entry-point / module name).
    level : int
        Root logging level to configure.

    """
    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_DATE_FORMAT)
    return logging.getLogger(name)
