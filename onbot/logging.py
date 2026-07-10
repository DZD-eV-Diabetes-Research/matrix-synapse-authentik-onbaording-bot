"""Structured logging setup.

A thin, dependency-light wrapper over the stdlib ``logging`` module. Phase 2 keeps this
minimal; a structured-logging backend can be slotted in later without changing call sites.
"""

from __future__ import annotations

import logging
import os

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: str | int | None = None) -> None:
    """Configure root logging, honouring ``ONBOT_LOG_LEVEL`` when ``level`` is unset.

    Safe to call more than once: the CLI configures logging before the config file is readable, then
    re-applies the loaded ``log_level``. ``force`` makes the second call take effect (``basicConfig``
    is otherwise a no-op once the root logger has handlers).
    """
    if level is None:
        level = os.environ.get("ONBOT_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    logging.basicConfig(level=level, format=_DEFAULT_FORMAT, force=True)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger; prefer ``get_logger(__name__)`` at call sites."""
    return logging.getLogger(name)
