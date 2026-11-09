"""Highest-rigor tests for the quarantined account lifecycle (ADR-0005, G9.*).

Covers the pure state machine and account classification exhaustively, the known-users record, the
manager's dry-run/live behaviour and full multi-tick progression, the account-data ledger store, and
both effector implementations. End-to-end offboarding scenarios through the reconciler live in
``test_lifecycle_scenarios.py``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from onbot.clients.mas_admin import SESSION_KINDS, MasCapabilities
from onbot.config import OnbotConfig
from onbot.lifecycle.accounts import (
    LEDGER_RETENTION_SEC,
    AccountLifecycleManager,
    AccountStatus,
    AdminApiLifecycleEffectors,
    DirectorySnapshot,
    DirectoryUser,
    KnownUser,
    KnownUsers,
    LifecycleAction,
    LifecycleEntry,
    LifecycleLedger,
    LifecycleStepIncomplete,
    LifecycleTargetNotFound,
    MasLifecycleEffectors,
    MatrixAccountDataLedgerStore,
    classify_accounts,
    decide_account_action,
    delete_all_user_media,
    known_users_account_data_type,
    lifecycle_account_data_type,
    update_known_users,
)

DAY = 60 * 60 * 24
SERVER = "company.org"

_BASE: dict[str, Any] = {
    "synapse_server": {
        "server_name": SERVER,
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
    "admin_room": {"enabled": False},
}


def _config(**lifecycle: Any) -> OnbotConfig:
    cfg = OnbotConfig.model_validate(_BASE)
    lc = cfg.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
    for key, value in lifecycle.items():
        setattr(lc, key, value)
    return cfg


def mx(local: str) -> str:
    return f"@{local}:{SERVER}"


def user(
    local: str,
    *,
    active: bool = True,
    in_scope: bool = True,
    ignored: bool = False,
    pk: str | None = None,
    mxid: str | None = "",
) -> DirectoryUser:
    return DirectoryUser(
        pk=pk or f"pk-{local}",
        username=local,
        mxid=mx(local) if mxid == "" else mxid,
        is_active=active,
        in_scope=in_scope,
        ignored=ignored,
    )


def snapshot(
    *users: DirectoryUser,
    matrix: set[str] | None = None,
    excluded: set[str] | None = None,
    synced: set[str] | None = None,
) -> DirectorySnapshot:
    """A directory with the given users plus an unrelated admin without a Matrix account."""
    everyone = (*users, user("akadmin", mxid=None))
    return DirectorySnapshot(
        users=everyone,
        matrix_mxids=frozenset(matrix if matrix is not None else {u.mxid for u in users if u.mxid}),
        excluded_mxids=frozenset(excluded or {mx("bot")}),
        synced_mxids=frozenset(synced or set()),
    )


# --- pure state machine ------------------------------------------------------------------------


class TestDecideAccountAction:
    def test_active_user_without_state_is_noop(self) -> None:
        action = decide_account_action(
            status=AccountStatus.active,
            marked_ts=None,
            disabled_ts=None,
            now=100.0,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.none

    @pytest.mark.parametrize(
        ("marked_ts", "disabled_ts"),
        [(50.0, None), (50.0, 80.0), (None, 80.0)],
    )
    def test_active_user_with_any_state_is_reenabled(
        self, marked_ts: float | None, disabled_ts: float | None
    ) -> None:
        action = decide_account_action(
            status=AccountStatus.active,
            marked_ts=marked_ts,
            disabled_ts=disabled_ts,
            now=100.0,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.reenable

    def test_new_orphan_is_marked(self) -> None:
        action = decide_account_action(
            status=AccountStatus.orphaned,
            marked_ts=None,
            disabled_ts=None,
            now=100.0,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.mark

    def test_marked_orphan_in_cooldown_waits(self) -> None:
        action = decide_account_action(
            status=AccountStatus.orphaned,
            marked_ts=100.0,
            disabled_ts=None,
            now=100.0 + DAY - 1,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.wait

    def test_marked_orphan_at_cooldown_boundary_logs_out(self) -> None:
        action = decide_account_action(
            status=AccountStatus.orphaned,
            marked_ts=100.0,
            disabled_ts=None,
            now=100.0 + DAY,  # exactly at the boundary (>=)
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.logout

    def test_disabled_orphan_in_delete_cooldown_waits(self) -> None:
        action = decide_account_action(
            status=AccountStatus.orphaned,
            marked_ts=1.0,
            disabled_ts=100.0,
            now=100.0 + DAY - 1,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.wait

    def test_disabled_orphan_after_delete_cooldown_is_erased(self) -> None:
        action = decide_account_action(
            status=AccountStatus.orphaned,
            marked_ts=1.0,
            disabled_ts=100.0,
            now=100.0 + DAY,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.erase

    def test_disabled_orphan_never_erased_when_delete_disabled(self) -> None:
        action = decide_account_action(
            status=AccountStatus.orphaned,
            marked_ts=1.0,
            disabled_ts=100.0,
            now=100.0 + DAY * 1000,
            lock_after_n_sec=DAY,
            remove_after_n_sec=None,  # erasure disabled
        )
        assert action is LifecycleAction.wait

    @pytest.mark.parametrize("status", [AccountStatus.frozen, AccountStatus.untracked])
    @pytest.mark.parametrize(("marked_ts", "disabled_ts"), [(50.0, None), (50.0, 80.0), (None, 80.0)])
    def test_tracked_user_without_evidence_is_frozen_never_reenabled(
        self, status: AccountStatus, marked_ts: float | None, disabled_ts: float | None
    ) -> None:
        action = decide_account_action(
            status=status,
            marked_ts=marked_ts,
            disabled_ts=disabled_ts,
            now=100.0 + DAY * 1000,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.freeze

    @pytest.mark.parametrize("status", [AccountStatus.frozen, AccountStatus.untracked])
    def test_untracked_user_without_evidence_is_left_alone(self, status: AccountStatus) -> None:
        action = decide_account_action(
            status=status,
            marked_ts=None,
            disabled_ts=None,
            now=100.0,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.none

    def test_locked_entry_without_mark_continues_to_erase(self) -> None:
        action = decide_account_action(
            status=AccountStatus.orphaned,
            marked_ts=None,
            disabled_ts=100.0,
            now=100.0 + DAY,
            lock_after_n_sec=DAY,
            remove_after_n_sec=DAY,
        )
        assert action is LifecycleAction.erase


# --- classification ----------------------------------------------------------------------------


def _ledger(**entries: LifecycleEntry) -> LifecycleLedger:
    return LifecycleLedger(entries={mx(k): v for k, v in entries.items()})


def _known(**bindings: str) -> KnownUsers:
    return KnownUsers(users={pk: KnownUser(mxid=mx(local), first_seen=1.0) for local, pk in bindings.items()})


LOCKED = LifecycleEntry(marked_for_disabling_timestamp=1.0, disabled_user_timestamp=2.0)


class TestClassifyAccounts:
    def test_disabled_in_scope_user_with_matrix_account_is_orphaned(self) -> None:
        result = classify_accounts(snapshot(user("x", active=False)), KnownUsers(), LifecycleLedger())
        assert result[mx("x")].status is AccountStatus.orphaned
        assert result[mx("x")].pk == "pk-x"

    def test_disabled_user_without_matrix_account_is_not_started(self) -> None:
        snap = snapshot(user("x", active=False), matrix=set())
        result = classify_accounts(snap, KnownUsers(), LifecycleLedger())
        assert result[mx("x")].status is AccountStatus.untracked

    def test_active_users_are_not_candidates(self) -> None:
        assert classify_accounts(snapshot(user("x")), KnownUsers(), LifecycleLedger()) == {}

    def test_disabled_user_outside_scope_is_never_a_candidate(self) -> None:
        snap = snapshot(user("svc", active=False, in_scope=False))
        result = classify_accounts(snap, KnownUsers(), LifecycleLedger())
        assert mx("svc") not in result or result[mx("svc")].status is not AccountStatus.orphaned

    def test_tracked_user_deleted_from_authentik_is_orphaned(self) -> None:
        result = classify_accounts(snapshot(), KnownUsers(), _ledger(x=LOCKED))
        assert result[mx("x")].status is AccountStatus.orphaned
        assert result[mx("x")].reason == "deleted from Authentik"

    def test_known_user_deleted_from_authentik_is_orphaned(self) -> None:
        snap = snapshot(matrix={mx("x")})
        result = classify_accounts(snap, _known(x="pk-x"), LifecycleLedger())
        assert result[mx("x")].status is AccountStatus.orphaned

    def test_known_user_deleted_without_matrix_account_is_not_started(self) -> None:
        result = classify_accounts(snapshot(matrix=set()), _known(x="pk-x"), LifecycleLedger())
        assert result[mx("x")].status is AccountStatus.untracked

    def test_tracked_user_continues_even_when_synapse_stops_listing_it(self) -> None:
        snap = snapshot(user("x", active=False), matrix=set())
        result = classify_accounts(snap, KnownUsers(), _ledger(x=LOCKED))
        assert result[mx("x")].status is AccountStatus.orphaned

    def test_tracked_user_active_in_scope_is_active(self) -> None:
        result = classify_accounts(snapshot(user("x")), KnownUsers(), _ledger(x=LOCKED))
        assert result[mx("x")].status is AccountStatus.active

    @pytest.mark.parametrize(
        ("directory_user", "expected_reason"),
        [
            (user("x", in_scope=False), "active in Authentik but outside the sync scope"),
            (user("x", active=False, in_scope=False), "disabled in Authentik but outside the sync scope"),
            (user("x", active=False, ignored=True), "the Authentik user is on authentik_user_ignore_list"),
        ],
    )
    def test_tracked_user_without_positive_evidence_is_frozen(
        self, directory_user: DirectoryUser, expected_reason: str
    ) -> None:
        result = classify_accounts(snapshot(directory_user), KnownUsers(), _ledger(x=LOCKED))
        assert result[mx("x")].status is AccountStatus.frozen
        assert result[mx("x")].reason == expected_reason

    def test_known_user_disabled_outside_scope_is_orphaned(self) -> None:
        """D14: a known binding is enough evidence to offboard a disabled user outside the scope."""
        snap = snapshot(user("x", active=False, in_scope=False))
        result = classify_accounts(snap, _known(x="pk-x"), LifecycleLedger())
        assert result[mx("x")].status is AccountStatus.orphaned
        assert result[mx("x")].pk == "pk-x"

    def test_known_tracked_user_disabled_outside_scope_continues(self) -> None:
        snap = snapshot(user("x", active=False, in_scope=False))
        entry = LOCKED.model_copy(update={"authentik_pk": "pk-x"})
        result = classify_accounts(snap, _known(x="pk-x"), _ledger(x=entry))
        assert result[mx("x")].status is AccountStatus.orphaned

    def test_ledger_binding_alone_does_not_lift_the_scope_freeze(self) -> None:
        """Only a known binding counts for D14; a user never seen active stays frozen (D9)."""
        snap = snapshot(user("x", active=False, in_scope=False))
        entry = LOCKED.model_copy(update={"authentik_pk": "pk-x"})
        result = classify_accounts(snap, KnownUsers(), _ledger(x=entry))
        assert result[mx("x")].status is AccountStatus.frozen

    def test_known_user_active_outside_scope_is_still_frozen(self) -> None:
        snap = snapshot(user("x", in_scope=False))
        result = classify_accounts(snap, _known(x="pk-x"), _ledger(x=LOCKED))
        assert result[mx("x")].status is AccountStatus.frozen

    def test_known_user_disabled_outside_scope_with_an_active_namesake_is_frozen(self) -> None:
        old = user("x-old", active=False, in_scope=False, pk="pk-x", mxid=mx("x"))
        newcomer = user("x", pk="pk-new", in_scope=False)
        result = classify_accounts(snapshot(old, newcomer), _known(x="pk-x"), LifecycleLedger())
        assert result[mx("x")].status is AccountStatus.frozen

    def test_known_user_disabled_outside_scope_on_ignore_list_is_frozen(self) -> None:
        snap = snapshot(user("x", active=False, in_scope=False, ignored=True))
        result = classify_accounts(snap, _known(x="pk-x"), LifecycleLedger())
        assert result[mx("x")].status is AccountStatus.frozen

    def test_tracked_user_on_matrix_ignore_list_is_frozen(self) -> None:
        snap = snapshot(user("x"), excluded={mx("bot"), mx("x")})
        result = classify_accounts(snap, KnownUsers(), _ledger(x=LOCKED))
        assert result[mx("x")].status is AccountStatus.frozen

    def test_binding_beats_the_mapping_attribute(self) -> None:
        """The bound user decides, even when its mapping attribute now computes something else."""
        bound = user("x", active=False, mxid=mx("renamed"))
        entry = LifecycleEntry(
            marked_for_disabling_timestamp=1.0, disabled_user_timestamp=2.0, authentik_pk="pk-x"
        )
        result = classify_accounts(snapshot(bound, matrix={mx("x")}), KnownUsers(), _ledger(x=entry))
        assert result[mx("x")].status is AccountStatus.orphaned
        assert result[mx("x")].reason == "disabled in Authentik"

    def test_another_active_user_mapping_to_a_bound_account_freezes_it(self) -> None:
        """A different person now computes to the MXID of a locked account: no unlock, no erase."""
        newcomer = user("x", pk="pk-new")
        old = user("x-old", active=False, pk="pk-x", mxid=mx("x"))
        entry = LifecycleEntry(disabled_user_timestamp=2.0, authentik_pk="pk-x")
        result = classify_accounts(snapshot(newcomer, old), KnownUsers(), _ledger(x=entry))
        assert result[mx("x")].status is AccountStatus.frozen

    def test_bound_user_deleted_while_a_newcomer_maps_to_the_account_is_frozen(self) -> None:
        newcomer = user("x", pk="pk-new")
        entry = LifecycleEntry(disabled_user_timestamp=2.0, authentik_pk="pk-x")
        result = classify_accounts(snapshot(newcomer), KnownUsers(), _ledger(x=entry))
        assert result[mx("x")].status is AccountStatus.frozen

    def test_unbound_disabled_user_sharing_an_mxid_with_an_active_user_is_not_orphaned(self) -> None:
        """E6 (LIF-05): the guard covers every active Authentik user, in scope or not."""
        old = user("frank.old", active=False, mxid=mx("frank"))
        new = user("frank", in_scope=False)
        result = classify_accounts(snapshot(old, new), KnownUsers(), LifecycleLedger())
        assert result[mx("frank")].status is AccountStatus.frozen

    def test_account_synced_as_active_member_is_never_orphaned(self) -> None:
        snap = snapshot(user("x", active=False), synced={mx("x")})
        result = classify_accounts(snap, KnownUsers(), LifecycleLedger())
        assert result[mx("x")].status is AccountStatus.frozen

    def test_never_mapped_account_is_not_a_candidate(self) -> None:
        snap = snapshot(user("alice"), matrix={mx("alice"), mx("bridge")})
        assert mx("bridge") not in classify_accounts(snap, KnownUsers(), LifecycleLedger())


# --- known users record ------------------------------------------------------------------------


class TestKnownUsers:
    def test_records_active_in_scope_mapped_users_with_a_matrix_account(self) -> None:
        known = KnownUsers()
        snap = snapshot(
            user("a"),
            user("out", in_scope=False),
            user("off", active=False),
            user("ign", ignored=True),
            user("nomx"),
            matrix={mx("a"), mx("out"), mx("off"), mx("ign")},
        )
        changed, warnings = update_known_users(known, snap, LifecycleLedger(), now=5.0)
        assert changed and warnings == []
        assert known.users == {"pk-a": KnownUser(mxid=mx("a"), first_seen=5.0)}

    def test_excluded_accounts_are_never_recorded(self) -> None:
        known = KnownUsers()
        update_known_users(known, snapshot(user("bot")), LifecycleLedger(), now=5.0)
        assert known.users == {}

    def test_binding_is_never_rewritten(self) -> None:
        known = _known(a="pk-a")
        snap = snapshot(user("renamed", pk="pk-a"), matrix={mx("a"), mx("renamed")})
        changed, warnings = update_known_users(known, snap, LifecycleLedger(), now=5.0)
        assert not changed
        assert known.users["pk-a"].mxid == mx("a")
        assert len(warnings) == 1 and "bound to" in warnings[0]

    def test_duplicate_mxid_is_reported_and_not_recorded(self) -> None:
        known = _known(a="pk-a")
        snap = snapshot(user("a"), user("a", pk="pk-twin"))
        changed, warnings = update_known_users(known, snap, LifecycleLedger(), now=5.0)
        assert not changed
        assert set(known.users) == {"pk-a"}
        assert len(warnings) == 1 and "both map to" in warnings[0]

    def test_ledger_entry_binds_the_account_against_newcomers(self) -> None:
        entry = LifecycleEntry(disabled_user_timestamp=2.0, authentik_pk="pk-x")
        known = KnownUsers()
        changed, warnings = update_known_users(
            known, snapshot(user("x", pk="pk-new")), _ledger(x=entry), now=5.0
        )
        assert not changed and known.users == {}
        assert len(warnings) == 1 and "both map to" in warnings[0]

    def test_ledger_entry_owner_itself_is_recorded(self) -> None:
        entry = LifecycleEntry(marked_for_disabling_timestamp=2.0, authentik_pk="pk-x")
        known = KnownUsers()
        changed, warnings = update_known_users(known, snapshot(user("x")), _ledger(x=entry), now=5.0)
        assert changed and warnings == []
        assert known.users["pk-x"].mxid == mx("x")

    def test_pruned_when_gone_from_authentik_and_nothing_left_to_offboard(self) -> None:
        known = _known(gone="pk-gone", tracked="pk-tracked", listed="pk-listed")
        snap = snapshot(matrix={mx("listed")})
        changed, _ = update_known_users(known, snap, _ledger(tracked=LOCKED), now=5.0)
        assert changed
        assert set(known.users) == {"pk-tracked", "pk-listed"}


# --- fakes -------------------------------------------------------------------------------------


class InMemoryLedgerStore:
    def __init__(self, ledger: LifecycleLedger | None = None, known: KnownUsers | None = None) -> None:
        self.ledger = ledger or LifecycleLedger()
        self.known = known or KnownUsers()
        self.saves = 0

    async def load(self) -> LifecycleLedger:
        # Return a copy so the manager mutating it does not retroactively change what was "stored".
        return self.ledger.model_copy(deep=True)

    async def save(self, ledger: LifecycleLedger) -> None:
        self.ledger = ledger.model_copy(deep=True)
        self.saves += 1

    async def load_known_users(self) -> KnownUsers:
        return self.known.model_copy(deep=True)

    async def save_known_users(self, known: KnownUsers) -> None:
        self.known = known.model_copy(deep=True)


class RecordingEffectors:
    def __init__(self) -> None:
        self.logouts: list[str] = []
        self.reenables: list[str] = []
        self.erases: list[tuple[str, bool]] = []
        self.media: list[str] = []

    async def logout(self, mxid: str) -> None:
        self.logouts.append(mxid)

    async def reenable(self, mxid: str) -> None:
        self.reenables.append(mxid)

    async def erase(self, mxid: str, *, gdpr_erase: bool) -> None:
        self.erases.append((mxid, gdpr_erase))

    async def delete_media(self, mxid: str) -> None:
        self.media.append(mxid)

    @property
    def calls(self) -> int:
        return len(self.logouts) + len(self.reenables) + len(self.erases) + len(self.media)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _manager(
    store: InMemoryLedgerStore,
    effectors: RecordingEffectors,
    clock: Clock,
    **lifecycle: Any,
) -> AccountLifecycleManager:
    return AccountLifecycleManager(_config(**lifecycle), store, effectors, clock=clock)


X = mx("x")
ORPHAN_X = snapshot(user("x", active=False))
ACTIVE_X = snapshot(user("x"))
NOBODY = snapshot()


# --- manager: dry-run (default) ----------------------------------------------------------------


class TestManagerDryRun:
    async def test_disabled_config_is_total_noop(self) -> None:
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock, enabled=False)
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert outcomes == []
        assert store.saves == 0
        assert eff.calls == 0

    async def test_marks_new_orphan_even_in_dry_run(self) -> None:
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock)  # dry_run defaults to True
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert [o.action for o in outcomes] == [LifecycleAction.mark]
        assert outcomes[0].dry_run is True
        # Bookkeeping persisted; the clock started; the Authentik user is recorded.
        assert store.ledger.entries[X].marked_for_disabling_timestamp == clock.now
        assert store.ledger.entries[X].authentik_pk == "pk-x"
        assert eff.calls == 0

    async def test_dry_run_logout_does_not_act_or_advance_state(self) -> None:
        marked = 1000.0
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={X: LifecycleEntry(marked_for_disabling_timestamp=marked, authentik_pk="pk-x")}
            )
        )
        eff, clock = RecordingEffectors(), Clock(marked + DAY)
        mgr = _manager(store, eff, clock)
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert [o.action for o in outcomes] == [LifecycleAction.logout]
        assert eff.calls == 0  # no destructive call
        # disabled_ts NOT set, so a later live run still performs the logout.
        assert store.ledger.entries[X].disabled_user_timestamp is None
        assert store.saves == 0

    async def test_dry_run_erase_does_not_act(self) -> None:
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={
                    X: LifecycleEntry(marked_for_disabling_timestamp=1.0, disabled_user_timestamp=1000.0)
                }
            )
        )
        eff, clock = RecordingEffectors(), Clock(1000.0 + DAY)
        mgr = _manager(store, eff, clock, remove_after_n_sec=DAY)
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert [o.action for o in outcomes] == [LifecycleAction.erase]
        assert eff.calls == 0
        assert X in store.ledger.entries  # not removed in dry-run

    async def test_dry_run_reenable_of_a_real_lock_calls_no_effector(self) -> None:
        store = InMemoryLedgerStore(LifecycleLedger(entries={X: LOCKED.model_copy()}))
        eff, clock = RecordingEffectors(), Clock()
        outcomes = await _manager(store, eff, clock).reconcile_accounts(ACTIVE_X)
        assert [o.action for o in outcomes] == [LifecycleAction.reenable]
        assert eff.calls == 0
        assert X in store.ledger.entries  # kept, so live mode can still unlock it

    async def test_dry_run_clears_a_mere_mark_without_effector(self) -> None:
        store = InMemoryLedgerStore(
            LifecycleLedger(entries={X: LifecycleEntry(marked_for_disabling_timestamp=1.0)})
        )
        eff, clock = RecordingEffectors(), Clock()
        await _manager(store, eff, clock).reconcile_accounts(ACTIVE_X)
        assert eff.calls == 0
        assert store.ledger.entries == {}


# --- manager: live -----------------------------------------------------------------------------


class TestManagerLive:
    async def test_live_logout_locks_and_records(self) -> None:
        marked = 1000.0
        store = InMemoryLedgerStore(
            LifecycleLedger(entries={X: LifecycleEntry(marked_for_disabling_timestamp=marked)})
        )
        eff, clock = RecordingEffectors(), Clock(marked + DAY)
        mgr = _manager(store, eff, clock, dry_run=False)
        await mgr.reconcile_accounts(ORPHAN_X)
        assert eff.logouts == [X]
        assert store.ledger.entries[X].disabled_user_timestamp == clock.now

    @pytest.mark.parametrize(("media", "gdpr"), [(True, False), (False, True), (False, False), (True, True)])
    async def test_live_erase_deactivates_then_deletes_media_and_clears_entry(
        self, media: bool, gdpr: bool
    ) -> None:
        """D7: GDPR erasure follows ``gdpr_erase_on_delete``, independent of the media switch."""
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={
                    X: LifecycleEntry(marked_for_disabling_timestamp=1.0, disabled_user_timestamp=1000.0)
                }
            ),
            known=_known(x="pk-x"),
        )
        eff, clock = RecordingEffectors(), Clock(1000.0 + DAY)
        mgr = _manager(
            store,
            eff,
            clock,
            dry_run=False,
            remove_after_n_sec=DAY,
            include_user_media_on_delete=media,
            gdpr_erase_on_delete=gdpr,
        )
        await mgr.reconcile_accounts(ORPHAN_X)
        assert eff.erases == [(X, gdpr)]
        assert eff.media == ([X] if media else [])
        assert X not in store.ledger.entries  # terminal
        assert store.known.users == {}  # the binding goes with it

    async def test_reenable_of_a_real_lock_unlocks_and_clears(self) -> None:
        store = InMemoryLedgerStore(LifecycleLedger(entries={X: LOCKED.model_copy()}))
        eff, clock = RecordingEffectors(), Clock()
        outcomes = await _manager(store, eff, clock, dry_run=False).reconcile_accounts(ACTIVE_X)
        assert [o.action for o in outcomes] == [LifecycleAction.reenable]
        assert eff.reenables == [X]
        assert store.ledger.entries == {}

    async def test_reenable_of_a_mere_mark_calls_no_effector(self) -> None:
        """LIF-06: onbot never locked this account, so it must not unlock it either."""
        store = InMemoryLedgerStore(
            LifecycleLedger(entries={X: LifecycleEntry(marked_for_disabling_timestamp=1.0)})
        )
        eff, clock = RecordingEffectors(), Clock()
        outcomes = await _manager(store, eff, clock, dry_run=False).reconcile_accounts(ACTIVE_X)
        assert [o.action for o in outcomes] == [LifecycleAction.reenable]
        assert eff.calls == 0
        assert store.ledger.entries == {}

    async def test_reenable_after_deactivation_only_clears_state(self) -> None:
        entry = LOCKED.model_copy(update={"deactivated_timestamp": 3.0})
        store = InMemoryLedgerStore(LifecycleLedger(entries={X: entry}))
        eff, clock = RecordingEffectors(), Clock()
        await _manager(store, eff, clock, dry_run=False).reconcile_accounts(ACTIVE_X)
        assert eff.calls == 0
        assert store.ledger.entries == {}

    async def test_tracked_user_absent_from_authentik_is_never_reenabled(self) -> None:
        """LIF-01: absence is not evidence; the user is treated as deleted and stays locked."""
        store = InMemoryLedgerStore(LifecycleLedger(entries={X: LOCKED.model_copy()}))
        eff, clock = RecordingEffectors(), Clock()
        outcomes = await _manager(store, eff, clock, dry_run=False).reconcile_accounts(NOBODY)
        assert outcomes == []
        assert eff.calls == 0
        assert X in store.ledger.entries

    async def test_full_progression_across_ticks(self) -> None:
        """mark, wait, logout, wait, erase, driven by a moving clock (live mode)."""
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock(0.0)
        mgr = _manager(store, eff, clock, dry_run=False, lock_after_n_sec=DAY, remove_after_n_sec=DAY)

        # t=0: first sight: mark
        assert [o.action for o in await mgr.reconcile_accounts(ORPHAN_X)] == [LifecycleAction.mark]

        # t=1h: still in deactivate cooldown: wait (no outcome recorded)
        clock.now = 60 * 60
        assert await mgr.reconcile_accounts(ORPHAN_X) == []
        assert eff.logouts == []

        # t=1 day: deactivate cooldown elapsed: logout
        clock.now = DAY
        assert [o.action for o in await mgr.reconcile_accounts(ORPHAN_X)] == [LifecycleAction.logout]
        assert eff.logouts == [X]

        # t=1 day + 1h: delete cooldown not yet elapsed: wait
        clock.now = DAY + 60 * 60
        assert await mgr.reconcile_accounts(ORPHAN_X) == []
        assert eff.erases == []

        # t=2 days: delete cooldown elapsed: erase
        clock.now = DAY + DAY
        assert [o.action for o in await mgr.reconcile_accounts(ORPHAN_X)] == [LifecycleAction.erase]
        assert eff.erases == [(X, False)]
        assert store.ledger.entries == {}

    async def test_no_save_when_nothing_changes(self) -> None:
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock, dry_run=False)
        assert await mgr.reconcile_accounts(NOBODY) == []
        assert store.saves == 0

    async def test_empty_directory_is_refused(self) -> None:
        store = InMemoryLedgerStore(LifecycleLedger(entries={X: LOCKED.model_copy()}))
        eff, clock = RecordingEffectors(), Clock()
        empty = DirectorySnapshot(users=(), matrix_mxids=frozenset({X}), excluded_mxids=frozenset())
        assert await _manager(store, eff, clock, dry_run=False).reconcile_accounts(empty) == []
        assert eff.calls == 0 and store.saves == 0

    async def test_freeze_is_reported_once_and_cleared_when_evidence_returns(self) -> None:
        store = InMemoryLedgerStore(LifecycleLedger(entries={X: LOCKED.model_copy()}))
        eff, clock = RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock, dry_run=False)
        out_of_scope = snapshot(user("x", active=False, in_scope=False))
        first = await mgr.reconcile_accounts(out_of_scope)
        assert [(o.action, o.needs_attention) for o in first] == [(LifecycleAction.freeze, True)]
        assert first[0].detail == "disabled in Authentik but outside the sync scope"
        assert await mgr.reconcile_accounts(out_of_scope) == []
        assert store.ledger.entries[X].frozen_reason is not None

        await mgr.reconcile_accounts(ORPHAN_X)  # back in scope, still disabled
        assert store.ledger.entries[X].frozen_reason is None
        assert eff.calls == 0

    async def test_failure_backs_off_and_is_reset_on_success(self) -> None:
        class Flaky(RecordingEffectors):
            fail = True

            async def logout(self, mxid: str) -> None:
                if self.fail:
                    raise RuntimeError("boom")
                await super().logout(mxid)

        store = InMemoryLedgerStore(
            LifecycleLedger(entries={X: LifecycleEntry(marked_for_disabling_timestamp=1.0)})
        )
        eff, clock = Flaky(), Clock(DAY * 2)
        mgr = _manager(store, eff, clock, dry_run=False)
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert [(o.action, o.failed) for o in outcomes] == [(LifecycleAction.logout, True)]
        entry = store.ledger.entries[X]
        assert entry.failures == 1 and entry.disabled_user_timestamp is None
        assert entry.last_error == "RuntimeError: boom"
        assert entry.retry_after_timestamp == clock.now + 60

        clock.now += 30
        assert await mgr.reconcile_accounts(ORPHAN_X) == []  # backing off
        clock.now += 30
        await mgr.reconcile_accounts(ORPHAN_X)  # second failure doubles the wait
        assert store.ledger.entries[X].retry_after_timestamp == clock.now + 120

        eff.fail = False
        clock.now += 120
        await mgr.reconcile_accounts(ORPHAN_X)
        entry = store.ledger.entries[X]
        assert eff.logouts == [X]
        assert entry.failures == 0 and entry.retry_after_timestamp is None and entry.last_error is None

    async def test_legacy_entry_is_bound_to_its_authentik_user(self) -> None:
        """An entry written before pks were recorded gets one, so a mapping change cannot detach it."""
        store = InMemoryLedgerStore(LifecycleLedger(entries={X: LOCKED.model_copy()}))
        eff, clock = RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock, dry_run=False)
        await mgr.reconcile_accounts(ORPHAN_X)
        assert store.ledger.entries[X].authentik_pk == "pk-x"

        renamed_and_back = snapshot(user("x", active=False, pk="pk-x", mxid=mx("other")), user("x2", mxid=X))
        await mgr.reconcile_accounts(renamed_and_back)
        assert eff.reenables == []  # judged by pk-x (still disabled), not by the newcomer
        assert (
            store.ledger.entries[X].frozen_reason
            == "another active Authentik user maps to this Matrix account"
        )

    async def test_known_users_are_recorded(self) -> None:
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock()
        await _manager(store, eff, clock).reconcile_accounts(ACTIVE_X)
        assert store.known.users["pk-x"].mxid == X


# --- ledger store ------------------------------------------------------------------------------


class FakeMatrixClient:
    def __init__(self) -> None:
        self.account_data: dict[tuple[str, str], dict[str, Any]] = {}

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        return self.account_data.get((user_id, data_type), {})

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        self.account_data[(user_id, data_type)] = dict(content)


class TestLedgerStore:
    def test_account_data_types_are_namespaced(self) -> None:
        assert lifecycle_account_data_type("company.org") == "org.company.onbot.lifecycle"
        assert known_users_account_data_type("company.org") == "org.company.onbot.known_users"

    async def test_load_missing_returns_empty_records(self) -> None:
        store = MatrixAccountDataLedgerStore(FakeMatrixClient(), "@bot:company.org", "company.org")
        assert (await store.load()).entries == {}
        assert (await store.load_known_users()).users == {}

    async def test_round_trip(self) -> None:
        client = FakeMatrixClient()
        store = MatrixAccountDataLedgerStore(client, "@bot:company.org", "company.org")
        ledger = LifecycleLedger(
            entries={X: LifecycleEntry(marked_for_disabling_timestamp=5.0, authentik_pk="7")}
        )
        await store.save(ledger)
        reloaded = await store.load()
        assert reloaded.entries[X].marked_for_disabling_timestamp == 5.0
        assert reloaded.entries[X].authentik_pk == "7"
        # Unset fields are not written, which keeps the blob small.
        raw = client.account_data[("@bot:company.org", "org.company.onbot.lifecycle")]
        assert raw == {
            "schema_version": 1,
            "entries": {X: {"marked_for_disabling_timestamp": 5.0, "authentik_pk": "7"}},
        }

    async def test_old_ledger_blob_still_loads(self) -> None:
        client = FakeMatrixClient()
        client.account_data[("@bot:company.org", "org.company.onbot.lifecycle")] = {
            "schema_version": 1,
            "entries": {X: {"marked_for_disabling_timestamp": 5.0, "disabled_user_timestamp": 6.0}},
        }
        store = MatrixAccountDataLedgerStore(client, "@bot:company.org", "company.org")
        entry = (await store.load()).entries[X]
        assert entry.disabled_user_timestamp == 6.0 and entry.authentik_pk is None

    async def test_known_users_round_trip(self) -> None:
        client = FakeMatrixClient()
        store = MatrixAccountDataLedgerStore(client, "@bot:company.org", "company.org")
        await store.save_known_users(_known(x="12"))
        assert (await store.load_known_users()).users["12"].mxid == X


# --- admin-api effectors -----------------------------------------------------------------------


class FakeAdmin:
    """Synapse admin API: devices, deactivation and a user's media, deleted one page per request."""

    def __init__(self, media: int = 0, undeletable: int = 0) -> None:
        self.logged_out: list[str] = []
        self.deactivated: list[tuple[str, bool]] = []
        self.media_requests: list[tuple[str, int]] = []
        self.media = media  # deletable files
        self.undeletable = undeletable  # files Synapse keeps whatever happens

    async def logout_account(self, user_id: str) -> int:
        self.logged_out.append(user_id)
        return 2

    async def deactivate_account(self, user_id: str, *, erase: bool) -> None:
        self.deactivated.append((user_id, erase))

    async def delete_user_media(self, user_id: str, *, limit: int = 100) -> dict[str, Any]:
        self.media_requests.append((user_id, limit))
        gone = min(limit, self.media)
        self.media -= gone
        return {"deleted_media": ["m"] * gone, "total": gone}

    async def count_user_media(self, user_id: str) -> int:
        return self.media + self.undeletable

    @property
    def media_deleted(self) -> list[str]:
        return sorted({user_id for user_id, _ in self.media_requests})


class FakeMatrix:
    """The homeserver's answer to "do you delegate logins?": an issuer, ``None``, or an error."""

    def __init__(self, issuer: str | None, *, fail: int = 0) -> None:
        self.issuer = issuer
        self.fail = fail  # how many of the next questions raise
        self.asked = 0

    async def get_auth_issuer(self) -> str | None:
        self.asked += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError("homeserver unreachable")
        return self.issuer


MAS_ISSUER = "https://auth.company.org/"


def _synapse_effectors(
    admin: FakeAdmin, issuer: str | None = None, **kwargs: Any
) -> AdminApiLifecycleEffectors:
    return AdminApiLifecycleEffectors(admin, matrix=FakeMatrix(issuer, **kwargs))


class TestAdminApiEffectors:
    async def test_logout_deletes_devices_and_says_so(self) -> None:
        admin = FakeAdmin()
        detail = await _synapse_effectors(admin).logout(X)
        assert admin.logged_out == [X]
        assert detail == "deleted 2 Synapse device(s)"

    async def test_reenable_is_a_noop(self) -> None:
        admin = FakeAdmin()
        await _synapse_effectors(admin).reenable(X)
        assert admin.logged_out == [] and admin.deactivated == []

    @pytest.mark.parametrize("gdpr", [True, False])
    async def test_erase_passes_the_erase_flag(self, gdpr: bool) -> None:
        admin = FakeAdmin()
        await _synapse_effectors(admin).erase(X, gdpr_erase=gdpr)
        assert admin.deactivated == [(X, gdpr)]
        assert admin.media_requests == []

    async def test_delete_media_delegates(self) -> None:
        admin = FakeAdmin(media=3)
        result = await _synapse_effectors(admin).delete_media(X)
        assert admin.media_deleted == [X]
        assert result.complete and result.deleted == 3

    async def test_preflight_without_mas_is_quiet(self) -> None:
        effectors = _synapse_effectors(FakeAdmin())
        assert await effectors.preflight() == []
        assert await effectors.destructive_refusal() is None
        assert "MAS" not in await effectors.logout(X)

    async def test_preflight_detects_a_mas_backed_homeserver(self) -> None:
        """LIF-09: the fallback under MAS is reported as a refusal to lock or erase."""
        effectors = _synapse_effectors(FakeAdmin(), MAS_ISSUER)
        warnings = await effectors.preflight()
        assert len(warnings) == 1
        assert "mas_admin" in warnings[0] and "refuses to lock or erase" in warnings[0]
        assert warnings == [await effectors.destructive_refusal()]

    @pytest.mark.parametrize("step", ["logout", "erase", "delete_media"])
    async def test_under_mas_no_destructive_step_reaches_synapse(self, step: str) -> None:
        """LIF-09 (D19): fail closed even when called directly, without the manager's check."""
        admin = FakeAdmin(media=3)
        effectors = _synapse_effectors(admin, MAS_ISSUER)
        kwargs: dict[str, Any] = {"gdpr_erase": False} if step == "erase" else {}
        with pytest.raises(LifecycleStepIncomplete, match="mas_admin"):
            await getattr(effectors, step)(X, **kwargs)
        assert admin.logged_out == [] and admin.deactivated == [] and admin.media_requests == []

    async def test_an_unanswered_question_blocks_and_is_asked_again(self) -> None:
        """Fail closed: no answer from the homeserver is not a "no, not MAS"."""
        admin = FakeAdmin()
        effectors = _synapse_effectors(admin, None, fail=1)
        with pytest.raises(ConnectionError):
            await effectors.logout(X)
        assert admin.logged_out == []
        await effectors.logout(X)  # the homeserver answered this time: it handles logins itself
        assert admin.logged_out == [X]

    async def test_the_answer_is_asked_once_and_kept(self) -> None:
        effectors = _synapse_effectors(FakeAdmin())
        await effectors.preflight()
        await effectors.logout(X)
        await effectors.erase(X, gdpr_erase=False)
        assert effectors.matrix.asked == 1


# --- media deletion in pages (LIF-12) ----------------------------------------------------------


class TestMediaDeletion:
    async def test_more_than_one_page_is_deleted_completely(self) -> None:
        admin = FakeAdmin(media=250)
        result = await delete_all_user_media(admin, X)
        assert (result.deleted, result.remaining) == (250, 0)
        assert len(admin.media_requests) == 3

    async def test_a_huge_upload_history_resumes_on_the_next_pass(self) -> None:
        admin = FakeAdmin(media=100 * 60)
        first = await delete_all_user_media(admin, X)
        assert not first.complete and first.remaining == 100 * 10
        second = await delete_all_user_media(admin, X)
        assert second.complete

    async def test_media_synapse_will_not_delete_is_a_failure(self) -> None:
        admin = FakeAdmin(media=0, undeletable=4)
        with pytest.raises(LifecycleStepIncomplete, match="4 media"):
            await delete_all_user_media(admin, X)

    async def test_manager_keeps_the_entry_until_all_media_is_gone(self) -> None:
        admin = FakeAdmin(media=100 * 60)
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={X: LifecycleEntry(marked_for_disabling_timestamp=1.0, disabled_user_timestamp=1.0)}
            )
        )
        mgr = AccountLifecycleManager(
            _config(dry_run=False, remove_after_n_sec=0, include_user_media_on_delete=True),
            store,
            _synapse_effectors(admin),
            clock=Clock(DAY),
        )
        await mgr.reconcile_accounts(ORPHAN_X)
        entry = store.ledger.entries[X]
        assert entry.deactivated_timestamp is not None and entry.media_deleted_timestamp is None
        await mgr.reconcile_accounts(ORPHAN_X)  # the next pass finishes the job
        assert admin.media == 0
        assert X not in store.ledger.entries
        assert len(admin.deactivated) == 1  # the irreversible half ran once

    async def test_the_continuation_is_planned_as_media_cleanup_not_as_a_new_erasure(self) -> None:
        """S5: the guard asks for erasures (layer 3), but never again for the media of one that ran."""
        admin = FakeAdmin(media=100 * 60)
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={X: LifecycleEntry(marked_for_disabling_timestamp=1.0, disabled_user_timestamp=1.0)}
            )
        )
        mgr = AccountLifecycleManager(
            _config(dry_run=False, remove_after_n_sec=0, include_user_media_on_delete=True),
            store,
            _synapse_effectors(admin),
            clock=Clock(DAY),
        )
        [planned] = await mgr.plan_accounts(ORPHAN_X)
        assert planned.action is LifecycleAction.erase
        [outcome] = await mgr.reconcile_accounts(ORPHAN_X)
        assert outcome.action is LifecycleAction.erase

        [planned] = await mgr.plan_accounts(ORPHAN_X)
        assert planned.action is LifecycleAction.delete_media
        asked: list[LifecycleAction] = []

        def permit(mxid: str, action: LifecycleAction) -> bool:
            asked.append(action)
            return action is LifecycleAction.delete_media

        [outcome] = await mgr.reconcile_accounts(ORPHAN_X, permit=permit)
        assert outcome.action is LifecycleAction.delete_media
        assert asked == [LifecycleAction.delete_media]
        assert admin.media == 0 and X not in store.ledger.entries
        assert len(admin.deactivated) == 1


# --- MAS effectors (LIF-16, MNT-05, LIF-07, D7) ------------------------------------------------


ALL_FINISHABLE = MasCapabilities(finishable_sessions=frozenset(SESSION_KINDS), deactivate_skip_erase=True)
MAS_0_14 = MasCapabilities()  # no finish endpoints, deactivation always erases


class FakeMasAdmin:
    def __init__(
        self,
        known: dict[str, str] | None = None,
        *,
        caps: MasCapabilities = ALL_FINISHABLE,
        sessions: dict[str, list[str]] | None = None,
    ) -> None:
        self.known = known if known is not None else {"x": "01ULIDX"}
        self.caps = caps
        self.sessions = (
            sessions if sessions is not None else {"compat-sessions": ["c1"], "oauth2-sessions": ["o1", "o2"]}
        )
        self.lookups: list[str] = []
        self.calls: list[tuple[str, ...]] = []
        self.fail_finish = False

    async def capabilities(self) -> MasCapabilities:
        return self.caps

    async def get_user_id_by_username(self, username: str) -> str | None:
        self.lookups.append(username)
        return self.known.get(username)

    async def lock_user(self, user_id: str) -> None:
        self.calls.append(("lock", user_id))

    async def unlock_user(self, user_id: str) -> None:
        self.calls.append(("unlock", user_id))

    async def list_active_session_ids(self, kind: str, user_id: str) -> list[str]:
        return list(self.sessions.get(kind, []))

    async def finish_session(self, kind: str, session_id: str) -> bool:
        if self.fail_finish:
            raise RuntimeError("MAS 500")
        self.calls.append(("finish", kind, session_id))
        self.sessions[kind].remove(session_id)
        return True

    async def deactivate_user(self, user_id: str, *, skip_erase: bool | None = None) -> None:
        self.calls.append(("deactivate", user_id, str(skip_erase)))

    async def count_locked_users(self) -> int:
        return 3


def _ops(mas: FakeMasAdmin) -> list[str]:
    return [call[0] for call in mas.calls]


class TestMasEffectors:
    async def test_lock_resolves_the_localpart_then_ends_every_session(self) -> None:
        """LIF-07: after the lock nothing is left that an unlock could bring back."""
        mas = FakeMasAdmin()
        detail = await MasLifecycleEffectors(mas).logout(X)
        assert mas.lookups == ["x"]
        assert _ops(mas) == ["lock", "finish", "finish", "finish"]
        assert mas.sessions == {"compat-sessions": [], "oauth2-sessions": []}
        assert "1 compat, 2 oauth2, 0 user" in detail

    async def test_unlock_ends_sessions_left_from_an_old_lock_first(self) -> None:
        mas = FakeMasAdmin()
        await MasLifecycleEffectors(mas).reenable(X)
        assert _ops(mas) == ["finish", "finish", "finish", "unlock"]

    async def test_unlock_is_not_done_when_sessions_cannot_be_ended(self) -> None:
        mas = FakeMasAdmin()
        mas.fail_finish = True
        with pytest.raises(RuntimeError):
            await MasLifecycleEffectors(mas).reenable(X)
        assert "unlock" not in _ops(mas)

    async def test_lock_stands_when_ending_sessions_fails(self) -> None:
        mas = FakeMasAdmin()
        mas.fail_finish = True
        detail = await MasLifecycleEffectors(mas).logout(X)
        assert _ops(mas) == ["lock"]
        assert "retried on unlock" in detail

    async def test_a_mas_without_finish_endpoints_only_locks_and_says_so(self) -> None:
        mas = FakeMasAdmin(caps=MAS_0_14)
        detail = await MasLifecycleEffectors(mas).logout(X)
        assert _ops(mas) == ["lock"]
        assert "only suspended" in detail
        detail = await MasLifecycleEffectors(mas).reenable(X)
        assert _ops(mas) == ["lock", "unlock"]
        assert "come back on unlock" in detail

    @pytest.mark.parametrize("gdpr", [True, False])
    async def test_erase_sends_skip_erase_as_configured(self, gdpr: bool) -> None:
        """D7: MAS gets ``skip_erase = not gdpr_erase_on_delete``."""
        mas, admin = FakeMasAdmin(), FakeAdmin()
        await MasLifecycleEffectors(mas, synapse_admin=admin).erase(X, gdpr_erase=gdpr)
        assert mas.calls == [("deactivate", "01ULIDX", str(not gdpr))]
        assert admin.media_requests == []

    async def test_an_old_mas_erases_when_gdpr_erasure_is_wanted(self) -> None:
        mas = FakeMasAdmin(caps=MAS_0_14)
        await MasLifecycleEffectors(mas).erase(X, gdpr_erase=True)
        assert mas.calls == [("deactivate", "01ULIDX", "None")]

    @pytest.mark.parametrize("caps", [MAS_0_14, MasCapabilities(known=False)])
    async def test_an_old_mas_refuses_an_erase_without_gdpr_erasure(self, caps: MasCapabilities) -> None:
        """D7 on a MAS that always erases: refuse rather than erase more than configured."""
        mas = FakeMasAdmin(caps=caps)
        with pytest.raises(LifecycleStepIncomplete, match="gdpr_erase_on_delete"):
            await MasLifecycleEffectors(mas).erase(X, gdpr_erase=False)
        assert mas.calls == []

    async def test_delete_media_goes_through_synapse(self) -> None:
        mas, admin = FakeMasAdmin(), FakeAdmin(media=1)
        await MasLifecycleEffectors(mas, synapse_admin=admin).delete_media(X)
        assert admin.media_deleted == [X]
        assert mas.calls == []

    async def test_delete_media_without_synapse_admin_raises(self) -> None:
        with pytest.raises(LifecycleTargetNotFound):
            await MasLifecycleEffectors(FakeMasAdmin()).delete_media(X)

    @pytest.mark.parametrize("method", ["logout", "reenable", "erase"])
    async def test_unknown_mas_user_raises_and_calls_nothing(self, method: str) -> None:
        """LIF-10: a lookup miss is a failure, never a silent success."""
        mas = FakeMasAdmin(known={})
        effectors = MasLifecycleEffectors(mas, synapse_admin=FakeAdmin())
        kwargs: dict[str, Any] = {"gdpr_erase": False} if method == "erase" else {}
        with pytest.raises(LifecycleTargetNotFound, match="'x'"):
            await getattr(effectors, method)(X, **kwargs)
        assert mas.calls == []

    async def test_preflight_reports_what_an_old_mas_cannot_do(self) -> None:
        assert await MasLifecycleEffectors(FakeMasAdmin()).preflight() == []
        warnings = await MasLifecycleEffectors(FakeMasAdmin(caps=MAS_0_14)).preflight()
        assert len(warnings) == 2
        assert "cannot finish sessions" in warnings[0] and "GDPR-erases" in warnings[1]

    async def test_manager_does_not_advance_on_unknown_mas_user(self) -> None:
        store = InMemoryLedgerStore(
            LifecycleLedger(entries={X: LifecycleEntry(marked_for_disabling_timestamp=1.0)})
        )
        mgr = AccountLifecycleManager(
            _config(dry_run=False), store, MasLifecycleEffectors(FakeMasAdmin(known={})), clock=Clock(DAY * 2)
        )
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert [(o.action, o.failed) for o in outcomes] == [(LifecycleAction.logout, True)]
        assert store.ledger.entries[X].disabled_user_timestamp is None
        assert "LifecycleTargetNotFound" in (store.ledger.entries[X].last_error or "")

    async def test_manager_does_not_advance_when_an_old_mas_refuses_the_erase(self) -> None:
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={X: LifecycleEntry(marked_for_disabling_timestamp=1.0, disabled_user_timestamp=1.0)}
            )
        )
        mas = FakeMasAdmin(caps=MAS_0_14)
        mgr = AccountLifecycleManager(
            _config(dry_run=False, remove_after_n_sec=0), store, MasLifecycleEffectors(mas), clock=Clock(DAY)
        )
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert [(o.action, o.failed) for o in outcomes] == [(LifecycleAction.erase, True)]
        assert store.ledger.entries[X].deactivated_timestamp is None
        assert mas.calls == []


# --- startup check (LIF-09) --------------------------------------------------------------------


class TestPreflight:
    async def test_logs_the_setup_and_returns_the_effector_warnings(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        effectors = AdminApiLifecycleEffectors(FakeAdmin(), matrix=FakeMatrix("https://auth/"))
        mgr = AccountLifecycleManager(_config(dry_run=False), InMemoryLedgerStore(), effectors)
        with caplog.at_level(logging.INFO):
            warnings = await mgr.preflight()
        assert len(warnings) == 1
        text = caplog.text
        assert "effectors=AdminApiLifecycleEffectors" in text and "dry_run=False" in text
        assert any(r.levelno == logging.ERROR and "mas_admin" in r.getMessage() for r in caplog.records)

    async def test_disabled_offboarding_probes_nothing(self) -> None:
        effectors = AdminApiLifecycleEffectors(FakeAdmin(), matrix=FakeMatrix("https://auth/"))
        mgr = AccountLifecycleManager(_config(enabled=False), InMemoryLedgerStore(), effectors)
        assert await mgr.preflight() == []
        assert effectors.delegated_issuer is None


# --- refusal under MAS without mas_admin (LIF-09, D19) -----------------------------------------


def _refusing_manager(
    store: InMemoryLedgerStore, admin: FakeAdmin, matrix: FakeMatrix, **lifecycle: Any
) -> AccountLifecycleManager:
    settings: dict[str, Any] = {"dry_run": False, "remove_after_n_sec": 0} | lifecycle
    return AccountLifecycleManager(
        _config(**settings),
        store,
        AdminApiLifecycleEffectors(admin, matrix=matrix),
        clock=Clock(DAY * 2),
    )


def _marked_x(**fields: float) -> InMemoryLedgerStore:
    return InMemoryLedgerStore(
        LifecycleLedger(entries={X: LifecycleEntry(marked_for_disabling_timestamp=1.0, **fields)})
    )


class TestRefusalUnderMas:
    async def test_lock_is_refused_reported_once_and_nothing_advances(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        admin, store = FakeAdmin(), _marked_x()
        mgr = _refusing_manager(store, admin, FakeMatrix(MAS_ISSUER))
        with caplog.at_level(logging.INFO):
            first = await mgr.reconcile_accounts(ORPHAN_X)
            second = await mgr.reconcile_accounts(ORPHAN_X)

        assert admin.logged_out == [] and admin.deactivated == []
        [report] = first
        assert (report.mxid, report.action, report.failed) == (None, LifecycleAction.abort, False)
        assert report.needs_attention and "mas_admin" in (report.detail or "")
        assert second == []  # reported once, not every pass
        entry = store.ledger.entries[X]
        assert entry.disabled_user_timestamp is None  # not locked
        assert entry.failures == 0 and entry.retry_after_timestamp is None  # held, not backed off
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and "refuses to lock or erase" in errors[0].getMessage()
        refused = [r.getMessage() for r in caplog.records if r.getMessage().startswith("REFUSED")]
        assert len(refused) == 1 and refused[0].startswith(f"REFUSED logout of {X}")

    async def test_erase_is_refused_too(self) -> None:
        admin, store = FakeAdmin(media=2), _marked_x(disabled_user_timestamp=1.0)
        mgr = _refusing_manager(store, admin, FakeMatrix(MAS_ISSUER), include_user_media_on_delete=True)
        await mgr.reconcile_accounts(ORPHAN_X)
        assert admin.deactivated == [] and admin.media_requests == []
        assert store.ledger.entries[X].deactivated_timestamp is None

    async def test_marking_and_clearing_go_on(self) -> None:
        """Only the destructive steps are refused: the cooldown clock still starts."""
        store = InMemoryLedgerStore()
        mgr = _refusing_manager(store, FakeAdmin(), FakeMatrix(MAS_ISSUER))
        await mgr.reconcile_accounts(ORPHAN_X)
        assert store.ledger.entries[X].marked_for_disabling_timestamp is not None
        await mgr.reconcile_accounts(ACTIVE_X)
        assert X not in store.ledger.entries

    async def test_the_guard_is_not_asked_about_refused_steps(self) -> None:
        mgr = _refusing_manager(_marked_x(), FakeAdmin(), FakeMatrix(MAS_ISSUER))
        assert await mgr.plan_accounts(ORPHAN_X) == []

    async def test_a_startup_report_is_not_repeated_by_the_pass(self) -> None:
        mgr = _refusing_manager(_marked_x(), FakeAdmin(), FakeMatrix(MAS_ISSUER))
        assert len(await mgr.preflight()) == 1
        assert await mgr.reconcile_accounts(ORPHAN_X) == []

    async def test_a_pass_report_is_not_repeated_by_a_late_startup_check(self) -> None:
        mgr = _refusing_manager(_marked_x(), FakeAdmin(), FakeMatrix(MAS_ISSUER))
        assert len(await mgr.reconcile_accounts(ORPHAN_X)) == 1
        assert await mgr.preflight() == []

    async def test_no_answer_yet_holds_the_lock_without_a_report(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fail closed: the startup check has not run or failed, so the pass asks; no answer, no lock."""
        admin, store = FakeAdmin(), _marked_x()
        mgr = _refusing_manager(store, admin, FakeMatrix(None, fail=2))
        with caplog.at_level(logging.WARNING):
            assert await mgr.plan_accounts(ORPHAN_X) == []
            assert await mgr.reconcile_accounts(ORPHAN_X) == []
        assert admin.logged_out == []
        assert store.ledger.entries[X].disabled_user_timestamp is None
        assert store.ledger.entries[X].failures == 0
        assert "could not ask the homeserver" in caplog.text

        # The homeserver answers on the next pass: it handles logins itself, so the lock goes ahead.
        [outcome] = await mgr.reconcile_accounts(ORPHAN_X)
        assert (outcome.action, outcome.failed) == (LifecycleAction.logout, False)
        assert admin.logged_out == [X]

    async def test_without_mas_the_synapse_fallback_locks(self) -> None:
        admin, store = FakeAdmin(), _marked_x()
        mgr = _refusing_manager(store, admin, FakeMatrix(None))
        [planned] = await mgr.plan_accounts(ORPHAN_X)
        assert planned.action is LifecycleAction.logout
        await mgr.reconcile_accounts(ORPHAN_X)
        assert admin.logged_out == [X]
        assert store.ledger.entries[X].disabled_user_timestamp is not None


# --- ledger robustness (LIF-14) ----------------------------------------------------------------

BOT = "@bot:company.org"
LEDGER_TYPE = "org.company.onbot.lifecycle"
KNOWN_TYPE = "org.company.onbot.known_users"


def _account_store(
    lifecycle: Any = None, known: Any = None
) -> tuple[FakeMatrixClient, MatrixAccountDataLedgerStore]:
    client = FakeMatrixClient()
    if lifecycle is not None:
        client.account_data[(BOT, LEDGER_TYPE)] = lifecycle
    if known is not None:
        client.account_data[(BOT, KNOWN_TYPE)] = known
    return client, MatrixAccountDataLedgerStore(client, BOT, SERVER)


class TestLedgerRobustness:
    async def test_a_corrupt_entry_is_quarantined_and_the_rest_goes_on(self) -> None:
        y = mx("y")
        client, store = _account_store(
            {
                "schema_version": 1,
                "entries": {
                    X: {"marked_for_disabling_timestamp": "yesterday"},  # hand-edited, broken
                    y: {"marked_for_disabling_timestamp": 1.0},
                },
            }
        )
        eff = RecordingEffectors()
        mgr = AccountLifecycleManager(_config(dry_run=False), store, eff, clock=Clock(DAY * 2))
        both = snapshot(user("x", active=False), user("y", active=False))
        outcomes = await mgr.reconcile_accounts(both)
        assert eff.logouts == [y]
        assert [(o.mxid, o.action) for o in outcomes if o.needs_attention] == [(X, LifecycleAction.alert)]
        raw = client.account_data[(BOT, LEDGER_TYPE)]
        assert raw["quarantined"] == {X: {"marked_for_disabling_timestamp": "yesterday"}}  # kept verbatim
        assert X not in raw["entries"]
        again = await mgr.reconcile_accounts(both)
        assert not [o for o in again if o.needs_attention]  # reported once
        assert eff.logouts == [y]

    @pytest.mark.parametrize(
        "blob",
        [
            {"schema_version": 99, "entries": {}},
            {"schema_version": 1, "entries": ["not", "a", "map"]},
            ["not", "an", "object"],
        ],
    )
    async def test_an_unreadable_ledger_aborts_the_step_without_writing(self, blob: Any) -> None:
        client, store = _account_store(blob)
        eff = RecordingEffectors()
        mgr = AccountLifecycleManager(_config(dry_run=False), store, eff, clock=Clock(DAY))
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert [(o.mxid, o.action) for o in outcomes] == [(None, LifecycleAction.abort)]
        assert eff.calls == 0
        assert client.account_data[(BOT, LEDGER_TYPE)] == blob  # untouched
        assert await mgr.reconcile_accounts(ORPHAN_X) == []  # reported once per process

    async def test_an_unreadable_known_users_record_aborts_the_step(self) -> None:
        _, store = _account_store({"schema_version": 1, "entries": {}}, known={"users": {"1": {"mxid": 5}}})
        mgr = AccountLifecycleManager(_config(dry_run=False), store, RecordingEffectors(), clock=Clock(DAY))
        outcomes = await mgr.reconcile_accounts(ORPHAN_X)
        assert [o.action for o in outcomes] == [LifecycleAction.abort]

    async def test_a_missing_ledger_with_locked_accounts_raises_an_alarm_once(self) -> None:
        client, store = _account_store()
        mgr = AccountLifecycleManager(
            _config(dry_run=False), store, MasLifecycleEffectors(FakeMasAdmin()), clock=Clock(DAY)
        )
        outcomes = await mgr.reconcile_accounts(ACTIVE_X)
        alerts = [o for o in outcomes if o.action is LifecycleAction.alert]
        assert len(alerts) == 1 and "3 account(s) are locked" in (alerts[0].detail or "")
        assert (BOT, LEDGER_TYPE) in client.account_data  # an empty ledger now exists
        restarted = AccountLifecycleManager(
            _config(dry_run=False), store, MasLifecycleEffectors(FakeMasAdmin()), clock=Clock(DAY)
        )
        later = await restarted.reconcile_accounts(ACTIVE_X)
        assert not [o for o in later if o.action is LifecycleAction.alert]

    async def test_a_missing_ledger_without_a_lock_counter_is_quiet(self) -> None:
        _, store = _account_store()
        mgr = AccountLifecycleManager(_config(dry_run=False), store, RecordingEffectors(), clock=Clock(DAY))
        assert await mgr.reconcile_accounts(ACTIVE_X) == []

    async def test_a_final_lock_record_is_forgotten_after_the_retention(self) -> None:
        """Locked, deleted upstream, erase disabled: nothing can follow, so the record goes."""
        locked_at = 1000.0
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={X: LifecycleEntry(disabled_user_timestamp=locked_at, authentik_pk="pk-x")}
            ),
            known=_known(x="pk-x"),
        )
        eff, clock = RecordingEffectors(), Clock(locked_at + LEDGER_RETENTION_SEC - 1)
        mgr = _manager(store, eff, clock, dry_run=False, remove_after_n_sec=None)
        deleted = snapshot(matrix={X})  # the Authentik user is gone, the Matrix account is not
        assert await mgr.reconcile_accounts(deleted) == []
        assert X in store.ledger.entries
        clock.now = locked_at + LEDGER_RETENTION_SEC
        outcomes = await mgr.reconcile_accounts(deleted)
        assert [o.action for o in outcomes] == [LifecycleAction.forget]
        assert store.ledger.entries == {} and store.known.users == {}
        clock.now += DAY * 400
        assert await mgr.reconcile_accounts(deleted) == []  # never touched again
        assert eff.calls == 0

    @pytest.mark.parametrize("variant", ["still_disabled", "erase_enabled", "frozen"])
    async def test_other_lock_records_are_kept_however_old(self, variant: str) -> None:
        entry = LifecycleEntry(disabled_user_timestamp=1.0, authentik_pk="pk-x")
        snap = snapshot(matrix={X})
        delete_after: int | None = None
        if variant == "still_disabled":
            snap = ORPHAN_X  # may still be re-enabled: the lock record is needed for the unlock
        elif variant == "erase_enabled":
            delete_after = DAY * 10_000
        else:
            entry.frozen_reason = "the Authentik user is on authentik_user_ignore_list"
        store = InMemoryLedgerStore(LifecycleLedger(entries={X: entry}), known=_known(x="pk-x"))
        mgr = _manager(
            store,
            RecordingEffectors(),
            Clock(LEDGER_RETENTION_SEC * 5),
            dry_run=False,
            remove_after_n_sec=delete_after,
        )
        await mgr.reconcile_accounts(snap)
        assert X in store.ledger.entries
