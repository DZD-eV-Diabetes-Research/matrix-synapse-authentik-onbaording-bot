"""Quarantined account lifecycle (ADR-0005, G9.*): the scariest code in the repo.

When an Authentik account is disabled or deleted, the matching Matrix account must eventually be
locked out and, optionally, erased. This is destructive and irreversible past a point, so the module
is built around these safety principles:

* **Cooldowns absorb accidents (G12.3).** Detection only *marks* a user; nothing destructive happens
  until ``lock_after_n_sec`` has elapsed, and erasure waits a further ``remove_after_n_sec``
  (counted from the lock).
* **Fail closed (review S1).** Access is only ever given back on positive evidence: the bound
  Authentik user is present, active and in the sync scope. Dropping out of a query (deletion, a lost
  attribute, an ignore-list edit, a mapping change, an empty API answer) never unlocks anybody. A
  tracked user who leaves the sync scope is *frozen* (no unlock, no further step) and reported (D9).
  The one exception (D14): a *known* user who is disabled is offboarded normally even outside the
  scope, because "disable, archive and strip the groups" is the usual way to remove somebody.
* **Dry-run + audit by default (Q6, ADR-0005).** While ``dry_run`` is on, no effector is called at all,
  not even the restorative unlock; the manager only records bookkeeping timestamps and logs an audit
  line describing what *would* happen.
* **Pure decisions, isolated effects.** :func:`classify_accounts` and :func:`decide_account_action`
  are side-effect free and exhaustively unit-tested; all I/O lives behind the
  :class:`LifecycleEffectors` and :class:`LifecycleLedgerStore` seams.
* **One user cannot stall the rest (LIF-03).** Every user is handled in isolation, a failing user
  backs off, and the ledger is saved after every user, so a crash never repeats a completed
  irreversible step.

**Two stages.** Stage one (``lock_after_n_sec``) locks the user out: under
MAS (ADR-0006) a MAS lock, which blocks new logins reversibly, plus finishing every session so an unlock
cannot revive them (LIF-07, ADR-0005). Stage two (``remove_after_n_sec``, counted from the lock)
erases the account (G9.4), GDPR-erasing it when ``gdpr_erase_on_delete`` says so (D7), and optionally
its uploaded media (G9.5).

**Who is tracked.** Disabled Authentik users in the sync scope whose Matrix account exists, plus
*known users*: every in-scope, active, mapped user is recorded once as ``authentik pk -> MXID``
(:class:`KnownUsers`). A known user who later disappears from Authentik is treated as deleted and goes
through the same cooldowns (LIF-02); a known user who is disabled is offboarded in or out of the scope
(D14). A Matrix account that was never mapped (service accounts, MAS-only
users) is never touched.

There is no database (ADR-0001): the ledger and the known-users record are blobs in the bot's Matrix
*account data*.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

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
# How long a lock record in its final state is kept (locked, deleted from Authentik, erase disabled)
# before onbot forgets it, which keeps the ledger bounded (LIF-14). The MAS lock itself stays.
LEDGER_RETENTION_SEC = 90 * 24 * 60 * 60.0


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


class LifecycleStepIncomplete(RuntimeError):
    """An effector could not do its job as configured (for example an erase this MAS cannot do
    without GDPR erasure). Audited as a failure; the state does not advance."""


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
    # The account is already deactivated: continue deleting its media (LIF-12). Not a new
    # irreversible decision, so the guard's layer 3 does not ask for it again.
    delete_media = "delete_media"
    wait = "wait"  # in a cooldown window: do nothing yet
    freeze = "freeze"  # tracked but out of scope or ambiguous: hold everything and report (D9)
    forget = "forget"  # locked, deleted upstream, erase disabled, retention over: drop the record
    abort = "abort"  # the whole step was refused (unreadable ledger); not a per-user decision
    alert = "alert"  # something an admin must look at (quarantined entry, lost ledger)


def decide_account_action(
    *,
    status: AccountStatus,
    marked_ts: float | None,
    disabled_ts: float | None,
    now: float,
    lock_after_n_sec: float,
    remove_after_n_sec: float | None,
) -> LifecycleAction:
    """Pure state machine: given a user's status and timestamps, decide the next action.

    ``marked_ts`` is when the user was first seen orphaned; ``disabled_ts`` is when onbot locked them.
    ``remove_after_n_sec=None`` disables erasure entirely (a locked user simply stays so). Only an
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
        if now - marked_ts >= lock_after_n_sec:
            return LifecycleAction.logout
        return LifecycleAction.wait
    if remove_after_n_sec is not None and now - disabled_ts >= remove_after_n_sec:
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
    # The Authentik username that put the entry on hold through ``authentik_user_ignore_list``. The
    # ignore list names users, so a deleted user can only be checked against it through this record.
    ignored_username: str | None = None
    # Backoff after a failed step (LIF-03).
    failures: int = 0
    retry_after_timestamp: float | None = None
    last_error: str | None = None


class LifecycleLedger(BaseModel):
    """Versioned blob of every tracked user's lifecycle state (stored in bot account data)."""

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, LifecycleEntry] = Field(default_factory=dict)
    # Entries that did not validate, kept verbatim until an admin repairs them (LIF-14).
    quarantined: dict[str, Any] = Field(default_factory=dict)
    # Not stored: the blob did not exist when it was loaded (see the missing-ledger check).
    missing: bool = Field(default=False, exclude=True)


class KnownUser(BaseModel):
    """One Authentik user onbot has seen active, in scope and mapped to an existing Matrix account."""

    mxid: str
    first_seen: float


class KnownUsers(BaseModel):
    """Record of ``authentik pk -> MXID`` bindings, first seen wins (stored in bot account data).

    It makes deletion detectable (LIF-02) and keeps a tracked account tied to the person it belongs
    to across mapping changes. It is bounded: a binding is dropped once the Authentik user is gone and
    nothing is left to offboard. It is also the identity binding of D8: :class:`onbot.identity.
    IdentityResolver` records first sights in it and resolves bound users through it, so a renamed
    user keeps their account. Keep the shape stable (``users[pk].mxid``) when changing it.
    """

    schema_version: int = SCHEMA_VERSION
    users: dict[str, KnownUser] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DirectoryUser:
    """One Authentik user, as the lifecycle sees it this pass."""

    pk: str
    username: str
    mxid: str | None  # as resolved (D8); None when unmappable or in a conflict (see held_mxids)
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
    ignored_usernames: frozenset[str] = frozenset()  # authentik_user_ignore_list
    # Accounts several Authentik users resolve to (D8): frozen, never locked or erased.
    held_mxids: frozenset[str] = frozenset()


IGNORED_REASON = "the Authentik user is on authentik_user_ignore_list"
CONFLICT_REASON = "several Authentik users resolve to this Matrix account"
DELETED_REASON = "deleted from Authentik"


@dataclass(frozen=True, slots=True)
class Classification:
    status: AccountStatus
    reason: str
    pk: str | None = None  # the Authentik user this account is bound to, when unambiguous
    ignored_username: str | None = None  # the ignore-listed Authentik username behind a freeze


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

    known_pks: dict[str, set[str]] = defaultdict(set)
    for pk, known_user in known.users.items():
        known_pks[known_user.mxid].add(pk)

    candidates = set(ledger.entries) | set(bound)
    candidates |= {u.mxid for u in snapshot.users if u.mxid is not None and u.in_scope and not u.is_active}
    return {
        mxid: _classify_one(
            mxid,
            snapshot,
            by_pk,
            computed.get(mxid, []),
            bound.get(mxid, set()),
            known_pks.get(mxid, set()),
            ledger,
        )
        for mxid in candidates
    }


def _classify_one(
    mxid: str,
    snapshot: DirectorySnapshot,
    by_pk: dict[str, DirectoryUser],
    computed: list[DirectoryUser],
    bound_pks: set[str],
    known_pks: set[str],
    ledger: LifecycleLedger,
) -> Classification:
    """Classify one account. ``bound_pks`` come from the known users record and the ledger,
    ``known_pks`` from the known users record alone (the D14 exception needs that stronger evidence).
    """
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
    if mxid in snapshot.held_mxids:
        return frozen(CONFLICT_REASON)
    ignored = [u for u in everyone if u.ignored]
    if ignored:
        return Classification(AccountStatus.frozen, IGNORED_REASON, pk, ignored_username=ignored[0].username)
    entry = ledger.entries.get(mxid)
    if not primary and entry is not None and entry.frozen_reason == IGNORED_REASON:
        # The ignore list names present users only, so deleting an ignored user would end the freeze
        # and resume offboarding at once (wave 1 review). Keep it while the recorded username is
        # still listed; an entry frozen before the username was recorded stays frozen (fail closed).
        name = entry.ignored_username
        if name is None or name in snapshot.ignored_usernames:
            return Classification(AccountStatus.frozen, IGNORED_REASON, pk, ignored_username=name)
    if any(u.is_active and u.in_scope for u in primary):
        return Classification(AccountStatus.active, "active in Authentik and in the sync scope", pk)
    if any(u.is_active for u in primary):
        return frozen("active in Authentik but outside the sync scope")
    if any(u.is_active for u in others) or mxid in snapshot.synced_mxids:
        return frozen("another active Authentik user maps to this Matrix account")
    if primary:
        if any(u.in_scope for u in primary):
            status = Classification(AccountStatus.orphaned, "disabled in Authentik", pk)
        elif any(u.pk in known_pks for u in primary):
            # D14: a known user (seen active, in scope and mapped earlier) who is disabled and left the
            # scope in the same go ("disable, archive, strip the groups") is offboarded normally.
            status = Classification(
                AccountStatus.orphaned, "disabled in Authentik and outside the sync scope (known user)", pk
            )
        else:
            return frozen("disabled in Authentik but outside the sync scope")
    elif bound_pks or tracked:
        status = Classification(AccountStatus.orphaned, DELETED_REASON, pk)
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
    never rewritten here: a changed mapping or a second Authentik user claiming an already bound MXID
    is reported and left alone (fail closed). The reconciler's identity resolver (D8) records first
    sights before the lifecycle runs and hands it resolved MXIDs, so in a pass this mostly prunes.
    """
    changed = False
    warnings: list[str] = []
    # A ledger entry binds its account too: a newcomer must not become a second owner of an account
    # that is being offboarded for somebody else.
    owner_of = {mxid: e.authentik_pk for mxid, e in ledger.entries.items() if e.authentik_pk is not None}
    owner_of.update({k.mxid: pk for pk, k in known.users.items()})
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
        if owner is not None and owner != user.pk:
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


@dataclass(frozen=True, slots=True)
class MediaDeletion:
    """Result of one media deletion run: how many files went, how many are left (LIF-12)."""

    deleted: int
    remaining: int

    @property
    def complete(self) -> bool:
        return self.remaining == 0


# Media deletion per pass: Synapse deletes at most one page per request, so a user with many uploads
# takes several requests; more than this many are left for the next pass (the entry resumes).
_MEDIA_BATCH = 100
_MEDIA_REQUESTS_PER_PASS = 50


async def delete_all_user_media(admin: Any, mxid: str) -> MediaDeletion:
    """Delete the user's media page by page until none is left or the per-pass budget is used up.

    Raises when Synapse stops deleting while media is left (nothing to resume, an admin has to look).
    """
    deleted = 0
    last = _MEDIA_BATCH
    for _ in range(_MEDIA_REQUESTS_PER_PASS):
        result = await admin.delete_user_media(mxid, limit=_MEDIA_BATCH)
        last = int((result or {}).get("total", 0))
        deleted += last
        if last < _MEDIA_BATCH:
            break
    remaining = int(await admin.count_user_media(mxid))
    if remaining and last == 0:
        raise LifecycleStepIncomplete(
            f"{remaining} media file(s) of {mxid} could not be deleted (Synapse deleted none of them)"
        )
    return MediaDeletion(deleted=deleted, remaining=remaining)


@runtime_checkable
class LifecycleEffectors(Protocol):
    """The destructive Matrix/Synapse operations, isolated behind a seam (separate boundary, ADR-0005).

    Every method raises when it could not do its job (for example :class:`LifecycleTargetNotFound`);
    returning normally means the action happened. The returned text says exactly what was done; it
    goes into the audit line (LIF-09), so it must not claim more than the effector achieved.
    """

    async def logout(self, mxid: str) -> str | None:
        """Lock the account out (G9.2). Reversible (see :meth:`reenable`)."""

    async def reenable(self, mxid: str) -> str | None:
        """Undo a lock onbot set when the user returns (G9.6). May be a no-op."""

    async def erase(self, mxid: str, *, gdpr_erase: bool) -> str | None:
        """Deactivate the account, irreversibly (G9.4); GDPR-erase it too when asked (D7)."""

    async def delete_media(self, mxid: str) -> MediaDeletion | None:
        """Delete the media the user uploaded (G9.5); ``None`` means everything is gone."""

    # Optional, looked up with getattr by the manager:
    #   preflight() -> list[str]: the startup check's warnings for the operator.
    #   destructive_refusal() -> str | None: why no lock or erase may run at all (LIF-09); raising
    #     means "cannot tell yet", which holds them too.
    #   count_locked_accounts() -> int: for the missing-ledger alarm (LIF-14).


def mas_without_mas_admin_message(issuer: str) -> str:
    """Why lock and erase are refused on a MAS-backed homeserver without ``mas_admin`` (LIF-09, D19)."""
    return (
        f"the homeserver delegates logins to {issuer} (MAS) but `mas_admin` is not configured: onbot "
        "refuses to lock or erase any account, because the Synapse admin API fallback would leave every "
        "MAS session valid. Offboarding stays paused (users are still marked, nobody is locked) until "
        "`mas_admin` is configured; onboarding and rooms are not affected"
    )


class AdminApiLifecycleEffectors:
    """Concrete effectors over the Synapse Admin API, for homeservers that handle logins themselves.

    NOTE (BATTLE_PLAN §7 Q1, verified by the Phase 7b harness): under MAS these do **not** revoke a
    live MAS-issued session; deleting devices/deactivating in Synapse leaves the MAS token valid.
    Use :class:`MasLifecycleEffectors` for real enforcement under the MAS topology (ADR-0006).

    So these effectors refuse to lock or erase on a homeserver that delegates logins (LIF-09, D19),
    and they fail closed: they act only after the homeserver positively answered that it handles
    logins itself. The answer is asked once (by the startup check :meth:`preflight`, or by the first
    destructive step if that comes first) and kept; a failed question is asked again next time.
    """

    def __init__(self, admin: Any, *, matrix: Any) -> None:
        self.admin = admin
        self.matrix = matrix  # client-server API client, asked for the auth issuer
        #: The OAuth 2.0 issuer the homeserver delegates logins to, once it answered with one.
        self.delegated_issuer: str | None = None
        self._delegation_known = False

    async def _detect_delegation(self) -> str | None:
        """The issuer the homeserver delegates logins to, or ``None``; raises when it cannot tell."""
        if not self._delegation_known:
            self.delegated_issuer = await self.matrix.get_auth_issuer()
            self._delegation_known = True
        return self.delegated_issuer

    async def preflight(self) -> list[str]:
        """Detect a MAS-backed homeserver; return the warnings an operator must see."""
        issuer = await self._detect_delegation()
        return [] if issuer is None else [mas_without_mas_admin_message(issuer)]

    async def destructive_refusal(self) -> str | None:
        """Why lock and erase must not run through these effectors, or ``None`` when they may.

        Raises when the homeserver cannot be asked; the caller then holds the step (fail closed).
        """
        issuer = await self._detect_delegation()
        return None if issuer is None else mas_without_mas_admin_message(issuer)

    async def _ensure_allowed(self) -> None:
        refusal = await self.destructive_refusal()
        if refusal is not None:
            raise LifecycleStepIncomplete(refusal)

    async def logout(self, mxid: str) -> str:
        await self._ensure_allowed()
        devices = await self.admin.logout_account(mxid)
        count = "all" if devices is None else str(devices)
        return f"deleted {count} Synapse device(s)"

    async def reenable(self, mxid: str) -> str:
        # Synapse logout deletes devices; there is nothing to restore, the user logs in afresh.
        return "nothing to undo in Synapse (the deleted devices stay deleted, the user logs in again)"

    async def erase(self, mxid: str, *, gdpr_erase: bool) -> str:
        await self._ensure_allowed()
        await self.admin.deactivate_account(mxid, erase=gdpr_erase)
        return f"Synapse account deactivated, GDPR erase {'yes' if gdpr_erase else 'no'}"

    async def delete_media(self, mxid: str) -> MediaDeletion:
        await self._ensure_allowed()
        return await delete_all_user_media(self.admin, mxid)


# Synapse caches MAS token introspection, so a lock or an unlock reaches tokens already in use only
# after about two minutes (LIV-03, measured 115 s on Synapse 1.156); the audit line is immediate.
_TOKEN_CACHE_NOTE = "; tokens already in use follow within about 2 minutes (Synapse token cache)"


class MasLifecycleEffectors:
    """Effectors that enforce lockout through the **MAS admin API** (ADR-0006, §7 Q1).

    This is the path that actually works under MAS. ``logout`` locks the MAS user (blocking new logins
    and suspending live sessions) and then finishes every session it has, so a later unlock does not
    bring back the session of, say, a stolen laptop (LIF-07). ``reenable`` finishes any session that
    is still active (a lock from before this behaviour) and then unlocks. ``erase`` deactivates the
    account irreversibly, with or without GDPR erasure (D7). Media deletion goes through the Synapse
    admin API (MAS does not own media). A MAS user that cannot be resolved raises
    :class:`LifecycleTargetNotFound` (LIF-10).

    What a given MAS can do is read from its admin API description (:class:`MasCapabilities`). A MAS
    that cannot finish sessions still locks; the audit line then says the sessions are only
    suspended. A MAS that cannot deactivate without erasing refuses an erase that must not erase.
    """

    def __init__(self, mas_admin: Any, synapse_admin: Any | None = None) -> None:
        self.mas = mas_admin
        self.synapse_admin = synapse_admin

    async def preflight(self) -> list[str]:
        """Read and log what this MAS can do; return the warnings an operator must see."""
        caps = await self.mas.capabilities()
        log.info("MAS admin API capabilities: %s", caps.describe())
        warnings: list[str] = []
        if caps.missing_finish:
            warnings.append(
                "this MAS cannot finish sessions through its admin API ("
                + ", ".join(caps.missing_finish)
                + "): a lock only suspends sessions, and unlocking a re-enabled user brings every "
                "pre-lock session back. Upgrade MAS to end them"
            )
        if not caps.deactivate_skip_erase:
            warnings.append(
                "this MAS always GDPR-erases on deactivation: with `gdpr_erase_on_delete: false` "
                "onbot refuses the erase stage instead of erasing more than configured"
            )
        return warnings

    async def _resolve(self, mxid: str) -> str:
        from onbot.clients.mas_admin import mxid_localpart

        localpart = mxid_localpart(mxid)
        uid: str | None = await self.mas.get_user_id_by_username(localpart)
        if uid is None:
            raise LifecycleTargetNotFound(f"MAS has no user {localpart!r} (for {mxid})")
        return uid

    async def _finish_sessions(self, uid: str) -> str:
        """Finish every active session of the user; describe what was ended and what could not be."""
        caps = await self.mas.capabilities()
        ended: list[str] = []
        for kind in sorted(caps.finishable_sessions):
            ids = await self.mas.list_active_session_ids(kind, uid)
            done = 0
            for session_id in ids:
                if await self.mas.finish_session(kind, session_id):
                    done += 1
            ended.append(f"{done} {kind.removesuffix('-sessions')}")
        text = "sessions ended: " + (", ".join(ended) if ended else "none")
        if caps.missing_finish:
            text += (
                f"; this MAS cannot end {', '.join(caps.missing_finish)}"
                + ("" if caps.known else " (capabilities unknown)")
                + ", those are only suspended and come back on unlock"
            )
        return text

    async def logout(self, mxid: str) -> str:
        uid = await self._resolve(mxid)
        await self.mas.lock_user(uid)
        try:
            sessions = await self._finish_sessions(uid)
        except Exception as exc:
            # The lock stands and is recorded; ``reenable`` finishes what is left before it unlocks.
            log.warning("locked %s but could not end its sessions yet: %s", mxid, exc)
            sessions = f"sessions suspended, ending them failed ({type(exc).__name__}); retried on unlock"
        return f"MAS user locked, {sessions}{_TOKEN_CACHE_NOTE}"

    async def reenable(self, mxid: str) -> str:
        uid = await self._resolve(mxid)
        # Before the unlock, so no session from before the lock comes back (LIF-07). A failure here
        # raises: better a retried unlock than a revived stolen session.
        sessions = await self._finish_sessions(uid)
        await self.mas.unlock_user(uid)
        return f"MAS user unlocked, {sessions}{_TOKEN_CACHE_NOTE}"

    async def erase(self, mxid: str, *, gdpr_erase: bool) -> str:
        uid = await self._resolve(mxid)
        caps = await self.mas.capabilities()
        if caps.deactivate_skip_erase:
            await self.mas.deactivate_user(uid, skip_erase=not gdpr_erase)
        elif gdpr_erase:
            await self.mas.deactivate_user(uid)  # this MAS always erases, which is what is wanted
        else:
            raise LifecycleStepIncomplete(
                "refusing to deactivate: this MAS "
                + (
                    "always GDPR-erases on deactivation"
                    if caps.known
                    else "may GDPR-erase (capabilities unknown)"
                )
                + " but gdpr_erase_on_delete is false; upgrade MAS or set gdpr_erase_on_delete: true"
            )
        return f"MAS user deactivated (all sessions ended), GDPR erase {'yes' if gdpr_erase else 'no'}"

    async def delete_media(self, mxid: str) -> MediaDeletion:
        if self.synapse_admin is None:
            raise LifecycleTargetNotFound(f"no Synapse admin client to delete the media of {mxid}")
        return await delete_all_user_media(self.synapse_admin, mxid)

    async def count_locked_accounts(self) -> int:
        """How many MAS users are locked, for the missing-ledger alarm (LIF-14)."""
        return int(await self.mas.count_locked_users())


class LedgerUnreadable(RuntimeError):
    """The ledger or known-users blob cannot be read as a whole (LIF-14); the step is refused."""


def parse_ledger(raw: Any) -> LifecycleLedger:
    """Read a stored ledger blob, quarantining entries that do not validate (LIF-14).

    A broken entry must not stall offboarding for everybody, and it must not be lost either: it moves
    to ``quarantined`` with its raw content and its account is left alone until an admin repairs or
    removes it. A blob that is broken as a whole, or written by a newer schema, raises
    :class:`LedgerUnreadable` so nothing is decided on a misread and nothing overwrites it.
    """
    if not raw:
        ledger = LifecycleLedger()
        ledger.missing = True
        return ledger
    if not isinstance(raw, dict):
        raise LedgerUnreadable("the lifecycle ledger is not a JSON object")
    version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise LedgerUnreadable(
            f"the lifecycle ledger has schema_version {version!r}, this onbot understands up to "
            f"{SCHEMA_VERSION} (written by a newer onbot?)"
        )
    entries = raw.get("entries", {})
    if not isinstance(entries, dict):
        raise LedgerUnreadable("the lifecycle ledger's `entries` is not a JSON object")
    stored_quarantine = raw.get("quarantined", {})
    quarantined: dict[str, Any] = dict(stored_quarantine) if isinstance(stored_quarantine, dict) else {}
    valid: dict[str, LifecycleEntry] = {}
    for mxid, content in entries.items():
        try:
            valid[mxid] = LifecycleEntry.model_validate(content)
        except ValidationError:
            quarantined[mxid] = content
    return LifecycleLedger(schema_version=version, entries=valid, quarantined=quarantined)


@runtime_checkable
class LifecycleLedgerStore(Protocol):
    async def load(self) -> LifecycleLedger: ...

    async def save(self, ledger: LifecycleLedger) -> None: ...

    async def load_known_users(self) -> KnownUsers: ...

    async def save_known_users(self, known: KnownUsers) -> None: ...


class MatrixAccountDataLedgerStore:
    """Persists the ledger and the known users as account-data blobs on the bot user (ADR-0001)."""

    def __init__(self, client: Any, bot_id: str, server_name: str) -> None:
        self.client = client
        self.bot_id = bot_id
        self.data_type = lifecycle_account_data_type(server_name)
        self.known_users_type = known_users_account_data_type(server_name)

    async def load(self) -> LifecycleLedger:
        return parse_ledger(await self.client.get_account_data(self.bot_id, self.data_type))

    async def save(self, ledger: LifecycleLedger) -> None:
        content = ledger.model_dump(mode="json", exclude_defaults=True)
        content["schema_version"] = ledger.schema_version
        await self.client.set_account_data(self.bot_id, self.data_type, content)

    async def load_known_users(self) -> KnownUsers:
        raw = await self.client.get_account_data(self.bot_id, self.known_users_type)
        if not raw:
            return KnownUsers()
        try:
            return KnownUsers.model_validate(raw)
        except ValidationError as exc:
            raise LedgerUnreadable(
                f"the known-users record does not validate ({exc.error_count()} error(s))"
            ) from exc

    async def save_known_users(self, known: KnownUsers) -> None:
        await self.client.set_account_data(self.bot_id, self.known_users_type, known.model_dump(mode="json"))


class LifecycleOutcome(BaseModel):
    """What the manager did for one user (or, with ``mxid=None``, the step) this pass."""

    mxid: str | None
    action: LifecycleAction
    dry_run: bool
    failed: bool = False
    detail: str | None = None

    @property
    def needs_attention(self) -> bool:
        """Whether an admin should hear about it: a failure, a new freeze, an abort or an alert."""
        return self.failed or self.action in _ATTENTION_ACTIONS


@dataclass(frozen=True, slots=True)
class PlannedAccountAction:
    """A destructive step the next :meth:`AccountLifecycleManager.reconcile_accounts` would take."""

    mxid: str
    action: LifecycleAction  # logout, erase or delete_media
    reason: str


# The steps the destructive-action guard (onbot.guard) decides on. Only taken in live mode.
GUARDED_ACTIONS = frozenset({LifecycleAction.logout, LifecycleAction.erase, LifecycleAction.delete_media})
# The steps that finish an account's offboarding: its entry is gone afterwards.
_FINAL_ACTIONS = frozenset({LifecycleAction.erase, LifecycleAction.delete_media, LifecycleAction.forget})

# Whether the guard lets one step run this pass: a lock or erase it decided on, or ``forget``,
# which drops a record because the user is gone from Authentik and so waits while the guard does not
# trust the pass's user list.
Permit = Callable[[str, LifecycleAction], bool]

_ATTENTION_ACTIONS = frozenset({LifecycleAction.freeze, LifecycleAction.abort, LifecycleAction.alert})

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
        preview: bool = False,
    ) -> None:
        self.cfg = config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
        self.store = store
        self.effectors = effectors
        self.clock = clock
        # ``reconcile-once --dry-run``: plan as configured (so the preview shows what the guard would
        # do with the locks of a live configuration), but act like a dry run.
        self.preview = preview
        self._effector_name = type(effectors).__name__
        self._reported_warnings: set[str] = set()
        # The step whose "DRY-RUN: would ..." or "HELD" line was last logged per account, so the audit
        # channel gets one line per stage transition instead of one per tick (LIF-08).
        self._noted: dict[str, str] = {}
        self._missing_ledger_checked = False
        # What the last :meth:`plan_accounts` handed to the guard, to tell a step the guard paused
        # from one that only became due after the plan (the clock moved on between the two).
        self._planned: frozenset[tuple[str, LifecycleAction]] = frozenset()

    async def preflight(self) -> list[str]:
        """Log the offboarding setup once at startup and return what an operator must know (LIF-09).

        Asks the effectors to check their side (whether the homeserver is MAS-backed although the
        Synapse fallback is in use, what this MAS can do). Network errors propagate, so the startup
        step retries.
        """
        cfg = self.cfg
        log.info(
            "offboarding: enabled=%s, dry_run=%s, effectors=%s, lock after %ss, %s, "
            "gdpr_erase_on_delete=%s, include_user_media_on_delete=%s",
            cfg.enabled,
            cfg.dry_run,
            self._effector_name,
            cfg.lock_after_n_sec,
            self._erase_plan(),
            cfg.gdpr_erase_on_delete,
            cfg.include_user_media_on_delete,
        )
        if not cfg.enabled:
            return []
        check = getattr(self.effectors, "preflight", None)
        if check is None:
            return []
        warnings: list[str] = list(await check())
        for warning in warnings:
            log.error("offboarding: %s", warning)
        # A warning the pass already reported (the refusal under MAS, LIF-09) is not reported twice.
        return [warning for warning in warnings if self._once(warning)]

    async def _destructive_refusal(self) -> tuple[str | None, bool]:
        """Why no lock or erase may run this pass, and whether that is an operator's problem.

        ``(None, False)``: they may run. ``(message, True)``: the effectors refuse them for good, for
        example the Synapse fallback on a MAS-backed homeserver (LIF-09, D19). ``(message, False)``:
        the effectors could not tell yet (the homeserver did not answer), so the steps wait as well
        (fail closed) and the next pass asks again.
        """
        check = getattr(self.effectors, "destructive_refusal", None)
        if check is None:
            return None, False
        try:
            refusal: str | None = await check()
        except Exception as exc:
            message = (
                f"could not ask the homeserver whether it delegates logins to MAS "
                f"({type(exc).__name__}: {exc}); lock and erase wait for its answer"
            )
            log.warning("offboarding: %s", message)
            return message, False
        return refusal, refusal is not None

    def _once(self, message: str) -> bool:
        """Whether ``message`` is new in this process (reports go out once, not every tick)."""
        if message in self._reported_warnings:
            return False
        self._reported_warnings.add(message)
        return True

    @property
    def dry_run(self) -> bool:
        """Whether no effector may be called: the configured dry run, or a preview."""
        return self.cfg.dry_run or self.preview

    async def plan_accounts(self, snapshot: DirectorySnapshot) -> list[PlannedAccountAction] | None:
        """The destructive steps (lock, erase) a pass over ``snapshot`` would take now; acts on nothing.

        The reconciler hands these to the destructive-action guard before the real pass, which then
        only takes the steps the guard permits. A configured dry run plans nothing: it destroys
        nothing either. A preview plans as configured. ``None`` means the step is refused this pass
        (an unreadable ledger): :meth:`reconcile_accounts` then aborts and reports it.
        """
        self._planned = frozenset()
        if not self.cfg.enabled or self.cfg.dry_run or not snapshot.users:
            return []
        refusal, _ = await self._destructive_refusal()
        if refusal is not None:
            return []  # the pass takes none of them, so the guard has nothing to weigh
        try:
            ledger = await self.store.load()
            known = await self.store.load_known_users()
        except LedgerUnreadable as exc:
            log.debug("lifecycle plan skipped: %s", exc)
            return None
        now = self.clock()
        update_known_users(known, snapshot, ledger, now)  # in memory only, as the pass will see it
        planned: list[PlannedAccountAction] = []
        for mxid, classification in sorted(classify_accounts(snapshot, known, ledger).items()):
            if mxid in ledger.quarantined:
                continue  # reconcile_accounts skips it too
            entry = ledger.entries.get(mxid)
            if (
                entry is not None
                and entry.retry_after_timestamp is not None
                and now < entry.retry_after_timestamp
            ):
                continue
            action = self._decide(entry, classification, now)
            if action in GUARDED_ACTIONS:
                planned.append(PlannedAccountAction(mxid=mxid, action=action, reason=classification.reason))
        self._planned = frozenset((p.mxid, p.action) for p in planned)
        return planned

    def _decide(
        self, entry: LifecycleEntry | None, classification: Classification, now: float
    ) -> LifecycleAction:
        action = decide_account_action(
            status=classification.status,
            marked_ts=entry.marked_for_disabling_timestamp if entry else None,
            disabled_ts=entry.disabled_user_timestamp if entry else None,
            now=now,
            lock_after_n_sec=self.cfg.lock_after_n_sec,
            remove_after_n_sec=self.cfg.remove_after_n_sec,
        )
        if action is LifecycleAction.erase and entry is not None and entry.deactivated_timestamp is not None:
            # The irreversible half ran in an earlier pass (confirmed, or below the guard's limit);
            # what is left is the media cleanup, which never needs a new confirmation.
            return LifecycleAction.delete_media
        return action

    def _note_once(self, mxid: str, what: str) -> bool:
        """True the first time ``what`` is noted for ``mxid`` in a row (one audit line per transition)."""
        if self._noted.get(mxid) == what:
            return False
        self._noted[mxid] = what
        return True

    async def reconcile_accounts(
        self, snapshot: DirectorySnapshot, *, permit: Permit | None = None
    ) -> list[LifecycleOutcome]:
        """Classify, decide and act for every tracked account.

        ``permit`` is the destructive-action guard's verdict: a lock or erase it does not permit is
        held (logged once, no state change) and decided again on the next pass. ``None`` permits all.
        """
        if not self.cfg.enabled:
            return []
        if not snapshot.users:
            # The reconciler checks this before calling; kept here so no caller can get it wrong.
            log.error(EMPTY_DIRECTORY_MESSAGE)
            return []
        try:
            ledger = await self.store.load()
            known = await self.store.load_known_users()
        except LedgerUnreadable as exc:
            message = (
                f"lifecycle step aborted: {exc}. No account is judged and nothing is written until the "
                "bot's lifecycle account data is repaired"
            )
            log.error("%s", message)
            if not self._once(message):
                return []
            return [
                LifecycleOutcome(
                    mxid=None, action=LifecycleAction.abort, dry_run=self.cfg.dry_run, detail=message
                )
            ]
        now = self.clock()
        outcomes: list[LifecycleOutcome] = []
        if ledger.missing and not self._missing_ledger_checked:
            outcomes.extend(await self._check_missing_ledger(ledger))
        for mxid in sorted(ledger.quarantined):
            message = (
                f"the lifecycle entry of {mxid} does not validate and is quarantined; onbot leaves the "
                "account alone until an admin repairs or removes it in the bot's lifecycle account data"
            )
            if self._once(message):
                audit.error("QUARANTINED %s", message)
                outcomes.append(
                    LifecycleOutcome(
                        mxid=mxid, action=LifecycleAction.alert, dry_run=self.cfg.dry_run, detail=message
                    )
                )

        known_changed, warnings = update_known_users(known, snapshot, ledger, now)
        for warning in warnings:
            if self._once(warning):
                log.warning("lifecycle identity: %s", warning)
        if known_changed:
            await self.store.save_known_users(known)

        refusal, report = await self._destructive_refusal()
        if refusal is not None and report and self._once(refusal):
            log.error("offboarding: %s", refusal)
            outcomes.append(
                LifecycleOutcome(
                    mxid=None, action=LifecycleAction.abort, dry_run=self.cfg.dry_run, detail=refusal
                )
            )

        finished: list[str] = []
        for mxid, classification in sorted(classify_accounts(snapshot, known, ledger).items()):
            if mxid in ledger.quarantined:
                continue
            outcome = await self._reconcile_one(ledger, mxid, classification, now, permit, refusal)
            if outcome is None:
                continue
            outcomes.append(outcome)
            if outcome.action in _FINAL_ACTIONS and mxid not in ledger.entries:
                finished.append(mxid)

        if finished:  # nothing left to offboard: drop the bindings, which keeps the record bounded
            for pk, known_user in list(known.users.items()):
                if known_user.mxid in finished:
                    del known.users[pk]
            await self.store.save_known_users(known)
        return outcomes

    async def _check_missing_ledger(self, ledger: LifecycleLedger) -> list[LifecycleOutcome]:
        """Alarm when the ledger is gone while locks exist (LIF-14), then write an empty one.

        Without the ledger onbot no longer knows which locks it set, so a user re-enabled in
        Authentik would stay locked. Writing the empty ledger makes a later loss detectable again.
        """
        self._missing_ledger_checked = True
        counter = getattr(self.effectors, "count_locked_accounts", None)
        locked: int | None = None
        if counter is not None:
            try:
                locked = int(await counter())
            except Exception as exc:
                log.warning("could not count locked accounts for the missing-ledger check: %s", exc)
        await self.store.save(ledger)
        ledger.missing = False
        if not locked:
            return []
        message = (
            f"the lifecycle ledger is missing (first start, or the bot's account data was lost) while "
            f"{locked} account(s) are locked in MAS. If onbot locked any of them, it no longer knows: "
            "they stay locked even when re-enabled in Authentik, and an admin must unlock them by hand"
        )
        audit.error("ALERT %s", message)
        return [
            LifecycleOutcome(
                mxid=None, action=LifecycleAction.alert, dry_run=self.cfg.dry_run, detail=message
            )
        ]

    async def _reconcile_one(
        self,
        ledger: LifecycleLedger,
        mxid: str,
        classification: Classification,
        now: float,
        permit: Permit | None = None,
        refusal: str | None = None,
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
            entry.ignored_username = None
            await self.store.save(ledger)
        if (
            entry is not None
            and entry.authentik_pk is None
            and classification.pk is not None
            and classification.status is AccountStatus.orphaned
        ):
            # An entry from before pks were recorded: bind it once to the disabled user it tracks, so
            # a later rename or lost mapping attribute cannot detach it.
            entry.authentik_pk = classification.pk
            await self.store.save(ledger)
        if entry is not None and self._retention_over(entry, classification, now):
            if permit is not None and not permit(mxid, LifecycleAction.forget):
                if self._note_once(mxid, "held:forget"):
                    audit.warning(
                        "HELD forgetting the lock record of %s: the destructive-action guard does not "
                        "trust this pass's user list, so 'deleted from Authentik' is not final yet",
                        mxid,
                    )
                return None
            return await self._forget(ledger, mxid, entry, now)

        action = self._decide(entry, classification, now)
        noted = self._noted.get(mxid)
        if noted is not None and not noted.endswith(f":{action.value}"):
            del self._noted[mxid]  # moved on to another step: its next line is news again
        if action is LifecycleAction.none or action is LifecycleAction.wait:
            return None
        if refusal is not None and action in GUARDED_ACTIONS:
            # Refused for every account (LIF-09) or not decidable yet: no state change, no backoff;
            # decided again on the next pass. The step itself was reported once, not per account.
            if self._note_once(mxid, f"refused:{action.value}"):
                audit.warning("REFUSED %s of %s (%s): %s", action.value, mxid, classification.reason, refusal)
            return None
        if (
            permit is not None
            and action in GUARDED_ACTIONS
            and not self.cfg.dry_run
            and not permit(mxid, action)
        ):
            if (mxid, action) not in self._planned:
                # Became due between the plan and now: the guard has not seen it, nobody is asked.
                if self._note_once(mxid, f"deferred:{action.value}"):
                    audit.info(
                        "DEFERRED %s of %s (%s): it became due after this pass was planned; the "
                        "destructive-action guard decides on it in the next pass",
                        action.value,
                        mxid,
                        classification.reason,
                    )
            elif self._note_once(mxid, f"held:{action.value}"):
                audit.warning(
                    "HELD %s of %s (%s): paused by the destructive-action guard until an admin confirms",
                    action.value,
                    mxid,
                    classification.reason,
                )
            return None
        if action is LifecycleAction.freeze:
            return await self._freeze(ledger, mxid, classification)

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
        return LifecycleOutcome(mxid=mxid, action=action, dry_run=self.dry_run)

    def _retention_over(self, entry: LifecycleEntry, classification: Classification, now: float) -> bool:
        """Whether a lock record has reached its final state and outlived the retention (LIF-14).

        Final means: onbot locked the account, erasure is disabled (so no further step can come), and
        the Authentik user is gone (so no re-enable can come either). Every other entry may still be
        needed and is kept, however old.
        """
        return (
            self.cfg.remove_after_n_sec is None
            and entry.disabled_user_timestamp is not None
            and entry.deactivated_timestamp is None
            and entry.frozen_reason is None
            and classification.status is AccountStatus.orphaned
            and classification.reason == DELETED_REASON
            and now - entry.disabled_user_timestamp >= LEDGER_RETENTION_SEC
        )

    async def _forget(
        self, ledger: LifecycleLedger, mxid: str, entry: LifecycleEntry, now: float
    ) -> LifecycleOutcome:
        assert entry.disabled_user_timestamp is not None
        days = int((now - entry.disabled_user_timestamp) // 86400)
        ledger.entries.pop(mxid)
        await self.store.save(ledger)
        audit.info(
            "forgot %s: locked %d days ago, deleted from Authentik, erase disabled; the account stays "
            "locked and onbot no longer tracks it",
            mxid,
            days,
        )
        return LifecycleOutcome(mxid=mxid, action=LifecycleAction.forget, dry_run=self.cfg.dry_run)

    async def _freeze(
        self, ledger: LifecycleLedger, mxid: str, classification: Classification
    ) -> LifecycleOutcome | None:
        entry = ledger.entries[mxid]
        reason = classification.reason
        username = classification.ignored_username
        if entry.frozen_reason == reason:
            if username is not None and entry.ignored_username != username:
                entry.ignored_username = username  # an entry frozen before the name was recorded
                await self.store.save(ledger)
            return None  # reported when it was frozen; stay quiet until something changes
        entry.frozen_reason = reason
        entry.ignored_username = username
        await self.store.save(ledger)
        audit.warning(
            "FROZEN %s: %s. No unlock and no further offboarding step until an admin decides (D9)",
            mxid,
            reason,
        )
        return LifecycleOutcome(mxid=mxid, action=LifecycleAction.freeze, dry_run=self.dry_run, detail=reason)

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
        return LifecycleOutcome(mxid=mxid, action=action, dry_run=self.dry_run, failed=True, detail=error)

    def _erase_plan(self) -> str:
        if self.cfg.remove_after_n_sec is None:
            return "erase disabled (remove_after_n_sec is null)"
        return f"erase in {self.cfg.remove_after_n_sec}s"

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
            audit.info("marked %s for offboarding (%s); lock in %ss", mxid, reason, self.cfg.lock_after_n_sec)
            return True

        if action is LifecycleAction.logout:
            if self.dry_run:
                if self._note_once(mxid, f"dry-run:{action.value}"):
                    audit.warning("DRY-RUN: would lock out %s (%s)", mxid, reason)
                return False
            detail = await self.effectors.logout(mxid)
            entry = ledger.entries.setdefault(mxid, LifecycleEntry())
            entry.disabled_user_timestamp = now
            if entry.authentik_pk is None and classification.pk is not None:
                entry.authentik_pk = classification.pk
            audit.warning(
                "locked out %s via %s (%s): %s; %s",
                mxid,
                self._effector_name,
                reason,
                detail or "done",
                self._erase_plan(),
            )
            return True

        # erase, or delete_media: ``_erase`` resumes at whichever half is left.
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
        if self.dry_run:
            if self._note_once(mxid, f"dry-run:{LifecycleAction.reenable.value}"):
                audit.warning("DRY-RUN: would unlock %s (%s)", mxid, reason)
            return False
        detail = await self.effectors.reenable(mxid)
        ledger.entries.pop(mxid)
        audit.info(
            "re-enabled %s via %s (%s): %s; cleared lifecycle state",
            mxid,
            self._effector_name,
            reason,
            detail or "done",
        )
        return True

    async def _erase(self, ledger: LifecycleLedger, mxid: str, now: float) -> bool:
        gdpr_erase = self.cfg.gdpr_erase_on_delete
        delete_media = self.cfg.include_user_media_on_delete
        if self.dry_run:
            if self._note_once(mxid, f"dry-run:{LifecycleAction.erase.value}"):
                audit.warning(
                    "DRY-RUN: would erase %s (deactivate account, GDPR erase=%s, delete media=%s)",
                    mxid,
                    gdpr_erase,
                    delete_media,
                )
            return False
        entry = ledger.entries.setdefault(mxid, LifecycleEntry())
        if entry.deactivated_timestamp is None:
            detail = await self.effectors.erase(mxid, gdpr_erase=gdpr_erase)
            entry.deactivated_timestamp = now
            # Persist the irreversible half at once, so a failure below never repeats it (LIF-03).
            await self.store.save(ledger)
            audit.warning(
                "deactivated %s via %s (irreversible): %s", mxid, self._effector_name, detail or "done"
            )
        if delete_media and entry.media_deleted_timestamp is None:
            result = await self.effectors.delete_media(mxid)
            if result is not None and not result.complete:
                # Synapse deletes one page per request; the rest follows on the next pass (LIF-12).
                audit.warning(
                    "deleting the media of %s: %d file(s) deleted this pass, %d left; continues next pass",
                    mxid,
                    result.deleted,
                    result.remaining,
                )
                return True
            entry.media_deleted_timestamp = now
        ledger.entries.pop(mxid)
        audit.warning(
            "erased %s (account deactivated, GDPR erase=%s, media deleted=%s)", mxid, gdpr_erase, delete_media
        )
        return True
