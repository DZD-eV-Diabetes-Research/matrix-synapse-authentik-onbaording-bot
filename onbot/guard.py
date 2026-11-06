"""Guard against mass destruction: input plausibility and per-pass limits (D4 layers 1 and 2, D13).

A reconcile pass computes everything it would destroy (kicks, power-level withdrawals, room
teardowns, account locks, erasures) *before* it destroys anything, as a list of
:class:`DestructiveAction`. This module decides which of them may run now:

* **Layer 1, input plausibility.** The pass's input counts (active users in the sync scope, Authentik
  groups mapped to rooms) are compared with the last known good counts. An empty list, or a drop of
  more than ``max_drop_percent``, pauses *every* destructive action of the pass. A narrowed API token
  that answers ``200`` with one user (REC-01, live check 3) looks exactly like this. The baseline moves
  with every pass that does not trip, so gradual removals never trip it.
* **Layer 2, per-pass limit.** More than ``max_reversible_actions_per_pass`` reversible actions (kick,
  lock, room teardown, power-level withdrawal) in one pass pauses those actions. This also catches a
  long dry run being switched off (LIF-08) and a broken group mapping that keeps the counts intact.

A tripped guard never just blocks (D13). The paused actions become a :class:`GuardBatch` with a short
id, reported to the admin room; an allowlisted admin releases exactly that batch with ``!confirm <id>``
or closes it with ``!reject <id>``. Released actions run on the next pass, and only those that still
apply then: a confirmation never covers more than the admin saw. A batch expires after
``confirmation_expiry_sec``; if its actions still apply, a later pass proposes them again.

Layer 3 (irreversible actions above a small number, review session S5) plugs into the same batch
store: it only needs its own rule in :func:`decide`.

The pure part (:func:`input_drops`, :func:`decide`, :func:`record`) is side-effect free; the
:class:`DestructiveActionGuard` service persists the state in the bot's account data (AD-1) and emits
the reports on the event bus.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
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
    withdraw_power_level = "withdraw_power_level"
    room_teardown = "room_teardown"
    lock = "lock"
    erase = "erase"


# D13 layer 2: the reversible destructive actions. Everything else is irreversible (layer 3, S5).
REVERSIBLE_KINDS = frozenset(
    {ActionKind.kick, ActionKind.withdraw_power_level, ActionKind.room_teardown, ActionKind.lock}
)

_KIND_PLURAL = {
    ActionKind.kick: "kick {n} room member(s)",
    ActionKind.withdraw_power_level: "withdraw {n} power level(s)",
    ActionKind.room_teardown: "tear down {n} room(s)",
    ActionKind.lock: "lock {n} account(s)",
    ActionKind.erase: "erase {n} account(s)",
}


class DestructiveAction(BaseModel):
    """One thing a pass would destroy. Its :attr:`key` identifies it across passes."""

    model_config = {"frozen": True}

    kind: ActionKind
    target: str  # the MXID, or the room id for a room teardown
    room_id: str | None = None
    group_pk: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind.value}|{self.room_id or ''}|{self.target}"

    @property
    def reversible(self) -> bool:
        return self.kind in REVERSIBLE_KINDS

    def describe(self) -> str:
        group = f" (group {self.group_pk})" if self.group_pk else ""
        if self.kind is ActionKind.kick:
            return f"kick {self.target} from {self.room_id}{group}"
        if self.kind is ActionKind.withdraw_power_level:
            return f"withdraw the power level of {self.target} in {self.room_id}{group}"
        if self.kind is ActionKind.room_teardown:
            return f"tear down room {self.target}{group}"
        if self.kind is ActionKind.lock:
            return f"lock account {self.target}"
        return f"erase account {self.target}"


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

    @classmethod
    def from_config(cls, config: OnbotConfig) -> GuardLimits:
        cfg = config.destructive_action_guard
        return cls(
            max_drop_percent=cfg.max_drop_percent,
            max_reversible_actions_per_pass=cfg.max_reversible_actions_per_pass,
            confirmation_expiry_sec=float(cfg.confirmation_expiry_sec),
        )


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """What a pass may do, decided before it does anything destructive."""

    counts: InputCounts
    permitted: frozenset[str]  # keys of the planned actions that may run now
    held: tuple[DestructiveAction, ...]  # planned actions paused this pass
    reasons: tuple[str, ...]  # why they are paused
    input_tripped: bool  # layer 1: the baseline must not move
    confirmed_batches: tuple[str, ...]  # confirmed batches whose actions run in this pass
    decided_at: float = 0.0

    def permits(self, action: DestructiveAction) -> bool:
        return action.key in self.permitted


@dataclass(frozen=True, slots=True)
class GuardNotice:
    """Something the admin room should hear about."""

    signal: Signal
    payload: dict[str, Any] = field(default_factory=dict)


# --- pure logic ---------------------------------------------------------------------------------


def input_drops(baseline: InputCounts, counts: InputCounts, max_drop_percent: float) -> list[str]:
    """Layer 1: why ``counts`` look implausible against ``baseline`` (empty list: they do not)."""
    reasons: list[str] = []
    for metric, label in _METRICS:
        new = getattr(counts, metric)
        if new is None:
            continue  # not measured this pass
        old = getattr(baseline, metric)
        if new == 0:
            # D13: an empty list always needs confirmation, unless an admin already accepted zero.
            if old is None or old > 0:
                known = f" (last known good: {old})" if old else ""
                reasons.append(f"Authentik returned no {label}{known}")
            continue
        if old and new < old:
            drop = (old - new) * 100.0 / old
            if drop > max_drop_percent:
                reasons.append(
                    f"{label} dropped from {old} to {new} (-{drop:.0f}%), more than the "
                    f"{max_drop_percent:g}% allowed"
                )
    return reasons


def decide(
    state: GuardState,
    counts: InputCounts,
    planned: Sequence[DestructiveAction],
    limits: GuardLimits,
    *,
    now: float,
) -> GuardDecision:
    """Decide which planned actions may run this pass (pure).

    * Actions of a confirmed batch run if they are still planned (the re-check of D13).
    * Actions of an open batch (pending, or rejected while the incident lasts) stay paused: only
      confirming *that* batch releases them, never a later pass that happens to look normal.
    * Everything else passes layer 1 (implausible input pauses all of it) and layer 2 (too many
      reversible actions pauses those).
    """
    confirmed: dict[str, str] = {}
    waiting: dict[str, str] = {}
    for batch in state.batches.values():
        if batch.status is BatchStatus.confirmed:
            confirmed.update(dict.fromkeys(batch.keys, batch.id))
        elif batch.status is BatchStatus.rejected or now < batch.expires_at:
            waiting.update(dict.fromkeys(batch.keys, batch.id))
    unique = list({a.key: a for a in planned}.values())
    permitted = {a.key for a in unique if a.key in confirmed}
    held = [a for a in unique if a.key not in confirmed and a.key in waiting]
    rest = [a for a in unique if a.key not in confirmed and a.key not in waiting]
    confirmed_batches = tuple(sorted({confirmed[k] for k in permitted}))
    reasons: list[str] = []
    if held:
        reasons.append("waiting for open batch(es) " + ", ".join(sorted({waiting[a.key] for a in held})))

    drops = input_drops(state.baseline, counts, limits.max_drop_percent)
    input_tripped = bool(drops)
    if input_tripped:
        held += rest
        reasons += drops
    else:
        reversible = [a for a in rest if a.reversible]
        if len(reversible) > limits.max_reversible_actions_per_pass:
            held += reversible
            reasons.append(
                f"{len(reversible)} reversible destructive actions in one pass, more than the "
                f"{limits.max_reversible_actions_per_pass} allowed"
            )
    held_keys = {a.key for a in held}
    permitted |= {a.key for a in rest if a.key not in held_keys}
    return GuardDecision(
        counts=counts,
        permitted=frozenset(permitted),
        held=tuple(held),
        reasons=tuple(reasons),
        input_tripped=input_tripped,
        confirmed_batches=confirmed_batches,
        decided_at=now,
    )


def _merge_counts(baseline: InputCounts, counts: InputCounts) -> InputCounts:
    """``counts`` where measured, the old baseline elsewhere."""
    return InputCounts(
        **{
            metric: getattr(counts, metric)
            if getattr(counts, metric) is not None
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


def describe_batch(batch: GuardBatch, *, limit: int = MAX_LISTED_ACTIONS) -> str:
    """The admin-room text for a batch: why, what, and how to release it."""
    lines = [
        f"Batch {batch.id} ({batch.status.value}, expires {_utc(batch.expires_at)}): "
        f"{summarize_actions(batch.actions)}.",
    ]
    lines += [f"Why: {reason}." for reason in batch.reasons]
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
        if not complete and batch_id not in decision.confirmed_batches:
            continue  # a failed pass may just not have seen its actions; try again
        del state.batches[batch_id]
        if batch.observed is not None:
            # D13: after a confirmed drop, the counts the admin saw become the last known good.
            state.baseline = _merge_counts(state.baseline, batch.observed)
            baseline_moved_by_confirmation = True
        ran = batch.keys & decision.permitted & planned_keys
        skipped = len(batch.keys) - len(ran)
        text = f"Batch {batch.id} confirmed by {batch.decided_by}: ran {len(ran)} action(s)" + (
            f"; {skipped} no longer applied and were skipped." if skipped else "."
        )
        notices.append(
            GuardNotice(
                Signal.guard_batch_closed, {"batch_id": batch.id, "outcome": "executed", "text": text}
            )
        )

    if complete and not decision.input_tripped:
        state.baseline = _merge_counts(state.baseline, decision.counts)

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
            if batch.status is BatchStatus.pending:
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


# --- persistence --------------------------------------------------------------------------------


@runtime_checkable
class GuardStore(Protocol):
    async def load(self) -> GuardState: ...

    async def save(self, state: GuardState) -> None: ...


class MemoryGuardStore:
    """Keeps the state in memory only (tests, and the engine's default)."""

    def __init__(self, state: GuardState | None = None) -> None:
        self.state = state or GuardState()

    async def load(self) -> GuardState:
        return self.state.model_copy(deep=True)

    async def save(self, state: GuardState) -> None:
        self.state = state.model_copy(deep=True)


class MatrixAccountDataGuardStore:
    """Persists the guard state as an account-data blob on the bot user (AD-1)."""

    def __init__(self, client: Any, bot_id: str, server_name: str) -> None:
        self.client = client
        self.bot_id = bot_id
        self.data_type = guard_account_data_type(server_name)

    async def load(self) -> GuardState:
        raw = await self.client.get_account_data(self.bot_id, self.data_type)
        if not raw:
            return GuardState()
        try:
            return GuardState.model_validate(raw)
        except ValidationError:
            # Losing the baseline only means the next pass sets a new one; losing batches only means
            # paused actions are proposed again. Neither releases anything.
            log.error("guard state in account data %s is unreadable; starting from empty", self.data_type)
            return GuardState()

    async def save(self, state: GuardState) -> None:
        await self.client.set_account_data(self.bot_id, self.data_type, state.model_dump(mode="json"))


class ReadOnlyGuardStore:
    """Reads through, never writes (``reconcile-once --dry-run``)."""

    def __init__(self, inner: GuardStore) -> None:
        self.inner = inner

    async def load(self) -> GuardState:
        return await self.inner.load()

    async def save(self, state: GuardState) -> None:
        log.info("[dry-run] would save the guard state (%d open batches)", len(state.batches))


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
        self._lock = asyncio.Lock()

    async def decide(self, counts: InputCounts, planned: Sequence[DestructiveAction]) -> GuardDecision:
        async with self._lock:
            state = await self.store.load()
        return decide(state, counts, planned, self.limits, now=self.clock())

    async def record(
        self, decision: GuardDecision, planned: Sequence[DestructiveAction], *, complete: bool
    ) -> None:
        async with self._lock:
            state = await self.store.load()
            before = state.model_dump(mode="json")
            notices = record(
                state,
                decision,
                planned,
                self.limits,
                now=self.clock(),
                complete=complete,
                id_factory=self.id_factory,
            )
            if state.model_dump(mode="json") != before:
                await self.store.save(state)
        for notice in notices:
            await self.events.emit(notice.signal, **notice.payload)

    async def open_batches(self) -> list[GuardBatch]:
        """Pending batches that have not expired, and confirmed ones still waiting for a pass."""
        async with self._lock:
            state = await self.store.load()
        now = self.clock()
        return sorted(
            (
                b
                for b in state.batches.values()
                if b.status is BatchStatus.confirmed
                or (b.status is BatchStatus.pending and now < b.expires_at)
            ),
            key=lambda b: b.created_at,
        )

    async def confirm(self, batch_id: str, admin: str) -> tuple[bool, str]:
        """Release one batch; returns (released, reply text)."""
        return await self._decide_batch(batch_id, admin, BatchStatus.confirmed)

    async def reject(self, batch_id: str, admin: str) -> tuple[bool, str]:
        """Close one batch without running it; returns (rejected, reply text)."""
        return await self._decide_batch(batch_id, admin, BatchStatus.rejected)

    async def _decide_batch(self, raw_id: str, admin: str, status: BatchStatus) -> tuple[bool, str]:
        batch_id = normalize_batch_id(raw_id)
        async with self._lock:
            state = await self.store.load()
            batch = state.batches.get(batch_id)
            now = self.clock()
            if batch is None or batch.status is BatchStatus.rejected:
                return False, f"There is no open batch {batch_id!r}. !pending lists the open batches."
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
            await self.store.save(state)
        if status is BatchStatus.confirmed:
            log.warning(
                "guard: %s confirmed batch %s (%s)", admin, batch_id, summarize_actions(batch.actions)
            )
            return True, (
                f"Batch {batch_id} confirmed ({summarize_actions(batch.actions)}). It runs on the next "
                "pass, which starts now; only actions that still apply are carried out."
            )
        log.warning("guard: %s rejected batch %s (%s)", admin, batch_id, summarize_actions(batch.actions))
        return True, (
            f"Batch {batch_id} rejected: none of its actions runs, and they are not proposed again while "
            "this incident lasts."
        )
