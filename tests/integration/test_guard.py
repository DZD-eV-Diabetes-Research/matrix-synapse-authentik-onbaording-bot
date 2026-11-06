"""The destructive-action guard against the live stack (review session S3: REC-01, D13, MNT-09).

The live verification (08, check 3) showed the trigger: an Authentik API token whose role may see
every group but only some users answers ``200`` with just those users, and one pass then kicked every
member of every group room. Since S1 the same answer also makes every other known user look deleted
and locks them after the cooldown. Here such a token must pause everything (no kick, no withdrawn
power level, no lock), report a batch in the admin control room, and carry the batch out once an
admin confirms it there, batch by batch.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from urllib.parse import quote

import pytest
from pydantic import SecretStr

from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastService
from onbot.admin.control_room import ControlRoomHandler
from onbot.app import run_reconcile_once
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.events import EventBus
from onbot.guard import DestructiveActionGuard, GuardLimits, MatrixAccountDataGuardStore
from onbot.reconciler.rooms import compute_room_attributes
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration

PL_ATTR = "chat-systemwide-powerlevel"


@pytest.fixture
def groups(authentik_admin: S.AuthentikAdmin) -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    for pk in created:
        authentik_admin.delete_group(pk)


async def _bot_messages(client: ApiClientMatrix, room_id: str) -> list[str]:
    resp = await client.get_json(f"v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 100})
    return [
        (ev.get("content") or {}).get("body", "")
        for ev in reversed(resp.get("chunk", []))
        if ev.get("type") == "m.room.message" and ev.get("sender") == S.BOT_USER_ID
    ]


def _proposed_batches(messages: list[str]) -> list[str]:
    return [m.group(1) for body in messages for m in [re.search(r"!confirm ([a-z0-9]{6})", body)] if m]


async def _confirm(
    handler: ControlRoomHandler, client: ApiClientMatrix, admin_token: str, room_id: str, batch_id: str
) -> None:
    """The admin types ``!confirm <id>`` in the control room; the handler reads it from a real sync."""
    assert S.send_message_status(admin_token, room_id, f"!confirm {batch_id}") == 200
    await handler.handle_sync(await client.sliding_sync(None))


async def test_a_narrowed_token_pauses_everything_until_an_admin_confirms(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    groups: list[str],
    own_offboarding_records: None,
) -> None:
    prefix = S.uniq("rec01")
    scope = {"rec01scope": prefix}  # only this test's users are in the sync scope
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True, PL_ATTR: 50})
    groups.append(group["pk"])
    keep_user, keep = S.provision(
        authentik_admin, S.uniq("rec01keep"), groups=[group["pk"]], attributes=scope
    )
    gone = [
        S.provision(authentik_admin, S.uniq(f"rec01gone{n}"), groups=[group["pk"]], attributes=scope)
        for n in range(2)
    ]
    admin_user, admin = S.provision(authentik_admin, S.uniq("rec01admin"), attributes=scope)

    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    config.sync_authentik_users_with_matrix_rooms.sync_only_users_with_authentik_attributes = scope
    life = config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
    life.enabled, life.dry_run, life.deactivate_after_n_sec, life.delete_after_n_sec = True, False, 0, None
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]

    await run_reconcile_once(config)  # the normal pass: room, members, levels, baseline, known users
    room_id = await matrix_client.resolve_room_alias(
        compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    )
    control_room = await matrix_client.resolve_room_alias(f"#{config.admin_room.alias}:{S.SERVER_NAME}")
    assert room_id is not None and control_room is not None
    everyone = {keep.mxid, *(login.mxid for _u, login in gone)}
    assert everyone <= set(await admin_client.list_room_members(room_id))
    levels = (await matrix_client.get_room_power_levels(room_id))["users"]
    assert all(levels[mxid] == 50 for mxid in everyone)

    # The token is narrowed: it still sees every group, but of the users only `keep` and the admin.
    narrowed = config.model_copy(deep=True)
    narrowed.authentik_server.api_key = SecretStr(
        authentik_admin.narrowed_api_token([keep_user["pk"], admin_user["pk"]])
    )
    await run_reconcile_once(narrowed)  # kicks and withdrawals planned and paused; the others marked
    await run_reconcile_once(narrowed)  # cooldown 0: their locks planned and paused as well

    members = set(await admin_client.list_room_members(room_id))
    levels = (await matrix_client.get_room_power_levels(room_id))["users"]
    for user, login in gone:
        assert login.mxid in members, "kicked on a collapsed Authentik answer"
        assert levels.get(login.mxid) == 50, "power level withdrawn on a collapsed Authentik answer"
        assert S.mas_user(user["username"])["locked_at"] is None, "locked on a collapsed answer"
    reports = await _bot_messages(matrix_client, control_room)
    batches = _proposed_batches(reports)
    assert len(batches) == 2, reports  # kicks and withdrawals first, the locks one pass later
    first, second = batches
    first_report = next(r for r in reports if f"!confirm {first}" in r)
    assert "active users in the sync scope dropped from 4 to 2" in first_report
    assert "kick 2 room member(s)" in first_report and "withdraw 2 power level(s)" in first_report
    assert "lock 2 account(s)" in next(r for r in reports if f"!confirm {second}" in r)

    # An admin releases the first batch in the control room.
    await admin_client.add_user_to_room(control_room, admin.mxid)
    guard = DestructiveActionGuard(
        GuardLimits.from_config(config),
        MatrixAccountDataGuardStore(matrix_client, S.BOT_USER_ID, S.SERVER_NAME),
        EventBus(),
    )
    handler = ControlRoomHandler(
        matrix_client,
        config,
        BroadcastService(matrix_client, config),
        AdminResolver(authentik_client, config, ttl_sec=0),
        guard=guard,
    )
    await handler.start(control_room)
    await _confirm(handler, matrix_client, admin.access_token, control_room, first)
    await run_reconcile_once(narrowed)

    members = set(await admin_client.list_room_members(room_id))
    levels = (await matrix_client.get_room_power_levels(room_id))["users"]
    for user, login in gone:
        assert login.mxid not in members
        assert login.mxid not in levels
        assert S.mas_user(user["username"])["locked_at"] is None  # not part of what was confirmed
    assert keep.mxid in members and levels[keep.mxid] == 50

    # Then the second one.
    await _confirm(handler, matrix_client, admin.access_token, control_room, second)
    await run_reconcile_once(narrowed)
    for user, _login in gone:
        assert S.mas_user(user["username"])["locked_at"] is not None
    reports = await _bot_messages(matrix_client, control_room)
    assert any(f"Batch {first} confirmed by {admin.mxid}: ran 4 action(s)" in r for r in reports)
    assert any(f"Batch {second} confirmed by {admin.mxid}: ran 2 action(s)" in r for r in reports)


async def _synapse_deactivated(
    admin_client: ApiClientSynapseAdmin, mxid: str, *, timeout: float = 60.0
) -> bool:
    """Whether Synapse reports ``mxid`` deactivated; MAS hands it over through a background job."""
    deadline = time.monotonic() + timeout
    while True:
        user = await admin_client.get_json(f"v2/users/{quote(mxid, safe='@:')}")
        if user.get("deactivated"):
            return True
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(1)


async def test_three_erasures_wait_for_a_confirmation_in_the_admin_room(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    own_offboarding_records: None,
) -> None:
    """S5 (D4 layer 3): three disabled users past the erase cooldown are erased only after !confirm."""
    prefix = S.uniq("layer3")
    scope = {"layer3scope": prefix}
    gone = [S.provision(authentik_admin, S.uniq(f"layer3gone{n}"), attributes=scope) for n in range(3)]
    admin_user, admin = S.provision(authentik_admin, S.uniq("layer3admin"), attributes=scope)
    for _ in range(17):  # active users who stay, so three leaving is no implausible drop (layer 1)
        authentik_admin.create_user(S.uniq("layer3anchor"), password=S.DEFAULT_PASSWORD, attributes=scope)

    config = make_config()
    config.sync_authentik_users_with_matrix_rooms.sync_only_users_with_authentik_attributes = scope
    life = config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
    life.enabled, life.dry_run, life.deactivate_after_n_sec, life.delete_after_n_sec = True, False, 0, 0
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]

    await run_reconcile_once(config)  # the normal pass: baseline and known users
    for user, _login in gone:
        authentik_admin.set_active(user["pk"], False)
    await run_reconcile_once(config)  # marked
    await run_reconcile_once(config)  # locked: three locks are below the layer-2 limit
    for user, _login in gone:
        assert S.mas_user(user["username"])["locked_at"] is not None
    await run_reconcile_once(config)  # the erasures are due: three of them wait for an admin

    for user, login in gone:
        assert S.mas_user(user["username"]).get("deactivated_at") is None, "erased without a confirmation"
        assert not (await admin_client.get_json(f"v2/users/{quote(login.mxid, safe='@:')}")).get(
            "deactivated"
        )
    control_room = await matrix_client.resolve_room_alias(f"#{config.admin_room.alias}:{S.SERVER_NAME}")
    assert control_room is not None
    reports = await _bot_messages(matrix_client, control_room)
    [batch_id] = _proposed_batches(reports)
    report = next(r for r in reports if f"!confirm {batch_id}" in r)
    assert "erase 3 account(s) (irreversible)" in report
    assert "Irreversible: 3 of these action(s) cannot be undone" in report
    assert all(f"erase account {login.mxid} (irreversible)" in report for _u, login in gone)

    await admin_client.add_user_to_room(control_room, admin.mxid)
    guard = DestructiveActionGuard(
        GuardLimits.from_config(config),
        MatrixAccountDataGuardStore(matrix_client, S.BOT_USER_ID, S.SERVER_NAME),
        EventBus(),
    )
    handler = ControlRoomHandler(
        matrix_client,
        config,
        BroadcastService(matrix_client, config),
        AdminResolver(authentik_client, config, ttl_sec=0),
        guard=guard,
    )
    await handler.start(control_room)
    await _confirm(handler, matrix_client, admin.access_token, control_room, batch_id)
    await run_reconcile_once(config)

    for _user, login in gone:
        assert await _synapse_deactivated(admin_client, login.mxid)
    reports = await _bot_messages(matrix_client, control_room)
    assert any(f"Batch {batch_id} confirmed by {admin.mxid}: ran 3 action(s)" in r for r in reports)
    assert S.mas_user(admin_user["username"]).get("deactivated_at") is None


async def test_reconcile_once_dry_run_changes_nothing(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    groups: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MNT-09: the preview reads the live servers and writes nothing, the admin API included."""
    prefix = S.uniq("dry")
    group = authentik_admin.create_group(f"{prefix}-room", attributes={"is_chatroom": True})
    groups.append(group["pk"])
    leaver_user, leaver = S.provision(authentik_admin, S.uniq("dryleaver"), groups=[group["pk"]])
    S.provision(authentik_admin, S.uniq("drystay"), groups=[group["pk"]])
    config = make_config()
    config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
    await run_reconcile_once(config)
    room_id = await matrix_client.resolve_room_alias(
        compute_room_attributes(group, config, S.SERVER_NAME).canonical_alias
    )
    assert room_id is not None

    authentik_admin.remove_user_from_group(group["pk"], leaver_user["pk"])
    new_group = authentik_admin.create_group(f"{prefix}-new", attributes={"is_chatroom": True})
    groups.append(new_group["pk"])
    with caplog.at_level("INFO"):
        await run_reconcile_once(config, dry_run=True)

    assert leaver.mxid in await admin_client.list_room_members(room_id)
    new_alias = compute_room_attributes(new_group, config, S.SERVER_NAME).canonical_alias
    assert await matrix_client.resolve_room_alias(new_alias) is None
    assert f"[dry-run] would kick {leaver.mxid} from {room_id}" in caplog.text
    assert "[dry-run] would create group room" in caplog.text

    await run_reconcile_once(config)  # and the real pass does what the preview said
    assert leaver.mxid not in await admin_client.list_room_members(room_id)
    assert await matrix_client.resolve_room_alias(new_alias) is not None
