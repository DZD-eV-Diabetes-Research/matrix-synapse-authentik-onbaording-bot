"""Reconciler pass robustness (review session S2).

One room the bot cannot fully manage must never stop the pass: other rooms and the account lifecycle
still run, and the failure is reported once per pass with room and group (REC-03, SEC-04, LIF-04).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from onbot.clients.base import ApiError
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.reconciler.engine import ReconcilerEngine
from tests.unit.test_engine import (
    _BASE,
    _GROUP_G1,
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
        async def create_group_room(self, attrs: Any, parent_space_id: str | None) -> str:
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
    assert lifecycle.calls == [{"@dave:company.org"}]


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

    assert lifecycle.calls == [{"@dave:company.org"}]
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
    config.sync_matrix_rooms_based_on_authentik_groups.disable_rooms_when_mapped_authentik_group_disappears = True
    return config


async def test_a_state_read_error_on_one_room_does_not_stop_the_teardown_of_another() -> None:
    class FlakyState(RecordingEffectors):
        async def get_room_state(
            self, room_id: str, event_type: str, state_key: str = ""
        ) -> dict[str, Any] | None:
            if room_id == _ORPHAN_A:
                raise ApiError("GET", "http://synapse/state", 500, {"errcode": "M_UNKNOWN"})
            return await super().get_room_state(room_id, event_type, state_key)

    effectors = FlakyState()
    effectors.state_store[(_ORPHAN_B, _GROUP_EVENT)] = {"room_type": "group_room", "group_id": "g-gone"}
    engine, admin, _, bus = _engine(admin=OrphansAdmin(), effectors=effectors, config=_teardown_config())
    reported = _collect(bus, Signal.room_not_converged)
    await engine.reconcile_once()

    assert (_ORPHAN_B, True) in admin.blocked_changes
    assert (_ORPHAN_A, True) not in admin.blocked_changes
    assert _ORPHAN_A in {e.payload["room_id"] for e in reported}
    # ...and the membership pass after the teardown still ran.
    assert (_ROOM2, "@carol:company.org") in admin.added


async def test_a_refused_kick_while_disabling_a_room_does_not_stop_the_other_kicks() -> None:
    class RefuseX(RecordingEffectors):
        async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
            if user_id == "@x:company.org":
                raise _forbidden("kick", "You cannot kick user @x:company.org")
            await super().kick_user(room_id, user_id, reason)

    effectors = RefuseX()
    effectors.state_store[(_ORPHAN_A, _GROUP_EVENT)] = {"room_type": "group_room", "group_id": "g-gone"}
    engine, _, _, bus = _engine(admin=OrphansAdmin(), effectors=effectors, config=_teardown_config())
    reported = _collect(bus, Signal.room_not_converged)
    await engine.reconcile_once()

    assert (_ORPHAN_A, "@y:company.org") in effectors.kicks
    orphan_reports = [e.payload for e in reported if e.payload["room_id"] == _ORPHAN_A]
    assert orphan_reports and orphan_reports[0]["group_pk"] == "g-gone"
