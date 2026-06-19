"""Composition root: build clients, wire the reconciler, manage lifecycle.

Keeps construction in one place (AD-4) so the CLI stays thin. The Matrix CS client / effectors land
in Phase 4; until then the engine runs with :class:`DryRunEffectors` (writes are logged, not applied).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.events import EventBus
from onbot.logging import get_logger
from onbot.reconciler.engine import ReconcilerEngine

log = get_logger(__name__)


@asynccontextmanager
async def build_engine(config: OnbotConfig) -> AsyncIterator[ReconcilerEngine]:
    """Construct the reconciler with its clients, closing them on exit."""
    authentik = ApiClientAuthentik(
        url=config.authentik_server.url,
        api_key=config.authentik_server.api_key,
    )
    admin = ApiClientSynapseAdmin(
        server_url=config.synapse_server.server_url,
        access_token=config.synapse_server.bot_access_token,
        admin_api_path=config.synapse_server.admin_api_path,
    )
    engine = ReconcilerEngine(config, authentik, admin, events=EventBus())
    try:
        yield engine
    finally:
        await authentik.aclose()
        await admin.aclose()


async def run_service(config: OnbotConfig) -> None:
    """Run the long-lived reconcile loop (scheduled + on-demand) until stopped."""
    async with build_engine(config) as engine:
        await engine.run()


async def run_reconcile_once(config: OnbotConfig) -> None:
    """Run a single reconcile pass and exit (``onbot reconcile-once``)."""
    async with build_engine(config) as engine:
        await engine.reconcile_once()
