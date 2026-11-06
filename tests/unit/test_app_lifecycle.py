"""Process lifecycle of the composition root (review findings RUN-02, RUN-09, RUN-12, RUN-13).

The cold start (RUN-02) is simulated here with the real wiring from :func:`onbot.app.build_app` and
fakes only at the network edge: a homeserver that refuses connections until it is "up", and a welcome
send that fails with the MAS foreign-key 500 until the bot's device is registered. The live version
of that scenario is ``tests/integration/test_runtime.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from onbot import app as app_module
from onbot import runtime
from onbot.admin.control_room import ControlRoomHandler
from onbot.app import build_app, run_broadcast, run_reconcile_once, run_service
from onbot.clients.base import ApiError, BaseApiClient
from onbot.clients.matrix import ApiClientMatrix
from onbot.config import AuthentikServer, MasAdmin, OnbotConfig, SynapseServer
from onbot.events import Signal
from onbot.rooms.admin import AdminRoomProvisioner
from onbot.runtime import (
    Shutdown,
    StartupStep,
    StartupSupervisor,
    is_permanent_failure,
    run_interruptible,
)
from onbot.safe_fetch import SafeImageFetcher

HS = "https://matrix.test"


@pytest.fixture(autouse=True)
def _homeserver_without_mas(monkeypatch: pytest.MonkeyPatch) -> None:
    """The offboarding check asks the homeserver for its auth issuer; these tests are not about it."""

    async def no_issuer(_self: object) -> None:
        return None

    monkeypatch.setattr(ApiClientMatrix, "get_auth_issuer", no_issuer)


def _config(server_url: str = HS, **overrides: Any) -> OnbotConfig:
    config = OnbotConfig(
        synapse_server=SynapseServer(
            server_name="example.test",
            server_url=server_url,
            bot_user_id="@bot:example.test",
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(
            url=overrides.pop("authentik_url", "https://authentik.test"), api_key="ak"
        ),
        **overrides,
    )
    return config


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _down() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused")


# --- StartupSupervisor ------------------------------------------------------------------------


async def test_a_transient_failure_is_retried_until_it_succeeds() -> None:
    attempts: list[int] = []

    async def flaky() -> None:
        attempts.append(1)
        if len(attempts) < 4:
            raise _down()

    step = StartupStep("flaky", flaky)
    await asyncio.wait_for(
        StartupSupervisor([step], initial_backoff_sec=0.001, max_backoff_sec=0.01).run(), timeout=2
    )

    assert len(attempts) == 4
    assert step.succeeded and step.done.is_set()


async def test_a_permanent_refusal_is_given_up_after_one_attempt() -> None:
    attempts: list[int] = []

    async def refused() -> None:
        attempts.append(1)
        raise ApiError("POST", "u", 403, {"errcode": "M_FORBIDDEN"})

    step = StartupStep("refused", refused)
    await asyncio.wait_for(StartupSupervisor([step], initial_backoff_sec=0.001).run(), timeout=2)

    assert attempts == [1]
    assert step.done.is_set() and not step.succeeded


_REQ = httpx.Request("GET", "https://cdn.example/avatar.png")


@pytest.mark.parametrize(
    ("exc", "permanent"),
    [
        (ApiError("GET", "u", 403, {"errcode": "M_FORBIDDEN", "error": "no"}), True),
        (ApiError("GET", "u", 400, {"errcode": "M_BAD_JSON"}), True),
        (ApiError("GET", "u", 404, {"errcode": "M_UNRECOGNIZED"}), True),
        (ApiError("GET", "u", 401, {"errcode": "M_UNKNOWN_TOKEN"}), False),  # MAS not introspecting yet
        (ApiError("GET", "u", 429, {"errcode": "M_LIMIT_EXCEEDED"}), False),
        (ApiError("GET", "u", 502), False),
        # A reverse proxy in front of a homeserver that is still booting (RUN-02): no Matrix body.
        (ApiError("GET", "u", 404, "<html><body>404 Not Found</body></html>"), False),
        (ApiError("GET", "u", 403, "Forbidden"), False),
        (ApiError("GET", "u", 403), False),
        (ApiError("GET", "u", 421, {"detail": "misdirected"}), False),
        (ApiError("GET", "u", 404, {"errcode": ""}), False),
        # The avatar's own host: a 4xx there is final, a 5xx is not.
        (httpx.HTTPStatusError("x", request=_REQ, response=httpx.Response(404, request=_REQ)), True),
        (httpx.HTTPStatusError("x", request=_REQ, response=httpx.Response(503, request=_REQ)), False),
        (httpx.ConnectError("refused"), False),
        (RuntimeError("anything else"), False),
    ],
)
def test_failure_classification(exc: BaseException, permanent: bool) -> None:
    assert is_permanent_failure(exc) is permanent


async def test_a_dependent_step_waits_for_its_predecessor() -> None:
    order: list[str] = []
    gate = asyncio.Event()

    async def first() -> None:
        await gate.wait()
        order.append("first")

    async def second() -> None:
        order.append("second")

    one = StartupStep("first", first)
    two = StartupStep("second", second, after=one)
    task = asyncio.create_task(StartupSupervisor([two, one]).run())
    await asyncio.sleep(0.05)
    assert order == []
    gate.set()
    await asyncio.wait_for(task, timeout=1)

    assert order == ["first", "second"]


async def test_stop_interrupts_a_retry_backoff() -> None:
    async def down() -> None:
        raise _down()

    supervisor = StartupSupervisor([StartupStep("down", down)], initial_backoff_sec=3600)
    task = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.05)
    supervisor.request_stop()
    await asyncio.wait_for(task, timeout=1)


# --- RUN-02: the cold start, simulated ---------------------------------------------------------


class _Homeserver:
    """The network edge of the cold start: down until ``up()``, then a MAS-backed Synapse."""

    def __init__(self) -> None:
        self.is_up = False
        self.device_registered = False
        self.ratelimit_lifted = False
        self.control_room: str | None = None
        self.welcomed: list[str] = []

    def up(self) -> None:
        self.is_up = True

    def _check(self) -> None:
        if not self.is_up:
            raise _down()

    async def negotiate_versions(self) -> None:
        self._check()

    async def ensure_device_registered(self) -> None:
        self._check()
        self.device_registered = True

    async def override_ratelimit(self, _user_id: str) -> None:
        self._check()
        self.ratelimit_lifted = True

    async def ensure_control_room(self) -> str:
        self._check()
        self.control_room = "!control:example.test"
        return self.control_room

    async def welcome_user(self, mxid: str) -> None:
        self._check()
        if not self.device_registered:
            # What a MAS compat token without a device row gets on every send (08 check 10).
            raise ApiError("PUT", "send", 500, {"errcode": "M_UNKNOWN", "error": "Internal server error"})
        self.welcomed.append(mxid)


async def test_cold_start_heals_once_the_homeserver_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    hs = _Homeserver()
    config = _config()
    config.admin_room.enabled = True

    async def ensure(_self: AdminRoomProvisioner) -> str:
        return await hs.ensure_control_room()

    async def start(self: ControlRoomHandler, room_id: str) -> None:
        self.room_id = room_id

    monkeypatch.setattr(AdminRoomProvisioner, "ensure", ensure)
    monkeypatch.setattr(ControlRoomHandler, "start", start)

    async with build_app(config) as app:
        monkeypatch.setattr(app.matrix, "negotiate_versions", hs.negotiate_versions)
        monkeypatch.setattr(app.matrix, "ensure_device_registered", hs.ensure_device_registered)
        monkeypatch.setattr(app.admin, "override_ratelimit", hs.override_ratelimit)
        monkeypatch.setattr(app.listener.welcome, "welcome_user", hs.welcome_user)

        supervisor = StartupSupervisor(app.service_steps(), initial_backoff_sec=0.005, max_backoff_sec=0.02)
        task = asyncio.create_task(supervisor.run())

        # Synapse is still booting. The engine's first pass runs regardless (it is not blocked by
        # startup) and sees a user; nobody tries to welcome them into a broken send path.
        app.engine.passes_started = 1
        await app.events.emit(Signal.user_synced, mxid="@alice:example.test")
        await asyncio.sleep(0.05)
        assert {"device registration", "rate-limit override", "admin control room"} <= set(supervisor.pending)
        assert hs.welcomed == []

        hs.up()
        await asyncio.wait_for(task, timeout=2)

        # Every step healed without a restart (before the fix each was tried exactly once).
        assert supervisor.pending == []
        assert hs.device_registered and hs.ratelimit_lifted
        assert app.control_room is not None and app.control_room.room_id == hs.control_room
        assert app.events._handlers[Signal.reconcile_completed]  # admin invites ride on the tick
        # Onboarding opened after the device and asked for a pass, so alice need not wait a tick.
        assert app.engine._trigger.is_set()
        await app.events.emit(Signal.user_synced, mxid="@alice:example.test")
        assert hs.welcomed == ["@alice:example.test"]


async def test_onboarding_opens_even_when_device_registration_is_refused_for_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with build_app(_config()) as app:

        async def refused() -> None:
            raise ApiError("POST", "keys/upload", 404, {"errcode": "M_UNRECOGNIZED"})

        async def noop(*_a: object) -> None:
            return None

        monkeypatch.setattr(app.matrix, "negotiate_versions", noop)
        monkeypatch.setattr(app.matrix, "ensure_device_registered", refused)
        monkeypatch.setattr(app.admin, "override_ratelimit", noop)

        await asyncio.wait_for(StartupSupervisor(app.service_steps()).run(), timeout=2)

        assert app.events._handlers[Signal.user_synced]  # as before the fix: try the sends anyway


async def test_a_proxy_404_during_boot_does_not_open_onboarding_without_a_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUN-02 behind a reverse proxy: a bare 404 while Synapse boots is retried, not given up."""
    attempts: list[int] = []

    async with build_app(_config()) as app:

        async def booting() -> None:
            attempts.append(1)
            if len(attempts) < 3:
                raise ApiError("POST", "keys/upload", 404, "<html><body>404 Not Found</body></html>")

        async def noop(*_a: object) -> None:
            return None

        monkeypatch.setattr(app.matrix, "negotiate_versions", noop)
        monkeypatch.setattr(app.matrix, "ensure_device_registered", booting)
        monkeypatch.setattr(app.admin, "override_ratelimit", noop)

        steps = app.service_steps()
        supervisor = StartupSupervisor(steps, initial_backoff_sec=0.001, max_backoff_sec=0.01)
        await asyncio.wait_for(supervisor.run(), timeout=2)

        assert len(attempts) == 3
        device = next(s for s in steps if s.name == "device registration")
        assert device.succeeded
        assert app.events._handlers[Signal.user_synced]


async def test_a_late_control_room_replays_the_stream_it_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    config.admin_room.enabled = True

    async def ensure(_self: AdminRoomProvisioner) -> str:
        return "!control:example.test"

    async def start(self: ControlRoomHandler, room_id: str) -> None:
        self.room_id = room_id

    monkeypatch.setattr(AdminRoomProvisioner, "ensure", ensure)
    monkeypatch.setattr(ControlRoomHandler, "start", start)
    async with build_app(config) as app:
        app.pump._pos = "p7"  # the pump already consumed slices the handler could not see

        await app.start_control_room()

        assert not app.pump.has_position


async def test_a_control_room_without_its_cursor_routes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    config.admin_room.enabled = True

    async def ensure(_self: AdminRoomProvisioner) -> str:
        return "!control:example.test"

    monkeypatch.setattr(AdminRoomProvisioner, "ensure", ensure)
    async with build_app(config) as app:
        assert app.control_room is not None
        monkeypatch.setattr(app.matrix, "get_account_data", _raise_down)

        with pytest.raises(httpx.ConnectError):
            await app.start_control_room()

        assert app.control_room.room_id is None


async def _raise_down(*_a: object, **_k: object) -> Any:
    raise _down()


# --- RUN-12: nothing leaks when construction fails ---------------------------------------------


async def test_clients_built_before_a_failure_are_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []
    original = BaseApiClient.aclose

    async def spy(self: BaseApiClient) -> None:
        closed.append(type(self).__name__)
        await original(self)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("engine construction failed")

    monkeypatch.setattr(BaseApiClient, "aclose", spy)
    monkeypatch.setattr(app_module, "ReconcilerEngine", boom)
    config = _config(mas_admin=MasAdmin(url="https://mas.test", client_id="c", client_secret="s"))

    with pytest.raises(RuntimeError, match="engine construction failed"):
        async with build_app(config):
            pass

    assert sorted(closed) == [
        "ApiClientAuthentik",
        "ApiClientMasAdmin",
        "ApiClientMatrix",
        "ApiClientSynapseAdmin",
    ]


async def test_the_avatar_fetcher_is_closed_with_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fetcher's HTTP client belongs to the app, not to the uploader it is handed to (RUN-12)."""
    closed: list[SafeImageFetcher] = []
    original = SafeImageFetcher.aclose

    async def spy(self: SafeImageFetcher) -> None:
        closed.append(self)
        await original(self)

    monkeypatch.setattr(SafeImageFetcher, "aclose", spy)
    async with build_app(_config()) as app:
        fetcher = app.media._fetcher
        assert not fetcher._http.is_closed
    assert closed == [fetcher]
    assert fetcher._http.is_closed


async def test_the_avatar_fetcher_is_closed_when_construction_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[SafeImageFetcher] = []
    original = SafeImageFetcher.aclose

    async def spy(self: SafeImageFetcher) -> None:
        closed.append(self)
        await original(self)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("engine construction failed")

    monkeypatch.setattr(SafeImageFetcher, "aclose", spy)
    monkeypatch.setattr(app_module, "ReconcilerEngine", boom)
    with pytest.raises(RuntimeError, match="engine construction failed"):
        async with build_app(_config()):
            pass
    assert len(closed) == 1


# --- RUN-13: one-shot commands skip what they do not need --------------------------------------


def _called_paths(router: respx.Router) -> set[str]:
    return {f"{call.request.method} {call.request.url.path}" for call in router.calls}


async def test_broadcast_touches_only_what_a_broadcast_needs() -> None:
    config = _config()
    config.admin_room.enabled = True
    config.synapse_server.bot_avatar_url = "https://cdn.test/bot.png"
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as router:
        router.get(f"{HS}/_matrix/client/v3/account/whoami").respond(
            200, json={"user_id": "@bot:example.test", "device_id": "DEV"}
        )
        router.get(f"{HS}/_matrix/client/v3/user/@bot:example.test/account_data/m.direct").respond(
            200, json={}
        )

        assert await run_broadcast(config, "hello") == 0
        called = _called_paths(router)

    assert called == {
        "GET /_matrix/client/v3/account/whoami",
        "POST /_matrix/client/v3/keys/upload",
        "POST /_synapse/admin/v1/users/@bot:example.test/override_ratelimit",
        "GET /_matrix/client/v3/user/@bot:example.test/account_data/m.direct",
    }


async def test_reconcile_once_runs_every_startup_step_before_its_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cron-driven reconcile-once never runs the service, so it needs the whole startup: the lifted
    rate limit (a pass creates rooms and sends messages), the bot avatar, the admin room, and the
    re-invite of new admins after the pass."""
    config = _config()
    config.admin_room.enabled = True
    config.synapse_server.bot_avatar_url = "https://cdn.test/bot.png"
    calls: list[str] = []

    async def avatar(*_a: object, **_k: object) -> None:
        calls.append("avatar")

    async def ensure(self: AdminRoomProvisioner) -> str:
        calls.append("admin room")
        self.room_id = "!control:example.test"
        return self.room_id

    async def invite(_self: AdminRoomProvisioner) -> None:
        calls.append("re-invite")

    async def start(self: ControlRoomHandler, room_id: str) -> None:
        self.room_id = room_id

    async def one_pass(self: Any) -> None:
        calls.append("pass")
        await self.events.emit(Signal.reconcile_completed)

    monkeypatch.setattr(app_module, "ensure_bot_avatar", avatar)
    monkeypatch.setattr(AdminRoomProvisioner, "ensure", ensure)
    monkeypatch.setattr(AdminRoomProvisioner, "ensure_admins_invited", invite)
    monkeypatch.setattr(ControlRoomHandler, "start", start)
    monkeypatch.setattr(app_module.ReconcilerEngine, "reconcile_once", one_pass)
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as router:
        router.get(f"{HS}/_matrix/client/versions").respond(200, json={"versions": ["v1.11"]})
        router.get(f"{HS}/_matrix/client/v3/account/whoami").respond(
            200, json={"user_id": "@bot:example.test", "device_id": "DEV"}
        )
        await run_reconcile_once(config)
        called = _called_paths(router)

    assert calls == ["avatar", "admin room", "pass", "re-invite"]
    assert called == {
        "GET /_matrix/client/versions",
        "GET /_matrix/client/v3/account/whoami",
        "POST /_matrix/client/v3/keys/upload",
        "POST /_synapse/admin/v1/users/@bot:example.test/override_ratelimit",
    }


async def test_reconcile_once_without_avatar_and_admin_room_skips_both() -> None:
    async with build_app(_config()) as app:
        names = [step.name for step in app.reconcile_once_steps()]
    assert names == [
        "version negotiation",
        "device registration",
        "onboarding",
        "rate-limit override",
        "offboarding check",
    ]


# --- RUN-09: bounded shutdown ------------------------------------------------------------------


@contextlib.contextmanager
def _black_hole() -> Iterator[str]:
    """A TCP port that accepts connections and never answers: every request hangs."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


async def test_service_stops_within_the_grace_period_while_everything_hangs(tmp_path: Path) -> None:
    # Startup steps, the reconcile pass and the long-poll all hang on a homeserver that never
    # answers. Before the fix only cooperative flags existed, so a stop waited for each of them.
    with _black_hole() as url:
        config = _config(url, authentik_url=url)
        shutdown = Shutdown()
        heartbeat = tmp_path / "hb.json"
        task = asyncio.create_task(
            run_service(config, heartbeat_path=heartbeat, shutdown=shutdown, grace=0.3)
        )
        await _until(heartbeat.exists)
        await asyncio.sleep(0.3)

        started = time.monotonic()
        shutdown.request(signal.SIGTERM)
        code = await asyncio.wait_for(task, timeout=5)

    assert code == 0
    assert time.monotonic() - started < 0.3 + runtime.CANCEL_TIMEOUT_SEC
    assert json.loads(heartbeat.read_text())["pid"] == os.getpid()


async def test_a_second_signal_cancels_at_once(tmp_path: Path) -> None:
    with _black_hole() as url:
        shutdown = Shutdown()
        heartbeat = tmp_path / "hb.json"
        task = asyncio.create_task(
            run_service(
                _config(url, authentik_url=url), heartbeat_path=heartbeat, shutdown=shutdown, grace=60
            )
        )
        await _until(heartbeat.exists)
        await asyncio.sleep(0.2)

        started = time.monotonic()
        shutdown.request(signal.SIGTERM)
        await asyncio.sleep(0.1)
        shutdown.request(signal.SIGTERM)
        assert await asyncio.wait_for(task, timeout=5) == 0

    assert time.monotonic() - started < 1 + runtime.CANCEL_TIMEOUT_SEC


async def test_a_crashed_loop_ends_the_service_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def crash(_self: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(app_module.DiscoveryPoller, "run", crash)
    with _black_hole() as url:
        code = await asyncio.wait_for(
            run_service(_config(url, authentik_url=url), heartbeat_path=tmp_path / "hb.json", grace=0.2),
            timeout=5,
        )

    assert code == 1


async def test_a_one_shot_command_finishing_within_the_grace_keeps_its_result() -> None:
    shutdown = Shutdown()

    async def work() -> int:
        shutdown.request(signal.SIGTERM)
        await asyncio.sleep(0.05)
        return 1

    assert await run_interruptible(work(), shutdown=shutdown, grace=1) == 1


async def test_a_hanging_one_shot_command_is_cancelled_after_the_grace() -> None:
    shutdown = Shutdown()
    cancelled: list[bool] = []

    async def work() -> None:
        shutdown.request(signal.SIGTERM)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    assert await asyncio.wait_for(run_interruptible(work(), shutdown=shutdown, grace=0.1), timeout=3) == 143
    assert cancelled == [True]


async def test_shutdown_restores_the_previous_signal_handlers() -> None:
    before = signal.getsignal(signal.SIGTERM)
    with Shutdown().installed():
        pass
    assert signal.getsignal(signal.SIGTERM) == before


# --- RUN-09 / SUP-10: a real SIGTERM to a real process -----------------------------------------


def test_sigterm_stops_onbot_run_cleanly(tmp_path: Path) -> None:
    """`onbot run` as a child process, every upstream hanging, then a real SIGTERM."""
    with _black_hole() as url:
        heartbeat = tmp_path / "hb.json"
        env = {
            **{k: v for k, v in os.environ.items() if not k.upper().startswith("ONBOT_")},
            "ONBOT_CONFIG_FILE_PATH": str(tmp_path / "absent.yml"),
            "ONBOT_HEARTBEAT_FILE": str(heartbeat),
            "ONBOT_SYNAPSE_SERVER__SERVER_NAME": "example.test",
            "ONBOT_SYNAPSE_SERVER__SERVER_URL": url,
            "ONBOT_SYNAPSE_SERVER__BOT_USER_ID": "@bot:example.test",
            "ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN": "tok",
            "ONBOT_AUTHENTIK_SERVER__URL": url,
            "ONBOT_AUTHENTIK_SERVER__API_KEY": "ak",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "onbot", "run"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not heartbeat.exists():
                assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
                assert time.monotonic() < deadline, "onbot run never wrote its heartbeat"
                time.sleep(0.05)
            time.sleep(0.5)  # startup steps and the first pass are now stuck on the black hole

            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            code = proc.wait(timeout=15)
            elapsed = time.monotonic() - started
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        stderr = proc.stderr.read() if proc.stderr else ""

    assert code == 0, stderr
    assert elapsed < runtime.SHUTDOWN_GRACE_SEC + runtime.CANCEL_TIMEOUT_SEC + 1, elapsed
    assert "received SIGTERM; shutting down" in stderr
