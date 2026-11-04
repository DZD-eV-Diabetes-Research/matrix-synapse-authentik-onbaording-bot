"""Runtime lifecycle against the live stack (review session S10, live checks 7, 10 and 11).

* check 7: the sync pump recovers from a connection position the server no longer knows, and the
  control room answers again (RUN-03, ONB-14);
* check 10: ``onbot run`` started while Synapse is down heals once Synapse is up: the device is
  registered, the admin room exists, a new user is welcomed (RUN-02);
* check 11: ``onbot run`` stops cleanly and quickly on SIGTERM (RUN-09), and its heartbeat makes the
  default ``onbot healthcheck`` pass while it runs (RUN-04).

The process tests run the real CLI as a child process, configured through a YAML file written from
the harness config. A child process is not PID 1, so these do not prove the container behaviour;
``docs/deployment.md`` records the ``docker stop`` measurements of the image.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from testcontainers.compose import DockerCompose

from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastService
from onbot.admin.control_room import ControlRoomHandler
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.rooms.admin import AdminRoomProvisioner
from onbot.sync import SyncPump
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration

STOP_BUDGET_SEC = 10.0  # docker stop's default timeout


async def _bot_messages(client: ApiClientMatrix, room_id: str) -> list[str]:
    resp = await client.get_json(f"v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 100})
    return [
        (ev.get("content") or {}).get("body", "")
        for ev in resp.get("chunk", [])
        if ev.get("type") == "m.room.message" and ev.get("sender") == S.BOT_USER_ID
    ]


async def _eventually(probe: Callable[[], Any], *, timeout: float, what: str, interval: float = 1.0) -> Any:
    """Await ``probe()`` until it returns something truthy (exceptions count as "not yet")."""
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            result = await probe()
            if result:
                return result
            last = result
        except Exception as exc:
            last = exc
        await asyncio.sleep(interval)
    raise AssertionError(f"{what} did not happen within {timeout:.0f}s (last: {last!r})")


# --- check 7 ----------------------------------------------------------------------------------


async def test_pump_recovers_from_an_unknown_position_and_the_control_room_answers(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    _a, admin = S.provision(authentik_admin, S.uniq("posadmin"))
    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]
    resolver = AdminResolver(authentik_client, config, ttl_sec=0)
    await matrix_client.ensure_device_registered()  # the session token's device may be new (RUN-02)
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver).ensure()
    assert control_room is not None
    await admin_client.add_user_to_room(control_room, admin.mxid)

    # A well-formed position of a connection the server does not know: what an expired or purged
    # connection looks like to the client (08 check 7).
    live = await matrix_client.sliding_sync(None, timeout_ms=0)
    assert live.pos and "/" in live.pos
    dead = "99999/" + live.pos.split("/", 1)[1]
    with pytest.raises(ApiError) as rejected:
        await matrix_client.sliding_sync(dead, timeout_ms=0)
    assert rejected.value.status_code == 400
    assert rejected.value.payload["errcode"] == "M_UNKNOWN_POS"

    handler = ControlRoomHandler(matrix_client, config, BroadcastService(matrix_client, config), resolver)
    await handler.start(control_room)
    pump = SyncPump(matrix_client)
    pump.register(handler)
    pump._pos = dead
    task = asyncio.create_task(pump.run())
    try:
        assert S.send_message_status(admin.access_token, control_room, "!status") == 200

        async def answered() -> bool:
            return any(m.startswith("onbot ") for m in await _bot_messages(matrix_client, control_room))

        await _eventually(answered, timeout=30, what="the !status reply")
        assert pump._pos is not None and pump._pos != dead
    finally:
        pump.request_stop()
        await asyncio.wait_for(task, timeout=5)


# --- the CLI as a child process ------------------------------------------------------------------


def _write_config(config: OnbotConfig, path: Path) -> Path:
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return path


@contextlib.contextmanager
def _onbot_run(config_file: Path, heartbeat: Path, log_file: Path) -> Iterator[subprocess.Popen[bytes]]:
    env = {
        **{k: v for k, v in os.environ.items() if not k.upper().startswith("ONBOT_")},
        "ONBOT_CONFIG_FILE_PATH": str(config_file),
        "ONBOT_HEARTBEAT_FILE": str(heartbeat),
    }
    with log_file.open("wb") as log:
        proc = subprocess.Popen([sys.executable, "-m", "onbot", "run"], env=env, stdout=log, stderr=log)
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _sigterm(proc: subprocess.Popen[bytes]) -> tuple[int, float]:
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    code = proc.wait(timeout=30)
    return code, time.monotonic() - started


def _heartbeat(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError, ValueError:
        return {}
    return data


def _ok(data: dict[str, Any], component: str) -> bool:
    return bool(((data.get("components") or {}).get(component) or {}).get("last_ok_at"))


def _healthcheck(heartbeat: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "onbot", "healthcheck", "--heartbeat-file", str(heartbeat)],
        capture_output=True,
        text=True,
        timeout=30,
    )


# --- check 11 ---------------------------------------------------------------------------------


async def test_onbot_run_is_healthy_and_stops_cleanly_on_sigterm(make_config, tmp_path: Path) -> None:
    config = make_config()
    config.server_tick_rate_sec = 30
    heartbeat = tmp_path / "hb.json"
    log_file = tmp_path / "onbot.log"
    with _onbot_run(_write_config(config, tmp_path / "config.yml"), heartbeat, log_file) as proc:

        async def steady() -> bool:
            assert proc.poll() is None, log_file.read_text()
            data = _heartbeat(heartbeat)
            return _ok(data, "reconcile") and _ok(data, "sync") and not data.get("startup_pending")

        await _eventually(steady, timeout=90, what="a steady state (reconcile + sync ok, startup done)")
        health = _healthcheck(heartbeat)
        assert health.returncode == 0, health.stdout + health.stderr

        await asyncio.sleep(3)  # the pump now sits in a 30 s long-poll
        code, elapsed = _sigterm(proc)

    log = log_file.read_text()
    assert code == 0, log
    assert elapsed < STOP_BUDGET_SEC, (elapsed, log)
    assert "onbot stopped" in log
    # The healthcheck now sees a dead writer.
    assert _healthcheck(heartbeat).returncode == 1


# --- check 10 ---------------------------------------------------------------------------------


def _fresh_bot_token(compose: DockerCompose) -> str:
    """A new compat token, so a new device the bot has never registered (as after a token rotation)."""
    out, err, _ = compose.exec_in_container(
        [
            "mas-cli",
            "manage",
            "issue-compatibility-token",
            "onbot",
            "--yes-i-want-to-grant-synapse-admin-privileges",
            "--config",
            "/config/config.yaml",
        ],
        service_name="mas",
    )
    match = re.search(r"mct_[A-Za-z0-9._~+/=-]+", out + err)
    assert match, out + err
    return match.group(0)


def _compose(compose: DockerCompose, *args: str) -> None:
    compose._run_command(cmd=[*compose.compose_command_property, *args])


async def _wait_synapse_healthy(timeout: float = 120) -> None:
    async def healthy() -> bool:
        async with httpx.AsyncClient(timeout=5) as client:
            return (await client.get(f"{S.SYNAPSE_URL}/health")).status_code == 200

    await _eventually(healthy, timeout=timeout, what="Synapse /health")


async def test_cold_start_with_synapse_down_heals_once_it_is_up(
    make_config,
    compose_stack: DockerCompose,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    tmp_path: Path,
) -> None:
    token = _fresh_bot_token(compose_stack)
    device_id = httpx.get(
        f"{S.SYNAPSE_URL}/_matrix/client/v3/account/whoami",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ).json()["device_id"]

    config = make_config()
    config.synapse_server.bot_access_token = token
    config.server_tick_rate_sec = 10
    config.authentik_poll_rate_sec = 2
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    heartbeat = tmp_path / "hb.json"
    log_file = tmp_path / "onbot.log"
    config_file = _write_config(config, tmp_path / "config.yml")

    _compose(compose_stack, "stop", "synapse")
    try:
        with _onbot_run(config_file, heartbeat, log_file) as proc:
            # Synapse is down: every startup step fails and keeps retrying, the process keeps running.
            await asyncio.sleep(8)
            assert proc.poll() is None, log_file.read_text()
            assert "device registration" in _heartbeat(heartbeat).get("startup_pending", [])

            _compose(compose_stack, "start", "synapse")
            await _wait_synapse_healthy()

            async def started() -> bool:
                assert proc.poll() is None, log_file.read_text()
                return _heartbeat(heartbeat).get("startup_pending") == []

            await _eventually(started, timeout=120, what="startup completion after Synapse came up")

            # Before the fix: device 404, admin room 404, and every welcome send a 500 (08 check 10).
            devices = await admin_client.get_json(f"v2/users/{S.BOT_USER_ID}/devices")
            assert device_id in {d["device_id"] for d in devices.get("devices", [])}
            assert await matrix_client.resolve_room_alias(f"#{config.admin_room.alias}:{S.SERVER_NAME}")

            _u, newcomer = S.provision(authentik_admin, S.uniq("coldstart"))

            async def welcomed() -> bool:
                assert proc.poll() is None, log_file.read_text()
                direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
                rooms = direct.get(newcomer.mxid) or []
                return bool(rooms) and len(await _bot_messages(matrix_client, rooms[0])) == len(
                    config.welcome_new_users_messages or []
                )

            await _eventually(welcomed, timeout=90, what="the newcomer's welcome messages")

            code, elapsed = _sigterm(proc)
            assert code == 0, log_file.read_text()
            assert elapsed < STOP_BUDGET_SEC
    finally:
        _compose(compose_stack, "start", "synapse")
        await _wait_synapse_healthy()
