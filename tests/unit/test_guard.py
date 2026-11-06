"""The destructive-action guard on its own (D4 layers 1 and 2, D13): decisions, batches, commands."""

from __future__ import annotations

from typing import Any

import pytest

from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.guard import (
    BATCH_ID_ALPHABET,
    ActionKind,
    BatchStatus,
    DestructiveAction,
    DestructiveActionGuard,
    GuardLimits,
    GuardState,
    InputCounts,
    MatrixAccountDataGuardStore,
    MemoryGuardStore,
    decide,
    describe_batch,
    input_drops,
    new_batch_id,
    record,
)

LIMITS = GuardLimits(max_drop_percent=20, max_reversible_actions_per_pass=10, confirmation_expiry_sec=100)


def kick(n: int, room: str = "!r:x") -> DestructiveAction:
    return DestructiveAction(kind=ActionKind.kick, target=f"@u{n}:x", room_id=room, group_pk="g")


def lock(n: int) -> DestructiveAction:
    return DestructiveAction(kind=ActionKind.lock, target=f"@u{n}:x")


def erase(n: int) -> DestructiveAction:
    return DestructiveAction(kind=ActionKind.erase, target=f"@u{n}:x")


def counts(users: int | None = 100, groups: int | None = 10) -> InputCounts:
    return InputCounts(users_in_scope=users, mapped_groups=groups)


def state_with_baseline(users: int | None = 100, groups: int | None = 10) -> GuardState:
    return GuardState(baseline=counts(users, groups))


# --- ids ------------------------------------------------------------------------------------------


def test_batch_ids_have_six_characters_without_look_alikes() -> None:
    ids = {new_batch_id() for _ in range(200)}
    assert len(ids) > 190
    for batch_id in ids:
        assert len(batch_id) == 6
        assert set(batch_id) <= set(BATCH_ID_ALPHABET)
    assert not set("0O1lIio") & set(BATCH_ID_ALPHABET)


# --- layer 1: input plausibility --------------------------------------------------------------------


def test_a_drop_of_more_than_the_limit_trips() -> None:
    reasons = input_drops(counts(100, 10), counts(79, 10), 20)
    assert len(reasons) == 1
    assert "from 100 to 79" in reasons[0]


def test_a_drop_up_to_the_limit_does_not_trip() -> None:
    assert input_drops(counts(100, 10), counts(80, 10), 20) == []
    assert input_drops(counts(100, 10), counts(150, 12), 20) == []


def test_an_empty_answer_always_trips_even_without_a_baseline() -> None:
    assert input_drops(InputCounts(), counts(0, 3), 20)
    assert input_drops(counts(5, 3), counts(5, 0), 100)  # 100 % switches the ratio off, not this


def test_zero_accepted_by_an_admin_is_not_a_drop() -> None:
    assert input_drops(counts(0, 0), counts(0, 0), 20) == []


def test_unmeasured_counts_are_ignored() -> None:
    assert input_drops(counts(100, 10), counts(None, None), 20) == []


def test_the_baseline_moves_with_every_normal_pass_so_gradual_removals_never_trip() -> None:
    state = state_with_baseline(100, 10)
    users = 100
    for _ in range(10):
        users = users * 85 // 100  # 15 % of the last pass, every pass
        planned = [kick(1)]
        decision = decide(state, counts(users, 10), planned, LIMITS, now=0)
        assert decision.held == ()
        record(state, decision, planned, LIMITS, now=0, complete=True)
    assert state.baseline.users_in_scope == users


def test_layer_1_pauses_everything_and_keeps_the_baseline() -> None:
    state = state_with_baseline()
    planned = [kick(1), lock(2), erase(3)]
    decision = decide(state, counts(1, 10), planned, LIMITS, now=0)
    assert decision.input_tripped
    assert decision.permitted == frozenset()
    assert {a.key for a in decision.held} == {a.key for a in planned}

    notices = record(state, decision, planned, LIMITS, now=0, complete=True, id_factory=lambda: "abcdef")
    assert state.baseline.users_in_scope == 100
    batch = state.batches["abcdef"]
    assert batch.observed == counts(1, 10)
    assert batch.expires_at == 100
    [notice] = notices
    assert notice.signal is Signal.destructive_actions_suppressed
    assert notice.payload["batch_id"] == "abcdef"
    assert "!confirm abcdef" in notice.payload["summary"]
    assert "from 100 to 1" in notice.payload["summary"]


# --- layer 2: per-pass limit ------------------------------------------------------------------------


def test_up_to_the_limit_of_reversible_actions_runs() -> None:
    planned = [kick(n) for n in range(10)]
    decision = decide(state_with_baseline(), counts(), planned, LIMITS, now=0)
    assert decision.held == ()
    assert len(decision.permitted) == 10


def test_above_the_limit_pauses_the_reversible_actions_only() -> None:
    planned = [kick(n) for n in range(6)] + [lock(n) for n in range(5)] + [erase(99)]
    decision = decide(state_with_baseline(), counts(), planned, LIMITS, now=0)
    assert not decision.input_tripped
    assert len(decision.held) == 11
    assert decision.permitted == {erase(99).key}  # irreversible actions are layer 3 (S5)
    assert "11 reversible destructive actions" in decision.reasons[0]


def test_layer_2_does_not_move_the_baseline_logic() -> None:
    state = state_with_baseline(100, 10)
    planned = [kick(n) for n in range(11)]
    decision = decide(state, counts(95, 10), planned, LIMITS, now=0)
    record(state, decision, planned, LIMITS, now=0, complete=True)
    assert state.baseline.users_in_scope == 95  # plausible input: the baseline moves anyway


# --- batches ----------------------------------------------------------------------------------------


def _proposed(state: GuardState, planned: list[DestructiveAction], c: InputCounts, *, now: float = 0) -> str:
    decision = decide(state, c, planned, LIMITS, now=now)
    ids = iter(["aaaaaa", "bbbbbb", "cccccc"])
    before = set(state.batches)
    record(state, decision, planned, LIMITS, now=now, complete=True, id_factory=lambda: next(ids))
    [new] = set(state.batches) - before
    return new


def test_a_paused_set_that_is_still_open_is_not_proposed_again() -> None:
    state = state_with_baseline()
    planned = [kick(1), kick(2)]
    _proposed(state, planned, counts(1, 10))
    decision = decide(state, counts(1, 10), planned, LIMITS, now=1)
    notices = record(state, decision, planned, LIMITS, now=1, complete=True)
    assert notices == []
    assert len(state.batches) == 1


def test_a_grown_set_gets_a_new_batch_for_the_new_part_only() -> None:
    state = state_with_baseline()
    first = _proposed(state, [kick(1), kick(2)], counts(1, 10))
    second = _proposed(state, [kick(1), kick(2), lock(3)], counts(1, 10), now=1)
    assert first != second
    assert [a.key for a in state.batches[second].actions] == [lock(3).key]


def test_actions_of_an_open_batch_stay_paused_when_the_pass_looks_normal_again() -> None:
    """A confirmation never covers more than the admin saw: only confirming *that* batch releases it."""
    state = state_with_baseline()
    batch_id = _proposed(state, [kick(n) for n in range(11)], counts())
    decision = decide(state, counts(), [kick(1), kick(2)], LIMITS, now=1)
    assert decision.permitted == frozenset()
    assert batch_id in decision.reasons[0]


def test_a_confirmed_batch_runs_only_what_still_applies_and_closes() -> None:
    state = state_with_baseline()
    batch_id = _proposed(state, [kick(n) for n in range(11)], counts())
    state.batches[batch_id].status = BatchStatus.confirmed
    state.batches[batch_id].decided_by = "@admin:x"
    state.batches[batch_id].decided_at = 1

    still = [kick(n) for n in range(3)] + [kick(50)]  # 8 no longer apply, one is new
    decision = decide(state, counts(), still, LIMITS, now=2)
    assert decision.permitted == {a.key for a in still}  # the new one alone is below the limit
    assert decision.confirmed_batches == (batch_id,)
    notices = record(state, decision, still, LIMITS, now=2, complete=True)
    assert batch_id not in state.batches
    assert notices[0].signal is Signal.guard_batch_closed
    assert "ran 3 action(s); 8 no longer applied" in notices[0].payload["text"]


def test_a_confirmed_drop_becomes_the_new_baseline() -> None:
    state = state_with_baseline(100, 10)
    batch_id = _proposed(state, [kick(1)], counts(40, 10))
    state.batches[batch_id].status = BatchStatus.confirmed
    state.batches[batch_id].decided_at = 1
    planned = [kick(1), lock(7)]
    decision = decide(state, counts(40, 10), planned, LIMITS, now=2)
    assert decision.permitted == {kick(1).key}  # the lock was not part of what the admin saw
    record(state, decision, planned, LIMITS, now=2, complete=True)
    assert state.baseline.users_in_scope == 40
    assert state.batches == {}  # the next pass decides on the lock against the new baseline

    decision = decide(state, counts(40, 10), [lock(7)], LIMITS, now=3)
    assert decision.permitted == {lock(7).key}


def test_a_batch_confirmed_during_a_pass_waits_for_the_next_one() -> None:
    state = state_with_baseline()
    planned = [kick(n) for n in range(11)]
    batch_id = _proposed(state, planned, counts())
    decision = decide(state, counts(), planned, LIMITS, now=5)
    state.batches[batch_id].status = BatchStatus.confirmed  # the admin confirms meanwhile
    state.batches[batch_id].decided_at = 6
    record(state, decision, planned, LIMITS, now=7, complete=True)
    assert state.batches[batch_id].status is BatchStatus.confirmed


def test_an_unconfirmed_batch_expires_and_is_proposed_again_if_it_still_applies() -> None:
    state = state_with_baseline()
    planned = [kick(n) for n in range(11)]
    first = _proposed(state, planned, counts())
    decision = decide(state, counts(), planned, LIMITS, now=100)
    notices = record(state, decision, planned, LIMITS, now=100, complete=True, id_factory=lambda: "zzzzzz")
    assert first not in state.batches
    assert "zzzzzz" in state.batches
    outcomes = [n.payload.get("outcome") for n in notices if n.signal is Signal.guard_batch_closed]
    assert outcomes == ["expired"]


def test_a_rejected_batch_is_not_proposed_again_while_the_incident_lasts() -> None:
    state = state_with_baseline()
    planned = [kick(1), kick(2)]
    batch_id = _proposed(state, planned, counts(1, 10))
    state.batches[batch_id].status = BatchStatus.rejected
    for now in (1, 500, 10_000):  # no expiry for a rejection
        decision = decide(state, counts(1, 10), planned, LIMITS, now=now)
        assert decision.permitted == frozenset()
        assert record(state, decision, planned, LIMITS, now=now, complete=True) == []
    assert list(state.batches) == [batch_id]

    # The incident ends (the token was fixed): nothing of it is planned any more.
    decision = decide(state, counts(), [], LIMITS, now=10_001)
    record(state, decision, [], LIMITS, now=10_001, complete=True)
    assert state.batches == {}


def test_a_pending_batch_that_no_longer_applies_is_closed() -> None:
    state = state_with_baseline()
    batch_id = _proposed(state, [kick(1)], counts(1, 10))
    decision = decide(state, counts(), [], LIMITS, now=1)
    notices = record(state, decision, [], LIMITS, now=1, complete=True)
    assert batch_id not in state.batches
    assert notices[0].payload["outcome"] == "obsolete"


def test_a_failed_pass_closes_nothing_and_keeps_the_baseline() -> None:
    state = state_with_baseline(100, 10)
    batch_id = _proposed(state, [kick(1)], counts(1, 10))
    decision = decide(state, counts(100, 3), [], LIMITS, now=1)
    record(state, decision, [], LIMITS, now=1, complete=False)
    assert batch_id in state.batches
    assert state.baseline.mapped_groups == 10


def test_the_report_lists_actions_and_cuts_long_lists() -> None:
    state = state_with_baseline()
    batch_id = _proposed(state, [kick(n) for n in range(40)], counts(1, 10))
    text = describe_batch(state.batches[batch_id], limit=5)
    assert "kick 40 room member(s)" in text
    assert "- kick @u0:x from !r:x (group g)" in text
    assert "and 35 more" in text


# --- the service: commands and persistence ------------------------------------------------------------


class AccountData:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        return dict(self.data.get(data_type, {}))

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        self.data[data_type] = content


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _service(store: Any = None) -> tuple[DestructiveActionGuard, Clock, list[Event]]:
    bus = EventBus()
    seen: list[Event] = []

    async def collect(event: Event) -> None:
        seen.append(event)

    bus.subscribe(Signal.destructive_actions_suppressed, collect)
    bus.subscribe(Signal.guard_batch_closed, collect)
    clock = Clock()
    guard = DestructiveActionGuard(LIMITS, store or MemoryGuardStore(), bus, clock=clock)
    return guard, clock, seen


async def _pause(guard: DestructiveActionGuard, planned: list[DestructiveAction]) -> str:
    decision = await guard.decide(counts(1, 10), planned)
    await guard.record(decision, planned, complete=True)
    [batch] = await guard.open_batches()
    return batch.id


async def test_confirm_releases_exactly_that_batch() -> None:
    guard, _, seen = _service(MemoryGuardStore(state_with_baseline()))
    first = await _pause(guard, [kick(1)])
    assert seen[0].payload["batch_id"] == first
    decision = await guard.decide(counts(1, 10), [kick(1), kick(2)])
    await guard.record(decision, [kick(1), kick(2)], complete=True)
    second = next(b.id for b in await guard.open_batches() if b.id != first)

    ok, reply = await guard.confirm(first.upper(), "@admin:x")  # ids are case-insensitive
    assert ok and first in reply
    decision = await guard.decide(counts(1, 10), [kick(1), kick(2)])
    assert decision.permitted == {kick(1).key}
    assert any(a.key == kick(2).key for a in decision.held)
    assert second in decision.reasons[0]


async def test_confirm_and_reject_refuse_unknown_expired_and_repeated_ids() -> None:
    guard, _, _ = _service(MemoryGuardStore(state_with_baseline()))
    batch_id = await _pause(guard, [kick(1)])

    assert (await guard.confirm("nosuch", "@a:x"))[0] is False
    assert (await guard.confirm(batch_id, "@a:x"))[0] is True
    ok, reply = await guard.confirm(batch_id, "@a:x")
    assert not ok and "already confirmed" in reply
    assert (await guard.reject(batch_id, "@a:x"))[0] is False

    other_guard, other_clock, _ = _service(MemoryGuardStore(state_with_baseline()))
    other = await _pause(other_guard, [kick(1)])
    other_clock.now += 100  # the expiry
    ok, reply = await other_guard.confirm(other, "@a:x")
    assert not ok and "expired" in reply


async def test_rejecting_closes_the_batch_without_running_it() -> None:
    guard, _, _ = _service(MemoryGuardStore(state_with_baseline()))
    batch_id = await _pause(guard, [kick(1)])
    ok, reply = await guard.reject(batch_id, "@admin:x")
    assert ok and "rejected" in reply
    assert await guard.open_batches() == []
    decision = await guard.decide(counts(1, 10), [kick(1)])
    assert decision.permitted == frozenset()


async def test_the_state_survives_a_restart_in_account_data() -> None:
    account_data = AccountData()
    store = MatrixAccountDataGuardStore(account_data, "@bot:x", "x.org")
    guard, _, _ = _service(store)
    await guard.record(await guard.decide(counts(100, 10), []), [], complete=True)
    batch_id = await _pause(guard, [kick(1)])
    assert "org.x.onbot.guard" in account_data.data

    restarted, _, _ = _service(MatrixAccountDataGuardStore(account_data, "@bot:x", "x.org"))
    [batch] = await restarted.open_batches()
    assert batch.id == batch_id
    assert (await restarted.confirm(batch_id, "@admin:x"))[0]


async def test_an_unreadable_state_starts_empty_and_releases_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    account_data = AccountData()
    account_data.data["org.x.onbot.guard"] = {"batches": "not a mapping"}
    guard, _, _ = _service(MatrixAccountDataGuardStore(account_data, "@bot:x", "x.org"))
    decision = await guard.decide(counts(0, 10), [kick(1)])
    assert decision.permitted == frozenset()  # an empty answer still trips
    assert "unreadable" in caplog.text


_CONFIG: dict[str, Any] = {
    "synapse_server": {
        "server_name": "x.org",
        "server_url": "https://m",
        "bot_user_id": "@bot:x.org",
        "bot_access_token": "t",
    },
    "authentik_server": {"url": "https://a/", "api_key": "k"},
    "admin_room": {"enabled": False},
}


def test_limits_come_from_the_config() -> None:
    assert GuardLimits.from_config(OnbotConfig.model_validate(_CONFIG)) == GuardLimits(20, 10, 72 * 3600)
    config = OnbotConfig.model_validate(
        {
            **_CONFIG,
            "destructive_action_guard": {
                "max_drop_percent": 35,
                "max_reversible_actions_per_pass": 3,
                "confirmation_expiry_sec": 60,
            },
        }
    )
    assert GuardLimits.from_config(config) == GuardLimits(35, 3, 60)


@pytest.mark.parametrize(
    "bad",
    [{"max_drop_percent": 0}, {"max_drop_percent": 101}, {"max_reversible_actions_per_pass": -1}, {"x": 1}],
)
def test_implausible_limits_are_config_errors(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OnbotConfig.model_validate({**_CONFIG, "destructive_action_guard": bad})


def test_a_group_drop_pauses_what_happens_in_rooms_only() -> None:
    """Locks are judged on the users alone: an empty group list must not stall offboarding."""
    state = state_with_baseline(100, 10)
    teardown = DestructiveAction(kind=ActionKind.room_teardown, target="!r:x", room_id="!r:x", group_pk="g")
    planned = [kick(1), teardown, lock(2), erase(3)]
    decision = decide(state, counts(100, 0), planned, LIMITS, now=0)
    assert {a.key for a in decision.held} == {kick(1).key, teardown.key}
    assert decision.permitted == {lock(2).key, erase(3).key}
    assert decision.tripped_metrics == {"mapped_groups"}


def test_the_baseline_moves_per_metric() -> None:
    """A deployment without group rooms still learns its user count, so a user drop is caught."""
    state = GuardState()
    for users in (50, 52, 51):
        decision = decide(state, counts(users, 0), [lock(1)], LIMITS, now=0)
        assert decision.permitted == {lock(1).key}
        record(state, decision, [lock(1)], LIMITS, now=0, complete=True)
    assert state.baseline == InputCounts(users_in_scope=51, mapped_groups=None)

    decision = decide(state, counts(5, 0), [lock(1)], LIMITS, now=1)
    assert decision.permitted == frozenset()
    assert "from 51 to 5" in " ".join(decision.reasons)
