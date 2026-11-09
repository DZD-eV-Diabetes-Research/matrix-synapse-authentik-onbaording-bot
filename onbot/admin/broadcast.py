"""Fan a single announcement out into every user's notice board (G4.6).

The bot's ``m.direct`` account data maps every onboarded user to the direct room the bot opened with
them. Those rooms are read-only notice boards (:mod:`onbot.onboarding.notice_board`), and
announcements are exactly what they exist for. Two filters decide who is reached:

* **Mapped users only** (SEC-06). ``m.direct`` may hold users the bot no longer manages: somebody who
  left the sync scope, or an account an older version welcomed only because it joined a room the bot
  was in. A broadcast goes to the users the reconciler maps, handed in as ``recipients``: in the
  service the set of the last pass, in ``onbot broadcast`` a fresh read-only mapping. Without such a
  set (no pass could map anybody yet, or user sync is off), and with an empty one (Authentik
  returned nobody in the sync scope, W4R-05), nothing is sent and the broadcast is refused.
* **Members only** (ONB-07). A user who left their notice board would not see the message, but the
  send would still succeed because the bot is in the room. The user's membership is read first; a
  room they left (or were banned from) is skipped and counted separately, so "sent to N rooms" means
  N users who can read it. An invited user counts as reached: the message is waiting when they join.

Messages go out as ``m.notice``, the convention for bot-originated content: clients render it more
quietly and, more importantly, other bots are expected not to reply to it, which is what keeps two
bots in one room from talking to each other forever.

Announcements are rendered like the welcome messages (ONB-10): ``body`` keeps the text as written
(the markdown source, which is also what Element puts there), and ``formatted_body`` carries the HTML
from :func:`~onbot.formatting.render_markdown`. Plain text stays a plain message.

**On rate limits (ONB-03).** Synapse limits message sends *per user*, and a broadcast is one user
(the bot) sending into hundreds of rooms at once, so this is the one place in the codebase that
reliably provokes HTTP 429. Three things stand between the fan-out and the limiter:

1. ``ApiClientSynapseAdmin.override_ratelimit`` on the bot, called best-effort at startup
   (:mod:`onbot.app`). This is the real fix: it lifts the limiter for the bot account entirely.
   Whether it worked is recorded in :attr:`BroadcastService.rate_limit_lifted` and shown by
   ``!status`` and in the ``!announce`` result, because it can fail for ordinary reasons (the admin
   API is not reachable on the configured path, the token lacks the admin scope).
2. The concurrency bound below. An unbounded ``gather`` over 500 rooms opens 500 sockets and hits
   the limiter as hard as it possibly can; a few workers keep the send rate civil.
3. **Pacing by the server's answers.** A send raises a 429 at once instead of waiting it out on its
   own (the shared HTTP retry would let every worker burn its few attempts against the same empty
   token bucket). The first 429 switches the whole fan-out to one send at a time; everybody waits as
   long as ``retry_after_ms`` asks (capped at :data:`MAX_RATE_LIMIT_WAIT_SEC`), and the room goes
   back into the queue. A throttled broadcast is slow, at the server's pace, but complete. Only when
   nothing at all got through for :data:`STALL_LIMIT_SEC` does it give up on the rest.

Each room's send carries a transaction id derived from the broadcast and the room, so a send repeated
after a lost answer or a 429 is deduplicated by Synapse instead of posted twice.

A room that still fails is reported, not raised: one unreachable room must not silence the
announcement for everybody else. The report stays short however many rooms fail (ONB-04): counts, the
failures grouped by cause, and at most :data:`MAX_LISTED_FAILURES` rooms by name. Every failed room
is in the log.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field

from onbot.clients.base import ApiError, is_rate_limited
from onbot.clients.matrix import ApiClientMatrix
from onbot.config import OnbotConfig
from onbot.formatting import render_markdown
from onbot.logging import get_logger

log = get_logger(__name__)

M_DIRECT = "m.direct"
NOTICE_MSGTYPE = "m.notice"

# How many rooms are written to at once. Deliberately small: see the module docstring.
DEFAULT_CONCURRENCY = 5

# The longest single wait one 429 answer can impose. A server asking for more is asked again after
# this long (and answers with the time that is still left).
MAX_RATE_LIMIT_WAIT_SEC = 60.0
# The wait after a 429 that does not say how long (no ``retry_after_ms``).
DEFAULT_RATE_LIMIT_WAIT_SEC = 2.0
# Give up on the rooms still waiting when no send at all got through for this long.
STALL_LIMIT_SEC = 300.0

# How many failed rooms the summary names; the rest are counted (ONB-04). Synapse refuses events
# above 65536 bytes, so a list of every failure of a large broadcast cannot be posted.
MAX_LISTED_FAILURES = 20
# How many distinct causes the summary counts.
MAX_LISTED_CAUSES = 5

# Memberships in which the user can read what is posted in the room (now, or once they accept).
_REACHABLE = frozenset({"join", "invite"})

# Who a broadcast may reach: the MXIDs the reconciler maps, or ``None`` when that is not known.
RecipientSource = Callable[[], Awaitable[Collection[str] | None]]

NO_RECIPIENTS = (
    "no reconcile pass has mapped the users yet (or user sync is off), so the bot does not know who "
    "may receive it"
)
NO_MAPPED_USERS = (
    "no users are mapped: Authentik returned nobody in the sync scope (is it reachable, does the API "
    "token see the users, are the scope filters right?)"
)


class RecipientsUnknown(RuntimeError):
    """Nobody can be told apart from an outsider right now, so nobody is broadcast to (SEC-06)."""


def describe_error(exc: BaseException) -> str:
    """A short cause for a failed room: status and errcode, not the request URL (ONB-04)."""
    if isinstance(exc, ApiError):
        if exc.status_code == 429:
            return "rate limited (HTTP 429)"
        text = f"HTTP {exc.status_code}"
        if exc.errcode:
            text += f" {exc.errcode}"
        message = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if isinstance(message, str) and message:
            text += f": {message[:120]}"
        return text
    detail = str(exc)
    return f"{type(exc).__name__}: {detail[:160]}" if detail else type(exc).__name__


def broadcast_txn_id(broadcast_id: str, room_id: str) -> str:
    """The transaction id of one broadcast's send into one room, the same on every attempt."""
    return "onbot-broadcast-" + hashlib.sha256(f"{broadcast_id}\n{room_id}".encode()).hexdigest()[:32]


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
    # How often the homeserver answered a send with 429, and whether the bot's override was in
    # effect (``None``: the startup step has not run, or this process does not run it).
    rate_limited: int = 0
    rate_limit_lifted: bool | None = None

    @property
    def sent_count(self) -> int:
        return len(self.sent)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    def summary(self) -> str:
        """What an operator reads, in the CLI or back in the control room; bounded in size (ONB-04)."""
        if self.refused is not None:
            return f"not sent: {self.refused}"
        line = f"sent to {self.sent_count} rooms, {self.failed_count} failed"
        if self.not_member:
            line += f", {len(self.not_member)} skipped (the user left their notice board)"
        lines = [line]
        if self.rate_limited:
            lines.append(
                f"The homeserver rate-limited the bot {self.rate_limited} time(s); the broadcast went out "
                "at the pace it allowed."
            )
            if self.rate_limit_lifted is False:
                lines.append(
                    "The bot's send rate limit is not lifted (the Synapse override failed at startup), so "
                    "large broadcasts are slow. See !status and the bot's log."
                )
        if self.failures:
            causes = Counter(failure.error for failure in self.failures)
            listed = ", ".join(f"{count} {cause}" for cause, count in causes.most_common(MAX_LISTED_CAUSES))
            if len(causes) > MAX_LISTED_CAUSES:
                listed += f", {len(causes) - MAX_LISTED_CAUSES} other cause(s)"
            lines.append(f"Failed: {listed}")
            lines.extend(f"  - {failure}" for failure in self.failures[:MAX_LISTED_FAILURES])
            if self.failed_count > MAX_LISTED_FAILURES:
                lines.append(
                    f"  - ... and {self.failed_count - MAX_LISTED_FAILURES} more (every room is in the "
                    "bot's log)"
                )
        return "\n".join(lines)


class BroadcastService:
    """Send one message into every direct room the bot manages."""

    def __init__(
        self,
        client: ApiClientMatrix,
        config: OnbotConfig,
        *,
        recipients: RecipientSource,
        concurrency: int = DEFAULT_CONCURRENCY,
        stall_limit_sec: float = STALL_LIMIT_SEC,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.config = config
        self.bot_id = config.synapse_server.bot_user_id
        self._recipients = recipients
        self._concurrency = max(1, concurrency)
        self._stall_limit_sec = stall_limit_sec
        self._clock = clock
        self._sleep = sleep
        # Whether the startup step lifted Synapse's send rate limit for the bot: ``None`` until it
        # ran, then its outcome (set by :mod:`onbot.app`). Shown by !status and with each result.
        self.rate_limit_lifted: bool | None = None

    async def target_rooms(self) -> dict[str, str]:
        """Map ``room_id -> user_id`` for every notice board the bot should announce into.

        Only users the reconciler maps (SEC-06); the bot's own entry and the configured ignore list
        are dropped too. A user with several direct rooms gets all of them: they are all rooms the
        bot opened. Raises :class:`RecipientsUnknown` when the mapped users are not known.
        """
        mapped = await self._recipients()
        if mapped is None:
            raise RecipientsUnknown(NO_RECIPIENTS)
        if not mapped:
            # An Authentik answer without users (an outage, a token without the scope, a filter
            # typo) maps nobody. Reporting that as "sent to 0 rooms" would read as success (W4R-05).
            raise RecipientsUnknown(NO_MAPPED_USERS)
        allowed = set(mapped) - {self.bot_id, *self.config.matrix_user_ignore_list}
        direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
        rooms: dict[str, str] = {}
        for user_id, room_ids in sorted(direct.items()):
            if user_id not in allowed or not isinstance(room_ids, list):
                continue
            for room_id in room_ids or []:
                rooms[str(room_id)] = user_id
        return rooms

    async def broadcast(
        self, message: str, *, on_start: Callable[[int], Awaitable[None]] | None = None
    ) -> BroadcastResult:
        """Send ``message`` as ``m.notice`` to every target room, paced and fail-soft.

        ``on_start`` is called with the number of target rooms before the first send, when there is
        at least one (the control room acknowledges the command with it).
        """
        try:
            rooms = await self.target_rooms()
        except RecipientsUnknown as exc:
            log.warning("broadcast refused: %s", exc)
            return BroadcastResult(refused=str(exc))
        if not rooms:
            log.warning("broadcast: the bot manages no direct rooms; nothing to send")
            return BroadcastResult(rate_limit_lifted=self.rate_limit_lifted)
        if on_start is not None:
            try:
                await on_start(len(rooms))
            except Exception:
                log.warning("could not acknowledge the broadcast before sending it", exc_info=True)

        log.info("broadcasting to %d rooms (concurrency %d)", len(rooms), self._concurrency)
        fan_out = _FanOut(self, message, rooms)
        try:
            await fan_out.run()
        except asyncio.CancelledError:
            done = fan_out.result
            log.warning(
                "broadcast interrupted after %d of %d rooms; the rest did not get it",
                done.sent_count + done.failed_count + len(done.not_member),
                len(rooms),
            )
            raise
        log.info("broadcast finished: %s", fan_out.result.summary())
        return fan_out.result


class _FanOut:
    """One broadcast's delivery: a queue of rooms, a few workers, and a pace set by the server."""

    def __init__(self, service: BroadcastService, message: str, rooms: dict[str, str]) -> None:
        self.client = service.client
        self.message = message
        self.formatted = render_markdown(message)
        self.broadcast_id = uuid.uuid4().hex
        self.queue: deque[tuple[str, str]] = deque(rooms.items())
        self.result = BroadcastResult(rate_limit_lifted=service.rate_limit_lifted)
        self._concurrency = service._concurrency
        self._stall_limit_sec = service._stall_limit_sec
        self._clock = service._clock
        self._sleep = service._sleep
        self._resume_at = 0.0
        self._last_progress = self._clock()
        # Set by the first 429: from then on one send at a time, at the server's pace.
        self._throttled = False
        self._serial = asyncio.Lock()
        self._gave_up = False
        # Rooms whose user was already found to be a member, so a retried room is not read again.
        self._members: set[str] = set()

    async def run(self) -> None:
        await asyncio.gather(*(self._worker() for _ in range(self._concurrency)))
        # What is left when the fan-out gave up waiting for the rate limiter.
        while self.queue:
            room_id, user_id = self.queue.popleft()
            self._fail(
                room_id,
                user_id,
                f"rate limited, nothing got through for {self._stall_limit_sec:.0f}s, gave up",
            )

    async def _worker(self) -> None:
        while self.queue and not self._gave_up:
            room_id, user_id = self.queue.popleft()
            if self._throttled:
                async with self._serial:
                    await self._attempt(room_id, user_id)
            else:
                await self._attempt(room_id, user_id)

    async def _attempt(self, room_id: str, user_id: str) -> None:
        await self._pace()
        stalled = self._throttled and self._clock() - self._last_progress > self._stall_limit_sec
        if self._gave_up or stalled:
            self._gave_up = True
            self.queue.appendleft((room_id, user_id))
            return
        try:
            if room_id not in self._members:
                membership = await self.client.get_membership(room_id, user_id)
                if membership not in _REACHABLE:
                    log.info("broadcast: skipping %s, %s is not in it (%s)", room_id, user_id, membership)
                    self.result.not_member.append((room_id, user_id))
                    return
                self._members.add(room_id)
            await self.client.send_text_message(
                room_id,
                self.message,
                msgtype=NOTICE_MSGTYPE,
                formatted_body=self.formatted,
                txn_id=broadcast_txn_id(self.broadcast_id, room_id),
                retry_rate_limited=False,
            )
        except Exception as exc:
            if is_rate_limited(exc):
                self._slow_down(exc, room_id, user_id)
                return
            log.warning("broadcast to %s (%s) failed: %s", room_id, user_id, exc)
            self.result.failures.append(BroadcastFailure(room_id, user_id, describe_error(exc)))
        else:
            self.result.sent.append(room_id)
            self._last_progress = self._clock()

    def _slow_down(self, exc: BaseException, room_id: str, user_id: str) -> None:
        """Pace the whole fan-out by the server's answer and put the room back in the queue."""
        self.result.rate_limited += 1
        if not self._throttled:
            log.warning(
                "broadcast: the homeserver rate-limits the bot; sending one message at a time at its "
                "pace (did the rate-limit override fail at startup?)"
            )
        self._throttled = True
        asked = exc.retry_after if isinstance(exc, ApiError) else None
        wait = DEFAULT_RATE_LIMIT_WAIT_SEC if asked is None else asked
        self._resume_at = max(self._resume_at, self._clock() + min(max(wait, 0.0), MAX_RATE_LIMIT_WAIT_SEC))
        self.queue.appendleft((room_id, user_id))

    async def _pace(self) -> None:
        while (delay := self._resume_at - self._clock()) > 0:
            await self._sleep(delay)

    def _fail(self, room_id: str, user_id: str, error: str) -> None:
        log.warning("broadcast to %s (%s) failed: %s", room_id, user_id, error)
        self.result.failures.append(BroadcastFailure(room_id, user_id, error))
