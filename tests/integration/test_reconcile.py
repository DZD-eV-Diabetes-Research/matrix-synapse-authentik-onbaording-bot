"""End-to-end reconciler behaviour against the live stack: group->room projection, membership,
power levels, and the parent space (GOALS G2/G3/G6/G8). Each test drives the real composition root
(``onbot.app.run_reconcile_once``) so app.py is exercised too."""

from __future__ import annotations

import pytest

from onbot.app import run_reconcile_once
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.reconciler.rooms import compute_room_attributes
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


async def test_group_projection_and_membership(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("team")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    _user, login = S.provision(authentik_admin, S.uniq("member"), groups=[group["pk"]])

    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    await run_reconcile_once(config)

    # The group was projected to a room at the alias onbot computes from the group.
    alias = compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    room_id = await matrix_client.resolve_room_alias(alias)
    assert room_id is not None
    # The group member was joined to that room.
    members = await admin_client.list_room_members(room_id)
    assert login.mxid in members


async def test_power_level_from_group_attribute(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    prefix = S.uniq("pl")
    group = authentik_admin.create_group(
        f"{prefix}-room", attributes={"is_chatroom": True, "chat-systemwide-powerlevel": 50}
    )
    _user, login = S.provision(authentik_admin, S.uniq("plmember"), groups=[group["pk"]])

    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    await run_reconcile_once(config)

    alias = compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    room_id = await matrix_client.resolve_room_alias(alias)
    assert room_id is not None
    power = await matrix_client.get_room_power_levels(room_id)
    assert power.get("users", {}).get(login.mxid) == 50


async def test_parent_space_creation_and_membership(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    config = make_config()
    alias = S.uniq("space")
    space_cfg = config.create_matrix_rooms_in_a_matrix_space
    space_cfg.enabled = True
    space_cfg.alias = alias
    space_cfg.create_matrix_space_if_not_exists.enabled = True
    _user, login = S.provision(authentik_admin, S.uniq("spaceuser"))

    await run_reconcile_once(config)

    space_id = await matrix_client.resolve_room_alias(f"#{alias}:{S.SERVER_NAME}")
    assert space_id is not None
    members = await admin_client.list_room_members(space_id)
    assert login.mxid in members
