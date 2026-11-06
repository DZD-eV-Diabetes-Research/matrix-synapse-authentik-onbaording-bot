"""Reconciler pass robustness (review session S2).

One room the bot cannot fully manage must never stop the pass: other rooms and the account lifecycle
still run, and the failure is reported once per pass with room and group (REC-03, SEC-04, LIF-04).
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import pytest

from onbot.clients.base import ApiError
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.reconciler.engine import ReconcilerEngine
from onbot.reconciler.state import OnbotRoomType
from onbot.rooms.provision import ManagedRoomSpec
from tests.unit.room_fakes import room_state
from tests.unit.test_engine import (
    _BASE,
    _GROUP_G1,
    LOCKED_EVENTS,
    FakeAdmin,
    FakeAuthentik,
    FakeLifecycle,
    RecordingEffectors,
)

_ROOM1 = "!room1:company.org"
_ROOM2 = "!room2:company.org"
_GROUP_G2 = {"pk": "g2", "name": "Other", "attributes": {}, "users": ["carol-pk"]}


def _forbidden(path: str, error: str) -> ApiError:
    return ApiError("POST", f"http://synapse/{path}", 403, {"errcode": "M_FORBIDDEN", "error": error})


class TwoGroupAuthentik(FakeAuthentik):
    """g1 (alice, bob) and g2 (carol); dave is disabled upstream and has a Matrix account."""

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs.get("filter_is_active") is False:
            return [{"username": "dave", "pk": "dave-pk"}]
        return await super().list_users(**kwargs)

    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        return [_GROUP_G1, _GROUP_G2]


class TwoRoomAdmin(FakeAdmin):
    """room1 belongs to g1, room2 to g2. Bob is banned from room1, so joining him is refused."""

    room_markers: ClassVar[dict[str, tuple[OnbotRoomType, str | None]]] = {
        _ROOM1: (OnbotRoomType.group_room, "g1"),
        _ROOM2: (OnbotRoomType.group_room, "g2"),
        "!space:company.org": (OnbotRoomType.space, None),
    }

    def __init__(self) -> None:
        super().__init__()
        self.refuse: set[tuple[str, str]] = {(_ROOM1, "@bob:company.org")}

    async def list_users(self) -> list[dict[str, Any]]:
        return [{"name": f"@{u}:company.org"} for u in ("alice", "bob", "carol", "dave")]

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return [
            {"room_id": _ROOM1, "canonical_alias": "#g1:company.org", "name": "Team"},
            {"room_id": _ROOM2, "canonical_alias": "#g2:company.org", "name": "Other"},
        ]

    async def list_room_members(self, room_id: str) -> list[str]:
        if room_id == _ROOM2:
            return ["@bot:company.org"]
        return await super().list_room_members(room_id)

    async def add_user_to_room(self, room_id: str, user_id: str) -> None:
        if (room_id, user_id) in self.refuse:
            raise _forbidden(f"_synapse/admin/v1/join/{room_id}", f"{user_id} is banned from the room")
        await super().add_user_to_room(room_id, user_id)


def _collect(bus: EventBus, signal: Signal) -> list[Event]:
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    bus.subscribe(signal, handler)
    return seen


def _engine(
    admin: FakeAdmin | None = None,
    effectors: RecordingEffectors | None = None,
    *,
    config: OnbotConfig | None = None,
    lifecycle: FakeLifecycle | None = None,
    authentik: Any = None,
) -> tuple[ReconcilerEngine, Any, RecordingEffectors, EventBus]:
    bus = EventBus()
    admin = admin or TwoRoomAdmin()
    effectors = effectors or RecordingEffectors()
    engine = ReconcilerEngine(
        config or OnbotConfig.model_validate(_BASE),
        authentik or TwoGroupAuthentik(),
        admin,  # type: ignore[arg-type]
        effectors,
        bus,
        lifecycle=lifecycle,  # type: ignore[arg-type]
    )
    return engine, admin, effectors, bus


# --- REC-03: one failing room never aborts the pass ---


async def test_a_member_who_cannot_be_joined_does_not_stop_the_pass() -> None:
    """08 check 3 style: a banned group member makes the join fail, every pass, forever."""
    engine, admin, effectors, bus = _engine()
    reported = _collect(bus, Signal.room_not_converged)

    await engine.reconcile_once()

    # The other room converged.
    assert (_ROOM2, "@carol:company.org") in admin.added
    # The broken room converged as far as it could: the kick and the power levels still ran.
    assert (_ROOM1, "@stale:company.org") in effectors.kicks
    assert (_ROOM1, {"users": {"@alice:company.org": 50}}) in effectors.power_levels
    assert engine.last_reconcile_at is not None
    # Reported once, naming the room, the group and the user.
    assert len(reported) == 1
    payload = reported[0].payload
    assert payload["room_id"] == _ROOM1
    assert payload["group_pk"] == "g1"
    assert payload["room_kind"] == "group_room"
    assert any("@bob:company.org" in p for p in payload["problems"])


async def test_a_room_failing_in_several_steps_is_reported_once_per_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenPowerLevels(RecordingEffectors):
        async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
            if room_id == _ROOM1:
                raise ApiError("PUT", "http://synapse/pl", 403, {"errcode": "M_FORBIDDEN"})
            await super().set_room_power_levels(room_id, power_levels)

    engine, admin, _effectors, bus = _engine(effectors=BrokenPowerLevels())
    reported = _collect(bus, Signal.room_not_converged)

    with caplog.at_level(logging.WARNING, logger="onbot.reconciler.engine"):
        await engine.reconcile_once()

    assert [e.payload["room_id"] for e in reported] == [_ROOM1]
    assert len(reported[0].payload["problems"]) == 2  # the refused join and the refused PL write
    lines = [r for r in caplog.records if "room not converged" in r.getMessage()]
    assert len(lines) == 1
    assert _ROOM1 in lines[0].getMessage() and "g1" in lines[0].getMessage()
    # ...and the pass went on to the other room.
    assert (_ROOM2, "@carol:company.org") in admin.added

    # The next pass reports it again (once), it does not accumulate.
    reported.clear()
    await engine.reconcile_once()
    assert [e.payload["room_id"] for e in reported] == [_ROOM1]


async def test_an_unexpected_error_in_one_room_does_not_stop_the_next_room() -> None:
    class ExplodingMembers(TwoRoomAdmin):
        async def list_room_members(self, room_id: str) -> list[str]:
            if room_id == _ROOM1:
                raise RuntimeError("boom")
            return await super().list_room_members(room_id)

    engine, admin, _, bus = _engine(admin=ExplodingMembers())
    reported = _collect(bus, Signal.room_not_converged)
    await engine.reconcile_once()

    assert (_ROOM2, "@carol:company.org") in admin.added
    assert [e.payload["room_id"] for e in reported] == [_ROOM1]
    assert "RuntimeError: boom" in reported[0].payload["problems"][0]


async def test_a_failing_room_creation_does_not_stop_the_other_groups() -> None:
    """A bad ``chatroom_params`` (or a taken alias) makes createRoom fail on every pass."""

    class NoRoom2(TwoRoomAdmin):
        async def list_non_space_rooms(self) -> list[dict[str, Any]]:
            return [r for r in await super().list_non_space_rooms() if r["room_id"] != _ROOM2]

    class RefusingCreate(RecordingEffectors):
        async def create_managed_room(self, spec: ManagedRoomSpec) -> str:
            raise ApiError("POST", "http://synapse/createRoom", 400, {"errcode": "M_ROOM_IN_USE"})

    admin = NoRoom2()
    admin.refuse = set()
    engine, admin, effectors, bus = _engine(admin=admin, effectors=RefusingCreate())
    reported = _collect(bus, Signal.room_not_converged)
    await engine.reconcile_once()

    # g1's room still converged after g2's createRoom was refused.
    assert (_ROOM1, "@bob:company.org") in admin.added
    assert (_ROOM1, "@stale:company.org") in effectors.kicks
    assert engine.last_reconcile_at is not None
    assert [(e.payload["room_id"], e.payload["group_pk"]) for e in reported] == [(None, "g2")]
    assert "room setup failed" in reported[0].payload["problems"][0]
    assert "M_ROOM_IN_USE" in reported[0].payload["problems"][0]


async def test_a_kick_the_server_refuses_does_not_stop_the_other_kicks() -> None:
    class StaleTwice(TwoRoomAdmin):
        async def list_room_members(self, room_id: str) -> list[str]:
            if room_id == _ROOM1:
                return [
                    "@alice:company.org",
                    "@creator:company.org",
                    "@stale:company.org",
                    "@bot:company.org",
                ]
            return await super().list_room_members(room_id)

    class RefusingKick(RecordingEffectors):
        async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
            if user_id == "@creator:company.org":
                raise _forbidden("kick", "You cannot kick user @creator:company.org")
            await super().kick_user(room_id, user_id, reason)

    engine, _, effectors, bus = _engine(admin=StaleTwice(), effectors=RefusingKick())
    reported = _collect(bus, Signal.room_not_converged)
    await engine.reconcile_once()

    assert (_ROOM1, "@stale:company.org") in effectors.kicks
    assert any("@creator:company.org" in p for p in reported[0].payload["problems"])


# --- LIF-04 / SEC-04: the account lifecycle never depends on the rooms ---


async def test_lifecycle_runs_although_a_room_fails() -> None:
    lifecycle = FakeLifecycle()
    engine, _, _, _ = _engine(lifecycle=lifecycle)
    await engine.reconcile_once()
    # The lifecycle step ran once and saw the full directory (S1 feeds it a DirectorySnapshot).
    assert len(lifecycle.calls) == 1
    assert "@dave:company.org" in lifecycle.calls[0].matrix_mxids


async def test_lifecycle_runs_although_the_whole_room_stage_fails() -> None:
    """A missing space (ConfigurationError) or a Synapse 500 on the room list fails the room stage."""

    class NoRoomList(TwoRoomAdmin):
        async def list_non_space_rooms(self) -> list[dict[str, Any]]:
            raise ApiError("GET", "http://synapse/rooms", 500, {"errcode": "M_UNKNOWN"})

    lifecycle = FakeLifecycle()
    engine, _, _, bus = _engine(admin=NoRoomList(), lifecycle=lifecycle)
    completed = _collect(bus, Signal.reconcile_completed)

    with pytest.raises(ApiError):
        await engine.reconcile_once()

    # The lifecycle step ran once and saw the full directory (S1 feeds it a DirectorySnapshot).
    assert len(lifecycle.calls) == 1
    assert "@dave:company.org" in lifecycle.calls[0].matrix_mxids
    # The pass still counts as failed: no completion timestamp, no completion signal.
    assert engine.last_reconcile_at is None
    assert completed == []


# --- obsolete-room teardown: per room, per member ---

_ORPHAN_A = "!orphan-a:company.org"
_ORPHAN_B = "!orphan-b:company.org"
_GROUP_EVENT = "org.company.onbot.group_room"


class OrphansAdmin(TwoRoomAdmin):
    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return [
            *await super().list_non_space_rooms(),
            {"room_id": _ORPHAN_A, "canonical_alias": "#gonea:company.org", "name": "Gone A"},
            {"room_id": _ORPHAN_B, "canonical_alias": "#goneb:company.org", "name": "Gone B"},
        ]

    async def list_room_members(self, room_id: str) -> list[str]:
        if room_id in (_ORPHAN_A, _ORPHAN_B):
            return ["@x:company.org", "@y:company.org", "@bot:company.org"]
        return await super().list_room_members(room_id)


def _teardown_config() -> OnbotConfig:
    config = OnbotConfig.model_validate(_BASE)
    room_cfg = config.sync_matrix_rooms_based_on_authentik_groups
    room_cfg.disable_rooms_when_mapped_authentik_group_disappears = True
    return config


async def test_an_unreadable_bot_room_holds_the_teardown_until_it_can_be_read() -> None:
    """Fail closed (D20): while one bot room cannot be read, no room is torn down at all.

    Before S6 a state read error on one room only skipped that room. Identity now comes from the whole
    picture, so the teardown waits; the membership pass still runs, and the next pass catches up.
    """

    class FlakyState(OrphansAdmin):
        broken = True

        async def get_room_state_events(self, room_id: str) -> list[dict[str, Any]]:
            if room_id == _ORPHAN_A and self.broken:
                raise ApiError("GET", "http://synapse/state", 500, {"errcode": "M_UNKNOWN"})
            return await super().get_room_state_events(room_id)

    admin = FlakyState()
    admin.room_state_events = {
        _ORPHAN_A: room_state(OnbotRoomType.group_room, "g-gone"),
        _ORPHAN_B: room_state(OnbotRoomType.group_room, "g-gone"),
    }
    engine, admin, _, _ = _engine(admin=admin, config=_teardown_config())
    await engine.reconcile_once()

    assert admin.blocked_changes == []
    assert (_ROOM2, "@carol:company.org") in admin.added

    admin.broken = False
    await engine.reconcile_once()
    assert (_ORPHAN_A, True) in admin.blocked_changes
    assert (_ORPHAN_B, True) in admin.blocked_changes


async def test_a_refused_kick_while_disabling_a_room_does_not_stop_the_other_kicks() -> None:
    class RefuseX(RecordingEffectors):
        async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
            if user_id == "@x:company.org":
                raise _forbidden("kick", "You cannot kick user @x:company.org")
            await super().kick_user(room_id, user_id, reason)

    effectors = RefuseX()
    admin = OrphansAdmin()
    admin.room_state_events = {_ORPHAN_A: room_state(OnbotRoomType.group_room, "g-gone")}
    engine, _, _, bus = _engine(admin=admin, effectors=effectors, config=_teardown_config())
    reported = _collect(bus, Signal.room_not_converged)
    await engine.reconcile_once()

    assert (_ORPHAN_A, "@y:company.org") in effectors.kicks
    orphan_reports = [e.payload for e in reported if e.payload["room_id"] == _ORPHAN_A]
    assert orphan_reports and orphan_reports[0]["group_pk"] == "g-gone"


# --- REC-02 / MNT-01: switching user sync off, or an empty answer, never kicks ---


async def test_user_sync_disabled_does_not_manage_membership_or_power_levels() -> None:
    config = OnbotConfig.model_validate(
        {**_BASE, "sync_authentik_users_with_matrix_rooms": {"enabled": False}}
    )
    admin = TwoRoomAdmin()
    admin.refuse = set()
    engine, admin, effectors, _ = _engine(admin=admin, config=config, authentik=RenamedTwoGroupAuthentik())
    await engine.reconcile_once()

    assert effectors.kicks == []
    assert admin.added == []
    assert effectors.power_levels == []
    # Rooms themselves are still kept in step with their groups.
    assert (_ROOM1, "Team renamed") in effectors.names


class NobodyActiveAuthentik(TwoGroupAuthentik):
    """Authentik answers 200 with an empty user list (a narrowed RBAC token, 08 check 3)."""

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs.get("filter_is_active") is False:
            return await super().list_users(**kwargs)
        return []


async def test_an_empty_desired_set_in_every_room_kicks_nobody() -> None:
    """An empty answer always needs a confirmation: nothing is kicked, one batch is proposed (D13)."""
    engine, _, effectors, bus = _engine(authentik=NobodyActiveAuthentik())
    suppressed = _collect(bus, Signal.destructive_actions_suppressed)
    await engine.reconcile_once()

    assert effectors.kicks == []
    assert len(suppressed) == 1
    assert suppressed[0].payload["action"] == "kick"


class OnlyCarolAuthentik(TwoGroupAuthentik):
    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        users = await super().list_users(**kwargs)
        if kwargs.get("filter_is_active") is False:
            return users
        return [u for u in users if u["username"] == "carol"]


async def test_kicks_still_run_when_some_room_has_desired_members() -> None:
    """One legitimately emptied group still kicks: without a baseline (the first pass) only an empty
    answer trips the guard. A partial answer against a baseline is in ``test_engine_guard.py``."""
    engine, _, effectors, bus = _engine(authentik=OnlyCarolAuthentik())
    suppressed = _collect(bus, Signal.destructive_actions_suppressed)
    await engine.reconcile_once()

    assert (_ROOM1, "@alice:company.org") in effectors.kicks
    assert suppressed == []


# --- issues.md entry 2 / MNT-04: keep_updating_matrix_attributes_from_authentik per group ---


class RenamedTwoGroupAuthentik(TwoGroupAuthentik):
    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        return [
            {**_GROUP_G1, "attributes": {**_GROUP_G1["attributes"], "chatroom_name": "Team renamed"}},
            {**_GROUP_G2, "attributes": {"chatroom_name": "Other renamed"}},
        ]


async def test_a_per_group_keep_updating_override_is_honoured() -> None:
    config = OnbotConfig.model_validate(
        {
            **_BASE,
            "per_authentik_group_pk_matrix_room_settings": {
                "g1": {"keep_updating_matrix_attributes_from_authentik": False}
            },
        }
    )
    admin = TwoRoomAdmin()
    admin.refuse = set()
    engine, _, effectors, _ = _engine(admin=admin, config=config, authentik=RenamedTwoGroupAuthentik())
    await engine.reconcile_once()

    # Only g2 follows Authentik; g1 opted out per group and keeps its name.
    assert effectors.names == [(_ROOM2, "Other renamed")]


# --- REC-07: matrix_user_ignore_list is honoured on the kick path ---


async def test_an_ignored_matrix_user_in_a_group_room_is_never_kicked() -> None:
    config = OnbotConfig.model_validate({**_BASE, "matrix_user_ignore_list": ["@stale:company.org"]})
    admin = TwoRoomAdmin()
    admin.refuse = set()
    engine, _, effectors, _ = _engine(admin=admin, config=config)
    await engine.reconcile_once()

    sync_cfg = config.sync_authentik_users_with_matrix_rooms
    assert sync_cfg.kick_matrix_room_members_not_in_mapped_authentik_group_anymore
    assert (_ROOM1, "@stale:company.org") not in effectors.kicks


# --- REC-12: the bot's own MXID never lands in the power-level users map ---


class BotIsAnAuthentikUser(TwoGroupAuthentik):
    """The bot's account is also an Authentik superuser in g1 (a plausible setup mistake)."""

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        users = await super().list_users(**kwargs)
        if kwargs.get("filter_is_active") is False:
            return users
        bot = {"username": "bot", "pk": "bot-pk", "is_superuser": True, "groups_obj": [{"pk": "g1"}]}
        return [*users, bot]


class BotInMatrixAdmin(TwoRoomAdmin):
    async def list_users(self) -> list[dict[str, Any]]:
        return [*await super().list_users(), {"name": "@bot:company.org"}]


async def test_the_bot_is_never_written_into_the_power_levels() -> None:
    admin = BotInMatrixAdmin()
    admin.refuse = set()
    engine, _, effectors, _ = _engine(admin=admin, authentik=BotIsAnAuthentikUser())
    await engine.reconcile_once()

    written = [pl["users"] for _room, pl in effectors.power_levels]
    assert written, "the room still gets its power levels"
    assert all("@bot:company.org" not in users for users in written)


# --- REC-05: pre-v12 rooms, where the bot is an ordinary entry at 100 ---

_ALICE = "@alice:company.org"
_BOB = "@bob:company.org"
_CAROL = "@carol:company.org"
_GOUT = "@gout:company.org"
_BOT = "@bot:company.org"


class StatefulPowerLevels(RecordingEffectors):
    """Power levels that persist between calls and passes, like a real room."""

    def __init__(self, levels: dict[str, dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.levels: dict[str, dict[str, Any]] = levels or {}

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        current = self.levels.get(room_id, {})
        return {**current, "users": dict(current.get("users", {}))}

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        await super().set_room_power_levels(room_id, power_levels)
        self.levels[room_id] = {**power_levels, "users": dict(power_levels.get("users", {}))}


class LegacyRoomAdmin(TwoRoomAdmin):
    """room1 is a v10 room (the legacy bot's, adopted by alias). gout sits in it from an old grant."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = set()

    async def list_users(self) -> list[dict[str, Any]]:
        return [*await super().list_users(), {"name": _GOUT}]

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        rooms = await super().list_non_space_rooms()
        return [{**r, "version": "10", "creator": _BOT} if r["room_id"] == _ROOM1 else r for r in rooms]

    async def list_room_members(self, room_id: str) -> list[str]:
        if room_id == _ROOM1:
            return [_ALICE, _BOB, _GOUT, _BOT]
        return await super().list_room_members(room_id)


async def test_a_legacy_room_never_gets_a_grant_at_the_bots_level() -> None:
    config = OnbotConfig.model_validate({**_BASE, "global_room_admins": ["carol"]})
    effectors = StatefulPowerLevels({_ROOM1: {"users": {_BOT: 100}}})
    engine, _, effectors, _ = _engine(admin=LegacyRoomAdmin(), effectors=effectors, config=config)
    await engine.reconcile_once()

    assert effectors.levels[_ROOM1]["users"] == {_BOT: 100, _ALICE: 50, _CAROL: 99}
    # A v12 room the bot created still gets the full 100.
    assert effectors.levels[_ROOM2]["users"] == {_CAROL: 100}


async def test_a_legacy_room_with_users_at_the_bots_level_is_reported_and_not_fought() -> None:
    """08 check 5: an old grant of 100 cannot be demoted or kicked by the bot any more."""
    effectors = StatefulPowerLevels(
        {_ROOM1: {"users": {_BOT: 100, _ALICE: 100, _GOUT: 100}, "events": dict(LOCKED_EVENTS)}}
    )
    # An older bot version seated both as global admins and recorded them in the room state.
    effectors.state_store[(_ROOM1, _GROUP_EVENT)] = {
        "room_type": "group_room",
        "group_id": "g1",
        "global_admins": [_ALICE, _GOUT],
    }
    engine, _, effectors, bus = _engine(admin=LegacyRoomAdmin(), effectors=effectors)
    reported = _collect(bus, Signal.room_not_converged)

    for _ in range(2):
        reported.clear()
        await engine.reconcile_once()

        # Nothing the server would refuse is even attempted ...
        assert all(user != _GOUT for room, user in effectors.kicks if room == _ROOM1)
        assert all(room != _ROOM1 for room, _pl in effectors.power_levels)
        assert effectors.levels[_ROOM1]["users"] == {_BOT: 100, _ALICE: 100, _GOUT: 100}
        # ... and both are reported once for the room, every pass.
        room1 = [e.payload for e in reported if e.payload["room_id"] == _ROOM1]
        assert len(room1) == 1
        problems = " | ".join(room1[0]["problems"])
        assert f"cannot kick {_GOUT}" in problems
        assert f"cannot change power level of {_ALICE}" in problems
        assert f"cannot change power level of {_GOUT}" in problems


async def test_an_extra_creator_is_neither_kicked_nor_named_in_power_levels() -> None:
    """D6: ``chatroom_params`` can plant ``additional_creators``; the room is reported, not fought."""

    class ExtraCreatorAdmin(TwoRoomAdmin):
        async def list_room_members(self, room_id: str) -> list[str]:
            if room_id == _ROOM1:
                return [_ALICE, "@mallory:company.org", _BOT]
            return await super().list_room_members(room_id)

    admin = ExtraCreatorAdmin()
    admin.refuse = set()
    effectors = StatefulPowerLevels()
    effectors.state_store[(_ROOM1, "m.room.create")] = {
        "room_version": "12",
        "additional_creators": ["@mallory:company.org"],
    }
    engine, _, effectors, bus = _engine(admin=admin, effectors=effectors)
    reported = _collect(bus, Signal.room_not_converged)
    await engine.reconcile_once()

    assert (_ROOM1, "@mallory:company.org") not in effectors.kicks
    assert "cannot kick @mallory:company.org" in " | ".join(reported[0].payload["problems"])


# --- D10 / REC-08: withdraw only what the bot itself granted ---


class MutableAuthentik(TwoGroupAuthentik):
    """g1's membership can change between passes; g1 carries power level 50."""

    def __init__(self) -> None:
        self.g1 = {"alice-pk", "bob-pk"}

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs.get("filter_is_active") is False:
            return []
        users = [("alice", "alice-pk"), ("bob", "bob-pk"), ("carol", "carol-pk")]
        return [
            {
                "username": name,
                "pk": pk,
                "is_superuser": False,
                "groups_obj": [{"pk": "g1"}] if pk in self.g1 else [{"pk": "g2"}],
            }
            for name, pk in users
        ]

    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        return [{**_GROUP_G1, "users": sorted(self.g1 & {"alice-pk"})}, _GROUP_G2]


def _d10_engine(levels: dict[str, Any]) -> tuple[ReconcilerEngine, MutableAuthentik, StatefulPowerLevels]:
    config = OnbotConfig.model_validate(
        {
            **_BASE,
            "sync_authentik_users_with_matrix_rooms": {
                "kick_matrix_room_members_not_in_mapped_authentik_group_anymore": False
            },
        }
    )
    admin = TwoRoomAdmin()
    admin.refuse = set()
    authentik = MutableAuthentik()
    effectors = StatefulPowerLevels({_ROOM1: levels})
    engine, _, effectors, _ = _engine(admin=admin, effectors=effectors, config=config, authentik=authentik)
    return engine, authentik, effectors


async def test_a_level_the_bot_granted_is_withdrawn_when_the_member_leaves_the_group() -> None:
    """REC-08: with kicks off, an ex-member used to stay a moderator forever."""
    engine, authentik, effectors = _d10_engine({"users": {}})
    await engine.reconcile_once()
    assert effectors.levels[_ROOM1]["users"] == {_ALICE: 50}

    authentik.g1 = {"bob-pk"}
    await engine.reconcile_once()
    assert effectors.levels[_ROOM1]["users"] == {}


async def test_a_level_a_human_set_is_never_withdrawn() -> None:
    """bob is in g1 without a power-level group; a room admin made him a moderator by hand."""
    engine, _, effectors = _d10_engine({"users": {_BOB: 50}})
    await engine.reconcile_once()
    assert effectors.levels[_ROOM1]["users"] == {_ALICE: 50, _BOB: 50}


async def test_a_bot_grant_a_human_changed_is_left_to_the_human() -> None:
    engine, authentik, effectors = _d10_engine({"users": {}})
    await engine.reconcile_once()
    effectors.levels[_ROOM1]["users"][_ALICE] = 75  # a room admin promotes alice further

    authentik.g1 = {"bob-pk"}
    await engine.reconcile_once()
    assert effectors.levels[_ROOM1]["users"] == {_ALICE: 75}


async def test_the_grant_record_lives_in_the_bots_room_account_data() -> None:
    """Not in room state: a moderator must not be able to make the bot demote somebody."""
    engine, _, effectors = _d10_engine({"users": {}})
    await engine.reconcile_once()
    assert effectors.account_data[(_ROOM1, "org.company.onbot.granted_power_levels")]["users"] == {_ALICE: 50}
    state = effectors.state_store.get((_ROOM1, _GROUP_EVENT)) or {}
    assert "users" not in state
