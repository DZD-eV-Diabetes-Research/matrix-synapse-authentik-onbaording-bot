"""Renamed users keep their Matrix account, rooms and power levels (D8, SEC-07) on the live stack.

The stack's Authentik provider sends the hashed user ID as ``sub`` (Authentik's default), so MAS
keeps linking a renamed user to the account it created at the first login. Each test renames users
in Authentik (or changes the mapping attribute in the config) between real ``reconcile-once`` runs
and asserts that nothing is kicked, demoted, locked, re-invited or welcomed again. The tests run
with live offboarding (no dry run, no cooldown), so a wrong identity would lock somebody.
"""

from __future__ import annotations

from typing import Any

import pytest

from onbot.app import run_reconcile_once
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.reconciler.rooms import compute_room_attributes
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


def _config(make_config: Any, prefix: str, group_pks: list[str], *, mas: bool = True) -> OnbotConfig:
    config: OnbotConfig = make_config()
    if not mas:
        config.mas_admin = None
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    sync = config.sync_authentik_users_with_matrix_rooms
    # Only this test's users are in scope, so the live offboarding below judges nobody else.
    sync.sync_only_users_of_groups_with_id = group_pks
    life = sync.deactivate_disabled_authentik_users_in_matrix
    life.enabled = mas  # without mas_admin onbot refuses to lock on this MAS-backed homeserver
    life.dry_run = False
    life.deactivate_after_n_sec = 0
    config.welcome_new_users_messages = ["Welcome!"]
    return config


async def _room_of(group: dict[str, Any], config: OnbotConfig, matrix: ApiClientMatrix) -> str:
    alias = compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    room_id = await matrix.resolve_room_alias(alias)
    assert room_id is not None
    return room_id


async def _state(
    room_id: str, matrix: ApiClientMatrix, admin: ApiClientSynapseAdmin
) -> tuple[set[str], dict[str, int], dict[str, Any]]:
    members = set(await admin.list_room_members(room_id))
    levels = dict((await matrix.get_room_power_levels(room_id)).get("users") or {})
    direct = await matrix.get_account_data(S.BOT_USER_ID, "m.direct")
    return members, levels, direct


def _not_locked(*localparts: str) -> None:
    for localpart in localparts:
        user = S.mas_user(localpart)
        assert user["locked_at"] is None and user["deactivated_at"] is None, localpart


@pytest.mark.parametrize("mas", [True, False], ids=["through-mas", "through-binding"])
async def test_renamed_users_keep_account_rooms_and_power_levels(
    mas: bool,
    make_config: Any,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    prefix = S.uniq("rename")
    group = authentik_admin.create_group(
        f"{prefix}-room", attributes={"is_chatroom": True, "chat-systemwide-powerlevel": 50}
    )
    alice_name, bob_name = S.uniq("alice"), S.uniq("bob")
    alice, alice_login = S.provision(authentik_admin, alice_name, groups=[group["pk"]])
    _bob, bob_login = S.provision(authentik_admin, bob_name, groups=[group["pk"]])
    config = _config(make_config, prefix, [str(group["pk"])], mas=mas)

    await run_reconcile_once(config)
    room_id = await _room_of(group, config, matrix_client)
    before = await _state(room_id, matrix_client, admin_client)
    assert {alice_login.mxid, bob_login.mxid} <= before[0]
    assert before[1][alice_login.mxid] == 50
    assert alice_login.mxid in before[2]  # welcomed once

    new_name = S.uniq("alicia")
    authentik_admin.rename(alice["pk"], new_name)
    for _ in range(2):
        await run_reconcile_once(config)

    after = await _state(room_id, matrix_client, admin_client)
    assert after[0] == before[0]  # nobody kicked, nobody new joined
    assert after[1] == before[1]  # power levels untouched
    assert after[2] == before[2]  # no second welcome room, none for the new name
    assert f"@{new_name}:{S.SERVER_NAME}" not in after[2]
    _not_locked(alice_name, bob_name)
    assert S.whoami_status(alice_login.access_token) == 200

    # MAS links by the immutable subject: logging in under the new name lands in the same account.
    relogin = S.mas_login(new_name, S.DEFAULT_PASSWORD)
    assert relogin.mxid == alice_login.mxid


async def test_a_changed_mapping_attribute_kicks_and_locks_nobody(
    make_config: Any,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """A config change that maps every user to another MXID (D8: with the binding, no mass kicks)."""
    prefix = S.uniq("remap")
    group = authentik_admin.create_group(
        f"{prefix}-room", attributes={"is_chatroom": True, "chat-systemwide-powerlevel": 50}
    )
    names = [S.uniq(f"remap{i}") for i in range(3)]
    logins = [
        S.provision(authentik_admin, n, groups=[group["pk"]], attributes={"matrix_name": f"x{n}"})[1]
        for n in names
    ]
    config = _config(make_config, prefix, [str(group["pk"])])
    await run_reconcile_once(config)
    room_id = await _room_of(group, config, matrix_client)
    before = await _state(room_id, matrix_client, admin_client)
    assert {login.mxid for login in logins} <= before[0]

    config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute = (
        "attributes.matrix_name"
    )
    await run_reconcile_once(config)
    after = await _state(room_id, matrix_client, admin_client)
    assert after[:2] == before[:2]
    _not_locked(*names)


async def test_an_attribute_guess_does_not_take_a_mas_linked_account(
    make_config: Any,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    """SEC-07, D21: a second Authentik user whose mapping attribute names alice's account, without
    a MAS link of their own, gets nothing: none of their groups reach the account. MAS links the
    account to alice, so she stays managed as usual: not kicked or demoted, and offboarded when
    she is disabled."""
    prefix = S.uniq("dup")
    team = authentik_admin.create_group(
        f"{prefix}-team", attributes={"is_chatroom": True, "chat-systemwide-powerlevel": 50}
    )
    other = authentik_admin.create_group(f"{prefix}-other", attributes={"is_chatroom": True})
    alice_name = S.uniq("dupalice")
    alice, alice_login = S.provision(
        authentik_admin, alice_name, groups=[team["pk"]], attributes={"matrix_name": alice_name}
    )
    config = _config(make_config, prefix, [str(team["pk"]), str(other["pk"])])
    config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute = (
        "attributes.matrix_name"
    )
    await run_reconcile_once(config)
    team_room = await _room_of(team, config, matrix_client)
    other_room = await _room_of(other, config, matrix_client)
    before = await _state(team_room, matrix_client, admin_client)
    assert alice_login.mxid in before[0]

    authentik_admin.create_user(
        S.uniq("zcarl"),  # sorts after alice: the old code let the last user win the account
        password=S.DEFAULT_PASSWORD,
        groups=[other["pk"]],
        attributes={"matrix_name": alice_name},
    )
    await run_reconcile_once(config)

    after = await _state(team_room, matrix_client, admin_client)
    assert after[:2] == before[:2]  # alice stays, at her level
    assert alice_login.mxid not in await admin_client.list_room_members(other_room)  # carl's groups
    _not_locked(alice_name)

    # Others stay in scope, so one leaver stays under the guard's 20 % drop limit (they never log
    # in, an attribute is enough to count as a user in scope).
    for _ in range(6):
        anchor = S.uniq("anchor")
        authentik_admin.create_user(
            anchor, password=S.DEFAULT_PASSWORD, groups=[team["pk"]], attributes={"matrix_name": anchor}
        )
    await run_reconcile_once(config)  # the new baseline
    authentik_admin.set_active(alice["pk"], False)
    await run_reconcile_once(config)  # marks her
    await run_reconcile_once(config)  # locks her: the conflict did not freeze her account
    assert S.mas_user(alice_name)["locked_at"] is not None
