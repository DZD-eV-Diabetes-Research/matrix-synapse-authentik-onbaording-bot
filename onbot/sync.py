"""The single sliding-sync connection, fanned out to whoever needs it.

Two features now read the Matrix event stream: onboarding welcomes users who join
(:class:`~onbot.onboarding.listener.OnboardingListener`) and the admin control room routes commands
addressed to the bot (:class:`~onbot.admin.control_room.ControlRoomHandler`). They want different
event types off the *same* stream, and opening a second sync connection to get them would be wrong:
each connection carries its own stream position, so the two would drift and each would pay the
long-poll cost separately.

So the loop lives here instead of inside either consumer. :class:`SyncPump` owns the stream position,
the error backoff and the stop event, and hands every :class:`~onbot.clients.matrix.SyncResult` to
each registered handler in turn. Handlers are isolated from one another: one that raises is logged
and the next still runs, because a broken command router must not stop new employees being welcomed.

The pump does **not** own replay protection. It starts at ``pos=None`` and the server then replays up
to ``timeline_limit`` events per room, so every handler sees old events on each restart. Onboarding
tolerates this because welcoming is idempotent; the control room must not, and guards itself (see
:mod:`onbot.admin.control_room`).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from onbot.clients.matrix import ApiClientMatrix, SyncNotSupportedError, SyncResult
from onbot.logging import get_logger

log = get_logger(__name__)

ERROR_BACKOFF_SEC = 5.0


class SyncHandler(Protocol):
    """Anything that wants a look at each sync slice."""

    async def handle_sync(self, result: SyncResult) -> None: ...


class SyncPump:
    """Drive Simplified Sliding Sync and fan each slice out to the registered handlers."""

    def __init__(self, client: ApiClientMatrix, *, error_backoff_sec: float = ERROR_BACKOFF_SEC) -> None:
        self.client = client
        self._handlers: list[SyncHandler] = []
        self._pos: str | None = None
        self._stop = asyncio.Event()
        self._error_backoff_sec = error_backoff_sec

    def register(self, handler: SyncHandler) -> None:
        """Add a handler. Handlers are called in registration order, once per sync slice."""
        self._handlers.append(handler)

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Consume the sync stream until stopped, or until the server proves it cannot serve it."""
        log.info("sync pump started (sliding sync), %d handler(s)", len(self._handlers))
        while not self._stop.is_set():
            try:
                result = await self.client.sliding_sync(self._pos)
            except SyncNotSupportedError:
                # The homeserver does not support Simplified Sliding Sync. Onboarding still works
                # off the reconciler signal; the control room simply never sees a command.
                log.warning("sliding sync unsupported; event-driven features are inactive")
                break
            except Exception:
                log.exception("sync failed; backing off %.0fs", self._error_backoff_sec)
                await self._sleep(self._error_backoff_sec)
                continue
            self._pos = result.pos
            await self._dispatch(result)
        log.info("sync pump stopped")

    async def _dispatch(self, result: SyncResult) -> None:
        for handler in self._handlers:
            if self._stop.is_set():
                return
            try:
                await handler.handle_sync(result)
            except Exception:
                log.exception("sync handler %s failed", type(handler).__name__)

    async def _sleep(self, seconds: float) -> None:
        # Sleep, but wake immediately on stop.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
