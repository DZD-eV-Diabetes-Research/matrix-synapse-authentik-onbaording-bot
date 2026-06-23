"""Quarantined account lifecycle (AD-5, G9.*): the scariest code in the repo.

When an Authentik account is disabled (or removed), the matching Matrix account must eventually be
locked out and, optionally, erased. This is destructive and irreversible past a point, so the module
is built around three safety principles:

* **Cooldowns absorb accidents (G12.3).** Detection only *marks* a user; nothing destructive happens
  until ``deactivate_after_n_sec`` has elapsed, and erasure waits a further ``delete_after_n_sec``.
  A user who reappears in Authentik before erasure is silently re-enabled (G9.6).
* **Dry-run + audit by default (Q6, AD-5).** Destructive effector calls are gated on an explicit
  ``dry_run=False``; until an operator opts in, the manager only records bookkeeping timestamps and
  logs an audit line describing what *would* happen.
* **Pure decision, isolated effects.** :func:`decide_account_action` is a side-effect-free state
  machine (exhaustively unit-tested); all I/O lives behind the :class:`LifecycleEffectors` and
  :class:`LifecycleLedgerStore` seams.

**Two-stage semantics (legacy-faithful, GOALS-aligned).** Stage one (``deactivate_after_n_sec``)
*logs the user out* — revoking every device/session (G9.2). Under the MAS topology (AD-6) this is the
effective lock-out: re-login flows through MAS→Authentik, which already blocks the disabled upstream
user. It is also reversible, which keeps the G9.6 re-enable window open. Stage two
(``delete_after_n_sec``) *erases* the account (G9.4) and optionally its uploaded media (G9.5).

Whether MAS *itself* propagates the upstream disable (revoking sessions / locking the account) is an
open question for the maintainer (BATTLE_PLAN §7 Q1); this module is the enforcement backstop either
way.

There is no database (AD-1): the per-user bookkeeping (mark/disable timestamps) is persisted as a
single blob in the bot's Matrix *account data*, keyed by MXID — decoupled from the onboarding DM.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from onbot.config import OnbotConfig
from onbot.logging import get_logger
from onbot.reconciler.state import SCHEMA_VERSION, event_type_name

log = get_logger(__name__)
# Dedicated audit channel so operators can route the irreversible-action trail separately.
audit = get_logger("onbot.lifecycle.audit")


def lifecycle_account_data_type(server_name: str) -> str:
    """Account-data type holding the lifecycle ledger, e.g. ``org.company.onbot.lifecycle``."""
    return event_type_name(server_name, "lifecycle")


class LifecycleAction(StrEnum):
    none = "none"  # active user, no bookkeeping to do
    reenable = "reenable"  # previously marked/disabled but active again → clear (G9.6)
    mark = "mark"  # newly detected orphan → start the cooldown clock (no destructive action)
    logout = "logout"  # cooldown elapsed → revoke sessions (G9.2)
    erase = "erase"  # further cooldown elapsed → deactivate/erase account (G9.4/G9.5)
    wait = "wait"  # in a cooldown window → do nothing yet


def decide_account_action(
    *,
    is_orphaned: bool,
    marked_ts: float | None,
    disabled_ts: float | None,
    now: float,
    deactivate_after_n_sec: float,
    delete_after_n_sec: float | None,
) -> LifecycleAction:
    """Pure state machine: given a user's orphan status and timestamps, decide the next action.

    ``marked_ts`` is when the user was first seen orphaned; ``disabled_ts`` is when they were logged
    out. ``delete_after_n_sec=None`` disables erasure entirely (a logged-out user simply stays so).
    """
    has_state = marked_ts is not None or disabled_ts is not None
    if not is_orphaned:
        return LifecycleAction.reenable if has_state else LifecycleAction.none
    if marked_ts is None:
        return LifecycleAction.mark
    if disabled_ts is None:
        if now - marked_ts >= deactivate_after_n_sec:
            return LifecycleAction.logout
        return LifecycleAction.wait
    if delete_after_n_sec is not None and now - disabled_ts >= delete_after_n_sec:
        return LifecycleAction.erase
    return LifecycleAction.wait


class LifecycleEntry(BaseModel):
    """Per-user bookkeeping for the lifecycle state machine."""

    marked_for_disabling_timestamp: float | None = None
    disabled_user_timestamp: float | None = None


class LifecycleLedger(BaseModel):
    """Versioned blob of every tracked user's lifecycle state (stored in bot account data)."""

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, LifecycleEntry] = Field(default_factory=dict)


@runtime_checkable
class LifecycleEffectors(Protocol):
    """The destructive Matrix/Synapse operations, isolated behind a seam (separate boundary, AD-5)."""

    async def logout(self, mxid: str) -> None:
        """Revoke every session for ``mxid`` (G9.2). Reversible (see :meth:`reenable`)."""

    async def reenable(self, mxid: str) -> None:
        """Reverse a non-destructive logout when the user returns (G9.6). May be a no-op."""

    async def erase(self, mxid: str, *, delete_media: bool) -> None:
        """Deactivate/erase the account (G9.4) and optionally its uploaded media (G9.5)."""


class AdminApiLifecycleEffectors:
    """Concrete effectors over the Synapse Admin API.

    NOTE (BATTLE_PLAN §7 Q1, verified by the Phase 7b harness): under MAS these do **not** revoke a
    live MAS-issued session — deleting devices/deactivating in Synapse leaves the MAS token valid.
    Use :class:`MasLifecycleEffectors` for real enforcement under the MAS topology (ADR-0006). These
    remain correct for non-MAS Synapse deployments.
    """

    def __init__(self, admin: Any) -> None:
        self.admin = admin

    async def logout(self, mxid: str) -> None:
        await self.admin.logout_account(mxid)

    async def reenable(self, mxid: str) -> None:
        # Synapse logout deletes devices; there is nothing to restore — the user logs in afresh.
        return None

    async def erase(self, mxid: str, *, delete_media: bool) -> None:
        if delete_media:
            await self.admin.delete_user_media(mxid)
        await self.admin.deactivate_account(mxid, erase=delete_media)


class MasLifecycleEffectors:
    """Effectors that enforce lockout through the **MAS admin API** (ADR-0006, §7 Q1).

    This is the path that actually works under MAS: ``logout`` locks the MAS user (revoking live
    sessions, reversibly), ``reenable`` unlocks them when their upstream account returns (G9.6), and
    ``erase`` deactivates the account irreversibly. Media deletion still goes through the Synapse
    admin API (MAS does not own media).
    """

    def __init__(self, mas_admin: Any, synapse_admin: Any | None = None) -> None:
        self.mas = mas_admin
        self.synapse_admin = synapse_admin

    async def _resolve(self, mxid: str) -> str | None:
        from onbot.clients.mas_admin import mxid_localpart

        uid: str | None = await self.mas.get_user_id_by_username(mxid_localpart(mxid))
        if uid is None:
            log.warning("no MAS user for %s; cannot enforce lifecycle action", mxid)
        return uid

    async def logout(self, mxid: str) -> None:
        uid = await self._resolve(mxid)
        if uid is not None:
            await self.mas.lock_user(uid)

    async def reenable(self, mxid: str) -> None:
        uid = await self._resolve(mxid)
        if uid is not None:
            await self.mas.unlock_user(uid)

    async def erase(self, mxid: str, *, delete_media: bool) -> None:
        uid = await self._resolve(mxid)
        if uid is not None:
            await self.mas.deactivate_user(uid)
        if delete_media and self.synapse_admin is not None:
            await self.synapse_admin.delete_user_media(mxid)


@runtime_checkable
class LifecycleLedgerStore(Protocol):
    async def load(self) -> LifecycleLedger: ...

    async def save(self, ledger: LifecycleLedger) -> None: ...


class MatrixAccountDataLedgerStore:
    """Persists the ledger as a single account-data blob on the bot user (no database, AD-1)."""

    def __init__(self, client: Any, bot_id: str, server_name: str) -> None:
        self.client = client
        self.bot_id = bot_id
        self.data_type = lifecycle_account_data_type(server_name)

    async def load(self) -> LifecycleLedger:
        raw = await self.client.get_account_data(self.bot_id, self.data_type)
        if not raw:
            return LifecycleLedger()
        return LifecycleLedger.model_validate(raw)

    async def save(self, ledger: LifecycleLedger) -> None:
        await self.client.set_account_data(self.bot_id, self.data_type, ledger.model_dump(mode="json"))


class LifecycleOutcome(BaseModel):
    """What the manager decided for one user this pass (returned for tests/observability)."""

    mxid: str
    action: LifecycleAction
    dry_run: bool


class AccountLifecycleManager:
    """Orchestrates the lifecycle pass: load ledger → decide per user → act → persist.

    Invoked only by the reconciler with the set of *orphaned* MXIDs (Matrix accounts whose Authentik
    user is disabled/gone). Any MXID already in the ledger that is no longer orphaned is re-enabled.
    """

    def __init__(
        self,
        config: OnbotConfig,
        store: LifecycleLedgerStore,
        effectors: LifecycleEffectors,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
        self.store = store
        self.effectors = effectors
        self.clock = clock

    async def reconcile_accounts(self, orphaned_mxids: set[str]) -> list[LifecycleOutcome]:
        if not self.cfg.enabled:
            return []
        ledger = await self.store.load()
        now = self.clock()
        outcomes: list[LifecycleOutcome] = []
        changed = False

        # Decide for every orphan and for anyone we are already tracking (so they can be re-enabled).
        for mxid in sorted(orphaned_mxids | set(ledger.entries)):
            entry = ledger.entries.get(mxid)
            action = decide_account_action(
                is_orphaned=mxid in orphaned_mxids,
                marked_ts=entry.marked_for_disabling_timestamp if entry else None,
                disabled_ts=entry.disabled_user_timestamp if entry else None,
                now=now,
                deactivate_after_n_sec=self.cfg.deactivate_after_n_sec,
                delete_after_n_sec=self.cfg.delete_after_n_sec,
            )
            if action is LifecycleAction.none or action is LifecycleAction.wait:
                continue
            changed |= await self._apply(ledger, mxid, action, now)
            outcomes.append(LifecycleOutcome(mxid=mxid, action=action, dry_run=self.cfg.dry_run))

        if changed:
            await self.store.save(ledger)
        return outcomes

    async def _apply(self, ledger: LifecycleLedger, mxid: str, action: LifecycleAction, now: float) -> bool:
        """Carry out one decision; return whether the ledger changed. Destructive ops respect dry-run."""
        if action is LifecycleAction.reenable:
            # Restorative (only ever grants access back), so it runs even under dry-run — it undoes a
            # prior real logout/lock. A no-op when nothing was locked.
            await self.effectors.reenable(mxid)
            ledger.entries.pop(mxid, None)
            audit.info("re-enabled %s (Authentik account active again); cleared lifecycle state", mxid)
            return True

        if action is LifecycleAction.mark:
            # Bookkeeping only — harmless to persist even in dry-run, and it starts the cooldown clock.
            ledger.entries[mxid] = LifecycleEntry(marked_for_disabling_timestamp=now)
            audit.info(
                "marked %s for lifecycle action (Authentik account disabled); logout in %ss",
                mxid,
                self.cfg.deactivate_after_n_sec,
            )
            return True

        if action is LifecycleAction.logout:
            if self.cfg.dry_run:
                audit.warning("DRY-RUN: would log out %s (revoke all sessions)", mxid)
                return False
            await self.effectors.logout(mxid)
            entry = ledger.entries.setdefault(mxid, LifecycleEntry())
            entry.disabled_user_timestamp = now
            audit.warning(
                "logged out %s (revoked all sessions); erase in %ss", mxid, self.cfg.delete_after_n_sec
            )
            return True

        # action is LifecycleAction.erase
        if self.cfg.dry_run:
            audit.warning(
                "DRY-RUN: would erase %s (deactivate account, delete_media=%s)",
                mxid,
                self.cfg.include_user_media_on_delete,
            )
            return False
        await self.effectors.erase(mxid, delete_media=self.cfg.include_user_media_on_delete)
        ledger.entries.pop(mxid, None)
        audit.warning(
            "erased %s (account deactivated, media=%s)", mxid, self.cfg.include_user_media_on_delete
        )
        return True
