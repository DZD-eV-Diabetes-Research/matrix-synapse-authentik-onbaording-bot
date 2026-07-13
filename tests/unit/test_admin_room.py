"""Provisioning the control room: shape, invitations, and the idempotent pinned help."""

from __future__ import annotations

from typing import Any

from onbot.admin.admins import AdminResolver
from onbot.clients.base import ApiError
from onbot.config import AdminRoom, AuthentikServer, OnbotConfig, SynapseServer
from onbot.events import Event, Signal
from onbot.rooms.admin import PINNED_EVENTS_TYPE, AdminRoomProvisioner, admin_room_power_levels

BOT = "@bot:matrix.test"
ADMIN = "@admin:matrix.test"
ROOM = "!control:matrix.test"
ALIAS = "#onbot-admin:matrix.test"
GROUP = "group-pk-1"
# Custom onbot state events are namespaced with the reversed server name.
MARKER_TYPE = "test.matrix.onbot.admin_room"


def _config(**admin_room: Any) -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id=BOT,
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        admin_room=AdminRoom(**{"enabled": True, "admin_user_ids": [ADMIN], **admin_room}),
    )


class _FakeAuthentik:
    def __init__(self, *usernames: str) -> None:
        self.usernames = usernames

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"pk": name, "username": name} for name in self.usernames]


class _FakeClient:
    """Enough of ApiClientMatrix to provision a room: alias directory, state, membership, sends."""

    def __init__(self, *, existing_room: str | None = None) -> None:
        self.existing_room = existing_room
        self.state: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.invited: list[str] = []
        self.sends: list[str] = []
        self.memberships: dict[str, str] = {}
        self.topics: list[str] = []
        self._events = 0

    async def resolve_room_alias(self, alias: str) -> str | None:
        assert alias == ALIAS
        return self.existing_room

    async def create_room(self, **kwargs: Any) -> str:
        self.created.append(kwargs)
        return ROOM

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        return self.state.get((room_id, event_type, state_key))

    async def put_room_state_event(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None:
        self.state[(room_id, event_type, state_key)] = dict(content)

    async def get_membership(self, room_id: str, user_id: str) -> str | None:
        return self.memberships.get(user_id)

    async def invite_user(self, room_id: str, user_id: str) -> None:
        self.invited.append(user_id)

    async def set_room_topic(self, room_id: str, topic: str) -> None:
        self.topics.append(topic)
        self.state[(room_id, "m.room.topic", "")] = {"topic": topic}

    async def send_text_message(self, room_id: str, body: str, *, msgtype: str = "m.text") -> str:
        self.sends.append(body)
        self._events += 1
        return f"$help{self._events}"


def _provisioner(
    client: _FakeClient,
    config: OnbotConfig | None = None,
    authentik: _FakeAuthentik | None = None,
) -> AdminRoomProvisioner:
    config = config or _config()
    resolver = AdminResolver(authentik or _FakeAuthentik(), config)  # type: ignore[arg-type]
    return AdminRoomProvisioner(client, config, resolver)  # type: ignore[arg-type]


# --- power levels ----------------------------------------------------------


def test_members_may_speak_but_only_the_bot_governs() -> None:
    content = admin_room_power_levels()

    # The bot creates this room and is its creator: under room version 12 it must NOT be named in
    # m.room.power_levels (the auth rules reject it), and on older versions the server seats the
    # creator at 100 for us. Either way the override leaves `users` out.
    assert "users" not in content
    assert content["events_default"] == 0  # admins must be able to talk to the bot, and each other
    for key in ("state_default", "invite", "kick", "ban", "redact"):
        assert content[key] == 100


# --- creation --------------------------------------------------------------


async def test_a_missing_room_is_created_unencrypted_unfederated_and_invite_only() -> None:
    client = _FakeClient(existing_room=None)

    room_id = await _provisioner(client).ensure()

    assert room_id == ROOM
    (created,) = client.created
    assert created["encrypted"] is False  # ADR-0009: the bot has to be able to read this room
    assert "invite" not in created  # the room is created empty; the invites follow, one by one
    assert client.invited == [ADMIN]  # invited, never force-joined — admins are people
    params = created["room_params"]
    assert params["creation_content"] == {"m.federate": False}
    assert params["preset"] == "private_chat"
    assert params["power_level_content_override"] == admin_room_power_levels()


async def test_a_created_room_is_marked_as_bot_managed() -> None:
    client = _FakeClient(existing_room=None)
    provisioner = _provisioner(client)

    await provisioner.ensure()

    marker = client.state[(ROOM, MARKER_TYPE, "")]
    assert marker["room_type"] == "admin_room"


async def test_an_existing_room_is_found_by_alias_and_not_recreated() -> None:
    client = _FakeClient(existing_room=ROOM)

    assert await _provisioner(client).ensure() == ROOM
    assert client.created == []


async def test_the_feature_switch_provisions_nothing() -> None:
    client = _FakeClient(existing_room=None)

    assert await _provisioner(client, _config(enabled=False)).ensure() is None
    assert client.created == []


# --- invitations -----------------------------------------------------------


async def test_admins_already_in_the_room_are_not_re_invited() -> None:
    client = _FakeClient(existing_room=ROOM)
    client.memberships = {ADMIN: "join"}

    await _provisioner(client).ensure()

    assert client.invited == []


async def test_an_admin_added_to_the_config_later_gets_invited() -> None:
    client = _FakeClient(existing_room=ROOM)
    client.memberships = {ADMIN: "join"}
    newcomer = "@ops:matrix.test"

    await _provisioner(client, _config(admin_user_ids=[ADMIN, newcomer])).ensure()

    assert client.invited == [newcomer]


async def test_members_of_the_authentik_admin_group_are_invited_too() -> None:
    # A new group member finds the room waiting for them, without being named in config.yml.
    client = _FakeClient(existing_room=ROOM)
    config = _config(authentik_group_pks_granting_bot_admin=[GROUP])

    await _provisioner(client, config, _FakeAuthentik("alice")).ensure()

    assert client.invited == [ADMIN, "@alice:matrix.test"]


async def test_an_admin_added_to_the_group_after_startup_is_invited_on_the_next_reconcile() -> None:
    # The room is invite-only and `invite` sits at power level 100, which only the bot holds — so
    # nobody can let a new group member in by hand. Were the invite pass startup-only, they could
    # command the bot (the router re-resolves per command) with no way into the room to do it.
    client = _FakeClient(existing_room=ROOM)
    config = _config(authentik_group_pks_granting_bot_admin=[GROUP])
    authentik = _FakeAuthentik()  # the Authentik admin group is empty when the bot starts
    resolver = AdminResolver(authentik, config, ttl_sec=0)  # type: ignore[arg-type]
    provisioner = AdminRoomProvisioner(client, config, resolver)  # type: ignore[arg-type]

    await provisioner.ensure()
    assert client.invited == [ADMIN]

    authentik.usernames = ("alice",)  # ...and alice is added to it while the bot runs
    client.memberships[ADMIN] = "invite"

    await provisioner.on_reconcile(Event(signal=Signal.reconcile_completed, payload={}))

    # alice gets in on the next tick; the admin already invited is not invited (or notified) twice
    assert client.invited == [ADMIN, "@alice:matrix.test"]


async def test_the_invite_pass_is_a_no_op_before_the_room_is_bound() -> None:
    # A provisioner whose ensure() never ran must not try to invite anybody into a room it has not
    # resolved.
    client = _FakeClient(existing_room=ROOM)

    await _provisioner(client).ensure_admins_invited()

    assert client.invited == []


async def test_a_failed_invitation_does_not_abort_provisioning() -> None:
    # An admin from the Authentik group may not have logged in yet, so may have no Matrix account
    # for Synapse to invite. That must cost them an invitation, not everybody else a control room.
    client = _FakeClient(existing_room=None)

    async def _refuse(room_id: str, user_id: str) -> None:
        raise ApiError("POST", "/invite", 403, {"errcode": "M_FORBIDDEN"})

    client.invite_user = _refuse  # type: ignore[method-assign]

    assert await _provisioner(client).ensure() == ROOM
    assert client.sends  # the help message still went up


# --- topic + pinned help ---------------------------------------------------


async def test_the_topic_is_repaired_when_it_drifts_and_left_alone_when_it_matches() -> None:
    client = _FakeClient(existing_room=ROOM)
    config = _config()
    client.state[(ROOM, "m.room.topic", "")] = {"topic": "somebody changed this"}

    await _provisioner(client, config).ensure()
    assert client.topics == [config.admin_room.topic]

    client.topics.clear()
    await _provisioner(client, config).ensure()
    assert client.topics == []  # already correct; no state write


async def test_the_help_is_posted_and_pinned_once() -> None:
    client = _FakeClient(existing_room=ROOM)

    await _provisioner(client).ensure()

    assert len(client.sends) == 1
    assert "!announce <message>" in client.sends[0]
    assert client.state[(ROOM, PINNED_EVENTS_TYPE, "")] == {"pinned": ["$help1"]}


async def test_restarting_does_not_post_the_help_again() -> None:
    # Without the content hash every restart would leave another copy of the help in the room.
    client = _FakeClient(existing_room=ROOM)

    await _provisioner(client).ensure()
    await _provisioner(client).ensure()
    await _provisioner(client).ensure()

    assert len(client.sends) == 1


async def test_changed_help_text_replaces_the_old_pin_but_keeps_unrelated_ones() -> None:
    client = _FakeClient(existing_room=ROOM)
    client.state[(ROOM, PINNED_EVENTS_TYPE, "")] = {"pinned": ["$someone-elses-pin"]}
    await _provisioner(client).ensure()
    assert client.state[(ROOM, PINNED_EVENTS_TYPE, "")]["pinned"] == ["$someone-elses-pin", "$help1"]

    # Simulate the help text changing: the stored hash no longer matches.
    marker = client.state[(ROOM, MARKER_TYPE, "")]
    marker["help_text_hash"] = "stale"

    await _provisioner(client).ensure()

    pinned = client.state[(ROOM, PINNED_EVENTS_TYPE, "")]["pinned"]
    assert pinned == ["$someone-elses-pin", "$help2"]  # old help pin dropped, foreign pin kept
    assert len(client.sends) == 2
