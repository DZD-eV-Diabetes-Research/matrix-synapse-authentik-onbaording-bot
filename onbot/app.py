"""Composition root: build clients, wire the reconciler + onboarding, manage lifecycle.

Keeps construction in one place (AD-4) so the CLI stays thin. The reconciler converges
Authentik→Matrix state; onboarding reacts (event-driven) to the reconciler's "user provisioned"
signal and to Matrix join events. They share the async API clients and an in-process event bus.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.matrix import ApiClientMatrix, CSApiEffectors
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.events import EventBus
from onbot.lifecycle.accounts import (
    AccountLifecycleManager,
    AdminApiLifecycleEffectors,
    MatrixAccountDataLedgerStore,
)
from onbot.logging import get_logger
from onbot.onboarding.listener import OnboardingListener
from onbot.onboarding.welcome import WelcomeService
from onbot.reconciler.engine import ReconcilerEngine

log = get_logger(__name__)


@dataclass(slots=True)
class App:
    """Wired application: the reconciler engine and the onboarding listener over a shared bus."""

    engine: ReconcilerEngine
    listener: OnboardingListener


@asynccontextmanager
async def build_app(config: OnbotConfig) -> AsyncIterator[App]:
    """Construct the reconciler + onboarding with their clients, closing them on exit."""
    authentik = ApiClientAuthentik(
        url=config.authentik_server.url,
        api_key=config.authentik_server.api_key,
    )
    admin = ApiClientSynapseAdmin(
        server_url=config.synapse_server.server_url,
        access_token=config.synapse_server.bot_access_token,
        admin_api_path=config.synapse_server.admin_api_path,
    )
    matrix = ApiClientMatrix(
        server_url=config.synapse_server.server_url,
        access_token=config.synapse_server.bot_access_token,
        server_name=config.synapse_server.server_name,
    )
    events = EventBus()
    lifecycle = AccountLifecycleManager(
        config,
        store=MatrixAccountDataLedgerStore(
            matrix, config.synapse_server.bot_user_id, config.synapse_server.server_name
        ),
        effectors=AdminApiLifecycleEffectors(admin),
    )
    engine = ReconcilerEngine(
        config, authentik, admin, effectors=CSApiEffectors(matrix), events=events, lifecycle=lifecycle
    )
    welcome = WelcomeService(matrix, config)
    listener = OnboardingListener(matrix, welcome, config, events)
    listener.start()  # subscribe onboarding to the reconciler's user-provisioned signal (AD-4)
    try:
        yield App(engine=engine, listener=listener)
    finally:
        await authentik.aclose()
        await admin.aclose()
        await matrix.aclose()


async def run_service(config: OnbotConfig) -> None:
    """Run the reconcile loop and the onboarding listener concurrently until stopped."""
    async with build_app(config) as app:
        # The engine owns the signal handlers; when it stops, stop the listener too.
        async def _reconcile() -> None:
            try:
                await app.engine.run()
            finally:
                app.listener.request_stop()

        await asyncio.gather(_reconcile(), app.listener.run())


async def run_reconcile_once(config: OnbotConfig) -> None:
    """Run a single reconcile pass and exit (``onbot reconcile-once``).

    Onboarding still fires for users discovered this pass — the listener is subscribed to the bus —
    but the long-running sync stream is not started.
    """
    async with build_app(config) as app:
        await app.engine.reconcile_once()
