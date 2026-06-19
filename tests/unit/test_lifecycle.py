"""Highest-rigor tests for the quarantined account lifecycle (AD-5, G9.*).

Covers the pure state machine exhaustively, the manager's dry-run/live behaviour and full
multi-tick progression, the account-data ledger store, and the admin-API effectors.
"""

from __future__ import annotations

from typing import Any

import pytest

from onbot.config import OnbotConfig
from onbot.lifecycle.accounts import (
    AccountLifecycleManager,
    AdminApiLifecycleEffectors,
    LifecycleAction,
    LifecycleEntry,
    LifecycleLedger,
    MatrixAccountDataLedgerStore,
    decide_account_action,
    lifecycle_account_data_type,
)

DAY = 60 * 60 * 24

_BASE: dict[str, Any] = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
}


def _config(**lifecycle: Any) -> OnbotConfig:
    cfg = OnbotConfig.model_validate(_BASE)
    lc = cfg.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
    for key, value in lifecycle.items():
        setattr(lc, key, value)
    return cfg


# --- pure state machine ------------------------------------------------------------------------


class TestDecideAccountAction:
    def test_active_user_without_state_is_noop(self) -> None:
        action = decide_account_action(
            is_orphaned=False,
            marked_ts=None,
            disabled_ts=None,
            now=100.0,
            deactivate_after_n_sec=DAY,
            delete_after_n_sec=DAY,
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
            is_orphaned=False,
            marked_ts=marked_ts,
            disabled_ts=disabled_ts,
            now=100.0,
            deactivate_after_n_sec=DAY,
            delete_after_n_sec=DAY,
        )
        assert action is LifecycleAction.reenable

    def test_new_orphan_is_marked(self) -> None:
        action = decide_account_action(
            is_orphaned=True,
            marked_ts=None,
            disabled_ts=None,
            now=100.0,
            deactivate_after_n_sec=DAY,
            delete_after_n_sec=DAY,
        )
        assert action is LifecycleAction.mark

    def test_marked_orphan_in_cooldown_waits(self) -> None:
        action = decide_account_action(
            is_orphaned=True,
            marked_ts=100.0,
            disabled_ts=None,
            now=100.0 + DAY - 1,
            deactivate_after_n_sec=DAY,
            delete_after_n_sec=DAY,
        )
        assert action is LifecycleAction.wait

    def test_marked_orphan_at_cooldown_boundary_logs_out(self) -> None:
        action = decide_account_action(
            is_orphaned=True,
            marked_ts=100.0,
            disabled_ts=None,
            now=100.0 + DAY,  # exactly at the boundary (>=)
            deactivate_after_n_sec=DAY,
            delete_after_n_sec=DAY,
        )
        assert action is LifecycleAction.logout

    def test_disabled_orphan_in_delete_cooldown_waits(self) -> None:
        action = decide_account_action(
            is_orphaned=True,
            marked_ts=1.0,
            disabled_ts=100.0,
            now=100.0 + DAY - 1,
            deactivate_after_n_sec=DAY,
            delete_after_n_sec=DAY,
        )
        assert action is LifecycleAction.wait

    def test_disabled_orphan_after_delete_cooldown_is_erased(self) -> None:
        action = decide_account_action(
            is_orphaned=True,
            marked_ts=1.0,
            disabled_ts=100.0,
            now=100.0 + DAY,
            deactivate_after_n_sec=DAY,
            delete_after_n_sec=DAY,
        )
        assert action is LifecycleAction.erase

    def test_disabled_orphan_never_erased_when_delete_disabled(self) -> None:
        action = decide_account_action(
            is_orphaned=True,
            marked_ts=1.0,
            disabled_ts=100.0,
            now=100.0 + DAY * 1000,
            deactivate_after_n_sec=DAY,
            delete_after_n_sec=None,  # erasure disabled
        )
        assert action is LifecycleAction.wait


# --- fakes -------------------------------------------------------------------------------------


class InMemoryLedgerStore:
    def __init__(self, ledger: LifecycleLedger | None = None) -> None:
        self.ledger = ledger or LifecycleLedger()
        self.saves = 0

    async def load(self) -> LifecycleLedger:
        # Return a copy so the manager mutating it does not retroactively change what was "stored".
        return self.ledger.model_copy(deep=True)

    async def save(self, ledger: LifecycleLedger) -> None:
        self.ledger = ledger.model_copy(deep=True)
        self.saves += 1


class RecordingEffectors:
    def __init__(self) -> None:
        self.logouts: list[str] = []
        self.erases: list[tuple[str, bool]] = []

    async def logout(self, mxid: str) -> None:
        self.logouts.append(mxid)

    async def erase(self, mxid: str, *, delete_media: bool) -> None:
        self.erases.append((mxid, delete_media))


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


# --- manager: dry-run (default) ----------------------------------------------------------------


class TestManagerDryRun:
    async def test_disabled_config_is_total_noop(self) -> None:
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock, enabled=False)
        outcomes = await mgr.reconcile_accounts({"@x:company.org"})
        assert outcomes == []
        assert store.saves == 0
        assert eff.logouts == [] and eff.erases == []

    async def test_marks_new_orphan_even_in_dry_run(self) -> None:
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock)  # dry_run defaults to True
        outcomes = await mgr.reconcile_accounts({"@x:company.org"})
        assert [o.action for o in outcomes] == [LifecycleAction.mark]
        assert outcomes[0].dry_run is True
        # Bookkeeping persisted; the clock started.
        assert store.ledger.entries["@x:company.org"].marked_for_disabling_timestamp == clock.now
        assert eff.logouts == []

    async def test_dry_run_logout_does_not_act_or_advance_state(self) -> None:
        marked = 1000.0
        store = InMemoryLedgerStore(
            LifecycleLedger(entries={"@x:company.org": LifecycleEntry(marked_for_disabling_timestamp=marked)})
        )
        eff, clock = RecordingEffectors(), Clock(marked + DAY)
        mgr = _manager(store, eff, clock)
        outcomes = await mgr.reconcile_accounts({"@x:company.org"})
        assert [o.action for o in outcomes] == [LifecycleAction.logout]
        assert eff.logouts == []  # no destructive call
        # disabled_ts NOT set, so a later live run still performs the logout.
        assert store.ledger.entries["@x:company.org"].disabled_user_timestamp is None
        assert store.saves == 0

    async def test_dry_run_erase_does_not_act(self) -> None:
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={
                    "@x:company.org": LifecycleEntry(
                        marked_for_disabling_timestamp=1.0, disabled_user_timestamp=1000.0
                    )
                }
            )
        )
        eff, clock = RecordingEffectors(), Clock(1000.0 + DAY)
        mgr = _manager(store, eff, clock, delete_after_n_sec=DAY)
        outcomes = await mgr.reconcile_accounts({"@x:company.org"})
        assert [o.action for o in outcomes] == [LifecycleAction.erase]
        assert eff.erases == []
        assert "@x:company.org" in store.ledger.entries  # not removed in dry-run


# --- manager: live -----------------------------------------------------------------------------


class TestManagerLive:
    async def test_live_logout_revokes_and_records(self) -> None:
        marked = 1000.0
        store = InMemoryLedgerStore(
            LifecycleLedger(entries={"@x:company.org": LifecycleEntry(marked_for_disabling_timestamp=marked)})
        )
        eff, clock = RecordingEffectors(), Clock(marked + DAY)
        mgr = _manager(store, eff, clock, dry_run=False)
        await mgr.reconcile_accounts({"@x:company.org"})
        assert eff.logouts == ["@x:company.org"]
        assert store.ledger.entries["@x:company.org"].disabled_user_timestamp == clock.now

    async def test_live_erase_calls_effector_with_media_flag_and_clears_entry(self) -> None:
        store = InMemoryLedgerStore(
            LifecycleLedger(
                entries={
                    "@x:company.org": LifecycleEntry(
                        marked_for_disabling_timestamp=1.0, disabled_user_timestamp=1000.0
                    )
                }
            )
        )
        eff, clock = RecordingEffectors(), Clock(1000.0 + DAY)
        mgr = _manager(
            store, eff, clock, dry_run=False, delete_after_n_sec=DAY, include_user_media_on_delete=True
        )
        await mgr.reconcile_accounts({"@x:company.org"})
        assert eff.erases == [("@x:company.org", True)]
        assert "@x:company.org" not in store.ledger.entries  # terminal

    async def test_reenable_clears_state_for_returning_user(self) -> None:
        store = InMemoryLedgerStore(
            LifecycleLedger(entries={"@x:company.org": LifecycleEntry(marked_for_disabling_timestamp=1.0)})
        )
        eff, clock = RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock, dry_run=False)
        # @x is no longer in the orphaned set → re-enabled.
        outcomes = await mgr.reconcile_accounts(set())
        assert [o.action for o in outcomes] == [LifecycleAction.reenable]
        assert store.ledger.entries == {}
        assert eff.logouts == [] and eff.erases == []

    async def test_full_progression_across_ticks(self) -> None:
        """mark → wait → logout → wait → erase, driven by a moving clock (live mode)."""
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock(0.0)
        mgr = _manager(store, eff, clock, dry_run=False, deactivate_after_n_sec=DAY, delete_after_n_sec=DAY)
        orphan = {"@x:company.org"}

        # t=0: first sight → mark
        assert [o.action for o in await mgr.reconcile_accounts(orphan)] == [LifecycleAction.mark]

        # t=1h: still in deactivate cooldown → wait (no outcome recorded)
        clock.now = 60 * 60
        assert await mgr.reconcile_accounts(orphan) == []
        assert eff.logouts == []

        # t=1 day: deactivate cooldown elapsed → logout
        clock.now = DAY
        assert [o.action for o in await mgr.reconcile_accounts(orphan)] == [LifecycleAction.logout]
        assert eff.logouts == ["@x:company.org"]

        # t=1 day + 1h: delete cooldown not yet elapsed → wait
        clock.now = DAY + 60 * 60
        assert await mgr.reconcile_accounts(orphan) == []
        assert eff.erases == []

        # t≈2 days: delete cooldown elapsed → erase
        clock.now = DAY + DAY
        assert [o.action for o in await mgr.reconcile_accounts(orphan)] == [LifecycleAction.erase]
        assert eff.erases == [("@x:company.org", False)]
        assert store.ledger.entries == {}

    async def test_no_save_when_nothing_changes(self) -> None:
        store, eff, clock = InMemoryLedgerStore(), RecordingEffectors(), Clock()
        mgr = _manager(store, eff, clock, dry_run=False)
        assert await mgr.reconcile_accounts(set()) == []
        assert store.saves == 0


# --- ledger store ------------------------------------------------------------------------------


class FakeMatrixClient:
    def __init__(self) -> None:
        self.account_data: dict[tuple[str, str], dict[str, Any]] = {}

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        return self.account_data.get((user_id, data_type), {})

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        self.account_data[(user_id, data_type)] = dict(content)


class TestLedgerStore:
    def test_account_data_type_is_namespaced(self) -> None:
        assert lifecycle_account_data_type("company.org") == "org.company.onbot.lifecycle"

    async def test_load_missing_returns_empty_ledger(self) -> None:
        store = MatrixAccountDataLedgerStore(FakeMatrixClient(), "@bot:company.org", "company.org")
        ledger = await store.load()
        assert ledger.entries == {}

    async def test_round_trip(self) -> None:
        client = FakeMatrixClient()
        store = MatrixAccountDataLedgerStore(client, "@bot:company.org", "company.org")
        ledger = LifecycleLedger(
            entries={"@x:company.org": LifecycleEntry(marked_for_disabling_timestamp=5.0)}
        )
        await store.save(ledger)
        reloaded = await store.load()
        assert reloaded.entries["@x:company.org"].marked_for_disabling_timestamp == 5.0


# --- admin-api effectors -----------------------------------------------------------------------


class FakeAdmin:
    def __init__(self) -> None:
        self.logged_out: list[str] = []
        self.deactivated: list[tuple[str, bool]] = []
        self.media_deleted: list[str] = []

    async def logout_account(self, user_id: str) -> None:
        self.logged_out.append(user_id)

    async def deactivate_account(self, user_id: str, *, erase: bool) -> None:
        self.deactivated.append((user_id, erase))

    async def delete_user_media(self, user_id: str) -> dict[str, Any]:
        self.media_deleted.append(user_id)
        return {}


class TestAdminApiEffectors:
    async def test_logout_delegates(self) -> None:
        admin = FakeAdmin()
        await AdminApiLifecycleEffectors(admin).logout("@x:company.org")
        assert admin.logged_out == ["@x:company.org"]

    async def test_erase_without_media(self) -> None:
        admin = FakeAdmin()
        await AdminApiLifecycleEffectors(admin).erase("@x:company.org", delete_media=False)
        assert admin.media_deleted == []
        assert admin.deactivated == [("@x:company.org", False)]

    async def test_erase_with_media_deletes_media_first(self) -> None:
        admin = FakeAdmin()
        await AdminApiLifecycleEffectors(admin).erase("@x:company.org", delete_media=True)
        assert admin.media_deleted == ["@x:company.org"]
        assert admin.deactivated == [("@x:company.org", True)]
