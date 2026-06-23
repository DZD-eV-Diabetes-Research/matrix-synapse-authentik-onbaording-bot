"""Unit tests for the onboarding listener: join extraction, filtering, and signal wiring."""

from __future__ import annotations

from onbot.clients.matrix import RoomSync, SyncNotSupportedError, SyncResult
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


class _ScriptedClient:
    """A fake Matrix client whose ``sliding_sync`` yields scripted results then signals stop.

    Once the script is exhausted it requests stop and returns an empty result, so the final
    scripted batch is fully processed before ``run()`` exits (it only checks ``_stop`` between
    iterations and per joined user).
    """

    def __init__(self, listener: OnboardingListener, results: list[object]) -> None:
        self._listener = listener
        self._results = list(results)
        self.calls = 0

    async def sliding_sync(self, pos: str | None) -> SyncResult:
        self.calls += 1
        if not self._results:
            self._listener.request_stop()
            return SyncResult(pos=None, rooms=[])
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, SyncResult)
        return item


def _join(mxid: str) -> SyncResult:
    return SyncResult(
        pos="s1",
        rooms=[
            RoomSync(
                room_id="!r:x",
                timeline=[{"type": "m.room.member", "state_key": mxid, "content": {"membership": "join"}}],
            )
        ],
    )


async def test_run_welcomes_joining_users_until_stopped() -> None:
    welcome = _RecordingWelcome()
    listener = OnboardingListener(client=None, welcome=welcome, config=_config(), events=EventBus())  # type: ignore[arg-type]
    listener.client = _ScriptedClient(listener, [_join("@real:matrix.test")])  # type: ignore[assignment]

    await listener.run()

    assert welcome.welcomed == ["@real:matrix.test"]


async def test_run_breaks_when_sliding_sync_unsupported() -> None:
    welcome = _RecordingWelcome()
    listener = OnboardingListener(client=None, welcome=welcome, config=_config(), events=EventBus())  # type: ignore[arg-type]
    # A lone SyncNotSupportedError must end the loop (fall back to the signal path).
    listener._stop.clear()

    class _Unsupported:
        async def sliding_sync(self, pos: str | None) -> SyncResult:
            raise SyncNotSupportedError("nope")

    listener.client = _Unsupported()  # type: ignore[assignment]
    await listener.run()
    assert welcome.welcomed == []


async def test_run_backs_off_on_transient_error_then_continues() -> None:
    welcome = _RecordingWelcome()
    listener = OnboardingListener(client=None, welcome=welcome, config=_config(), events=EventBus())  # type: ignore[arg-type]
    # First call errors (backoff path), second yields a join and stops.
    listener.client = _ScriptedClient(  # type: ignore[assignment]
        listener, [RuntimeError("transient"), _join("@real:matrix.test")]
    )
    # Keep the backoff instant.
    listener._sleep = lambda seconds: _noop()  # type: ignore[method-assign]

    await listener.run()

    # error → backoff → join (welcomed) → empty (stop).
    assert listener.client.calls == 3  # type: ignore[attr-defined]
    assert welcome.welcomed == ["@real:matrix.test"]


async def _noop() -> None:
    return None
