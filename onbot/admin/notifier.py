"""Operational reports into the admin control room (ADR-0010, review session S3).

The reconciler and the lifecycle emit what needs a human on the event bus; this subscriber posts it
into the control room as ``m.notice`` messages, which the command router ignores:

* ``destructive_actions_suppressed``: the guard paused destructive actions and proposed a batch
  (D13), with what dropped, from how many to how many, and who or what would be affected;
* ``guard_batch_closed``: a batch ran, expired or no longer applies, or a rejected batch ended;
* ``guard_state_unreadable``: the guard cannot read its state and holds every destructive action;
* ``lifecycle_attention``: an offboarding user was frozen (D9), a step failed, or the step was aborted
  on an empty Authentik answer;
* ``room_not_converged``: a managed room the pass could not fully converge (REC-03);
* ``identity_attention``: a user maps to another Matrix ID than the account they own (a rename), MAS
  disagrees with a recorded binding, several Authentik users resolve to one account, or the identity
  sources cannot be read (D8). The resolver emits each finding once per process.
* ``room_attention``: a room a server admin blocked, a lobby closed by hand (D16), an alias taken by
  another room, a marker restored after somebody removed or rewrote it. Emitted once per process.

The room must stay readable, so nothing is repeated on every pass: a room is posted again only when
its problems change (or after it converged in between), and an identical lifecycle report only once
per process. The log keeps every occurrence.

Reports raised before the control room is bound (the service starts it in the background) are queued,
up to a bound, and posted once it is.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any

from onbot.events import Event, EventBus, Signal
from onbot.logging import get_logger

log = get_logger(__name__)

NOTICE_MSGTYPE = "m.notice"
MAX_QUEUED = 50
MAX_REMEMBERED = 500

RoomKey = tuple[str, str | None, str | None]


def format_lifecycle_attention(payload: dict[str, Any]) -> str:
    mxid, action, detail = payload.get("mxid"), payload.get("action"), payload.get("detail")
    if mxid is None:
        return f"Offboarding skipped this pass: {detail}"
    if action == "freeze":
        return (
            f"Offboarding of {mxid} is on hold: {detail}. Nothing is unlocked and no further step is "
            "taken until an admin decides."
        )
    return f"Offboarding step '{action}' of {mxid} failed: {detail}. It is retried with a growing delay."


def format_room_not_converged(payload: dict[str, Any]) -> str:
    group = f", group {payload.get('group_pk')}" if payload.get("group_pk") else ""
    lines = [f"Room {payload.get('room_id')} ({payload.get('room_kind')}{group}) did not fully converge:"]
    lines += [f"- {problem}" for problem in payload.get("problems") or []]
    return "\n".join(lines)


def format_suppressed(payload: dict[str, Any]) -> str:
    summary = payload.get("summary")
    if summary:
        return str(summary)
    return f"Destructive actions paused ({payload.get('action')}): {payload.get('reason')}"


class AdminRoomNotifier:
    """Subscribes to the reports on the bus and posts them into the bound control room."""

    def __init__(self, client: Any, *, max_queued: int = MAX_QUEUED) -> None:
        self.client = client
        self.room_id: str | None = None
        self._queue: deque[str] = deque(maxlen=max_queued)
        self._rooms_posted: dict[RoomKey, tuple[str, ...]] = {}
        self._rooms_this_pass: set[RoomKey] = set()
        self._lifecycle_posted: OrderedDict[tuple[Any, ...], None] = OrderedDict()

    def subscribe(self, events: EventBus) -> None:
        events.subscribe(Signal.destructive_actions_suppressed, self._on_suppressed)
        events.subscribe(Signal.guard_batch_closed, self._on_batch_closed)
        events.subscribe(Signal.guard_state_unreadable, self._on_batch_closed)
        events.subscribe(Signal.lifecycle_attention, self._on_lifecycle_attention)
        events.subscribe(Signal.room_not_converged, self._on_room_not_converged)
        events.subscribe(Signal.identity_attention, self._on_identity_attention)
        events.subscribe(Signal.room_attention, self._on_room_attention)
        events.subscribe(Signal.reconcile_completed, self._on_reconcile_completed)

    async def bind(self, room_id: str) -> None:
        """Start posting into ``room_id``, beginning with what queued up meanwhile."""
        self.room_id = room_id
        while self._queue:
            await self._send(self._queue.popleft())

    async def post(self, text: str) -> None:
        if self.room_id is None:
            if len(self._queue) == self._queue.maxlen:
                log.warning("admin room not bound yet; dropping the oldest queued report")
            self._queue.append(text)
            return
        await self._send(text)

    async def _send(self, text: str) -> None:
        assert self.room_id is not None
        try:
            await self.client.send_text_message(self.room_id, text, msgtype=NOTICE_MSGTYPE)
        except Exception:
            # The report is in the log already; a failed post must not fail the pass that emitted it.
            log.exception("could not post a report into the admin room %s", self.room_id)

    async def _on_suppressed(self, event: Event) -> None:
        await self.post(format_suppressed(event.payload))

    async def _on_batch_closed(self, event: Event) -> None:
        await self.post(str(event.payload.get("text")))

    async def _on_lifecycle_attention(self, event: Event) -> None:
        payload = event.payload
        key = (payload.get("mxid"), payload.get("action"), payload.get("detail"))
        if key in self._lifecycle_posted:
            return
        self._lifecycle_posted[key] = None
        while len(self._lifecycle_posted) > MAX_REMEMBERED:
            self._lifecycle_posted.popitem(last=False)
        await self.post(format_lifecycle_attention(payload))

    async def _on_identity_attention(self, event: Event) -> None:
        await self.post(f"User identity: {event.payload.get('text')}")

    async def _on_room_attention(self, event: Event) -> None:
        await self.post(f"Rooms: {event.payload.get('text')}")

    async def _on_room_not_converged(self, event: Event) -> None:
        payload = event.payload
        key: RoomKey = (str(payload.get("room_kind")), payload.get("room_id"), payload.get("group_pk"))
        problems = tuple(payload.get("problems") or ())
        self._rooms_this_pass.add(key)
        if self._rooms_posted.get(key) == problems:
            return
        self._rooms_posted[key] = problems
        await self.post(format_room_not_converged(payload))

    async def _on_reconcile_completed(self, event: Event) -> None:
        # A room that converged in this pass is news again the next time it breaks.
        for key in set(self._rooms_posted) - self._rooms_this_pass:
            del self._rooms_posted[key]
        self._rooms_this_pass = set()
