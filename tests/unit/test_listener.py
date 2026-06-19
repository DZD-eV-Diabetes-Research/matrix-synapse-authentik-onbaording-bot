"""Unit tests for the onboarding listener: join extraction, filtering, and signal wiring."""

from __future__ import annotations

from onbot.clients.matrix import RoomSync, SyncResult
from onbot.config import AuthentikServer, OnbotConfig, SynapseServer
from onbot.events import EventBus, Signal
from onbot.onboarding.listener import OnboardingListener, extract_joined_users


def _config() -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id="@bot:matrix.test",
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        matrix_user_ignore_list=["@ignored:matrix.test"],
    )


class _RecordingWelcome:
    def __init__(self) -> None:
        self.welcomed: list[str] = []

    async def welcome_user(self, mxid: str) -> None:
        self.welcomed.append(mxid)


def test_extract_joined_users_picks_only_joins() -> None:
    result = SyncResult(
        pos="s1",
        rooms=[
            RoomSync(
                room_id="!r:x",
                timeline=[
                    {"type": "m.room.member", "state_key": "@a:x", "content": {"membership": "join"}},
                    {"type": "m.room.member", "state_key": "@b:x", "content": {"membership": "leave"}},
                    {"type": "m.room.message", "sender": "@c:x", "content": {}},
                ],
                required_state=[
                    {"type": "m.room.member", "state_key": "@d:x", "content": {"membership": "join"}},
                ],
            )
        ],
    )
    assert extract_joined_users(result) == {"@a:x", "@d:x"}


async def test_maybe_welcome_filters_bot_and_ignored() -> None:
    welcome = _RecordingWelcome()
    listener = OnboardingListener(client=None, welcome=welcome, config=_config(), events=EventBus())  # type: ignore[arg-type]

    await listener._maybe_welcome("@bot:matrix.test")
    await listener._maybe_welcome("@ignored:matrix.test")
    await listener._maybe_welcome("@real:matrix.test")

    assert welcome.welcomed == ["@real:matrix.test"]


async def test_user_synced_signal_triggers_welcome() -> None:
    welcome = _RecordingWelcome()
    events = EventBus()
    listener = OnboardingListener(client=None, welcome=welcome, config=_config(), events=events)  # type: ignore[arg-type]
    listener.start()

    await events.emit(Signal.user_synced, mxid="@real:matrix.test")

    assert welcome.welcomed == ["@real:matrix.test"]
