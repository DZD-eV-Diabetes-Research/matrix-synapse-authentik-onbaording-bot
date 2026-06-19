"""Unit tests for the welcome flow's two idempotency layers (one DM per user, each message once)."""

from __future__ import annotations

from typing import Any

from onbot.config import AuthentikServer, OnbotConfig, SynapseServer
from onbot.onboarding.welcome import WelcomeService


class FakeMatrixClient:
    """In-memory stand-in for ApiClientMatrix: tracks account data, room state and sent messages."""

    def __init__(self) -> None:
        self.account_data: dict[tuple[str, str], dict[str, Any]] = {}
        self.room_state: dict[tuple[str, str], dict[str, Any]] = {}
        self.sent: list[tuple[str, str]] = []
        self.created_dms = 0
        self.aliases: dict[str, str] = {}
        self.space_links: list[tuple[str, str]] = []

    async def resolve_room_alias(self, alias: str) -> str | None:
        return self.aliases.get(alias)

    async def link_room_to_space(self, space_id: str, room_id: str, *, suggested: bool = False) -> None:
        self.space_links.append((space_id, room_id))

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        return dict(self.account_data.get((user_id, data_type), {}))

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        self.account_data[(user_id, data_type)] = dict(content)

    async def create_direct_message_room(self, user_id: str) -> str:
        self.created_dms += 1
        return f"!dm-{self.created_dms}:matrix.test"

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        return self.room_state.get((room_id, event_type))

    async def put_room_state_event(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None:
        self.room_state[(room_id, event_type)] = dict(content)

    async def send_text_message(self, room_id: str, body: str) -> str:
        self.sent.append((room_id, body))
        return f"$e{len(self.sent)}"


def _config(messages: list[str] | None, *, place_in_space: bool = False) -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id="@bot:matrix.test",
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        welcome_new_users_messages=messages,
        place_onboarding_rooms_in_space=place_in_space,
    )


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
    await svc.welcome_user("@alice:matrix.test")
    assert client.space_links == []
