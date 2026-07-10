"""Onboarding listener (AD-3): event-driven welcome via the sync stream + the reconciler signal.

Two trigger paths converge here (AD-4 explicit coupling):

* **Reconciler signal** — the reconciler emits :attr:`Signal.user_synced` for every provisioned user
  it sees; the listener subscribes and welcomes them. This is the dependable path and works without
  any sync support.
* **Sync stream** — :meth:`handle_sync` is called by the shared :class:`~onbot.sync.SyncPump` for
  each slice and welcomes users on ``join`` membership events, for instant onboarding with no tick
  latency.

Both funnel through :meth:`_maybe_welcome`, which filters out the bot and ignored users and defers to
the idempotent :class:`~onbot.onboarding.welcome.WelcomeService`. That idempotency is what lets the
listener ignore the sync stream's replay-on-restart entirely: re-welcoming an already-welcomed user
sends nothing.

Sending nothing is not the same as costing nothing, though. The reconciler emits ``user_synced`` for
*every* mapped user on *every* pass, and proving a user is already welcomed costs three CS-API reads
(their DM room from account data, its onbot state event, its power levels). At a few hundred users
and a short tick that is the bot's entire Matrix traffic, spent to conclude nothing. So the listener
remembers who it has welcomed in :attr:`OnboardingListener._welcomed` and short-circuits before
touching Matrix at all.

The memory is per-process and deliberately not persisted: after a restart the first pass re-checks
each user once and repopulates it. That is also what keeps ``welcome_new_users_messages`` editable —
a changed message is picked up on the restart that loads it, and :mod:`onbot.onboarding.welcome`
then re-sends only the message that actually changed.
"""

from __future__ import annotations

from onbot.clients.matrix import ApiClientMatrix, SyncResult
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.logging import get_logger
from onbot.onboarding.welcome import WelcomeService

log = get_logger(__name__)


def extract_joined_users(result: SyncResult) -> set[str]:
    """MXIDs with a ``join`` membership event in this sync slice."""
    joined: set[str] = set()
    for room in result.rooms:
        for ev in room.member_events():
            if (ev.get("content") or {}).get("membership") != "join":
                continue
            mxid = ev.get("state_key") or ev.get("sender")
            if mxid:
                joined.add(mxid)
    return joined


class OnboardingListener:
    def __init__(
        self,
        client: ApiClientMatrix,
        welcome: WelcomeService,
        config: OnbotConfig,
        events: EventBus,
    ) -> None:
        self.client = client
        self.welcome = welcome
        self.config = config
        self.events = events
        self.bot_id = config.synapse_server.bot_user_id
        # Users this process has already welcomed. Bounded by the directory size; see the module
        # docstring for why it is not persisted.
        self._welcomed: set[str] = set()

    def start(self) -> None:
        """Subscribe to the reconciler's user-provisioned signal (call once, before running)."""
        self.events.subscribe(Signal.user_synced, self._on_user_synced)

    async def handle_sync(self, result: SyncResult) -> None:
        """Welcome every user who joined in this sync slice (a :class:`~onbot.sync.SyncPump` handler)."""
        for mxid in extract_joined_users(result):
            await self._maybe_welcome(mxid)

    async def _on_user_synced(self, event: Event) -> None:
        await self._maybe_welcome(event.payload["mxid"])

    async def _maybe_welcome(self, mxid: str) -> None:
        if mxid == self.bot_id or mxid in self.config.matrix_user_ignore_list:
            return
        if mxid in self._welcomed:
            return
        try:
            await self.welcome.welcome_user(mxid)
        except Exception:
            # Not remembered, so the next tick retries: a homeserver that was briefly unreachable
            # must not cost the user their welcome.
            log.exception("welcome flow failed for %s", mxid)
            return
        self._welcomed.add(mxid)
