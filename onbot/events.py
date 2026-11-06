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
    # Emitted at the end of every reconcile pass, so state that must converge on the tick but is not
    # the reconciler's business can ride along (the admin room's invites, ADR-0010).
    reconcile_completed = "reconcile_completed"
    # The offboarding step needs a human: a user was frozen (D9), a step failed, the step was aborted
    # on an implausible Authentik answer or an unreadable ledger, a ledger entry was quarantined, the
    # ledger went missing while locks exist, or the startup check found a problem (action
    # ``preflight``). Payload: ``mxid`` (None when no single account is meant), ``action``,
    # ``detail``. Routed to the admin room (``onbot.admin.notifier``); the lifecycle also logs it.
    lifecycle_attention = "lifecycle_attention"
    # A managed room (or the space) the pass could not fully converge: emitted once per room per pass,
    # with ``room_kind``, ``room_id``, ``group_pk`` and ``problems`` (a list of one-line strings).
    # Routed to the admin room, which repeats a room only when its problems change.
    room_not_converged = "room_not_converged"
    # The destructive-action guard paused actions and proposed a new batch for an admin to confirm
    # (D13): ``action`` (the kinds, comma-separated), ``reason``, ``batch_id`` and ``summary`` (the
    # full report). Emitted once per batch, not on every pass that keeps the actions paused.
    destructive_actions_suppressed = "destructive_actions_suppressed"
    # A guard batch was closed: ``batch_id``, ``outcome`` (executed, expired, obsolete,
    # rejection_ended) and ``text``.
    guard_batch_closed = "guard_batch_closed"
    # The guard cannot read its stored state and holds every destructive action until an admin
    # replaces it (``!guard reset``): ``detail`` (the problem) and ``text`` (the report). Emitted once
    # per problem per process, not on every pass.
    guard_state_unreadable = "guard_state_unreadable"
    # User identity needs a human (D8, ``onbot.identity``): a user maps to another Matrix ID than
    # the one they are bound to (a rename, a changed mapping attribute), MAS links a user to another
    # account than the recorded binding, several Authentik users resolve to one Matrix account (all
    # of them are skipped), or MAS or the binding record cannot be read. Payload: ``text``. Each
    # finding is emitted once per process, not on every pass.
    identity_attention = "identity_attention"
    # A managed room needs a human, or a human changed it and the bot now leaves it alone (S7): a room
    # a server admin blocked (REC-11), a visitor lobby closed by hand (D16), a room's alias taken by
    # another room, an onbot marker or its lock that somebody else removed or rewrote (restored).
    # Payload: ``text``. Each finding is emitted once per process, not on every pass.
    room_attention = "room_attention"


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
