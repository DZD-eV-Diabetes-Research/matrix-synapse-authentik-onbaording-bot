"""End-to-end admin control room (ADR-0010) against the live stack.

The unit tests prove the allowlist is consulted. Only the live homeserver can prove the parts that
depend on Synapse's own rules: that a non-admin *can* speak in the control room (so the allowlist,
not the power level, is what actually stops them), that the announcement is really delivered into a
provisioned user's read-only notice board, and that the replayed sync timeline does not send it twice.

And only a live Authentik can prove that a group membership really grants the bot's commands, and —
the part that matters — that taking it away really takes them away, on a running bot.
"""

from __future__ import annotations

import pytest

from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastService
from onbot.admin.control_room import ControlRoomHandler
from onbot.app import run_reconcile_once
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.rooms.admin import AdminRoomProvisioner
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


async def _messages_in(client: ApiClientMatrix, room_id: str) -> list[str]:
    resp = await client.get_json(f"v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 100})
    return [
        (ev.get("content") or {}).get("body", "")
        for ev in resp.get("chunk", [])
        if ev.get("type") == "m.room.message" and ev.get("sender") == S.BOT_USER_ID
    ]


async def _pump_once(handler: ControlRoomHandler, client: ApiClientMatrix) -> None:
    """Feed the handler one real sync slice, exactly as the SyncPump would."""
    await handler.handle_sync(await client.sliding_sync(None))


def _resolver(authentik: ApiClientAuthentik, config: OnbotConfig, *, ttl_sec: float = 0) -> AdminResolver:
    """A resolver that re-reads Authentik on every command, so tests need not wait out a TTL."""
    return AdminResolver(authentik, config, ttl_sec=ttl_sec)


async def test_only_an_allowlisted_admin_can_announce(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    # A user whose notice board is the broadcast target, plus an admin and a stranger.
    _u, target = S.provision(authentik_admin, S.uniq("target"))
    _a, admin = S.provision(authentik_admin, S.uniq("botadmin"))
    _s, stranger = S.provision(authentik_admin, S.uniq("stranger"))

    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]

    # Provision the target's notice board (welcome fires on the reconciler's user-synced signal).
    await run_reconcile_once(config)
    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    notice_board = direct[target.mxid][0]

    resolver = _resolver(authentik_client, config)
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver).ensure()
    assert control_room is not None

    # Both humans join. The stranger is in the room and is *not* on the allowlist — the whole point.
    await admin_client.add_user_to_room(control_room, admin.mxid)
    await admin_client.add_user_to_room(control_room, stranger.mxid)

    handler = ControlRoomHandler(matrix_client, config, BroadcastService(matrix_client, config), resolver)
    await handler.start(control_room)

    # The room's power levels let any member speak; Synapse accepts the stranger's command.
    assert S.send_message_status(stranger.access_token, control_room, "!announce you are all fired") == 200
    assert S.send_message_status(admin.access_token, control_room, "!announce Maintenance at 22:00") == 200

    await _pump_once(handler, matrix_client)

    delivered = await _messages_in(matrix_client, notice_board)
    assert "Maintenance at 22:00" in delivered
    assert "you are all fired" not in delivered  # in the room, refused by the allowlist

    replies = await _messages_in(matrix_client, control_room)
    assert any("not on the bot's admin allowlist" in r for r in replies)
    assert any("sent to" in r and "failed" in r for r in replies)


async def test_a_replayed_sync_timeline_does_not_re_announce(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    # Sliding sync from pos=None replays the timeline, which on restart would otherwise re-page
    # every user with the last announcement.
    _u, target = S.provision(authentik_admin, S.uniq("replaytarget"))
    _a, admin = S.provision(authentik_admin, S.uniq("replayadmin"))

    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]

    await run_reconcile_once(config)
    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    notice_board = direct[target.mxid][0]

    resolver = _resolver(authentik_client, config)
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver).ensure()
    assert control_room is not None
    await admin_client.add_user_to_room(control_room, admin.mxid)

    handler = ControlRoomHandler(matrix_client, config, BroadcastService(matrix_client, config), resolver)
    await handler.start(control_room)
    assert S.send_message_status(admin.access_token, control_room, "!announce Only once please") == 200

    # The same slice, twice — a restart re-reading the same timeline.
    await _pump_once(handler, matrix_client)
    await _pump_once(handler, matrix_client)

    delivered = await _messages_in(matrix_client, notice_board)
    assert delivered.count("Only once please") == 1


async def test_an_authentik_group_grants_commands_and_leaving_it_revokes_them(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
) -> None:
    # The hand-maintained list is at least honest about needing a deploy to revoke. A group
    # membership that *looks* revocable and is not would be worse, so prove it against real
    # Authentik: in the group, `!announce` lands; out of the group, the same running handler refuses.
    group = authentik_admin.create_group(S.uniq("bot-admins"))
    _u, target = S.provision(authentik_admin, S.uniq("grouptarget"))
    admin_user, admin = S.provision(authentik_admin, S.uniq("groupadmin"), groups=[group["pk"]])

    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = []  # the group is the only source
    config.admin_room.authentik_group_pks_granting_bot_admin = [group["pk"]]

    await run_reconcile_once(config)
    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    notice_board = direct[target.mxid][0]

    resolver = _resolver(authentik_client, config)
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver).ensure()
    assert control_room is not None
    await admin_client.add_user_to_room(control_room, admin.mxid)

    handler = ControlRoomHandler(matrix_client, config, BroadcastService(matrix_client, config), resolver)
    await handler.start(control_room)

    assert S.send_message_status(admin.access_token, control_room, "!announce Granted by group") == 200
    await _pump_once(handler, matrix_client)
    assert "Granted by group" in await _messages_in(matrix_client, notice_board)

    # Removed from the group, and never restarted: the next command is refused after a refresh.
    authentik_admin.remove_user_from_group(group["pk"], admin_user["pk"])

    assert S.send_message_status(admin.access_token, control_room, "!announce Revoked") == 200
    await _pump_once(handler, matrix_client)

    assert "Revoked" not in await _messages_in(matrix_client, notice_board)
    replies = await _messages_in(matrix_client, control_room)
    assert any("not on the bot's admin allowlist" in r for r in replies)
