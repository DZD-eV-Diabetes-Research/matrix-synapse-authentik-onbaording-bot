"""Structured logging setup.

A thin, dependency-light wrapper over the stdlib ``logging`` module. Phase 2 keeps this
minimal; a structured-logging backend can be slotted in later without changing call sites.
"""

from __future__ import annotations

import logging
import os

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: str | int | None = None) -> None:
    """Configure root logging once, honouring ``ONBOT_LOG_LEVEL`` when ``level`` is unset."""
    if level is None:
        level = os.environ.get("ONBOT_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    logging.basicConfig(level=level, format=_DEFAULT_FORMAT)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger; prefer ``get_logger(__name__)`` at call sites."""
    return logging.getLogger(name)
