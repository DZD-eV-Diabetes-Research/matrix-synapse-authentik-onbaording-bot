"""Unit tests for the onboarding listener: who is welcomed (SEC-06, ONB-13), and the worker queue
that delivers welcomes beside the reconcile pass (RUN-08)."""

from __future__ import annotations

import asyncio
from collections.abc import Collection

import pytest

from onbot.clients.matrix import RoomSync, SyncResult
from onbot.config import AdminRoom, AuthentikServer, OnbotConfig, SynapseServer
from onbot.events import EventBus, Signal
from onbot.onboarding.listener import OnboardingListener, extract_joined_users, extract_left_users
from onbot.onboarding.welcome import NoticeBoardUnreadable

ALICE = "@alice:matrix.test"
BOB = "@bob:matrix.test"
OUTSIDER = "@outsider:matrix.test"


def _config() -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id="@bot:matrix.test",
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        admin_room=AdminRoom(enabled=False),
        matrix_user_ignore_list=["@ignored:matrix.test"],
    )


class _RecordingWelcome:
    def __init__(self) -> None:
        self.welcomed: list[str] = []
        self.pruned: list[frozenset[str]] = []

    async def welcome_user(self, mxid: str) -> None:
        await asyncio.sleep(0)
        self.welcomed.append(mxid)

    async def prune_direct(self, accounts: Collection[str]) -> list[str]:
        self.pruned.append(frozenset(accounts))
        return []


def _listener(welcome: object, **kwargs: object) -> tuple[OnboardingListener, EventBus]:
    events = EventBus()
    listener = OnboardingListener(None, welcome, _config(), events, **kwargs)  # type: ignore[arg-type]
    listener.start()
    return listener, events


async def _pass(events: EventBus, *mxids: str, accounts: Collection[str] | None = None) -> None:
    await events.emit(Signal.users_mapped, mxids=frozenset(mxids), accounts=frozenset(accounts or mxids))


def _joins(*mxids: str, room: str = "!r:x") -> SyncResult:
    return SyncResult(
        pos="s1",
        rooms=[
            RoomSync(
                room_id=room,
                timeline=[
                    {"type": "m.room.member", "state_key": m, "content": {"membership": "join"}}
                    for m in mxids
                ],
            )
        ],
    )


def _leave(mxid: str, room: str = "!board:x") -> SyncResult:
    return SyncResult(
        pos="s2",
        rooms=[
            RoomSync(
                room_id=room,
                timeline=[{"type": "m.room.member", "state_key": mxid, "content": {"membership": "leave"}}],
            )
        ],
    )


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
                    {"type": "m.room.member", "state_key": "@e:x", "content": {"membership": "leave"}},
                ],
            )
        ],
    )
    assert extract_joined_users(result) == {"@a:x", "@d:x"}
    # A leave in the room's current state is no news; only the timeline's leaves are.
    assert extract_left_users(result) == {"@b:x"}


# --- who (SEC-06, ONB-13) ---


async def test_a_pass_queues_the_mapped_users_and_the_workers_welcome_them() -> None:
    welcome = _RecordingWelcome()
    listener, events = _listener(welcome)

    await _pass(events, ALICE, BOB)
    assert listener.queued == {ALICE, BOB}
    await listener.process_queued()

    assert sorted(welcome.welcomed) == [ALICE, BOB]
    assert listener.queued == frozenset()


async def test_somebody_who_joins_a_room_but_is_not_mapped_gets_no_welcome() -> None:
    """SEC-06, live check 6: an account outside Authentik joined a group room the bot sits in."""
    welcome = _RecordingWelcome()
    listener, events = _listener(welcome)
    await _pass(events, ALICE)
    await listener.process_queued()

    await listener.handle_sync(_joins(OUTSIDER, "@guest:partner.org"))
    await listener.process_queued()

    assert welcome.welcomed == [ALICE]


async def test_before_the_first_pass_the_sync_stream_welcomes_nobody() -> None:
    """ONB-13: the first sync after a restart replays every member of every room."""
    welcome = _RecordingWelcome()
    listener, _ = _listener(welcome)

    await listener.handle_sync(_joins(ALICE, BOB, OUTSIDER))
    await listener.process_queued()

    assert welcome.welcomed == []


async def test_a_mapped_user_seen_joining_is_welcomed_without_waiting_for_a_pass() -> None:
    welcome = _RecordingWelcome()
    listener, events = _listener(welcome)
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    welcome.welcomed.clear()
    listener._welcomed.discard(BOB)  # a welcome that failed in that pass

    await listener.handle_sync(_joins(ALICE, BOB))
    await listener.process_queued()

    assert welcome.welcomed == [BOB]


async def test_the_bot_and_ignored_users_are_never_welcomed() -> None:
    welcome = _RecordingWelcome()
    listener, events = _listener(welcome)

    await _pass(events, "@bot:matrix.test", "@ignored:matrix.test", ALICE)
    await listener.process_queued()

    assert welcome.welcomed == [ALICE]


async def test_a_user_unmapped_while_queued_is_skipped() -> None:
    welcome = _RecordingWelcome()
    listener, events = _listener(welcome)
    await _pass(events, ALICE, BOB)
    await _pass(events, ALICE)  # Bob left the sync scope before a worker got to him

    await listener.process_queued()

    assert welcome.welcomed == [ALICE]


async def test_an_already_welcomed_user_is_not_welcomed_again() -> None:
    """Every pass reports every mapped user; proving one is welcomed costs reads, so it is skipped."""
    welcome = _RecordingWelcome()
    listener, events = _listener(welcome)

    for _ in range(3):
        await _pass(events, ALICE)
        await listener.process_queued()

    assert welcome.welcomed == [ALICE]


async def test_a_user_who_left_a_room_is_checked_again() -> None:
    """ONB-07: the welcome flow looks at their notice board and re-invites once."""
    welcome = _RecordingWelcome()
    listener, events = _listener(welcome)
    await _pass(events, ALICE)
    await listener.process_queued()

    await listener.handle_sync(_leave(ALICE))
    await listener.handle_sync(_leave(OUTSIDER))
    await listener.process_queued()

    assert welcome.welcomed == [ALICE, ALICE]


async def test_each_pass_queues_one_prune_job_with_the_listed_accounts() -> None:
    welcome = _RecordingWelcome()
    listener, events = _listener(welcome)

    await _pass(events, ALICE, accounts={ALICE, BOB})
    await listener.process_queued()

    assert welcome.pruned == [frozenset({ALICE, BOB})]


# --- failures ---


async def test_a_failed_welcome_is_retried_with_the_next_pass() -> None:
    """A briefly unreachable homeserver must not cost the user their welcome."""

    class _FlakyWelcome(_RecordingWelcome):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def welcome_user(self, mxid: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synapse is down")

    welcome = _FlakyWelcome()
    listener, events = _listener(welcome)
    for _ in range(3):
        await _pass(events, ALICE)
        await listener.process_queued()

    assert welcome.attempts == 2  # retried after the failure, then remembered


async def test_a_failure_that_repeats_logs_its_traceback_once(caplog: pytest.LogCaptureFixture) -> None:
    class _Broken(_RecordingWelcome):
        async def welcome_user(self, mxid: str) -> None:
            raise RuntimeError("M_UNKNOWN")

    listener, events = _listener(_Broken())
    for _ in range(3):
        await _pass(events, ALICE)
        await listener.process_queued()

    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) == 1
    assert sum("still failing" in r.getMessage() for r in caplog.records) == 2


async def test_an_unreadable_notice_board_is_not_retried_every_pass() -> None:
    """ONB-08: retrying cannot help; the error is logged once per process."""

    class _Unreadable(_RecordingWelcome):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def welcome_user(self, mxid: str) -> None:
            self.attempts += 1
            raise NoticeBoardUnreadable("bad marker")

    welcome = _Unreadable()
    listener, events = _listener(welcome)
    for _ in range(3):
        await _pass(events, ALICE)
        await listener.process_queued()

    assert welcome.attempts == 1


# --- decoupled from the pass (RUN-08) ---


class _GatedWelcome(_RecordingWelcome):
    """Each welcome waits for the test to open the gate; tracks how many run at once, per user too."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.running: list[str] = []
        self.max_running = 0
        self.overlap = False

    async def welcome_user(self, mxid: str) -> None:
        if mxid in self.running:
            self.overlap = True
        self.running.append(mxid)
        self.max_running = max(self.max_running, len(self.running))
        try:
            await self.gate.wait()
            self.welcomed.append(mxid)
        finally:
            self.running.remove(mxid)


async def test_the_pass_does_not_wait_for_welcomes() -> None:
    """Before the fix the pass ran the full welcome flow of every new user inside its gather phase."""
    welcome = _GatedWelcome()
    listener, events = _listener(welcome)
    worker = asyncio.create_task(listener.run())

    await asyncio.wait_for(_pass(events, *(f"@u{i}:matrix.test" for i in range(20))), timeout=1)
    await asyncio.sleep(0.01)
    assert welcome.welcomed == []  # all of them still waiting at the gate, and the pass is done

    welcome.gate.set()
    for _ in range(100):
        if len(welcome.welcomed) == 20:
            break
        await asyncio.sleep(0.01)
    assert len(welcome.welcomed) == 20
    listener.request_stop()
    await asyncio.wait_for(worker, timeout=1)


async def test_concurrency_is_bounded_and_one_user_never_runs_twice_at_once() -> None:
    welcome = _GatedWelcome()
    listener, events = _listener(welcome, concurrency=3)
    worker = asyncio.create_task(listener.run())

    await _pass(events, *(f"@u{i}:matrix.test" for i in range(10)))
    await asyncio.sleep(0.01)
    # The next pass and a join both report a user already in hand: it runs again afterwards, not now.
    await _pass(events, *(f"@u{i}:matrix.test" for i in range(10)))
    await listener.handle_sync(_joins("@u0:matrix.test"))
    welcome.gate.set()
    for _ in range(100):
        if not listener.queued and not welcome.running:
            break
        await asyncio.sleep(0.01)

    assert welcome.max_running == 3
    assert not welcome.overlap
    # The three in hand when the second pass came ran once more; nobody else did.
    assert len(welcome.welcomed) == 13
    listener.request_stop()
    await asyncio.wait_for(worker, timeout=1)


async def test_stopping_finishes_the_users_in_hand_and_leaves_the_rest() -> None:
    welcome = _GatedWelcome()
    listener, events = _listener(welcome, concurrency=2)
    worker = asyncio.create_task(listener.run())
    await _pass(events, *(f"@u{i}:matrix.test" for i in range(6)))
    await asyncio.sleep(0.01)
    assert len(welcome.running) == 2

    listener.request_stop()
    welcome.gate.set()
    await asyncio.wait_for(worker, timeout=1)

    assert len(welcome.welcomed) == 2  # the two in hand were finished, nobody new was started


async def test_stop_returns_at_once_when_the_workers_are_idle() -> None:
    listener, _ = _listener(_RecordingWelcome())
    worker = asyncio.create_task(listener.run())
    await asyncio.sleep(0)
    listener.request_stop()
    await asyncio.wait_for(worker, timeout=1)
