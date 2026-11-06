"""Composition root: build clients, wire the reconciler + onboarding, manage lifecycle.

Keeps construction in one place (AD-4) so the CLI stays thin. The reconciler converges
Authentik→Matrix state; onboarding reacts (event-driven) to the reconciler's "user provisioned"
signal and to Matrix join events. They share the async API clients and an in-process event bus.

Process lifecycle (review findings RUN-02, RUN-09, RUN-12, RUN-13):

* **Construction does no I/O.** :func:`build_app` only builds objects, each client registered with
  an :class:`~contextlib.AsyncExitStack` the moment it exists, so a failure halfway through closes
  everything built so far.
* **Startup steps that talk to the homeserver are retried, not attempted once.** Version
  negotiation, device registration, the rate-limit override, the bot avatar and the admin control
  room each run under a :class:`~onbot.runtime.StartupSupervisor`, in the background, beside the
  loops. A step that fails because Synapse or MAS is still booting is retried with a capped backoff
  until it succeeds; one the server refuses for good (a 4xx) is given up with an error, as before.
  The reconcile loop never waits for them.
* **Onboarding opens only after device registration.** Under MAS a message send fails until the
  bot's device is registered, so the welcome flow is subscribed once that step is done, and the
  engine is then asked for a pass, so nobody waits a whole tick for a welcome.
* **Signals are handled from the first moment** (:class:`~onbot.runtime.Shutdown`), not only once
  the reconcile loop runs. On SIGTERM or SIGINT the loops are asked to stop, the read-only ones are
  cancelled at once, and the rest get ``SHUTDOWN_GRACE_SEC`` (6 s) to finish what they are doing
  before they are cancelled too. A second signal cancels immediately. That keeps a ``docker stop``
  (10 s by default) clean instead of ending in SIGKILL.
* **One-shot commands only do what they need.** ``reconcile-once`` runs every startup step once
  (device, rate-limit override, bot avatar, admin room and its re-invites), because a cron-driven
  deployment never runs the service and still needs all of them, and then one pass. ``broadcast``
  builds only the Matrix clients, registers the device and lifts the rate limit. Neither starts the
  sync stream, and neither writes the heartbeat file.
* **``reconcile-once --dry-run`` changes nothing** (MNT-09). It reads the real state and logs every
  change the pass would make, including what the destructive-action guard would pause, but every
  write goes to a recording wrapper, offboarding runs in its own dry-run mode, and no bookkeeping
  (ledger, guard state) is saved. Only version negotiation runs as a startup step.
* **Reports reach the admin room.** With the control room enabled, an :class:`AdminRoomNotifier`
  posts the guard's paused batches, offboarding that needs a human and rooms that did not converge.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastService
from onbot.admin.control_room import ControlRoomHandler
from onbot.admin.notifier import AdminRoomNotifier
from onbot.auth.token_provider import (
    OAuth2ClientCredentialsTokenProvider,
    StaticTokenProvider,
    TokenProvider,
)
from onbot.avatar import ensure_bot_avatar
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.mas_admin import ApiClientMasAdmin
from onbot.clients.matrix import ApiClientMatrix, CSApiEffectors
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig, SynapseServer
from onbot.discovery import DiscoveryPoller
from onbot.events import EventBus, Signal
from onbot.guard import (
    DestructiveActionGuard,
    GuardLimits,
    GuardStore,
    MatrixAccountDataGuardStore,
    ReadOnlyGuardStore,
)
from onbot.heartbeat import RECONCILE, SYNC, SYNC_MAX_AGE_SEC, Heartbeat, reconcile_max_age_sec
from onbot.lifecycle.accounts import (
    AccountLifecycleManager,
    AdminApiLifecycleEffectors,
    KnownUsers,
    LifecycleEffectors,
    LifecycleLedger,
    LifecycleLedgerStore,
    MasLifecycleEffectors,
    MatrixAccountDataLedgerStore,
)
from onbot.logging import get_logger
from onbot.media import MediaUploader
from onbot.onboarding.listener import OnboardingListener
from onbot.onboarding.welcome import WelcomeService
from onbot.reconciler.effectors import DryRunEffectors, DryRunSynapseAdmin, MatrixEffectors
from onbot.reconciler.engine import ReconcilerEngine
from onbot.rooms.admin import AdminRoomProvisioner
from onbot.runtime import (
    SHUTDOWN_GRACE_SEC,
    Shutdown,
    StartupStep,
    StartupSupervisor,
    cancel,
    drain,
    run_interruptible,
)
from onbot.safe_fetch import FetchPolicy, SafeImageFetcher
from onbot.sync import SyncPump

__all__ = [
    "App",
    "build_app",
    "build_matrix_token_provider",
    "run_broadcast",
    "run_interruptible",
    "run_reconcile_once",
    "run_service",
]

log = get_logger(__name__)


def build_matrix_token_provider(synapse: SynapseServer) -> TokenProvider:
    """The bot's credential for the Synapse APIs (AD-6): its static compatibility token.

    OAuth2 client credentials are used for the MAS admin API only (``mas_admin``); MAS does not grant
    the Matrix API scopes to that grant (RUN-11).
    """
    return StaticTokenProvider(synapse.bot_access_token.get_secret_value())


def _fetch_policy(config: OnbotConfig) -> FetchPolicy:
    """The avatar download limits from ``avatar_fetch`` (SEC-03, D11)."""
    cfg = config.avatar_fetch
    return FetchPolicy.from_entries(
        cfg.allowed_internal_destinations, max_bytes=cfg.max_size_bytes, timeout_sec=cfg.timeout_sec
    )


async def _relax_bot_ratelimit(admin: ApiClientSynapseAdmin, config: OnbotConfig) -> None:
    """Lift Synapse's per-user send limit for the bot.

    A broadcast is one account writing into every managed direct room at once, which the limiter sees
    as a flood. A homeserver that refuses the override for good (no admin rights, the endpoint
    remounted) only makes broadcasts slower, never wrong: the fan-out is bounded and retries 429s (see
    :mod:`onbot.admin.broadcast`).
    """
    await admin.override_ratelimit(config.synapse_server.bot_user_id)
    log.info("lifted Synapse send rate limit for %s", config.synapse_server.bot_user_id)


class _ReadOnlyLedgerStore:
    """Reads the lifecycle ledger, never writes it (``reconcile-once --dry-run``)."""

    def __init__(self, inner: LifecycleLedgerStore) -> None:
        self.inner = inner

    async def load(self) -> LifecycleLedger:
        return await self.inner.load()

    async def save(self, ledger: LifecycleLedger) -> None:
        log.info("[dry-run] would save the offboarding ledger (%d entries)", len(ledger.entries))

    async def load_known_users(self) -> KnownUsers:
        return await self.inner.load_known_users()

    async def save_known_users(self, known: KnownUsers) -> None:
        log.info("[dry-run] would save the known users record (%d users)", len(known.users))


# --- wiring -----------------------------------------------------------------------------------


@dataclass(slots=True)
class App:
    """Wired application: the reconciler engine and the sync-driven handlers over a shared bus."""

    config: OnbotConfig
    matrix: ApiClientMatrix
    admin: ApiClientSynapseAdmin
    media: MediaUploader
    events: EventBus
    engine: ReconcilerEngine
    listener: OnboardingListener
    broadcast: BroadcastService
    pump: SyncPump
    discovery: DiscoveryPoller
    admins: AdminResolver
    guard: DestructiveActionGuard
    control_room: ControlRoomHandler | None = None
    notifier: AdminRoomNotifier | None = None
    dry_run: bool = False
    _onboarding_open: bool = False
    _control_room_subscribed: bool = False

    # Startup step actions. Each is safe to repeat after a partial failure.

    async def negotiate_versions(self) -> None:
        await self.matrix.negotiate_versions()

    async def register_device(self) -> None:
        await self.matrix.ensure_device_registered()

    async def open_onboarding(self) -> None:
        """Subscribe the welcome flow, once message sends can work (after device registration)."""
        if self._onboarding_open:
            return
        self._onboarding_open = True
        self.listener.start()  # the reconciler's user-provisioned signal (AD-4)
        self.pump.register(self.listener)
        if self.engine.passes_started:
            # Users seen by passes that ran before this point were not welcomed; do not make them
            # wait for the next tick.
            self.engine.trigger()
        log.info("onboarding active")

    async def check_offboarding(self) -> None:
        """Log the offboarding setup and report what the effectors found wrong (LIF-09).

        Detects a MAS-backed homeserver behind the Synapse fallback (offboarding is then refused) and
        reads what the configured MAS can do. Each finding goes to the log at ERROR and, once, to the
        admin-room signal.
        """
        lifecycle = self.engine.lifecycle
        if lifecycle is None:
            return
        for warning in await lifecycle.preflight():
            await self.events.emit(Signal.lifecycle_attention, mxid=None, action="preflight", detail=warning)

    async def relax_ratelimit(self) -> None:
        await _relax_bot_ratelimit(self.admin, self.config)

    async def apply_bot_avatar(self) -> None:
        """Set the bot's own avatar from the configured URL (G6.8).

        Idempotent: re-setting an unchanged picture would announce "changed their profile picture"
        in every room the bot is in, so :mod:`onbot.avatar` writes the profile only when it has to.
        """
        await ensure_bot_avatar(
            self.matrix,
            self.media,
            user_id=self.config.synapse_server.bot_user_id,
            server_name=self.config.synapse_server.server_name,
            source_url=self.config.synapse_server.bot_avatar_url,
        )

    async def start_control_room(self) -> None:
        """Provision the admin control room and bind its command router (ADR-0010)."""
        handler = self.control_room
        if handler is None:
            return
        provisioner = AdminRoomProvisioner(self.matrix, self.config, self.admins)
        room_id = await provisioner.ensure()
        if room_id is None:
            return
        try:
            await handler.start(room_id)
        except Exception:
            handler.room_id = None  # never route commands without the restored replay cursor
            raise
        if self.notifier is not None:
            await self.notifier.bind(room_id)
        if not self._control_room_subscribed:
            # Re-invite on every reconcile, so somebody added to the Authentik admin group gets into
            # the room on the same tick that grants them the right to command the bot.
            self.events.subscribe(Signal.reconcile_completed, provisioner.on_reconcile)
            self._control_room_subscribed = True
        if self.pump.has_position:
            # The pump ran before the room was bound, so commands sent meanwhile went past the
            # handler. A fresh connection replays them; the handler's replay guard filters the rest.
            self.pump.restart_stream()

    def service_steps(self) -> list[StartupStep]:
        """Startup steps of the long-running service, in dependency order."""
        device = StartupStep("device registration", self.register_device)
        steps = [
            StartupStep("version negotiation", self.negotiate_versions),
            device,
            StartupStep("onboarding", self.open_onboarding, after=device),
            StartupStep("rate-limit override", self.relax_ratelimit),
            StartupStep("offboarding check", self.check_offboarding),
        ]
        if self.config.synapse_server.bot_avatar_url:
            steps.append(StartupStep("bot avatar", self.apply_bot_avatar))
        if self.control_room is not None:
            # Posting the pinned help is a message send, so it needs the device too.
            steps.append(StartupStep("admin control room", self.start_control_room, after=device))
        return steps

    def reconcile_once_steps(self) -> list[StartupStep]:
        """The service's startup steps, for a single pass that runs each of them once, in list order.

        A cron-driven ``reconcile-once`` deployment never runs the service, so it needs all of them: a
        registered device for the welcome sends a pass may trigger, the lifted rate limit (a pass
        creates rooms and sends messages just like the service), the bot avatar, and the admin room,
        whose re-invite of new admins rides on the pass's ``reconcile_completed`` signal. A dry run
        only negotiates versions: every other step writes.
        """
        if self.dry_run:
            return [StartupStep("version negotiation", self.negotiate_versions)]
        return self.service_steps()


def _matrix_clients(
    stack: AsyncExitStack, config: OnbotConfig
) -> tuple[ApiClientMatrix, ApiClientSynapseAdmin]:
    # One MAS-aware token provider shared by the admin + CS clients (same bot identity, AD-6).
    token_provider = build_matrix_token_provider(config.synapse_server)
    stack.push_async_callback(token_provider.aclose)
    admin = ApiClientSynapseAdmin(
        server_url=config.synapse_server.server_url,
        token_provider=token_provider,
        admin_api_path=config.synapse_server.admin_api_path,
    )
    stack.push_async_callback(admin.aclose)
    matrix = ApiClientMatrix(
        server_url=config.synapse_server.server_url,
        token_provider=token_provider,
        server_name=config.synapse_server.server_name,
        room_version=config.synapse_server.room_version,
    )
    stack.push_async_callback(matrix.aclose)
    return matrix, admin


@asynccontextmanager
async def build_app(
    config: OnbotConfig, *, heartbeat: Heartbeat | None = None, dry_run: bool = False
) -> AsyncIterator[App]:
    """Construct the reconciler + onboarding with their clients, closing them on exit.

    No network I/O happens here; see :class:`StartupSupervisor` for what runs against the servers.
    ``dry_run`` wires the pass for ``reconcile-once --dry-run``: writes are logged, not sent.
    """
    async with AsyncExitStack() as stack:
        authentik = ApiClientAuthentik(
            url=config.authentik_server.url,
            api_key=config.authentik_server.api_key.get_secret_value(),
        )
        stack.push_async_callback(authentik.aclose)
        matrix, admin = _matrix_clients(stack, config)
        # One uploader for the bot avatar, the group rooms and the onboarding rooms: it caches by
        # source URL, so the bot's avatar is fetched and uploaded once no matter how many rooms wear it.
        # The uploader does not own a fetcher it is handed, so the stack closes it (RUN-12).
        fetcher = SafeImageFetcher(_fetch_policy(config))
        stack.push_async_callback(fetcher.aclose)
        media = MediaUploader(matrix, fetcher=fetcher)
        stack.push_async_callback(media.aclose)
        events = EventBus()
        # Lifecycle enforcement: under MAS only the MAS admin API can end a live session (§7 Q1),
        # so prefer it when configured; otherwise fall back to the Synapse-admin effectors, which
        # ask the homeserver whether it is MAS-backed after all and then refuse every lock and
        # erase (LIF-09). Until it answered they act on nobody.
        lifecycle_effectors: LifecycleEffectors
        if config.mas_admin is not None:
            mas_admin = ApiClientMasAdmin(
                mas_url=config.mas_admin.url,
                token_provider=OAuth2ClientCredentialsTokenProvider(
                    token_endpoint=f"{config.mas_admin.url.rstrip('/')}/oauth2/token",
                    client_id=config.mas_admin.client_id,
                    client_secret=config.mas_admin.client_secret.get_secret_value(),
                    scope="urn:mas:admin",
                ),
            )
            stack.push_async_callback(mas_admin.aclose)
            lifecycle_effectors = MasLifecycleEffectors(mas_admin, synapse_admin=admin)
        else:
            lifecycle_effectors = AdminApiLifecycleEffectors(admin, matrix=matrix)
        bot_id, server_name = config.synapse_server.bot_user_id, config.synapse_server.server_name
        ledger_store: LifecycleLedgerStore = MatrixAccountDataLedgerStore(matrix, bot_id, server_name)
        guard_store: GuardStore = MatrixAccountDataGuardStore(matrix, bot_id, server_name)
        if dry_run:
            ledger_store = _ReadOnlyLedgerStore(ledger_store)
            guard_store = ReadOnlyGuardStore(guard_store)
        # A preview plans offboarding as configured, so it shows what the guard would do with the
        # locks of a live configuration, but it calls no effector (offboarding's own dry run).
        lifecycle = AccountLifecycleManager(
            config, store=ledger_store, effectors=lifecycle_effectors, preview=dry_run
        )
        # One guard for the engine (decides, records) and the control room (!confirm, !reject): its
        # state is reloaded under one lock for every operation (D13).
        guard = DestructiveActionGuard(GuardLimits.from_config(config), guard_store, events)
        live_effectors = CSApiEffectors(matrix, media=media)
        stack.push_async_callback(live_effectors.aclose)
        effectors: MatrixEffectors = live_effectors
        engine_admin = admin
        if dry_run:
            effectors = DryRunEffectors(reads=live_effectors)
            engine_admin = cast(ApiClientSynapseAdmin, DryRunSynapseAdmin(admin))
        engine = ReconcilerEngine(
            config,
            authentik,
            engine_admin,
            effectors=effectors,
            events=events,
            lifecycle=lifecycle,
            guard=guard,
            dry_run=dry_run,
        )
        welcome = WelcomeService(matrix, config, admin=admin, media=media)
        # Not started here: the welcome flow is subscribed by the "onboarding" startup step.
        listener = OnboardingListener(matrix, welcome, config, events)
        broadcast = BroadcastService(matrix, config)
        # One sync connection, fanned out to every consumer of the event stream (see onbot/sync.py).
        pump = SyncPump(matrix, heartbeat=heartbeat)
        # Watches Authentik cheaply and wakes the engine on a real change, so the engine's own tick
        # can stay slow (see onbot/discovery.py).
        discovery = DiscoveryPoller(authentik, config, engine.trigger)
        admins = AdminResolver(authentik, config)
        control_room: ControlRoomHandler | None = None
        notifier: AdminRoomNotifier | None = None
        if config.admin_room.enabled and not dry_run:
            # Registered now, bound to its room by the "admin control room" startup step; until then
            # it ignores every slice.
            control_room = ControlRoomHandler(matrix, config, broadcast, admins, engine=engine, guard=guard)
            pump.register(control_room)
            # Reports raised before the room is bound are queued and posted once it is.
            notifier = AdminRoomNotifier(matrix)
            notifier.subscribe(events)
        yield App(
            config=config,
            matrix=matrix,
            admin=admin,
            media=media,
            events=events,
            engine=engine,
            listener=listener,
            broadcast=broadcast,
            pump=pump,
            discovery=discovery,
            admins=admins,
            guard=guard,
            control_room=control_room,
            notifier=notifier,
            dry_run=dry_run,
        )


# --- commands ---------------------------------------------------------------------------------


async def run_service(
    config: OnbotConfig,
    *,
    heartbeat_path: Path | None = None,
    shutdown: Shutdown | None = None,
    grace: float = SHUTDOWN_GRACE_SEC,
) -> int:
    """Run the reconcile loop, the Authentik poll and the sync pump until a signal; return the exit code.

    Exit code 0 after a signal-initiated shutdown, 1 when a loop crashed (so a restart policy helps).
    """
    shutdown = shutdown or Shutdown()
    with shutdown.installed():  # first, so a SIGTERM during startup is a clean stop, not a kill
        heartbeat = Heartbeat(
            heartbeat_path,
            max_age_sec={
                RECONCILE: reconcile_max_age_sec(config.server_tick_rate_sec),
                SYNC: SYNC_MAX_AGE_SEC,
            },
        )
        heartbeat.start()
        async with build_app(config, heartbeat=heartbeat) as app:
            supervisor = StartupSupervisor(app.service_steps(), heartbeat=heartbeat)
            return await _supervise(app, supervisor, heartbeat, shutdown, grace=grace)


async def _supervise(
    app: App,
    supervisor: StartupSupervisor,
    heartbeat: Heartbeat,
    shutdown: Shutdown,
    *,
    grace: float,
) -> int:
    def on_pass(ok: bool) -> None:
        (heartbeat.ok if ok else heartbeat.failed)(RECONCILE)

    tasks: dict[str, asyncio.Task[None]] = {
        "startup": asyncio.create_task(supervisor.run(), name="onbot-startup"),
        "reconcile": asyncio.create_task(app.engine.run(on_pass=on_pass), name="onbot-reconcile"),
        "sync": asyncio.create_task(app.pump.run(), name="onbot-sync"),
        "discovery": asyncio.create_task(app.discovery.run(), name="onbot-discovery"),
    }
    names: dict[asyncio.Future[Any], str] = {task: name for name, task in tasks.items()}
    exit_code = 0
    stop = asyncio.ensure_future(shutdown.requested.wait())
    pending: set[asyncio.Future[Any]] = set(tasks.values())
    try:
        while True:
            done, _ = await asyncio.wait({*pending, stop}, return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                break
            crashed = False
            for task in done:
                pending.discard(task)
                exc = task.exception() if not task.cancelled() else asyncio.CancelledError()
                if exc is not None:
                    log.error("the %s loop crashed; stopping the service", names[task], exc_info=exc)
                    crashed = True
            if crashed:
                exit_code = 1
                break
            if tasks["reconcile"] in done:
                break  # the engine only returns when asked to stop
    finally:
        stop.cancel()
        await _stop_loops(app, supervisor, tasks, shutdown, grace=grace)
    log.info("onbot stopped")
    return exit_code


async def _stop_loops(
    app: App,
    supervisor: StartupSupervisor,
    tasks: dict[str, asyncio.Task[None]],
    shutdown: Shutdown,
    *,
    grace: float,
) -> None:
    app.engine.request_stop()
    app.pump.request_stop()  # abandons a long-poll in flight; a slice being handled is finished
    app.discovery.request_stop()
    supervisor.request_stop()
    # The discovery poll only reads Authentik; nothing is lost by cutting it off.
    tasks["discovery"].cancel()
    unfinished = await drain(set(tasks.values()), grace=grace, forced=shutdown.forced)
    if unfinished:
        log.warning(
            "cancelling %s after the %.0fs shutdown grace period",
            ", ".join(sorted(t.get_name() for t in unfinished)),
            grace,
        )
        await cancel(unfinished)
    # Collect results so no "exception was never retrieved" noise reaches the log.
    for task in tasks.values():
        if task.done() and not task.cancelled():
            task.exception()


async def run_reconcile_once(config: OnbotConfig, *, dry_run: bool = False) -> None:
    """Run a single reconcile pass and exit (``onbot reconcile-once``).

    Runs the service's startup steps once each (device, rate-limit override, bot avatar, admin room),
    then the pass. Onboarding still fires for users discovered this pass, but the long-running sync
    stream is not started, so admin room commands (``!confirm`` among them) are not read; the guard's
    reports are still posted to the admin room.

    ``dry_run`` reads everything and changes nothing: see the module docstring.
    """
    async with build_app(config, dry_run=dry_run) as app:
        await StartupSupervisor(app.reconcile_once_steps()).run_once()
        await app.engine.reconcile_once()


async def run_broadcast(config: OnbotConfig, message: str) -> int:
    """Send one announcement to every managed direct room (``onbot broadcast``).

    Returns a shell exit code: non-zero when any room refused the message, so a script can tell a
    partial delivery from a clean one. Builds only the Matrix clients it needs.
    """
    async with AsyncExitStack() as stack:
        matrix, admin = _matrix_clients(stack, config)
        await StartupSupervisor(
            [
                StartupStep("device registration", matrix.ensure_device_registered),
                StartupStep("rate-limit override", lambda: _relax_bot_ratelimit(admin, config)),
            ]
        ).run_once()
        result = await BroadcastService(matrix, config).broadcast(message)
    print(result.summary())
    return 1 if result.failures else 0
