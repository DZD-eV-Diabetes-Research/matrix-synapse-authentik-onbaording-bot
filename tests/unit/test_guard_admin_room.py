"""The guard in the admin control room (D13): ``!pending``, ``!confirm <id>``, ``!reject <id>``, and
the reports the bot posts there (review session S3)."""

from __future__ import annotations

from typing import Any

from onbot.admin.admins import AdminResolver
from onbot.admin.control_room import ControlRoomHandler
from onbot.admin.notifier import AdminRoomNotifier
from onbot.events import EventBus, Signal
from onbot.guard import (
    ActionKind,
    DestructiveAction,
    DestructiveActionGuard,
    GuardLimits,
    GuardState,
    InputCounts,
    MatrixAccountDataGuardStore,
    MemoryGuardStore,
)
from tests.unit.test_control_room import (
    ROOM,
    STRANGER,
    _config,
    _FakeAuthentik,
    _FakeBroadcast,
    _FakeClient,
    _message,
    _run,
)

KICKS = [
    DestructiveAction(kind=ActionKind.kick, target=f"@u{n}:matrix.test", room_id="!r:x", group_pk="g")
    for n in range(3)
]


class _Engine:
    last_reconcile_at = None

    def __init__(self) -> None:
        self.triggered = 0

    def trigger(self) -> None:
        self.triggered += 1


async def _guard_with_batch() -> tuple[DestructiveActionGuard, str]:
    guard = DestructiveActionGuard(
        GuardLimits(),
        MemoryGuardStore(GuardState(baseline=InputCounts(users_in_scope=100, mapped_groups=5))),
        EventBus(),
    )
    decision = await guard.decide(InputCounts(users_in_scope=1, mapped_groups=5), KICKS)
    await guard.record(decision, KICKS, complete=True)
    [batch] = await guard.open_batches()
    return guard, batch.id


def _handler(client: _FakeClient, guard: DestructiveActionGuard, engine: _Engine) -> ControlRoomHandler:
    config = _config()
    handler = ControlRoomHandler(
        client,  # type: ignore[arg-type]
        config,
        _FakeBroadcast(),  # type: ignore[arg-type]
        AdminResolver(_FakeAuthentik(), config),  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
        guard=guard,
        started_at_ms=0,
    )
    handler.room_id = ROOM
    return handler


async def test_pending_lists_the_open_batches() -> None:
    guard, batch_id = await _guard_with_batch()
    client = _FakeClient()
    await _run(_handler(client, guard, _Engine()), _message("!pending"))
    body = client.sends[0][1]
    assert batch_id in body
    assert "kick 3 room member(s)" in body
    assert "from 100 to 1" in body


ERASURES = [DestructiveAction(kind=ActionKind.erase, target=f"@gone{n}:matrix.test") for n in range(3)]


async def _guard_with_layer_3_batch() -> tuple[DestructiveActionGuard, str]:
    guard = DestructiveActionGuard(
        GuardLimits(),
        MemoryGuardStore(GuardState(baseline=InputCounts(users_in_scope=100, mapped_groups=5))),
        EventBus(),
    )
    decision = await guard.decide(InputCounts(users_in_scope=100, mapped_groups=5), ERASURES)
    await guard.record(decision, ERASURES, complete=True)
    [batch] = await guard.open_batches()
    return guard, batch.id


async def test_pending_says_that_a_layer_3_batch_is_irreversible() -> None:
    guard, batch_id = await _guard_with_layer_3_batch()
    client = _FakeClient()
    await _run(_handler(client, guard, _Engine()), _message("!pending"))
    body = client.sends[0][1]
    assert f"Batch {batch_id} (pending, expires" in body
    assert "erase 3 account(s) (irreversible)" in body
    assert "3 irreversible actions in one pass" in body
    assert "Irreversible: 3 of these action(s) cannot be undone" in body
    assert "- erase account @gone0:matrix.test (irreversible)" in body


async def test_confirm_of_a_layer_3_batch_names_the_irreversible_actions_and_releases_them() -> None:
    guard, batch_id = await _guard_with_layer_3_batch()
    client, engine = _FakeClient(), _Engine()
    await _run(_handler(client, guard, engine), _message(f"!confirm {batch_id}"))
    assert f"Batch {batch_id} confirmed (erase 3 account(s) (irreversible))" in client.sends[0][1]
    assert engine.triggered == 1
    decision = await guard.decide(InputCounts(users_in_scope=100, mapped_groups=5), ERASURES)
    assert decision.permitted == {a.key for a in ERASURES}


async def test_confirm_releases_the_batch_and_starts_a_pass() -> None:
    guard, batch_id = await _guard_with_batch()
    client, engine = _FakeClient(), _Engine()
    await _run(_handler(client, guard, engine), _message(f"!confirm {batch_id}"))
    assert "confirmed" in client.sends[0][1]
    assert engine.triggered == 1
    decision = await guard.decide(InputCounts(users_in_scope=1, mapped_groups=5), KICKS)
    assert decision.permitted == {a.key for a in KICKS}


async def test_confirm_without_an_id_releases_nothing() -> None:
    guard, _ = await _guard_with_batch()
    client, engine = _FakeClient(), _Engine()
    await _run(_handler(client, guard, engine), _message("!confirm"))
    assert "Usage: !confirm <batch id>" in client.sends[0][1]
    assert engine.triggered == 0
    [batch] = await guard.open_batches()
    assert batch.status == "pending"


async def test_confirm_of_an_unknown_id_says_so() -> None:
    guard, _ = await _guard_with_batch()
    client, engine = _FakeClient(), _Engine()
    await _run(_handler(client, guard, engine), _message("!confirm zzzzzz"))
    assert "no open batch" in client.sends[0][1]
    assert engine.triggered == 0


async def test_reject_closes_the_batch_without_a_pass() -> None:
    guard, batch_id = await _guard_with_batch()
    client, engine = _FakeClient(), _Engine()
    handler = _handler(client, guard, engine)
    await _run(handler, _message(f"!reject {batch_id}"))
    assert "rejected" in client.sends[0][1]
    assert engine.triggered == 0
    # Wave 2 review: a rejected batch still holds its actions, so !pending shows it, marked.
    await _run(handler, _message("!pending", event_id="$cmd2"))
    assert batch_id in client.sends[1][1]
    assert "rejected by @admin:matrix.test" in client.sends[1][1]
    assert "at the latest until" in client.sends[1][1]


async def test_guard_reset_replaces_an_unreadable_state_and_starts_a_pass() -> None:
    account_data = _FakeClient()
    account_data.account_data["test.matrix.onbot.guard"] = {"batches": "broken"}
    guard = DestructiveActionGuard(
        GuardLimits(),
        MatrixAccountDataGuardStore(account_data, "@bot:matrix.test", "matrix.test"),
        EventBus(),
    )
    client, engine = _FakeClient(), _Engine()
    handler = _handler(client, guard, engine)
    await _run(handler, _message("!pending"))
    assert "cannot read its state" in client.sends[0][1]
    assert "!guard reset" in client.sends[0][1]
    await _run(handler, _message("!status", event_id="$cmd2"))
    assert "cannot read its state" in client.sends[1][1]
    await _run(handler, _message("!guard", event_id="$cmd3"))
    assert "Usage: !guard reset" in client.sends[2][1]
    assert engine.triggered == 0

    await _run(handler, _message("!guard reset", event_id="$cmd4"))
    assert "kept verbatim" in client.sends[3][1]
    assert engine.triggered == 1
    assert await guard.open_batches() == []


async def test_guard_reset_of_a_readable_state_does_nothing() -> None:
    guard, batch_id = await _guard_with_batch()
    client, engine = _FakeClient(), _Engine()
    await _run(_handler(client, guard, engine), _message("!guard reset"))
    assert "nothing to reset" in client.sends[0][1]
    assert engine.triggered == 0
    assert [b.id for b in await guard.open_batches()] == [batch_id]


async def test_a_stranger_cannot_confirm() -> None:
    guard, batch_id = await _guard_with_batch()
    client, engine = _FakeClient(), _Engine()
    await _run(_handler(client, guard, engine), _message(f"!confirm {batch_id}", sender=STRANGER))
    assert "not on the bot's admin allowlist" in client.sends[0][1]
    [batch] = await guard.open_batches()
    assert batch.status == "pending"


async def test_status_counts_the_paused_batches() -> None:
    guard, _ = await _guard_with_batch()
    client = _FakeClient()
    await _run(_handler(client, guard, _Engine()), _message("!status"))
    assert "paused in 1 batch(es), see !pending" in client.sends[0][1]


# --- the notifier -------------------------------------------------------------------------------------


def _notifier() -> tuple[AdminRoomNotifier, EventBus, _FakeClient]:
    client = _FakeClient()
    notifier = AdminRoomNotifier(client)
    bus = EventBus()
    notifier.subscribe(bus)
    return notifier, bus, client


async def test_reports_before_the_room_is_bound_are_posted_once_it_is() -> None:
    notifier, bus, client = _notifier()
    await bus.emit(Signal.destructive_actions_suppressed, action="kick", reason="r", summary="Paused: abc")
    assert client.sends == []
    await notifier.bind(ROOM)
    assert client.sends == [(ROOM, "Paused: abc")]


async def test_an_unreadable_guard_state_is_posted() -> None:
    notifier, bus, client = _notifier()
    await notifier.bind(ROOM)
    await bus.emit(Signal.guard_state_unreadable, detail="broken", text="The guard cannot read its state")
    assert client.sends == [(ROOM, "The guard cannot read its state")]


async def test_guard_and_lifecycle_reports_reach_the_room() -> None:
    notifier, bus, client = _notifier()
    await notifier.bind(ROOM)
    await bus.emit(Signal.guard_batch_closed, batch_id="abc", outcome="executed", text="Batch abc ran")
    await bus.emit(Signal.lifecycle_attention, mxid="@a:x", action="freeze", detail="left the scope")
    await bus.emit(Signal.lifecycle_attention, mxid="@a:x", action="freeze", detail="left the scope")
    await bus.emit(Signal.lifecycle_attention, mxid="@b:x", action="logout", detail="MAS 500")
    await bus.emit(Signal.lifecycle_attention, mxid=None, action="abort", detail="no users at all")
    bodies = [body for _, body in client.sends]
    assert bodies[0] == "Batch abc ran"
    assert "Offboarding of @a:x is on hold: left the scope" in bodies[1]
    assert "Offboarding step 'logout' of @b:x failed: MAS 500" in bodies[2]
    assert "Offboarding skipped this pass: no users at all" in bodies[3]
    assert len(bodies) == 4  # the repeated freeze is posted once


async def test_a_broken_room_is_posted_when_its_problems_change_not_every_pass() -> None:
    notifier, bus, client = _notifier()
    await notifier.bind(ROOM)
    report: dict[str, Any] = {
        "room_kind": "group_room",
        "room_id": "!r:x",
        "group_pk": "g",
        "problems": ["p1"],
    }
    for _ in range(3):
        await bus.emit(Signal.room_not_converged, **report)
        await bus.emit(Signal.reconcile_completed)
    assert len(client.sends) == 1
    assert "Room !r:x (group_room, group g) did not fully converge:\n- p1" in client.sends[0][1]

    await bus.emit(Signal.room_not_converged, **{**report, "problems": ["p1", "p2"]})
    assert len(client.sends) == 2

    await bus.emit(Signal.reconcile_completed)
    await bus.emit(Signal.reconcile_completed)  # a pass in which the room converged
    await bus.emit(Signal.room_not_converged, **report)
    assert len(client.sends) == 3  # broken again: news again


async def test_a_failed_post_does_not_fail_the_pass() -> None:
    class _Failing(_FakeClient):
        async def send_text_message(self, room_id: str, body: str, *, msgtype: str = "m.text") -> str:
            raise RuntimeError("synapse down")

    notifier = AdminRoomNotifier(_Failing())
    await notifier.bind(ROOM)
    await notifier.post("hello")  # logged, not raised
