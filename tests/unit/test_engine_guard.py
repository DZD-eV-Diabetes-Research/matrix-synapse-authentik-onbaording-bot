"""The destructive-action guard inside the reconcile pass (review session S3: REC-01, LIF-08, D10, D13).

A pass first records everything it would destroy, the guard decides, and only then is the permitted
part carried out. These tests drive whole passes against stateful fakes: rooms remember their members
and power levels, so "zero kicks" and "zero withdrawals" are observed, not inferred.
"""

from __future__ import annotations

from typing import Any

from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.reconciler.engine import ReconcilerEngine
from onbot.reconciler.state import granted_power_levels_type
from tests.unit.test_engine import _BASE, FakeAdmin, RecordingEffectors

ROOM1 = "!room1:company.org"
ROOM2 = "!room2:company.org"
SPACE = "!space:company.org"
BOT = "@bot:company.org"
PL_ATTR = "chat-systemwide-powerlevel"


def mx(local: str) -> str:
    return f"@{local}:company.org"


class Directory:
    """Authentik: users in groups g1 and g2; ``visible`` narrows what the API token sees (REC-01)."""

    def __init__(self, g1: list[str], g2: list[str]) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        for name in g1:
            self.add(name, "g1")
        for name in g2:
            self.add(name, "g2")
        self.visible: set[str] | None = None
        self.groups = [
            {"pk": "g1", "name": "Team", "attributes": {PL_ATTR: 50}},
            {"pk": "g2", "name": "Other", "attributes": {}},
        ]

    def add(self, name: str, group: str) -> None:
        self.users[name] = {
            "username": name,
            "pk": f"{name}-pk",
            "is_active": True,
            "is_superuser": False,
            "groups_obj": [{"pk": group}],
        }

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        users = [u for n, u in self.users.items() if self.visible is None or n in self.visible]
        flag = kwargs.get("filter_is_active", True)
        if flag is not None:
            users = [u for u in users if u["is_active"] is flag]
        return [dict(u) for u in users]

    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        # The group object carries its members' pks whatever the token may see of the users.
        return [
            dict(g, users=[u["pk"] for u in self.users.values() if {"pk": g["pk"]} in u["groups_obj"]])
            for g in self.groups
        ]


class Rooms(FakeAdmin):
    """Synapse admin API over rooms that remember their members."""

    def __init__(self, directory: Directory) -> None:
        super().__init__()
        self.directory = directory
        self.members: dict[str, set[str]] = {ROOM1: {BOT}, ROOM2: {BOT}, SPACE: {BOT}}

    async def list_users(self) -> list[dict[str, Any]]:
        return [{"name": mx(n)} for n in self.directory.users] + [{"name": BOT}]

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return [
            {"room_id": ROOM1, "canonical_alias": "#g1:company.org", "name": "Team"},
            {"room_id": ROOM2, "canonical_alias": "#g2:company.org", "name": "Other"},
        ]

    async def list_room_members(self, room_id: str) -> list[str]:
        return sorted(self.members.setdefault(room_id, {BOT}))

    async def add_user_to_room(self, room_id: str, user_id: str) -> None:
        await super().add_user_to_room(room_id, user_id)
        self.members.setdefault(room_id, {BOT}).add(user_id)


class LiveEffectors(RecordingEffectors):
    """CS API over the same rooms: kicks remove members, power levels are kept."""

    def __init__(self, rooms: Rooms) -> None:
        super().__init__()
        self.rooms = rooms
        self.levels: dict[str, dict[str, Any]] = {}

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
        await super().kick_user(room_id, user_id, reason)
        self.rooms.members[room_id].discard(user_id)

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        return {**self.levels.get(room_id, {}), "users": dict(self.levels.get(room_id, {}).get("users", {}))}

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        await super().set_room_power_levels(room_id, power_levels)
        self.levels[room_id] = power_levels


def world(
    g1: list[str], g2: list[str], **config: Any
) -> tuple[ReconcilerEngine, Directory, Rooms, LiveEffectors, list[Event]]:
    directory = Directory(g1, g2)
    rooms = Rooms(directory)
    effectors = LiveEffectors(rooms)
    bus = EventBus()
    seen: list[Event] = []

    async def collect(event: Event) -> None:
        seen.append(event)

    bus.subscribe(Signal.destructive_actions_suppressed, collect)
    bus.subscribe(Signal.guard_batch_closed, collect)
    engine = ReconcilerEngine(
        OnbotConfig.model_validate({**_BASE, **config}),
        directory,  # type: ignore[arg-type]
        rooms,  # type: ignore[arg-type]
        effectors,
        bus,
    )
    return engine, directory, rooms, effectors, seen


def levels(effectors: LiveEffectors, room: str) -> dict[str, Any]:
    return dict(effectors.levels.get(room, {}).get("users", {}))


# --- REC-01: a token that suddenly sees one of several users ----------------------------------------


async def test_a_narrowed_token_kicks_nobody_withdraws_nothing_and_proposes_a_batch() -> None:
    engine, directory, rooms, effectors, seen = world(["alice", "bob", "dan"], ["carol"])
    await engine.reconcile_once()
    assert rooms.members[ROOM1] == {BOT, mx("alice"), mx("bob"), mx("dan")}
    assert levels(effectors, ROOM1) == {mx("alice"): 50, mx("bob"): 50, mx("dan"): 50}

    directory.visible = {"carol"}  # 08 check 3: object-level view_user on one user only
    await engine.reconcile_once()
    await engine.reconcile_once()  # and again: still nothing, and no second report

    assert effectors.kicks == []
    assert rooms.members[ROOM1] == {BOT, mx("alice"), mx("bob"), mx("dan")}
    assert levels(effectors, ROOM1) == {mx("alice"): 50, mx("bob"): 50, mx("dan"): 50}
    [proposed] = [e for e in seen if e.signal is Signal.destructive_actions_suppressed]
    assert proposed.payload["action"] == "kick,withdraw_power_level"
    assert "from 4 to 1" in proposed.payload["reason"]
    assert f"!confirm {proposed.payload['batch_id']}" in proposed.payload["summary"]
    # Room upkeep goes on: carol is still joined where she belongs.
    assert mx("carol") in rooms.members[ROOM2]


async def test_confirming_the_batch_carries_it_out_on_the_next_pass() -> None:
    engine, directory, _rooms, effectors, seen = world(["alice", "bob", "dan"], ["carol"])
    await engine.reconcile_once()
    directory.visible = {"carol"}
    await engine.reconcile_once()
    batch_id = seen[0].payload["batch_id"]

    ok, _ = await engine.guard.confirm(batch_id, "@admin:company.org")
    assert ok
    await engine.reconcile_once()

    assert sorted(effectors.kicks) == [(ROOM1, mx(n)) for n in ("alice", "bob", "dan")]
    assert levels(effectors, ROOM1) == {}
    closed = [e for e in seen if e.signal is Signal.guard_batch_closed]
    assert closed[0].payload["outcome"] == "executed"
    # The confirmed counts are the new normal: the next pass changes nothing and proposes nothing.
    await engine.reconcile_once()
    assert len([e for e in seen if e.signal is Signal.destructive_actions_suppressed]) == 1


async def test_confirmation_skips_what_no_longer_applies() -> None:
    engine, directory, _rooms, effectors, seen = world(["alice", "bob", "dan"], ["carol"])
    await engine.reconcile_once()
    directory.visible = {"carol"}
    await engine.reconcile_once()
    batch_id = seen[0].payload["batch_id"]

    directory.visible = {"carol", "alice"}  # alice is visible again before the admin confirms
    await engine.guard.confirm(batch_id, "@admin:company.org")
    await engine.reconcile_once()

    assert sorted(effectors.kicks) == [(ROOM1, mx("bob")), (ROOM1, mx("dan"))]
    assert levels(effectors, ROOM1) == {mx("alice"): 50}


async def test_withdrawals_are_guarded_also_when_kicks_are_off() -> None:
    """Wave 1 review: a collapsed answer suppressed kicks but still withdrew every recorded grant."""
    engine, directory, _rooms, effectors, seen = world(
        ["alice", "bob", "dan"],
        ["carol"],
        sync_authentik_users_with_matrix_rooms={
            "kick_matrix_room_members_not_in_mapped_authentik_group_anymore": False
        },
    )
    await engine.reconcile_once()
    directory.visible = {"carol"}
    await engine.reconcile_once()

    assert levels(effectors, ROOM1) == {mx("alice"): 50, mx("bob"): 50, mx("dan"): 50}
    # Still recorded as the bot's own grants, so they can be withdrawn once confirmed.
    record = effectors.account_data[(ROOM1, granted_power_levels_type("company.org"))]
    assert record["users"] == {mx("alice"): 50, mx("bob"): 50, mx("dan"): 50}
    [proposed] = [e for e in seen if e.signal is Signal.destructive_actions_suppressed]
    assert proposed.payload["action"] == "withdraw_power_level"


async def test_an_empty_group_list_tears_down_no_room() -> None:
    engine, directory, rooms, effectors, seen = world(
        ["alice"],
        ["carol"],
        sync_matrix_rooms_based_on_authentik_groups={
            "disable_rooms_when_mapped_authentik_group_disappears": True,
            "delete_disabled_rooms": True,
        },
    )
    for room, group in ((ROOM1, "g1"), (ROOM2, "g2")):
        effectors.state_store[(room, "org.company.onbot.group_room")] = {
            "room_type": "group_room",
            "group_id": group,
        }
    await engine.reconcile_once()
    directory.groups = []  # Authentik answers 200 with no groups
    await engine.reconcile_once()

    assert rooms.blocked_changes == []
    assert rooms.deleted == []
    assert effectors.kicks == []
    [proposed] = [e for e in seen if e.signal is Signal.destructive_actions_suppressed]
    assert proposed.payload["action"] == "room_teardown"
    assert "no Authentik groups mapped to rooms" in proposed.payload["reason"]


# --- layer 2: more than the per-pass limit ----------------------------------------------------------


async def test_more_kicks_than_the_limit_wait_for_a_confirmation() -> None:
    staff = [f"u{n:02d}" for n in range(30)]
    engine, directory, _rooms, effectors, seen = world(staff, ["carol"])
    await engine.reconcile_once()

    for name in staff[:11]:  # 11 people leave the group at once; the user count stays the same
        directory.users[name]["groups_obj"] = [{"pk": "g2"}]
    await engine.reconcile_once()
    assert effectors.kicks == []
    [proposed] = [e for e in seen if e.signal is Signal.destructive_actions_suppressed]
    assert "22 reversible destructive actions" in proposed.payload["reason"]  # kicks plus withdrawals


async def test_up_to_the_limit_runs_without_a_confirmation() -> None:
    staff = [f"u{n:02d}" for n in range(30)]
    engine, directory, _rooms, effectors, seen = world(
        staff,
        ["carol"],
        sync_matrix_rooms_based_on_authentik_groups={"authentik_group_attr_for_matrix_power_level": "none"},
    )
    await engine.reconcile_once()
    for name in staff[:10]:
        directory.users[name]["groups_obj"] = [{"pk": "g2"}]
    await engine.reconcile_once()
    assert len(effectors.kicks) == 10
    assert seen == []
