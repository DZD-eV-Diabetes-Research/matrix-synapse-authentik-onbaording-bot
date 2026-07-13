"""End-to-end lobby behaviour against the live stack (ADR-0012, Session D).

Asserts the four halves of the lobby's promise and its two consequences:

* a space member outside the group **sees the lobby in the space hierarchy and does not see the group
  room**, **joins the lobby unaided**, **survives a full reconcile tick**, and **cannot join the
  group room**;
* a user removed from the Authentik group is kicked from the group room but **keeps their lobby seat**;
* a deactivated user is gone from **both** rooms, with no onbot code involved.

Each test drives the real composition root (``onbot.app.run_reconcile_once``).
"""

from __future__ import annotations

import asyncio

import pytest

from onbot.app import run_reconcile_once
from onbot.auth.token_provider import OAuth2ClientCredentialsTokenProvider
from onbot.clients.mas_admin import ApiClientMasAdmin, mxid_localpart
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.reconciler.rooms import compute_lobby_attributes, compute_room_attributes
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


async def _wait_not_member(
    admin: ApiClientSynapseAdmin, room_id: str, mxid: str, *, timeout: float = 30.0
) -> bool:
    """Poll until ``mxid`` is gone from ``room_id`` (room-leave on deactivation can trail slightly)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if mxid not in await admin.list_room_members(room_id):
            return True
        await asyncio.sleep(1.0)
    return mxid not in await admin.list_room_members(room_id)


def _lobby_config(make_config, prefix: str, space_alias: str) -> OnbotConfig:
    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    space_cfg = config.create_matrix_rooms_in_a_matrix_space
    space_cfg.enabled = True
    space_cfg.alias = space_alias
    space_cfg.create_matrix_space_if_not_exists.enabled = True
    config.matrix_room_default_settings.visitor_lobby_enabled = True
    return config


async def _resolve_rooms(
    matrix: ApiClientMatrix, config: OnbotConfig, group: dict, space_alias: str
) -> tuple[str, str, str]:
    """Resolve (space_id, group_room_id, lobby_id) from their aliases."""
    group_attrs = compute_room_attributes(group, config, S.SERVER_NAME)
    lobby_attrs = compute_lobby_attributes(group_attrs, config.matrix_room_default_settings, S.SERVER_NAME)
    space_id = await matrix.resolve_room_alias(f"#{space_alias}:{S.SERVER_NAME}")
    group_room_id = await matrix.resolve_room_alias(group_attrs.canonical_alias)
    lobby_id = await matrix.resolve_room_alias(lobby_attrs.canonical_alias)
    assert space_id and group_room_id and lobby_id
    return space_id, group_room_id, lobby_id


async def test_lobby_four_halves_of_the_promise(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("lobby")
    config = _lobby_config(make_config, prefix, S.uniq("lobbyspace"))
    group = authentik_admin.create_group(f"{prefix}-duesseldorf", attributes={"is_chatroom": True})
    # A group member (in the group room) and an outsider who is in the space but not the group.
    _member, member_login = S.provision(authentik_admin, S.uniq("member"), groups=[group["pk"]])
    _outsider, visitor = S.provision(authentik_admin, S.uniq("visitor"))

    await run_reconcile_once(config)
    space_id, group_room_id, lobby_id = await _resolve_rooms(
        matrix_client, config, group, config.create_matrix_rooms_in_a_matrix_space.alias
    )

    # Half 1: the outsider sees the lobby in the space hierarchy, and NOT the private group room.
    visible = S.space_hierarchy_room_ids(visitor.access_token, space_id)
    assert lobby_id in visible
    assert group_room_id not in visible

    # Half 2: the outsider joins the lobby unaided (restricted-to-space join rule permits it).
    assert S.join_room_status(visitor.access_token, lobby_id) == 200

    # Half 3: the visitor survives a full reconcile tick — a lobby is add-only, never kicks.
    await run_reconcile_once(config)
    assert visitor.mxid in await admin_client.list_room_members(lobby_id)

    # Half 4: the outsider cannot join the private group room.
    assert S.join_room_status(visitor.access_token, group_room_id) == 403

    # The group member is in both rooms (injected into the lobby by default).
    assert member_login.mxid in await admin_client.list_room_members(group_room_id)
    assert member_login.mxid in await admin_client.list_room_members(lobby_id)


async def test_removed_from_group_keeps_the_lobby_seat(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("keep")
    config = _lobby_config(make_config, prefix, S.uniq("keepspace"))
    group = authentik_admin.create_group(f"{prefix}-team", attributes={"is_chatroom": True})
    member, member_login = S.provision(authentik_admin, S.uniq("leaver"), groups=[group["pk"]])

    await run_reconcile_once(config)
    _space_id, group_room_id, lobby_id = await _resolve_rooms(
        matrix_client, config, group, config.create_matrix_rooms_in_a_matrix_space.alias
    )
    assert member_login.mxid in await admin_client.list_room_members(group_room_id)
    assert member_login.mxid in await admin_client.list_room_members(lobby_id)

    # Remove the user from the Authentik group and reconcile: kicked from the group room, kept in the
    # lobby, where they are now indistinguishable from any other visitor (ADR-0012).
    authentik_admin.remove_user_from_group(group["pk"], member["pk"])
    await run_reconcile_once(config)
    assert member_login.mxid not in await admin_client.list_room_members(group_room_id)
    assert member_login.mxid in await admin_client.list_room_members(lobby_id)


async def test_deactivated_user_leaves_both_rooms(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("deact")
    config = _lobby_config(make_config, prefix, S.uniq("deactspace"))
    group = authentik_admin.create_group(f"{prefix}-team", attributes={"is_chatroom": True})
    _member, member_login = S.provision(authentik_admin, S.uniq("gone"), groups=[group["pk"]])

    await run_reconcile_once(config)
    _space_id, group_room_id, lobby_id = await _resolve_rooms(
        matrix_client, config, group, config.create_matrix_rooms_in_a_matrix_space.alias
    )
    assert member_login.mxid in await admin_client.list_room_members(group_room_id)
    assert member_login.mxid in await admin_client.list_room_members(lobby_id)

    # "Except users removed from the whole system" needs no lobby-specific code (ADR-0012): removing
    # the account from the system drops it from every room, the lobby included. Under MAS the
    # system-removal path is MAS-side deactivation (§7 Q1 / ADR-0005), not the Synapse admin API,
    # which cannot revoke a MAS account. Deactivate straight through the MAS admin API — no reconciler
    # or lobby code runs — and the user leaves both rooms.
    mas = ApiClientMasAdmin(
        mas_url=S.MAS_URL,
        token_provider=OAuth2ClientCredentialsTokenProvider(
            token_endpoint=f"{S.MAS_URL}/oauth2/token",
            client_id=S.MAS_ADMIN_CLIENT_ID,
            client_secret=S.MAS_ADMIN_CLIENT_SECRET,
            scope="urn:mas:admin",
        ),
    )
    try:
        uid = await mas.get_user_id_by_username(mxid_localpart(member_login.mxid))
        assert uid is not None
        await mas.deactivate_user(uid)
    finally:
        await mas.aclose()

    assert await _wait_not_member(admin_client, group_room_id, member_login.mxid)
    assert await _wait_not_member(admin_client, lobby_id, member_login.mxid)
