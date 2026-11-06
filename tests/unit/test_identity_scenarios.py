"""Renames and identity conflicts end to end through the reconciler engine (D8, SEC-07, MNT-13).

The engine runs with the real offboarding lifecycle and the real destructive-action guard against an
in-memory Authentik, a fake Synapse with group rooms and stateful power levels, and the MAS fake of
the lifecycle scenarios. The tests assert what a person would notice: a renamed user is not kicked,
not demoted, not locked and not welcomed again; a changed mapping attribute moves nobody; two
Authentik users resolving to one account leave that account alone.
"""

from __future__ import annotations

from typing import Any, ClassVar

from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.guard import DestructiveActionGuard, GuardLimits, MatrixAccountDataGuardStore
from onbot.identity import IdentityResolver
from onbot.lifecycle.accounts import (
    AccountLifecycleManager,
    MasLifecycleEffectors,
    MatrixAccountDataLedgerStore,
)
from onbot.reconciler.engine import ReconcilerEngine
from onbot.reconciler.state import OnbotRoomType
from tests.unit.room_fakes import IdentityScanMixin
from tests.unit.test_engine import RecordingEffectors
from tests.unit.test_lifecycle_scenarios import Clock, FakeMas, FakeMediaAdmin, Store

SERVER = "company.org"
ROOM1, ROOM2 = f"!room1:{SERVER}", f"!room2:{SERVER}"
_BASE: dict[str, Any] = {
    "synapse_server": {
        "server_name": SERVER,
        "server_url": "https://internal.matrix",
        "bot_user_id": f"@bot:{SERVER}",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
    "admin_room": {"enabled": False},
}
_GROUPS = [
    {"pk": "g1", "name": "Team", "attributes": {"chat-systemwide-powerlevel": 50}},
    {"pk": "g2", "name": "Other", "attributes": {}},
]


def mx(local: str) -> str:
    return f"@{local}:{SERVER}"


class Directory:
    """In-memory Authentik: users with group membership; every user is in the sync scope."""

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}

    def add(self, pk: int, username: str, *groups: str, **attributes: Any) -> None:
        self.users[pk] = {
            "pk": pk,
            "uid": f"hash-{pk}",
            "username": username,
            "is_active": True,
            "is_superuser": False,
            "path": "users",
            "attributes": dict(attributes),
            "groups_obj": [{"pk": g} for g in groups],
        }

    async def list_users(self, *, filter_is_active: bool | None = True, **_: Any) -> list[dict[str, Any]]:
        return [
            dict(u, attributes=dict(u["attributes"]))
            for u in self.users.values()
            if filter_is_active is None or u["is_active"] is filter_is_active
        ]

    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        def members(group_pk: str) -> list[int]:
            return [pk for pk, u in self.users.items() if {"pk": group_pk} in u["groups_obj"]]

        return [dict(g, users=members(g["pk"])) for g in _GROUPS]


class Synapse(IdentityScanMixin):
    """Synapse admin surface: accounts, two group rooms (bot-created, with markers, D20), members."""

    room_markers: ClassVar[dict[str, tuple[OnbotRoomType, str | None]]] = {
        ROOM1: (OnbotRoomType.group_room, "g1"),
        ROOM2: (OnbotRoomType.group_room, "g2"),
    }

    def __init__(self) -> None:
        self.room_state_events = {}
        self.mxids: list[str] = [mx("bot")]
        self.members: dict[str, set[str]] = {ROOM1: {mx("bot")}, ROOM2: {mx("bot")}}
        self.added: list[tuple[str, str]] = []

    async def list_users(self) -> list[dict[str, Any]]:
        return [{"name": m} for m in self.mxids]

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return [
            {"room_id": ROOM1, "canonical_alias": f"#g1:{SERVER}", "name": "Team"},
            {"room_id": ROOM2, "canonical_alias": f"#g2:{SERVER}", "name": "Other"},
        ]

    async def list_spaces(self) -> list[dict[str, Any]]:
        return []

    async def list_room_members(self, room_id: str) -> list[str]:
        return sorted(self.members[room_id])

    async def add_user_to_room(self, room_id: str, user_id: str) -> None:
        self.added.append((room_id, user_id))
        self.members[room_id].add(user_id)

    async def room_is_blocked(self, room_id: str) -> bool:
        return False

    async def get_room_details(self, room_id: str) -> dict[str, Any]:
        return {"room_id": room_id, "version": "12", "creator": mx("bot")}


class Effectors(RecordingEffectors):
    """Recording effectors whose power levels are state: a pass sees what the last one wrote."""

    def __init__(self, synapse: Synapse) -> None:
        super().__init__()
        self.synapse = synapse
        self.levels: dict[str, dict[str, Any]] = {}

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        return dict(self.levels.get(room_id, {}))

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        await super().set_room_power_levels(room_id, power_levels)
        self.levels[room_id] = dict(power_levels)

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
        await super().kick_user(room_id, user_id, reason)
        self.synapse.members[room_id].discard(user_id)


class LinkedMas(FakeMas):
    """MAS with upstream links: subject ``hash-<pk>`` -> the localpart MAS fixed at first login."""

    def __init__(self) -> None:
        super().__init__()
        self.subjects: dict[str, str] = {}

    async def list_upstream_links(self, *, provider_id: str | None = None) -> list[Any]:
        from onbot.clients.mas_admin import UpstreamLink

        return [UpstreamLink("P1", s, self.users[lp]["id"]) for s, lp in self.subjects.items()]

    async def list_usernames(self) -> dict[str, str]:
        return {str(u["id"]): lp for lp, u in self.users.items()}


class World:
    def __init__(self, *, mas_identity: bool = False, attribute: str = "username") -> None:
        raw = dict(_BASE)
        if mas_identity:
            raw["mas_admin"] = {"url": "https://mas", "client_id": "c", "client_secret": "s"}
        self.config = OnbotConfig.model_validate(raw)
        self.config.create_matrix_rooms_in_a_matrix_space.enabled = False
        sync = self.config.sync_authentik_users_with_matrix_rooms
        sync.authentik_username_mapping_attribute = attribute
        lc = sync.deactivate_disabled_authentik_users_in_matrix
        lc.enabled = True
        lc.dry_run = False
        lc.deactivate_after_n_sec = 0
        self.directory = Directory()
        self.synapse = Synapse()
        self.effectors = Effectors(self.synapse)
        self.mas = LinkedMas()
        self.store = Store()
        self.clock = Clock()
        self.events = EventBus()
        self.identity_reports: list[str] = []
        self.guard_events: list[Event] = []
        self.synced: list[str] = []
        self.events.subscribe(Signal.identity_attention, self._on_identity)
        self.events.subscribe(Signal.destructive_actions_suppressed, self._on_guard)
        self.events.subscribe(Signal.users_mapped, self._on_synced)
        ledger_store = MatrixAccountDataLedgerStore(self.store, mx("bot"), SERVER)
        manager = AccountLifecycleManager(
            self.config,
            ledger_store,
            MasLifecycleEffectors(self.mas, synapse_admin=FakeMediaAdmin()),
            clock=self.clock,
        )
        guard = DestructiveActionGuard(
            GuardLimits.from_config(self.config),
            MatrixAccountDataGuardStore(self.store, mx("bot"), SERVER),
            self.events,
            clock=self.clock,
        )
        identity = IdentityResolver(
            self.config, store=ledger_store, mas=self.mas if mas_identity else None, events=self.events
        )
        self.engine = ReconcilerEngine(
            self.config,
            self.directory,  # type: ignore[arg-type]
            self.synapse,  # type: ignore[arg-type]
            self.effectors,
            events=self.events,
            lifecycle=manager,
            guard=guard,
            identity=identity,
        )

    async def _on_identity(self, event: Event) -> None:
        self.identity_reports.append(event.payload["text"])

    async def _on_guard(self, event: Event) -> None:
        self.guard_events.append(event)

    async def _on_synced(self, event: Event) -> None:
        self.synced.extend(sorted(event.payload["mxids"]))

    def person(self, pk: int, local: str, *groups: str, **attributes: Any) -> None:
        """An Authentik user who logged in once: MAS fixed ``local`` as their localpart."""
        self.directory.add(pk, local, *groups, **attributes)
        self.synapse.mxids.append(mx(local))
        self.mas.users[local] = {"id": f"ULID-{local}", "locked": False, "deactivated": False}
        self.mas.subjects[f"hash-{pk}"] = local

    async def tick(self) -> None:
        self.clock.now += 1
        await self.engine.reconcile_once()

    def snapshot(self) -> tuple[set[str], set[str], dict[str, Any]]:
        return set(self.synapse.members[ROOM1]), set(self.synapse.members[ROOM2]), dict(self.effectors.levels)


async def _settled(w: World) -> tuple[set[str], set[str], dict[str, Any]]:
    await w.tick()
    await w.tick()
    w.effectors.kicks.clear()
    w.synapse.added.clear()
    w.synced.clear()
    return w.snapshot()


def _assert_untouched(w: World, before: tuple[set[str], set[str], dict[str, Any]]) -> None:
    assert w.effectors.kicks == []
    assert w.synapse.added == []
    assert w.snapshot() == before  # same members, same power levels
    assert w.mas.calls == []  # nobody locked
    assert w.guard_events == []  # nothing for the guard to hold either


async def test_a_renamed_user_keeps_rooms_and_power_levels_and_is_not_locked() -> None:
    w = World()
    w.person(1, "alice", "g1")
    w.person(2, "bob", "g1")
    before = await _settled(w)
    assert before[0] == {mx("bot"), mx("alice"), mx("bob")}
    assert before[2][ROOM1]["users"] == {mx("alice"): 50, mx("bob"): 50}

    w.directory.users[1]["username"] = "alicia"  # married, renamed in Authentik
    for _ in range(3):
        await w.tick()
    _assert_untouched(w, before)
    # Still the same synced account: the welcome flow sees @alice again, never an @alicia.
    assert mx("alicia") not in w.synced and mx("alice") in w.synced
    assert len(w.identity_reports) == 1 and "'alicia' (pk 1)" in w.identity_reports[0]


async def test_a_changed_mapping_attribute_kicks_and_locks_nobody() -> None:
    """A mass re-mapping by config would have planned a kick per user; with the binding it plans none."""
    w = World()
    for pk in range(1, 16):
        w.person(pk, f"user{pk}", "g1", matrix_name=f"m{pk}")
    before = await _settled(w)
    assert len(before[0]) == 16

    w.config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute = (
        "attributes.matrix_name"
    )
    await w.tick()
    await w.tick()
    _assert_untouched(w, before)
    assert len(w.identity_reports) == 1 and "15 Authentik user(s)" in w.identity_reports[0]


async def test_a_newcomer_naming_an_existing_account_leaves_it_alone() -> None:
    """SEC-07: carl's attribute names alice's account, which only a binding ties to alice (no MAS).
    Neither carl's groups reach @alice nor is @alice kicked, demoted or (once disabled) locked: both
    are skipped until an admin decides."""
    w = World(attribute="attributes.mx")
    w.person(1, "alice", "g1", mx="alice")
    before = await _settled(w)

    w.directory.add(3, "carl", "g2", mx="alice")
    await w.tick()
    await w.tick()
    _assert_untouched(w, before)
    assert (ROOM2, mx("alice")) not in w.synapse.added
    assert any("'carl' (pk 3)" in r and mx("alice") in r for r in w.identity_reports)

    w.directory.users[1]["is_active"] = False  # alice leaves the company meanwhile
    await w.tick()
    await w.tick()
    assert w.mas.calls == []  # frozen, never locked while the conflict stands


async def test_with_mas_the_linked_owner_keeps_her_account_against_a_guess() -> None:
    """D21: MAS links @alice to alice; carl only names it by the attribute. Carl gets nothing,
    alice stays managed as usual: untouched while active, locked once disabled."""
    w = World(mas_identity=True, attribute="attributes.mx")
    w.person(1, "alice", "g1", mx="alice")
    for pk in range(2, 8):  # one leaver of seven stays under the guard's 20 % drop limit
        w.person(pk, f"user{pk}", "g1", mx=f"user{pk}")
    before = await _settled(w)
    w.mas.calls.clear()

    w.directory.add(30, "carl", "g2", mx="alice")
    await w.tick()
    await w.tick()
    _assert_untouched(w, before)
    assert (ROOM2, mx("alice")) not in w.synapse.added
    assert (
        sum("'carl' (pk 30)" in r and "MAS links that account to 'alice'" in r for r in w.identity_reports)
        == 1
    )

    w.directory.users[1]["is_active"] = False
    await w.tick()  # mark
    await w.tick()  # lock: the guess froze nothing
    assert w.mas.ops("lock") == ["alice"]


async def test_with_mas_a_rename_is_resolved_through_the_upstream_link() -> None:
    w = World(mas_identity=True)
    w.person(1, "alice", "g1")
    w.person(2, "bob", "g1")
    before = await _settled(w)
    w.mas.calls.clear()

    w.directory.users[1]["username"] = "alicia"
    w.directory.users[2]["username"] = "robert"
    await w.tick()
    await w.tick()
    _assert_untouched(w, before)


async def test_with_mas_an_attribute_change_on_a_fresh_deployment_moves_nobody() -> None:
    """MAS knows who is who even before onbot recorded a single binding."""
    w = World(mas_identity=True, attribute="attributes.matrix_name")
    for pk in range(1, 13):
        w.person(pk, f"user{pk}", "g1", matrix_name=f"other{pk}")
    before = await _settled(w)
    assert len(before[0]) == 13  # everybody joined by the MAS answer, not by the wrong attribute
    w.store.raw.pop("org.company.onbot.known_users", None)  # forget the bindings again
    await w.tick()
    _assert_untouched(w, before)


async def test_an_unreadable_binding_record_projects_no_memberships() -> None:
    w = World()
    w.person(1, "alice", "g1")
    before = await _settled(w)
    w.store.raw["org.company.onbot.known_users"] = {"users": {"1": 5}}
    w.directory.users[1]["username"] = "alicia"
    await w.tick()
    await w.tick()
    _assert_untouched(w, before)
    assert sum("known_users" in r for r in w.identity_reports) == 1
