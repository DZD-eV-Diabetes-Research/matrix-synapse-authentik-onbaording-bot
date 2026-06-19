"""Tiny async signal bus (AD-4).

Explicit, in-process coupling between the bounded domains: the reconciler emits signals (e.g. a
user was synced) and onboarding (Phase 4) subscribes. Kept deliberately minimal — no broker, no
persistence — but designed so a domain could later move to its own process behind the same API.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from onbot.logging import get_logger

log = get_logger(__name__)


class Signal(StrEnum):
    user_synced = "user_synced"
    drift_detected = "drift_detected"


@dataclass(frozen=True, slots=True)
class Event:
    signal: Signal
    payload: dict[str, Any]


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[Signal, list[Handler]] = defaultdict(list)

    def subscribe(self, signal: Signal, handler: Handler) -> None:
        self._handlers[signal].append(handler)

    async def emit(self, signal: Signal, **payload: Any) -> None:
        handlers = self._handlers.get(signal, [])
        if not handlers:
            return
        event = Event(signal=signal, payload=payload)
        results = await asyncio.gather(*(h(event) for h in handlers), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.exception("event handler for %s failed", signal, exc_info=result)
