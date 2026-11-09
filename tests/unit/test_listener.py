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
        self.board_avatar: str | None = None

    def board_avatar_sha256(self) -> str | None:
        return self.board_avatar

    async def welcome_user(self, mxid: str) -> str | None:
        await asyncio.sleep(0)
        self.welcomed.append(mxid)
        return self.board_avatar  # the board gets the picture known at the time

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
    listener._welcomed.pop(BOB, None)  # a welcome that failed in that pass

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
    # The next pass and a join (the force-join into their board) report users already in hand: not
    # run now, and not again once their run succeeded. A leave asks for another look, though.
    await _pass(events, *(f"@u{i}:matrix.test" for i in range(10)))
    await listener.handle_sync(_joins("@u0:matrix.test"))
    await listener.handle_sync(_leave("@u1:matrix.test"))
    welcome.gate.set()
    for _ in range(100):
        if not listener.queued and not welcome.running:
            break
        await asyncio.sleep(0.01)

    assert welcome.max_running == 3
    assert not welcome.overlap
    assert sorted(welcome.welcomed) == sorted([f"@u{i}:matrix.test" for i in range(10)] + ["@u1:matrix.test"])
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


async def test_a_user_reported_again_while_a_failing_run_is_in_hand_runs_once_more() -> None:
    class _FailOnce(_GatedWelcome):
        async def welcome_user(self, mxid: str) -> None:
            await super().welcome_user(mxid)
            if len(self.welcomed) == 1:
                raise RuntimeError("synapse hiccup")

    welcome = _FailOnce()
    listener, events = _listener(welcome)
    worker = asyncio.create_task(listener.run())
    await _pass(events, ALICE)
    await asyncio.sleep(0.01)
    await listener.handle_sync(_joins(ALICE))
    welcome.gate.set()
    for _ in range(100):
        if len(welcome.welcomed) == 2 and not welcome.running:
            break
        await asyncio.sleep(0.01)

    assert welcome.welcomed == [ALICE, ALICE]
    listener.request_stop()
    await asyncio.wait_for(worker, timeout=1)


# --- ONB-15 (S18): a new bot avatar reaches every notice board without a restart ---------------------


async def test_a_new_bot_avatar_has_every_board_looked_at_again() -> None:
    welcome = _RecordingWelcome()
    welcome.board_avatar = "red"
    listener, events = _listener(welcome)
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert sorted(welcome.welcomed) == [ALICE, BOB]  # welcomed once, as before

    welcome.board_avatar = "blue"  # the media layer saw a new picture behind the URL
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert sorted(welcome.welcomed) == [ALICE, ALICE, BOB, BOB]

    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert len(welcome.welcomed) == 4


async def test_boards_welcomed_before_the_bot_avatar_could_be_fetched_get_it_once_it_can() -> None:
    """W6R-07: the first digest to appear cleared nothing, so those boards waited for a restart."""
    welcome = _RecordingWelcome()  # the bot avatar URL cannot be fetched yet: no digest
    listener, events = _listener(welcome)
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert sorted(welcome.welcomed) == [ALICE, BOB]

    welcome.board_avatar = "red"  # fetched at last
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert sorted(welcome.welcomed) == [ALICE, ALICE, BOB, BOB]

    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert len(welcome.welcomed) == 4


async def test_a_bot_avatar_known_from_the_start_costs_no_second_look() -> None:
    welcome = _RecordingWelcome()
    welcome.board_avatar = "red"  # fetched by the startup step, before the first pass
    listener, events = _listener(welcome)
    for _ in range(2):
        await _pass(events, ALICE, BOB)
        await listener.process_queued()
    assert sorted(welcome.welcomed) == [ALICE, BOB]


async def test_boards_welcomed_with_the_picture_are_not_looked_at_again_when_it_first_appears() -> None:
    """W7R-05: the first digest known cleared the whole cache, although the welcomes had fetched it."""

    class _FetchingWelcome(_RecordingWelcome):
        async def welcome_user(self, mxid: str) -> str | None:
            applied = await super().welcome_user(mxid)
            if mxid == BOB:
                self.board_avatar = "red"  # this welcome fetched the picture and set it on the board
                return "red"
            return applied

    welcome = _FetchingWelcome()  # nothing fetched before the first pass (a restart)
    listener, events = _listener(welcome, concurrency=1)
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert welcome.welcomed == [ALICE, BOB]  # ALICE's board was welcomed without a picture

    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert welcome.welcomed == [ALICE, BOB, ALICE]  # only the board without the picture again

    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert len(welcome.welcomed) == 3


async def test_only_boards_welcomed_with_an_older_picture_are_looked_at_again() -> None:
    class _BobFailsOnce(_RecordingWelcome):
        failed = False

        async def welcome_user(self, mxid: str) -> str | None:
            if mxid == BOB and not self.failed:
                self.failed = True
                raise RuntimeError("homeserver briefly unreachable")
            return await super().welcome_user(mxid)

    welcome = _BobFailsOnce()
    welcome.board_avatar = "red"
    listener, events = _listener(welcome)
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert welcome.welcomed == [ALICE]

    welcome.board_avatar = "blue"  # the media layer saw a new picture
    await listener.handle_sync(_joins(BOB))  # BOB's welcome now runs with the new picture
    await listener.process_queued()
    assert welcome.welcomed == [ALICE, BOB]

    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert welcome.welcomed == [ALICE, BOB, ALICE]  # BOB's board already wears it
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert len(welcome.welcomed) == 3


async def test_a_board_welcomed_while_a_concurrent_welcome_saw_a_new_picture_is_looked_at_again() -> None:
    """W8R-06: the cache recorded the picture known at the end of a welcome, not the one the board
    got. A re-fetch by a concurrent welcome in between hid the board with the old picture until a
    restart."""

    class _Racy(_RecordingWelcome):
        def __init__(self) -> None:
            super().__init__()
            self.picture_changed = asyncio.Event()

        async def welcome_user(self, mxid: str) -> str | None:
            if mxid == ALICE:
                applied = self.board_avatar  # her board gets the old picture
                await self.picture_changed.wait()  # still sending her messages
            else:
                self.board_avatar = applied = "blue"  # BOB's welcome re-fetched it: a new picture
                self.picture_changed.set()
            self.welcomed.append(mxid)
            return applied

    welcome = _Racy()
    welcome.board_avatar = "red"
    listener, events = _listener(welcome, concurrency=2)
    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert sorted(welcome.welcomed) == [ALICE, BOB]

    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert sorted(welcome.welcomed) == [ALICE, ALICE, BOB]  # only ALICE's board needs the new one

    await _pass(events, ALICE, BOB)
    await listener.process_queued()
    assert len(welcome.welcomed) == 3
