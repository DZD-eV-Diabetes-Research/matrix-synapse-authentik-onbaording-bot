"""Fan a single announcement out into every user's notice board (G4.6).

The bot's ``m.direct`` account data already maps every onboarded user to the direct room the bot
opened with them, so it *is* the broadcast target list and needs no separate bookkeeping. Those
rooms are read-only notice boards (:mod:`onbot.onboarding.notice_board`) — announcements are exactly
what they exist for.

Messages go out as ``m.notice``, the convention for bot-originated content: clients render it more
quietly and, more importantly, other bots are expected not to reply to it, which is what keeps two
bots in one room from talking to each other forever.

**On rate limits.** Synapse limits message sends *per user*, and a broadcast is one user (the bot)
sending into hundreds of rooms at once, so this is the one place in the codebase that reliably
provokes HTTP 429. Three things stand between the fan-out and the limiter, in order of how much they
actually buy:

1. ``ApiClientSynapseAdmin.override_ratelimit`` on the bot, called best-effort at startup
   (:mod:`onbot.app`). This is the real fix: it lifts the limiter for the bot account entirely.
2. The concurrency bound below. An unbounded ``gather`` over 500 rooms opens 500 sockets and hits
   the limiter as hard as it possibly can; a small semaphore keeps the send rate civil even when
   step 1 did not happen.
3. The shared retry in :class:`~onbot.clients.base.BaseApiClient`, which does list 429 in
   ``RETRYABLE_STATUS_CODES``. Note this is a *blind* exponential backoff — 4 attempts, ~3.5s of
   cumulative waiting, and it ignores the ``retry_after_ms`` Synapse hands back. It absorbs a brief
   burst, not a sustained throttle, so it is the last line rather than the first.

A room that still fails after all of that is reported, not raised: one unreachable room must not
silence the announcement for everybody else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from onbot.clients.matrix import ApiClientMatrix
from onbot.config import OnbotConfig
from onbot.logging import get_logger

log = get_logger(__name__)

M_DIRECT = "m.direct"
NOTICE_MSGTYPE = "m.notice"

# How many rooms are written to at once. Deliberately small: see the module docstring.
DEFAULT_CONCURRENCY = 5


@dataclass(frozen=True, slots=True)
class BroadcastFailure:
    """One room the announcement could not be delivered to, and why."""

    room_id: str
    user_id: str
    error: str

    def __str__(self) -> str:
        return f"{self.user_id} ({self.room_id}): {self.error}"


@dataclass(slots=True)
class BroadcastResult:
    """Outcome of one fan-out: which rooms took the message and which refused it."""

    sent: list[str] = field(default_factory=list)
    failures: list[BroadcastFailure] = field(default_factory=list)

    @property
    def sent_count(self) -> int:
        return len(self.sent)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    def summary(self) -> str:
        """One line an operator can read, in the CLI or back in the control room."""
        line = f"sent to {self.sent_count} rooms, {self.failed_count} failed"
        if self.failures:
            line += "\n" + "\n".join(f"  - {failure}" for failure in self.failures)
        return line


class BroadcastService:
    """Send one message into every direct room the bot manages."""

    def __init__(
        self,
        client: ApiClientMatrix,
        config: OnbotConfig,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.client = client
        self.config = config
        self.bot_id = config.synapse_server.bot_user_id
        self._concurrency = max(1, concurrency)

    async def target_rooms(self) -> dict[str, str]:
        """Map ``room_id -> user_id`` for every notice board the bot should announce into.

        The bot's own entry and the configured ignore list are dropped: those users are ones the bot
        never touches. A user with several direct rooms gets all of them — they are all rooms the
        bot opened.
        """
        direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
        ignored = {self.bot_id, *self.config.matrix_user_ignore_list}
        rooms: dict[str, str] = {}
        for user_id, room_ids in sorted(direct.items()):
            if user_id in ignored:
                continue
            for room_id in room_ids or []:
                rooms[str(room_id)] = user_id
        return rooms

    async def broadcast(self, message: str) -> BroadcastResult:
        """Send ``message`` as ``m.notice`` to every target room, bounded and fail-soft."""
        rooms = await self.target_rooms()
        if not rooms:
            log.warning("broadcast: the bot manages no direct rooms; nothing to send")
            return BroadcastResult()

        log.info("broadcasting to %d rooms (concurrency %d)", len(rooms), self._concurrency)
        semaphore = asyncio.Semaphore(self._concurrency)
        result = BroadcastResult()

        async def _send(room_id: str, user_id: str) -> None:
            async with semaphore:
                try:
                    await self.client.send_text_message(room_id, message, msgtype=NOTICE_MSGTYPE)
                except Exception as exc:
                    log.warning("broadcast to %s (%s) failed: %s", room_id, user_id, exc)
                    result.failures.append(BroadcastFailure(room_id, user_id, str(exc)))
                else:
                    result.sent.append(room_id)

        await asyncio.gather(*(_send(room, user) for room, user in rooms.items()))
        log.info("broadcast finished: %s", result.summary())
        return result
