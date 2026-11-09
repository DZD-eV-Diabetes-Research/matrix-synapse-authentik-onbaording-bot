"""Rooms converge back to their shape on every pass, not only at creation (session S7).

REC-09: the space link, the onbot marker (and its lock) and the alias were written once at creation
and never repaired. REC-10: switching a lobby off abandoned it open, and a lobby a human closed was
reopened on the next tick (D16 decides it stays closed). REC-11: the bot unblocked rooms a server
admin had blocked.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from onbot.admin.notifier import AdminRoomNotifier
from onbot.clients.base import ApiError
from onbot.clients.synapse_admin import RoomBlock
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.reconciler.engine import ReconcilerEngine
from onbot.rooms.identity import RoomIdentityResolver
from onbot.state import (
    GroupRoomState,
    OnbotRoomType,
    SpaceRoomState,
    VisitorLobbyRoomState,
    dump_room_state,
)
from tests.unit.room_fakes import room_state
from tests.unit.test_engine import (
    _BASE,
    BOT,
    LOCKED_EVENTS,
    AvatarAuthentik,
    FakeAdmin,
    FakeAuthentik,
    LobbyAdmin,
    OrphanAdmin,
    OrphanLobbyAdmin,
    RecordingEffectors,
    _lobby_config,
)

ROOM1 = "!room1:company.org"
SPACE = "!space:company.org"
LOBBY = "!lobby1:company.org"
ORPHAN = "!orphan:company.org"
ORPHAN_LOBBY = "!orphanlobby:company.org"
HUMAN_ADMIN = "@admin:company.org"
MODERATOR = "@mod:company.org"
GROUP_MARKER = "org.company.onbot.group_room"
LOBBY_MARKER = "org.company.onbot.visitor_lobby"
SPACE_MARKER = "org.company.onbot.space"
JOIN_RULES = "m.room.join_rules"
CHILD = "m.space.child"
PARENT = "m.space.parent"
RESTRICTED = {"join_rule": "restricted", "allow": [{"type": "m.room_membership", "room_id": SPACE}]}


def _engine(
    admin: FakeAdmin | None = None,
    effectors: RecordingEffectors | None = None,
    *,
    config: OnbotConfig | None = None,
    authentik: Any = None,
) -> tuple[ReconcilerEngine, Any, RecordingEffectors, list[Event]]:
    bus = EventBus()
    attention: list[Event] = []

    async def collect(event: Event) -> None:
        attention.append(event)

    bus.subscribe(Signal.room_attention, collect)
    admin = admin or FakeAdmin()
    effectors = effectors or RecordingEffectors()
    engine = ReconcilerEngine(
        config or OnbotConfig.model_validate(_BASE),
        authentik or FakeAuthentik(),
        admin,  # type: ignore[arg-type]
        effectors,
        bus,
    )
    return engine, admin, effectors, attention


def _seed_converged(effectors: RecordingEffectors, room_id: str = ROOM1, *, suggested: bool = False) -> None:
    """A room as the bot left it: its marker (and the space's), the space link both ways."""
    effectors.state_store.setdefault(
        (SPACE, SPACE_MARKER), dump_room_state(SpaceRoomState(configured_alias="OnBotSpace"))
    )
    if room_id == ROOM1:
        effectors.state_store[(room_id, GROUP_MARKER)] = dump_room_state(GroupRoomState(group_id="g1"))
    else:
        effectors.state_store[(room_id, LOBBY_MARKER)] = dump_room_state(VisitorLobbyRoomState(group_id="g1"))
        _seed_converged(effectors)  # and the group room beside it
    effectors.state_store[(SPACE, CHILD, room_id)] = {"suggested": suggested, "via": ["company.org"]}
    effectors.state_store[(room_id, PARENT, SPACE)] = {"canonical": True, "via": ["company.org"]}


def _writes(effectors: RecordingEffectors, room_id: str) -> list[str]:
    return [event_type for room, event_type, _content in effectors.state_writes if room == room_id]


# --- REC-11: a block a server admin set ----------------------------------------------------------


async def test_a_room_a_server_admin_blocked_stays_blocked_and_is_left_alone() -> None:
    engine, admin, effectors, attention = _engine()
    admin.blocks[ROOM1] = HUMAN_ADMIN

    await engine.reconcile_once()
    await engine.reconcile_once()

    assert admin.blocked_changes == []  # never unblocked
    assert all(room != ROOM1 for room, _ in admin.added)  # nobody joined into it
    assert all(room != ROOM1 for room, _ in effectors.kicks)
    assert all(room != ROOM1 for room, _ in effectors.power_levels)
    assert ("!space:company.org", "@bob:company.org") in admin.added  # the rest of the pass ran
    (report,) = [e.payload["text"] for e in attention if ROOM1 in e.payload["text"]]  # once
    assert f"blocked by {HUMAN_ADMIN}" in report


async def test_a_room_the_bot_blocked_is_unblocked_when_its_group_returns() -> None:
    engine, admin, _effectors, attention = _engine()
    admin.blocks[ROOM1] = BOT

    await engine.reconcile_once()

    assert admin.blocked_changes == [(ROOM1, False)]
    assert (ROOM1, "@bob:company.org") in admin.added
    assert not [e for e in attention if "blocked" in e.payload["text"]]


async def test_a_block_without_a_recorded_owner_is_left_in_place() -> None:
    engine, admin, _effectors, _attention = _engine()
    admin.blocks[ROOM1] = None

    await engine.reconcile_once()

    assert admin.blocked_changes == []


async def test_a_lobby_the_bot_blocked_is_unblocked_with_its_group() -> None:
    """Teardown blocks the lobby with its group room; before S7 only the group room came back."""
    admin = LobbyAdmin(with_lobby_room=True)
    admin.blocks = {ROOM1: BOT, LOBBY: BOT}
    engine, admin, _effectors, _attention = _engine(admin, config=_lobby_config())

    await engine.reconcile_once()

    assert sorted(admin.blocked_changes) == [(LOBBY, False), (ROOM1, False)]


class FlakyBlockAdmin(FakeAdmin):
    """Synapse answers the block status of ROOM1 with a 500."""

    async def get_room_block(self, room_id: str) -> RoomBlock:
        if room_id == ROOM1:
            raise ApiError("GET", f"/rooms/{room_id}/block", 500)
        return await super().get_room_block(room_id)


async def test_a_room_whose_block_cannot_be_read_is_left_alone_this_pass() -> None:
    """W4R-04: the room was resolved before its block was checked, so a failed read left a room a
    server admin may have blocked fully managed (REC-11 failed open): kicks, joins, power levels."""
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, "m.room.topic")] = {"topic": "old"}
    admin = FlakyBlockAdmin()
    admin.blocks[ROOM1] = HUMAN_ADMIN  # a server admin's block the bot cannot read this pass
    engine, admin, effectors, _attention = _engine(admin, effectors)

    await engine.reconcile_once()

    assert all(room != ROOM1 for room, _ in effectors.kicks)
    assert all(room != ROOM1 for room, _ in admin.added)
    assert all(room != ROOM1 for room, _ in effectors.power_levels)
    assert _writes(effectors, ROOM1) == []
    assert admin.blocked_changes == []
    (problem,) = engine.last_pass_report.rooms
    assert problem.group_pk == "g1" and "room setup failed" in problem.problems[0]
    assert ("!space:company.org", "@bob:company.org") in admin.added  # the rest of the pass ran


# --- D23: an obsolete room somebody else blocked is a hold --------------------------------------------


def _held_orphan(
    *, delete: bool = False, limit: int = 10
) -> tuple[ReconcilerEngine, Any, RecordingEffectors, list[Event]]:
    config = OnbotConfig.model_validate(
        {**_BASE, "destructive_action_guard": {"max_reversible_actions_per_pass": limit}}
    )
    rooms = config.sync_matrix_rooms_based_on_authentik_groups
    rooms.disable_rooms_when_mapped_authentik_group_disappears = True
    rooms.delete_disabled_rooms = delete
    admin = OrphanAdmin()
    admin.room_state_events = {ORPHAN: room_state(OnbotRoomType.group_room, "g-gone")}
    admin.blocks[ORPHAN] = HUMAN_ADMIN  # moderation or a legal hold, then the group went away
    return _engine(admin, config=config)


async def test_an_obsolete_room_a_server_admin_blocked_is_neither_cleared_nor_deleted() -> None:
    """W4R-02, D23: the teardown kicked every member and `delete_disabled_rooms` purged the room."""
    engine, admin, effectors, attention = _held_orphan(delete=True)

    await engine.reconcile_once()
    await engine.reconcile_once()

    assert all(room != ORPHAN for room, _ in effectors.kicks)
    assert admin.deleted == []
    assert admin.blocked_changes == [] and admin.blocks[ORPHAN] == HUMAN_ADMIN
    (report,) = [e.payload["text"] for e in attention if ORPHAN in e.payload["text"]]  # once
    assert f"{HUMAN_ADMIN} blocked it" in report and "D23" in report


async def test_a_held_room_does_not_count_against_the_limit() -> None:
    """Planned again on every pass, the held room's teardown kept the one kick in ROOM1 paused."""
    engine, _admin, effectors, _attention = _held_orphan(limit=1)

    await engine.reconcile_once()

    assert effectors.kicks == [(ROOM1, "@stale:company.org")]


async def test_a_held_room_is_torn_down_once_the_block_is_lifted() -> None:
    engine, admin, effectors, _attention = _held_orphan(delete=True)
    await engine.reconcile_once()

    del admin.blocks[ORPHAN]  # the server admin lifts the block
    await engine.reconcile_once()

    assert (ORPHAN, True) in admin.blocked_changes
    assert {mxid for room, mxid in effectors.kicks if room == ORPHAN} == {
        "@alice:company.org",
        "@stale:company.org",
    }
    assert admin.deleted == [ORPHAN]


async def test_a_block_set_between_planning_and_teardown_stops_the_teardown() -> None:
    """The guard decides after planning; a hold that appears meanwhile still wins (D23)."""
    config = OnbotConfig.model_validate(_BASE)
    rooms = config.sync_matrix_rooms_based_on_authentik_groups
    rooms.disable_rooms_when_mapped_authentik_group_disappears = True
    rooms.delete_disabled_rooms = True

    class LateBlockAdmin(OrphanAdmin):
        reads = 0

        async def get_room_block(self, room_id: str) -> RoomBlock:
            if room_id == ORPHAN:
                self.reads += 1
                if self.reads > 1:  # after the planning read
                    self.blocks[ORPHAN] = HUMAN_ADMIN
            return await super().get_room_block(room_id)

    admin = LateBlockAdmin()
    admin.room_state_events = {ORPHAN: room_state(OnbotRoomType.group_room, "g-gone")}
    engine, admin, effectors, _attention = _engine(admin, config=config)

    await engine.reconcile_once()

    assert all(room != ORPHAN for room, _ in effectors.kicks)
    assert admin.deleted == [] and admin.blocked_changes == []
    assert any(r.room_id == ORPHAN and "D23" in "; ".join(r.problems) for r in engine.last_pass_report.rooms)


# --- REC-09: space link -----------------------------------------------------------------------------


async def test_a_room_missing_from_the_space_is_linked_again() -> None:
    """The child link is a second request after createRoom; a failure or restart in between lost it."""
    engine, _admin, effectors, _attention = _engine()

    await engine.reconcile_once()

    assert effectors.state_store[(SPACE, CHILD, ROOM1)] == {"suggested": False, "via": ["company.org"]}
    assert effectors.state_store[(ROOM1, PARENT, SPACE)] == {"canonical": True, "via": ["company.org"]}
    writes = len(effectors.state_writes)
    await engine.reconcile_once()
    assert len(effectors.state_writes) == writes  # converged: reads only


async def test_a_lobby_is_linked_as_suggested() -> None:
    effectors = RecordingEffectors()
    effectors.state_store[(LOBBY, JOIN_RULES)] = RESTRICTED
    engine, _admin, effectors, _attention = _engine(
        LobbyAdmin(with_lobby_room=True), effectors, config=_lobby_config()
    )

    await engine.reconcile_once()

    assert effectors.state_store[(SPACE, CHILD, LOBBY)] == {"suggested": True, "via": ["company.org"]}


async def test_a_converged_room_gets_no_writes() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    engine, _admin, effectors, attention = _engine(effectors=effectors)

    await engine.reconcile_once()

    assert _writes(effectors, ROOM1) == []
    assert not any(
        room == SPACE and event_type.startswith(CHILD) for room, event_type, _ in effectors.state_writes
    )
    assert attention == []


OLD_SPACE = "!oldspace:company.org"
HUMAN_SPACE = "!department:company.org"


async def test_the_links_to_an_earlier_space_go_when_the_room_is_linked_into_a_recreated_one() -> None:
    """S7 leftover: after the space was recreated, the room kept naming the old space as its parent."""
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    # The room as the old space left it, and not yet in the new one.
    del effectors.state_store[(SPACE, CHILD, ROOM1)]
    del effectors.state_store[(ROOM1, PARENT, SPACE)]
    effectors.state_store[(ROOM1, PARENT, OLD_SPACE)] = {"canonical": True, "via": ["company.org"]}
    effectors.state_store[(OLD_SPACE, CHILD, ROOM1)] = {"suggested": False, "via": ["company.org"]}
    # A link a human added to a space of their own stays.
    effectors.state_store[(ROOM1, PARENT, HUMAN_SPACE)] = {"via": ["company.org"]}
    effectors.senders[(ROOM1, PARENT, HUMAN_SPACE)] = MODERATOR
    engine, _admin, effectors, _attention = _engine(SharedStateAdmin(effectors), effectors)

    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, PARENT, SPACE)] == {"canonical": True, "via": ["company.org"]}
    assert effectors.state_store[(ROOM1, PARENT, OLD_SPACE)] == {}
    assert effectors.state_store[(OLD_SPACE, CHILD, ROOM1)] == {}
    assert effectors.state_store[(ROOM1, PARENT, HUMAN_SPACE)] == {"via": ["company.org"]}
    writes = len(effectors.state_writes)
    await engine.reconcile_once()
    assert len(effectors.state_writes) == writes  # converged: reads only


async def test_a_stale_link_into_an_earlier_space_the_bot_left_is_not_a_room_problem() -> None:
    class OldSpaceGone(RecordingEffectors):
        async def get_room_state(
            self, room_id: str, event_type: str, state_key: str = ""
        ) -> dict[str, Any] | None:
            if room_id == OLD_SPACE:
                raise ApiError("GET", f"/rooms/{room_id}/state", 403, {"errcode": "M_FORBIDDEN"})
            return await super().get_room_state(room_id, event_type, state_key)

    effectors = OldSpaceGone()
    _seed_converged(effectors)
    del effectors.state_store[(ROOM1, PARENT, SPACE)]
    effectors.state_store[(ROOM1, PARENT, OLD_SPACE)] = {"canonical": True, "via": ["company.org"]}
    engine, _admin, effectors, _attention = _engine(SharedStateAdmin(effectors), effectors)

    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, PARENT, OLD_SPACE)] == {}
    assert all(not r.problems for r in engine.last_pass_report.rooms)


def _linked_into_the_old_space(effectors: RecordingEffectors) -> None:
    """ROOM1 as the old space left it, and not yet in the current one."""
    _seed_converged(effectors)
    del effectors.state_store[(SPACE, CHILD, ROOM1)]
    del effectors.state_store[(ROOM1, PARENT, SPACE)]
    effectors.state_store[(ROOM1, PARENT, OLD_SPACE)] = {"canonical": True, "via": ["company.org"]}
    effectors.state_store[(OLD_SPACE, CHILD, ROOM1)] = {"suggested": False, "via": ["company.org"]}


class _FlakyOldSpace(RecordingEffectors):
    """The first write that removes a link between ROOM1 and the old space fails with a 502."""

    fail_on: tuple[str, str, str] = (ROOM1, PARENT, OLD_SPACE)

    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def put_room_state(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None:
        if self.fail and (room_id, event_type, state_key) == self.fail_on and content == {}:
            self.fail = False
            raise ApiError("PUT", "/state", 502, {"errcode": "M_UNKNOWN"})
        await super().put_room_state(room_id, event_type, content, state_key)


@pytest.mark.parametrize("fail_on", [(ROOM1, PARENT, OLD_SPACE), (OLD_SPACE, CHILD, ROOM1)])
async def test_a_failed_removal_of_a_link_to_an_earlier_space_is_retried(
    fail_on: tuple[str, str, str],
) -> None:
    """W8R-03: the cleanup ran only in the pass that wrote the current parent link, so one failed
    write left the old links for good."""
    effectors = _FlakyOldSpace()
    effectors.fail_on = fail_on
    _linked_into_the_old_space(effectors)
    engine, _admin, effectors, _attention = _engine(SharedStateAdmin(effectors), effectors)

    await engine.reconcile_once()
    (problem,) = [r for r in engine.last_pass_report.rooms if r.room_id == ROOM1]
    assert problem.problems == ["space link repair failed: HTTP 502 M_UNKNOWN"]

    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, PARENT, OLD_SPACE)] == {}
    assert effectors.state_store[(OLD_SPACE, CHILD, ROOM1)] == {}
    assert all(not r.problems for r in engine.last_pass_report.rooms)
    writes = len(effectors.state_writes)
    await engine.reconcile_once()
    assert len(effectors.state_writes) == writes  # done: no more cleanup


async def test_a_child_link_a_human_wrote_into_the_earlier_space_stays() -> None:
    """W8R-04: the old space's child link was removed whoever wrote it."""
    effectors = RecordingEffectors()
    _linked_into_the_old_space(effectors)
    effectors.state_store[(OLD_SPACE, CHILD, ROOM1)] = {"suggested": True, "via": ["company.org"]}
    effectors.senders[(OLD_SPACE, CHILD, ROOM1)] = MODERATOR  # added to the old space by hand
    engine, _admin, effectors, _attention = _engine(SharedStateAdmin(effectors), effectors)

    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, PARENT, OLD_SPACE)] == {}  # the bot's own parent link goes
    assert effectors.state_store[(OLD_SPACE, CHILD, ROOM1)] == {"suggested": True, "via": ["company.org"]}


# --- REC-09: marker and lock -----------------------------------------------------------------------


class _SharedState:
    """The admin Room State API serves the markers (and join rules) the effectors hold, so the identity
    scan and the repair see one room (W4R-06: the repair tests used to pass only because the scan kept
    seeing the intact marker while the effectors showed it removed)."""

    effectors: RecordingEffectors

    async def get_room_state_events(self, room_id: str) -> list[dict[str, Any]]:
        events = [
            e
            for e in await super().get_room_state_events(room_id)  # type: ignore[misc]
            if not _shared(e["type"])
        ]
        for key, content in self.effectors.state_store.items():
            if key[0] == room_id and _shared(key[1]):
                sender = self.effectors.senders.get(key, BOT)
                state_key = key[2] if len(key) == 3 else ""
                events.append({"type": key[1], "state_key": state_key, "sender": sender, "content": content})
        return events


class SharedStateAdmin(_SharedState, FakeAdmin):
    def __init__(self, effectors: RecordingEffectors) -> None:
        super().__init__()
        self.effectors = effectors


class SharedLobbyAdmin(_SharedState, LobbyAdmin):
    """:class:`SharedStateAdmin` over the g1 room and its visitor lobby."""

    def __init__(self, effectors: RecordingEffectors) -> None:
        super().__init__(with_lobby_room=True)
        self.effectors = effectors


def _is_marker(event_type: str) -> bool:
    return event_type.startswith("org.company.onbot.")


def _shared(event_type: str) -> bool:
    return _is_marker(event_type) or event_type in (JOIN_RULES, PARENT, CHILD)


def _running(
    effectors: RecordingEffectors | None = None, **kwargs: Any
) -> tuple[ReconcilerEngine, Any, RecordingEffectors, list[Event]]:
    """An engine whose scan and repair share one room state, over converged rooms."""
    effectors = effectors or RecordingEffectors()
    _seed_converged(effectors)
    return _engine(SharedStateAdmin(effectors), effectors, **kwargs)


async def test_a_removed_marker_is_restored_and_reported_once() -> None:
    engine, _admin, effectors, attention = _running()
    await engine.reconcile_once()
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}  # somebody at the bot's level removed it

    await engine.reconcile_once()
    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, GROUP_MARKER)]["group_id"] == "g1"
    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT
    (report,) = [e.payload["text"] for e in attention]
    assert "marker was removed" in report


async def test_a_marker_rewritten_to_another_group_is_restored_from_what_the_bot_knows() -> None:
    engine, _admin, effectors, attention = _running()
    await engine.reconcile_once()
    effectors.state_store[(ROOM1, GROUP_MARKER)] = dump_room_state(GroupRoomState(group_id="g-other"))
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR

    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, GROUP_MARKER)]["group_id"] == "g1"
    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT
    assert any("another role" in e.payload["text"] for e in attention)


async def test_a_marker_somebody_else_wrote_is_rewritten_by_the_bot() -> None:
    """Same content, wrong sender: after a restart the scan would no longer trust it (W3R)."""
    engine, _admin, effectors, attention = _running()
    await engine.reconcile_once()
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR

    await engine.reconcile_once()

    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT
    assert effectors.state_store[(ROOM1, GROUP_MARKER)] == dump_room_state(GroupRoomState(group_id="g1"))
    assert any(f"rewritten by {MODERATOR}" in e.payload["text"] for e in attention)


# --- W4R-08: what a restored marker contains ------------------------------------------------------


def _marker_writes(effectors: RecordingEffectors, since: int) -> list[dict[str, Any]]:
    return [c for room, t, c in effectors.state_writes[since:] if (room, t) == (ROOM1, GROUP_MARKER)]


def _with_global_admin() -> OnbotConfig:
    config = OnbotConfig.model_validate(_BASE)
    config.global_room_admins = ["carol"]
    return config


async def test_a_removed_marker_is_restored_with_what_the_bot_wrote_into_it() -> None:
    """A fresh marker dropped `global_admins` (still a withdrawal source, D10) and
    `avatar_source_url` (the avatar was fetched and uploaded again)."""
    engine, _admin, effectors, _attention = _running(config=_with_global_admin(), authentik=AvatarAuthentik())
    await engine.reconcile_once()
    before = effectors.state_store[(ROOM1, GROUP_MARKER)]
    assert before["global_admins"] == ["@carol:company.org"]
    assert before["avatar_source_url"] == "https://cdn/t.png"
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    writes = len(effectors.state_writes)

    await engine.reconcile_once()

    (restored,) = _marker_writes(effectors, writes)  # one write: the restore, nothing to fix after it
    assert restored == before
    assert effectors.uploads == ["https://cdn/t.png"]  # not uploaded again


async def test_a_same_role_marker_somebody_else_wrote_is_not_signed_by_the_bot() -> None:
    """The foreign content (a human-chosen `global_admins`) used to be re-signed verbatim."""
    engine, _admin, effectors, _attention = _running(config=_with_global_admin())
    await engine.reconcile_once()
    before = effectors.state_store[(ROOM1, GROUP_MARKER)]
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {**before, "global_admins": [MODERATOR]}
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR
    writes = len(effectors.state_writes)

    await engine.reconcile_once()

    (restored,) = _marker_writes(effectors, writes)
    assert restored == before and MODERATOR not in restored["global_admins"]


# --- W4R-06: a marker lost while the bot did not run ------------------------------------------------


def _restarted(
    effectors: RecordingEffectors,
) -> tuple[ReconcilerEngine, Any, RecordingEffectors, list[Event]]:
    """A fresh process over ROOM1 as it is now; the group's alias still points at it."""
    effectors.aliases["#g1:company.org"] = ROOM1
    return _engine(SharedStateAdmin(effectors), effectors)


async def test_a_marker_removed_while_the_bot_was_stopped_is_reported_with_what_to_do() -> None:
    """A fresh process cannot tell the room from one that never was the group's room, so it does not
    take it back (D20). Before, the group was skipped with a bare "carries no onbot marker"."""
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    engine, admin, effectors, _attention = _restarted(effectors)

    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, GROUP_MARKER)] == {}  # not taken back on a guess
    assert effectors.created == []  # and no second room
    assert all(room != ROOM1 for room, _ in effectors.kicks + admin.added)
    problems = {r.room_id: "; ".join(r.problems) for r in engine.last_pass_report.rooms}
    assert "which the bot created, but its onbot group_room marker was removed" in problems[None]
    assert "restores the marker as the bot account or moves the alias away" in problems[None]
    assert "docs/troubleshooting.md" in problems[ROOM1]  # the room itself, as a stray


async def test_a_marker_a_human_restores_as_the_bot_counts_from_the_next_pass() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    marker = effectors.state_store[(ROOM1, GROUP_MARKER)]
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    engine, admin, effectors, _attention = _restarted(effectors)
    await engine.reconcile_once()

    effectors.state_store[(ROOM1, GROUP_MARKER)] = marker  # a PUT with the bot's access token
    await engine.reconcile_once()

    assert (ROOM1, "@bob:company.org") in admin.added
    assert engine.last_pass_report.rooms == []


async def test_a_marker_rewritten_while_the_bot_was_stopped_is_reported_with_what_to_do() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR
    engine, _admin, effectors, _attention = _restarted(effectors)

    await engine.reconcile_once()

    assert effectors.senders[(ROOM1, GROUP_MARKER)] == MODERATOR
    (group,) = ["; ".join(r.problems) for r in engine.last_pass_report.rooms if r.room_id is None]
    assert f"the bot created, but it carries an onbot group_room marker written by {MODERATOR}" in group


async def test_an_incomplete_marker_from_an_older_version_is_completed_quietly() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {
        "room_type": "group_room",
        "group_id": "g1",
        "authentik_server": "https://authentik.internal/",  # SEC-11: no longer written
    }
    engine, _admin, effectors, attention = _engine(effectors=effectors)

    await engine.reconcile_once()

    marker = effectors.state_store[(ROOM1, GROUP_MARKER)]
    assert "authentik_server" not in marker and marker["group_id"] == "g1"
    assert attention == []


async def test_a_lowered_marker_lock_is_raised_again() -> None:
    class Unlocked(RecordingEffectors):
        raw: ClassVar[list[dict[str, Any]]] = []

        async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
            if room_id == ROOM1:
                return {"users": {}, "events": {**LOCKED_EVENTS, GROUP_MARKER: 0}}
            return await super().get_room_power_levels(room_id)

        async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
            self.raw.append(power_levels)
            await super().set_room_power_levels(room_id, power_levels)

    effectors = Unlocked()
    _seed_converged(effectors)
    engine, _admin, effectors, attention = _engine(effectors=effectors)

    await engine.reconcile_once()

    assert isinstance(effectors, Unlocked)
    assert effectors.raw and effectors.raw[0]["events"][GROUP_MARKER] == 1000
    assert any("power level required to change its onbot marker" in e.payload["text"] for e in attention)


# --- REC-09: alias (S6 leftover) -------------------------------------------------------------------


class NoAliasAdmin(FakeAdmin):
    async def fake_rooms(self) -> list[dict[str, Any]]:
        return [{"room_id": ROOM1, "canonical_alias": None, "name": "Team"}]


async def test_a_removed_alias_is_recreated_when_it_is_free() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    engine, _admin, effectors, _attention = _engine(NoAliasAdmin(), effectors)

    await engine.reconcile_once()

    assert effectors.alias_writes == [("#g1:company.org", ROOM1)]
    assert effectors.state_store[(ROOM1, "m.room.canonical_alias")] == {"alias": "#g1:company.org"}


async def test_an_alias_another_room_took_is_reported_and_left_there() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.aliases["#g1:company.org"] = "!squat:company.org"
    engine, _admin, effectors, attention = _engine(NoAliasAdmin(), effectors)

    await engine.reconcile_once()
    await engine.reconcile_once()

    assert effectors.alias_writes == []
    assert (ROOM1, "m.room.canonical_alias") not in effectors.state_store
    (report,) = [e.payload["text"] for e in attention]
    assert "#g1:company.org" in report and "!squat:company.org" in report
    assert (ROOM1, "@bob:company.org") in _admin.added  # the room is still managed


# --- REC-10: a lobby switched off -------------------------------------------------------------------


class SwitchedOffLobbyAdmin(LobbyAdmin):
    def __init__(self) -> None:
        super().__init__(with_lobby_room=True)


def _switched_off(**guard: Any) -> tuple[ReconcilerEngine, Any, RecordingEffectors, list[Event]]:
    config = OnbotConfig.model_validate({**_BASE, "destructive_action_guard": guard} if guard else _BASE)
    effectors = RecordingEffectors()
    effectors.state_store[(LOBBY, JOIN_RULES)] = RESTRICTED
    _seed_converged(effectors, LOBBY, suggested=True)
    return _engine(SwitchedOffLobbyAdmin(), effectors, config=config)  # lobby off by default


async def test_a_switched_off_lobby_is_closed_and_detached_without_kicks() -> None:
    engine, admin, effectors, _attention = _switched_off()

    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == {"join_rule": "invite"}
    assert effectors.state_store[(SPACE, CHILD, LOBBY)] == {}
    assert effectors.state_store[(LOBBY, PARENT, SPACE)] == {}
    assert all(room != LOBBY for room, _ in effectors.kicks)
    assert all(room != LOBBY for room, _ in admin.added)

    writes = len(effectors.state_writes)
    await engine.reconcile_once()
    assert len(effectors.state_writes) == writes  # closed and detached: nothing more to do


async def test_closing_a_lobby_goes_through_the_guard() -> None:
    """D19: closing or detaching a room is a reversible destructive action (layer 2)."""
    engine, _admin, effectors, _attention = _switched_off(max_reversible_actions_per_pass=0)

    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED
    assert effectors.state_store[(SPACE, CHILD, LOBBY)]["via"] == ["company.org"]


CAROL = "@carol:company.org"
GRANTS = "org.company.onbot.granted_power_levels"


class _CarolAdminInLobby(RecordingEffectors):
    """The lobby's power levels as the bot left them: the global admin carol seated at 100."""

    def __init__(self) -> None:
        super().__init__()
        self.lobby_users: dict[str, int] = {CAROL: 100}
        self.account_data[(LOBBY, GRANTS)] = {"users": {CAROL: 100}}

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        power = await super().get_room_power_levels(room_id)
        return {**power, "users": dict(self.lobby_users)} if room_id == LOBBY else power

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        await super().set_room_power_levels(room_id, power_levels)
        if room_id == LOBBY:
            self.lobby_users = dict(power_levels.get("users") or {})


@pytest.mark.parametrize("still_global_admin", [True, False])
async def test_a_switched_off_lobby_loses_the_levels_the_bot_granted_there(still_global_admin: bool) -> None:
    """S7 leftover: a closed lobby is no longer managed, so a grant left there would stay for good."""
    config = OnbotConfig.model_validate(_BASE)  # lobbies off by default
    if still_global_admin:
        config.global_room_admins = ["carol"]
    effectors = _CarolAdminInLobby()
    effectors.state_store[(LOBBY, JOIN_RULES)] = RESTRICTED
    _seed_converged(effectors, LOBBY, suggested=True)
    engine, _admin, effectors, _attention = _engine(SwitchedOffLobbyAdmin(), effectors, config=config)
    assert isinstance(effectors, _CarolAdminInLobby)

    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == {"join_rule": "invite"}
    assert CAROL not in effectors.lobby_users
    assert effectors.account_data[(LOBBY, GRANTS)]["users"] == {}
    lobby_writes = [w for w in effectors.power_levels if w[0] == LOBBY]
    await engine.reconcile_once()
    assert [w for w in effectors.power_levels if w[0] == LOBBY] == lobby_writes  # nothing more to do


async def test_withdrawing_the_grants_of_a_switched_off_lobby_goes_through_the_guard() -> None:
    effectors = _CarolAdminInLobby()
    effectors.state_store[(LOBBY, JOIN_RULES)] = RESTRICTED
    _seed_converged(effectors, LOBBY, suggested=True)
    config = OnbotConfig.model_validate(
        {**_BASE, "destructive_action_guard": {"max_reversible_actions_per_pass": 0}}
    )
    engine, _admin, effectors, _attention = _engine(SwitchedOffLobbyAdmin(), effectors, config=config)
    assert isinstance(effectors, _CarolAdminInLobby)

    await engine.reconcile_once()

    assert effectors.lobby_users == {CAROL: 100}


def _carol_switched_off(
    admin: SwitchedOffLobbyAdmin | None = None, config: OnbotConfig | None = None
) -> tuple[ReconcilerEngine, Any, _CarolAdminInLobby, list[Event]]:
    effectors = _CarolAdminInLobby()
    effectors.state_store[(LOBBY, JOIN_RULES)] = RESTRICTED
    _seed_converged(effectors, LOBBY, suggested=True)
    engine, admin, _effectors, attention = _engine(
        admin or SwitchedOffLobbyAdmin(), effectors, config=config or OnbotConfig.model_validate(_BASE)
    )
    return engine, admin, effectors, attention


async def test_a_switched_off_lobby_a_server_admin_blocked_is_a_hold() -> None:
    """W8R-01 (D23): the block check only ran for wanted lobbies, so a switched-off lobby under a
    legal hold was closed and lost the levels the bot granted there."""
    admin = SwitchedOffLobbyAdmin()
    admin.blocks[LOBBY] = HUMAN_ADMIN
    engine, admin, effectors, attention = _carol_switched_off(admin)

    await engine.reconcile_once()
    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED
    assert effectors.state_store[(SPACE, CHILD, LOBBY)]["via"] == ["company.org"]
    assert effectors.lobby_users == {CAROL: 100}
    assert admin.blocked_changes == []
    (report,) = [e.payload["text"] for e in attention if LOBBY in e.payload["text"]]  # once
    assert "blocked it, not the bot" in report and HUMAN_ADMIN in report and "D23" in report
    assert all(r.room_id != LOBBY for r in engine.last_pass_report.rooms)  # a hold, not a failure

    del admin.blocks[LOBBY]  # the hold ends
    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == {"join_rule": "invite"}
    assert CAROL not in effectors.lobby_users


async def test_a_switched_off_lobby_blocked_by_the_bot_is_still_closed() -> None:
    admin = SwitchedOffLobbyAdmin()
    admin.blocks[LOBBY] = BOT
    engine, admin, effectors, _attention = _carol_switched_off(admin)

    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == {"join_rule": "invite"}
    assert CAROL not in effectors.lobby_users


class _FlakyLobbyBlockAdmin(SwitchedOffLobbyAdmin):
    async def get_room_block(self, room_id: str) -> RoomBlock:
        if room_id == LOBBY:
            raise ApiError("GET", f"/rooms/{room_id}/block", 500)
        return await super().get_room_block(room_id)


async def test_a_switched_off_lobby_whose_block_cannot_be_read_is_left_alone_this_pass() -> None:
    """W8R-01 with W4R-04 semantics: an unreadable block may be a server admin's."""
    engine, _admin, effectors, _attention = _carol_switched_off(_FlakyLobbyBlockAdmin())

    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED
    assert effectors.state_store[(SPACE, CHILD, LOBBY)]["via"] == ["company.org"]
    assert effectors.lobby_users == {CAROL: 100}
    (problem,) = [r for r in engine.last_pass_report.rooms if r.room_id == LOBBY]
    assert problem.problems == ["block check failed: HTTP 500"]


async def test_a_switched_off_lobby_keeps_its_levels_while_user_sync_is_off() -> None:
    """W8R-02 (D28): with user sync off the bot leaves every room's power levels alone; the
    switched-off lobby's withdrawal ignored that. The lobby is still closed."""
    sync = {**(_BASE.get("sync_authentik_users_with_matrix_rooms") or {}), "enabled": False}
    config = OnbotConfig.model_validate({**_BASE, "sync_authentik_users_with_matrix_rooms": sync})
    engine, _admin, effectors, _attention = _carol_switched_off(config=config)

    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == {"join_rule": "invite"}
    assert effectors.lobby_users == {CAROL: 100}
    assert effectors.account_data[(LOBBY, GRANTS)]["users"] == {CAROL: 100}


async def test_a_lobby_the_bot_closed_opens_again_when_switched_back_on() -> None:
    engine, _admin, effectors, _attention = _switched_off()
    await engine.reconcile_once()

    engine.config.matrix_room_default_settings.visitor_lobby_enabled = True
    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED
    assert effectors.state_store[(SPACE, CHILD, LOBBY)] == {"suggested": True, "via": ["company.org"]}
    assert effectors.state_store[(LOBBY, PARENT, SPACE)]["via"] == ["company.org"]


async def test_a_lobby_of_a_group_that_disappeared_is_torn_down_not_closed() -> None:
    config = _lobby_config()
    rooms = config.sync_matrix_rooms_based_on_authentik_groups
    rooms.disable_rooms_when_mapped_authentik_group_disappears = True
    admin = OrphanLobbyAdmin()
    admin.room_state_events = {ORPHAN_LOBBY: room_state(OnbotRoomType.visitor_lobby, "g-gone")}
    engine, admin, effectors, _attention = _engine(admin, config=config)

    await engine.reconcile_once()

    assert (ORPHAN_LOBBY, True) in admin.blocked_changes
    assert (ORPHAN_LOBBY, JOIN_RULES) not in effectors.state_store


# --- D16: a lobby a human closed -------------------------------------------------------------------


def _manually_closed(
    join_rule: str = "invite",
) -> tuple[ReconcilerEngine, Any, RecordingEffectors, list[Event]]:
    effectors = RecordingEffectors()
    effectors.state_store[(LOBBY, JOIN_RULES)] = {"join_rule": join_rule}
    effectors.senders[(LOBBY, JOIN_RULES)] = MODERATOR
    _seed_converged(effectors, LOBBY, suggested=True)
    return _engine(LobbyAdmin(with_lobby_room=True), effectors, config=_lobby_config())


async def test_a_lobby_a_human_closed_stays_closed_and_is_reported_once() -> None:
    engine, _admin, effectors, attention = _manually_closed()

    await engine.reconcile_once()
    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == {"join_rule": "invite"}
    assert JOIN_RULES not in _writes(effectors, LOBBY)
    (report,) = [e.payload["text"] for e in attention]
    assert f"closed by {MODERATOR}" in report and "!lobby reopen g1" in report


async def test_a_lobby_a_human_opened_to_everyone_is_restricted_again() -> None:
    """D16 covers closing; a lobby made public is outside what the bot governs and is reverted."""
    engine, _admin, effectors, _attention = _manually_closed("public")

    await engine.reconcile_once()

    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED


async def test_reopen_withdraws_the_manual_close() -> None:
    engine, _admin, effectors, _attention = _manually_closed()
    await engine.reconcile_once()

    reply = await engine.reopen_lobby("g1")

    assert reply.startswith("Reopened the visitor lobby")
    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED
    assert effectors.senders[(LOBBY, JOIN_RULES)] == BOT
    # From now on the bot manages it again: a later close by the bot's own config is not "manual".
    await engine.reconcile_once()
    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED


async def test_reopen_by_room_id_and_refusals() -> None:
    engine, _admin, effectors, _attention = _manually_closed()
    assert "no reconcile pass has finished yet" in await engine.reopen_lobby("g1")
    await engine.reconcile_once()

    assert "not a visitor lobby the bot manages" in await engine.reopen_lobby("g-unknown")
    assert "not a visitor lobby the bot manages" in await engine.reopen_lobby(ROOM1)
    assert (await engine.reopen_lobby(LOBBY)).startswith("Reopened")
    assert "not closed by hand" in await engine.reopen_lobby(LOBBY)
    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED


async def test_reopen_names_the_group_in_any_form_of_its_uuid() -> None:
    """W6R-01: `!lobby reopen <group>` compared the raw string, so the 32 hex digit form missed."""
    engine, _admin, effectors, _attention = _manually_closed()
    await engine.reconcile_once()
    engine.active_lobbies = {LOBBY: "1120a6e1-124f-309b-be96-c8be5fb09eab"}  # as Authentik names it

    reply = await engine.reopen_lobby("1120A6E1124F309BBE96C8BE5FB09EAB")

    assert reply.startswith("Reopened the visitor lobby"), reply
    assert effectors.state_store[(LOBBY, JOIN_RULES)] == RESTRICTED


# --- W4R-07: one refused repair does not stop the others -------------------------------------------


class RefusingEffectors(RecordingEffectors):
    """The server refuses the state writes named in ``refused`` ((room, event type) pairs)."""

    def __init__(self, *refused: tuple[str, str]) -> None:
        super().__init__()
        self.refused = set(refused)

    async def put_room_state(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None:
        if (room_id, event_type) in self.refused:
            raise ApiError("PUT", f"/rooms/{room_id}/state/{event_type}", 403, {"errcode": "M_FORBIDDEN"})
        await super().put_room_state(room_id, event_type, content, state_key)


async def test_a_refused_space_repair_does_not_keep_new_users_out_of_the_space() -> None:
    """The space's upkeep and joining users into it were one step: a refused marker restore (or an
    alias an appservice reserved) failed it on every pass, and nobody new got into the space."""
    effectors = RefusingEffectors((SPACE, SPACE_MARKER))
    _seed_converged(effectors)
    del effectors.state_store[(SPACE, SPACE_MARKER)]
    engine, admin, effectors, _attention = _engine(effectors=effectors)

    await engine.reconcile_once()

    assert (SPACE, "@bob:company.org") in admin.added
    (problem,) = [r for r in engine.last_pass_report.rooms if r.room_id == SPACE]
    assert len(problem.problems) == 1 and problem.problems[0].startswith("marker repair failed")


async def test_a_refused_repair_of_a_group_room_does_not_skip_the_others() -> None:
    """A refused `m.space.parent` used to end the upkeep there: no avatar for the room."""
    effectors = RefusingEffectors((ROOM1, PARENT))
    _seed_converged(effectors)
    del effectors.state_store[(ROOM1, PARENT, SPACE)]
    del effectors.state_store[(SPACE, CHILD, ROOM1)]
    engine = ReconcilerEngine(
        OnbotConfig.model_validate(_BASE),
        AvatarAuthentik(),
        FakeAdmin(),  # type: ignore[arg-type]
        effectors,
        EventBus(),
    )

    await engine.reconcile_once()

    assert effectors.state_store[(SPACE, CHILD, ROOM1)]["via"] == ["company.org"]  # before the refusal
    assert effectors.avatars == [(ROOM1, "mxc://company.org/1")]  # after it
    (problem,) = [r for r in engine.last_pass_report.rooms if r.room_id == ROOM1]
    assert len(problem.problems) == 1 and problem.problems[0].startswith("space link repair failed")


async def test_room_findings_are_posted_in_the_admin_room() -> None:
    sends: list[tuple[str, str]] = []

    class Client:
        async def send_text_message(self, room_id: str, text: str, *, msgtype: str = "m.text") -> str:
            sends.append((room_id, text))
            return "$e"

    notifier = AdminRoomNotifier(Client())
    bus = EventBus()
    notifier.subscribe(bus)
    await notifier.bind("!control:company.org")
    await bus.emit(Signal.room_attention, text="the visitor lobby !l of group g1 was closed by @mod")
    assert sends == [("!control:company.org", "Rooms: the visitor lobby !l of group g1 was closed by @mod")]


# --- a room the bot closed already is not torn down again ------------------------------------------


class ClosedOrphanAdmin(OrphanAdmin):
    """The orphan room as a teardown left it: blocked by the bot, nobody but the bot in it."""

    async def list_room_members(self, room_id: str) -> list[str]:
        if room_id == ORPHAN and room_id in self.blocks:
            return [BOT]
        return await super().list_room_members(room_id)


def _closed_orphan(
    *, delete: bool = False, limit: int = 10
) -> tuple[ReconcilerEngine, Any, RecordingEffectors]:
    config = OnbotConfig.model_validate(
        {**_BASE, "destructive_action_guard": {"max_reversible_actions_per_pass": limit}}
    )
    rooms = config.sync_matrix_rooms_based_on_authentik_groups
    rooms.disable_rooms_when_mapped_authentik_group_disappears = True
    rooms.delete_disabled_rooms = delete
    admin = ClosedOrphanAdmin()
    admin.room_state_events = {ORPHAN: room_state(OnbotRoomType.group_room, "g-gone")}
    admin.blocks[ORPHAN] = BOT
    engine, admin, effectors, _attention = _engine(admin, config=config)
    return engine, admin, effectors


async def test_closed_rooms_do_not_count_against_the_limit_on_every_pass() -> None:
    """Every room ever closed used to be torn down again on every pass. From eleven of them on,
    every kick and teardown of every pass waited for a confirmation, for good."""
    engine, admin, effectors = _closed_orphan(limit=1)

    await engine.reconcile_once()

    assert admin.blocked_changes == []  # not blocked again
    # The one kick in the live room is within the limit of 1; with the closed room's teardown
    # planned again it made two actions, and both were paused.
    assert effectors.kicks == [(ROOM1, "@stale:company.org")]


LEGACY_ADMIN = "@legacy-admin:company.org"


class StuckMemberOrphanAdmin(ClosedOrphanAdmin):
    """A teardown left one member the bot cannot kick (here a v12 additional creator)."""

    newcomer: str | None = None

    async def list_room_members(self, room_id: str) -> list[str]:
        if room_id == ORPHAN and room_id in self.blocks:
            return [BOT, LEGACY_ADMIN, *([self.newcomer] if self.newcomer else [])]
        return await super().list_room_members(room_id)


def _stuck_orphan(limit: int) -> tuple[ReconcilerEngine, StuckMemberOrphanAdmin, RecordingEffectors]:
    config = OnbotConfig.model_validate(
        {**_BASE, "destructive_action_guard": {"max_reversible_actions_per_pass": limit}}
    )
    rooms = config.sync_matrix_rooms_based_on_authentik_groups
    rooms.disable_rooms_when_mapped_authentik_group_disappears = True
    admin = StuckMemberOrphanAdmin()
    admin.room_state_events = {ORPHAN: room_state(OnbotRoomType.group_room, "g-gone")}
    admin.blocks[ORPHAN] = BOT
    effectors = RecordingEffectors()
    effectors.state_store[(ORPHAN, "m.room.create")] = {
        "room_version": "12",
        "additional_creators": [LEGACY_ADMIN],
    }
    engine, admin, effectors, _attention = _engine(admin, effectors, config=config)
    assert isinstance(admin, StuckMemberOrphanAdmin)
    return engine, admin, effectors


async def test_a_closed_room_with_a_member_the_bot_cannot_kick_is_not_torn_down_again() -> None:
    """W4R-01: closed meant "only the bot is left", so a room keeping a member nobody can kick (a v12
    additional creator, a PL 100 human of a migrated room) was torn down again on every pass, and
    from eleven of them on every pass tripped layer 2."""
    engine, admin, effectors = _stuck_orphan(limit=1)

    for _ in range(3):
        await engine.reconcile_once()

    assert admin.blocked_changes == []
    assert all(room != ORPHAN for room, _ in effectors.kicks)
    # Within the limit of 1 on every pass (the fake keeps the kicked member listed).
    assert effectors.kicks == [(ROOM1, "@stale:company.org")] * 3


async def test_a_closed_room_somebody_kickable_joined_is_torn_down_again() -> None:
    engine, admin, effectors = _stuck_orphan(limit=10)
    admin.newcomer = "@alice:company.org"  # back in the blocked room somehow (an admin join)

    await engine.reconcile_once()

    assert (ORPHAN, "@alice:company.org") in effectors.kicks
    assert (ORPHAN, LEGACY_ADMIN) not in effectors.kicks


async def test_a_closed_room_is_still_deleted_once_deletion_is_switched_on() -> None:
    engine, admin, _effectors = _closed_orphan(delete=True)

    await engine.reconcile_once()

    assert admin.deleted == [ORPHAN]
    assert admin.blocked_changes == []


# --- W5R-01: no marker is built from what is read in the room ------------------------------------


class FlakyMarkerRead(RecordingEffectors):
    """The marker read of the repair step fails once (a 502 after retries)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    async def get_room_state_event(self, room_id: str, event_type: str, state_key: str = "") -> Any:
        if self.fail_next and (room_id, event_type) == (ROOM1, GROUP_MARKER):
            self.fail_next = False
            raise ApiError("GET", "/state", 502, {})
        return await super().get_room_state_event(room_id, event_type, state_key)


def _avatar_engine(effectors: RecordingEffectors, config: OnbotConfig | None = None) -> ReconcilerEngine:
    return ReconcilerEngine(
        config or OnbotConfig.model_validate(_BASE),
        AvatarAuthentik(),  # type: ignore[arg-type]
        SharedStateAdmin(effectors),  # type: ignore[arg-type]
        effectors,
        EventBus(),
    )


def _rewrite_to_a_gone_group(effectors: RecordingEffectors) -> None:
    """Somebody above the marker lock names another group and another avatar URL."""
    effectors.state_store[(ROOM1, GROUP_MARKER)] = dump_room_state(
        GroupRoomState(group_id="g-gone", avatar_source_url="https://evil/x.png")
    )
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR


async def test_a_failed_marker_repair_never_lets_the_avatar_step_sign_a_foreign_marker() -> None:
    effectors = FlakyMarkerRead()
    _seed_converged(effectors)
    engine = _avatar_engine(effectors)
    await engine.reconcile_once()
    assert effectors.state_store[(ROOM1, GROUP_MARKER)]["group_id"] == "g1"

    _rewrite_to_a_gone_group(effectors)
    effectors.fail_next = True  # the repair cannot read the marker this pass
    await engine.reconcile_once()

    assert effectors.senders[(ROOM1, GROUP_MARKER)] == MODERATOR  # not re-signed by the bot
    known = engine.rooms.known_marker(ROOM1)
    assert known is not None and known["group_id"] == "g1"

    await engine.reconcile_once()  # the next pass repairs it from what the bot knows
    assert effectors.state_store[(ROOM1, GROUP_MARKER)]["group_id"] == "g1"
    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT


async def test_a_restart_after_a_failed_repair_does_not_tear_down_the_room_as_the_gone_group() -> None:
    effectors = FlakyMarkerRead()
    _seed_converged(effectors)
    config = OnbotConfig.model_validate(_BASE)
    rooms_cfg = config.sync_matrix_rooms_based_on_authentik_groups
    rooms_cfg.disable_rooms_when_mapped_authentik_group_disappears = True
    engine = _avatar_engine(effectors, config)
    await engine.reconcile_once()
    _rewrite_to_a_gone_group(effectors)
    effectors.fail_next = True
    await engine.reconcile_once()

    effectors.aliases["#g1:company.org"] = ROOM1
    restarted = _avatar_engine(effectors, config)
    effectors.kicks.clear()
    await restarted.reconcile_once()

    assert [k for k in effectors.kicks if k[0] == ROOM1] == []
    assert restarted.rooms.known_marker(ROOM1) is None  # a foreign marker is never trusted
    assert isinstance(restarted.admin, SharedStateAdmin)
    assert [b for b in restarted.admin.blocked_changes if b[0] == ROOM1] == []


async def test_the_power_level_step_writes_the_known_marker_not_the_one_in_the_room() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    engine = _avatar_engine(effectors)
    await engine.reconcile_once()
    # The marker is rewritten after the upkeep read it, before the power-level step writes it.
    original = engine._converge_room_upkeep

    async def rewrite_after_upkeep(room: Any, *args: Any, **kwargs: Any) -> None:
        await original(room, *args, **kwargs)
        if room.room_id == ROOM1:
            _rewrite_to_a_gone_group(effectors)

    engine._converge_room_upkeep = rewrite_after_upkeep  # type: ignore[method-assign]
    engine.config.global_room_admins = ["carol"]  # a new global admin: the marker records them
    effectors.state_writes.clear()
    await engine.reconcile_once()

    written = [c for r, t, c in effectors.state_writes if (r, t) == (ROOM1, GROUP_MARKER)]
    assert written, "the power-level step wrote the marker"
    assert all(c["group_id"] == "g1" for c in written)


def test_the_identity_cache_refuses_a_marker_of_another_role() -> None:
    from onbot.rooms.identity import RoomIdentity, RoomIdentityResolver, RoomKey

    rooms = RoomIdentityResolver(None, None, bot_id=BOT, server_name="company.org")  # type: ignore[arg-type]
    rooms._cache[ROOM1] = RoomIdentity(
        room_id=ROOM1,
        key=RoomKey(OnbotRoomType.group_room, "g1"),
        locked=True,
        marker_content={"room_type": "group_room", "group_id": "g1"},
    )
    foreign = {"room_type": "group_room", "group_id": "g-gone"}
    rooms.remember_marker(ROOM1, foreign)
    assert rooms.known_marker(ROOM1) == {"room_type": "group_room", "group_id": "g1"}
    assert rooms.marker_conflict(ROOM1, foreign) is not None
    own = {"room_type": "group_room", "group_id": "g1", "global_admins": ["@a:x"]}
    rooms.remember_marker(ROOM1, own)
    assert rooms.known_marker(ROOM1) == own


async def test_a_refused_marker_restore_does_not_set_the_avatar_on_every_pass() -> None:
    effectors = RefusingEffectors()
    _seed_converged(effectors)
    engine = _avatar_engine(effectors)
    await engine.reconcile_once()
    effectors.avatars.clear()
    effectors.refused.add((ROOM1, GROUP_MARKER))
    del effectors.state_store[(ROOM1, GROUP_MARKER)]  # removed; the restore is refused (403)

    for _ in range(3):
        await engine.reconcile_once()

    assert [a for a in effectors.avatars if a[0] == ROOM1] == []
    problems = [p for r in engine.last_pass_report.rooms for p in r.problems]
    assert any("marker repair failed" in p for p in problems)


# --- W5R-03: a marker without a recorded image hash looks at the picture the room wears ----------


async def test_an_upgraded_room_gets_the_picture_that_changed_behind_its_url_before_the_upgrade() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {
        **effectors.state_store[(ROOM1, GROUP_MARKER)],
        "avatar_source_url": "https://cdn/t.png",
    }
    effectors.state_store[(ROOM1, "m.room.avatar")] = {"url": "mxc://company.org/old-logo"}
    engine = _avatar_engine(effectors)

    await engine.reconcile_once()

    assert effectors.uploads == ["https://cdn/t.png"]
    assert [a for a in effectors.avatars if a[0] == ROOM1] == [(ROOM1, "mxc://company.org/1")]
    assert effectors.state_store[(ROOM1, GROUP_MARKER)]["avatar_sha256"]


# --- S18 (W4R-06): `!room adopt` hands a room that lost its marker back to the bot ------------------


async def test_room_adopt_takes_back_a_room_whose_marker_was_removed_while_the_bot_was_stopped() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    engine, admin, effectors, _attention = _restarted(effectors)
    await engine.reconcile_once()
    assert engine.last_pass_report.rooms  # skipped and reported, as before

    reply = await engine.adopt_room("g1", ROOM1, by="@admin:company.org")

    assert reply.startswith(f"Adopted {ROOM1} as the room of group g1")
    assert effectors.state_store[(ROOM1, GROUP_MARKER)]["group_id"] == "g1"
    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT
    known = engine.rooms.known_marker(ROOM1)
    assert known is not None and known["group_id"] == "g1"
    await engine.reconcile_once()
    assert (ROOM1, "@bob:company.org") in admin.added
    assert engine.last_pass_report.rooms == []


async def test_room_adopt_takes_back_a_room_whose_marker_somebody_else_rewrote() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = dump_room_state(GroupRoomState(group_id="g-gone"))
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR
    engine, _admin, effectors, _attention = _restarted(effectors)
    await engine.reconcile_once()

    reply = await engine.adopt_room("g1", "#g1:company.org", by="@admin:company.org")

    assert reply.startswith("Adopted")
    assert effectors.state_store[(ROOM1, GROUP_MARKER)] == dump_room_state(GroupRoomState(group_id="g1"))
    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT


async def test_room_adopt_refuses_an_unknown_group_and_a_group_that_has_its_room() -> None:
    engine, _admin, effectors, _attention = _running()
    await engine.reconcile_once()
    writes = len(effectors.state_writes)

    unknown = await engine.adopt_room("g-nope", ROOM1, by="@admin:company.org")
    managed = await engine.adopt_room("g1", ROOM1, by="@admin:company.org")
    usage = await engine.adopt_room("g1", "room1", by="@admin:company.org")

    assert "is not a group the bot keeps a room for" in unknown and "Nothing was changed" in unknown
    assert "Group g1 is already that room" in managed
    assert usage.startswith("Usage: !room adopt")
    assert len(effectors.state_writes) == writes


async def test_room_adopt_refuses_a_room_the_bot_did_not_create() -> None:
    class ForeignCreator(SharedStateAdmin):
        async def get_room_state_events(self, room_id: str) -> list[dict[str, Any]]:
            events = await super().get_room_state_events(room_id)
            if room_id == ROOM1:
                for event in events:
                    if event["type"] == "m.room.create":
                        event["sender"] = MODERATOR
                        event["content"] = {**event.get("content", {}), "creator": MODERATOR}
            return events

    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    effectors.aliases["#g1:company.org"] = ROOM1
    engine, _admin, effectors, _attention = _engine(ForeignCreator(effectors), effectors)
    writes = len(effectors.state_writes)

    reply = await engine.adopt_room("g1", ROOM1, by="@admin:company.org")

    assert "was not created by the bot" in reply and "D20" in reply
    assert len(effectors.state_writes) == writes


def test_only_a_lost_group_room_marker_qualifies_for_adoption() -> None:
    from onbot.reconciler.engine import _adoption_refusal
    from onbot.rooms.identity import RoomIdentity, RoomKey

    group, lobby = OnbotRoomType.group_room, OnbotRoomType.visitor_lobby
    removed = RoomIdentity(room_id=ROOM1, key=None, removed_markers=(group,))
    foreign = RoomIdentity(
        room_id=ROOM1, key=None, problem="... not by the bot", marker_types=(group,), foreign_markers=(group,)
    )
    assert _adoption_refusal(removed) is None
    assert _adoption_refusal(foreign) is None
    never = RoomIdentity(room_id=ROOM1, key=None)  # a notice board, or any other room of the bot's
    assert "never was a group room" in (_adoption_refusal(never) or "")
    lost_lobby = RoomIdentity(room_id=ROOM1, key=None, removed_markers=(lobby,))
    assert "not a lost group room marker" in (_adoption_refusal(lost_lobby) or "")
    trusted = RoomIdentity(room_id=ROOM1, key=RoomKey(group, "g2"), locked=True)
    assert "is already the room of group g2" in (_adoption_refusal(trusted) or "")
    upgraded = RoomIdentity(room_id=ROOM1, key=None, removed_markers=(group,), tombstone="!new:x")
    assert "was upgraded" in (_adoption_refusal(upgraded) or "")


async def test_room_adopt_waits_for_a_running_pass_and_gives_up_after_a_while(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adoption racing a pass could leave the group with a new room beside the adopted one."""
    from onbot.reconciler import engine as engine_module

    monkeypatch.setattr(engine_module, "ADOPT_WAIT_SEC", 0.01)
    engine, _admin, effectors, _attention = _running()
    writes = len(effectors.state_writes)
    async with engine._pass_lock:  # a pass is running
        reply = await engine.adopt_room("g1", ROOM1, by="@admin:company.org")
    assert reply.startswith("A reconcile pass is still running")
    assert len(effectors.state_writes) == writes


# --- W6R-05: `!room adopt` checks what kind of room it takes ---------------------------------------


async def test_room_adopt_refuses_a_space_that_carries_a_foreign_group_room_marker() -> None:
    class SpaceAt(SharedStateAdmin):
        async def get_room_state_events(self, room_id: str) -> list[dict[str, Any]]:
            events = await super().get_room_state_events(room_id)
            if room_id == ROOM1:
                for event in events:
                    if event["type"] == "m.room.create":
                        event["content"] = {**event["content"], "type": "m.space"}
            return events

    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = dump_room_state(GroupRoomState(group_id="g1"))
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR
    effectors.aliases["#g1:company.org"] = ROOM1
    engine, _admin, effectors, _attention = _engine(SpaceAt(effectors), effectors)
    writes = len(effectors.state_writes)

    reply = await engine.adopt_room("g1", ROOM1, by=HUMAN_ADMIN)

    assert "is a space, not a group room" in reply and "Nothing was changed" in reply
    assert len(effectors.state_writes) == writes


async def test_room_adopt_refuses_a_notice_board() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    effectors.aliases["#g1:company.org"] = ROOM1
    admin = SharedStateAdmin(effectors)

    async def boards() -> list[str]:
        return [ROOM1]  # in the bot's m.direct

    rooms = RoomIdentityResolver(admin, effectors, bot_id=BOT, server_name="company.org", direct_rooms=boards)
    engine = ReconcilerEngine(
        OnbotConfig.model_validate(_BASE),
        FakeAuthentik(),  # type: ignore[arg-type]
        admin,  # type: ignore[arg-type]
        effectors,
        EventBus(),
        rooms=rooms,
    )
    writes = len(effectors.state_writes)

    reply = await engine.adopt_room("g1", ROOM1, by=HUMAN_ADMIN)

    assert "is a user's notice board" in reply and "Nothing was changed" in reply
    assert len(effectors.state_writes) == writes


DIRECT_MARKER = "org.company.onbot.direct_room"


def _adopt_engine(effectors: RecordingEffectors, boards: Any) -> ReconcilerEngine:
    admin = SharedStateAdmin(effectors)
    rooms = RoomIdentityResolver(admin, effectors, bot_id=BOT, server_name="company.org", direct_rooms=boards)
    return ReconcilerEngine(
        OnbotConfig.model_validate(_BASE),
        FakeAuthentik(),  # type: ignore[arg-type]
        admin,  # type: ignore[arg-type]
        effectors,
        EventBus(),
        rooms=rooms,
    )


async def _unreadable_m_direct() -> list[str]:
    raise ApiError("GET", "https://company.org/account_data/m.direct", 502, "bad gateway")


@pytest.mark.parametrize("marker", [{"schema_version": 1, "room_type": "direct_room", "user_id": "@a:x"}, {}])
async def test_room_adopt_refuses_a_room_with_a_notice_board_marker_even_without_m_direct(
    marker: dict[str, Any],
) -> None:
    """W7R-02: m.direct may be unreadable, and the legacy welcome rooms were never in it."""
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, DIRECT_MARKER)] = marker
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}

    async def no_boards() -> list[str]:
        return []

    engine = _adopt_engine(effectors, no_boards)
    writes = len(effectors.state_writes)

    reply = await engine.adopt_room("g1", ROOM1, by=HUMAN_ADMIN)

    assert "notice board" in reply and "Nothing was changed" in reply, reply
    assert len(effectors.state_writes) == writes


async def test_room_adopt_fails_closed_when_m_direct_cannot_be_read() -> None:
    """W7R-02: a lost group room marker qualifies, but the room could still be a notice board."""
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    engine = _adopt_engine(effectors, _unreadable_m_direct)
    writes = len(effectors.state_writes)

    reply = await engine.adopt_room("g1", ROOM1, by=HUMAN_ADMIN)

    assert "cannot read its list of notice boards" in reply and "Nothing was changed" in reply, reply
    assert len(effectors.state_writes) == writes


async def test_a_room_scan_reads_every_bot_room_when_m_direct_cannot_be_read() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    admin = SharedStateAdmin(effectors)
    rooms = RoomIdentityResolver(
        admin, effectors, bot_id=BOT, server_name="company.org", direct_rooms=_unreadable_m_direct
    )

    index = await rooms.scan()

    assert index.complete and ROOM1 in index.identities


@pytest.mark.parametrize(
    "other",
    [
        "org.company.onbot.admin_room",  # a former control room
        LOBBY_MARKER,  # a former lobby
    ],
)
def test_a_removed_marker_of_another_kind_is_seen_beside_a_foreign_group_marker(other: str) -> None:
    """W7R-03: a foreign marker of the wanted kind hid the removed marker of the room's real role."""
    from onbot.reconciler.engine import _adoption_refusal
    from onbot.rooms.identity import classify_room

    state = [
        {"type": "m.room.create", "state_key": "", "sender": BOT, "content": {"room_version": "10"}},
        {"type": JOIN_RULES, "state_key": "", "sender": BOT, "content": {"join_rule": "invite"}},
        {"type": other, "state_key": "", "sender": MODERATOR, "content": {}},
        {
            "type": GROUP_MARKER,
            "state_key": "",
            "sender": MODERATOR,
            "content": dump_room_state(GroupRoomState(group_id="g1")),
        },
    ]
    identity = classify_room("!ex:company.org", state, bot_id=BOT, server_name="company.org")

    assert identity is not None and identity.removed_markers
    refusal = _adoption_refusal(identity)
    assert refusal is not None and "not a lost group room marker" in refusal


async def test_room_adopt_refuses_an_ex_lobby_as_a_group_room() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    effectors.state_store[(ROOM1, JOIN_RULES)] = RESTRICTED
    engine, _admin, effectors, _attention = _restarted(effectors)
    writes = len(effectors.state_writes)

    reply = await engine.adopt_room("g1", ROOM1, by=HUMAN_ADMIN)

    assert "join rule of a visitor lobby" in reply and "!room adopt lobby" in reply
    assert len(effectors.state_writes) == writes


async def test_room_adopt_whose_marker_write_fails_after_the_lock_says_what_changed() -> None:
    """Before W6R-05 the reply said "Nothing was changed" although the marker lock had been raised."""

    class RefusingMarker(RecordingEffectors):
        async def put_room_state(
            self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
        ) -> None:
            if room_id == ROOM1 and event_type == GROUP_MARKER:
                raise ApiError("PUT", "/state", 403, {"errcode": "M_FORBIDDEN"})
            await super().put_room_state(room_id, event_type, content, state_key)

        async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
            return dict(self.levels.get(room_id, {"users": {}}))

        async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
            self.levels[room_id] = power_levels

    effectors = RefusingMarker()
    effectors.levels = {}  # type: ignore[attr-defined]
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    engine, _admin, effectors, _attention = _restarted(effectors)

    reply = await engine.adopt_room("g1", ROOM1, by=HUMAN_ADMIN)

    assert "Writing the marker into" in reply and "Nothing was changed" not in reply
    assert "raised the power level" in reply and "send the command again" in reply
    assert effectors.levels[ROOM1]["events"][GROUP_MARKER] > 100  # type: ignore[attr-defined]


# --- D25: `!room adopt lobby` hands a visitor lobby that lost its marker back -------------------------


def _restarted_lobby(
    effectors: RecordingEffectors,
) -> tuple[ReconcilerEngine, Any, RecordingEffectors, list[Event]]:
    """A fresh process over the g1 room and its lobby as they are now; the lobby alias points at it."""
    effectors.state_store.setdefault((LOBBY, JOIN_RULES), RESTRICTED)
    effectors.aliases["#g1:company.org"] = ROOM1
    effectors.aliases["#g1-lobby:company.org"] = LOBBY
    return _engine(SharedLobbyAdmin(effectors), effectors, config=_lobby_config())


async def test_room_adopt_lobby_takes_back_a_lobby_whose_marker_was_removed_while_the_bot_was_stopped() -> (
    None
):
    effectors = RecordingEffectors()
    _seed_converged(effectors, LOBBY, suggested=True)
    effectors.state_store[(LOBBY, LOBBY_MARKER)] = {}
    engine, _admin, effectors, _attention = _restarted_lobby(effectors)
    await engine.reconcile_once()
    assert engine.last_pass_report.rooms  # skipped and reported, as for a group room
    assert effectors.lobbies_created == []

    reply = await engine.adopt_room("g1", LOBBY, by=HUMAN_ADMIN, kind=OnbotRoomType.visitor_lobby)

    assert reply.startswith(f"Adopted {LOBBY} as the visitor lobby of group g1"), reply
    assert effectors.state_store[(LOBBY, LOBBY_MARKER)]["group_id"] == "g1"
    assert effectors.senders[(LOBBY, LOBBY_MARKER)] == BOT
    await engine.reconcile_once()
    assert engine.last_pass_report.rooms == []
    assert LOBBY in engine.active_lobbies


async def test_room_adopt_lobby_takes_back_a_lobby_whose_marker_somebody_else_rewrote() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors, LOBBY, suggested=True)
    effectors.state_store[(LOBBY, LOBBY_MARKER)] = dump_room_state(VisitorLobbyRoomState(group_id="g-gone"))
    effectors.senders[(LOBBY, LOBBY_MARKER)] = MODERATOR
    engine, _admin, effectors, _attention = _restarted_lobby(effectors)

    reply = await engine.adopt_room(
        "g1", "#g1-lobby:company.org", by=HUMAN_ADMIN, kind=OnbotRoomType.visitor_lobby
    )

    assert reply.startswith("Adopted"), reply
    assert effectors.state_store[(LOBBY, LOBBY_MARKER)] == dump_room_state(
        VisitorLobbyRoomState(group_id="g1")
    )


async def test_room_adopt_lobby_refusals() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors, LOBBY, suggested=True)
    engine, _admin, effectors, _attention = _restarted_lobby(effectors)
    writes = len(effectors.state_writes)

    managed = await engine.adopt_room("g1", LOBBY, by=HUMAN_ADMIN, kind=OnbotRoomType.visitor_lobby)
    assert "Group g1 is already that visitor lobby" in managed

    effectors.state_store[(LOBBY, LOBBY_MARKER)] = {}  # both lost their markers while it was stopped
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}
    engine = _restarted_lobby(effectors)[0]
    group_room = await engine.adopt_room("g1", ROOM1, by=HUMAN_ADMIN, kind=OnbotRoomType.visitor_lobby)
    assert "not a lost visitor lobby marker" in group_room and "Nothing was changed" in group_room
    assert len(effectors.state_writes) == writes

    off = _engine(SharedStateAdmin(effectors), effectors)[0]  # lobbies switched off
    switched_off = await off.adopt_room("g1", LOBBY, by=HUMAN_ADMIN, kind=OnbotRoomType.visitor_lobby)
    assert "has no visitor lobby" in switched_off and "Nothing was changed" in switched_off


def test_only_a_lost_lobby_marker_qualifies_for_lobby_adoption() -> None:
    from onbot.reconciler.engine import _adoption_refusal
    from onbot.rooms.identity import RoomIdentity

    group, lobby = OnbotRoomType.group_room, OnbotRoomType.visitor_lobby
    removed = RoomIdentity(room_id=LOBBY, key=None, removed_markers=(lobby,), join_rule="restricted")
    assert _adoption_refusal(removed, lobby) is None
    lost_group = RoomIdentity(room_id=ROOM1, key=None, removed_markers=(group,))
    assert "not a lost visitor lobby marker" in (_adoption_refusal(lost_group, lobby) or "")
    space = RoomIdentity(room_id=SPACE, key=None, removed_markers=(lobby,), is_space=True)
    assert "is a space" in (_adoption_refusal(space, lobby) or "")
