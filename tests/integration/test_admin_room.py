"""End-to-end admin control room (ADR-0010) against the live stack.

The unit tests prove the allowlist is consulted. Only the live homeserver can prove the parts that
depend on Synapse's own rules: that a non-admin *can* speak in the control room (so the allowlist,
not the power level, is what actually stops them), that the announcement is really delivered into a
provisioned user's read-only notice board, and that the replayed sync timeline does not send it twice.

And only a live Authentik can prove that a group membership really grants the bot's commands, and —
the part that matters — that taking it away really takes them away, on a running bot.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest

from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastService
from onbot.admin.control_room import ControlRoomHandler
from onbot.admin.provisioner import AdminRoomProvisioner
from onbot.app import run_reconcile_once
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.events import Event, Signal
from onbot.rooms.identity import RoomIdentityResolver
from onbot.rooms.provision import markers_locked
from onbot.state import OnbotRoomType, event_type_name
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
    """Feed the handler one real sync slice, exactly as the SyncPump would.

    A ``!announce`` runs beside the stream (ONB-03); wait for it, so its result is in the room.
    """
    await handler.handle_sync(await client.sliding_sync(None))
    await handler.wait_for_broadcast()


def _resolver(authentik: ApiClientAuthentik, config: OnbotConfig, *, ttl_sec: float = 0) -> AdminResolver:
    """A resolver that re-reads Authentik on every command, so tests need not wait out a TTL."""
    return AdminResolver(authentik, config, ttl_sec=ttl_sec)


async def test_only_an_allowlisted_admin_can_announce(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    room_identity: RoomIdentityResolver,
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
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver, room_identity).ensure()
    assert control_room is not None

    # Both humans join. The stranger is in the room and is *not* on the allowlist — the whole point.
    await admin_client.add_user_to_room(control_room, admin.mxid)
    await admin_client.add_user_to_room(control_room, stranger.mxid)

    handler = ControlRoomHandler(
        matrix_client,
        config,
        BroadcastService(matrix_client, config, recipients=S.recipients(target.mxid)),
        resolver,
    )
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
    room_identity: RoomIdentityResolver,
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
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver, room_identity).ensure()
    assert control_room is not None
    await admin_client.add_user_to_room(control_room, admin.mxid)

    handler = ControlRoomHandler(
        matrix_client,
        config,
        BroadcastService(matrix_client, config, recipients=S.recipients(target.mxid)),
        resolver,
    )
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
    room_identity: RoomIdentityResolver,
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
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver, room_identity).ensure()
    assert control_room is not None
    await admin_client.add_user_to_room(control_room, admin.mxid)

    handler = ControlRoomHandler(
        matrix_client,
        config,
        BroadcastService(matrix_client, config, recipients=S.recipients(target.mxid)),
        resolver,
    )
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


async def test_reconcile_once_provisions_the_admin_room_and_invites_new_admins(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    """A cron-only deployment never runs the service, so reconcile-once must keep the room going."""
    group = authentik_admin.create_group(S.uniq("cron-admins"))
    _f, first = S.provision(authentik_admin, S.uniq("cronadmin"), groups=[group["pk"]])

    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = []
    config.admin_room.authentik_group_pks_granting_bot_admin = [group["pk"]]

    alias = f"#{config.admin_room.alias}:{config.synapse_server.server_name}"
    assert await matrix_client.resolve_room_alias(alias) is None

    await run_reconcile_once(config)
    control_room = await matrix_client.resolve_room_alias(alias)
    assert control_room is not None
    assert await matrix_client.get_membership(control_room, first.mxid) == "invite"

    second_user, second = S.provision(authentik_admin, S.uniq("cronadmin"))
    authentik_admin.add_user_to_group(group["pk"], second_user["pk"])
    await run_reconcile_once(config)
    assert await matrix_client.get_membership(control_room, second.mxid) == "invite"


# --- S12: pacing (ONB-03), markdown (ONB-10), edits (ONB-11) ---------------------------------------


async def _bot_contents(client: ApiClientMatrix, room_id: str) -> list[dict[str, Any]]:
    resp = await client.get_json(f"v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 100})
    return [
        ev.get("content") or {}
        for ev in resp.get("chunk", [])
        if ev.get("type") == "m.room.message" and ev.get("sender") == S.BOT_USER_ID
    ]


class _BotRooms(BroadcastService):
    """Targets rooms the bot is alone in, so the pacing runs against Synapse with no users to set up."""

    def __init__(self, client: ApiClientMatrix, config: OnbotConfig, rooms: list[str]) -> None:
        super().__init__(client, config, recipients=S.recipients(S.BOT_USER_ID))
        self.rooms = rooms

    async def target_rooms(self) -> dict[str, str]:
        return dict.fromkeys(self.rooms, S.BOT_USER_ID)


async def test_a_throttled_broadcast_reaches_every_room_and_keeps_the_markdown(
    make_config, matrix_client: ApiClientMatrix, admin_client: ApiClientSynapseAdmin
) -> None:
    """ONB-03 against Synapse's real limiter: the bot limited to 1 message/s with a burst of 1.

    The stack lifts the limit globally (rc_message 1000/s), so the test narrows it for the bot alone
    with the same admin API the bot uses to lift it, and restores it afterwards.
    """
    config = make_config()
    rooms = [await matrix_client.create_room(name=S.uniq("paced")) for _ in range(12)]
    await admin_client.override_ratelimit(S.BOT_USER_ID, messages_per_second=1, burst_count=1)
    try:
        service = _BotRooms(matrix_client, config, rooms)
        service.rate_limit_lifted = False
        result = await service.broadcast("**Maintenance** tonight, see [status](https://status.example.org)")
    finally:
        await admin_client.override_ratelimit(S.BOT_USER_ID)  # 0/0: no limit, as the bot sets it

    assert result.failures == []
    assert sorted(result.sent) == sorted(rooms)
    assert result.rate_limited >= 1  # Synapse really did push back
    assert "send rate limit is not lifted" in result.summary()
    for room in rooms:
        contents = await _bot_contents(matrix_client, room)
        assert len(contents) == 1  # exactly once, despite the retries
        assert contents[0]["msgtype"] == "m.notice"
        assert contents[0]["body"].startswith("**Maintenance**")
        assert "<strong>Maintenance</strong>" in contents[0]["formatted_body"]


async def test_an_edited_announce_is_not_sent_again(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    room_identity: RoomIdentityResolver,
) -> None:
    """ONB-11: an edit without Element's ``* `` fallback prefix must not page everybody twice."""
    _u, target = S.provision(authentik_admin, S.uniq("edittarget"))
    _a, admin = S.provision(authentik_admin, S.uniq("editadmin"))

    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]

    await run_reconcile_once(config)
    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    notice_board = direct[target.mxid][0]

    resolver = _resolver(authentik_client, config)
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver, room_identity).ensure()
    assert control_room is not None
    await admin_client.add_user_to_room(control_room, admin.mxid)
    handler = ControlRoomHandler(
        matrix_client,
        config,
        BroadcastService(matrix_client, config, recipients=S.recipients(target.mxid)),
        resolver,
    )
    await handler.start(control_room)

    original = S.send_message(
        admin.access_token, control_room, {"msgtype": "m.text", "body": "!announce At 22:00"}
    )
    await _pump_once(handler, matrix_client)
    S.send_message(
        admin.access_token,
        control_room,
        {
            "msgtype": "m.text",
            "body": "!announce At 23:00",  # no "* " prefix: a client that does not follow Element
            "m.new_content": {"msgtype": "m.text", "body": "!announce At 23:00"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": original},
        },
    )
    await _pump_once(handler, matrix_client)

    delivered = await _messages_in(matrix_client, notice_board)
    assert "At 22:00" in delivered
    assert "At 23:00" not in delivered
    replies = await _messages_in(matrix_client, control_room)
    assert any("Edits are not commands" in r for r in replies)


async def test_an_admin_who_declines_or_leaves_is_not_invited_again(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    room_identity: RoomIdentityResolver,
) -> None:
    """ONB-12: a declined invitation used to come back on every reconcile tick."""
    _a, decliner = S.provision(authentik_admin, S.uniq("decliner"))
    _b, leaver = S.provision(authentik_admin, S.uniq("leaver"))

    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [decliner.mxid, leaver.mxid]

    provisioner = AdminRoomProvisioner(
        matrix_client, config, _resolver(authentik_client, config), room_identity
    )
    control_room = await provisioner.ensure()
    assert control_room is not None
    assert await matrix_client.get_membership(control_room, decliner.mxid) == "invite"

    S.leave_room(decliner.access_token, control_room)  # rejecting an invitation is a leave
    assert S.join_room_status(leaver.access_token, control_room) == 200
    S.leave_room(leaver.access_token, control_room)

    for _ in range(2):  # two reconcile ticks
        await provisioner.ensure_admins_invited()

    assert await matrix_client.get_membership(control_room, decliner.mxid) == "leave"
    assert await matrix_client.get_membership(control_room, leaver.mxid) == "leave"

    # A restarted bot reads the same record from the room.
    restarted = AdminRoomProvisioner(
        matrix_client, config, _resolver(authentik_client, config), room_identity
    )
    assert await restarted.ensure() == control_room
    assert await matrix_client.get_membership(control_room, decliner.mxid) == "leave"


# --- S18 (ONB-11): the same announcement twice within a minute asks first ---------------------------


async def test_a_repeated_announce_is_held_until_its_admin_confirms_it(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    room_identity: RoomIdentityResolver,
) -> None:
    _u, target = S.provision(authentik_admin, S.uniq("duptarget"))
    _a, admin = S.provision(authentik_admin, S.uniq("dupadmin"))
    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]
    await run_reconcile_once(config)
    notice_board = (await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct"))[target.mxid][0]

    resolver = _resolver(authentik_client, config)
    control_room = await AdminRoomProvisioner(matrix_client, config, resolver, room_identity).ensure()
    assert control_room is not None
    await admin_client.add_user_to_room(control_room, admin.mxid)
    handler = ControlRoomHandler(
        matrix_client,
        config,
        BroadcastService(matrix_client, config, recipients=S.recipients(target.mxid)),
        resolver,
    )
    await handler.start(control_room)

    assert S.send_message_status(admin.access_token, control_room, "!announce Pasted twice") == 200
    await _pump_once(handler, matrix_client)
    assert S.send_message_status(admin.access_token, control_room, "!announce  Pasted twice ") == 200
    await _pump_once(handler, matrix_client)

    assert (await _messages_in(matrix_client, notice_board)).count("Pasted twice") == 1
    held = [r for r in await _messages_in(matrix_client, control_room) if "was not sent" in r]
    assert len(held) == 1
    held_id = held[0].split("!confirm ", 1)[1].split()[0]

    assert S.send_message_status(admin.access_token, control_room, f"!confirm {held_id}") == 200
    await _pump_once(handler, matrix_client)
    assert (await _messages_in(matrix_client, notice_board)).count("Pasted twice") == 1 + 1


# --- W6R-08: the control room is repaired like the other managed rooms -------------------------------


async def test_the_control_rooms_marker_lock_and_alias_are_repaired_after_a_pass(
    make_config,
    authentik_admin: S.AuthentikAdmin,
    authentik_client: ApiClientAuthentik,
    matrix_client: ApiClientMatrix,
    room_identity: RoomIdentityResolver,
) -> None:
    """Before W6R-08 only the group rooms, lobbies and the space got their marker, its lock and their
    alias back; the control room kept whatever somebody at the bot's level had done to it."""
    _a, admin = S.provision(authentik_admin, S.uniq("repairadmin"))
    config = make_config()
    config.admin_room.enabled = True
    config.admin_room.alias = S.uniq("onbot-admin")
    config.admin_room.admin_user_ids = [admin.mxid]
    provisioner = AdminRoomProvisioner(
        matrix_client, config, _resolver(authentik_client, config), room_identity
    )
    control_room = await provisioner.ensure()
    assert control_room is not None
    marker_type = event_type_name(S.SERVER_NAME, OnbotRoomType.admin_room)
    before = await matrix_client.get_room_state_event(control_room, marker_type)
    assert before and before.get("invited_admins") == [admin.mxid]

    # What somebody at the bot's own level could do: remove the marker, lower its lock, drop the alias.
    await matrix_client.put_room_state_event(control_room, marker_type, {})
    power = await matrix_client.get_room_power_levels(control_room)
    power["events"] = {**power["events"], marker_type: 50}
    await matrix_client.set_room_power_levels(control_room, power)
    alias = f"#{config.admin_room.alias}:{S.SERVER_NAME}"
    await matrix_client.delete_json(f"v3/directory/room/{quote(alias, safe='')}")
    assert await matrix_client.resolve_room_alias(alias) is None

    await provisioner.on_reconcile(Event(signal=Signal.reconcile_completed, payload={}))

    marker = await matrix_client.get_room_state_event_with_sender(control_room, marker_type)
    assert marker is not None and marker["sender"] == S.BOT_USER_ID
    assert marker["content"] == before  # restored from what the bot knows, invitations included
    power = await matrix_client.get_room_power_levels(control_room)
    create = await matrix_client.get_room_state_event(control_room, "m.room.create") or {}
    assert markers_locked(power, S.SERVER_NAME, S.BOT_USER_ID, room_version=create.get("room_version"))
    assert await matrix_client.resolve_room_alias(alias) == control_room
    reports = await _messages_in(matrix_client, control_room)
    assert any("marker was removed" in r for r in reports), reports
    assert any("had been lowered" in r for r in reports), reports
