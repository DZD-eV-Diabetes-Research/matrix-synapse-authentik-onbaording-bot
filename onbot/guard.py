"""Guard against mass destruction: input plausibility and per-pass limits (D4 layers 1 to 3, D13).

A reconcile pass computes everything it would destroy (kicks, power-level withdrawals, room
teardowns and deletions, account locks, erasures) *before* it destroys anything, as a list of
:class:`DestructiveAction`. This module decides which of them may run now:

* **Layer 1, input plausibility.** The pass's input counts (active users in the sync scope, Authentik
  groups mapped to rooms) are compared with the last known good counts. An empty list, or a drop of
  more than ``max_drop_percent``, pauses *every* destructive action of the pass. A narrowed API token
  that answers ``200`` with one user (REC-01, live check 3) looks exactly like this. The baseline moves
  with every pass that does not trip, so gradual removals never trip it.
* **Layer 2, per-pass limit.** More than ``max_reversible_actions_per_pass`` reversible actions (kick,
  lock, room teardown, power-level withdrawal) in one pass pauses those actions. This also catches a
  long dry run being switched off (LIF-08) and a broken group mapping that keeps the counts intact.
* **Layer 3, irreversible actions.** ``confirm_irreversible_actions_from`` or more irreversible actions
  (account erasure, room deletion, D19) in one pass pause those actions; fewer run on their own. The
  media cleanup that continues an erasure over later passes (LIF-12) is not a new irreversible
  decision: the erasure that started it already ran, so it is only held by layer 1.

A room deletion never runs in a pass whose teardown of the same room is paused: the teardown (block,
kick) goes first, and a deletion without it would remove the members irreversibly while the
reversible part is still waiting for an admin.

A tripped guard never just blocks (D13). The paused actions become a :class:`GuardBatch` with a short
id, reported to the admin room; an allowlisted admin releases exactly that batch with ``!confirm <id>``
or closes it with ``!reject <id>``. Released actions run on the next pass, and only those that still
apply then: a confirmation never covers more than the admin saw. A batch expires after
``confirmation_expiry_sec``; if its actions still apply, a later pass proposes them again. A
confirmed batch has the same time, counted from its confirmation, to run (a room deletion can wait
for the teardown of its room); one that has not run by then is closed and reported, and its actions
are judged again. A rejected batch holds its actions until the condition that created it is gone (the
input looks plausible again, or the pass no longer exceeds the limit with them), at the latest until
it expires; then they are judged again like any others. Its irreversible actions (erasures, room
deletions) stay held until it expires, whatever the pass looks like: that fewer of them remain than
``confirm_irreversible_actions_from`` is no reason to carry out what an admin rejected.

The guard fails closed on its own memory: a stored state it cannot read holds every destructive action
of the pass, is reported once in the admin room and is never overwritten. An allowlisted admin resets
it with ``!guard reset``, which keeps a verbatim copy of the unreadable blob (see
:meth:`MatrixAccountDataGuardStore.reset_unreadable`).

The pure part (:func:`input_drops`, :func:`decide`, :func:`record`) is side-effect free; the
:class:`DestructiveActionGuard` service persists the state in the bot's account data (AD-1) and emits
the reports on the event bus.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from onbot.config import OnbotConfig
from onbot.events import EventBus, Signal
from onbot.logging import get_logger
from onbot.reconciler.state import SCHEMA_VERSION, event_type_name

log = get_logger(__name__)

# Batch ids: 6 characters, no look-alikes (no 0/O, 1/l/I, and no i or o either).
BATCH_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
BATCH_ID_LENGTH = 6
# How many actions a report lists before it summarises the rest.
MAX_LISTED_ACTIONS = 25


def guard_account_data_type(server_name: str) -> str:
    """Account-data type holding the guard state, e.g. ``org.company.onbot.guard``."""
    return event_type_name(server_name, "guard")


def new_batch_id() -> str:
    return "".join(secrets.choice(BATCH_ID_ALPHABET) for _ in range(BATCH_ID_LENGTH))


def normalize_batch_id(text: str) -> str:
    return text.strip().lower()


class ActionKind(StrEnum):
    kick = "kick"
    # Lowering a level, whether the entry goes or a still-desired user gets a lower one (D10).
    withdraw_power_level = "withdraw_power_level"
    # Block an obsolete room and kick its members; reversible. Deleting it is ``room_delete``.
    room_teardown = "room_teardown"
    # Close a visitor lobby whose feature was switched off and detach it from the space; reversible
    # (D19, REC-10). Nobody is kicked.
    room_close = "room_close"
    lock = "lock"
    # Deactivate the account (and GDPR-erase it, delete its media, as configured); irreversible.
    erase = "erase"
    # Delete an obsolete room (``delete_disabled_rooms``); irreversible (D19).
    room_delete = "room_delete"
    # Continue deleting the media of an account an earlier pass already erased (LIF-12).
    delete_media = "delete_media"


# D13 layer 2: the reversible destructive actions.
REVERSIBLE_KINDS = frozenset(
    {
        ActionKind.kick,
        ActionKind.withdraw_power_level,
        ActionKind.room_teardown,
        ActionKind.room_close,
        ActionKind.lock,
    }
)
# D13 layer 3: the irreversible decisions. ``delete_media`` is neither: it finishes an erasure that
# already ran (confirmed, or below the limit), so it never asks again; layer 1 still holds it.
IRREVERSIBLE_KINDS = frozenset({ActionKind.erase, ActionKind.room_delete})

_KIND_PLURAL = {
    ActionKind.kick: "kick {n} room member(s)",
    ActionKind.withdraw_power_level: "lower or withdraw {n} power level(s)",
    ActionKind.room_teardown: "tear down {n} room(s)",
    ActionKind.room_close: "close {n} switched-off visitor lobby room(s)",
    ActionKind.lock: "lock {n} account(s)",
    ActionKind.erase: "erase {n} account(s) (irreversible)",
    ActionKind.room_delete: "delete {n} room(s) (irreversible)",
    ActionKind.delete_media: "continue deleting the media of {n} erased account(s)",
}


class DestructiveAction(BaseModel):
    """One thing a pass would destroy. Its :attr:`key` identifies it across passes."""

    model_config = {"frozen": True}

    kind: ActionKind
    target: str  # the MXID, or the room id for a room teardown or deletion
    room_id: str | None = None
    group_pk: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind.value}|{self.room_id or ''}|{self.target}"

    @property
    def reversible(self) -> bool:
        return self.kind in REVERSIBLE_KINDS

    @property
    def irreversible(self) -> bool:
        """Whether this is an irreversible decision (layer 3)."""
        return self.kind in IRREVERSIBLE_KINDS

    @property
    def prerequisite(self) -> str | None:
        """The key of the action that must run in the same pass before this one may run.

        A room is deleted only in a pass that also tears it down (blocks it, kicks its members).
        """
        if self.kind is ActionKind.room_delete:
            return DestructiveAction(
                kind=ActionKind.room_teardown, target=self.target, room_id=self.room_id
            ).key
        return None

    def describe(self) -> str:
        group = f" (group {self.group_pk})" if self.group_pk else ""
        if self.kind is ActionKind.kick:
            return f"kick {self.target} from {self.room_id}{group}"
        if self.kind is ActionKind.withdraw_power_level:
            return f"lower or withdraw the power level of {self.target} in {self.room_id}{group}"
        if self.kind is ActionKind.room_teardown:
            return f"tear down room {self.target}{group}"
        if self.kind is ActionKind.room_close:
            return f"close the visitor lobby {self.target}{group} (switched off) and detach it from the space"
        if self.kind is ActionKind.room_delete:
            return f"delete room {self.target}{group} (irreversible)"
        if self.kind is ActionKind.lock:
            return f"lock account {self.target}"
        if self.kind is ActionKind.delete_media:
            return f"continue deleting the media of the erased account {self.target}"
        return f"erase account {self.target} (irreversible)"


class InputCounts(BaseModel):
    """What one pass saw; ``None`` where the pass did not measure it (a switched-off feature)."""

    users_in_scope: int | None = None
    mapped_groups: int | None = None


_METRICS = (
    ("users_in_scope", "active users in the sync scope"),
    ("mapped_groups", "Authentik groups mapped to rooms"),
)


class BatchStatus(StrEnum):
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"


class GuardBatch(BaseModel):
    """Paused destructive actions waiting for an admin (D13)."""

    id: str
    status: BatchStatus = BatchStatus.pending
    created_at: float
    expires_at: float
    reasons: list[str] = Field(default_factory=list)
    # Layer 1: the counts the admin is shown; they become the baseline once the batch is executed.
    observed: InputCounts | None = None
    actions: list[DestructiveAction] = Field(default_factory=list)
    decided_by: str | None = None
    decided_at: float | None = None
    # What created the batch: the layer-1 metrics that dropped, whether layer 2's limit was exceeded,
    # and whether layer 3 asked for a confirmation. A rejected batch ends once none of them holds.
    metrics: list[str] = Field(default_factory=list)
    over_limit: bool = False
    irreversible_over_limit: bool = False

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(a.key for a in self.actions)


class GuardState(BaseModel):
    """Versioned blob of the guard's memory (stored in bot account data)."""

    schema_version: int = SCHEMA_VERSION
    baseline: InputCounts = Field(default_factory=InputCounts)
    batches: dict[str, GuardBatch] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GuardLimits:
    max_drop_percent: float = 20.0
    max_reversible_actions_per_pass: int = 10
    confirmation_expiry_sec: float = 72 * 60 * 60.0
    confirm_irreversible_actions_from: int = 3

    @classmethod
    def from_config(cls, config: OnbotConfig) -> GuardLimits:
        cfg = config.destructive_action_guard
        return cls(
            max_drop_percent=cfg.max_drop_percent,
            max_reversible_actions_per_pass=cfg.max_reversible_actions_per_pass,
            confirmation_expiry_sec=float(cfg.confirmation_expiry_sec),
            confirm_irreversible_actions_from=cfg.confirm_irreversible_actions_from,
        )


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """What a pass may do, decided before it does anything destructive."""

    counts: InputCounts
    permitted: frozenset[str]  # keys of the planned actions that may run now
    held: tuple[DestructiveAction, ...]  # planned actions paused this pass
    reasons: tuple[str, ...]  # why they are paused
    input_tripped: bool  # layer 1 tripped on at least one metric
    confirmed_batches: tuple[str, ...]  # confirmed batches whose actions run in this pass
    decided_at: float = 0.0
    tripped_metrics: frozenset[str] = frozenset()  # their baseline must not move
    over_limit: bool = False  # layer 2 paused the reversible actions of this pass
    irreversible_over_limit: bool = False  # layer 3 paused the irreversible actions of this pass
    # Actions held only because their prerequisite is held (a room deletion whose teardown waits).
    # A confirmed batch that holds one of them stays open until it can run.
    after_prerequisite: frozenset[str] = frozenset()
    # Rejected batches that end with this pass (id, why); their actions were judged afresh.
    ended_rejections: tuple[tuple[str, str], ...] = ()
    # Rejected batches whose condition is gone but whose irreversible actions stay held until they
    # expire (id, why): the other actions were judged afresh.
    narrowed_rejections: tuple[tuple[str, str], ...] = ()
    # Confirmed batches that expired before they could run; their actions were judged afresh.
    expired_confirmations: tuple[str, ...] = ()
    # The stored guard state could not be read: everything is held and nothing is recorded.
    state_unreadable: bool = False

    def permits(self, action: DestructiveAction) -> bool:
        return action.key in self.permitted

    @property
    def input_implausible(self) -> bool:
        """Whether the pass's user list is not trusted (layer 1 on users, or no readable state).

        Offboarding bookkeeping that reads "gone from Authentik" as final (forgetting a lock record)
        waits as long as this holds.
        """
        return self.state_unreadable or "users_in_scope" in self.tripped_metrics


@dataclass(frozen=True, slots=True)
class GuardNotice:
    """Something the admin room should hear about."""

    signal: Signal
    payload: dict[str, Any] = field(default_factory=dict)


# --- pure logic ---------------------------------------------------------------------------------


def input_drops(baseline: InputCounts, counts: InputCounts, max_drop_percent: float) -> list[str]:
    """Layer 1: why ``counts`` look implausible against ``baseline`` (empty list: they do not)."""
    return list(input_drops_by_metric(baseline, counts, max_drop_percent).values())


def input_drops_by_metric(
    baseline: InputCounts, counts: InputCounts, max_drop_percent: float
) -> dict[str, str]:
    """:func:`input_drops`, keyed by the metric that dropped."""
    reasons: dict[str, str] = {}
    for metric, label in _METRICS:
        new = getattr(counts, metric)
        if new is None:
            continue  # not measured this pass
        old = getattr(baseline, metric)
        if new == 0:
            # D13: an empty list always needs confirmation, unless an admin already accepted zero.
            if old is None or old > 0:
                known = f" (last known good: {old})" if old else ""
                reasons[metric] = f"Authentik returned no {label}{known}"
            continue
        if old and new < old:
            drop = (old - new) * 100.0 / old
            if drop > max_drop_percent:
                reasons[metric] = (
                    f"{label} dropped from {old} to {new} (-{drop:.0f}%), more than the "
                    f"{max_drop_percent:g}% allowed"
                )
    return reasons


def _batch_conditions(batch: GuardBatch) -> tuple[frozenset[str], bool, bool]:
    """The layer-1 metrics, the layer-2 flag and the layer-3 flag that created ``batch``.

    Batches stored before these were recorded carry none: a layer-1 batch (one with ``observed``
    counts) then counts every metric it observed, any other batch counts as a layer-2 batch.
    """
    if batch.metrics or batch.over_limit or batch.irreversible_over_limit:
        return frozenset(batch.metrics), batch.over_limit, batch.irreversible_over_limit
    if batch.observed is not None:
        return frozenset(m for m, _ in _METRICS if getattr(batch.observed, m) is not None), False, False
    return frozenset(), True, False


@dataclass(frozen=True, slots=True)
class _PassShape:
    """What :func:`_rejection_end` weighs a rejected batch against."""

    planned_keys: frozenset[str]
    reversible_keys: frozenset[str]
    irreversible_keys: frozenset[str]
    # Planned actions of each kind that no batch covers: they count towards the limits as well.
    fresh_reversible: int
    fresh_irreversible: int


def _rejection_end(
    batch: GuardBatch,
    *,
    shape: _PassShape,
    counts: InputCounts,
    drops: dict[str, str],
    limits: GuardLimits,
    now: float,
) -> str | None:
    """Why the rejected ``batch`` ends with this pass, or ``None`` while it still holds (D13).

    It ends when it expires, or when the condition that created it is gone: none of its layer-1
    metrics dropped in this pass (a metric this pass did not measure still counts as dropped), for a
    layer-2 batch this pass no longer exceeds the limit with its actions, and for a layer-3 batch this
    pass no longer reaches the limit with its irreversible actions. A batch none of whose actions this
    pass planned is left to :func:`record`, which only ends it after a complete pass.
    """
    if now >= batch.expires_at:
        return "it expired"
    if not batch.keys & shape.planned_keys:
        return None
    metrics, over_limit, irreversible_over_limit = _batch_conditions(batch)
    if any(m in drops or getattr(counts, m) is None for m in metrics):
        return None
    if over_limit:
        mine = len(batch.keys & shape.planned_keys & shape.reversible_keys)
        if mine and shape.fresh_reversible + mine > limits.max_reversible_actions_per_pass:
            return None
    if irreversible_over_limit:
        mine = len(batch.keys & shape.planned_keys & shape.irreversible_keys)
        if mine and shape.fresh_irreversible + mine >= limits.confirm_irreversible_actions_from:
            return None
    return "the condition that created it is gone"


def decide(
    state: GuardState,
    counts: InputCounts,
    planned: Sequence[DestructiveAction],
    limits: GuardLimits,
    *,
    now: float,
) -> GuardDecision:
    """Decide which planned actions may run this pass (pure).

    * Actions of a confirmed batch run if they are still planned (the re-check of D13), until the
      batch expires; then they are judged afresh.
    * Actions of a pending batch stay paused until it expires: only confirming *that* batch releases
      them, never a later pass that happens to look normal.
    * Actions of a rejected batch stay paused while the condition that created the batch lasts (see
      :func:`_rejection_end`). Once it ends they are judged afresh, like everything else, except its
      irreversible actions: they stay paused until the batch expires.
    * Everything else passes layer 1 (implausible input pauses all of it), layer 2 (too many
      reversible actions pauses those) and layer 3 (from a small number on, irreversible actions
      pause).
    * Last, an action whose prerequisite is planned but does not run this pass waits as well (a room
      deletion whose teardown is paused), even when a confirmed batch released it.
    """
    unique = list({a.key: a for a in planned}.values())
    planned_keys = frozenset(a.key for a in unique)
    reversible_keys = frozenset(a.key for a in unique if a.reversible)
    irreversible_keys = frozenset(a.key for a in unique if a.irreversible)
    drops = input_drops_by_metric(state.baseline, counts, limits.max_drop_percent)

    confirmed: dict[str, str] = {}
    waiting: dict[str, str] = {}
    rejected: list[GuardBatch] = []
    expired_confirmations: list[str] = []
    for batch in state.batches.values():
        if batch.status is BatchStatus.confirmed:
            if now >= batch.expires_at:
                # Not run within its time (a deletion whose teardown kept waiting): judged afresh.
                expired_confirmations.append(batch.id)
            else:
                confirmed.update(dict.fromkeys(batch.keys, batch.id))
        elif batch.status is BatchStatus.rejected:
            rejected.append(batch)
        elif now < batch.expires_at:
            waiting.update(dict.fromkeys(batch.keys, batch.id))
    in_rejected = {key for batch in rejected for key in batch.keys}
    covered = confirmed.keys() | waiting.keys() | in_rejected
    shape = _PassShape(
        planned_keys=planned_keys,
        reversible_keys=reversible_keys,
        irreversible_keys=irreversible_keys,
        fresh_reversible=len(reversible_keys - covered),
        fresh_irreversible=len(irreversible_keys - covered),
    )
    ended: list[tuple[str, str]] = []
    narrowed: list[tuple[str, str]] = []
    for batch in rejected:
        why = _rejection_end(
            batch,
            shape=shape,
            counts=counts,
            drops=drops,
            limits=limits,
            now=now,
        )
        irreversible_held = {a.key for a in batch.actions if a.irreversible}
        if why is None:
            for key in batch.keys:
                waiting.setdefault(key, batch.id)
        elif now < batch.expires_at and irreversible_held:
            # An admin rejected these erasures or room deletions: they wait for the expiry, even when
            # fewer of them remain than the limit (layer 3 would let one or two run on their own).
            for key in irreversible_held:
                waiting.setdefault(key, batch.id)
            if batch.keys - irreversible_held:
                narrowed.append((batch.id, why))
        else:
            ended.append((batch.id, why))

    permitted = {a.key for a in unique if a.key in confirmed}
    held = [a for a in unique if a.key not in confirmed and a.key in waiting]
    rest = [a for a in unique if a.key not in confirmed and a.key not in waiting]
    reasons: list[str] = []
    if held:
        reasons.append("waiting for open batch(es) " + ", ".join(sorted({waiting[a.key] for a in held})))

    reasons += drops.values()
    if "users_in_scope" in drops:
        # Fewer users: every removal of the pass is judged on that answer.
        held += rest
        rest = []
    elif "mapped_groups" in drops:
        # Fewer groups: only what happens in rooms depends on the group list. Locks and erasures are
        # judged on the users alone, so a deployment with no group rooms is not stuck.
        held += [a for a in rest if a.room_id is not None]
        rest = [a for a in rest if a.room_id is None]
    over_limit = False
    irreversible_over_limit = False
    if rest:
        reversible = [a for a in rest if a.reversible]
        if len(reversible) > limits.max_reversible_actions_per_pass:
            over_limit = True
            held += reversible
            reasons.append(
                f"{len(reversible)} reversible destructive actions in one pass, more than the "
                f"{limits.max_reversible_actions_per_pass} allowed"
            )
        irreversible = [a for a in rest if a.irreversible]
        if len(irreversible) >= limits.confirm_irreversible_actions_from:
            irreversible_over_limit = True
            held += irreversible
            reasons.append(
                f"{len(irreversible)} irreversible actions in one pass ({summarize_actions(irreversible)}); "
                f"from {limits.confirm_irreversible_actions_from} on they need a confirmation"
            )
    held_keys = {a.key for a in held}
    permitted |= {a.key for a in rest if a.key not in held_keys}

    # A room is deleted only in a pass that also tears it down: a deletion on its own would remove
    # the members irreversibly while the reversible teardown still waits for an admin.
    blocked = [
        a
        for a in unique
        if a.key in permitted
        and a.prerequisite is not None
        and a.prerequisite in planned_keys
        and a.prerequisite not in permitted
    ]
    if blocked:
        permitted -= {a.key for a in blocked}
        held += [a for a in blocked if a.key not in held_keys]
        reasons.append(f"{len(blocked)} room deletion(s) wait until the teardown of the same room may run")
    confirmed_batches = tuple(sorted({confirmed[k] for k in permitted if k in confirmed}))
    return GuardDecision(
        counts=counts,
        permitted=frozenset(permitted),
        held=tuple(held),
        reasons=tuple(reasons),
        input_tripped=bool(drops),
        confirmed_batches=confirmed_batches,
        decided_at=now,
        tripped_metrics=frozenset(drops),
        over_limit=over_limit,
        irreversible_over_limit=irreversible_over_limit,
        ended_rejections=tuple(ended),
        narrowed_rejections=tuple(narrowed),
        expired_confirmations=tuple(sorted(expired_confirmations)),
        after_prerequisite=frozenset(a.key for a in blocked),
    )


def hold_everything(
    counts: InputCounts, planned: Sequence[DestructiveAction], reason: str, *, now: float
) -> GuardDecision:
    """The decision when the guard cannot read its own state: nothing destructive runs."""
    held = tuple({a.key: a for a in planned}.values())
    return GuardDecision(
        counts=counts,
        permitted=frozenset(),
        held=held,
        reasons=(reason,),
        input_tripped=False,
        confirmed_batches=(),
        decided_at=now,
        state_unreadable=True,
    )


def _merge_counts(
    baseline: InputCounts, counts: InputCounts, *, keep: frozenset[str] = frozenset()
) -> InputCounts:
    """``counts`` where measured and not in ``keep``, the old baseline elsewhere."""
    return InputCounts(
        **{
            metric: getattr(counts, metric)
            if getattr(counts, metric) is not None and metric not in keep
            else getattr(baseline, metric)
            for metric, _ in _METRICS
        }
    )


def summarize_actions(actions: Iterable[DestructiveAction]) -> str:
    """``kick 3 room member(s), lock 2 account(s)``."""
    counts: dict[ActionKind, int] = {}
    for action in actions:
        counts[action.kind] = counts.get(action.kind, 0) + 1
    return ", ".join(_KIND_PLURAL[kind].format(n=n) for kind, n in counts.items()) or "nothing"


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _rejection_hold_text(batch: GuardBatch) -> str:
    """How long a rejected batch holds its actions."""
    until = _utc(batch.expires_at)
    irreversible = any(a.irreversible for a in batch.actions)
    reversible = any(not a.irreversible for a in batch.actions)
    if irreversible and reversible:
        return (
            "holds its irreversible actions until "
            f"{until} and the others until the condition that created it is gone, at the latest until then"
        )
    if irreversible:
        return f"holds its irreversible actions until {until}"
    return f"holds its actions until the condition that created it is gone, at the latest until {until}"


def _batch_status_text(batch: GuardBatch) -> str:
    if batch.status is BatchStatus.rejected:
        return f"rejected by {batch.decided_by}; {_rejection_hold_text(batch)}"
    if batch.status is BatchStatus.confirmed:
        # One whose room deletions wait for a paused teardown of the same room stays listed until
        # they can run, at the latest until it expires.
        return (
            f"confirmed by {batch.decided_by}; runs on the next pass that may carry it out, at the "
            f"latest until {_utc(batch.expires_at)}, then it is closed and its actions are judged again"
        )
    return f"{batch.status.value}, expires {_utc(batch.expires_at)}"


def describe_batch(batch: GuardBatch, *, limit: int = MAX_LISTED_ACTIONS) -> str:
    """The admin-room text for a batch: why, what, and how to release it."""
    lines = [f"Batch {batch.id} ({_batch_status_text(batch)}): {summarize_actions(batch.actions)}."]
    lines += [f"Why: {reason}." for reason in batch.reasons]
    irreversible = sum(1 for a in batch.actions if a.irreversible)
    if irreversible:
        lines.append(
            f"Irreversible: {irreversible} of these action(s) cannot be undone. An erased account stays "
            "deactivated for good, a deleted room is purged with its history."
        )
    actions = sorted(batch.actions, key=lambda a: (a.kind.value, a.room_id or "", a.target))
    lines += [f"- {a.describe()}" for a in actions[:limit]]
    if len(actions) > limit:
        lines.append(f"- and {len(actions) - limit} more")
    return "\n".join(lines)


def record(
    state: GuardState,
    decision: GuardDecision,
    planned: Sequence[DestructiveAction],
    limits: GuardLimits,
    *,
    now: float,
    complete: bool,
    id_factory: Callable[[], str] = new_batch_id,
) -> list[GuardNotice]:
    """Fold one pass's outcome into ``state`` (mutated) and return what the admin room should hear.

    ``complete`` is false when part of the pass failed: then nothing that was simply not planned is
    read as "no longer applies", and the baseline does not move.
    """
    notices: list[GuardNotice] = []
    planned_keys = {a.key for a in planned}

    # Confirmed batches that were confirmed when this pass decided are done: their actions ran, or no
    # longer apply. One confirmed later waits for the next pass (the confirming admin triggers one).
    baseline_moved_by_confirmation = False
    for batch_id in list(state.batches):
        batch = state.batches[batch_id]
        if batch.status is not BatchStatus.confirmed:
            continue
        if batch.decided_at is None or batch.decided_at > decision.decided_at:
            continue
        if batch_id in decision.expired_confirmations:
            del state.batches[batch_id]
            text = (
                f"Confirmed batch {batch.id} ({summarize_actions(batch.actions)}, confirmed by "
                f"{batch.decided_by}) expired before it could run, for example a room deletion that "
                "kept waiting for the teardown of its room. Its actions that still apply are judged "
                "again like any others: what still needs a confirmation is proposed again under a new id."
            )
            notices.append(
                GuardNotice(
                    Signal.guard_batch_closed,
                    {"batch_id": batch.id, "outcome": "confirmation_expired", "text": text},
                )
            )
            continue
        if not complete and batch_id not in decision.confirmed_batches:
            continue  # a failed pass may just not have seen its actions; try again
        if batch.observed is not None:
            # D13: after a confirmed drop, the counts the admin saw become the last known good.
            state.baseline = _merge_counts(state.baseline, batch.observed)
            baseline_moved_by_confirmation = True
        ran = batch.keys & decision.permitted & planned_keys
        if batch.keys & decision.after_prerequisite:
            # Some of it (a room deletion) waits for a paused teardown of the same room. The batch
            # stays confirmed for that part only; what ran is done, and after a complete pass what
            # was not planned no longer applies.
            batch.observed = None
            keep = decision.after_prerequisite if complete else batch.keys - ran
            batch.actions = [a for a in batch.actions if a.key in keep]
            if ran:
                text = (
                    f"Batch {batch.id} confirmed by {batch.decided_by}: ran {len(ran)} action(s); "
                    f"{len(batch.actions)} wait until the teardown of their room may run (it is "
                    "paused, see !pending), then they run without a new confirmation."
                )
                notices.append(
                    GuardNotice(
                        Signal.guard_batch_closed,
                        {"batch_id": batch.id, "outcome": "partly_executed", "text": text},
                    )
                )
            continue
        del state.batches[batch_id]
        skipped = len(batch.keys) - len(ran)
        text = f"Batch {batch.id} confirmed by {batch.decided_by}: ran {len(ran)} action(s)" + (
            f"; {skipped} no longer applied and were skipped." if skipped else "."
        )
        notices.append(
            GuardNotice(
                Signal.guard_batch_closed, {"batch_id": batch.id, "outcome": "executed", "text": text}
            )
        )

    if complete:
        # Every metric that looked plausible moves; one that dropped keeps its last known good value.
        state.baseline = _merge_counts(state.baseline, decision.counts, keep=decision.tripped_metrics)

    for batch_id, why in decision.ended_rejections:
        ended = state.batches.get(batch_id)
        if ended is not None and ended.status is BatchStatus.rejected:
            del state.batches[batch_id]
            notices.append(_rejection_ended(ended, why))

    for batch_id, why in decision.narrowed_rejections:
        narrowed = state.batches.get(batch_id)
        if narrowed is not None and narrowed.status is BatchStatus.rejected:
            released = [a for a in narrowed.actions if not a.irreversible]
            narrowed.actions = [a for a in narrowed.actions if a.irreversible]
            text = (
                f"Rejected batch {narrowed.id} (rejected by {narrowed.decided_by}) partly ended: {why}. "
                f"Its other actions ({summarize_actions(released)}) are judged again like any others. "
                f"Its irreversible actions ({summarize_actions(narrowed.actions)}) stay held until "
                f"{_utc(narrowed.expires_at)}."
            )
            notices.append(
                GuardNotice(
                    Signal.guard_batch_closed,
                    {"batch_id": narrowed.id, "outcome": "rejection_narrowed", "text": text},
                )
            )

    for batch_id in list(state.batches):
        batch = state.batches[batch_id]
        if batch.status is BatchStatus.pending and now >= batch.expires_at:
            del state.batches[batch_id]
            text = (
                f"Batch {batch.id} expired unconfirmed ({summarize_actions(batch.actions)}). "
                "If its actions still apply, a later pass proposes them again."
            )
            notices.append(
                GuardNotice(
                    Signal.guard_batch_closed, {"batch_id": batch.id, "outcome": "expired", "text": text}
                )
            )
        elif complete and batch.status is not BatchStatus.confirmed and not (batch.keys & planned_keys):
            # The incident is over: nothing in the batch is planned any more (the token was fixed,
            # the users came back). A rejected batch stops covering its actions here as well.
            del state.batches[batch_id]
            if batch.status is BatchStatus.rejected:
                notices.append(_rejection_ended(batch, "none of its actions applies any more"))
                continue
            text = f"Batch {batch.id} closed: none of its actions applies any more."
            notices.append(
                GuardNotice(
                    Signal.guard_batch_closed, {"batch_id": batch.id, "outcome": "obsolete", "text": text}
                )
            )

    covered: set[str] = set()
    for batch in state.batches.values():
        covered |= batch.keys
    uncovered = [a for a in decision.held if a.key not in covered]
    if uncovered and not baseline_moved_by_confirmation:
        # (After a confirmed drop the next pass decides against the new baseline instead.)
        batch_id = id_factory()
        while batch_id in state.batches:
            batch_id = id_factory()
        batch = GuardBatch(
            id=batch_id,
            created_at=now,
            expires_at=now + limits.confirmation_expiry_sec,
            reasons=list(decision.reasons),
            observed=decision.counts if decision.input_tripped else None,
            actions=uncovered,
            metrics=sorted(decision.tripped_metrics),
            over_limit=decision.over_limit,
            irreversible_over_limit=decision.irreversible_over_limit,
        )
        state.batches[batch_id] = batch
        text = (
            "Destructive actions paused.\n"
            + describe_batch(batch)
            + f"\nRelease exactly these with !confirm {batch_id}, discard them with !reject {batch_id}. "
            "!pending lists every open batch."
        )
        notices.append(
            GuardNotice(
                Signal.destructive_actions_suppressed,
                {
                    "action": ",".join(sorted({a.kind.value for a in uncovered})),
                    "reason": "; ".join(decision.reasons),
                    "batch_id": batch_id,
                    "summary": text,
                },
            )
        )
    return notices


def _rejection_ended(batch: GuardBatch, why: str) -> GuardNotice:
    text = (
        f"Rejected batch {batch.id} ({summarize_actions(batch.actions)}, rejected by {batch.decided_by}) "
        f"ended: {why}. Its actions that still apply are judged again like any others: a plausible "
        "pass carries them out, an implausible one pauses them in a new batch."
    )
    return GuardNotice(
        Signal.guard_batch_closed, {"batch_id": batch.id, "outcome": "rejection_ended", "text": text}
    )


# --- persistence --------------------------------------------------------------------------------


class GuardStateUnreadable(RuntimeError):
    """The stored guard state cannot be read as a whole; every destructive action is held."""


# What an admin does about an unreadable state; part of every report and refusal.
RESET_HINT = (
    "Every destructive action stays paused until an allowlisted admin runs !guard reset in the admin "
    "control room, which keeps a verbatim copy of the unreadable state in the bot's account data and "
    "starts a new one"
)


def parse_guard_state(raw: Any) -> GuardState:
    """Read a stored guard state; raise :class:`GuardStateUnreadable` instead of guessing.

    Nothing stored yet is an empty state. A blob that does not validate, or that a newer onbot wrote,
    is unreadable: starting from empty would drop the baseline (so a narrowed token no longer trips
    layer 1) and the rejected batches (so their actions run).
    """
    if not raw:
        return GuardState()
    if not isinstance(raw, dict):
        raise GuardStateUnreadable("the guard state is not a JSON object")
    version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise GuardStateUnreadable(
            f"the guard state has schema_version {version!r}, this onbot understands up to "
            f"{SCHEMA_VERSION} (written by a newer onbot?)"
        )
    try:
        return GuardState.model_validate(raw)
    except ValidationError as exc:
        raise GuardStateUnreadable(
            f"the guard state does not validate ({exc.error_count()} error(s))"
        ) from exc


def _salvage_baseline(raw: Any) -> InputCounts:
    """The last known good counts of an unreadable state, if that part still reads."""
    if isinstance(raw, dict):
        try:
            return InputCounts.model_validate(raw.get("baseline") or {})
        except ValidationError:
            pass
    return InputCounts()


@runtime_checkable
class GuardStore(Protocol):
    async def load(self) -> GuardState:
        """The stored state; raises :class:`GuardStateUnreadable` when it cannot be read."""
        ...

    async def save(self, state: GuardState) -> None: ...

    async def reset_unreadable(self, *, now: float) -> str | None:
        """Set an unreadable state aside and start a new one; return where the copy went.

        ``None`` when the state is readable (nothing to reset).
        """
        ...


class MemoryGuardStore:
    """Keeps the state in memory only (tests, and the engine's default)."""

    def __init__(self, state: GuardState | None = None) -> None:
        self.state = state or GuardState()

    async def load(self) -> GuardState:
        return self.state.model_copy(deep=True)

    async def save(self, state: GuardState) -> None:
        self.state = state.model_copy(deep=True)

    async def reset_unreadable(self, *, now: float) -> str | None:
        return None  # an in-memory state is always readable


class MatrixAccountDataGuardStore:
    """Persists the guard state as an account-data blob on the bot user (AD-1)."""

    def __init__(self, client: Any, bot_id: str, server_name: str) -> None:
        self.client = client
        self.bot_id = bot_id
        self.data_type = guard_account_data_type(server_name)

    async def load(self) -> GuardState:
        return parse_guard_state(await self.client.get_account_data(self.bot_id, self.data_type))

    async def save(self, state: GuardState) -> None:
        await self.client.set_account_data(self.bot_id, self.data_type, state.model_dump(mode="json"))

    async def reset_unreadable(self, *, now: float) -> str | None:
        """Copy an unreadable state verbatim to ``<type>.unreadable.<unix time>``, then start anew.

        The new state keeps the old baseline when that part still reads, so layer 1 keeps comparing
        against the last known good counts. Open batches are gone: actions they paused are judged
        again on the next pass (an implausible pass pauses them in a new batch).
        """
        raw = await self.client.get_account_data(self.bot_id, self.data_type)
        try:
            parse_guard_state(raw)
        except GuardStateUnreadable:
            pass
        else:
            return None
        backup_type = f"{self.data_type}.unreadable.{int(now)}"
        await self.client.set_account_data(self.bot_id, backup_type, {"saved_at": now, "content": raw})
        await self.save(GuardState(baseline=_salvage_baseline(raw)))
        return backup_type


class ReadOnlyGuardStore:
    """Reads through, never writes (``reconcile-once --dry-run``)."""

    def __init__(self, inner: GuardStore) -> None:
        self.inner = inner

    async def load(self) -> GuardState:
        return await self.inner.load()

    async def save(self, state: GuardState) -> None:
        log.info("[dry-run] would save the guard state (%d open batches)", len(state.batches))

    async def reset_unreadable(self, *, now: float) -> str | None:
        log.info("[dry-run] would set an unreadable guard state aside")
        return None


# --- service ------------------------------------------------------------------------------------


class DestructiveActionGuard:
    """The guard state shared by the reconciler (decide, record) and the admin room (commands).

    Every operation reloads the state under one lock, so a confirmation that arrives during a pass
    is never overwritten by the pass's own save.
    """

    def __init__(
        self,
        limits: GuardLimits,
        store: GuardStore,
        events: EventBus,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = new_batch_id,
    ) -> None:
        self.limits = limits
        self.store = store
        self.events = events
        self.clock = clock
        self.id_factory = id_factory
        # Ids taken by something else `!confirm <id>` reaches (held announcements, W6R-04).
        self._reserved_ids: list[Callable[[], Collection[str]]] = []
        self._lock = asyncio.Lock()
        # The unreadable-state problem last reported, so the admin room hears it once, not every pass.
        self._reported_unreadable: str | None = None

    def reserve_ids(self, ids: Callable[[], Collection[str]]) -> None:
        """Have new batch ids avoid the ids ``ids()`` names at the time (W6R-04)."""
        self._reserved_ids.append(ids)

    def _new_batch_id(self) -> str:
        batch_id = self.id_factory()
        while any(batch_id in ids() for ids in self._reserved_ids):
            batch_id = self.id_factory()
        return batch_id

    async def decide(self, counts: InputCounts, planned: Sequence[DestructiveAction]) -> GuardDecision:
        async with self._lock:
            try:
                state = await self.store.load()
            except GuardStateUnreadable as exc:
                log.error("guard: %s; holding every destructive action of this pass", exc)
                return hold_everything(counts, planned, f"{exc}. {RESET_HINT}", now=self.clock())
        return decide(state, counts, planned, self.limits, now=self.clock())

    async def record(
        self, decision: GuardDecision, planned: Sequence[DestructiveAction], *, complete: bool
    ) -> None:
        async with self._lock:
            try:
                state = await self.store.load()
            except GuardStateUnreadable as exc:
                # Nothing is recorded and nothing overwrites the blob: an admin decides (!guard reset).
                notices = self._unreadable_notices(str(exc), planned)
            else:
                self._reported_unreadable = None
                before = state.model_dump(mode="json")
                notices = record(
                    state,
                    decision,
                    planned,
                    self.limits,
                    now=self.clock(),
                    complete=complete,
                    id_factory=self._new_batch_id,
                )
                if state.model_dump(mode="json") != before:
                    await self.store.save(state)
        for notice in notices:
            await self.events.emit(notice.signal, **notice.payload)

    def _unreadable_notices(self, problem: str, planned: Sequence[DestructiveAction]) -> list[GuardNotice]:
        if self._reported_unreadable == problem:
            return []
        self._reported_unreadable = problem
        held = list({a.key: a for a in planned}.values())
        text = (
            f"The destructive-action guard cannot read its state: {problem}. {RESET_HINT}. "
            f"Paused this pass: {summarize_actions(held)}."
        )
        return [GuardNotice(Signal.guard_state_unreadable, {"detail": problem, "text": text})]

    async def open_batches(self) -> list[GuardBatch]:
        """Batches that have not expired: pending, rejected, and confirmed ones waiting for a pass.

        Raises :class:`GuardStateUnreadable` when the state cannot be read.
        """
        async with self._lock:
            state = await self.store.load()
        now = self.clock()
        return sorted((b for b in state.batches.values() if now < b.expires_at), key=lambda b: b.created_at)

    async def confirm(self, batch_id: str, admin: str) -> tuple[bool, str]:
        """Release one batch; returns (released, reply text)."""
        return await self._decide_batch(batch_id, admin, BatchStatus.confirmed)

    async def reject(self, batch_id: str, admin: str) -> tuple[bool, str]:
        """Close one batch without running it; returns (rejected, reply text)."""
        return await self._decide_batch(batch_id, admin, BatchStatus.rejected)

    async def reset(self, admin: str) -> tuple[bool, str]:
        """``!guard reset``: replace an unreadable state, keeping a verbatim copy; (done, reply)."""
        async with self._lock:
            backup = await self.store.reset_unreadable(now=self.clock())
        if backup is None:
            return False, "The guard state is readable; there is nothing to reset."
        self._reported_unreadable = None
        log.warning("guard: %s replaced the unreadable guard state; the old one is kept in %s", admin, backup)
        return True, (
            f"Guard state replaced. The unreadable state is kept verbatim in the bot's account data "
            f"{backup}. The last known good counts were kept where they could be read; open batches "
            "are gone, so actions that still need a confirmation are proposed again under new ids."
        )

    async def _decide_batch(self, raw_id: str, admin: str, status: BatchStatus) -> tuple[bool, str]:
        batch_id = normalize_batch_id(raw_id)
        async with self._lock:
            try:
                state = await self.store.load()
            except GuardStateUnreadable as exc:
                return False, f"The guard cannot read its state: {exc}. {RESET_HINT}."
            batch = state.batches.get(batch_id)
            now = self.clock()
            if batch is None:
                return False, f"There is no open batch {batch_id!r}. !pending lists the open batches."
            if batch.status is BatchStatus.rejected:
                return False, (
                    f"Batch {batch_id} was rejected by {batch.decided_by}. It "
                    f"{_rejection_hold_text(batch)}; then they are judged again."
                )
            if batch.status is BatchStatus.confirmed:
                return False, f"Batch {batch_id} is already confirmed; it runs on the next pass."
            if now >= batch.expires_at:
                return False, (
                    f"Batch {batch_id} has expired. If its actions still apply, a later pass proposes "
                    "them again under a new id."
                )
            batch.status = status
            batch.decided_by = admin
            batch.decided_at = now
            if status is BatchStatus.confirmed:
                # A confirmation in the last minute must not be lost: the batch gets the full time
                # to run from now (a room deletion may wait for the teardown of its room).
                batch.expires_at = now + self.limits.confirmation_expiry_sec
            await self.store.save(state)
        if status is BatchStatus.confirmed:
            log.warning(
                "guard: %s confirmed batch %s (%s)", admin, batch_id, summarize_actions(batch.actions)
            )
            return True, (
                f"Batch {batch_id} confirmed ({summarize_actions(batch.actions)}). It runs on the next "
                "pass, which starts now; only actions that still apply are carried out. If it cannot "
                f"run by {_utc(batch.expires_at)}, it is closed and its actions are judged again."
            )
        log.warning("guard: %s rejected batch %s (%s)", admin, batch_id, summarize_actions(batch.actions))
        if any(a.irreversible for a in batch.actions):
            held = f"Its irreversible actions do not run before {_utc(batch.expires_at)}; " + (
                "the others not while the condition that created it lasts, at the latest until then. "
                if any(not a.irreversible for a in batch.actions)
                else ""
            )
        else:
            held = (
                "None of its actions runs while the condition that created it lasts, at the latest until "
                f"{_utc(batch.expires_at)}. "
            )
        return True, (
            f"Batch {batch_id} rejected. {held}Then its actions are judged again like any others; "
            "!pending shows it meanwhile."
        )
