"""Offboarding scenarios end to end through the reconciler engine (review session S1).

Each test drives :meth:`ReconcilerEngine.reconcile_once` with a real :class:`AccountLifecycleManager`
against an in-memory Authentik directory, a fake Synapse user list and recording effectors. The
tests only talk to the engine and to the fakes, so they describe behaviour, not internals: they
reproduce the review findings LIF-01, SEC-01, MNT-02, LIF-02, LIF-03, LIF-05, REC-14, LIF-06,
LIF-10 and LIV-02 (docs/review_26-11-04/04-lifecycle.md) and stay valid across refactorings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from onbot.clients.mas_admin import SESSION_KINDS, MasCapabilities
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.guard import (
    DestructiveAction,
    DestructiveActionGuard,
    GuardDecision,
    GuardLimits,
    InputCounts,
    MatrixAccountDataGuardStore,
    MemoryGuardStore,
)
from onbot.lifecycle.accounts import (
    AccountLifecycleManager,
    LifecycleLedger,
    MasLifecycleEffectors,
)
from onbot.reconciler.effectors import DryRunEffectors
from onbot.reconciler.engine import ReconcilerEngine

SERVER = "company.org"
DAY = 60 * 60 * 24

_BASE: dict[str, Any] = {
    "synapse_server": {
        "server_name": SERVER,
        "server_url": "https://internal.matrix",
        "bot_user_id": f"@bot:{SERVER}",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
}


def mx(local: str) -> str:
    return f"@{local}:{SERVER}"


# --- fakes -------------------------------------------------------------------------------------


class Directory:
    """In-memory Authentik that honours the user filters onbot sends (path, attributes, groups)."""

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self._next_pk = 1
        self.blind = False  # simulate a token that suddenly sees nobody (200 with an empty list)
        self.visible: set[str] | None = None  # a token narrowed to these usernames (REC-01)

    def add(
        self,
        username: str,
        *,
        active: bool = True,
        path: str = "users",
        attributes: dict[str, Any] | None = None,
        groups: list[str] | None = None,
    ) -> int:
        pk = self._next_pk
        self._next_pk += 1
        self.users[pk] = {
            "pk": pk,
            "username": username,
            "is_active": active,
            "path": path,
            "attributes": attributes or {},
            "groups": groups or [],
        }
        return pk

    def set_active(self, pk: int, active: bool) -> None:
        self.users[pk]["is_active"] = active

    def delete(self, pk: int) -> None:
        del self.users[pk]

    async def list_users(
        self,
        *,
        filter_by_path: str | None = None,
        filter_by_attribute: str | dict[str, Any] | None = None,
        filter_groups_by_pk: str | list[str] | None = None,
        filter_is_active: bool | None = True,
        **_: Any,
    ) -> list[dict[str, Any]]:
        if self.blind:
            return []
        attrs = (
            json.loads(filter_by_attribute) if isinstance(filter_by_attribute, str) else filter_by_attribute
        )
        groups = [filter_groups_by_pk] if isinstance(filter_groups_by_pk, str) else filter_groups_by_pk
        out = []
        for user in self.users.values():
            if self.visible is not None and user["username"] not in self.visible:
                continue
            if filter_is_active is not None and user["is_active"] is not filter_is_active:
                continue
            if filter_by_path is not None and user["path"] != filter_by_path:
                continue
            if attrs and any(user["attributes"].get(k) != v for k, v in attrs.items()):
                continue
            if groups and not set(groups) & set(user["groups"]):
                continue
            out.append(dict(user, attributes=dict(user["attributes"])))
        return out

    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        return []


class Synapse:
    """The Synapse admin surface the engine reads: just the user list."""

    def __init__(self, *locals_: str) -> None:
        self.mxids: list[str] = [mx(lp) for lp in locals_]

    async def list_users(self) -> list[dict[str, Any]]:
        return [{"name": m} for m in self.mxids]

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return []

    async def list_spaces(self) -> list[dict[str, Any]]:
        return []


class Store:
    """Ledger store that counts saves and can be inspected between passes."""

    def __init__(self) -> None:
        self.raw: dict[str, Any] = {}
        self.saves = 0

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        return json.loads(json.dumps(self.raw.get(data_type, {})))

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        self.raw[data_type] = json.loads(json.dumps(content))
        self.saves += 1

    def ledger(self) -> LifecycleLedger:
        return LifecycleLedger.model_validate(self.raw.get("org.company.onbot.lifecycle", {}))


class FakeMas:
    """MAS admin API: users by username, with lock state. Unknown users answer like a 404."""

    def __init__(self, *locals_: str) -> None:
        self.users: dict[str, dict[str, Any]] = {
            lp: {"id": f"ULID-{lp}", "locked": False, "deactivated": False} for lp in locals_
        }
        self.calls: list[tuple[str, str]] = []
        self.fail_for: set[str] = set()

    def _by_id(self, user_id: str) -> tuple[str, dict[str, Any]]:
        for lp, u in self.users.items():
            if u["id"] == user_id:
                return lp, u
        raise AssertionError(f"unknown MAS id {user_id}")

    async def get_user_id_by_username(self, username: str) -> str | None:
        user = self.users.get(username)
        return None if user is None else str(user["id"])

    async def lock_user(self, user_id: str) -> None:
        lp, u = self._by_id(user_id)
        self.calls.append(("lock", lp))
        if lp in self.fail_for:
            raise RuntimeError(f"MAS 500 for {lp}")
        u["locked"] = True

    async def unlock_user(self, user_id: str) -> None:
        lp, u = self._by_id(user_id)
        self.calls.append(("unlock", lp))
        if lp in self.fail_for:
            raise RuntimeError(f"MAS 500 for {lp}")
        u["locked"] = False

    async def deactivate_user(self, user_id: str, *, skip_erase: bool | None = None) -> None:
        lp, u = self._by_id(user_id)
        self.calls.append(("deactivate", lp))
        if lp in self.fail_for:
            raise RuntimeError(f"MAS 500 for {lp}")
        u["deactivated"] = True
        u["erased"] = skip_erase is not True

    async def capabilities(self) -> MasCapabilities:
        return MasCapabilities(finishable_sessions=frozenset(SESSION_KINDS), deactivate_skip_erase=True)

    async def list_active_session_ids(self, kind: str, user_id: str) -> list[str]:
        lp, u = self._by_id(user_id)
        return [f"{kind}-{lp}"] if kind == "oauth2-sessions" and u.get("session", True) else []

    async def finish_session(self, kind: str, session_id: str) -> bool:
        lp = session_id.removeprefix(f"{kind}-")
        self.calls.append(("finish", lp))
        self.users[lp]["session"] = False
        return True

    async def count_locked_users(self) -> int:
        return sum(1 for u in self.users.values() if u["locked"])

    def ops(self, op: str) -> list[str]:
        return [lp for o, lp in self.calls if o == op]


class FakeMediaAdmin:
    def __init__(self) -> None:
        self.media_deleted: list[str] = []
        self.fail = False

    async def delete_user_media(self, user_id: str, *, limit: int = 100) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("media API 500")
        self.media_deleted.append(user_id)
        return {"total": 0}

    async def count_user_media(self, user_id: str) -> int:
        return 0


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class AllowAllGuard(DestructiveActionGuard):
    """Permits every planned action: these scenarios are about the lifecycle's own decisions.

    The guard has its own tests (``test_guard.py``) and its own scenarios below (``World(guard=True)``).
    """

    def __init__(self) -> None:
        super().__init__(GuardLimits(), MemoryGuardStore(), EventBus())

    async def decide(self, counts: InputCounts, planned: Any) -> GuardDecision:
        actions: list[DestructiveAction] = list(planned)
        return GuardDecision(
            counts=counts,
            permitted=frozenset(a.key for a in actions),
            held=(),
            reasons=(),
            input_tripped=False,
            confirmed_batches=(),
        )


class World:
    """One deployment: directory, Synapse, MAS, ledger and an engine wired to all of them.

    ``guard=True`` puts the real destructive-action guard in front of the lifecycle, with its state in
    the same account-data store as the ledger.
    """

    def __init__(
        self,
        *,
        dry_run: bool = False,
        delete_after: int | None = None,
        guard: bool = False,
        preview: bool = False,
        **sync: Any,
    ) -> None:
        self.config = OnbotConfig.model_validate(_BASE)
        self.config.sync_matrix_rooms_based_on_authentik_groups.enabled = False
        self.config.create_matrix_rooms_in_a_matrix_space.enabled = False
        sync_cfg = self.config.sync_authentik_users_with_matrix_rooms
        for key, value in sync.items():
            setattr(sync_cfg, key, value)
        lc = sync_cfg.deactivate_disabled_authentik_users_in_matrix
        lc.enabled = True
        lc.dry_run = dry_run
        lc.deactivate_after_n_sec = 0
        lc.delete_after_n_sec = delete_after
        self.directory = Directory()
        self.directory.add("akadmin", path="goauthentik.io")  # a real Authentik is never empty
        self.synapse = Synapse()
        self.mas = FakeMas()
        self.media = FakeMediaAdmin()
        self.store = Store()
        self.clock = Clock()
        self.events = EventBus()
        self.reports: list[Event] = []
        signal = getattr(Signal, "lifecycle_attention", None)
        if signal is not None:
            self.events.subscribe(signal, self._on_report)
        self.guard_events: list[Event] = []
        self.events.subscribe(Signal.destructive_actions_suppressed, self._on_guard)
        self.events.subscribe(Signal.guard_batch_closed, self._on_guard)
        self.events.subscribe(Signal.guard_state_unreadable, self._on_guard)
        from onbot.lifecycle.accounts import MatrixAccountDataLedgerStore

        self.manager = AccountLifecycleManager(
            self.config,
            MatrixAccountDataLedgerStore(self.store, mx("bot"), SERVER),
            MasLifecycleEffectors(self.mas, synapse_admin=self.media),
            clock=self.clock,
            preview=preview,
        )
        self.guard: DestructiveActionGuard = (
            DestructiveActionGuard(
                GuardLimits.from_config(self.config),
                MatrixAccountDataGuardStore(self.store, mx("bot"), SERVER),
                self.events,
                clock=self.clock,
            )
            if guard
            else AllowAllGuard()
        )
        self.engine = ReconcilerEngine(
            self.config,
            self.directory,  # type: ignore[arg-type]
            self.synapse,  # type: ignore[arg-type]
            DryRunEffectors(),
            events=self.events,
            lifecycle=self.manager,
            guard=self.guard,
            dry_run=preview,
        )

    async def _on_report(self, event: Event) -> None:
        self.reports.append(event)

    async def _on_guard(self, event: Event) -> None:
        self.guard_events.append(event)

    def person(self, local: str, **kwargs: Any) -> int:
        """An Authentik user with a provisioned Matrix account and a MAS user."""
        self.synapse.mxids.append(mx(local))
        self.mas.users[local] = {"id": f"ULID-{local}", "locked": False, "deactivated": False}
        return self.directory.add(local, **kwargs)

    async def tick(self, advance: float = 1.0) -> None:
        self.clock.now += advance
        await self.engine.reconcile_once()

    @property
    def lc(self) -> Any:
        return (
            self.config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
        )

    def locked(self, local: str) -> bool:
        return bool(self.mas.users[local]["locked"])


# --- LIF-01 / SEC-01 / MNT-02: no re-enable without positive evidence ------------------------------


async def test_locked_user_deleted_in_authentik_stays_locked_and_proceeds_to_erase() -> None:
    w = World(delete_after=DAY)
    pk = w.person("alice")
    w.directory.set_active(pk, False)
    await w.tick()  # mark
    await w.tick()  # lock
    assert w.locked("alice")

    w.directory.delete(pk)
    await w.tick()
    assert w.mas.ops("unlock") == []
    assert w.locked("alice")
    assert mx("alice") in w.store.ledger().entries  # still tracked

    await w.tick(DAY)  # the erase cooldown runs from the lock, unaffected by the deletion
    assert w.mas.ops("deactivate") == ["alice"]
    assert w.mas.ops("unlock") == []


@pytest.mark.parametrize("change", ["attribute", "authentik_ignore", "matrix_ignore"])
async def test_locked_user_who_drops_out_of_scope_is_frozen_not_unlocked(change: str) -> None:
    w = World(delete_after=DAY, sync_only_users_with_authentik_attributes={"chat": True})
    pk = w.person("alice", attributes={"chat": True})
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    assert w.locked("alice")

    if change == "attribute":
        w.directory.users[pk]["attributes"] = {}
    elif change == "authentik_ignore":
        w.config.authentik_user_ignore_list = ["alice"]
    else:
        w.config.matrix_user_ignore_list = [mx("alice")]
    await w.tick()
    await w.tick(DAY * 2)  # frozen: no unlock, and no further destructive step either (D9)
    assert w.mas.ops("unlock") == []
    assert w.mas.ops("deactivate") == []
    assert w.locked("alice")
    assert mx("alice") in w.store.ledger().entries


async def test_ignore_list_freeze_survives_the_deletion_of_the_user() -> None:
    """Wave 1 review: the ignore list names present users, deleting the user must not end the freeze."""
    w = World(delete_after=DAY)
    pk = w.person("alice")
    w.directory.set_active(pk, False)
    await w.tick()  # mark
    await w.tick()  # lock
    w.config.authentik_user_ignore_list = ["alice"]
    await w.tick()  # frozen
    w.directory.delete(pk)
    await w.tick(DAY * 2)  # the erase cooldown has long elapsed
    await w.tick(DAY * 2)
    assert w.mas.ops("deactivate") == []
    assert w.mas.ops("unlock") == []
    assert w.store.ledger().entries[mx("alice")].frozen_reason is not None
    assert [e.payload["action"] for e in w.reports if e.payload.get("mxid") == mx("alice")] == ["freeze"]

    w.config.authentik_user_ignore_list = []  # an admin lifts the exemption: offboarding resumes
    await w.tick()
    assert w.mas.ops("deactivate") == ["alice"]


async def test_ignore_list_freeze_recorded_without_a_username_stays_after_deletion() -> None:
    """An entry frozen before the username was recorded fails closed once the user is deleted."""
    w = World(delete_after=DAY)
    pk = w.person("alice")
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    w.config.authentik_user_ignore_list = ["alice"]
    await w.tick()
    raw = w.store.raw["org.company.onbot.lifecycle"]
    raw["entries"][mx("alice")].pop("ignored_username", None)  # as written by the previous release
    w.directory.delete(pk)
    w.config.authentik_user_ignore_list = []
    await w.tick(DAY * 2)
    assert w.mas.ops("deactivate") == []


async def test_locked_user_whose_mapping_attribute_disappears_is_not_unlocked() -> None:
    """The ledger remembers which Authentik user it locked, so a mapping change is no escape."""
    w = World(delete_after=DAY)
    pk = w.person("alice")
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    w.config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute = (
        "attributes.matrix_name"
    )
    await w.tick()
    assert w.mas.ops("unlock") == []
    await w.tick(DAY)  # still disabled in Authentik: offboarding continues
    assert w.mas.ops("deactivate") == ["alice"]


async def test_frozen_user_is_reported_once() -> None:
    w = World(sync_only_users_with_authentik_attributes={"chat": True})
    pk = w.person("alice", attributes={"chat": True})
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    w.directory.users[pk]["attributes"] = {}
    await w.tick()
    await w.tick()
    await w.tick()
    frozen = [e for e in w.reports if e.payload.get("mxid") == mx("alice")]
    assert len(frozen) == 1
    assert frozen[0].payload["action"] == "freeze"


async def test_frozen_user_who_returns_in_scope_and_active_is_unlocked() -> None:
    w = World(sync_only_users_with_authentik_attributes={"chat": True})
    pk = w.person("alice", attributes={"chat": True})
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    w.directory.users[pk]["attributes"] = {}
    await w.tick()
    assert w.mas.ops("unlock") == []

    w.directory.users[pk]["attributes"] = {"chat": True}
    w.directory.set_active(pk, True)
    await w.tick()
    assert w.mas.ops("unlock") == ["alice"]
    assert not w.locked("alice")
    assert mx("alice") not in w.store.ledger().entries


async def test_real_reenable_unlocks() -> None:
    w = World()
    pk = w.person("bob")
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    assert w.locked("bob")
    w.directory.set_active(pk, True)
    await w.tick()
    assert w.mas.ops("unlock") == ["bob"]
    assert mx("bob") not in w.store.ledger().entries


async def test_empty_authentik_answer_aborts_the_lifecycle_step(caplog: pytest.LogCaptureFixture) -> None:
    w = World()
    pk = w.person("alice")
    w.person("carol")
    await w.tick()  # carol becomes a known user
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    assert w.locked("alice")
    saves = w.store.saves

    w.directory.blind = True
    with caplog.at_level(logging.WARNING):
        await w.tick(DAY)
    assert w.mas.calls == [("lock", "alice"), ("finish", "alice")]  # no unlock, no lock of carol
    assert w.store.saves == saves
    assert "lifecycle step aborted" in caplog.text


# --- LIF-02: known users who are deleted are offboarded; never-mapped accounts are not ------------


async def test_known_user_deleted_directly_is_offboarded() -> None:
    w = World(delete_after=DAY)
    pk = w.person("dora")
    await w.tick()  # seen active and mapped: dora becomes a known user
    w.directory.delete(pk)
    await w.tick()  # mark (deleted)
    assert mx("dora") in w.store.ledger().entries
    assert w.mas.calls == []
    await w.tick()  # cooldown 0: lock
    assert w.locked("dora")
    await w.tick(DAY)
    assert w.mas.ops("deactivate") == ["dora"]


async def test_never_mapped_matrix_account_is_never_touched() -> None:
    w = World(delete_after=0)
    w.synapse.mxids.append(mx("bridge"))  # no Authentik user ever mapped to it
    w.mas.users["bridge"] = {"id": "ULID-bridge", "locked": False, "deactivated": False}
    w.person("alice")
    for _ in range(4):
        await w.tick(DAY)
    assert w.mas.calls == []
    assert w.store.ledger().entries == {}


# --- LIF-05 / REC-14: one scope definition, guard against all active users -----------------------


async def test_disabled_user_outside_the_path_scope_is_not_offboarded() -> None:
    w = World(sync_only_users_in_authentik_pathes=["users/staff"])
    w.person("svc", path="service", active=False)  # same localpart as a Matrix-only account
    for _ in range(3):
        await w.tick()
    assert w.mas.calls == []
    assert w.store.ledger().entries == {}


async def test_mxid_of_an_active_user_outside_the_scope_is_never_locked() -> None:
    """E6: a reused mapping value must not lock the live account it now belongs to."""
    w = World(
        sync_only_users_in_authentik_pathes=["staff"],
        authentik_username_mapping_attribute="attributes.matrix_name",
    )
    w.synapse.mxids.append(mx("frank"))
    w.mas.users["frank"] = {"id": "ULID-frank", "locked": False, "deactivated": False}
    w.directory.add("frank.old", path="staff", active=False, attributes={"matrix_name": "frank"})
    w.directory.add("frank", path="contractors", active=True, attributes={"matrix_name": "frank"})
    for _ in range(3):
        await w.tick()
    assert w.mas.calls == []


# --- D14: a known user who is disabled and leaves the scope at once is still offboarded ----------

_SCOPES: dict[str, dict[str, Any]] = {
    "path": {"sync_only_users_in_authentik_pathes": ["staff"]},
    "group": {"sync_only_users_of_groups_with_id": ["grp-staff"]},
    "attribute": {"sync_only_users_with_authentik_attributes": {"chat": True}},
}
_IN_SCOPE: dict[str, dict[str, Any]] = {
    "path": {"path": "staff"},
    "group": {"path": "staff", "groups": ["grp-staff"]},
    "attribute": {"path": "staff", "attributes": {"chat": True}},
}


def _leave_scope(w: World, pk: int, variant: str) -> None:
    """The usual "remove somebody" flow in Authentik: archive the user and strip what synced them."""
    if variant == "path":
        w.directory.users[pk]["path"] = "archive"
    elif variant == "group":
        w.directory.users[pk]["groups"] = []
    else:
        w.directory.users[pk]["attributes"] = {}


@pytest.mark.parametrize("variant", sorted(_SCOPES))
async def test_known_user_disabled_and_moved_out_of_scope_is_offboarded(variant: str) -> None:
    w = World(delete_after=DAY, **_SCOPES[variant])
    pk = w.person("erin", **_IN_SCOPE[variant])
    await w.tick()  # seen active, in scope and mapped: erin becomes a known user
    assert w.store.ledger().entries == {}

    w.directory.set_active(pk, False)
    _leave_scope(w, pk, variant)  # in the same interval as the disable
    await w.tick()  # mark
    assert mx("erin") in w.store.ledger().entries
    await w.tick()  # cooldown 0: lock
    assert w.locked("erin")
    await w.tick(DAY)
    assert w.mas.ops("deactivate") == ["erin"]
    assert w.mas.ops("unlock") == []
    assert [e for e in w.reports if e.payload.get("action") == "freeze"] == []


@pytest.mark.parametrize("variant", sorted(_SCOPES))
async def test_known_locked_user_moved_out_of_scope_while_disabled_continues(variant: str) -> None:
    """Archiving after the lock (a later interval) does not freeze a known user either (D14)."""
    w = World(delete_after=DAY, **_SCOPES[variant])
    pk = w.person("erin", **_IN_SCOPE[variant])
    await w.tick()
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    assert w.locked("erin")
    _leave_scope(w, pk, variant)
    await w.tick(DAY)
    assert w.mas.ops("deactivate") == ["erin"]


@pytest.mark.parametrize("variant", sorted(_SCOPES))
async def test_never_mapped_disabled_account_outside_the_scope_stays_untouched(variant: str) -> None:
    """REC-14 still holds: onbot never saw this user active in scope, so it never binds the account."""
    w = World(delete_after=0, **_SCOPES[variant])
    w.person("erin", **_IN_SCOPE[variant])  # an unrelated known user, so the pass is not trivial
    pk = w.person("svc", path="service")  # Matrix account exists, Authentik user never in scope
    await w.tick()
    w.directory.set_active(pk, False)
    for _ in range(3):
        await w.tick(DAY)
    assert w.mas.calls == []
    assert w.store.ledger().entries == {}


async def test_known_user_reactivated_outside_the_scope_while_locked_is_frozen() -> None:
    """D9 still applies to a user who is active again but outside the scope: no unlock, no erase."""
    w = World(delete_after=DAY, sync_only_users_in_authentik_pathes=["staff"])
    pk = w.person("erin", path="staff")
    await w.tick()
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    assert w.locked("erin")
    w.directory.set_active(pk, True)
    w.directory.users[pk]["path"] = "archive"
    await w.tick()
    await w.tick(DAY * 2)
    assert w.mas.ops("unlock") == [] and w.mas.ops("deactivate") == []
    assert w.locked("erin")
    frozen = [e for e in w.reports if e.payload.get("mxid") == mx("erin")]
    assert [e.payload["action"] for e in frozen] == ["freeze"]


# --- LIF-06: dry-run calls no effector; only onbot's own locks are unlocked ---------------------


async def test_dry_run_never_unlocks() -> None:
    w = World(dry_run=True)
    pk = w.person("yara")
    w.directory.set_active(pk, False)
    await w.tick()  # mark
    w.mas.users["yara"]["locked"] = True  # the security team locks yara by hand
    w.directory.set_active(pk, True)
    await w.tick()
    assert w.mas.calls == []
    assert w.locked("yara")


async def test_live_mode_does_not_unlock_a_lock_onbot_never_set() -> None:
    w = World()
    w.lc.deactivate_after_n_sec = DAY
    pk = w.person("yara")
    w.directory.set_active(pk, False)
    await w.tick()  # marked only
    w.mas.users["yara"]["locked"] = True  # locked by a human
    w.directory.set_active(pk, True)
    await w.tick()
    assert w.mas.ops("unlock") == []
    assert w.locked("yara")
    assert mx("yara") not in w.store.ledger().entries  # the mark is cleared


async def test_dry_run_keeps_a_real_lock_until_live_mode_unlocks_it() -> None:
    w = World()
    pk = w.person("zed")
    w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    assert w.locked("zed")
    lc = w.lc
    lc.dry_run = True
    w.directory.set_active(pk, True)
    await w.tick()
    assert w.mas.ops("unlock") == []
    assert mx("zed") in w.store.ledger().entries  # the lock record survives the dry-run pass
    lc.dry_run = False
    await w.tick()
    assert w.mas.ops("unlock") == ["zed"]


# --- LIF-03: per-user isolation, per-user saves, irreversible steps not repeated -----------------


async def test_one_failing_user_does_not_block_the_others() -> None:
    w = World()
    for name in ("aaa", "zzz"):
        w.directory.set_active(w.person(name), False)
    w.mas.fail_for = {"aaa"}
    await w.tick()  # mark both
    await w.tick()  # lock: aaa fails, zzz must still be locked
    assert w.locked("zzz")
    assert not w.locked("aaa")
    entries = w.store.ledger().entries
    assert entries[mx("zzz")].disabled_user_timestamp is not None
    assert entries[mx("aaa")].disabled_user_timestamp is None


async def test_failing_user_backs_off() -> None:
    w = World()
    w.directory.set_active(w.person("aaa"), False)
    w.mas.fail_for = {"aaa"}
    await w.tick()  # mark
    await w.tick()  # lock fails
    await w.tick()  # within the backoff: no new attempt
    assert w.mas.ops("lock") == ["aaa"]
    w.mas.fail_for = set()
    await w.tick(DAY)  # backoff elapsed: retried and succeeds
    assert w.mas.ops("lock") == ["aaa", "aaa"]
    assert w.locked("aaa")


async def test_erase_is_not_repeated_after_a_later_failure() -> None:
    w = World(delete_after=0)
    for name in ("a0", "aaa"):
        w.directory.set_active(w.person(name), False)
    await w.tick()  # mark
    await w.tick()  # lock
    w.mas.fail_for = {"aaa"}
    for _ in range(3):
        await w.tick(DAY)
    assert w.mas.ops("deactivate").count("a0") == 1


async def test_media_failure_after_deactivation_resumes_without_repeating_or_unlocking() -> None:
    w = World(delete_after=0)
    lc = w.lc
    lc.include_user_media_on_delete = True
    w.directory.set_active(w.person("gdpr"), False)
    await w.tick()  # mark
    await w.tick()  # lock
    w.media.fail = True
    await w.tick(DAY)  # deactivate succeeds, media fails
    w.synapse.mxids.remove(mx("gdpr"))  # Synapse hides deactivated users from the listing
    w.media.fail = False
    await w.tick(DAY)
    assert w.mas.ops("deactivate") == ["gdpr"]
    assert w.mas.ops("unlock") == []
    assert w.media.media_deleted == [mx("gdpr")]
    assert mx("gdpr") not in w.store.ledger().entries


async def test_ledger_is_saved_after_each_user() -> None:
    w = World()
    for name in ("u1", "u2", "u3"):
        w.directory.set_active(w.person(name), False)
    await w.tick()
    before = w.store.saves
    await w.tick()  # three locks
    assert w.store.saves - before >= 3


# --- LIF-10: an unresolvable MAS user is a failure, not a success -------------------------------


async def test_mas_lookup_miss_does_not_advance_and_is_audited_as_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    w = World()
    pk = w.person("ghost")
    del w.mas.users["ghost"]  # MAS answers 404 for the username
    w.directory.set_active(pk, False)
    await w.tick()
    with caplog.at_level(logging.INFO, logger="onbot.lifecycle.audit"):
        await w.tick()
    entry = w.store.ledger().entries[mx("ghost")]
    assert entry.disabled_user_timestamp is None
    audit_lines = [r.getMessage() for r in caplog.records if r.name == "onbot.lifecycle.audit"]
    assert any("FAILED" in line for line in audit_lines)
    assert not any(line.startswith("locked out") for line in audit_lines)


# --- LIV-02: audit wording when erasure is disabled ----------------------------------------------


async def test_lock_audit_line_says_erase_disabled(caplog: pytest.LogCaptureFixture) -> None:
    w = World(delete_after=None)
    w.directory.set_active(w.person("liv"), False)
    await w.tick()
    with caplog.at_level(logging.INFO, logger="onbot.lifecycle.audit"):
        await w.tick()
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "onbot.lifecycle.audit")
    assert "Nones" not in text
    assert "erase disabled" in text


# --- REC-01 / D13: the destructive-action guard in front of the lifecycle ---------------------------


def _proposals(w: World) -> list[Event]:
    return [e for e in w.guard_events if e.signal is Signal.destructive_actions_suppressed]


async def test_a_narrowed_token_locks_no_known_user() -> None:
    """S1 handover: a token that sees one user makes every other known user look deleted."""
    w = World(guard=True)
    for name in ("anna", "ben", "cleo", "dino"):
        w.person(name)
    await w.tick()  # all four become known users; the baseline is five active users

    w.directory.visible = {"anna"}
    await w.tick()  # the others look deleted: marked (bookkeeping only)
    await w.tick(DAY)  # cooldown elapsed: the locks are planned, and paused
    await w.tick(DAY)

    assert w.mas.calls == []
    [proposal] = _proposals(w)
    assert proposal.payload["action"] == "lock"
    assert "from 5 to 1" in proposal.payload["reason"]

    ok, _ = await w.guard.confirm(proposal.payload["batch_id"], "@admin:company.org")
    assert ok
    await w.tick()
    assert sorted(w.mas.ops("lock")) == ["ben", "cleo", "dino"]


async def test_switching_dry_run_off_after_a_long_dry_run_goes_through_the_limit() -> None:
    """LIF-08: every user marked during the dry run would be locked in the first live pass."""
    w = World(guard=True, dry_run=True)
    staff = [w.person(f"p{n:02d}") for n in range(40)]
    await w.tick()
    for week in range(3):  # people leave over weeks, a few at a time: never an implausible drop
        for pk in staff[week * 4 : week * 4 + 4][: 11 - week * 4]:
            w.directory.set_active(pk, False)
        await w.tick(7 * DAY)
    assert w.mas.calls == []  # dry run

    w.lc.dry_run = False
    await w.tick()
    assert w.mas.calls == []  # 11 locks at once: paused, not done
    [proposal] = _proposals(w)
    assert "11 reversible destructive actions" in proposal.payload["reason"]

    await w.guard.confirm(proposal.payload["batch_id"], "@admin:company.org")
    await w.tick()
    assert len(w.mas.ops("lock")) == 11


async def test_a_few_locks_still_run_without_a_confirmation() -> None:
    w = World(guard=True)
    staff = [w.person(f"q{n:02d}") for n in range(40)]
    await w.tick()
    for pk in staff[:3]:
        w.directory.set_active(pk, False)
    await w.tick()
    await w.tick()
    assert len(w.mas.ops("lock")) == 3
    assert _proposals(w) == []


async def test_dry_run_audit_lines_are_written_once_per_stage_not_every_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LIF-08: the audit channel used to repeat "would lock out" for every user on every tick."""
    w = World(dry_run=True)
    pk = w.person("dry")
    w.directory.set_active(pk, False)
    with caplog.at_level(logging.INFO, logger="onbot.lifecycle.audit"):
        for _ in range(5):
            await w.tick()
    lines = [r.getMessage() for r in caplog.records if r.name == "onbot.lifecycle.audit"]
    assert sum("DRY-RUN: would lock out" in line for line in lines) == 1

    # A new stage is news again: the user comes back (nothing was locked) and leaves once more.
    w.directory.set_active(pk, True)
    await w.tick()
    w.directory.set_active(pk, False)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="onbot.lifecycle.audit"):
        for _ in range(3):
            await w.tick()
    lines = [r.getMessage() for r in caplog.records if r.name == "onbot.lifecycle.audit"]
    assert sum("DRY-RUN: would lock out" in line for line in lines) == 1


async def test_a_preview_shows_what_switching_dry_run_off_would_pause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LIF-08 / MNT-09: ``reconcile-once --dry-run`` with ``dry_run: false`` previews the first live pass."""
    w = World(guard=True, preview=True)
    staff = [w.person(f"r{n:02d}") for n in range(40)]
    await w.tick()
    for week in range(3):
        for pk in staff[week * 4 : week * 4 + 4][: 11 - week * 4]:
            w.directory.set_active(pk, False)
        await w.tick(7 * DAY)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await w.tick()
    assert w.mas.calls == []
    assert "[dry-run] guard: paused 11 destructive action(s)" in caplog.text
    assert "11 reversible destructive actions in one pass" in caplog.text
    assert _proposals(w) == []  # a preview records no batch


# --- wave 2 review: an unreadable ledger through the engine ----------------------------------------

LEDGER_TYPE = "org.company.onbot.lifecycle"
GUARD_TYPE = "org.company.onbot.guard"


@pytest.mark.parametrize("blob_key", [LEDGER_TYPE, "org.company.onbot.known_users"])
async def test_an_unreadable_ledger_aborts_the_step_reports_once_and_the_pass_still_succeeds(
    blob_key: str,
) -> None:
    """S1 reported an unreadable ledger once; the guard's planning step (S3) lost that and raised."""
    w = World(guard=True)
    pk = w.person("anna")
    w.person("ben")
    await w.tick()
    broken = {"schema_version": 99, "entries": {}} if blob_key == LEDGER_TYPE else {"users": {"1": 5}}
    w.store.raw[blob_key] = broken
    w.directory.set_active(pk, False)
    for _ in range(3):
        await w.tick()  # must not raise
        assert w.engine.last_reconcile_at is not None

    aborts = [e for e in w.reports if e.payload.get("action") == "abort"]
    assert len(aborts) == 1
    assert w.mas.calls == []
    assert w.store.raw[blob_key] == broken  # never overwritten


async def test_an_unreadable_ledger_does_not_close_the_open_batches() -> None:
    """Its locks are unknown while the step is refused; that is not "no longer applies"."""
    w = World(guard=True)
    for name in ("anna", "ben", "cleo", "dino"):
        w.person(name)
    await w.tick()
    w.directory.visible = {"anna"}
    await w.tick()
    await w.tick(DAY)
    [proposal] = _proposals(w)
    w.store.raw[LEDGER_TYPE] = {"schema_version": 99, "entries": {}}
    await w.tick()
    assert proposal.payload["batch_id"] in w.store.raw[GUARD_TYPE]["batches"]
    assert [e for e in w.guard_events if e.signal is Signal.guard_batch_closed] == []


# --- wave 2 review: rejected batches and an unreadable guard state ---------------------------------


async def _rejected_narrowed_token(w: World) -> str:
    for name in ("anna", "ben", "cleo", "dino"):
        w.person(name)
    await w.tick()
    w.directory.visible = {"anna"}
    await w.tick()
    await w.tick(DAY)
    [proposal] = _proposals(w)
    batch_id: str = proposal.payload["batch_id"]
    ok, _ = await w.guard.reject(batch_id, "@admin:company.org")
    assert ok
    await w.tick()
    assert w.mas.calls == []
    return batch_id


async def test_a_rejected_batch_ends_with_its_incident_and_a_genuine_deletion_is_still_offboarded() -> None:
    """The token is fixed, but dino really was deleted meanwhile: the reject must not hold that forever."""
    w = World(guard=True)
    batch_id = await _rejected_narrowed_token(w)
    w.directory.visible = None
    dino_pk = next(pk for pk, u in w.directory.users.items() if u["username"] == "dino")
    w.directory.delete(dino_pk)
    for _ in range(3):
        await w.tick(DAY)
    assert w.mas.ops("lock") == ["dino"]
    ended = [e for e in w.guard_events if e.payload.get("outcome") == "rejection_ended"]
    assert [e.payload["batch_id"] for e in ended] == [batch_id]  # reported once


async def test_a_lock_record_is_not_forgotten_while_the_user_list_is_implausible() -> None:
    """Wave 2 review: retention read "deleted from Authentik" as final while layer 1 was tripped."""
    w = World(guard=True)
    for name in ("anna", "ben", "cleo"):
        w.person(name)
    dino = w.person("dino")
    await w.tick()
    w.directory.set_active(dino, False)
    await w.tick()  # mark
    await w.tick()  # lock
    assert w.locked("dino")

    w.directory.visible = {"anna"}  # dino is only disabled, but now looks deleted
    for _ in range(3):
        await w.tick(100 * DAY)
    assert mx("dino") in w.store.ledger().entries
    assert w.mas.ops("unlock") == []

    w.directory.visible = None
    w.directory.set_active(dino, True)  # dino comes back: onbot still knows it set the lock
    await w.tick()
    assert w.mas.ops("unlock") == ["dino"]


async def test_a_step_that_became_due_after_the_plan_is_not_logged_as_waiting_for_an_admin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    w = World(guard=True)
    w.lc.deactivate_after_n_sec = 10
    for name in ("anna", "ben", "cleo"):
        w.person(name)
    pk = w.person("dino")
    await w.tick()
    w.directory.set_active(pk, False)
    await w.tick()  # mark

    plan = w.manager.plan_accounts

    async def plan_then_let_time_pass(snapshot: Any) -> Any:
        planned = await plan(snapshot)
        w.clock.now += 20  # the lock becomes due between the plan and the run
        return planned

    w.manager.plan_accounts = plan_then_let_time_pass  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="onbot.lifecycle.audit"):
        await w.tick()
    assert w.mas.calls == []
    assert "DEFERRED logout of @dino:company.org" in caplog.text
    assert "until an admin confirms" not in caplog.text
    w.manager.plan_accounts = plan  # type: ignore[method-assign]
    await w.tick()
    assert w.mas.ops("lock") == ["dino"]


async def test_an_unreadable_guard_state_holds_the_locks_of_a_rejected_batch() -> None:
    """Starting from empty dropped the baseline and the rejection, so the narrowed token's locks ran."""
    w = World(guard=True)
    await _rejected_narrowed_token(w)
    broken = {"schema_version": 1, "baseline": {}, "batches": {"x": {"bogus": 1}}}
    w.store.raw[GUARD_TYPE] = broken
    for _ in range(3):
        await w.tick()
    assert w.mas.calls == []
    assert w.store.raw[GUARD_TYPE] == broken
    assert [e.signal for e in w.guard_events].count(Signal.guard_state_unreadable) == 1
