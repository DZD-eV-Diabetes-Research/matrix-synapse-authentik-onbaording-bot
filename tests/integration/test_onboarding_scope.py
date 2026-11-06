"""Who gets a notice board, and how many, against the live stack (review session S8).

* live check 12 (RUN-01, ONB-01, RUN-08): 20 users are welcomed at once while the reconcile loop and
  the sync pump run side by side, as in the service. Before the fix the concurrent read-modify-write
  of ``m.direct`` lost 9 of 20 entries, and the next start gave those users a second notice board and
  the welcome messages again.
* live check 6 (SEC-06, ONB-13): a local account that is not in Authentik joins a group room the bot
  is in. Before the fix the sync stream welcomed it, and every later broadcast reached it.

Both tests restrict the sync scope to their own group, so the users other tests left on the shared
stack are not part of the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from testcontainers.compose import DockerCompose

from onbot.app import build_app, run_reconcile_once
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.reconciler.rooms import compute_room_attributes
from onbot.reconciler.state import OnbotRoomType, event_type_name
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration

MESSAGES = ["Welcome aboard, {user_id}.", "Read the guide.", "Save your security key."]
DIRECT_TYPE = event_type_name(S.SERVER_NAME, OnbotRoomType.direct_room)


def _scoped_config(make_config: Callable[[], OnbotConfig], prefix: str, group_pk: str) -> OnbotConfig:
    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    config.sync_authentik_users_with_matrix_rooms.sync_only_users_of_groups_with_id = [group_pk]
    config.welcome_new_users_messages = list(MESSAGES)
    return config


async def _bot_message_count(client: ApiClientMatrix, room_id: str) -> int:
    resp = await client.get_json(f"v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 100})
    return sum(
        1
        for ev in resp.get("chunk", [])
        if ev.get("type") == "m.room.message" and ev.get("sender") == S.BOT_USER_ID
    )


async def _notice_boards_by_user(client: ApiClientMatrix, users: set[str]) -> dict[str, list[str]]:
    """Every room the bot is in whose onbot marker names one of ``users``: the rooms that exist,
    whether or not ``m.direct`` still points at them."""
    boards: dict[str, list[str]] = {u: [] for u in users}
    for room_id in await client.joined_rooms():
        marker = await client.get_room_state_event(room_id, DIRECT_TYPE)
        if marker and marker.get("user_id") in users:
            boards[marker["user_id"]].append(room_id)
    return boards


async def _eventually(probe: Callable[[], Awaitable[Any]], *, timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await probe()
        if last:
            return last
        await asyncio.sleep(1)
    raise AssertionError(f"{what} did not happen within {timeout:.0f}s (last: {last!r})")


@contextlib.asynccontextmanager
async def _service(config: OnbotConfig) -> Any:
    """The service's loops in-process: reconcile, sync pump and onboarding workers, side by side."""
    async with build_app(config) as app:
        await app.negotiate_versions()
        await app.register_device()
        await app.open_onboarding()
        tasks = [
            asyncio.create_task(app.engine.run()),
            asyncio.create_task(app.pump.run()),
            asyncio.create_task(app.listener.run()),
        ]
        try:
            yield app
        finally:
            app.engine.request_stop()
            app.pump.request_stop()
            app.listener.request_stop()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)


async def test_twenty_concurrent_welcomes_give_everybody_exactly_one_notice_board(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """Live check 12: no lost m.direct entry, one notice board each, every message once."""
    prefix = S.uniq("crowd")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    logins = [S.provision(authentik_admin, S.uniq("crowd"), groups=[group["pk"]])[1] for _ in range(20)]
    users = {login.mxid for login in logins}

    # First pass without welcome messages: the users are joined into the group room, no notice board
    # exists yet. The pump's first sync then replays all 20 joins while the pass reports the users.
    config = _scoped_config(make_config, prefix, str(group["pk"]))
    config.welcome_new_users_messages = []
    await run_reconcile_once(config)
    room_id = await matrix_client.resolve_room_alias(
        compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    )
    assert room_id is not None
    assert users <= set(await admin_client.list_room_members(room_id))

    config = _scoped_config(make_config, prefix, str(group["pk"]))
    async with _service(config):

        async def all_welcomed() -> bool:
            direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
            if not all(direct.get(u) for u in users):
                return False
            counts = [await _bot_message_count(matrix_client, direct[u][0]) for u in users]
            return all(c == len(MESSAGES) for c in counts)

        await _eventually(all_welcomed, timeout=180, what="20 complete welcomes")

    async def check() -> None:
        direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
        boards = await _notice_boards_by_user(matrix_client, users)
        for mxid in users:
            assert len(direct.get(mxid) or []) == 1, (mxid, direct.get(mxid))
            assert boards[mxid] == direct[mxid], (mxid, boards[mxid], direct[mxid])
            assert await _bot_message_count(matrix_client, boards[mxid][0]) == len(MESSAGES)

    await check()
    # A restart: the in-memory caches are gone. Before the fix this is where lost users got a
    # second notice board and every message again.
    await run_reconcile_once(config)
    await check()


def _register_outsider(compose: DockerCompose) -> tuple[str, str]:
    """A local account MAS knows but Authentik does not (a MAS-only user), with an access token."""
    username = S.uniq("outsider")
    common = ["--config", "/config/config.yaml"]
    compose.exec_in_container(
        [
            "mas-cli",
            "manage",
            "register-user",
            username,
            "--email",
            f"{username}@onbot.test",
            "--yes",
            "--ignore-password-complexity",
            *common,
        ],
        service_name="mas",
    )
    out, err, _ = compose.exec_in_container(
        ["mas-cli", "manage", "issue-compatibility-token", username, *common], service_name="mas"
    )
    match = re.search(r"mct_[A-Za-z0-9._~+/=-]+", out + err)
    assert match, out + err
    token = match.group(0)
    assert S.whoami_status(token) == 200  # provisions the Synapse account
    return f"@{username}:{S.SERVER_NAME}", token


async def test_an_outsider_joining_a_group_room_gets_no_notice_board_and_no_broadcast(
    make_config,
    compose_stack: DockerCompose,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """Live check 6 (SEC-06): only users the reconciler maps are welcomed and broadcast to."""
    prefix = S.uniq("scope")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    _m, member = S.provision(authentik_admin, S.uniq("scopemember"), groups=[group["pk"]])
    outsider, _token = _register_outsider(compose_stack)

    config = _scoped_config(make_config, prefix, str(group["pk"]))
    await run_reconcile_once(config)
    room_id = await matrix_client.resolve_room_alias(
        compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    )
    assert room_id is not None
    # The outsider joins the group room (a member could have invited them; the admin API is quicker).
    await admin_client.add_user_to_room(room_id, outsider)

    async with build_app(config) as app:
        await app.register_device()
        await app.open_onboarding()
        await app.engine.reconcile_once()
        assert app.listener.mapped is not None and outsider not in app.listener.mapped
        # One real sync slice, handed to the listener exactly as the pump does: it holds the
        # outsider's join (required state and timeline of the group room).
        await app.listener.handle_sync(await app.matrix.sliding_sync(None, timeout_ms=0))
        await app.listener.process_queued()

        direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
        assert outsider not in direct
        assert direct.get(member.mxid)
        assert (await _notice_boards_by_user(matrix_client, {outsider}))[outsider] == []

        # An older version did give such an account a notice board: it stays out of every broadcast.
        legacy = await matrix_client.create_room(name="legacy board", invite=[outsider])
        await matrix_client.set_account_data(S.BOT_USER_ID, "m.direct", {**direct, outsider: [legacy]})
        try:
            assert legacy not in await app.broadcast.target_rooms()
            result = await app.broadcast.broadcast("Maintenance tonight")
            assert result.sent == direct[member.mxid]
            assert await _bot_message_count(matrix_client, legacy) == 0
        finally:
            current = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
            current.pop(outsider, None)
            await matrix_client.set_account_data(S.BOT_USER_ID, "m.direct", current)

    # The next pass removes the outsider from the group room, and still gives them nothing.
    await run_reconcile_once(config)
    assert outsider not in await admin_client.list_room_members(room_id)
    assert outsider not in await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
