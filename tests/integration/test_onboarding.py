"""End-to-end onboarding: a provisioned user gets a welcome DM, sent exactly once (idempotent,
GOALS G4.1/G4.3). The welcome fires via the reconciler's user-synced signal during reconcile.

That DM is a read-only notice board: the bot force-joins the user into it and holds the only power
level that may post. Both are asserted here against the live homeserver, because both depend on
Synapse's own rules — that the admin join API works on an invite-only room the calling admin may
invite into, and that a user at power level 0 is refused when they try to send."""

from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest

from onbot.app import run_reconcile_once
from onbot.clients.matrix import ApiClientMatrix
from onbot.onboarding.welcome import welcome_txn_id
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


async def test_welcome_dm_sent_once(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    _user, login = S.provision(authentik_admin, S.uniq("welcome"))
    config = make_config()
    assert config.welcome_new_users_messages  # the default welcome set

    # Two reconcile passes: onboarding must be idempotent across repeated triggers.
    await run_reconcile_once(config)
    await run_reconcile_once(config)

    # Exactly one DM room is recorded for the user in the bot's m.direct account data.
    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    rooms = direct.get(login.mxid) or []
    assert len(rooms) == 1
    room_id = rooms[0]

    # Each configured welcome message was sent exactly once (no duplicates on the second pass).
    resp = await matrix_client.get_json(f"v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 100})
    bot_messages = [
        ev
        for ev in resp.get("chunk", [])
        if ev.get("type") == "m.room.message" and ev.get("sender") == S.BOT_USER_ID
    ]
    assert len(bot_messages) == len(config.welcome_new_users_messages)


async def _welcome_room_of(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix, prefix: str
) -> tuple[S.LoginResult, str]:
    """Provision a user, reconcile once, and return them alongside their welcome room."""
    _user, login = S.provision(authentik_admin, S.uniq(prefix))
    await run_reconcile_once(make_config())
    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    return login, (direct[login.mxid])[0]


async def test_user_is_force_joined_into_the_welcome_room(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    login, room_id = await _welcome_room_of(make_config, authentik_admin, matrix_client, "forcejoin")

    # Joined outright — the user never saw, let alone accepted, an invitation.
    member = await matrix_client.get_room_state_event(room_id, "m.room.member", login.mxid)
    assert member is not None and member["membership"] == "join"


async def test_the_welcome_room_is_read_only_for_its_user(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    login, room_id = await _welcome_room_of(make_config, authentik_admin, matrix_client, "readonly")

    # The stack forces room version 12 (see stack/synapse/homeserver.yaml), so the bot — as room
    # creator — holds an infinite power level and is DELIBERATELY ABSENT from m.room.power_levels:
    # naming a creator in the `users` map is rejected by the v12 auth rules. Absent ≠ powerless.
    create = await matrix_client.get_room_state_event(room_id, "m.room.create")
    assert create is not None and create.get("room_version") == "12"

    levels = await matrix_client.get_room_power_levels(room_id)
    assert S.BOT_USER_ID not in levels.get("users", {})  # the creator is not (and cannot be) listed
    assert login.mxid not in levels.get("users", {})  # the user sits at users_default
    assert levels["users_default"] == 0

    # The bot's authority is real regardless of the empty `users` map: Synapse actually refuses the
    # user's send, so the composer being hidden in Element is not the only thing stopping them.
    assert S.send_message_status(login.access_token, room_id, "can I talk here?") == 403

    # Force-joining skips the invite that makes a client tag the room as a DM, so it carries a name.
    name = await matrix_client.get_room_state_event(room_id, "m.room.name")
    assert name is not None and name["name"]


async def test_a_repeated_welcome_send_is_deduplicated_by_its_transaction_id(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    """ONB-09: the welcome flow's deterministic txn id makes Synapse drop a repeated send."""
    _login, room_id = await _welcome_room_of(make_config, authentik_admin, matrix_client, "txn")
    before = await _bot_message_bodies(matrix_client, room_id)
    txn = welcome_txn_id(room_id, "test-key")

    first = await matrix_client.send_text_message(room_id, "only once", txn_id=txn)
    again = await matrix_client.send_text_message(room_id, "only once", txn_id=txn)

    assert first == again
    after = await _bot_message_bodies(matrix_client, room_id)
    assert after.count("only once") == 1 and len(after) == len(before) + 1


async def test_a_user_who_left_their_notice_board_is_invited_back_once(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    """ONB-07: the room is invite-only, so without the invitation a "Leave" would be for good."""
    login, room_id = await _welcome_room_of(make_config, authentik_admin, matrix_client, "leaver")
    S.leave_room(login.access_token, room_id)

    await run_reconcile_once(make_config())  # a new process checks every user once
    assert await matrix_client.get_membership(room_id, login.mxid) == "invite"

    S.leave_room(login.access_token, room_id)  # declined
    await run_reconcile_once(make_config())
    assert await matrix_client.get_membership(room_id, login.mxid) == "leave"  # not invited twice


async def test_a_notice_board_the_user_kicked_the_bot_from_is_replaced(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    """W4R-03 (ONB-08): in a room from before the notice boards a user can outrank the bot and kick
    it. Synapse then still answers the bot's reads from the state at the time it left, so a failed
    read never told the bot it had lost the board: every welcome failed with a 403 on the send."""
    _user, login = S.provision(authentik_admin, S.uniq("kicker"))
    legacy = await matrix_client.create_room(
        name="legacy welcome room",
        is_direct=True,
        invite=[login.mxid],
        room_params={"room_version": "10", "preset": "private_chat"},
    )
    assert S.join_room_status(login.access_token, legacy) == 200
    # The user above the bot: in a room older than version 12 the creator is an ordinary level.
    power = await matrix_client.get_room_power_levels(legacy)
    users = {S.BOT_USER_ID: 50, login.mxid: 100}
    await matrix_client.set_room_power_levels(legacy, {**power, "users": users})
    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    await matrix_client.set_account_data(S.BOT_USER_ID, "m.direct", {**direct, login.mxid: [legacy]})
    kicked = httpx.post(
        f"{S.SYNAPSE_URL}/_matrix/client/v3/rooms/{quote(legacy, safe='')}/kick",
        headers={"Authorization": f"Bearer {login.access_token}"},
        json={"user_id": S.BOT_USER_ID},
        timeout=30.0,
    )
    assert kicked.status_code == 200, kicked.text
    # The Synapse behaviour the fix rests on: reads still answer, the bot's own membership says leave.
    assert await matrix_client.get_room_state_event(legacy, "m.room.name") is not None
    assert await matrix_client.get_membership(legacy, S.BOT_USER_ID) == "leave"
    assert legacy not in await matrix_client.joined_rooms()

    config = make_config()
    await run_reconcile_once(config)

    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    (board,) = direct[login.mxid]
    assert board != legacy
    assert board in await matrix_client.joined_rooms()
    messages = config.welcome_new_users_messages or []
    assert len(await _bot_message_bodies(matrix_client, board)) == len(messages)


async def _bot_message_bodies(client: ApiClientMatrix, room_id: str) -> list[str]:
    resp = await client.get_json(f"v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 100})
    return [
        (ev.get("content") or {}).get("body", "")
        for ev in resp.get("chunk", [])
        if ev.get("type") == "m.room.message" and ev.get("sender") == S.BOT_USER_ID
    ]
