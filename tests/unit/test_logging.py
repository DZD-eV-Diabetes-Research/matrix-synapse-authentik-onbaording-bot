"""Logging setup (RUN-15): httpx and httpcore request lines only at DEBUG."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from onbot.logging import configure_logging


@pytest.fixture(autouse=True)
def _restore_levels() -> Iterator[None]:
    saved = {name: logging.getLogger(name).level for name in ("", "httpx", "httpcore")}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("level", ["INFO", "WARNING"])
def test_http_request_lines_are_hidden_above_debug(level: str) -> None:
    """Before the fix httpx wrote one INFO line per request, two a minute for the idle sync alone."""
    configure_logging(level)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_http_request_lines_show_at_debug() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")  # the CLI configures twice: before and after reading the config
    assert logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert logging.getLogger("httpcore").isEnabledFor(logging.DEBUG)
