"""Room teardown, deletion and repair against the live stack (session S7: REC-09, REC-10, REC-11, D16).

Before S7 the room path of the destructive-action guard was covered by engine unit tests only (S5
handover). Here the real thing runs: a room whose group disappeared is blocked and cleared, a deletion
below the layer 3 limit runs in the pass that closes the room, three deletions wait for ``!confirm``.
Then the repairs: a block a server admin set stays, the space link, the alias and the marker lock come
back, a lobby switched off is closed and detached, and a lobby a human closed stays closed until an
admin reopens it.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from testcontainers.compose import DockerCompose

from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastService
from onbot.admin.control_room import ControlRoomHandler
from onbot.app import App, build_app, run_reconcile_once
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.events import EventBus
from onbot.guard import DestructiveActionGuard, GuardLimits, MatrixAccountDataGuardStore
from onbot.reconciler.rooms import compute_lobby_attributes, compute_room_attributes
from onbot.reconciler.state import OnbotRoomType, event_type_name
from onbot.rooms.provision import markers_locked
from onbot.runtime import StartupSupervisor
from tests.integration import stack_api as S
from tests.integration.test_guard import _bot_messages, _confirm, _proposed_batches

pytestmark = pytest.mark.integration

GROUP_MARKER = event_type_name(S.SERVER_NAME, OnbotRoomType.group_room)
JOIN_RULES = "m.room.join_rules"


def _cs(method: str, token: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.request(
        method,
        f"{S.SYNAPSE_URL}/_matrix/client/v3/{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=30.0,
    )


def _config(make_config: Callable[[], OnbotConfig], prefix: str, *, teardown: bool = False) -> OnbotConfig:
    config = make_config()
    rooms = config.sync_matrix_rooms_based_on_authentik_groups
    rooms.only_for_groupnames_starting_with = prefix
    rooms.disable_rooms_when_mapped_authentik_group_disappears = teardown
    # One test group disappearing out of two is a 50 % drop; layer 1 has its own tests (test_guard).
    config.destructive_action_guard.max_drop_percent = 100
    return config


def _with_space(config: OnbotConfig) -> OnbotConfig:
    space = config.create_matrix_rooms_in_a_matrix_space
    space.enabled = True
    space.alias = S.uniq("repairspace")
    space.create_matrix_space_if_not_exists.enabled = True
    return config


async def _room_of(matrix: ApiClientMatrix, config: OnbotConfig, group: dict[str, Any]) -> str:
    room_id = await matrix.resolve_room_alias(
        compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    )
    assert room_id is not None
    return room_id


async def _lobby_of(matrix: ApiClientMatrix, config: OnbotConfig, group: dict[str, Any]) -> str:
    attrs = compute_room_attributes(group, config, S.SERVER_NAME)
    lobby = compute_lobby_attributes(attrs, config.matrix_room_default_settings, S.SERVER_NAME)
    room_id = await matrix.resolve_room_alias(lobby.canonical_alias)
    assert room_id is not None
    return room_id


async def _space_of(matrix: ApiClientMatrix, config: OnbotConfig) -> str:
    space_id = await matrix.resolve_room_alias(
        f"#{config.create_matrix_rooms_in_a_matrix_space.alias}:{S.SERVER_NAME}"
    )
    assert space_id is not None
    return space_id


async def _gone(admin: ApiClientSynapseAdmin, room_id: str) -> bool:
    try:
        await admin.get_room_details(room_id)
    except ApiError as exc:
        return exc.status_code == 404
    return False


@asynccontextmanager
async def _service(config: OnbotConfig) -> AsyncIterator[App]:
    """One bot process across several passes (state kept in memory), like the running service."""
    async with build_app(config) as app:
        await StartupSupervisor(app.reconcile_once_steps()).run_once()
        yield app


# --- teardown and deletion through the guard (S5 handover) ------------------------------------------


async def _sweep(config: OnbotConfig) -> None:
    """Delete the bot's group rooms of every other test's groups from the shared stack.

    Teardown judges every room the bot created on the server: under this test's name prefix, the
    rooms of all other tests' groups are obsolete too, and their number would trip the guard. One pass
    with this test's groups live, the guard's limits off and deletion on removes them first.
    """
    sweep = config.model_copy(deep=True)
    rooms = sweep.sync_matrix_rooms_based_on_authentik_groups
    rooms.disable_rooms_when_mapped_authentik_group_disappears = True
    rooms.delete_disabled_rooms = True
    guard = sweep.destructive_action_guard
    guard.max_reversible_actions_per_pass = 1_000_000
    guard.confirm_irreversible_actions_from = 1_000_000
    sweep.admin_room.enabled = False
    await run_reconcile_once(sweep)


async def test_teardown_blocks_and_kicks_and_a_returning_group_is_unblocked(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("tdn")
    authentik_admin.create_group(f"{prefix}-anchor", attributes={"is_chatroom": True})
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    _u, member = S.provision(authentik_admin, S.uniq("tdnmember"), groups=[group["pk"]])
    config = _config(make_config, prefix, teardown=True)
    await _sweep(config)
    await run_reconcile_once(config)
    room_id = await _room_of(matrix_client, config, group)
    assert member.mxid in await admin_client.list_room_members(room_id)

    authentik_admin.set_group_attributes(group["pk"], {})  # no longer opted into chat
    await run_reconcile_once(config)

    block = await admin_client.get_room_block(room_id)
    assert block.blocked and block.by == S.BOT_USER_ID  # Synapse records the bot as the blocker
    assert await admin_client.list_room_members(room_id) == [S.BOT_USER_ID]

    authentik_admin.set_group_attributes(group["pk"], {"is_chatroom": True})
    await run_reconcile_once(config)

    assert not (await admin_client.get_room_block(room_id)).blocked
    assert member.mxid in await admin_client.list_room_members(room_id)


async def test_a_deletion_below_the_limit_runs_in_the_pass_that_closes_the_room(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("del")
    authentik_admin.create_group(f"{prefix}-anchor", attributes={"is_chatroom": True})
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    S.provision(authentik_admin, S.uniq("delmember"), groups=[group["pk"]])
    config = _config(make_config, prefix, teardown=True)
    config.sync_matrix_rooms_based_on_authentik_groups.delete_disabled_rooms = True
    await _sweep(config)
    await run_reconcile_once(config)
    room_id = await _room_of(matrix_client, config, group)

    authentik_admin.delete_group(group["pk"])
    await run_reconcile_once(config)

    assert await _gone(admin_client, room_id)


async def test_three_deletions_wait_for_a_confirmation_while_the_rooms_are_closed(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("del3")
    authentik_admin.create_group(f"{prefix}-anchor", attributes={"is_chatroom": True})
    groups = [
        authentik_admin.create_group(f"{prefix}-room{n}", attributes={"is_chatroom": True}) for n in range(3)
    ]
    _m, member = S.provision(authentik_admin, S.uniq("del3member"), groups=[g["pk"] for g in groups])
    _a, admin = S.provision(authentik_admin, S.uniq("del3admin"))
    config = _config(make_config, prefix, teardown=True)
    config.sync_matrix_rooms_based_on_authentik_groups.delete_disabled_rooms = True
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]
    await _sweep(config)
    await run_reconcile_once(config)
    rooms = [await _room_of(matrix_client, config, g) for g in groups]

    for group in groups:
        authentik_admin.delete_group(group["pk"])
    await run_reconcile_once(config)

    # Closed (reversible, below the layer 2 limit), not deleted (three irreversible actions).
    for room_id in rooms:
        assert not await _gone(admin_client, room_id), "deleted without a confirmation"
        assert (await admin_client.get_room_block(room_id)).blocked
        assert member.mxid not in await admin_client.list_room_members(room_id)
    control_room = await matrix_client.resolve_room_alias(f"#{config.admin_room.alias}:{S.SERVER_NAME}")
    assert control_room is not None
    reports = await _bot_messages(matrix_client, control_room)
    [batch_id] = _proposed_batches(reports)
    assert "delete 3 room(s) (irreversible)" in next(r for r in reports if f"!confirm {batch_id}" in r)

    await admin_client.add_user_to_room(control_room, admin.mxid)
    handler = ControlRoomHandler(
        matrix_client,
        config,
        BroadcastService(matrix_client, config, recipients=S.recipients()),
        AdminResolver(authentik_client, config, ttl_sec=0),
        guard=DestructiveActionGuard(
            GuardLimits.from_config(config),
            MatrixAccountDataGuardStore(matrix_client, S.BOT_USER_ID, S.SERVER_NAME),
            EventBus(),
        ),
    )
    await handler.start(control_room)
    await _confirm(handler, matrix_client, admin.access_token, control_room, batch_id)
    await run_reconcile_once(config)

    for room_id in rooms:
        assert await _gone(admin_client, room_id)


# --- REC-11: a block a server admin set --------------------------------------------------------------


def _server_admin_token(compose_stack: DockerCompose) -> str:
    """A second account with Synapse admin rights, as a human server admin would have."""
    common = ["--config", "/config/config.yaml"]
    name = S.uniq("srvadmin")
    compose_stack.exec_in_container(
        [
            "mas-cli",
            "manage",
            "register-user",
            name,
            "--yes",
            "--admin",
            "--ignore-password-complexity",
            *common,
        ],
        service_name="mas",
    )
    out, err, _ = compose_stack.exec_in_container(
        [
            "mas-cli",
            "manage",
            "issue-compatibility-token",
            name,
            "--yes-i-want-to-grant-synapse-admin-privileges",
            *common,
        ],
        service_name="mas",
    )
    match = re.search(r"mct_[A-Za-z0-9._~+/=-]+", out + err)
    assert match, out + err
    token = match.group(0)
    _cs("GET", token, "account/whoami").raise_for_status()  # provisions the Synapse account
    return token


async def test_a_room_a_server_admin_blocked_is_never_unblocked(
    make_config,
    compose_stack: DockerCompose,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("blk")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    config = _config(make_config, prefix)
    await run_reconcile_once(config)
    room_id = await _room_of(matrix_client, config, group)

    token = _server_admin_token(compose_stack)
    blocked = httpx.put(
        f"{S.SYNAPSE_URL}/_synapse/admin/v1/rooms/{quote(room_id, safe='')}/block",
        headers={"Authorization": f"Bearer {token}"},
        json={"block": True},
        timeout=30.0,
    )
    assert blocked.status_code == 200, blocked.text
    blocker = (await admin_client.get_room_block(room_id)).by
    assert blocker is not None and blocker != S.BOT_USER_ID

    _n, newcomer = S.provision(authentik_admin, S.uniq("blknew"), groups=[group["pk"]])
    await run_reconcile_once(config)

    block = await admin_client.get_room_block(room_id)
    assert block.blocked and block.by == blocker  # before S7 the bot lifted it within one pass
    assert newcomer.mxid not in await admin_client.list_room_members(room_id)


def _set_block(token: str, room_id: str, *, blocked: bool) -> None:
    r = httpx.put(
        f"{S.SYNAPSE_URL}/_synapse/admin/v1/rooms/{quote(room_id, safe='')}/block",
        headers={"Authorization": f"Bearer {token}"},
        json={"block": blocked},
        timeout=30.0,
    )
    assert r.status_code == 200, r.text


async def test_an_obsolete_room_a_server_admin_blocked_is_held_until_the_block_is_lifted(
    make_config,
    compose_stack: DockerCompose,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """W4R-02 (D23): the room of a group that disappeared was cleared and, with
    `delete_disabled_rooms`, purged, although a server admin had blocked it (a legal hold)."""
    prefix = S.uniq("hold")
    authentik_admin.create_group(f"{prefix}-anchor", attributes={"is_chatroom": True})
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    _u, member = S.provision(authentik_admin, S.uniq("holdmember"), groups=[group["pk"]])
    config = _config(make_config, prefix, teardown=True)
    config.sync_matrix_rooms_based_on_authentik_groups.delete_disabled_rooms = True
    await _sweep(config)
    await run_reconcile_once(config)
    room_id = await _room_of(matrix_client, config, group)
    token = _server_admin_token(compose_stack)
    _set_block(token, room_id, blocked=True)
    blocker = (await admin_client.get_room_block(room_id)).by

    authentik_admin.delete_group(group["pk"])
    await run_reconcile_once(config)
    await run_reconcile_once(config)

    assert not await _gone(admin_client, room_id)
    assert member.mxid in await admin_client.list_room_members(room_id)
    block = await admin_client.get_room_block(room_id)
    assert block.blocked and block.by == blocker

    _set_block(token, room_id, blocked=False)  # the hold ends
    await run_reconcile_once(config)

    assert await _gone(admin_client, room_id)


# --- REC-09: space link, alias, marker -------------------------------------------------------------


async def test_a_lost_space_link_is_restored(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
) -> None:
    prefix = S.uniq("lnk")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    config = _with_space(_config(make_config, prefix))
    await run_reconcile_once(config)
    space_id = await _space_of(matrix_client, config)
    room_id = await _room_of(matrix_client, config, group)
    # What a creation cut off after createRoom, or a room from before the space, looks like.
    await matrix_client.put_room_state_event(space_id, "m.space.child", {}, state_key=room_id)
    await matrix_client.put_room_state_event(room_id, "m.space.parent", {}, state_key=space_id)

    await run_reconcile_once(config)

    child = await matrix_client.get_room_state_event(space_id, "m.space.child", room_id) or {}
    assert child.get("via") == [S.SERVER_NAME] and child.get("suggested") is False
    parent = await matrix_client.get_room_state_event(room_id, "m.space.parent", space_id) or {}
    assert parent.get("via") == [S.SERVER_NAME]
    # The marker check reads the whole state event (format=event) and finds the bot as its sender.
    marker = await matrix_client.get_room_state_event_with_sender(room_id, GROUP_MARKER)
    assert marker is not None and marker["sender"] == S.BOT_USER_ID


async def test_an_alias_a_moderator_removed_is_recreated(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
) -> None:
    """S6 leftover: the room was still found by its marker, but its address stayed gone."""
    prefix = S.uniq("als")
    group = authentik_admin.create_group(
        f"{prefix}-room", attributes={"is_chatroom": True, "chat-systemwide-powerlevel": 50}
    )
    _u, moderator = S.provision(authentik_admin, S.uniq("alsmod"), groups=[group["pk"]])
    config = _config(make_config, prefix)
    await run_reconcile_once(config)
    alias = compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    room_id = await _room_of(matrix_client, config, group)
    deleted = _cs("DELETE", moderator.access_token, f"directory/room/{quote(alias, safe='')}")
    assert deleted.status_code == 200, deleted.text
    assert await matrix_client.resolve_room_alias(alias) is None

    await run_reconcile_once(config)

    assert await matrix_client.resolve_room_alias(alias) == room_id
    canonical = await matrix_client.get_room_state_event(room_id, "m.room.canonical_alias") or {}
    assert canonical.get("alias") == alias


async def test_a_lowered_marker_lock_is_raised_again_by_the_running_bot(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
) -> None:
    prefix = S.uniq("lck")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    config = _config(make_config, prefix)
    async with _service(config) as app:
        await app.engine.reconcile_once()
        room_id = await _room_of(matrix_client, config, group)
        power = await matrix_client.get_room_power_levels(room_id)
        # Only a creator can do this in a version 12 room: an extra creator from chatroom_params (D6).
        power["events"] = {**power["events"], GROUP_MARKER: 50}
        await matrix_client.set_room_power_levels(room_id, power)

        await app.engine.reconcile_once()

    power = await matrix_client.get_room_power_levels(room_id)
    create = await matrix_client.get_room_state_event(room_id, "m.room.create") or {}
    assert markers_locked(power, S.SERVER_NAME, S.BOT_USER_ID, room_version=create.get("room_version"))


# --- REC-10 and D16: lobbies -----------------------------------------------------------------------


def _lobby_group(authentik_admin: S.AuthentikAdmin, prefix: str, *, lobby: bool) -> dict[str, Any]:
    return authentik_admin.create_group(
        f"{prefix}-room", attributes={"is_chatroom": True, "chatroom_visitor_lobby": lobby}
    )


async def test_a_switched_off_lobby_is_closed_and_detached_and_reopens_when_switched_on(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("lbo")
    group = _lobby_group(authentik_admin, prefix, lobby=True)
    _v, visitor = S.provision(authentik_admin, S.uniq("lbovisitor"))
    config = _with_space(_config(make_config, prefix))
    await run_reconcile_once(config)
    space_id = await _space_of(matrix_client, config)
    lobby_id = await _lobby_of(matrix_client, config, group)
    assert S.join_room_status(visitor.access_token, lobby_id) == 200

    authentik_admin.set_group_attributes(group["pk"], {"is_chatroom": True, "chatroom_visitor_lobby": False})
    await run_reconcile_once(config)

    assert await matrix_client.get_room_state_event(lobby_id, JOIN_RULES) == {"join_rule": "invite"}
    child = await matrix_client.get_room_state_event(space_id, "m.space.child", lobby_id) or {}
    assert not child.get("via")
    assert lobby_id not in S.space_hierarchy_room_ids(visitor.access_token, space_id)
    assert visitor.mxid in await admin_client.list_room_members(lobby_id)  # nobody is kicked

    authentik_admin.set_group_attributes(group["pk"], {"is_chatroom": True, "chatroom_visitor_lobby": True})
    await run_reconcile_once(config)

    rule = await matrix_client.get_room_state_event(lobby_id, JOIN_RULES) or {}
    assert rule.get("join_rule") == "restricted"
    child = await matrix_client.get_room_state_event(space_id, "m.space.child", lobby_id) or {}
    assert child.get("suggested") is True


async def test_a_lobby_a_human_closed_stays_closed_until_an_admin_reopens_it(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
) -> None:
    prefix = S.uniq("lbm")
    group = _lobby_group(authentik_admin, prefix, lobby=True)
    keeper_name = S.uniq("lbmkeeper")
    _k, keeper = S.provision(authentik_admin, keeper_name)
    config = _with_space(_config(make_config, prefix))
    config.global_room_admins = [keeper_name]  # power level 100 in the lobby, like its room admins
    async with _service(config) as app:
        await app.engine.reconcile_once()
        lobby_id = await _lobby_of(matrix_client, config, group)
        closed = _cs(
            "PUT",
            keeper.access_token,
            f"rooms/{quote(lobby_id, safe='')}/state/{JOIN_RULES}/",
            {"join_rule": "invite"},
        )
        assert closed.status_code == 200, closed.text

        await app.engine.reconcile_once()
        await app.engine.reconcile_once()
        assert await matrix_client.get_room_state_event(lobby_id, JOIN_RULES) == {"join_rule": "invite"}

        reply = await app.engine.reopen_lobby(str(group["pk"]))
        assert reply.startswith("Reopened"), reply
        await app.engine.reconcile_once()

    rule = await matrix_client.get_room_state_event_with_sender(lobby_id, JOIN_RULES)
    assert rule is not None
    assert rule["content"].get("join_rule") == "restricted" and rule["sender"] == S.BOT_USER_ID


# --- S18 (W4R-06): `!room adopt` --------------------------------------------------------------------


async def test_room_adopt_hands_a_room_whose_marker_was_removed_back_to_the_bot(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """The marker goes while the bot is stopped; a fresh process skips the group (D20) until an
    allowlisted admin runs ``!room adopt`` in the control room. Then the next pass manages the room."""
    prefix = S.uniq("adopt")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    _a, admin = S.provision(authentik_admin, S.uniq("adoptadmin"))
    config = _config(make_config, prefix)
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]
    await run_reconcile_once(config)
    room_id = await _room_of(matrix_client, config, group)
    await matrix_client.put_room_state_event(room_id, GROUP_MARKER, {})  # removed while stopped

    _n, newcomer = S.provision(authentik_admin, S.uniq("adoptnew"), groups=[group["pk"]])
    async with _service(config) as app:  # a fresh process: it no longer knows the room
        await app.engine.reconcile_once()
        assert newcomer.mxid not in await admin_client.list_room_members(room_id)
        assert app.control_room is not None and app.control_room.room_id is not None
        control_room = app.control_room.room_id
        await admin_client.add_user_to_room(control_room, admin.mxid)

        command = f"!room adopt {group['pk']} {room_id}"
        assert S.send_message_status(admin.access_token, control_room, command) == 200
        await app.control_room.handle_sync(await matrix_client.sliding_sync(None))
        await app.engine.reconcile_once()

    replies = await _bot_messages(matrix_client, control_room)
    assert any(r.startswith(f"Adopted {room_id} as the room of group") for r in replies), replies
    marker = await matrix_client.get_room_state_event_with_sender(room_id, GROUP_MARKER)
    assert marker is not None and marker["sender"] == S.BOT_USER_ID
    assert marker["content"]["group_id"] == str(group["pk"])
    assert newcomer.mxid in await admin_client.list_room_members(room_id)


async def test_room_adopt_refuses_a_room_the_bot_did_not_create(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
) -> None:
    prefix = S.uniq("adoptno")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    _u, human = S.provision(authentik_admin, S.uniq("adoptnohuman"))
    created = _cs("POST", human.access_token, "createRoom", {"name": "not the bot's"})
    assert created.status_code == 200, created.text
    foreign_room = created.json()["room_id"]
    config = _config(make_config, prefix)
    async with _service(config) as app:  # no pass yet: the group has no room of its own
        reply = await app.engine.adopt_room(str(group["pk"]), foreign_room, by="@someadmin:" + S.SERVER_NAME)
    assert "was not created by the bot" in reply, reply
