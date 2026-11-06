"""Reconcile pass robustness against the live stack (review session S2).

Reproduces the two permanent pass aborts of the 2026-11-04 live verification
(docs/review_26-11-04/08-live-verification.md) and asserts the fixed behaviour: a room the bot
cannot fully manage is reported, and every other room and the account lifecycle still converge.

* A banned group member: the admin join API acts as that user and is refused on every pass (REC-03,
  08 "check 3" style failure, side evidence in check 9).
* A pre-v12 room (v10, v11) where users sit at the bot's own level 100: the bot can neither demote
  nor kick them (REC-05, 08 check 5).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from onbot.app import run_reconcile_once
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.lifecycle.accounts import lifecycle_account_data_type
from onbot.reconciler.rooms import compute_room_attributes
from onbot.reconciler.state import granted_power_levels_type
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration

_ENGINE_LOGGER = "onbot.reconciler.engine"


@pytest.fixture
def groups(authentik_admin: S.AuthentikAdmin) -> Iterator[list[str]]:
    """Group pks to delete after the test, so later unfiltered passes do not keep these rooms."""
    created: list[str] = []
    yield created
    for pk in created:
        authentik_admin.delete_group(pk)


def _not_converged(caplog: pytest.LogCaptureFixture, room_id: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _ENGINE_LOGGER and "room not converged" in r.getMessage() and room_id in r.getMessage()
    ]


async def test_a_banned_group_member_does_not_stop_rooms_or_offboarding(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    groups: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    prefix = S.uniq("ban")
    group_a = authentik_admin.create_group(f"{prefix}-a", attributes={"is_chatroom": True})
    groups.append(group_a["pk"])
    _member, member = S.provision(authentik_admin, S.uniq("banok"), groups=[group_a["pk"]])
    banned_user, banned = S.provision(authentik_admin, S.uniq("banned"))
    leaver_user, leaver = S.provision(authentik_admin, S.uniq("banleaver"))

    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    life = config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
    life.enabled = True
    life.dry_run = True  # marking is all this test needs from the lifecycle
    life.deactivate_after_n_sec = 3600
    await run_reconcile_once(config)

    room_a = await matrix_client.resolve_room_alias(
        compute_room_attributes(group_a, config, S.SERVER_NAME).canonical_alias
    )
    assert room_a is not None
    assert member.mxid in await admin_client.list_room_members(room_a)

    # A moderator bans a user who then joins the group: every join attempt is refused from now on.
    bot_token = config.synapse_server.bot_access_token
    assert bot_token is not None
    S.ban_user(bot_token.get_secret_value(), room_a, banned.mxid)
    authentik_admin.add_user_to_group(group_a["pk"], banned_user["pk"])
    # In the same pass: a brand new group with a member, and a user disabled in Authentik.
    group_b = authentik_admin.create_group(f"{prefix}-b", attributes={"is_chatroom": True})
    groups.append(group_b["pk"])
    _other, other = S.provision(authentik_admin, S.uniq("banother"), groups=[group_b["pk"]])
    authentik_admin.set_active(leaver_user["pk"], False)

    with caplog.at_level(logging.WARNING, logger=_ENGINE_LOGGER):
        await run_reconcile_once(config)  # used to raise on the refused join, every pass

    # The broken room is reported once, with its group and the refused user ...
    lines = _not_converged(caplog, room_a)
    assert len(lines) == 1
    assert str(group_a["pk"]) in lines[0] and banned.mxid in lines[0]
    assert banned.mxid not in await admin_client.list_room_members(room_a)
    # ... the other room converged ...
    room_b = await matrix_client.resolve_room_alias(
        compute_room_attributes(group_b, config, S.SERVER_NAME).canonical_alias
    )
    assert room_b is not None
    assert other.mxid in await admin_client.list_room_members(room_b)
    # ... and the lifecycle ran: the disabled user is marked in the bot's ledger.
    ledger = await matrix_client.get_account_data(S.BOT_USER_ID, lifecycle_account_data_type(S.SERVER_NAME))
    assert leaver.mxid in ledger.get("entries", {})


@pytest.mark.parametrize("room_version", ["10", "11"])
async def test_a_legacy_room_caps_grants_and_reports_users_at_the_bots_level(
    room_version: str,
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    groups: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    prefix = S.uniq(f"leg{room_version}")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    groups.append(group["pk"])
    gin_user, gin = S.provision(authentik_admin, S.uniq("gin"), groups=[group["pk"]])
    gout_user, gout = S.provision(authentik_admin, S.uniq("gout"))

    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    config.synapse_server.room_version = room_version
    config.global_room_admins = [gin_user["username"], gout_user["username"]]
    await run_reconcile_once(config)

    room_id = await matrix_client.resolve_room_alias(
        compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    )
    assert room_id is not None
    assert (await admin_client.get_room_details(room_id))["version"] == room_version
    users = (await matrix_client.get_room_power_levels(room_id))["users"]
    # The bot is an ordinary entry at 100 here: it never hands out its own level.
    assert users[S.BOT_USER_ID] == 100
    assert users[gin.mxid] == 99
    assert users[gout.mxid] == 99

    # The state an older bot version left behind: both seated at 100, no per-room grant record (only
    # the global_admins list in the room state, which that version already wrote).
    power = await matrix_client.get_room_power_levels(room_id)
    power["users"][gin.mxid] = 100
    power["users"][gout.mxid] = 100
    await matrix_client.set_room_power_levels(room_id, power)
    await matrix_client.set_room_account_data(
        S.BOT_USER_ID, room_id, granted_power_levels_type(S.SERVER_NAME), {}
    )

    # Both leave the config; an unrelated group appears in the same pass.
    config.global_room_admins = []
    other_group = authentik_admin.create_group(f"{prefix}-other", attributes={"is_chatroom": True})
    groups.append(other_group["pk"])
    _u, other = S.provision(authentik_admin, S.uniq("legother"), groups=[other_group["pk"]])

    for _ in range(2):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_ENGINE_LOGGER):
            await run_reconcile_once(config)  # used to raise 403 on kick or demotion, every pass

        lines = _not_converged(caplog, room_id)
        assert len(lines) == 1
        assert f"cannot kick {gout.mxid}" in lines[0]
        assert f"cannot change power level of {gin.mxid}" in lines[0]
        assert f"cannot change power level of {gout.mxid}" in lines[0]

    users = (await matrix_client.get_room_power_levels(room_id))["users"]
    assert users[gin.mxid] == 100 and users[gout.mxid] == 100
    assert gout.mxid in await admin_client.list_room_members(room_id)
    other_room = await matrix_client.resolve_room_alias(
        compute_room_attributes(other_group, config, S.SERVER_NAME).canonical_alias
    )
    assert other_room is not None
    assert other.mxid in await admin_client.list_room_members(other_room)


async def test_a_level_granted_by_the_bot_is_withdrawn_when_the_member_leaves(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    groups: list[str],
) -> None:
    """D10 / REC-08 live, with kicks off: the ex-member stays in the room but loses the level."""
    prefix = S.uniq("wd")
    group = authentik_admin.create_group(
        f"{prefix}-room", attributes={"is_chatroom": True, "chat-systemwide-powerlevel": 50}
    )
    groups.append(group["pk"])
    mod_user, mod = S.provision(authentik_admin, S.uniq("wdmod"), groups=[group["pk"]])
    _stay, stay = S.provision(authentik_admin, S.uniq("wdstay"), groups=[group["pk"]])

    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    sync = config.sync_authentik_users_with_matrix_rooms
    sync.kick_matrix_room_members_not_in_mapped_authentik_group_anymore = False
    await run_reconcile_once(config)

    room_id = await matrix_client.resolve_room_alias(
        compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    )
    assert room_id is not None
    users = (await matrix_client.get_room_power_levels(room_id))["users"]
    assert users[mod.mxid] == 50

    authentik_admin.remove_user_from_group(group["pk"], mod_user["pk"])
    await run_reconcile_once(config)

    users = (await matrix_client.get_room_power_levels(room_id))["users"]
    assert mod.mxid not in users
    assert users[stay.mxid] == 50
