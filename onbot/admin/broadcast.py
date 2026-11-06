"""Fan a single announcement out into every user's notice board (G4.6).

The bot's ``m.direct`` account data maps every onboarded user to the direct room the bot opened with
them. Those rooms are read-only notice boards (:mod:`onbot.onboarding.notice_board`), and
announcements are exactly what they exist for. Two filters decide who is reached:

* **Mapped users only** (SEC-06). ``m.direct`` may hold users the bot no longer manages: somebody who
  left the sync scope, or an account an older version welcomed only because it joined a room the bot
  was in. A broadcast goes to the users the reconciler maps, handed in as ``recipients``: in the
  service the set of the last pass, in ``onbot broadcast`` a fresh read-only mapping. Without such a
  set (no pass could map anybody yet, or user sync is off) nothing is sent.
* **Members only** (ONB-07). A user who left their notice board would not see the message, but the
  send would still succeed because the bot is in the room. The user's membership is read first; a
  room they left (or were banned from) is skipped and counted separately, so "sent to N rooms" means
  N users who can read it. An invited user counts as reached: the message is waiting when they join.

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
3. The shared retry in :class:`~onbot.clients.base.BaseApiClient`: a 429 is retried up to 4
   attempts, each time waiting as long as Synapse's ``retry_after_ms`` asks (capped at 30 s per
   wait). It rides out a throttle of a minute or so per message, not an hour, so it is the last
   line rather than the first.

A room that still fails after all of that is reported, not raised: one unreachable room must not
silence the announcement for everybody else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field

from onbot.clients.matrix import ApiClientMatrix
from onbot.config import OnbotConfig
from onbot.logging import get_logger

log = get_logger(__name__)

M_DIRECT = "m.direct"
NOTICE_MSGTYPE = "m.notice"

# How many rooms are written to at once. Deliberately small: see the module docstring.
DEFAULT_CONCURRENCY = 5

# Memberships in which the user can read what is posted in the room (now, or once they accept).
_REACHABLE = frozenset({"join", "invite"})

# Who a broadcast may reach: the MXIDs the reconciler maps, or ``None`` when that is not known.
RecipientSource = Callable[[], Awaitable[Collection[str] | None]]

NO_RECIPIENTS = (
    "no reconcile pass has mapped the users yet (or user sync is off), so the bot does not know who "
    "may receive it"
)


class RecipientsUnknown(RuntimeError):
    """Nobody can be told apart from an outsider right now, so nobody is broadcast to (SEC-06)."""


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
    # Rooms skipped because their user is no longer a member (left or banned), as (room, user).
    not_member: list[tuple[str, str]] = field(default_factory=list)
    # Why nothing was sent at all, when the broadcast was refused before any room.
    refused: str | None = None

    @property
    def sent_count(self) -> int:
        return len(self.sent)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    def summary(self) -> str:
        """One line an operator can read, in the CLI or back in the control room."""
        if self.refused is not None:
            return f"not sent: {self.refused}"
        line = f"sent to {self.sent_count} rooms, {self.failed_count} failed"
        if self.not_member:
            line += f", {len(self.not_member)} skipped (the user left their notice board)"
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
        recipients: RecipientSource,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.client = client
        self.config = config
        self.bot_id = config.synapse_server.bot_user_id
        self._recipients = recipients
        self._concurrency = max(1, concurrency)

    async def target_rooms(self) -> dict[str, str]:
        """Map ``room_id -> user_id`` for every notice board the bot should announce into.

        Only users the reconciler maps (SEC-06); the bot's own entry and the configured ignore list
        are dropped too. A user with several direct rooms gets all of them: they are all rooms the
        bot opened. Raises :class:`RecipientsUnknown` when the mapped users are not known.
        """
        mapped = await self._recipients()
        if mapped is None:
            raise RecipientsUnknown(NO_RECIPIENTS)
        allowed = set(mapped) - {self.bot_id, *self.config.matrix_user_ignore_list}
        direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
        rooms: dict[str, str] = {}
        for user_id, room_ids in sorted(direct.items()):
            if user_id not in allowed or not isinstance(room_ids, list):
                continue
            for room_id in room_ids or []:
                rooms[str(room_id)] = user_id
        return rooms

    async def broadcast(self, message: str) -> BroadcastResult:
        """Send ``message`` as ``m.notice`` to every target room, bounded and fail-soft."""
        try:
            rooms = await self.target_rooms()
        except RecipientsUnknown as exc:
            log.warning("broadcast refused: %s", exc)
            return BroadcastResult(refused=str(exc))
        if not rooms:
            log.warning("broadcast: the bot manages no direct rooms; nothing to send")
            return BroadcastResult()

        log.info("broadcasting to %d rooms (concurrency %d)", len(rooms), self._concurrency)
        semaphore = asyncio.Semaphore(self._concurrency)
        result = BroadcastResult()

        async def _send(room_id: str, user_id: str) -> None:
            async with semaphore:
                try:
                    membership = await self.client.get_membership(room_id, user_id)
                    if membership not in _REACHABLE:
                        log.info("broadcast: skipping %s, %s is not in it (%s)", room_id, user_id, membership)
                        result.not_member.append((room_id, user_id))
                        return
                    await self.client.send_text_message(room_id, message, msgtype=NOTICE_MSGTYPE)
                except Exception as exc:
                    log.warning("broadcast to %s (%s) failed: %s", room_id, user_id, exc)
                    result.failures.append(BroadcastFailure(room_id, user_id, str(exc)))
                else:
                    result.sent.append(room_id)

        await asyncio.gather(*(_send(room, user) for room, user in rooms.items()))
        log.info("broadcast finished: %s", result.summary())
        return result
