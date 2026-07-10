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


class _FailingWelcome:
    async def welcome_user(self, mxid: str) -> None:
        raise RuntimeError("boom")


async def test_maybe_welcome_swallows_welcome_errors() -> None:
    # A failing welcome flow must not crash the listener (logged, not raised).
    listener = OnboardingListener(client=None, welcome=_FailingWelcome(), config=_config(), events=EventBus())  # type: ignore[arg-type]
    await listener._maybe_welcome("@real:matrix.test")  # does not raise


async def test_handle_sync_welcomes_users_who_joined_in_the_slice() -> None:
    welcome = _RecordingWelcome()
    listener = OnboardingListener(client=None, welcome=welcome, config=_config(), events=EventBus())  # type: ignore[arg-type]

    await listener.handle_sync(
        SyncResult(
            pos="s1",
            rooms=[
                RoomSync(
                    room_id="!r:x",
                    timeline=[
                        {
                            "type": "m.room.member",
                            "state_key": "@real:matrix.test",
                            "content": {"membership": "join"},
                        },
                        {
                            "type": "m.room.member",
                            "state_key": "@ignored:matrix.test",
                            "content": {"membership": "join"},
                        },
                    ],
                )
            ],
        )
    )

    assert welcome.welcomed == ["@real:matrix.test"]


async def test_an_already_welcomed_user_is_not_welcomed_again() -> None:
    """The reconciler re-emits user_synced every pass. Proving a user is already welcomed costs
    three CS-API reads, so the listener must not even ask the WelcomeService."""
    welcome = _RecordingWelcome()
    events = EventBus()
    listener = OnboardingListener(None, welcome, _config(), events)  # type: ignore[arg-type]
    listener.start()

    for _ in range(3):
        await events.emit(Signal.user_synced, mxid="@real:matrix.test")

    assert welcome.welcomed == ["@real:matrix.test"]


async def test_a_failed_welcome_is_retried_on_the_next_tick() -> None:
    """A briefly unreachable homeserver must not cost the user their welcome."""

    class _FlakyWelcome:
        def __init__(self) -> None:
            self.attempts = 0

        async def welcome_user(self, mxid: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synapse is down")

    welcome = _FlakyWelcome()
    events = EventBus()
    listener = OnboardingListener(None, welcome, _config(), events)  # type: ignore[arg-type]
    listener.start()

    await events.emit(Signal.user_synced, mxid="@real:matrix.test")
    await events.emit(Signal.user_synced, mxid="@real:matrix.test")
    await events.emit(Signal.user_synced, mxid="@real:matrix.test")

    assert welcome.attempts == 2  # retried after the failure, then remembered
