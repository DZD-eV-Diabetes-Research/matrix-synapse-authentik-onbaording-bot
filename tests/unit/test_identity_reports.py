"""Identity findings reach the admin room, and conflicting accounts are frozen by the lifecycle (D8)."""

from __future__ import annotations

from onbot.admin.notifier import AdminRoomNotifier
from onbot.events import EventBus, Signal
from onbot.lifecycle.accounts import (
    CONFLICT_REASON,
    AccountStatus,
    DirectorySnapshot,
    DirectoryUser,
    KnownUser,
    KnownUsers,
    LifecycleEntry,
    LifecycleLedger,
    classify_accounts,
)
from tests.unit.test_control_room import ROOM, _FakeClient

ALICE = "@alice:company.org"


async def test_an_identity_report_is_posted_into_the_admin_room() -> None:
    client = _FakeClient()
    notifier = AdminRoomNotifier(client)
    bus = EventBus()
    notifier.subscribe(bus)
    await notifier.bind(ROOM)
    await bus.emit(Signal.identity_attention, text="2 Authentik users resolve to @alice")
    assert client.sends == [(ROOM, "User identity: 2 Authentik users resolve to @alice")]


def test_an_account_in_a_conflict_is_frozen_even_while_offboarding() -> None:
    snapshot = DirectorySnapshot(
        users=(DirectoryUser("1", "alice", None, False, True, False),),
        matrix_mxids=frozenset({ALICE}),
        excluded_mxids=frozenset(),
        held_mxids=frozenset({ALICE}),
    )
    known = KnownUsers(users={"1": KnownUser(mxid=ALICE, first_seen=1.0)})
    ledger = LifecycleLedger(entries={ALICE: LifecycleEntry(marked_for_disabling_timestamp=1.0)})
    result = classify_accounts(snapshot, known, ledger)
    assert result[ALICE].status is AccountStatus.frozen
    assert result[ALICE].reason == CONFLICT_REASON
