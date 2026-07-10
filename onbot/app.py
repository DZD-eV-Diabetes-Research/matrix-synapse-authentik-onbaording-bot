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

from onbot.auth.token_provider import (
    OAuth2ClientCredentialsTokenProvider,
    StaticTokenProvider,
    TokenProvider,
)
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.mas_admin import ApiClientMasAdmin
from onbot.clients.matrix import ApiClientMatrix, CSApiEffectors
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig, SynapseServer
from onbot.events import EventBus
from onbot.lifecycle.accounts import (
    AccountLifecycleManager,
    AdminApiLifecycleEffectors,
    LifecycleEffectors,
    MasLifecycleEffectors,
    MatrixAccountDataLedgerStore,
)
from onbot.logging import get_logger
from onbot.media import MediaUploader
from onbot.onboarding.listener import OnboardingListener
from onbot.onboarding.welcome import WelcomeService
from onbot.reconciler.engine import ReconcilerEngine

log = get_logger(__name__)


def build_matrix_token_provider(synapse: SynapseServer) -> TokenProvider:
    """Pick the bot's auth strategy (AD-6): OAuth2 client-credentials if configured, else a static
    compatibility/legacy token. Raises if neither is provided."""
    if synapse.oauth2 is not None:
        return OAuth2ClientCredentialsTokenProvider(
            token_endpoint=synapse.oauth2.token_endpoint,
            client_id=synapse.oauth2.client_id,
            client_secret=synapse.oauth2.client_secret,
            scope=synapse.oauth2.scope,
        )
    if synapse.bot_access_token:
        return StaticTokenProvider(synapse.bot_access_token)
    raise ValueError("synapse_server needs either bot_access_token or an oauth2 block")


async def _apply_bot_avatar(matrix: ApiClientMatrix, config: OnbotConfig, media: MediaUploader) -> None:
    """Set the bot's own avatar from the configured URL on startup (G6.8), best-effort."""
    url = config.synapse_server.bot_avatar_url
    if not url:
        return
    try:
        mxc = await media.upload_from_url(url)
        await matrix.set_user_avatar(config.synapse_server.bot_user_id, mxc)
        log.info("set bot avatar from %s", url)
    except Exception:
        log.exception("failed to set bot avatar from %s", url)


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
    # One MAS-aware token provider shared by the admin + CS clients (same bot identity, AD-6).
    token_provider = build_matrix_token_provider(config.synapse_server)
    admin = ApiClientSynapseAdmin(
        server_url=config.synapse_server.server_url,
        token_provider=token_provider,
        admin_api_path=config.synapse_server.admin_api_path,
    )
    matrix = ApiClientMatrix(
        server_url=config.synapse_server.server_url,
        token_provider=token_provider,
        server_name=config.synapse_server.server_name,
    )
    # Negotiate CS-API capabilities up front (sliding sync / authenticated media); best-effort so a
    # transient failure does not block startup — the listener re-checks and falls back if needed.
    try:
        await matrix.negotiate_versions()
    except Exception:
        log.exception("CS-API version negotiation failed; continuing with defaults")
    # Register the bot's device so welcome DM sends work under MAS (compat-token devices are
    # otherwise absent from Synapse's devices table; ADR-0006/0009).
    await matrix.ensure_device_registered()
    # One uploader for the bot avatar, the group rooms and the onboarding rooms: it caches by source
    # URL, so the bot's avatar is fetched and uploaded once no matter how many rooms wear it.
    media = MediaUploader(matrix)
    await _apply_bot_avatar(matrix, config, media)
    events = EventBus()
    # Lifecycle enforcement: under MAS only the MAS admin API can revoke a live session (§7 Q1), so
    # prefer it when configured; otherwise fall back to the Synapse-admin effectors.
    mas_admin: ApiClientMasAdmin | None = None
    lifecycle_effectors: LifecycleEffectors
    if config.mas_admin is not None:
        mas_admin = ApiClientMasAdmin(
            mas_url=config.mas_admin.url,
            token_provider=OAuth2ClientCredentialsTokenProvider(
                token_endpoint=f"{config.mas_admin.url.rstrip('/')}/oauth2/token",
                client_id=config.mas_admin.client_id,
                client_secret=config.mas_admin.client_secret,
                scope="urn:mas:admin",
            ),
        )
        lifecycle_effectors = MasLifecycleEffectors(mas_admin, synapse_admin=admin)
    else:
        lifecycle_effectors = AdminApiLifecycleEffectors(admin)
    lifecycle = AccountLifecycleManager(
        config,
        store=MatrixAccountDataLedgerStore(
            matrix, config.synapse_server.bot_user_id, config.synapse_server.server_name
        ),
        effectors=lifecycle_effectors,
    )
    effectors = CSApiEffectors(matrix, media=media)
    engine = ReconcilerEngine(
        config, authentik, admin, effectors=effectors, events=events, lifecycle=lifecycle
    )
    welcome = WelcomeService(matrix, config, admin=admin, media=media)
    listener = OnboardingListener(matrix, welcome, config, events)
    listener.start()  # subscribe onboarding to the reconciler's user-provisioned signal (AD-4)
    try:
        yield App(engine=engine, listener=listener)
    finally:
        await effectors.aclose()
        await media.aclose()
        await authentik.aclose()
        await admin.aclose()
        await matrix.aclose()
        if mas_admin is not None:
            await mas_admin.aclose()


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
