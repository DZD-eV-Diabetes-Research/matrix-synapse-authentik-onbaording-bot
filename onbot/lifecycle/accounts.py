"""Quarantined account lifecycle (AD-5, G9.*): the scariest code in the repo.

When an Authentik account is disabled or deleted, the matching Matrix account must eventually be
locked out and, optionally, erased. This is destructive and irreversible past a point, so the module
is built around these safety principles:

* **Cooldowns absorb accidents (G12.3).** Detection only *marks* a user; nothing destructive happens
  until ``deactivate_after_n_sec`` has elapsed, and erasure waits a further ``delete_after_n_sec``
  (counted from the lock).
* **Fail closed (review S1).** Access is only ever given back on positive evidence: the bound
  Authentik user is present, active and in the sync scope. Dropping out of a query (deletion, a lost
  attribute, an ignore-list edit, a mapping change, an empty API answer) never unlocks anybody. A
  tracked user who leaves the sync scope is *frozen* (no unlock, no further step) and reported (D9).
* **Dry-run + audit by default (Q6, AD-5).** While ``dry_run`` is on, no effector is called at all,
  not even the restorative unlock; the manager only records bookkeeping timestamps and logs an audit
  line describing what *would* happen.
* **Pure decisions, isolated effects.** :func:`classify_accounts` and :func:`decide_account_action`
  are side-effect free and exhaustively unit-tested; all I/O lives behind the
  :class:`LifecycleEffectors` and :class:`LifecycleLedgerStore` seams.
* **One user cannot stall the rest (LIF-03).** Every user is handled in isolation, a failing user
  backs off, and the ledger is saved after every user, so a crash never repeats a completed
  irreversible step.

**Two stages.** Stage one (``deactivate_after_n_sec``) locks the user out: under MAS (AD-6) a MAS lock,
which suspends every session and blocks new logins, reversibly (ADR-0005 §7 Q1). Stage two
(``delete_after_n_sec``) erases the account (G9.4) and optionally its uploaded media (G9.5).

**Who is tracked.** Disabled Authentik users in the sync scope whose Matrix account exists, plus
*known users*: every in-scope, active, mapped user is recorded once as ``authentik pk -> MXID``
(:class:`KnownUsers`). A known user who later disappears from Authentik is treated as deleted and goes
through the same cooldowns (LIF-02). A Matrix account that was never mapped (service accounts, MAS-only
users) is never touched.

There is no database (AD-1): the ledger and the known-users record are blobs in the bot's Matrix
*account data*.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from onbot.config import OnbotConfig
from onbot.logging import get_logger
from onbot.reconciler.state import SCHEMA_VERSION, event_type_name

log = get_logger(__name__)
# Dedicated audit channel so operators can route the irreversible-action trail separately.
audit = get_logger("onbot.lifecycle.audit")

# A user whose step failed waits before the next attempt (LIF-03): 1 minute, doubling, at most 6 hours.
_BACKOFF_BASE_SEC = 60.0
_BACKOFF_MAX_SEC = 6 * 60 * 60.0
_MAX_ERROR_LEN = 300


def lifecycle_account_data_type(server_name: str) -> str:
    """Account-data type holding the lifecycle ledger, e.g. ``org.company.onbot.lifecycle``."""
    return event_type_name(server_name, "lifecycle")


def known_users_account_data_type(server_name: str) -> str:
    """Account-data type holding the known users record, e.g. ``org.company.onbot.known_users``."""
    return event_type_name(server_name, "known_users")


class LifecycleTargetNotFound(RuntimeError):
    """The effector could not find the account it must act on (for example a MAS lookup 404, LIF-10).

    Raised instead of returning quietly, so the state machine does not advance and the audit log
    records a failure rather than a lockout that never happened.
    """


class AccountStatus(StrEnum):
    """What the directory says about one Matrix account this pass."""

    untracked = "untracked"  # no evidence and nothing tracked: never touched
    active = "active"  # bound Authentik user present, active and in scope: may be re-enabled
    orphaned = "orphaned"  # disabled in scope, or known and deleted: offboarding proceeds
    frozen = "frozen"  # tracked but neither (left the scope, ignored, ambiguous): hold (D9)


class LifecycleAction(StrEnum):
    none = "none"  # nothing tracked, nothing to do
    reenable = "reenable"  # tracked but positively active and in scope again: clear (G9.6)
    mark = "mark"  # newly detected orphan: start the cooldown clock (no destructive action)
    logout = "logout"  # cooldown elapsed: lock the account (G9.2)
    erase = "erase"  # further cooldown elapsed: deactivate/erase the account (G9.4/G9.5)
    wait = "wait"  # in a cooldown window: do nothing yet
    freeze = "freeze"  # tracked but out of scope or ambiguous: hold everything and report (D9)


def decide_account_action(
    *,
    status: AccountStatus,
    marked_ts: float | None,
    disabled_ts: float | None,
    now: float,
    deactivate_after_n_sec: float,
    delete_after_n_sec: float | None,
) -> LifecycleAction:
    """Pure state machine: given a user's status and timestamps, decide the next action.

    ``marked_ts`` is when the user was first seen orphaned; ``disabled_ts`` is when onbot locked them.
    ``delete_after_n_sec=None`` disables erasure entirely (a locked user simply stays so). Only an
    ``active`` status can lead to ``reenable``: absence of evidence never gives access back.
    """
    has_state = marked_ts is not None or disabled_ts is not None
    if status is AccountStatus.active:
        return LifecycleAction.reenable if has_state else LifecycleAction.none
    if status is not AccountStatus.orphaned:
        return LifecycleAction.freeze if has_state else LifecycleAction.none
    if marked_ts is None and disabled_ts is None:
        return LifecycleAction.mark
    if disabled_ts is None:
        assert marked_ts is not None
        if now - marked_ts >= deactivate_after_n_sec:
            return LifecycleAction.logout
        return LifecycleAction.wait
    if delete_after_n_sec is not None and now - disabled_ts >= delete_after_n_sec:
        return LifecycleAction.erase
    return LifecycleAction.wait


class LifecycleEntry(BaseModel):
    """Per-user bookkeeping for the lifecycle state machine."""

    marked_for_disabling_timestamp: float | None = None
    # Set only after onbot itself locked the account in live mode. It is the only lock onbot ever
    # undoes (LIF-06): a lock a human set, or one that never happened in dry-run, is left alone.
    disabled_user_timestamp: float | None = None
    # The Authentik user this entry belongs to, so a rename or a lost mapping attribute is no escape.
    authentik_pk: str | None = None
    # The two halves of the erase stage, persisted separately so a retry resumes (LIF-03).
    deactivated_timestamp: float | None = None
    media_deleted_timestamp: float | None = None
    # Why the entry is on hold (D9); reported once when it is set.
    frozen_reason: str | None = None
    # Backoff after a failed step (LIF-03).
    failures: int = 0
    retry_after_timestamp: float | None = None
    last_error: str | None = None


class LifecycleLedger(BaseModel):
    """Versioned blob of every tracked user's lifecycle state (stored in bot account data)."""

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, LifecycleEntry] = Field(default_factory=dict)


class KnownUser(BaseModel):
    """One Authentik user onbot has seen active, in scope and mapped to an existing Matrix account."""

    mxid: str
    first_seen: float


class KnownUsers(BaseModel):
    """Record of ``authentik pk -> MXID`` bindings, first seen wins (stored in bot account data).

    It makes deletion detectable (LIF-02) and keeps a tracked account tied to the person it belongs
    to across mapping changes. It is bounded: a binding is dropped once the Authentik user is gone and
    nothing is left to offboard. Review session S9 (D8) extends this record into the authoritative
    identity binding; keep the shape stable (``users[pk].mxid``) when changing it.
    """

    schema_version: int = SCHEMA_VERSION
    users: dict[str, KnownUser] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DirectoryUser:
    """One Authentik user, as the lifecycle sees it this pass."""

    pk: str
    username: str
    mxid: str | None  # None when the mapping attribute is missing
    is_active: bool
    in_scope: bool  # matched by the sync scope (paths, attributes, groups)
    ignored: bool  # on authentik_user_ignore_list


@dataclass(frozen=True, slots=True)
class DirectorySnapshot:
    """Everything the lifecycle decides on, gathered by the reconciler in one consistent read."""

    users: tuple[DirectoryUser, ...]  # every Authentik user, in any state and any scope
    matrix_mxids: frozenset[str]  # Matrix accounts that exist
    excluded_mxids: frozenset[str]  # the bot and matrix_user_ignore_list
    synced_mxids: frozenset[str] = frozenset()  # accounts the room sync treats as active members


@dataclass(frozen=True, slots=True)
class Classification:
    status: AccountStatus
    reason: str
    pk: str | None = None  # the Authentik user this account is bound to, when unambiguous


def classify_accounts(
    snapshot: DirectorySnapshot, known: KnownUsers, ledger: LifecycleLedger
) -> dict[str, Classification]:
    """Classify every account the lifecycle has to look at (pure).

    Candidates are tracked accounts (ledger entries), known users, and disabled in-scope users. An
    account bound to an Authentik user (known record or ledger entry) is judged by *that* user only;
    an unbound one by the users whose mapping attribute computes to it.
    """
    by_pk = {u.pk: u for u in snapshot.users}
    computed: dict[str, list[DirectoryUser]] = defaultdict(list)
    for user in snapshot.users:
        if user.mxid is not None:
            computed[user.mxid].append(user)
    bound: dict[str, set[str]] = defaultdict(set)
    for pk, known_user in known.users.items():
        bound[known_user.mxid].add(pk)
    for mxid, entry in ledger.entries.items():
        if entry.authentik_pk is not None:
            bound[mxid].add(entry.authentik_pk)

    candidates = set(ledger.entries) | set(bound)
    candidates |= {u.mxid for u in snapshot.users if u.mxid is not None and u.in_scope and not u.is_active}
    return {
        mxid: _classify_one(mxid, snapshot, by_pk, computed.get(mxid, []), bound.get(mxid, set()), ledger)
        for mxid in candidates
    }


def _classify_one(
    mxid: str,
    snapshot: DirectorySnapshot,
    by_pk: dict[str, DirectoryUser],
    computed: list[DirectoryUser],
    bound_pks: set[str],
    ledger: LifecycleLedger,
) -> Classification:
    tracked = mxid in ledger.entries
    if bound_pks:
        primary = [by_pk[pk] for pk in sorted(bound_pks) if pk in by_pk]
        others = [u for u in computed if u.pk not in bound_pks]
    else:
        primary, others = list(computed), []
    everyone = primary + others
    pk: str | None = None
    if len(primary) == 1:
        pk = primary[0].pk
    elif len(bound_pks) == 1:
        pk = next(iter(bound_pks))

    def frozen(reason: str) -> Classification:
        return Classification(AccountStatus.frozen, reason, pk)

    if mxid in snapshot.excluded_mxids:
        return frozen("the bot account or on matrix_user_ignore_list")
    if any(u.ignored for u in everyone):
        return frozen("the Authentik user is on authentik_user_ignore_list")
    if any(u.is_active and u.in_scope for u in primary):
        return Classification(AccountStatus.active, "active in Authentik and in the sync scope", pk)
    if any(u.is_active for u in primary):
        return frozen("active in Authentik but outside the sync scope")
    if any(u.is_active for u in others) or mxid in snapshot.synced_mxids:
        return frozen("another active Authentik user maps to this Matrix account")
    if primary:
        if not any(u.in_scope for u in primary):
            return frozen("disabled in Authentik but outside the sync scope")
        status = Classification(AccountStatus.orphaned, "disabled in Authentik", pk)
    elif bound_pks or tracked:
        status = Classification(AccountStatus.orphaned, "deleted from Authentik", pk)
    else:
        return Classification(AccountStatus.untracked, "no Authentik user maps to this account")
    # A new offboarding only starts for an account that exists; a tracked one continues regardless,
    # because Synapse hides deactivated accounts from its listing.
    if not tracked and mxid not in snapshot.matrix_mxids:
        return Classification(AccountStatus.untracked, "no Matrix account", pk)
    return status


def update_known_users(
    known: KnownUsers, snapshot: DirectorySnapshot, ledger: LifecycleLedger, now: float
) -> tuple[bool, list[str]]:
    """Record new bindings and prune dead ones; return (changed, warnings).

    Only an active, in-scope, mapped user with an existing Matrix account is recorded. A binding is
    never rewritten: a changed mapping (a rename, D8) or a second Authentik user claiming an already
    bound MXID is reported and left alone (fail closed; S9 decides what to do with it).
    """
    changed = False
    warnings: list[str] = []
    owner_of = {k.mxid: pk for pk, k in known.users.items()}
    for user in snapshot.users:
        if not (user.is_active and user.in_scope and not user.ignored and user.mxid is not None):
            continue
        if user.mxid not in snapshot.matrix_mxids or user.mxid in snapshot.excluded_mxids:
            continue
        current = known.users.get(user.pk)
        if current is not None:
            if current.mxid != user.mxid:
                warnings.append(
                    f"Authentik user {user.pk} now maps to {user.mxid} but is bound to {current.mxid}; "
                    "keeping the recorded binding"
                )
            continue
        owner = owner_of.get(user.mxid)
        if owner is not None:
            warnings.append(
                f"Authentik users {owner} and {user.pk} both map to {user.mxid}; "
                f"keeping the binding to {owner}"
            )
            continue
        known.users[user.pk] = KnownUser(mxid=user.mxid, first_seen=now)
        owner_of[user.mxid] = user.pk
        changed = True

    present = {u.pk for u in snapshot.users}
    for pk, known_user in list(known.users.items()):
        mxid = known_user.mxid
        if pk not in present and mxid not in snapshot.matrix_mxids and mxid not in ledger.entries:
            del known.users[pk]  # gone from Authentik and nothing left to offboard
            changed = True
    return changed, warnings


@runtime_checkable
class LifecycleEffectors(Protocol):
    """The destructive Matrix/Synapse operations, isolated behind a seam (separate boundary, AD-5).

    Every method raises when it could not do its job (for example :class:`LifecycleTargetNotFound`);
    returning normally means the action happened.
    """

    async def logout(self, mxid: str) -> None:
        """Lock the account out (G9.2). Reversible (see :meth:`reenable`)."""

    async def reenable(self, mxid: str) -> None:
        """Undo a lock onbot set when the user returns (G9.6). May be a no-op."""

    async def erase(self, mxid: str, *, gdpr_erase: bool) -> None:
        """Deactivate the account, irreversibly (G9.4)."""

    async def delete_media(self, mxid: str) -> None:
        """Delete the media the user uploaded (G9.5)."""


class AdminApiLifecycleEffectors:
    """Concrete effectors over the Synapse Admin API.

    NOTE (BATTLE_PLAN §7 Q1, verified by the Phase 7b harness): under MAS these do **not** revoke a
    live MAS-issued session; deleting devices/deactivating in Synapse leaves the MAS token valid.
    Use :class:`MasLifecycleEffectors` for real enforcement under the MAS topology (ADR-0006). These
    remain correct for non-MAS Synapse deployments.
    """

    def __init__(self, admin: Any) -> None:
        self.admin = admin

    async def logout(self, mxid: str) -> None:
        await self.admin.logout_account(mxid)

    async def reenable(self, mxid: str) -> None:
        # Synapse logout deletes devices; there is nothing to restore, the user logs in afresh.
        return None

    async def erase(self, mxid: str, *, gdpr_erase: bool) -> None:
        await self.admin.deactivate_account(mxid, erase=gdpr_erase)

    async def delete_media(self, mxid: str) -> None:
        await self.admin.delete_user_media(mxid)


class MasLifecycleEffectors:
    """Effectors that enforce lockout through the **MAS admin API** (ADR-0006, §7 Q1).

    This is the path that actually works under MAS: ``logout`` locks the MAS user (suspending live
    sessions and blocking new logins, reversibly), ``reenable`` unlocks them when their upstream
    account returns (G9.6), and ``erase`` deactivates the account irreversibly. Media deletion goes
    through the Synapse admin API (MAS does not own media). A MAS user that cannot be resolved raises
    :class:`LifecycleTargetNotFound` (LIF-10).
    """

    def __init__(self, mas_admin: Any, synapse_admin: Any | None = None) -> None:
        self.mas = mas_admin
        self.synapse_admin = synapse_admin

    async def _resolve(self, mxid: str) -> str:
        from onbot.clients.mas_admin import mxid_localpart

        localpart = mxid_localpart(mxid)
        uid: str | None = await self.mas.get_user_id_by_username(localpart)
        if uid is None:
            raise LifecycleTargetNotFound(f"MAS has no user {localpart!r} (for {mxid})")
        return uid

    async def logout(self, mxid: str) -> None:
        await self.mas.lock_user(await self._resolve(mxid))

    async def reenable(self, mxid: str) -> None:
        await self.mas.unlock_user(await self._resolve(mxid))

    async def erase(self, mxid: str, *, gdpr_erase: bool) -> None:
        # MAS erases on every deactivation today, whatever ``gdpr_erase`` says (LIF-11, D7).
        await self.mas.deactivate_user(await self._resolve(mxid))

    async def delete_media(self, mxid: str) -> None:
        if self.synapse_admin is None:
            raise LifecycleTargetNotFound(f"no Synapse admin client to delete the media of {mxid}")
        await self.synapse_admin.delete_user_media(mxid)


@runtime_checkable
class LifecycleLedgerStore(Protocol):
    async def load(self) -> LifecycleLedger: ...

    async def save(self, ledger: LifecycleLedger) -> None: ...

    async def load_known_users(self) -> KnownUsers: ...

    async def save_known_users(self, known: KnownUsers) -> None: ...


class MatrixAccountDataLedgerStore:
    """Persists the ledger and the known users as account-data blobs on the bot user (AD-1)."""

    def __init__(self, client: Any, bot_id: str, server_name: str) -> None:
        self.client = client
        self.bot_id = bot_id
        self.data_type = lifecycle_account_data_type(server_name)
        self.known_users_type = known_users_account_data_type(server_name)

    async def load(self) -> LifecycleLedger:
        raw = await self.client.get_account_data(self.bot_id, self.data_type)
        if not raw:
            return LifecycleLedger()
        return LifecycleLedger.model_validate(raw)

    async def save(self, ledger: LifecycleLedger) -> None:
        content = ledger.model_dump(mode="json", exclude_defaults=True)
        content["schema_version"] = ledger.schema_version
        await self.client.set_account_data(self.bot_id, self.data_type, content)

    async def load_known_users(self) -> KnownUsers:
        raw = await self.client.get_account_data(self.bot_id, self.known_users_type)
        if not raw:
            return KnownUsers()
        return KnownUsers.model_validate(raw)

    async def save_known_users(self, known: KnownUsers) -> None:
        await self.client.set_account_data(self.bot_id, self.known_users_type, known.model_dump(mode="json"))


class LifecycleOutcome(BaseModel):
    """What the manager did for one user this pass (returned for tests and observability)."""

    mxid: str
    action: LifecycleAction
    dry_run: bool
    failed: bool = False
    detail: str | None = None

    @property
    def needs_attention(self) -> bool:
        """Whether an admin should hear about it: a failed step or a newly frozen user."""
        return self.failed or self.action is LifecycleAction.freeze


EMPTY_DIRECTORY_MESSAGE = (
    "lifecycle step aborted: Authentik returned no users at all; refusing to judge any account on an "
    "empty answer (check the API token's permissions)"
)


class AccountLifecycleManager:
    """Orchestrates the lifecycle pass: load ledger, classify, decide and act per user, persist.

    Invoked only by the reconciler with a :class:`DirectorySnapshot` of the current Authentik
    directory and the existing Matrix accounts.
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
        self._effector_name = type(effectors).__name__
        self._reported_warnings: set[str] = set()

    async def reconcile_accounts(self, snapshot: DirectorySnapshot) -> list[LifecycleOutcome]:
        if not self.cfg.enabled:
            return []
        if not snapshot.users:
            # The reconciler checks this before calling; kept here so no caller can get it wrong.
            log.error(EMPTY_DIRECTORY_MESSAGE)
            return []
        ledger = await self.store.load()
        known = await self.store.load_known_users()
        now = self.clock()

        known_changed, warnings = update_known_users(known, snapshot, ledger, now)
        for warning in warnings:
            if warning not in self._reported_warnings:  # once per process, not every tick
                self._reported_warnings.add(warning)
                log.warning("lifecycle identity: %s", warning)
        if known_changed:
            await self.store.save_known_users(known)

        outcomes: list[LifecycleOutcome] = []
        erased: list[str] = []
        for mxid, classification in sorted(classify_accounts(snapshot, known, ledger).items()):
            outcome = await self._reconcile_one(ledger, mxid, classification, now)
            if outcome is None:
                continue
            outcomes.append(outcome)
            if outcome.action is LifecycleAction.erase and mxid not in ledger.entries:
                erased.append(mxid)

        if erased:  # nothing left to offboard: drop the bindings, which keeps the record bounded
            for pk, known_user in list(known.users.items()):
                if known_user.mxid in erased:
                    del known.users[pk]
            await self.store.save_known_users(known)
        return outcomes

    async def _reconcile_one(
        self, ledger: LifecycleLedger, mxid: str, classification: Classification, now: float
    ) -> LifecycleOutcome | None:
        entry = ledger.entries.get(mxid)
        if (
            entry is not None
            and entry.retry_after_timestamp is not None
            and now < entry.retry_after_timestamp
        ):
            return None  # backing off after a failed step
        if (
            entry is not None
            and entry.frozen_reason is not None
            and classification.status is not AccountStatus.frozen
        ):
            audit.info("%s is no longer frozen (%s)", mxid, classification.reason)
            entry.frozen_reason = None
            await self.store.save(ledger)

        action = decide_account_action(
            status=classification.status,
            marked_ts=entry.marked_for_disabling_timestamp if entry else None,
            disabled_ts=entry.disabled_user_timestamp if entry else None,
            now=now,
            deactivate_after_n_sec=self.cfg.deactivate_after_n_sec,
            delete_after_n_sec=self.cfg.delete_after_n_sec,
        )
        if action is LifecycleAction.none or action is LifecycleAction.wait:
            return None
        if action is LifecycleAction.freeze:
            return await self._freeze(ledger, mxid, classification.reason)

        try:
            changed = await self._apply(ledger, mxid, action, now, classification)
        except Exception as exc:
            return await self._record_failure(ledger, mxid, action, now, exc)
        entry = ledger.entries.get(mxid)
        if entry is not None and entry.failures:
            entry.failures, entry.retry_after_timestamp, entry.last_error = 0, None, None
            changed = True
        if changed:
            await self.store.save(ledger)  # after every user, never once at the end (LIF-03)
        return LifecycleOutcome(mxid=mxid, action=action, dry_run=self.cfg.dry_run)

    async def _freeze(self, ledger: LifecycleLedger, mxid: str, reason: str) -> LifecycleOutcome | None:
        entry = ledger.entries[mxid]
        if entry.frozen_reason == reason:
            return None  # reported when it was frozen; stay quiet until something changes
        entry.frozen_reason = reason
        await self.store.save(ledger)
        audit.warning(
            "FROZEN %s: %s. No unlock and no further offboarding step until an admin decides (D9)",
            mxid,
            reason,
        )
        return LifecycleOutcome(
            mxid=mxid, action=LifecycleAction.freeze, dry_run=self.cfg.dry_run, detail=reason
        )

    async def _record_failure(
        self, ledger: LifecycleLedger, mxid: str, action: LifecycleAction, now: float, exc: Exception
    ) -> LifecycleOutcome:
        entry = ledger.entries.setdefault(mxid, LifecycleEntry())
        entry.failures += 1
        delay = min(_BACKOFF_BASE_SEC * 2 ** (entry.failures - 1), _BACKOFF_MAX_SEC)
        entry.retry_after_timestamp = now + delay
        error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
        entry.last_error = error
        await self.store.save(ledger)
        audit.error(
            "FAILED %s of %s via %s (attempt %d): %s; state not advanced, next attempt in %ds",
            action.value,
            mxid,
            self._effector_name,
            entry.failures,
            error,
            delay,
        )
        log.debug("lifecycle %s of %s failed", action.value, mxid, exc_info=exc)
        return LifecycleOutcome(mxid=mxid, action=action, dry_run=self.cfg.dry_run, failed=True, detail=error)

    def _erase_plan(self) -> str:
        if self.cfg.delete_after_n_sec is None:
            return "erase disabled (delete_after_n_sec is null)"
        return f"erase in {self.cfg.delete_after_n_sec}s"

    async def _apply(
        self,
        ledger: LifecycleLedger,
        mxid: str,
        action: LifecycleAction,
        now: float,
        classification: Classification,
    ) -> bool:
        """Carry out one decision; return whether the ledger changed. Dry-run calls no effector."""
        reason = classification.reason
        if action is LifecycleAction.reenable:
            return await self._reenable(ledger, mxid, reason)

        if action is LifecycleAction.mark:
            # Bookkeeping only: harmless to persist even in dry-run, and it starts the cooldown clock.
            entry = ledger.entries.setdefault(mxid, LifecycleEntry())
            entry.marked_for_disabling_timestamp = now
            if classification.pk is not None:
                entry.authentik_pk = classification.pk
            audit.info(
                "marked %s for offboarding (%s); lock in %ss", mxid, reason, self.cfg.deactivate_after_n_sec
            )
            return True

        if action is LifecycleAction.logout:
            if self.cfg.dry_run:
                audit.warning("DRY-RUN: would lock out %s (%s)", mxid, reason)
                return False
            await self.effectors.logout(mxid)
            entry = ledger.entries.setdefault(mxid, LifecycleEntry())
            entry.disabled_user_timestamp = now
            if entry.authentik_pk is None and classification.pk is not None:
                entry.authentik_pk = classification.pk
            audit.warning(
                "locked out %s via %s (%s); %s", mxid, self._effector_name, reason, self._erase_plan()
            )
            return True

        return await self._erase(ledger, mxid, now)

    async def _reenable(self, ledger: LifecycleLedger, mxid: str, reason: str) -> bool:
        entry = ledger.entries[mxid]
        if entry.deactivated_timestamp is not None:
            ledger.entries.pop(mxid)
            audit.warning(
                "%s is %s again, but onbot already deactivated the account; it cannot be re-enabled. "
                "Cleared lifecycle state",
                mxid,
                reason,
            )
            return True
        if entry.disabled_user_timestamp is None:
            # Only marked: onbot never locked it, so there is nothing to undo (LIF-06).
            ledger.entries.pop(mxid)
            audit.info("cleared pending offboarding of %s (%s); nothing was locked", mxid, reason)
            return True
        if self.cfg.dry_run:
            audit.warning("DRY-RUN: would unlock %s (%s)", mxid, reason)
            return False
        await self.effectors.reenable(mxid)
        ledger.entries.pop(mxid)
        audit.info("re-enabled %s via %s (%s); cleared lifecycle state", mxid, self._effector_name, reason)
        return True

    async def _erase(self, ledger: LifecycleLedger, mxid: str, now: float) -> bool:
        delete_media = self.cfg.include_user_media_on_delete
        if self.cfg.dry_run:
            audit.warning("DRY-RUN: would erase %s (deactivate account, delete_media=%s)", mxid, delete_media)
            return False
        entry = ledger.entries.setdefault(mxid, LifecycleEntry())
        if entry.deactivated_timestamp is None:
            await self.effectors.erase(mxid, gdpr_erase=delete_media)
            entry.deactivated_timestamp = now
            # Persist the irreversible half at once, so a failure below never repeats it (LIF-03).
            await self.store.save(ledger)
            audit.warning("deactivated %s via %s (irreversible)", mxid, self._effector_name)
        if delete_media and entry.media_deleted_timestamp is None:
            await self.effectors.delete_media(mxid)
            entry.media_deleted_timestamp = now
        ledger.entries.pop(mxid)
        audit.warning("erased %s (account deactivated, media deleted=%s)", mxid, delete_media)
        return True
