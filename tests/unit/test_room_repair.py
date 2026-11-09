"""Rooms converge back to their shape on every pass, not only at creation (session S7).

REC-09: the space link, the onbot marker (and its lock) and the alias were written once at creation
and never repaired. REC-10: switching a lobby off abandoned it open, and a lobby a human closed was
reopened on the next tick (D16 decides it stays closed). REC-11: the bot unblocked rooms a server
admin had blocked.
"""

from __future__ import annotations

from typing import Any, ClassVar

from onbot.admin.notifier import AdminRoomNotifier
from onbot.clients.base import ApiError
from onbot.clients.synapse_admin import RoomBlock
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.reconciler.engine import ReconcilerEngine
from onbot.reconciler.state import (
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


# --- REC-09: marker and lock -----------------------------------------------------------------------


async def test_a_removed_marker_is_restored_and_reported_once() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = {}  # somebody at the bot's level removed it
    engine, _admin, effectors, attention = _engine(effectors=effectors)

    await engine.reconcile_once()
    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, GROUP_MARKER)]["group_id"] == "g1"
    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT
    (report,) = [e.payload["text"] for e in attention]
    assert "marker was removed" in report


async def test_a_marker_rewritten_to_another_group_is_restored_from_what_the_bot_knows() -> None:
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.state_store[(ROOM1, GROUP_MARKER)] = dump_room_state(GroupRoomState(group_id="g-other"))
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR
    engine, _admin, effectors, attention = _engine(effectors=effectors)

    await engine.reconcile_once()

    assert effectors.state_store[(ROOM1, GROUP_MARKER)]["group_id"] == "g1"
    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT
    assert any("another role" in e.payload["text"] for e in attention)


async def test_a_marker_somebody_else_wrote_is_rewritten_by_the_bot() -> None:
    """Same content, wrong sender: after a restart the scan would no longer trust it (W3R)."""
    effectors = RecordingEffectors()
    _seed_converged(effectors)
    effectors.senders[(ROOM1, GROUP_MARKER)] = MODERATOR
    engine, _admin, effectors, attention = _engine(effectors=effectors)

    await engine.reconcile_once()

    assert effectors.senders[(ROOM1, GROUP_MARKER)] == BOT
    assert effectors.state_store[(ROOM1, GROUP_MARKER)] == dump_room_state(GroupRoomState(group_id="g1"))
    assert any(f"rewritten by {MODERATOR}" in e.payload["text"] for e in attention)


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
    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
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
