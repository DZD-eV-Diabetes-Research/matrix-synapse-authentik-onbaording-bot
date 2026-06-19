"""Onboarding listener (AD-3): event-driven welcome via the sync stream + the reconciler signal.

Two trigger paths converge here (AD-4 explicit coupling):

* **Reconciler signal** — the reconciler emits :attr:`Signal.user_synced` for every provisioned user
  it sees; the listener subscribes and welcomes them. This is the dependable path and works without
  any sync support.
* **Sync stream** — :meth:`run` consumes Simplified Sliding Sync and welcomes users on ``join``
  membership events, for instant onboarding with no tick latency.

Both funnel through :meth:`_maybe_welcome`, which filters out the bot and ignored users and defers to
the idempotent :class:`~onbot.onboarding.welcome.WelcomeService` (so duplicate triggers are safe).
"""

from __future__ import annotations

import asyncio
import contextlib

from onbot.clients.matrix import ApiClientMatrix, SyncResult
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.logging import get_logger
from onbot.onboarding.welcome import WelcomeService

log = get_logger(__name__)

_ERROR_BACKOFF_SEC = 5.0


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
        self._pos: str | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """Subscribe to the reconciler's user-provisioned signal (call once, before running)."""
        self.events.subscribe(Signal.user_synced, self._on_user_synced)

    def request_stop(self) -> None:
        self._stop.set()

    async def _on_user_synced(self, event: Event) -> None:
        await self._maybe_welcome(event.payload["mxid"])

    async def _maybe_welcome(self, mxid: str) -> None:
        if mxid == self.bot_id or mxid in self.config.matrix_user_ignore_list:
            return
        try:
            await self.welcome.welcome_user(mxid)
        except Exception:
            log.exception("welcome flow failed for %s", mxid)

    async def run(self) -> None:
        """Consume the sync stream and welcome joining users until stopped."""
        log.info("onboarding listener started (sliding sync)")
        while not self._stop.is_set():
            try:
                result = await self.client.sliding_sync(self._pos)
            except Exception:
                log.exception("sync failed; backing off %.0fs", _ERROR_BACKOFF_SEC)
                await self._sleep(_ERROR_BACKOFF_SEC)
                continue
            self._pos = result.pos
            for mxid in extract_joined_users(result):
                if self._stop.is_set():
                    break
                await self._maybe_welcome(mxid)
        log.info("onboarding listener stopped")

    async def _sleep(self, seconds: float) -> None:
        # Sleep, but wake immediately on stop.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
