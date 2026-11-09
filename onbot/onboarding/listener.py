"""Onboarding listener (ADR-0003): who gets a notice board, and a worker queue that delivers it.

**Who.** Only the accounts the last reconcile pass mapped (SEC-06, ONB-13): the Matrix accounts of the
Authentik users in the sync scope, without held identity conflicts (D8, D21), the bot and ignored
users. The reconciler hands that set over once per pass as :attr:`Signal.users_mapped`. Somebody who
merely joins a room the bot sits in (a federated guest invited into a group room, a local account
that is not in Authentik, a break-glass admin entering the control room) is not welcomed, and before
the first pass nobody is. The same set filters the broadcast targets (:meth:`mapped_users`).

**Two triggers, one queue.**

* **Reconciler signal:** every mapped user not yet welcomed by this process is queued.
* **Sync stream:** :meth:`handle_sync` (a :class:`~onbot.sync.SyncPump` handler) queues a mapped user
  seen joining a room, for welcomes without tick latency, and re-checks a mapped user seen *leaving* a
  room, so a user who left their notice board gets their one re-invitation (ONB-07). Replays of the
  stream after a restart or a fresh connection cost a set lookup per member, nothing more.

**Delivery is decoupled from the pass (RUN-08).** Handlers only enqueue. A fixed number of workers
(:data:`DEFAULT_CONCURRENCY`) drain the queue, so a slow or failing welcome never stalls the
reconcile pass, and 500 new users are welcomed a few at a time instead of one after another inside
the pass. The queue holds each user at most once, so it is bounded by the number of mapped users.
One user is never handled by two workers at once, which keeps their messages in order. A user
reported again while being handled (the force-join into their own notice board does exactly that) runs
once more afterwards only if that run did not succeed, or if they left a room meanwhile (their notice
board must be looked at again). The welcome flow serialises the one document all
users share, the bot's ``m.direct`` (RUN-01).

**Stopping.** :meth:`request_stop` lets each worker finish the user in hand and drops the rest of the
queue; the next start queues them again. The service gives the workers the shutdown grace period like
every loop (:mod:`onbot.runtime`) and cancels them after it. A send cut off that way is safe to repeat:
its transaction id is deterministic (ONB-09). ``reconcile-once`` drains the queue after its pass
(:meth:`process_queued`).

**The welcomed cache.** Proving a user is already welcomed costs a few CS-API reads, and the pass
reports every mapped user every time, so :attr:`OnboardingListener._welcomed` remembers who this
process has welcomed and skips them. It is per process and deliberately not persisted: after a restart
each user is checked once more, which is also when a changed ``welcome_new_users_messages`` is picked
up (:mod:`onbot.onboarding.welcome` sends only the message that changed).

**A new bot avatar.** The notice boards wear the bot's avatar. When the picture behind its URL
changes (the media layer looks again about once a day, ONB-15), the welcomed cache is cleared at the
next pass, so every mapped user's board is looked at once more and gets the new picture.

**Housekeeping.** With every pass the queue also gets one prune job: notice boards of accounts that no
longer exist (deactivated or erased) or are not on this server are dropped from ``m.direct``
(SEC-06, ONB-07), see :meth:`~onbot.onboarding.welcome.WelcomeService.prune_direct`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection

from onbot.clients.matrix import ApiClientMatrix, SyncResult
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.logging import get_logger
from onbot.onboarding.welcome import NoticeBoardUnreadable, WelcomeService

log = get_logger(__name__)

# How many users are welcomed at once. Each welcome is a handful of sequential requests; a few in
# parallel keep a large first deployment moving without flooding the homeserver.
DEFAULT_CONCURRENCY = 4

# The queue item that stands for the m.direct prune job (an MXID always starts with "@").
_PRUNE = "prune"


def _memberships(result: SyncResult, membership: str, *, timeline_only: bool = False) -> set[str]:
    users: set[str] = set()
    for room in result.rooms:
        events = room.timeline if timeline_only else room.member_events()
        for ev in events:
            if ev.get("type") != "m.room.member":
                continue
            if (ev.get("content") or {}).get("membership") != membership:
                continue
            mxid = ev.get("state_key") or ev.get("sender")
            if mxid:
                users.add(mxid)
    return users


def extract_joined_users(result: SyncResult) -> set[str]:
    """MXIDs with a ``join`` membership event in this sync slice."""
    return _memberships(result, "join")


def extract_left_users(result: SyncResult) -> set[str]:
    """MXIDs that left a room in this slice's timeline (news, not the room's current state)."""
    return _memberships(result, "leave", timeline_only=True)


class OnboardingListener:
    def __init__(
        self,
        client: ApiClientMatrix,
        welcome: WelcomeService,
        config: OnbotConfig,
        events: EventBus,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.client = client
        self.welcome = welcome
        self.config = config
        self.events = events
        self.bot_id = config.synapse_server.bot_user_id
        self._concurrency = max(1, concurrency)
        # The accounts the last pass mapped; None until a pass could map anybody.
        self._mapped: frozenset[str] | None = None
        # Every active local account Synapse listed in that pass (what the prune job keeps).
        self._accounts: frozenset[str] = frozenset()
        # Users this process has already welcomed. Bounded by the directory size; see the module
        # docstring for why it is not persisted.
        self._welcomed: set[str] = set()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._pending: set[str] = set()  # queued, not yet taken by a worker
        self._in_flight: set[str] = set()
        self._again: set[str] = set()  # queued again while in flight
        # Users whose next run must look at their notice board even if they count as welcomed (they
        # left a room since the last look).
        self._recheck: set[str] = set()
        self._stop = asyncio.Event()
        # Users whose welcome failed, with the last error, so a failure repeating every pass is one
        # traceback and then a line per attempt.
        self._failing: dict[str, str] = {}
        # The picture the notice boards wear, as of the last pass (ONB-15).
        self._board_avatar: str | None = None

    def start(self) -> None:
        """Subscribe to the reconciler's signal (call once, before running)."""
        self.events.subscribe(Signal.users_mapped, self._on_users_mapped)

    @property
    def mapped(self) -> frozenset[str] | None:
        """The accounts the last reconcile pass mapped, ``None`` before the first one could."""
        return self._mapped

    async def mapped_users(self) -> frozenset[str] | None:
        """:attr:`mapped`, in the shape the broadcast asks for its recipients."""
        return self._mapped

    # --- triggers (never block) -------------------------------------------------------------------

    async def handle_sync(self, result: SyncResult) -> None:
        """Queue mapped users who joined, and re-check mapped users who left a room, in this slice."""
        if self._mapped is None:
            return
        for mxid in extract_joined_users(result):
            if mxid not in self._welcomed and self._is_target(mxid):
                self._enqueue(mxid)
        for mxid in extract_left_users(result):
            if self._is_target(mxid):
                # Possibly their notice board: the welcome flow looks and re-invites once (ONB-07).
                self._recheck.add(mxid)
                self._welcomed.discard(mxid)
                self._enqueue(mxid)

    async def _on_users_mapped(self, event: Event) -> None:
        self._mapped = frozenset(event.payload["mxids"])
        self._accounts = frozenset(event.payload.get("accounts") or ())
        self._notice_new_board_avatar()
        self._enqueue(_PRUNE)
        for mxid in sorted(self._mapped):
            if mxid not in self._welcomed and self._is_target(mxid):
                self._enqueue(mxid)

    def _notice_new_board_avatar(self) -> None:
        """Forget who was welcomed when the bot avatar's picture changed, so every board gets it."""
        current = self.welcome.board_avatar_sha256()
        if current is None:
            return
        if self._board_avatar is not None and current != self._board_avatar:
            log.info("the bot avatar changed; checking every notice board for the new picture")
            self._welcomed.clear()
        self._board_avatar = current

    def _is_target(self, mxid: str) -> bool:
        if self._mapped is None or mxid not in self._mapped:
            return False
        return mxid != self.bot_id and mxid not in self.config.matrix_user_ignore_list

    def _enqueue(self, item: str) -> None:
        if item in self._pending:
            return
        if item in self._in_flight:
            self._again.add(item)
            return
        self._pending.add(item)
        self._queue.put_nowait(item)

    # --- workers ----------------------------------------------------------------------------------

    async def run(self) -> None:
        """Deliver queued welcomes until :meth:`request_stop` (the service's onboarding task)."""
        await asyncio.gather(*(self._worker(until_empty=False) for _ in range(self._concurrency)))

    async def process_queued(self) -> None:
        """Deliver everything queued so far, then return (``reconcile-once``)."""
        await asyncio.gather(*(self._worker(until_empty=True) for _ in range(self._concurrency)))

    def request_stop(self) -> None:
        """Let each worker finish the user in hand; the rest of the queue is dropped."""
        self._stop.set()
        for _ in range(self._concurrency):
            self._queue.put_nowait(None)  # wakes an idle worker

    @property
    def queued(self) -> Collection[str]:
        """Users waiting for a worker (for tests and diagnostics)."""
        return frozenset(self._pending - {_PRUNE})

    async def _worker(self, *, until_empty: bool) -> None:
        while not self._stop.is_set():
            if until_empty:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
            else:
                item = await self._queue.get()
            if item is None or self._stop.is_set():
                return
            self._pending.discard(item)
            self._in_flight.add(item)
            try:
                await self._process(item)
            finally:
                self._in_flight.discard(item)
                if item in self._again:
                    self._again.discard(item)
                    if item in self._recheck or item not in self._welcomed:
                        self._enqueue(item)

    async def _process(self, item: str) -> None:
        if item == _PRUNE:
            try:
                await self.welcome.prune_direct(self._accounts)
            except Exception:
                log.warning("could not prune the notice boards of departed accounts", exc_info=True)
            return
        self._recheck.discard(item)  # this run is the look; a leave from now on asks for another
        if not self._is_target(item):
            return  # no longer mapped since it was queued
        try:
            await self.welcome.welcome_user(item)
        except NoticeBoardUnreadable as exc:
            # Retrying cannot help and would repeat the error every pass; a restart checks again.
            log.error("cannot welcome %s: %s", item, exc)
            self._welcomed.add(item)
            return
        except Exception as exc:
            # Not remembered as welcomed, so the next pass retries: a homeserver that was briefly
            # unreachable must not cost the user their welcome.
            signature = f"{type(exc).__name__}: {exc}"
            if self._failing.get(item) != signature:
                log.exception("welcome flow failed for %s; retrying with the next pass", item)
            else:
                log.warning("welcome flow for %s still failing (%s)", item, signature)
            self._failing[item] = signature
            return
        if self._failing.pop(item, None) is not None:
            log.info("welcome flow for %s succeeded after earlier failures", item)
        self._welcomed.add(item)
