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

The same replay happens when the server forgets the connection (RUN-03, ONB-14). MSC4186 answers an
expired or unknown ``pos`` with ``400 M_UNKNOWN_POS`` (Synapse says ``M_UNKNOWN`` "Invalid stream
token" for a malformed one), and the only way on is a fresh connection: drop ``pos`` and start over.
Retrying the dead position, as the pump once did, keeps the control room deaf until a restart.

Stopping is prompt: :meth:`SyncPump.request_stop` abandons a long-poll in flight (nothing has been
consumed yet, so nothing is lost), but a slice that is already being dispatched is finished handler by
handler until the next boundary, so a welcome is not cut in half.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix, SyncNotSupportedError, SyncResult
from onbot.heartbeat import SYNC, Heartbeat
from onbot.logging import get_logger

log = get_logger(__name__)

ERROR_BACKOFF_SEC = 5.0
MAX_ERROR_BACKOFF_SEC = 60.0

# errcodes with which a homeserver rejects the stream position rather than the request (see above).
_POSITION_REJECTED = frozenset({"M_UNKNOWN_POS", "M_UNKNOWN"})


def is_position_rejected(exc: BaseException) -> bool:
    """Whether ``exc`` says the server no longer knows our ``pos`` (so a fresh connection is due)."""
    if not isinstance(exc, ApiError) or exc.status_code != 400:
        return False
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    return payload.get("errcode") in _POSITION_REJECTED


class SyncHandler(Protocol):
    """Anything that wants a look at each sync slice."""

    async def handle_sync(self, result: SyncResult) -> None: ...


class SyncPump:
    """Drive Simplified Sliding Sync and fan each slice out to the registered handlers."""

    def __init__(
        self,
        client: ApiClientMatrix,
        *,
        error_backoff_sec: float = ERROR_BACKOFF_SEC,
        max_error_backoff_sec: float = MAX_ERROR_BACKOFF_SEC,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self.client = client
        self._handlers: list[SyncHandler] = []
        self._pos: str | None = None
        self._stop = asyncio.Event()
        self._error_backoff_sec = error_backoff_sec
        self._max_error_backoff_sec = max_error_backoff_sec
        self._heartbeat = heartbeat
        self._failures = 0
        self._last_error: str | None = None
        self._restart_requested = False

    def register(self, handler: SyncHandler) -> None:
        """Add a handler. Handlers are called in registration order, once per sync slice."""
        self._handlers.append(handler)

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def has_position(self) -> bool:
        """Whether the pump has consumed part of the stream already."""
        return self._pos is not None

    def restart_stream(self) -> None:
        """Start a fresh connection on the next request, replaying recent events to every handler.

        For a handler that came online late (the control room, provisioned once the homeserver was
        reachable): the slices it missed are replayed to it, and its own replay guard decides what is
        new. A poll already in flight is still dispatched, but its ``pos`` is discarded.
        """
        self._restart_requested = True
        self._pos = None

    async def run(self) -> None:
        """Consume the sync stream until stopped, or until the server proves it cannot serve it."""
        log.info("sync pump started (sliding sync), %d handler(s)", len(self._handlers))
        while not self._stop.is_set():
            self._restart_requested = False
            try:
                result = await self._poll()
            except SyncNotSupportedError:
                # The homeserver does not support Simplified Sliding Sync. Onboarding still works
                # off the reconciler signal; the control room simply never sees a command.
                log.warning("sliding sync unsupported; event-driven features are inactive")
                if self._heartbeat is not None:
                    self._heartbeat.disable(SYNC, "homeserver does not support sliding sync")
                break
            except Exception as exc:
                if self._pos is not None and is_position_rejected(exc):
                    # Not a failure of ours to back off from: the connection is gone server-side.
                    log.warning(
                        "sync position rejected by the homeserver (%s); starting a fresh connection", exc
                    )
                    self._pos = None
                    continue
                await self._backoff(exc)
                continue
            if result is None:  # stopped while polling
                break
            self._recovered()
            if not self._restart_requested:
                self._pos = result.pos
            if self._heartbeat is not None:
                self._heartbeat.ok(SYNC)
            await self._dispatch(result)
        log.info("sync pump stopped")

    async def _poll(self) -> SyncResult | None:
        """One sync request, abandoned (``None``) as soon as a stop is requested."""
        poll = asyncio.ensure_future(self.client.sliding_sync(self._pos))
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({poll, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in (poll, stop):
                if not fut.done():
                    fut.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await fut
        if poll.cancelled():
            return None
        return poll.result()

    async def _backoff(self, exc: Exception) -> None:
        self._failures += 1
        delay = min(self._error_backoff_sec * 2 ** (self._failures - 1), self._max_error_backoff_sec)
        signature = f"{type(exc).__name__}: {exc}"
        if signature != self._last_error:
            # A new kind of failure gets its traceback once; a repeat of it gets a line.
            log.error("sync failed; backing off %.0fs", delay, exc_info=exc)
            self._last_error = signature
        else:
            log.warning("sync still failing (%d in a row, %s); backing off %.0fs", self._failures, exc, delay)
        if self._heartbeat is not None:
            self._heartbeat.failed(SYNC)
        await self._sleep(delay)

    def _recovered(self) -> None:
        if self._failures:
            log.info("sync recovered after %d failed attempt(s)", self._failures)
        self._failures = 0
        self._last_error = None

    async def _dispatch(self, result: SyncResult) -> None:
        for handler in list(self._handlers):
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
