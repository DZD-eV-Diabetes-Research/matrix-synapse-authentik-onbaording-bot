"""Room identity against the live stack (D20, REC-04, SEC-02, REC-13, ONB-17; 08 check 4).

The live scenarios of ``08-live-verification.md`` check 4 ran here before S6 and showed one squatted
alias stopping the whole server's room sync, the admin join API force-joining a group into an
attacker's public room, and a moderator clearing an alias making the bot fail or build a second room.
Each scenario now runs as a test, plus the one-time adoption of rooms from older versions, the marker
lock, and a room upgraded by a human.

The groups use attribute-derived aliases (``attributes.chatroom_alias``, a documented example), so the
alias of a future room is known in advance, as an attacker would know it.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from onbot.admin.admins import AdminResolver
from onbot.app import run_reconcile_once
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.reconciler.state import GroupRoomState, OnbotRoomType, dump_room_state, event_type_name
from onbot.rooms.admin import AdminRoomProvisioner
from onbot.rooms.identity import RoomIdentityResolver
from onbot.rooms.provision import markers_locked
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration

GROUP_MARKER = event_type_name(S.SERVER_NAME, OnbotRoomType.group_room)


# --- what an ordinary user can do over the client-server API ---------------------------------------


def _cs(method: str, token: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.request(
        method,
        f"{S.SYNAPSE_URL}/_matrix/client/v3/{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=30.0,
    )


def _user_creates_room(token: str, alias_localpart: str, **body: Any) -> str:
    r = _cs("POST", token, "createRoom", {"room_alias_name": alias_localpart, **body})
    r.raise_for_status()
    room_id: str = r.json()["room_id"]
    return room_id


def _put_state(token: str, room_id: str, event_type: str, content: dict[str, Any]) -> int:
    return _cs("PUT", token, f"rooms/{quote(room_id, safe='')}/state/{event_type}/", content).status_code


def _alias(localpart: str) -> str:
    return f"#{localpart}:{S.SERVER_NAME}"


def _config(make_config: Any, prefix: str) -> OnbotConfig:
    config: OnbotConfig = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    config.matrix_room_default_settings.matrix_alias_from_authentik_attribute = "attributes.chatroom_alias"
    return config


def _group(authentik_admin: S.AuthentikAdmin, prefix: str, alias: str, **attributes: Any) -> dict[str, Any]:
    return authentik_admin.create_group(
        f"{prefix}-{secrets.token_hex(3)}",
        attributes={"is_chatroom": True, "chatroom_alias": alias, **attributes},
    )


async def _bot_rooms_named(admin: ApiClientSynapseAdmin, name: str) -> list[str]:
    return [
        r["room_id"]
        for r in await admin.list_rooms()
        if r.get("name") == name and r.get("creator") == S.BOT_USER_ID
    ]


# --- 08 check 4: squatting ----------------------------------------------------------------------


async def test_a_private_squat_room_skips_only_its_group(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """Check 4 A: before S6 every pass aborted with a 403, and no other group got a room."""
    prefix = S.uniq("sqa")
    _a, attacker = S.provision(authentik_admin, S.uniq("attacker"))
    squat_alias = S.uniq("squat")
    squat = _user_creates_room(attacker.access_token, squat_alias, preset="private_chat")

    squatted = _group(authentik_admin, prefix, squat_alias)
    other_alias = S.uniq("fair")
    other = _group(authentik_admin, prefix, other_alias)
    _m, member = S.provision(authentik_admin, S.uniq("sqmember"), groups=[squatted["pk"], other["pk"]])

    config = _config(make_config, prefix)
    await run_reconcile_once(config)
    await run_reconcile_once(config)

    # The squat room is left as it is: nobody joined into it, and the alias is not taken over.
    assert await matrix_client.resolve_room_alias(_alias(squat_alias)) == squat
    assert await admin_client.list_room_members(squat) == [attacker.mxid]
    # The rest of the server converged: the unrelated group has its room, with its member.
    other_room = await matrix_client.resolve_room_alias(_alias(other_alias))
    assert other_room is not None
    assert member.mxid in await admin_client.list_room_members(other_room)


async def test_a_public_squat_room_gets_nobody_force_joined(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """Check 4 B: before S6 the admin join API put the group into a room the attacker controls."""
    prefix = S.uniq("sqb")
    _a, attacker = S.provision(authentik_admin, S.uniq("attacker"))
    squat_alias = S.uniq("pubsquat")
    squat = _user_creates_room(
        attacker.access_token,
        squat_alias,
        preset="public_chat",
        initial_state=[
            {
                "type": "m.room.history_visibility",
                "state_key": "",
                "content": {"history_visibility": "world_readable"},
            }
        ],
    )
    group = _group(authentik_admin, prefix, squat_alias)
    _m, member = S.provision(authentik_admin, S.uniq("victim"), groups=[group["pk"]])

    await run_reconcile_once(_config(make_config, prefix))

    assert await admin_client.list_room_members(squat) == [attacker.mxid]
    assert member.mxid not in await admin_client.list_room_members(squat)


async def _moderated_room(
    make_config: Any, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix, tag: str
) -> tuple[OnbotConfig, dict[str, Any], str, str, S.LoginResult]:
    """A group room with a moderator (PL 50, the documented Moderators pattern)."""
    prefix = S.uniq(tag)
    alias = S.uniq(f"{tag}room")
    group = _group(authentik_admin, prefix, alias, **{"chat-systemwide-powerlevel": 50})
    _u, moderator = S.provision(authentik_admin, S.uniq("mod"), groups=[group["pk"]])
    config = _config(make_config, prefix)
    await run_reconcile_once(config)
    room_id = await matrix_client.resolve_room_alias(_alias(alias))
    assert room_id is not None
    power = await matrix_client.get_room_power_levels(room_id)
    assert power["users"][moderator.mxid] == 50
    return config, group, alias, room_id, moderator


async def test_a_moderator_clearing_the_main_address_changes_nothing(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """Check 4 C: before S6 every following pass failed createRoom with M_ROOM_IN_USE."""
    config, group, _alias_lp, room_id, moderator = await _moderated_room(
        make_config, authentik_admin, matrix_client, "clr"
    )
    assert _put_state(moderator.access_token, room_id, "m.room.canonical_alias", {}) == 200

    newcomer, login = S.provision(authentik_admin, S.uniq("newcomer"))
    authentik_admin.add_user_to_group(group["pk"], newcomer["pk"])
    await run_reconcile_once(config)

    assert login.mxid in await admin_client.list_room_members(room_id)
    assert await _bot_rooms_named(admin_client, group["name"]) == [room_id]


async def test_a_moderator_deleting_the_alias_gets_no_second_room(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """Check 4 C2: before S6 the bot built a second, empty room and stranded the first."""
    config, group, alias, room_id, moderator = await _moderated_room(
        make_config, authentik_admin, matrix_client, "del"
    )
    deleted = _cs("DELETE", moderator.access_token, f"directory/room/{quote(_alias(alias), safe='')}")
    assert deleted.status_code == 200

    newcomer, login = S.provision(authentik_admin, S.uniq("newcomer"))
    authentik_admin.add_user_to_group(group["pk"], newcomer["pk"])
    await run_reconcile_once(config)

    assert login.mxid in await admin_client.list_room_members(room_id)
    assert await _bot_rooms_named(admin_client, group["name"]) == [room_id]


async def test_a_moderator_cannot_rewrite_the_marker(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """The marker is locked at creation, so a moderator cannot move the room to another group."""
    *_, room_id, moderator = await _moderated_room(make_config, authentik_admin, matrix_client, "mrk")
    # The marker came with createRoom's initial_state: Synapse records the creator as its sender,
    # which is what the identity scan requires of a marker (wave 3 review).
    state = await admin_client.get_room_state_events(room_id)
    (marker,) = [e for e in state if e["type"] == GROUP_MARKER]
    assert marker["sender"] == S.BOT_USER_ID
    forged = dump_room_state(GroupRoomState(group_id="someone-elses-group"))
    assert _put_state(moderator.access_token, room_id, GROUP_MARKER, forged) == 403
    # ...nor add a second marker of another type to confuse the identity.
    lobby_marker = event_type_name(S.SERVER_NAME, OnbotRoomType.visitor_lobby)
    assert _put_state(moderator.access_token, room_id, lobby_marker, forged) == 403


async def test_a_room_upgraded_by_a_human_is_skipped(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """D20: the bot does not follow an upgrade it did not make; it reports and skips the group.

    In a v12 room only a creator may send the tombstone (150), so the group's ``chatroom_params``
    (D6) lowers it for its moderators: the one way a human gets to upgrade a room the bot created.
    """
    prefix = S.uniq("upg")
    alias = S.uniq("upgroom")
    params = json.dumps({"power_level_content_override": {"events": {"m.room.tombstone": 50}}})
    group = _group(
        authentik_admin, prefix, alias, chatroom_params=params, **{"chat-systemwide-powerlevel": 50}
    )
    _u, moderator = S.provision(authentik_admin, S.uniq("upgrader"), groups=[group["pk"]])
    config = _config(make_config, prefix)
    await run_reconcile_once(config)
    room_id = await matrix_client.resolve_room_alias(_alias(alias))
    assert room_id is not None

    upgraded = _cs(
        "POST", moderator.access_token, f"rooms/{quote(room_id, safe='')}/upgrade", {"new_version": "12"}
    )
    assert upgraded.status_code == 200, upgraded.text
    new_room = upgraded.json()["replacement_room"]

    newcomer, login = S.provision(authentik_admin, S.uniq("newcomer"))
    authentik_admin.add_user_to_group(group["pk"], newcomer["pk"])
    await run_reconcile_once(config)

    # Skipped: nobody is joined into either room, and no third room appears.
    assert login.mxid not in await admin_client.list_room_members(room_id)
    assert login.mxid not in await admin_client.list_room_members(new_room)
    assert await _bot_rooms_named(admin_client, group["name"]) == [room_id]


# --- the one-time adoption of rooms from older versions -------------------------------------------


async def _legacy_room(matrix_client: ApiClientMatrix, alias: str, marker: dict[str, Any] | None) -> str:
    """A room as an older version created it: marker written after createRoom, nothing locked."""
    room_id = await matrix_client.create_room(
        alias_localpart=alias, name=alias, room_params={"preset": "private_chat"}
    )
    if marker is not None:
        await matrix_client.put_room_state_event(room_id, GROUP_MARKER, marker)
    return room_id


async def test_a_marked_bot_room_at_the_alias_is_adopted_and_locked(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("mig")
    alias = S.uniq("migroom")
    group = _group(authentik_admin, prefix, alias)
    legacy = await _legacy_room(
        matrix_client, alias, {"schema_version": 1, "room_type": "group_room", "group_id": group["pk"]}
    )
    _u, member = S.provision(authentik_admin, S.uniq("migmember"), groups=[group["pk"]])
    config = _config(make_config, prefix)

    await run_reconcile_once(config)

    assert member.mxid in await admin_client.list_room_members(legacy)
    assert await matrix_client.resolve_room_alias(_alias(alias)) == legacy
    power = await matrix_client.get_room_power_levels(legacy)
    create = await matrix_client.get_room_state_event(legacy, "m.room.create") or {}
    assert markers_locked(power, S.SERVER_NAME, S.BOT_USER_ID, room_version=create.get("room_version"))

    # Adopted once: with the alias gone, the next pass still finds the room by its marker.
    await matrix_client.delete_json(f"v3/directory/room/{quote(_alias(alias), safe='')}")
    _n, newcomer = S.provision(authentik_admin, S.uniq("mignew"), groups=[group["pk"]])
    await run_reconcile_once(config)
    assert newcomer.mxid in await admin_client.list_room_members(legacy)


async def test_an_unmarked_bot_room_at_the_alias_is_not_adopted(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("unm")
    alias = S.uniq("unmroom")
    group = _group(authentik_admin, prefix, alias)
    unmarked = await _legacy_room(matrix_client, alias, None)
    _u, member = S.provision(authentik_admin, S.uniq("unmmember"), groups=[group["pk"]])

    await run_reconcile_once(_config(make_config, prefix))

    assert member.mxid not in await admin_client.list_room_members(unmarked)
    assert await matrix_client.resolve_room_alias(_alias(alias)) == unmarked
    assert await _bot_rooms_named(admin_client, group["name"]) == []


async def test_a_legacy_room_whose_marker_a_moderator_rewrote_is_not_adopted(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """Wave 3 review: a stray legacy room (unlocked marker of a group that is gone). A moderator
    points its marker at a live group and gives it that group's alias. The marker's sender is the
    moderator, so the bot neither adopts nor locks the room, and joins nobody into it."""
    prefix = S.uniq("rwr")
    alias = S.uniq("rwrroom")
    group = _group(authentik_admin, prefix, alias)
    _m, moderator = S.provision(authentik_admin, S.uniq("rwrmod"))
    stray = await _legacy_room(
        matrix_client,
        S.uniq("rwrold"),
        {"schema_version": 1, "room_type": "group_room", "group_id": "a-group-that-is-gone"},
    )
    power = await matrix_client.get_room_power_levels(stray)
    power["users"] = {**(power.get("users") or {}), moderator.mxid: 50}
    await matrix_client.set_room_power_levels(stray, power)
    await matrix_client.invite_user(stray, moderator.mxid)
    joined = _cs("POST", moderator.access_token, f"rooms/{quote(stray, safe='')}/join", {})
    assert joined.status_code == 200, joined.text
    forged = {"schema_version": 1, "room_type": "group_room", "group_id": group["pk"]}
    assert _put_state(moderator.access_token, stray, GROUP_MARKER, forged) == 200
    put_alias = _cs(
        "PUT", moderator.access_token, f"directory/room/{quote(_alias(alias), safe='')}", {"room_id": stray}
    )
    assert put_alias.status_code == 200, put_alias.text
    _u, member = S.provision(authentik_admin, S.uniq("rwrmember"), groups=[group["pk"]])

    await run_reconcile_once(_config(make_config, prefix))

    assert member.mxid not in await admin_client.list_room_members(stray)
    after = await matrix_client.get_room_power_levels(stray)
    create = await matrix_client.get_room_state_event(stray, "m.room.create") or {}
    assert not markers_locked(after, S.SERVER_NAME, S.BOT_USER_ID, room_version=create.get("room_version"))
    assert await matrix_client.resolve_room_alias(_alias(alias)) == stray  # no second room either
    assert await _bot_rooms_named(admin_client, group["name"]) == []


async def test_a_forged_marker_in_a_foreign_room_is_not_adopted(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """The marker alone proves nothing: the bot must also be the room's creator."""
    prefix = S.uniq("frg")
    alias = S.uniq("frgroom")
    group = _group(authentik_admin, prefix, alias)
    _a, attacker = S.provision(authentik_admin, S.uniq("forger"))
    forged = {
        "type": GROUP_MARKER,
        "state_key": "",
        "content": {"room_type": "group_room", "group_id": group["pk"]},
    }
    squat = _user_creates_room(attacker.access_token, alias, preset="public_chat", initial_state=[forged])
    _u, member = S.provision(authentik_admin, S.uniq("frgmember"), groups=[group["pk"]])

    await run_reconcile_once(_config(make_config, prefix))

    assert member.mxid not in await admin_client.list_room_members(squat)


# --- ONB-17: the control room ---------------------------------------------------------------------


async def test_a_foreign_room_at_the_control_room_alias_is_never_taken_over(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    room_identity: RoomIdentityResolver,
) -> None:
    _a, attacker = S.provision(authentik_admin, S.uniq("ctlsquat"))
    _b, admin = S.provision(authentik_admin, S.uniq("ctladmin"))
    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]
    squat = _user_creates_room(attacker.access_token, config.admin_room.alias, preset="public_chat")

    resolver = AdminResolver(authentik_client, config, ttl_sec=0)
    assert await AdminRoomProvisioner(matrix_client, config, resolver, room_identity).ensure() is None
    # No help posted there, no admin invited into it.
    assert await admin_client.list_room_members(squat) == [attacker.mxid]
