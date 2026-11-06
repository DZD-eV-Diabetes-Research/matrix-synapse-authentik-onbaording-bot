"""Structured logging setup.

A thin, dependency-light wrapper over the stdlib ``logging`` module. Phase 2 keeps this
minimal; a structured-logging backend can be slotted in later without changing call sites.
"""

from __future__ import annotations

import logging
import os

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Third-party loggers that log once per HTTP request.
_HTTP_LOGGERS = ("httpx", "httpcore")


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
    if not isinstance(level, int):
        # An unknown ONBOT_LOG_LEVEL: log at INFO until the config is loaded, whose validation then
        # reports the bad value properly instead of basicConfig crashing with a traceback (MNT-03).
        level = logging.INFO
    logging.basicConfig(level=level, format=_DEFAULT_FORMAT, force=True)
    # httpx logs every request at INFO, which buries the bot's own lines (one per sync long-poll,
    # one per room state read; RUN-15). Their request lines only show at DEBUG.
    http_level = logging.NOTSET if level <= logging.DEBUG else logging.WARNING
    for name in _HTTP_LOGGERS:
        logging.getLogger(name).setLevel(http_level)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger; prefer ``get_logger(__name__)`` at call sites."""
    return logging.getLogger(name)
