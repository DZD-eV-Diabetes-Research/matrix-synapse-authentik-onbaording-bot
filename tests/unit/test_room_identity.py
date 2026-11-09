"""Room identity by creator and locked marker, the one-time alias adoption, and fail-closed (D20).

Covers :mod:`onbot.rooms.identity` and the marker lock of :mod:`onbot.rooms.provision`. The engine
scenarios that build on them are in ``test_engine.py`` and ``test_engine_robustness.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from onbot.clients.base import ApiError
from onbot.models import MatrixRoom
from onbot.rooms.identity import RoomIdentityResolver, RoomKey, classify_room, stray_claims
from onbot.rooms.provision import (
    PRIVILEGED_LOCK_LEVEL,
    ManagedRoomSpec,
    build_create_room_params,
    marker_event_types,
    markers_locked,
    with_markers_locked,
)
from onbot.state import DirectRoomState, GroupRoomState, OnbotRoomType
from tests.unit.room_fakes import BOT, SERVER, room_state

G1 = RoomKey(OnbotRoomType.group_room, "g1")
G1_ALIAS = "#g1:company.org"
MARKER = "org.company.onbot.group_room"
MOD = "@mod:company.org"


class FakeServer:
    """The Synapse admin reads and the effector writes the resolver uses, over in-memory rooms."""

    def __init__(self) -> None:
        self.rooms: dict[str, list[dict[str, Any]]] = {}
        self.creators: dict[str, str] = {}
        self.aliases: dict[str, str] = {}
        self.state_reads: list[str] = []
        self.power_writes: list[tuple[str, dict[str, Any]]] = []
        self.state_writes: list[tuple[str, str, dict[str, Any]]] = []
        self.broken: dict[str, int] = {}

    def add(self, room_id: str, state: list[dict[str, Any]], *, alias: str | None = None) -> None:
        self.rooms[room_id] = state
        self.creators[room_id] = next(e["sender"] for e in state if e["type"] == "m.room.create")
        if alias is not None:
            self.aliases[alias] = room_id

    def _event(self, room_id: str, event_type: str) -> dict[str, Any] | None:
        for event in self.rooms[room_id]:
            if event["type"] == event_type and event.get("state_key", "") == "":
                return event
        return None

    async def list_rooms(self) -> list[dict[str, Any]]:
        return [{"room_id": r, "creator": c} for r, c in self.creators.items()]

    async def get_room_state_events(self, room_id: str) -> list[dict[str, Any]]:
        self.state_reads.append(room_id)
        if room_id in self.broken:
            raise ApiError("GET", f"/rooms/{room_id}/state", self.broken[room_id])
        return [dict(e) for e in self.rooms[room_id]]

    async def resolve_room_alias(self, alias: str) -> str | None:
        return self.aliases.get(alias)

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        event = self._event(room_id, event_type)
        return None if event is None else dict(event["content"])

    async def put_room_state(self, room_id: str, event_type: str, content: dict[str, Any]) -> None:
        self.state_writes.append((room_id, event_type, content))
        event = self._event(room_id, event_type)
        if event is None:
            self.rooms[room_id].append(
                {"type": event_type, "state_key": "", "sender": BOT, "content": content}
            )
        else:
            event["content"] = content

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        event = self._event(room_id, "m.room.power_levels")
        return dict(event["content"]) if event else {}

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        self.power_writes.append((room_id, power_levels))
        event = self._event(room_id, "m.room.power_levels")
        assert event is not None
        event["content"] = power_levels


def _resolver(server: FakeServer, **kwargs: Any) -> RoomIdentityResolver:
    return RoomIdentityResolver(server, server, bot_id=BOT, server_name=SERVER, **kwargs)


# --- the lock -------------------------------------------------------------------------------------


def test_a_v12_marker_is_locked_only_above_every_human_level() -> None:
    types = marker_event_types(SERVER)
    assert not markers_locked({}, SERVER, BOT, room_version="12")
    assert not markers_locked({"events": dict.fromkeys(types, 100)}, SERVER, BOT, room_version="12")
    assert markers_locked({"events": dict.fromkeys(types, 101)}, SERVER, BOT, room_version="12")
    # One marker type left at the room default is enough to forge a second identity.
    partial = dict.fromkeys(types[:-1], PRIVILEGED_LOCK_LEVEL)
    assert not markers_locked({"events": partial}, SERVER, BOT, room_version="12")


def test_an_old_room_version_is_locked_at_the_bots_own_level() -> None:
    types = marker_event_types(SERVER)
    users = {BOT: 100, "@mod:company.org": 99}
    assert not markers_locked(
        {"users": users, "events": dict.fromkeys(types, 99)}, SERVER, BOT, room_version="10"
    )
    locked = with_markers_locked({"users": users}, SERVER, 100)
    assert markers_locked(locked, SERVER, BOT, room_version="10")
    # A high state_default is not a lock: who wrote the marker before it was raised is unknown.
    assert not markers_locked({"users": users, "state_default": 100}, SERVER, BOT, room_version="10")


def test_the_marker_is_the_last_initial_state_event() -> None:
    operator_state = {"type": MARKER, "state_key": "", "content": {"group_id": "forged"}}
    params = build_create_room_params(
        ManagedRoomSpec(
            marker=GroupRoomState(group_id="g1"),
            room_params={"preset": "private_chat", "initial_state": [operator_state]},
            extra_initial_state=({"type": "m.room.join_rules", "state_key": "", "content": {}},),
        ),
        SERVER,
    )
    types = [e["type"] for e in params["initial_state"]]
    assert types == [MARKER, "m.room.join_rules", MARKER]
    assert params["initial_state"][-1]["content"]["group_id"] == "g1"


def test_a_space_spec_marks_the_creation_content() -> None:
    from onbot.state import SpaceRoomState

    params = build_create_room_params(ManagedRoomSpec(marker=SpaceRoomState(), is_space=True), SERVER)
    assert params["creation_content"]["type"] == "m.space"


# --- classification -------------------------------------------------------------------------------


def test_a_room_the_bot_did_not_create_has_no_identity() -> None:
    state = room_state(OnbotRoomType.group_room, "g1", creator="@mallory:company.org")
    assert classify_room("!r", state, bot_id=BOT, server_name=SERVER) is None


def test_a_locked_marker_of_a_bot_room_is_trusted() -> None:
    identity = classify_room("!r", room_state(OnbotRoomType.group_room, "g1"), bot_id=BOT, server_name=SERVER)
    assert identity is not None and identity.trusted and identity.key == G1


def test_an_unlocked_marker_is_a_claim_but_not_trusted() -> None:
    identity = classify_room(
        "!r", room_state(OnbotRoomType.group_room, "g1", locked=False), bot_id=BOT, server_name=SERVER
    )
    assert identity is not None and identity.key == G1 and not identity.trusted


def test_two_markers_in_one_room_are_a_problem_not_a_claim() -> None:
    lobby = room_state(OnbotRoomType.visitor_lobby, "g2")[-1]
    identity = classify_room(
        "!r", room_state(OnbotRoomType.group_room, "g1", extra=[lobby]), bot_id=BOT, server_name=SERVER
    )
    assert identity is not None and identity.problem is not None and not identity.trusted


def test_an_unreadable_marker_is_a_problem() -> None:
    state = room_state(OnbotRoomType.group_room, content={"room_type": "group_room"})  # no group_id
    identity = classify_room("!r", state, bot_id=BOT, server_name=SERVER)
    assert identity is not None and identity.problem is not None


def test_a_notice_board_is_not_a_managed_role() -> None:
    from onbot.state import dump_room_state

    dm = {
        "type": "org.company.onbot.direct_room",
        "state_key": "",
        "sender": BOT,
        "content": dump_room_state(DirectRoomState(user_id="@a:company.org")),
    }
    identity = classify_room("!dm", room_state(extra=[dm]), bot_id=BOT, server_name=SERVER)
    assert identity is not None and identity.key is None and identity.problem is None


# --- resolving a role -----------------------------------------------------------------------------


async def test_the_locked_room_plays_its_role_whatever_its_alias_says() -> None:
    """A moderator cleared or moved the alias: the room is still the group's room (REC-04)."""
    server = FakeServer()
    server.add("!r", room_state(OnbotRoomType.group_room, "g1"))
    server.add("!other", room_state(creator="@mod:company.org"), alias=G1_ALIAS)
    rooms = _resolver(server)

    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.room is not None and resolution.room.room_id == "!r"


async def test_a_free_alias_lets_the_caller_create_the_room() -> None:
    rooms = _resolver(FakeServer())
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.may_create and resolution.room is None and resolution.problem is None


async def test_a_squatted_alias_is_reported_and_never_adopted() -> None:
    """08 check 4 A and B: the room at the alias was created by somebody else (SEC-02)."""
    server = FakeServer()
    server.add("!squat", room_state(creator="@mallory:company.org"), alias=G1_ALIAS)
    rooms = _resolver(server)

    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.room is None and not resolution.may_create
    assert resolution.problem is not None and "did not create" in resolution.problem
    assert "@mallory:company.org" in resolution.problem
    assert server.state_reads == []  # a foreign room is never even read
    assert server.power_writes == []


async def test_a_marked_bot_room_at_the_alias_is_adopted_once_and_locked() -> None:
    """The one-time migration: a room of an older version, found at the alias the config gives it."""
    server = FakeServer()
    server.add("!legacy", room_state(OnbotRoomType.group_room, "g1", locked=False), alias=G1_ALIAS)
    rooms = _resolver(server)

    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.adopted and resolution.room is not None and resolution.room.room_id == "!legacy"
    ((room_id, power),) = server.power_writes
    assert room_id == "!legacy"
    assert markers_locked(power, SERVER, BOT, room_version="12")

    # Afterwards the alias is never consulted again for it: a moderator removing it changes nothing.
    del server.aliases[G1_ALIAS]
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.room is not None and resolution.room.room_id == "!legacy"
    # ...and a fresh process (no cache) finds it by its locked marker as well.
    resolution = await _resolver(server).resolve(await _resolver(server).scan(), G1, G1_ALIAS)
    assert resolution.room is not None and resolution.room.room_id == "!legacy"


async def test_an_unmarked_bot_room_at_the_alias_is_not_adopted() -> None:
    server = FakeServer()
    server.add("!unmarked", room_state(), alias=G1_ALIAS)
    rooms = _resolver(server)
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.problem is not None and "carries no onbot marker" in resolution.problem
    assert server.power_writes == []


async def test_a_bot_room_claiming_another_group_at_the_alias_is_not_adopted() -> None:
    """REC-13: an attribute alias collision points group g1 at group g2's room."""
    server = FakeServer()
    server.add("!g2room", room_state(OnbotRoomType.group_room, "g2", locked=False), alias=G1_ALIAS)
    rooms = _resolver(server)
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.problem is not None and "room of group g2" in resolution.problem
    assert server.power_writes == []


async def test_two_rooms_claiming_one_group_are_a_conflict() -> None:
    server = FakeServer()
    server.add("!a", room_state(OnbotRoomType.group_room, "g1"))
    server.add("!b", room_state(OnbotRoomType.group_room, "g1"))
    rooms = _resolver(server)
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.room is None and not resolution.may_create
    assert resolution.problem is not None and "!a, !b" in resolution.problem


async def test_a_colliding_alias_neither_adopts_nor_creates() -> None:
    server = FakeServer()
    server.add("!legacy", room_state(OnbotRoomType.group_room, "g1", locked=False), alias=G1_ALIAS)
    rooms = _resolver(server)
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS, alias_usable=False)
    assert resolution.problem is not None and not resolution.may_create
    assert server.power_writes == []


async def test_an_unreadable_bot_room_holds_creation_and_adoption() -> None:
    """Fail closed: the unreadable room could be this group's room."""
    server = FakeServer()
    server.add("!broken", room_state(OnbotRoomType.group_room, "g1"))
    server.broken["!broken"] = 500
    rooms = _resolver(server)
    index = await rooms.scan()
    assert not index.complete
    resolution = await rooms.resolve(index, G1, G1_ALIAS)
    assert resolution.problem is not None and resolution.transient and not resolution.may_create

    del server.broken["!broken"]
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.room is not None and resolution.room.room_id == "!broken"


async def test_a_room_deleted_since_the_listing_is_not_unreadable() -> None:
    server = FakeServer()
    server.add("!gone", room_state(OnbotRoomType.group_room, "g9"))
    server.broken["!gone"] = 404
    assert (await _resolver(server).scan()).complete


async def test_a_room_upgraded_by_a_human_is_reported_and_skipped() -> None:
    server = FakeServer()
    tombstone = {
        "type": "m.room.tombstone",
        "state_key": "",
        "sender": "@admin:company.org",
        "content": {"replacement_room": "!new"},
    }
    server.add("!r", room_state(OnbotRoomType.group_room, "g1", extra=[tombstone]))
    server.add("!new", room_state(creator="@admin:company.org"))
    rooms = _resolver(server)
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.room is None and not resolution.may_create
    assert resolution.problem is not None and "upgraded" in resolution.problem


async def test_a_tombstone_set_after_the_scan_is_still_seen() -> None:
    server = FakeServer()
    server.add("!r", room_state(OnbotRoomType.group_room, "g1"))
    rooms = _resolver(server)
    await rooms.scan()  # cached as trusted
    server.rooms["!r"].append(
        {
            "type": "m.room.tombstone",
            "state_key": "",
            "sender": "@admin:company.org",
            "content": {"replacement_room": "!x"},
        }
    )
    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.problem is not None and "upgraded" in resolution.problem


async def test_an_upgrade_to_a_room_the_bot_created_as_successor_takes_over_the_role() -> None:
    server = FakeServer()
    tombstone = {
        "type": "m.room.tombstone",
        "state_key": "",
        "sender": BOT,
        "content": {"replacement_room": "!new"},
    }
    server.add("!old", room_state(OnbotRoomType.group_room, "g1", extra=[tombstone]))
    successor = room_state(locked=False)
    successor[0]["content"]["predecessor"] = {"room_id": "!old"}
    server.add("!new", successor)
    rooms = _resolver(server)

    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.room is not None and resolution.room.room_id == "!new"
    assert any(room == "!new" and event_type == MARKER for room, event_type, _ in server.state_writes)
    assert any(room == "!new" for room, _ in server.power_writes)


# --- the scan -------------------------------------------------------------------------------------


async def test_the_scan_reads_each_bot_room_once_and_skips_foreign_rooms_and_notice_boards() -> None:
    server = FakeServer()
    server.add("!r", room_state(OnbotRoomType.group_room, "g1"))
    server.add("!dm", room_state())
    server.add("!foreign", room_state(creator="@alice:company.org"))

    async def notice_boards() -> set[str]:
        return {"!dm"}

    rooms = _resolver(server, direct_rooms=notice_boards)
    await rooms.scan()
    await rooms.scan()
    assert server.state_reads == ["!r"]


async def test_an_unlocked_claim_is_read_again_on_every_scan() -> None:
    server = FakeServer()
    server.add("!legacy", room_state(OnbotRoomType.group_room, "g1", locked=False))
    rooms = _resolver(server)
    await rooms.scan()
    await rooms.scan()
    assert server.state_reads == ["!legacy", "!legacy"]


@pytest.mark.parametrize("version", ["10", "12"])
async def test_the_lock_never_lowers_an_existing_level(version: str) -> None:
    """Adopting writes the lock level; a room already locked is not written again."""
    server = FakeServer()
    server.add(
        "!legacy",
        room_state(OnbotRoomType.group_room, "g1", locked=False, room_version=version),
        alias=G1_ALIAS,
    )
    rooms = _resolver(server)
    await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert len(server.power_writes) == 1


# --- the space and the control room: one per configuration ----------------------------------------


async def test_spaces_of_two_configurations_do_not_conflict() -> None:
    server = FakeServer()
    server.add("!a", room_state(OnbotRoomType.space, "SpaceA"))
    server.add("!b", room_state(OnbotRoomType.space, "SpaceB"))
    rooms = _resolver(server)
    index = await rooms.scan()
    resolution = await rooms.resolve(
        index, RoomKey(OnbotRoomType.space, name="SpaceB"), "#SpaceB:company.org"
    )
    assert resolution.room is not None and resolution.room.room_id == "!b"


async def test_a_legacy_space_is_adopted_by_alias_and_learns_its_configured_alias() -> None:
    server = FakeServer()
    legacy = room_state(
        OnbotRoomType.space, content={"schema_version": 1, "room_type": "space"}, locked=False
    )
    server.add("!space", legacy, alias="#OnBotSpace:company.org")
    key = RoomKey(OnbotRoomType.space, name="OnBotSpace")
    rooms = _resolver(server)

    resolution = await rooms.resolve(await rooms.scan(), key, "#OnBotSpace:company.org")
    assert resolution.adopted and resolution.room is not None and resolution.room.room_id == "!space"
    ((_room, event_type, content),) = server.state_writes
    assert event_type == "org.company.onbot.space" and content["configured_alias"] == "OnBotSpace"

    # A fresh process finds it by marker alone.
    del server.aliases["#OnBotSpace:company.org"]
    fresh = _resolver(server)
    resolution = await fresh.resolve(await fresh.scan(), key, "#OnBotSpace:company.org")
    assert resolution.room is not None and resolution.room.room_id == "!space"


async def test_a_successor_with_another_role_does_not_take_over() -> None:
    server = FakeServer()
    tombstone = {
        "type": "m.room.tombstone",
        "state_key": "",
        "sender": BOT,
        "content": {"replacement_room": "!new"},
    }
    server.add("!old", room_state(OnbotRoomType.group_room, "g1", extra=[tombstone]))
    successor = room_state(OnbotRoomType.group_room, "g2")
    successor[0]["content"]["predecessor"] = {"room_id": "!old"}
    server.add("!new", successor)
    rooms = _resolver(server)

    resolution = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert resolution.room is None and resolution.problem is not None
    assert server.state_writes == []


# --- wave 3 review: who wrote the marker, followed upgrades, a failed lock -------------------------


def _rewritten_by(state: list[dict[str, Any]], sender: str, **content: Any) -> list[dict[str, Any]]:
    """``state`` after ``sender`` rewrote its group marker (Matrix records the writer as sender)."""
    for event in state:
        if event["type"] == MARKER:
            event["sender"] = sender
            event["content"] = {**event["content"], **content}
    return state


def test_a_marker_written_by_somebody_else_is_never_a_claim() -> None:
    state = _rewritten_by(room_state(OnbotRoomType.group_room, "g1"), MOD)
    identity = classify_room("!r", state, bot_id=BOT, server_name=SERVER)
    assert identity is not None and not identity.trusted and identity.key is None
    assert identity.problem is not None and MOD in identity.problem


async def test_a_stray_legacy_room_rewritten_by_a_moderator_is_not_adopted() -> None:
    """A never-adopted (unlocked) legacy room: a moderator points its marker at group g2 and gives it
    g2's alias. The bot neither adopts nor locks it, and does not create a second room beside it."""
    server = FakeServer()
    legacy = room_state(OnbotRoomType.group_room, "g1", locked=False)
    server.add("!stray", _rewritten_by(legacy, MOD, group_id="g2"), alias="#g2:company.org")
    rooms = _resolver(server)
    index = await rooms.scan()
    g2 = RoomKey(OnbotRoomType.group_room, "g2")
    resolution = await rooms.resolve(index, g2, "#g2:company.org")
    assert resolution.room is None and not resolution.may_create
    assert resolution.problem is not None and MOD in resolution.problem
    assert server.power_writes == [] and server.state_writes == []
    assert [i.room_id for i in stray_claims(index)] == ["!stray"]  # reported


@pytest.mark.parametrize("version", ["10", "12"])
async def test_a_locked_marker_rewritten_by_a_human_is_no_teardown_candidate(version: str) -> None:
    """Before version 12 a human at the bot's level can rewrite even a locked marker, for example to
    a group that is gone, which would have the room torn down and deleted."""
    server = FakeServer()
    state = room_state(OnbotRoomType.group_room, "g1", room_version=version, users={MOD: 100})
    server.add("!r", _rewritten_by(state, MOD, group_id="vanished"))
    rooms = _resolver(server)
    index = await rooms.scan()
    assert index.trusted() == []
    resolution = await rooms.resolve(index, RoomKey(OnbotRoomType.group_room, "vanished"), None)
    assert resolution.room is None


def _upgraded_server() -> FakeServer:
    server = FakeServer()
    tombstone = {
        "type": "m.room.tombstone",
        "state_key": "",
        "sender": BOT,
        "content": {"replacement_room": "!new"},
    }
    server.add("!old", room_state(OnbotRoomType.group_room, "g1", extra=[tombstone]))
    successor = room_state(locked=False)
    successor[0]["content"]["predecessor"] = {"room_id": "!old"}
    server.add("!new", successor)
    return server


async def test_a_followed_upgrade_stays_followed_on_the_next_pass() -> None:
    """The old room keeps its locked marker; that is no conflict with the successor that took over."""
    server = _upgraded_server()
    rooms = _resolver(server)
    first = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert first.room is not None and first.room.room_id == "!new"
    second = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert second.problem is None
    assert second.room is not None and second.room.room_id == "!new"


async def test_a_followed_upgrade_stays_followed_after_a_restart() -> None:
    server = _upgraded_server()
    rooms = _resolver(server)
    await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    fresh = _resolver(server)
    resolution = await fresh.resolve(await fresh.scan(), G1, G1_ALIAS)
    assert resolution.problem is None
    assert resolution.room is not None and resolution.room.room_id == "!new"


async def test_an_upgrade_seen_only_live_stays_followed_on_the_next_pass() -> None:
    """The old room was cached before its upgrade; the tombstone is only read live."""
    server = FakeServer()
    server.add("!old", room_state(OnbotRoomType.group_room, "g1"))
    rooms = _resolver(server)
    await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)  # cached as trusted, no tombstone
    tombstone = {
        "type": "m.room.tombstone",
        "state_key": "",
        "sender": BOT,
        "content": {"replacement_room": "!new"},
    }
    server.rooms["!old"].append(tombstone)
    successor = room_state(locked=False)
    successor[0]["content"]["predecessor"] = {"room_id": "!old"}
    server.add("!new", successor)
    first = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert first.room is not None and first.room.room_id == "!new"
    second = await rooms.resolve(await rooms.scan(), G1, G1_ALIAS)
    assert second.room is not None and second.room.room_id == "!new", second.problem


async def test_a_lock_that_failed_at_creation_is_retried_on_the_next_pass() -> None:
    """register() no longer records a new room as locked: its real state is read, and the lock is
    retried for the room this process created, even without an alias to adopt it by."""
    server = FakeServer()
    server.add("!new", room_state(OnbotRoomType.group_room, "g1", locked=False))  # the lock failed
    rooms = _resolver(server)
    rooms.register(MatrixRoom(room_id="!new"), G1)

    identity = await rooms.identify("!new")
    assert identity is not None and not identity.locked

    resolution = await rooms.resolve(await rooms.scan(), G1, None)
    assert resolution.room is not None and resolution.room.room_id == "!new"
    assert not resolution.may_create
    ((room_id, power),) = server.power_writes
    assert room_id == "!new" and markers_locked(power, SERVER, BOT, room_version="12")
    assert (await rooms.identify("!new")).trusted  # type: ignore[union-attr]


async def test_a_lock_retry_that_fails_again_skips_the_role_for_the_pass() -> None:
    server = FakeServer()
    server.add("!new", room_state(OnbotRoomType.group_room, "g1", locked=False))
    rooms = _resolver(server)
    rooms.register(MatrixRoom(room_id="!new"), G1)

    async def refuse(room_id: str, power_levels: dict[str, Any]) -> None:
        raise ApiError("PUT", "/state/m.room.power_levels", 502)

    server.set_room_power_levels = refuse  # type: ignore[method-assign]
    with pytest.raises(ApiError):
        await rooms.resolve(await rooms.scan(), G1, None)
