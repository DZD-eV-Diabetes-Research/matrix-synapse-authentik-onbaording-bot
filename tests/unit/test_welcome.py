"""Unit tests for the welcome flow's idempotency layers (one DM per user, each message once, one
force-join ever) and for the read-only notice board the DM has become."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from typing import Any

import pytest

from onbot.clients.base import ApiError
from onbot.config import (
    AdminRoom,
    AuthentikServer,
    CreateMatrixRoomsInAMatrixSpace,
    OnbotConfig,
    SynapseServer,
    WelcomeMessage,
)
from onbot.onboarding.notice_board import notice_board_power_levels
from onbot.onboarding.welcome import NoticeBoardUnreadable, WelcomeService, welcome_txn_id
from onbot.reconciler.state import OnbotRoomType
from onbot.rooms.identity import RoomIdentity, RoomKey

BOT = "@bot:matrix.test"
ALICE = "@alice:matrix.test"
BOB = "@bob:matrix.test"


class FakeSynapseAdmin:
    """Stand-in for ApiClientSynapseAdmin's force-join, optionally refusing with a given status."""

    def __init__(self, *, fail_with: int | None = None) -> None:
        self.joined: list[tuple[str, str]] = []
        self.fail_with = fail_with
        self.accounts: dict[str, dict[str, Any]] = {}
        # Called after a successful join, to stand in for the membership event the real force-join
        # puts on the bot's sync stream.
        self.on_join: Callable[[], None] | None = None

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self.accounts.get(user_id)

    async def add_user_to_room(self, room_id: str, user_id: str) -> None:
        if self.fail_with is not None:
            raise ApiError("POST", f"/join/{room_id}", self.fail_with)
        self.joined.append((room_id, user_id))
        if self.on_join is not None:
            self.on_join()


class FakeMatrixClient:
    """In-memory stand-in for ApiClientMatrix: tracks account data, room state and sent messages."""

    def __init__(self) -> None:
        self.account_data: dict[tuple[str, str], dict[str, Any]] = {}
        self.room_state: dict[tuple[str, str], dict[str, Any]] = {}
        self.sent: list[tuple[str, str]] = []
        self.formatted: list[str | None] = []
        self.created_dms = 0
        self.aliases: dict[str, str] = {}
        self.space_links: list[tuple[str, str]] = []
        self.power_levels: dict[str, dict[str, Any]] = {}
        self.power_level_writes: list[str] = []
        self.avatars: dict[str, str] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.txn_ids: list[str | None] = []
        # (room, user) -> membership; a user is joined unless a test says otherwise.
        self.memberships: dict[tuple[str, str], str | None] = {}
        self.invites: list[tuple[str, str]] = []
        # Rooms the bot has left (purged or kicked): reading their state answers 403.
        self.gone: set[str] = set()

    async def get_membership(self, room_id: str, user_id: str) -> str | None:
        return self.memberships.get((room_id, user_id), "join")

    async def invite_user(self, room_id: str, user_id: str) -> None:
        self.invites.append((room_id, user_id))
        self.memberships[(room_id, user_id)] = "invite"

    async def joined_rooms(self) -> list[str]:
        rooms = {room for room, _ in self.room_state} | set(self.power_levels)
        return sorted(rooms - self.gone)

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        if room_id in self.gone:
            raise ApiError("GET", f"/rooms/{room_id}/state", 403, {"errcode": "M_FORBIDDEN"})
        return dict(self.power_levels.get(room_id, {}))

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        self.power_levels[room_id] = dict(power_levels)
        self.power_level_writes.append(room_id)

    async def set_room_avatar(self, room_id: str, mxc_uri: str) -> None:
        self.avatars[room_id] = mxc_uri

    async def resolve_room_alias(self, alias: str) -> str | None:
        return self.aliases.get(alias)

    async def link_room_to_space(self, space_id: str, room_id: str, *, suggested: bool = False) -> None:
        self.space_links.append((space_id, room_id))

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        return dict(self.account_data.get((user_id, data_type), {}))

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        self.account_data[(user_id, data_type)] = dict(content)

    server_name = "matrix.test"

    async def create_room(self, **kwargs: Any) -> str:
        self.created_dms += 1
        self.create_calls.append(kwargs)
        room_id = f"!dm-{self.created_dms}:matrix.test"
        params = kwargs.get("room_params") or {}
        self.power_levels[room_id] = dict(params.get("power_level_content_override") or {})
        self.room_state[(room_id, "m.room.name")] = {"name": kwargs.get("name")}
        self.room_state[(room_id, "m.room.topic")] = {"topic": kwargs.get("topic")}
        for event in params.get("initial_state") or []:
            self.room_state[(room_id, event["type"])] = dict(event["content"])
        return room_id

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        if room_id in self.gone:
            raise ApiError("GET", f"/rooms/{room_id}/state", 403, {"errcode": "M_FORBIDDEN"})
        return self.room_state.get((room_id, event_type))

    async def put_room_state_event(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None:
        self.room_state[(room_id, event_type)] = dict(content)

    async def send_text_message(
        self, room_id: str, body: str, *, formatted_body: str | None = None, txn_id: str | None = None
    ) -> str:
        # Yield to the loop, as a real HTTP round-trip does: any concurrent welcome for the same user
        # gets to run here, which is exactly where a duplicate would slip in.
        await asyncio.sleep(0)
        self.sent.append((room_id, body))
        self.formatted.append(formatted_body)
        self.txn_ids.append(txn_id)
        return f"$e{len(self.sent)}"


def _config(
    messages: list[str | WelcomeMessage] | None,
    *,
    place_in_space: bool = False,
    force_join: bool = True,
    space_enabled: bool = True,
) -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id=BOT,
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        admin_room=AdminRoom(enabled=False),
        welcome_new_users_messages=messages,
        place_onboarding_rooms_in_space=place_in_space,
        force_join_onboarding_room=force_join,
        create_matrix_rooms_in_a_matrix_space=CreateMatrixRoomsInAMatrixSpace(enabled=space_enabled),
    )


def _direct_state(client: FakeMatrixClient, room_id: str) -> dict[str, Any]:
    return client.room_state[(room_id, "test.matrix.onbot.direct_room")]


async def test_welcome_creates_dm_and_sends_all_messages() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi", "rules"]))  # type: ignore[arg-type]

    await svc.welcome_user("@alice:matrix.test")

    assert client.created_dms == 1
    assert [body for _, body in client.sent] == ["hi", "rules"]
    # m.direct now maps the user to their DM room.
    direct = client.account_data[("@bot:matrix.test", "m.direct")]
    assert direct["@alice:matrix.test"] == ["!dm-1:matrix.test"]


async def test_welcome_is_idempotent_across_calls() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi", "rules"]))  # type: ignore[arg-type]

    await svc.welcome_user("@alice:matrix.test")
    await svc.welcome_user("@alice:matrix.test")

    assert client.created_dms == 1  # no second DM
    assert len(client.sent) == 2  # messages not re-sent


async def test_force_join_event_racing_the_first_welcome_does_not_duplicate_messages() -> None:
    """The force-join puts a join event on the bot's own sync stream, and the listener welcomes on
    join events. That second call arrives while the first is still sending, and must send nothing."""
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin()
    svc = WelcomeService(client, _config(["hi", "rules"]), admin=admin)  # type: ignore[arg-type]

    racing: list[asyncio.Task[None]] = []
    admin.on_join = lambda: racing.append(asyncio.create_task(svc.welcome_user(ALICE)))

    await svc.welcome_user(ALICE)
    await asyncio.gather(*racing)

    assert [body for _, body in client.sent] == ["hi", "rules"]
    assert client.created_dms == 1


async def test_a_message_is_recorded_before_the_next_one_is_sent() -> None:
    """Each message is persisted as it lands, so a crash mid-welcome resumes rather than replays."""
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi", "rules"]))  # type: ignore[arg-type]

    sent_when_second_message_goes_out: list[int] = []
    original = client.send_text_message

    async def _spy(
        room_id: str, body: str, *, formatted_body: str | None = None, txn_id: str | None = None
    ) -> str:
        if body == "rules":
            sent_when_second_message_goes_out.append(
                len(_direct_state(client, room_id)["welcome_messages_sent"])
            )
        return await original(room_id, body, formatted_body=formatted_body, txn_id=txn_id)

    client.send_text_message = _spy  # type: ignore[method-assign]
    await svc.welcome_user(ALICE)

    assert sent_when_second_message_goes_out == [1]  # "hi" was already durably recorded


async def test_welcome_sends_only_new_messages_when_list_grows() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]
    await svc.welcome_user("@alice:matrix.test")

    # Operator adds a second message later; only the new one should go out.
    svc.config.welcome_new_users_messages = ["hi", "new-info"]
    await svc.welcome_user("@alice:matrix.test")

    assert [body for _, body in client.sent] == ["hi", "new-info"]


async def test_welcome_noop_without_messages() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(None))  # type: ignore[arg-type]
    await svc.welcome_user("@alice:matrix.test")
    assert client.created_dms == 0
    assert client.sent == []


async def test_welcome_places_new_dm_in_space_when_enabled() -> None:
    client = FakeMatrixClient()
    # The managed space resolves; the freshly created DM should be linked into it (G4.5).
    client.aliases["#OnBotSpace:matrix.test"] = "!space:matrix.test"
    svc = WelcomeService(client, _config(["hi"], place_in_space=True))  # type: ignore[arg-type]

    await svc.welcome_user("@alice:matrix.test")
    await svc.welcome_user("@alice:matrix.test")  # idempotent: DM reused, not re-linked

    assert client.space_links == [("!space:matrix.test", "!dm-1:matrix.test")]


async def test_welcome_does_not_place_dm_in_space_by_default() -> None:
    client = FakeMatrixClient()
    client.aliases["#OnBotSpace:matrix.test"] = "!space:matrix.test"
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    assert client.space_links == []


async def test_new_room_is_created_as_a_read_only_notice_board() -> None:
    client = FakeMatrixClient()
    config = _config(["hi"])
    svc = WelcomeService(client, config)  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)

    assert client.power_levels["!dm-1:matrix.test"] == notice_board_power_levels()
    # Force-joining skips the invite that would have tagged the room as a DM, so it needs a name.
    assert client.room_state[("!dm-1:matrix.test", "m.room.name")] == {"name": config.onboarding_room_name}


async def test_user_is_force_joined_once_and_it_is_recorded() -> None:
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin()
    svc = WelcomeService(client, _config(["hi"]), admin=admin)  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)

    assert admin.joined == [("!dm-1:matrix.test", ALICE)]
    recorded = _direct_state(client, "!dm-1:matrix.test")["force_joined_at"]
    # Whole seconds. Matrix's canonical JSON has no floats: Synapse answers M_BAD_JSON to a state
    # event carrying one, which no fake homeserver here would have caught.
    assert isinstance(recorded, int)


async def test_a_user_who_left_the_room_is_not_dragged_back_in() -> None:
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin()
    svc = WelcomeService(client, _config(["hi"]), admin=admin)  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)
    await svc.welcome_user(ALICE)  # a later reconcile tick, after Alice left the notice board

    assert len(admin.joined) == 1


@pytest.mark.parametrize("status", [403, 404])
async def test_force_join_failure_degrades_to_the_standing_invite(status: int) -> None:
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin(fail_with=status)
    svc = WelcomeService(client, _config(["hi"]), admin=admin)  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)  # must not raise: the invite is the fallback

    # Nothing recorded, so a later tick retries the join rather than assuming it happened.
    assert _direct_state(client, "!dm-1:matrix.test")["force_joined_at"] is None
    assert [body for _, body in client.sent] == ["hi"]


async def test_an_unexpected_force_join_error_is_not_swallowed() -> None:
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin(fail_with=500)
    svc = WelcomeService(client, _config(["hi"]), admin=admin)  # type: ignore[arg-type]

    with pytest.raises(ApiError):
        await svc.welcome_user(ALICE)


async def test_force_join_can_be_turned_off() -> None:
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin()
    svc = WelcomeService(client, _config(["hi"], force_join=False), admin=admin)  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)

    assert admin.joined == []
    assert _direct_state(client, "!dm-1:matrix.test")["force_joined_at"] is None


async def test_hand_edited_power_levels_are_restored_on_the_next_welcome() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    client.power_levels["!dm-1:matrix.test"]["events_default"] = 0  # somebody re-opened the composer

    await svc.welcome_user(ALICE)

    assert client.power_levels["!dm-1:matrix.test"] == notice_board_power_levels()
    assert client.power_level_writes == ["!dm-1:matrix.test"]


async def test_a_markdown_message_is_sent_with_its_rendered_html() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["read the [guide](https://element.io/user-guide)"]))  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)

    # body keeps the markdown source, for clients that render no HTML.
    assert client.sent == [("!dm-1:matrix.test", "read the [guide](https://element.io/user-guide)")]
    assert client.formatted == ['<p>read the <a href="https://element.io/user-guide">guide</a></p>']


async def test_a_plain_message_is_sent_without_a_formatted_body() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    assert client.formatted == [None]


async def test_space_and_user_placeholders_are_filled_in() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["{user_id}: browse [{space_name}]({space_link})"]))  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)

    body = client.sent[0][1]
    assert body == "@alice:matrix.test: browse [OnBotSpace](https://matrix.to/#/%23OnBotSpace:matrix.test)"
    assert client.formatted[0] == (
        '<p>@alice:matrix.test: browse <a href="https://matrix.to/#/%23OnBotSpace:matrix.test">'
        "OnBotSpace</a></p>"
    )


async def test_the_placeholder_key_is_the_configured_text_not_the_filled_in_one() -> None:
    """Otherwise every user would look like a different message, and a rename would re-send it."""
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["join {space_link}"]))  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)
    await svc.welcome_user(ALICE)

    assert len(client.sent) == 1


async def test_a_message_needing_a_space_is_held_back_when_there_is_none() -> None:
    client = FakeMatrixClient()
    config = _config(["hi", "join {space_link}"], space_enabled=False)
    svc = WelcomeService(client, config)  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)

    # Sent with a hole in it would be worse than not sent; nothing is recorded for it either, so
    # enabling the space later still gets it delivered.
    assert [body for _, body in client.sent] == ["hi"]
    assert len(_direct_state(client, "!dm-1:matrix.test")["welcome_messages_sent"]) == 1

    config.create_matrix_rooms_in_a_matrix_space.enabled = True
    svc = WelcomeService(client, config)  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)

    assert client.sent[-1][1] == "join https://matrix.to/#/%23OnBotSpace:matrix.test"


async def test_undrifted_power_levels_are_not_rewritten() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    await svc.welcome_user(ALICE)

    assert client.power_level_writes == []


# --- ONB-02: a setup interrupted after createRoom is finished later ---


async def test_the_notice_board_is_marked_at_creation_as_not_set_up_yet() -> None:
    """The marker travels with createRoom (initial_state), so no window leaves the room unmarked."""
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)

    (call,) = client.create_calls
    marker = call["room_params"]["initial_state"][-1]
    assert marker["type"] == "test.matrix.onbot.direct_room"
    assert marker["content"]["user_id"] == ALICE
    assert marker["content"]["setup_complete"] is False
    assert call["is_direct"] is True and call["invite"] == [ALICE]
    # ...and once set up, the room says so.
    assert _direct_state(client, "!dm-1:matrix.test")["setup_complete"] is True


async def test_a_failed_space_link_is_retried_with_the_force_join_and_the_messages() -> None:
    """Before the fix the second call took the "existing room" branch and never joined the user."""
    client = FakeMatrixClient()
    client.aliases["#OnBotSpace:matrix.test"] = "!space:matrix.test"
    admin = FakeSynapseAdmin()
    svc = WelcomeService(client, _config(["hi"], place_in_space=True), admin=admin)  # type: ignore[arg-type]
    link = client.link_room_to_space

    async def broken_link(space_id: str, room_id: str, *, suggested: bool = False) -> None:
        raise ApiError("PUT", "/state/m.space.child", 502)

    client.link_room_to_space = broken_link  # type: ignore[method-assign]
    with pytest.raises(ApiError):
        await svc.welcome_user(ALICE)
    assert admin.joined == [] and client.sent == []
    assert _direct_state(client, "!dm-1:matrix.test")["setup_complete"] is False

    client.link_room_to_space = link  # type: ignore[method-assign]
    await svc.welcome_user(ALICE)

    assert client.created_dms == 1
    assert client.space_links == [("!space:matrix.test", "!dm-1:matrix.test")]
    assert admin.joined == [("!dm-1:matrix.test", ALICE)]
    assert [body for _, body in client.sent] == ["hi"]
    assert _direct_state(client, "!dm-1:matrix.test")["setup_complete"] is True


async def test_a_notice_board_of_an_older_version_is_not_set_up_again() -> None:
    """A marker without `setup_complete` predates the field: its room is not force-joined again."""
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin()
    client.account_data[(BOT, "m.direct")] = {ALICE: ["!old:matrix.test"]}
    client.room_state[("!old:matrix.test", "test.matrix.onbot.direct_room")] = {"user_id": ALICE}
    svc = WelcomeService(client, _config(["hi"]), admin=admin)  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)

    assert admin.joined == []
    assert [body for _, body in client.sent] == ["hi"]


class _SpaceIdentity:
    """Stand-in for the room identity resolver: which room is the bot's space."""

    def __init__(self, space_id: str | None, trusted: frozenset[str] = frozenset()) -> None:
        self.space_id = space_id
        self.trusted = trusted

    async def identify(self, room_id: str) -> RoomIdentity | None:
        if room_id not in self.trusted:
            return None
        return RoomIdentity(room_id=room_id, key=RoomKey(OnbotRoomType.space, name="OnBotSpace"), locked=True)


async def test_the_dm_goes_into_the_space_the_reconciler_found() -> None:
    client = FakeMatrixClient()
    rooms = _SpaceIdentity("!real-space:matrix.test")
    svc = WelcomeService(client, _config(["hi"], place_in_space=True), rooms=rooms)  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    assert client.space_links == [("!real-space:matrix.test", "!dm-1:matrix.test")]


async def test_the_space_alias_is_used_once_it_is_shown_to_be_the_bots_space() -> None:
    client = FakeMatrixClient()
    client.aliases["#OnBotSpace:matrix.test"] = "!space:matrix.test"
    rooms = _SpaceIdentity(None, frozenset({"!space:matrix.test"}))
    svc = WelcomeService(client, _config(["hi"], place_in_space=True), rooms=rooms)  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    assert client.space_links == [("!space:matrix.test", "!dm-1:matrix.test")]


async def test_a_squatted_space_alias_does_not_get_the_dms() -> None:
    """D20: the alias alone does not make a room the bot's space."""
    client = FakeMatrixClient()
    client.aliases["#OnBotSpace:matrix.test"] = "!squat:matrix.test"
    rooms = _SpaceIdentity(None)
    svc = WelcomeService(client, _config(["hi"], place_in_space=True), rooms=rooms)  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    assert client.space_links == []
    assert [body for _, body in client.sent] == ["hi"]  # the welcome itself still goes out


# --- RUN-01 / ONB-01: concurrent welcomes of different users share one m.direct document ---


class YieldingMatrixClient(FakeMatrixClient):
    """Yields on every account-data and createRoom round trip, as the real HTTP client does."""

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        content = await super().get_account_data(user_id, data_type)
        await asyncio.sleep(0)
        return content

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        await super().set_account_data(user_id, data_type, content)

    async def create_room(self, **kwargs: Any) -> str:
        await asyncio.sleep(0)
        return await super().create_room(**kwargs)


async def test_concurrent_first_welcomes_of_different_users_keep_every_m_direct_entry() -> None:
    """Before the fix each welcome read m.direct, created its room and wrote the whole document back,
    so the last writer dropped the others' entries (live check 12 lost 9 of 20)."""
    client = YieldingMatrixClient()
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]
    users = [f"@user{i}:matrix.test" for i in range(20)]

    await asyncio.gather(*(svc.welcome_user(mxid) for mxid in users))

    direct = client.account_data[(BOT, "m.direct")]
    assert sorted(direct) == sorted(users)
    assert all(len(rooms) == 1 for rooms in direct.values())
    assert client.created_dms == 20

    # A second round (a restart) creates nothing and sends nothing new.
    await asyncio.gather(*(svc.welcome_user(mxid) for mxid in users))
    assert client.created_dms == 20
    assert len(client.sent) == 20


# --- ONB-09: a repeated send is deduplicated by its transaction id ---


async def test_each_welcome_message_has_a_transaction_id_stable_across_attempts() -> None:
    """A crash between the send and the record repeats the send; the same txn id lets Synapse drop it."""
    client = FakeMatrixClient()
    await WelcomeService(client, _config(["hi", "rules"])).welcome_user(ALICE)  # type: ignore[arg-type]
    first = list(client.txn_ids)

    # The record of both sends is lost (the process died before persisting): the retry repeats them.
    _direct_state(client, "!dm-1:matrix.test")["welcome_messages_sent"] = {}
    await WelcomeService(client, _config(["hi", "rules"])).welcome_user(ALICE)  # type: ignore[arg-type]

    assert client.txn_ids[2:] == first
    assert all(t and t.startswith("onbot-welcome-") for t in first)
    assert len(set(first)) == 2


def test_transaction_ids_differ_per_room_and_message() -> None:
    assert welcome_txn_id("!a:x", "k1") != welcome_txn_id("!b:x", "k1")
    assert welcome_txn_id("!a:x", "k1") != welcome_txn_id("!a:x", "k2")
    assert welcome_txn_id("!a:x", "k1") == welcome_txn_id("!a:x", "k1")


# --- ONB-18: message identity ---


async def test_whitespace_at_line_ends_and_around_the_text_does_not_resend() -> None:
    """Switching a YAML block from `|` to `|-`, or a stray trailing space, changes nothing visible."""
    client = FakeMatrixClient()
    config = _config(["hello\nworld\n"])
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]

    config.welcome_new_users_messages = ["hello  \nworld"]
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]

    assert len(client.sent) == 1


async def test_a_message_recorded_by_its_raw_text_hash_is_not_resent() -> None:
    """Records written before normalisation hold the hash of the raw text; they still count."""
    client = FakeMatrixClient()
    raw = "hello  \n"
    client.account_data[(BOT, "m.direct")] = {ALICE: ["!old:matrix.test"]}
    client.room_state[("!old:matrix.test", "test.matrix.onbot.direct_room")] = {
        "user_id": ALICE,
        "welcome_messages_sent": {hashlib.sha256(raw.encode()).hexdigest()[:16]: "2026-01-01T00:00:00+00:00"},
    }
    await WelcomeService(client, _config([raw])).welcome_user(ALICE)  # type: ignore[arg-type]

    assert client.sent == []


async def test_a_message_with_an_id_can_be_corrected_without_resending() -> None:
    client = FakeMatrixClient()
    config = _config([WelcomeMessage(id="intro", text="Welcome aboadr")])
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]

    config.welcome_new_users_messages = [WelcomeMessage(id="intro", text="Welcome aboard")]
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]

    assert [body for _, body in client.sent] == ["Welcome aboadr"]
    assert "id:intro" in _direct_state(client, "!dm-1:matrix.test")["welcome_messages_sent"]


async def test_giving_a_sent_message_an_id_sends_nothing_now_or_after_the_next_edit() -> None:
    client = FakeMatrixClient()
    config = _config(["Welcome aboadr"])
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]

    config.welcome_new_users_messages = [WelcomeMessage(id="intro", text="Welcome aboadr")]
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]
    # The id is recorded now, so the text can change afterwards.
    config.welcome_new_users_messages = [WelcomeMessage(id="intro", text="Welcome aboard")]
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]

    assert [body for _, body in client.sent] == ["Welcome aboadr"]


async def test_a_new_id_sends_the_message_again() -> None:
    client = FakeMatrixClient()
    config = _config([WelcomeMessage(id="intro", text="hi")])
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]

    config.welcome_new_users_messages = [WelcomeMessage(id="intro-2", text="hi, again")]
    await WelcomeService(client, config).welcome_user(ALICE)  # type: ignore[arg-type]

    assert [body for _, body in client.sent] == ["hi", "hi, again"]


# --- SEC-06 / ONB-07: notice boards of accounts that are gone leave m.direct ---


async def test_prune_drops_foreign_and_deactivated_accounts_and_keeps_the_rest() -> None:
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin()
    admin.accounts = {
        "@erased:matrix.test": {"name": "@erased:matrix.test", "deactivated": True},
        "@locked:matrix.test": {"name": "@locked:matrix.test", "deactivated": False, "locked": True},
    }
    client.account_data[(BOT, "m.direct")] = {
        ALICE: ["!a"],
        "@guest:partner.org": ["!g"],
        "@erased:matrix.test": ["!e"],
        "@locked:matrix.test": ["!l"],
        "@unknown:matrix.test": ["!u"],
    }
    svc = WelcomeService(client, _config(["hi"]), admin=admin)  # type: ignore[arg-type]

    gone = await svc.prune_direct({ALICE})

    assert gone == ["@erased:matrix.test", "@guest:partner.org"]
    assert sorted(client.account_data[(BOT, "m.direct")]) == [
        ALICE,
        "@locked:matrix.test",  # MAS-locked users are not listed; they keep their room for their return
        "@unknown:matrix.test",  # no evidence that it is gone
    ]


async def test_prune_writes_nothing_when_nothing_is_gone() -> None:
    client = FakeMatrixClient()
    client.account_data[(BOT, "m.direct")] = {ALICE: ["!a"]}
    writes: list[object] = []
    original = client.set_account_data

    async def spy(user_id: str, data_type: str, content: dict[str, Any]) -> None:
        writes.append(content)
        await original(user_id, data_type, content)

    client.set_account_data = spy  # type: ignore[method-assign]
    svc = WelcomeService(client, _config(["hi"]), admin=FakeSynapseAdmin())  # type: ignore[arg-type]

    assert await svc.prune_direct({ALICE}) == []
    assert writes == []


# --- ONB-07: a user who left their notice board is invited back once ---


async def test_a_user_who_left_is_invited_back_once_and_not_force_joined() -> None:
    client = FakeMatrixClient()
    admin = FakeSynapseAdmin()
    svc = WelcomeService(client, _config(["hi"]), admin=admin)  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    client.memberships[("!dm-1:matrix.test", ALICE)] = "leave"

    await svc.welcome_user(ALICE)  # after a restart, or the listener saw the leave
    assert client.invites == [("!dm-1:matrix.test", ALICE)]
    assert isinstance(_direct_state(client, "!dm-1:matrix.test")["reinvited_at"], int)

    client.memberships[("!dm-1:matrix.test", ALICE)] = "leave"  # declined, or left again
    await svc.welcome_user(ALICE)
    assert client.invites == [("!dm-1:matrix.test", ALICE)]  # once only
    assert len(admin.joined) == 1  # never dragged back in


async def test_a_banned_or_joined_user_is_not_invited() -> None:
    client = FakeMatrixClient()
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]
    await svc.welcome_user(ALICE)
    await svc.welcome_user(ALICE)
    client.memberships[("!dm-1:matrix.test", ALICE)] = "ban"
    await svc.welcome_user(ALICE)

    assert client.invites == []


# --- ONB-08: a notice board the bot cannot read any more ---


async def test_a_notice_board_the_bot_left_is_replaced_by_a_new_one() -> None:
    """Before the fix the 403 failed the welcome on every pass, forever."""
    client = FakeMatrixClient()
    client.account_data[(BOT, "m.direct")] = {ALICE: ["!purged:matrix.test"], BOB: ["!b:matrix.test"]}
    client.gone.add("!purged:matrix.test")
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]

    await svc.welcome_user(ALICE)

    direct = client.account_data[(BOT, "m.direct")]
    assert direct[ALICE] == ["!dm-1:matrix.test"]
    assert direct[BOB] == ["!b:matrix.test"]
    assert client.sent == [("!dm-1:matrix.test", "hi")]


async def test_a_403_in_a_room_the_bot_is_still_in_is_not_taken_for_a_lost_room() -> None:
    client = FakeMatrixClient()
    client.account_data[(BOT, "m.direct")] = {ALICE: ["!dm:matrix.test"]}
    client.power_levels["!dm:matrix.test"] = {}

    async def refused(room_id: str) -> dict[str, Any]:
        raise ApiError("GET", "/state/m.room.power_levels", 403)

    client.get_room_power_levels = refused  # type: ignore[method-assign]
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]

    with pytest.raises(ApiError):
        await svc.welcome_user(ALICE)
    assert client.account_data[(BOT, "m.direct")] == {ALICE: ["!dm:matrix.test"]}
    assert client.created_dms == 0


async def test_unparsable_notice_board_state_is_reported_not_guessed() -> None:
    client = FakeMatrixClient()
    client.account_data[(BOT, "m.direct")] = {ALICE: ["!dm:matrix.test"]}
    client.room_state[("!dm:matrix.test", "test.matrix.onbot.direct_room")] = {
        "user_id": ALICE,
        "welcome_messages_sent": "hand-edited",
    }
    svc = WelcomeService(client, _config(["hi"]))  # type: ignore[arg-type]

    with pytest.raises(NoticeBoardUnreadable, match=re.escape("!dm:matrix.test")):
        await svc.welcome_user(ALICE)
    assert client.sent == []
