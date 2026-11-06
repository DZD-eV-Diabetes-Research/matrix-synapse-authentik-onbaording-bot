"""Reconciler engine (AD-2): level-triggered, idempotent convergence.

Computes *desired* state (from Authentik) vs *actual* state (Synapse) and applies the diff. Runs on
a schedule **and** on demand (replacing the legacy ``while True: sleep`` tick loop) until
:meth:`ReconcilerEngine.request_stop`. Signals are handled by the process (``onbot.app``), not here, so
that they are caught from the first moment and not only once the loop runs (RUN-09). A single
``reconcile_once`` pass is fully re-runnable.

The pure decision logic lives in the sibling modules (``rooms``, ``membership``, ``power_levels``);
reads go through the Authentik + Synapse-admin clients; writes go through the Synapse-admin client
(membership/block) and the :class:`MatrixEffectors` seam (CS-API operations, Phase 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.base import ApiError
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig, SyncMatrixRoomsBasedOnAuthentikGroups
from onbot.events import EventBus, Signal
from onbot.guard import (
    ActionKind,
    DestructiveAction,
    DestructiveActionGuard,
    GuardDecision,
    GuardLimits,
    InputCounts,
    MemoryGuardStore,
    summarize_actions,
)
from onbot.identity import IdentityResolver, attribute_mxid, build_canonical, is_ignored
from onbot.identity import Resolution as UserResolution
from onbot.lifecycle.accounts import (
    EMPTY_DIRECTORY_MESSAGE,
    AccountLifecycleManager,
    DirectorySnapshot,
    DirectoryUser,
    LifecycleAction,
)
from onbot.logging import get_logger
from onbot.models import GroupRoomMap, MappedUser, MatrixRoom, RoomCreateAttributes
from onbot.reconciler.effectors import DryRunEffectors, MatrixEffectors
from onbot.reconciler.global_admins import GlobalRoomAdminResolver
from onbot.reconciler.join_rules import desired_join_rules, join_rules_change
from onbot.reconciler.membership import (
    desired_room_members,
    diff_room_membership,
    diff_space_membership,
)
from onbot.reconciler.power_levels import (
    PowerLevelGroup,
    RoomAuthority,
    apply_global_room_admins,
    compute_desired_user_levels,
    creators_are_privileged,
    extract_power_level_groups,
    hold_withdrawals,
    plan_power_levels,
    room_authority,
)
from onbot.reconciler.repair import (
    CANONICAL_ALIAS_EVENT_TYPE,
    SPACE_CHILD_EVENT_TYPE,
    SPACE_PARENT_EVENT_TYPE,
    closed_join_rules,
    is_closed,
    marker_repair,
    space_child_attached,
    space_child_content,
    space_child_ok,
    space_parent_content,
    space_parent_ok,
)
from onbot.reconciler.report import PassReport, describe_error
from onbot.reconciler.rooms import build_group_room_maps, colliding_aliases, resolve_room_settings
from onbot.reconciler.state import (
    AnyRoomState,
    GrantedPowerLevels,
    GroupRoomState,
    OnbotRoomType,
    SpaceRoomState,
    VisitorLobbyRoomState,
    dump_room_state,
    event_type_name,
    granted_power_levels_type,
    parse_room_state,
)
from onbot.rooms.identity import (
    Resolution,
    RoomIdentityResolver,
    RoomIndex,
    RoomKey,
    describe_stray,
    stray_claims,
)
from onbot.rooms.provision import ManagedRoomSpec, lock_markers, markers_locked

log = get_logger(__name__)

JOIN_RULES_EVENT_TYPE = "m.room.join_rules"
CREATE_EVENT_TYPE = "m.room.create"
TOPIC_EVENT_TYPE = "m.room.topic"


class ConfigurationError(RuntimeError):
    """Raised when the configuration cannot be satisfied (e.g. required space missing)."""


_LIFECYCLE_ACTION_KINDS = {
    LifecycleAction.logout: ActionKind.lock,
    LifecycleAction.erase: ActionKind.erase,
    LifecycleAction.delete_media: ActionKind.delete_media,
}


@dataclass(slots=True)
class _Deferred:
    """Destructive actions of one room step, and how to carry out the permitted ones later."""

    keys: frozenset[str]
    run: Callable[[frozenset[str]], Awaitable[object]]


@dataclass(slots=True)
class PassPlan:
    """Everything a pass would destroy, collected before anything is destroyed (D4, D13).

    The room stage records its kicks, power-level withdrawals, room teardowns and deletions here
    instead of carrying them out; the lifecycle adds its locks and erasures. The guard then decides on
    the whole list, and :meth:`execute` carries out the permitted part.
    """

    actions: list[DestructiveAction] = field(default_factory=list)
    _deferred: list[_Deferred] = field(default_factory=list)

    def defer(
        self, actions: Sequence[DestructiveAction], run: Callable[[frozenset[str]], Awaitable[object]]
    ) -> None:
        """Record ``actions``; ``run`` later receives the keys of those the guard permits."""
        if not actions:
            return
        self.actions.extend(actions)
        self._deferred.append(_Deferred(frozenset(a.key for a in actions), run))

    def note(self, actions: Sequence[DestructiveAction]) -> None:
        """Record ``actions`` that their stage carries out itself, asking the guard (the lifecycle)."""
        self.actions.extend(actions)

    async def execute(self, permitted: Collection[str]) -> None:
        allowed_keys = frozenset(permitted)
        for step in self._deferred:
            allowed = step.keys & allowed_keys
            if allowed:
                await step.run(allowed)


@dataclass(slots=True)
class _PassInputs:
    """The input counts of one pass, for the guard's plausibility check (layer 1)."""

    users_in_scope: int | None = None
    mapped_groups: int | None = None
    # The lifecycle refused its step (an unreadable ledger): its locks and erasures are unknown this
    # pass, so the guard must not read their absence as "no longer applies".
    lifecycle_refused: bool = False

    def counts(self) -> InputCounts:
        return InputCounts(users_in_scope=self.users_in_scope, mapped_groups=self.mapped_groups)


class _RoomStepProblems(Exception):
    """A room step finished, but some per-user actions in it were refused (see ``_guarded``)."""

    def __init__(self, problems: list[str], *, held: Sequence[str] = ()) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems
        # Withdrawals the step held for the guard although it ended with problems (power levels).
        self.held = list(held)


class ReconcilerEngine:
    def __init__(
        self,
        config: OnbotConfig,
        authentik: ApiClientAuthentik,
        admin: ApiClientSynapseAdmin,
        effectors: MatrixEffectors | None = None,
        events: EventBus | None = None,
        lifecycle: AccountLifecycleManager | None = None,
        guard: DestructiveActionGuard | None = None,
        *,
        identity: IdentityResolver | None = None,
        rooms: RoomIdentityResolver | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.authentik = authentik
        self.admin = admin
        self.effectors: MatrixEffectors = effectors or DryRunEffectors()
        self.events = events or EventBus()
        self.lifecycle = lifecycle
        self.guard = guard or DestructiveActionGuard(
            GuardLimits.from_config(config), MemoryGuardStore(), self.events
        )
        # A preview pass (``reconcile-once --dry-run``): the effectors only log, and the guard's
        # verdict is logged but not recorded.
        self.dry_run = dry_run
        self.server_name = config.synapse_server.server_name
        # The one Authentik user -> MXID resolution (D8, MNT-13). By default it keeps its bindings
        # where the lifecycle keeps its known users, so both read the same record.
        store = getattr(lifecycle, "store", None)
        self.identity = identity or IdentityResolver(
            config, store=store if hasattr(store, "load_known_users") else None, events=self.events
        )
        self.global_admins = GlobalRoomAdminResolver(authentik, config, identity=self.identity)
        # Matrix accounts that several Authentik users resolve to this pass: nobody joins, kicks,
        # changes the power level of, locks or erases them (D8, fail closed).
        self._held_mxids: frozenset[str] = frozenset()
        # Which room is which, by creator and marker (D20). Shared with the admin room and the welcome
        # flow in the service, so the room identities are read once per process.
        self.rooms = rooms or RoomIdentityResolver(
            admin,
            self.effectors,
            bot_id=config.synapse_server.bot_user_id,
            server_name=self.server_name,
        )
        # Unix timestamp of the last pass that ran to completion; reported by the admin room's
        # `!status` command. ``None`` until the first pass finishes.
        self.last_reconcile_at: float | None = None
        # Passes the run loop has begun, so a caller can tell "not started yet" from "missed one".
        self.passes_started = 0
        # What the last pass could not converge, per room (REC-03). Empty report before the first pass.
        self.last_pass_report = PassReport()
        # room id -> (room version, creators). The create event never changes, so one read per room.
        self._create_info_cache: dict[str, tuple[str | None, frozenset[str]]] = {}
        # Findings about rooms already reported to the admins (`room_attention`), once per process.
        self._reported_rooms: set[str] = set()
        # This pass: rooms created by it (nothing to repair yet), and managed rooms a server admin
        # blocked, which the bot leaves alone (REC-11).
        self._created_this_pass: set[str] = set()
        self._blocked_by_others: set[str] = set()
        # The visitor lobbies the last pass managed with their feature on (room id -> group), for the
        # admin room's `!lobby reopen` (D16).
        self.active_lobbies: dict[str, str] = {}
        self._stop = asyncio.Event()
        self._trigger = asyncio.Event()

    # --- runtime loop --------------------------------------------------------

    def trigger(self) -> None:
        """Request an out-of-band reconcile (on-demand) before the next scheduled tick."""
        self._trigger.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._trigger.set()  # unblock the wait so we exit promptly

    async def run(self, *, on_pass: Callable[[bool], None] | None = None) -> None:
        """Run scheduled + on-demand reconciles until :meth:`request_stop`.

        ``on_pass`` is told after every pass whether it succeeded (the liveness heartbeat).
        """
        log.info("reconciler started; tick=%ss", self.config.server_tick_rate_sec)
        while not self._stop.is_set():
            # Clear before the pass so a trigger raised *during* it is preserved for the next wait.
            self._trigger.clear()
            self.passes_started += 1
            try:
                await self.reconcile_once()
            except Exception:
                log.exception("reconcile pass failed; will retry next tick")
                if on_pass is not None:
                    on_pass(False)
            else:
                if on_pass is not None:
                    on_pass(True)
            if self._stop.is_set():
                break
            await self._wait_for_next_tick()
        log.info("reconciler stopped")

    async def _wait_for_next_tick(self) -> None:
        # Wake on an on-demand trigger, or fall through on the scheduled timeout.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._trigger.wait(), timeout=self.config.server_tick_rate_sec)

    # --- one convergence pass ------------------------------------------------

    async def reconcile_once(self) -> None:
        log.info(
            "reconcile: gathering desired (Authentik) and actual (Synapse) state%s",
            " (dry run: nothing is changed)" if self.dry_run else "",
        )
        matrix_users = await self.admin.list_users()
        # Bindings and MAS links for this pass, before anybody is resolved (D8).
        await self.identity.refresh()
        users, users_in_scope = await self._gather_mapped_users(matrix_users)
        # Before any membership or power-level decision: every one of them consults this set, and an
        # admin that resolves halfway through would land in some rooms and not others. Without a
        # readable binding record nobody can be resolved; the previous set stays (memberships and
        # power levels are not managed this pass anyway).
        if self.identity.unavailable is None:
            await self.global_admins.resolve(users, {u["name"] for u in matrix_users})

        # Rooms and offboarding are independent stages (SEC-04, LIF-04): a room the bot cannot
        # manage, or a room stage that fails as a whole (a missing space, a Synapse 500 on the room
        # list), must never keep disabled users signed in. Every room is isolated inside the stage;
        # a stage-wide failure is held until the rest has run, then fails the pass as before.
        #
        # Nothing destructive happens inside the stages (D4, D13): they record their kicks,
        # withdrawals, teardowns, deletions, locks and erasures in `plan`, the guard decides on all of
        # them at once, and only then is the permitted part carried out.
        report = PassReport()
        plan = PassPlan()
        inputs = _PassInputs(users_in_scope=users_in_scope)
        stage_errors: list[Exception] = []
        group_room_count = 0
        try:
            group_room_count = await self._converge_room_stage(users, report, plan, inputs)
        except Exception as exc:
            stage_errors.append(exc)
            log.error(
                "reconcile: room stage aborted (%s); the account lifecycle still runs",
                describe_error(exc),
            )
        try:
            snapshot: DirectorySnapshot | None = None
            try:
                snapshot = await self._plan_lifecycle(matrix_users, {u.mxid for u in users}, plan, inputs)
            except Exception as exc:
                stage_errors.append(exc)
                log.error("reconcile: offboarding stage aborted (%s)", describe_error(exc))
            decision = await self.guard.decide(inputs.counts(), plan.actions)
            self._log_decision(decision, plan)
            await plan.execute(decision.permitted)
            if snapshot is not None:
                await self._run_lifecycle(snapshot, decision)
            if not self.dry_run:
                await self.guard.record(
                    decision, plan.actions, complete=not stage_errors and not inputs.lifecycle_refused
                )
        finally:
            self.last_pass_report = report
            await self._publish_report(report)
        if stage_errors:
            raise stage_errors[0]

        self.last_reconcile_at = time.time()
        log.info(
            "reconcile: done (%d users, %d group rooms, %d rooms not converged)",
            len(users),
            group_room_count,
            len(report),
        )
        # Last, and after the timestamp: a subscriber that fails must not make the pass look unfinished.
        await self.events.emit(Signal.reconcile_completed)

    def _log_decision(self, decision: GuardDecision, plan: PassPlan) -> None:
        """One line per pass about the guard's verdict, loud when it pauses anything."""
        prefix = "[dry-run] " if self.dry_run else ""
        if decision.held:
            log.warning(
                "%sguard: paused %d destructive action(s) this pass (%s): %s.%s",
                prefix,
                len(decision.held),
                summarize_actions(decision.held),
                "; ".join(decision.reasons),
                ""  # the reason already says what the admin does (!guard reset)
                if decision.state_unreadable
                else " An admin releases them with !confirm <batch id> in the admin control room "
                "(!pending lists the batches)",
            )
            # A preview lists every one; a live pass has the batch report, so only at debug level.
            level = logging.INFO if self.dry_run else logging.DEBUG
            for action in decision.held:
                log.log(level, "%sguard: paused: %s", prefix, action.describe())
        if decision.confirmed_batches:
            log.warning(
                "%sguard: carrying out confirmed batch(es) %s", prefix, ", ".join(decision.confirmed_batches)
            )
        permitted = [a for a in plan.actions if decision.permits(a)]
        if permitted:
            log.info(
                "%sguard: %d destructive action(s) permitted (%s)",
                prefix,
                len(permitted),
                summarize_actions(permitted),
            )

    async def _converge_room_stage(
        self, users: list[MappedUser], report: PassReport, plan: PassPlan, inputs: _PassInputs
    ) -> int:
        """Everything that touches rooms and the space; returns the number of group rooms."""
        rooms_cfg = self.config.sync_matrix_rooms_based_on_authentik_groups
        if not rooms_cfg.enabled and not self.config.create_matrix_rooms_in_a_matrix_space.enabled:
            return 0
        self._created_this_pass = set()
        self._blocked_by_others = set()
        index = await self.rooms.scan()
        space = await self._resolve_space(index)
        group_maps = await self._gather_group_room_maps()
        if rooms_cfg.enabled:
            inputs.mapped_groups = len(group_maps)

        await self._converge_rooms(group_maps, space, index, report)
        self._report_stray_rooms(index, group_maps, report)
        await self._converge_obsolete_rooms(index, group_maps, report, plan)
        await self._converge_switched_off_lobbies(index, group_maps, space, report, plan)
        if space is not None:
            await self._guarded(
                report,
                room_kind=OnbotRoomType.space,
                room_id=space.room_id,
                group_pk=None,
                step="space",
                work=self._converge_space(space, users, index),
            )
        await self._converge_room_membership_and_levels(group_maps, users, space, report, plan)
        return len(group_maps)

    async def _guarded(
        self,
        report: PassReport,
        *,
        room_kind: str,
        room_id: str | None,
        group_pk: str | None,
        step: str,
        work: Awaitable[object],
    ) -> bool:
        """Run one room's ``work``; on any error record it for the room and return ``False``.

        This is the per-room isolation of REC-03: whatever one room does wrong ends that room's
        remaining steps for this pass and nothing else. An :class:`ApiError` is an answer from the
        server (a refusal, a missing room) and is reported without a traceback; anything else is a
        bug and is logged with one.
        """
        try:
            await work
        except _RoomStepProblems as partial:
            # The step ran to its end but left some users behind (refused joins or kicks).
            report.extend(room_kind=room_kind, room_id=room_id, group_pk=group_pk, problems=partial.problems)
            return False
        except Exception as exc:
            if not isinstance(exc, ApiError):
                log.exception("unexpected error in %s of room %s (group %s)", step, room_id, group_pk)
            report.add(
                room_kind=room_kind,
                room_id=room_id,
                group_pk=group_pk,
                problem=f"{step} failed: {describe_error(exc)}",
            )
            return False
        return True

    async def _publish_report(self, report: PassReport) -> None:
        """One WARNING line and one event per room that did not converge this pass."""
        for room in report.rooms:
            log.warning(
                "reconcile: room not converged: kind=%s room=%s group=%s: %s",
                room.room_kind,
                room.room_id,
                room.group_pk,
                "; ".join(room.problems),
            )
            await self.events.emit(
                Signal.room_not_converged,
                room_kind=room.room_kind,
                room_id=room.room_id,
                group_pk=room.group_pk,
                problems=list(room.problems),
            )

    async def _gather_mapped_users(
        self, matrix_users: list[dict[str, Any]]
    ) -> tuple[list[MappedUser], int | None]:
        """The synced users with a Matrix account, and how many active users in the sync scope map to
        a Matrix ID at all (the guard's ``users_in_scope``; ``None`` while user sync is off)."""
        cfg = self.config.sync_authentik_users_with_matrix_rooms
        self._held_mxids = frozenset()
        if not cfg.enabled or self.identity.unavailable is not None:
            # Off, or nobody can be resolved (an unreadable binding record): membership and power
            # levels are left alone this pass, never projected from an empty or guessed user list.
            return [], None
        scoped = [
            u for u in await self._list_scoped_users(filter_is_active=True) if not is_ignored(u, self.config)
        ]
        # Every one of them is active, in scope and not ignored: a first sight is recorded as their
        # binding, which keeps them on that account across renames (D8).
        resolution = await self.identity.resolve(
            scoped, bind=[str(u["pk"]) for u in scoped], matrix_mxids={u["name"] for u in matrix_users}
        )
        self._held_mxids = resolution.held_mxids
        by_mxid: dict[str, dict[str, Any]] = {}
        for user in scoped:
            mxid = resolution.mxid(user["pk"])
            if mxid is not None:
                by_mxid[mxid] = user

        bot_id = self.config.synapse_server.bot_user_id
        mapped: list[MappedUser] = []
        for matrix_user in matrix_users:
            mxid = matrix_user["name"]
            # The bot is never a synced user, even when its account also exists in Authentik: as the
            # room creator it must stay out of the power-level users map (ADR-0011, REC-12).
            if mxid == bot_id or mxid in self.config.matrix_user_ignore_list:
                continue
            authentik_user = by_mxid.get(mxid)
            if authentik_user is not None:
                mapped.append(MappedUser(authentik_obj=authentik_user, mxid=mxid, matrix_obj=matrix_user))
                await self.events.emit(Signal.user_synced, mxid=mxid)
        return mapped, len(by_mxid)

    async def _list_scoped_users(self, *, filter_is_active: bool | None) -> list[dict[str, Any]]:
        """Authentik users inside the sync scope (paths, attributes, groups), each once.

        The one scope definition: the room sync asks for the active users in it, the lifecycle for
        everyone in it, so "who is synced" and "who is offboarded" can never disagree (LIF-05, REC-14).
        ``filter_is_active=None`` returns active and inactive users.
        """
        cfg = self.config.sync_authentik_users_with_matrix_rooms
        configured_paths = cfg.sync_only_users_in_authentik_pathes
        paths: list[str | None] = [*configured_paths] if configured_paths else [None]
        users: list[dict[str, Any]] = []
        seen_pks: set[str] = set()
        for path in paths:
            for user in await self.authentik.list_users(
                filter_by_path=path,
                filter_by_attribute=cfg.sync_only_users_with_authentik_attributes,
                filter_groups_by_pk=cfg.sync_only_users_of_groups_with_id,
                filter_is_active=filter_is_active,
            ):
                if user["pk"] in seen_pks:
                    continue
                seen_pks.add(user["pk"])
                users.append(user)
        return users

    async def _gather_group_room_maps(self) -> list[GroupRoomMap]:
        settings = self.config.sync_matrix_rooms_based_on_authentik_groups
        if not settings.enabled:
            return []
        groups = await self.authentik.list_groups(filter_by_attribute=settings.only_groups_with_attributes)
        return build_group_room_maps(groups, self.config, self.server_name)

    async def _resolve_space(self, index: RoomIndex) -> MatrixRoom | None:
        """The space, found by its marker; created when there is none and its alias is free.

        A space the bot cannot settle on (two claim to be it, its alias belongs to somebody else's room,
        or bot rooms could not be read) aborts the room stage: every group room hangs off it.
        """
        cfg = self.config.create_matrix_rooms_in_a_matrix_space
        if not cfg.enabled:
            return None
        target_alias = build_canonical(cfg.alias, self.server_name, "#")
        key = RoomKey(OnbotRoomType.space, name=cfg.alias)
        resolution = await self.rooms.resolve(index, key, target_alias)
        if resolution.problem is not None:
            raise ConfigurationError(f"space {target_alias}: {resolution.problem}")
        if resolution.room is not None:
            space = resolution.room
            space.is_space = True
        else:
            if not cfg.create_matrix_space_if_not_exists.enabled:
                raise ConfigurationError(
                    f"space {target_alias!r} not found and auto-creation is disabled "
                    "(create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled)"
                )
            create = cfg.create_matrix_space_if_not_exists
            room_id = await self.effectors.create_managed_room(
                ManagedRoomSpec(
                    marker=SpaceRoomState(configured_alias=cfg.alias),
                    alias_localpart=cfg.alias,
                    name=create.name,
                    topic=create.topic,
                    room_params=create.space_params,
                    is_space=True,
                )
            )
            space = MatrixRoom(room_id=room_id, canonical_alias=target_alias, is_space=True)
            self.rooms.register(space, key)
            self._created_this_pass.add(room_id)
        self.rooms.space_id = space.room_id
        return space

    async def _converge_avatar(
        self,
        room_id: str,
        room_type: OnbotRoomType,
        desired: str | None,
        fresh_state: AnyRoomState,
        *,
        marker: dict[str, Any] | None = None,
    ) -> None:
        """Set/update a room or space icon (``m.room.avatar``) from a source URL (G6.8).

        Re-uploads only when the configured URL differs from the one recorded in the room's onbot
        state event (``avatar_source_url``), so a stable URL costs one state read per tick and no
        upload. Applies to already-existing rooms too, not just freshly created ones. Best-effort:
        a fetch/upload failure is logged and retried next tick. Clearing the source URL stops future
        updates but leaves the current icon in place (removing avatars is out of scope). ``fresh_state``
        seeds the state event (its ``group_id``) when the room has none yet. ``marker`` is the
        marker's content when the caller has just read it (the upkeep step), saving a read.
        """
        event_type = event_type_name(self.server_name, room_type)
        raw = marker if marker is not None else await self.effectors.get_room_state(room_id, event_type)
        state = parse_room_state(room_type, raw) if raw else fresh_state
        if desired == state.avatar_source_url:
            return
        if desired:
            try:
                mxc = await self.effectors.upload_avatar(desired)
                await self.effectors.set_room_avatar(room_id, mxc)
            except Exception:
                log.exception("failed to set avatar of %s from %s", room_id, desired)
                return
            log.info("set avatar of %s from %s", room_id, desired)
        state.avatar_source_url = desired
        await self.effectors.put_room_state(room_id, event_type, dump_room_state(state))

    async def _converge_rooms(
        self,
        group_maps: list[GroupRoomMap],
        space: MatrixRoom | None,
        index: RoomIndex,
        report: PassReport,
    ) -> None:
        parent_space_id = space.room_id if space else None
        # Two roles wanting one alias (REC-13): their rooms are still found by marker, but none of
        # them gets a room adopted or created through that alias.
        collisions = colliding_aliases(group_maps)
        for alias, owners in sorted(collisions.items()):
            log.error(
                "alias %s is wanted by %s; none of them gets a new room until that is resolved",
                alias,
                " and ".join(owners),
            )
        for gm in group_maps:
            await self._guarded(
                report,
                room_kind=OnbotRoomType.group_room,
                room_id=None,
                group_pk=gm.group_pk,
                step="room setup",
                work=self._converge_group_room_existence(gm, parent_space_id, index, collisions),
            )
            if gm.room is not None and gm.room.room_id not in self._blocked_by_others:
                await self._guarded(
                    report,
                    room_kind=OnbotRoomType.group_room,
                    room_id=gm.room.room_id,
                    group_pk=gm.group_pk,
                    step="room upkeep",
                    work=self._converge_room_upkeep(
                        gm.room,
                        GroupRoomState(group_id=gm.group_pk),
                        alias=gm.desired.canonical_alias,
                        alias_usable=gm.desired.canonical_alias not in collisions,
                        avatar_source_url=gm.desired.avatar_source_url,
                        index=index,
                        space_id=parent_space_id,
                        suggested=False,
                    ),
                )
            if gm.room is None or gm.lobby_desired is None:
                continue
            await self._guarded(
                report,
                room_kind=OnbotRoomType.visitor_lobby,
                room_id=None,
                group_pk=gm.group_pk,
                step="lobby setup",
                work=self._converge_lobby_existence(gm, parent_space_id, index, collisions),
            )
            if gm.lobby is not None and gm.lobby.room_id not in self._blocked_by_others:
                await self._guarded(
                    report,
                    room_kind=OnbotRoomType.visitor_lobby,
                    room_id=gm.lobby.room_id,
                    group_pk=gm.group_pk,
                    step="lobby upkeep",
                    # Same source URL as the group room, so the uploader's per-URL cache makes the
                    # avatar a state read and a set, not a second fetch+upload.
                    work=self._converge_room_upkeep(
                        gm.lobby,
                        VisitorLobbyRoomState(group_id=gm.group_pk),
                        alias=gm.lobby_desired.canonical_alias,
                        alias_usable=gm.lobby_desired.canonical_alias not in collisions,
                        avatar_source_url=gm.lobby_desired.avatar_source_url,
                        index=index,
                        space_id=parent_space_id,
                        # A lobby is a room a wandering space member should find.
                        suggested=True,
                    ),
                )
        self.active_lobbies = {
            gm.lobby.room_id: gm.group_pk
            for gm in group_maps
            if gm.lobby is not None and gm.lobby.room_id not in self._blocked_by_others
        }

    async def _resolve_room(
        self, index: RoomIndex, key: RoomKey, alias: str, collisions: dict[str, list[str]]
    ) -> Resolution:
        resolution = await self.rooms.resolve(index, key, alias, alias_usable=alias not in collisions)
        if resolution.problem is not None:
            # Reported for the group; the room's later steps are skipped because it stays unset.
            raise _RoomStepProblems([f"{key.describe()} skipped: {resolution.problem}"])
        return resolution

    async def _converge_group_room_existence(
        self,
        gm: GroupRoomMap,
        parent_space_id: str | None,
        index: RoomIndex,
        collisions: dict[str, list[str]],
    ) -> None:
        key = RoomKey(OnbotRoomType.group_room, gm.group_pk)
        resolution = await self._resolve_room(index, key, gm.desired.canonical_alias, collisions)
        if resolution.room is None:
            room_id = await self.effectors.create_managed_room(
                ManagedRoomSpec(
                    marker=GroupRoomState(group_id=gm.group_pk),
                    alias_localpart=gm.desired.alias,
                    name=gm.desired.name,
                    topic=gm.desired.topic,
                    encrypted=gm.desired.encrypted,
                    room_params=gm.desired.room_params,
                    parent_space_id=parent_space_id,
                )
            )
            gm.room = MatrixRoom(room_id=room_id, canonical_alias=gm.desired.canonical_alias)
            self.rooms.register(gm.room, key)
            self._created_this_pass.add(room_id)
        else:
            gm.room = resolution.room
            await self._converge_block(gm.room, key)

    async def _converge_block(self, room: MatrixRoom, key: RoomKey) -> None:
        """Unblock a managed room the bot blocked itself; leave one a server admin blocked (REC-11).

        The bot blocks a room when it tears it down (its group disappeared), so a blocked room whose
        group is back gets unblocked (G2.3). Synapse records who set a block; a block somebody else
        set (moderation, a legal hold) is theirs: the room stays blocked, the bot does not manage it
        while it is, and the admins hear of it once.
        """
        block = await self.admin.get_room_block(room.room_id)
        if not block.blocked:
            return
        if block.by == self.config.synapse_server.bot_user_id:
            log.info("unblocking room %s, the %s (group reappeared)", room.room_id, key.describe())
            await self.admin.room_set_blocked(room.room_id, blocked=False)
            return
        self._blocked_by_others.add(room.room_id)
        await self._report_once(
            f"blocked|{room.room_id}|{block.by}",
            f"room {room.room_id}, the {key.describe()}, was blocked by {block.by or 'an unknown account'}, "
            "not by the bot. The bot leaves it blocked and does not manage it (members, power levels, "
            "name) until a server admin unblocks it.",
        )

    async def _converge_lobby_existence(
        self,
        gm: GroupRoomMap,
        parent_space_id: str | None,
        index: RoomIndex,
        collisions: dict[str, list[str]],
    ) -> None:
        assert gm.lobby_desired is not None
        await self._converge_lobby_creation(gm, parent_space_id, index, collisions)
        if gm.lobby is not None and gm.lobby.room_id not in self._created_this_pass:
            await self._converge_block(gm.lobby, RoomKey(OnbotRoomType.visitor_lobby, gm.group_pk))

    async def _converge_lobby_creation(
        self,
        gm: GroupRoomMap,
        parent_space_id: str | None,
        index: RoomIndex,
        collisions: dict[str, list[str]],
    ) -> None:
        """Find the group's visitor lobby, or create it if it does not exist yet (ADR-0012).

        The lobby is marked with ``OnbotRoomType.visitor_lobby``, never ``group_room``, so no later
        pass mistakes it for the group room, projects Authentik membership onto it, and kicks every
        visitor. Its join rule is set at creation from the runtime space id, so it is never briefly
        invite-only nor briefly open. Requires a space (the config validator enforces that when the
        lobby comes from config; this guard covers a lobby enabled by an Authentik attribute while no
        space is configured).
        """
        assert gm.lobby_desired is not None
        if parent_space_id is None:
            log.warning(
                "group %s requests a visitor lobby but no parent space is configured; skipping",
                gm.group_pk,
            )
            return
        key = RoomKey(OnbotRoomType.visitor_lobby, gm.group_pk)
        resolution = await self._resolve_room(index, key, gm.lobby_desired.canonical_alias, collisions)
        if resolution.room is not None:
            gm.lobby = resolution.room
            return
        join_rules_content = desired_join_rules(OnbotRoomType.visitor_lobby, parent_space_id)
        assert join_rules_content is not None
        room_id = await self.effectors.create_managed_room(
            ManagedRoomSpec(
                marker=VisitorLobbyRoomState(group_id=gm.group_pk),
                alias_localpart=gm.lobby_desired.alias,
                name=gm.lobby_desired.name,
                topic=gm.lobby_desired.topic,
                encrypted=gm.lobby_desired.encrypted,
                room_params=gm.lobby_desired.room_params,
                parent_space_id=parent_space_id,
                # A lobby is a room a wandering space member should find.
                suggested=True,
                extra_initial_state=(
                    {"type": JOIN_RULES_EVENT_TYPE, "state_key": "", "content": join_rules_content},
                ),
            )
        )
        gm.lobby = MatrixRoom(room_id=room_id, canonical_alias=gm.lobby_desired.canonical_alias)
        self.rooms.register(gm.lobby, key)
        self._created_this_pass.add(room_id)

    # --- upkeep: rooms keep their shape after creation (REC-09) ----------------------------------

    async def _converge_room_upkeep(
        self,
        room: MatrixRoom,
        marker: AnyRoomState,
        *,
        alias: str | None,
        alias_usable: bool,
        avatar_source_url: str | None,
        index: RoomIndex,
        space_id: str | None = None,
        suggested: bool = False,
    ) -> None:
        """Repair what a managed room lost since it was created, then converge its avatar.

        Every check reads first and writes only on a difference: the onbot marker (present, complete,
        written by the bot) and its lock, the alias, and the link to the space. A room created in this
        pass got all of that with ``createRoom``. ``marker`` is the marker the room's role calls for;
        the role comes from how the room was resolved (its locked, bot-written marker, D20), never
        from what a marker says now.
        """
        room_type = OnbotRoomType(marker.room_type)
        content: dict[str, Any] | None = None
        if room.room_id not in self._created_this_pass:
            content = await self._repair_marker(room, marker)
            await self._repair_lock(room, marker)
            if alias is not None and alias_usable:
                await self._repair_alias(room, marker, alias, index)
            if space_id is not None:
                await self._repair_space_link(room, space_id, suggested=suggested)
        await self._converge_avatar(room.room_id, room_type, avatar_source_url, marker, marker=content)

    def _describe(self, marker: AnyRoomState) -> str:
        group_id = getattr(marker, "group_id", None)
        return RoomKey(OnbotRoomType(marker.room_type), group_id).describe()

    async def _repair_marker(self, room: MatrixRoom, expected: AnyRoomState) -> dict[str, Any]:
        """Restore the room's marker where needed; returns its content as it is now."""
        event_type = event_type_name(self.server_name, OnbotRoomType(expected.room_type))
        event = await self.effectors.get_room_state_event(room.room_id, event_type)
        bot_id = self.config.synapse_server.bot_user_id
        repair = marker_repair(event, bot_id=bot_id, expected=expected)
        if repair is None:
            return dict((event or {}).get("content") or {})
        await self.effectors.put_room_state(room.room_id, event_type, repair.content)
        message = f"room {room.room_id}, the {self._describe(expected)}: {repair.reason}; the bot restored it"
        if repair.tampered:
            event_id = (event or {}).get("event_id") or "missing"
            await self._report_once(f"marker|{room.room_id}|{event_id}", message + ".")
        else:
            log.info("%s", message)
        return repair.content

    async def _repair_lock(self, room: MatrixRoom, marker: AnyRoomState) -> None:
        """Raise the marker types' required power level again where somebody lowered it."""
        room_version, _creators = await self._create_info(room)
        bot_id = self.config.synapse_server.bot_user_id
        power = await self.effectors.get_room_power_levels(room.room_id)
        if markers_locked(power, self.server_name, bot_id, room_version=room_version):
            return
        await lock_markers(
            self.effectors, room.room_id, self.server_name, bot_id=bot_id, room_version=room_version
        )
        await self._report_once(
            f"lock|{room.room_id}|{sorted((power.get('events') or {}).items())}",
            f"room {room.room_id}, the {self._describe(marker)}: the power level required to change its "
            "onbot marker had been lowered; the bot raised it again.",
        )

    async def _repair_alias(
        self, room: MatrixRoom, marker: AnyRoomState, alias: str, index: RoomIndex
    ) -> None:
        """Recreate a removed alias when it is free, and the main address when the room has none.

        The alias is presentation only (D20): it never decides which room is which. An alias another
        room holds is reported once and left where it is.
        """
        if room.canonical_alias == alias:
            return  # the cheap check: the room listing names it as the main address
        target = await self.effectors.resolve_room_alias(alias)
        if target is None:
            await self.effectors.set_room_alias(alias, room.room_id)
            log.info("restored the alias %s of room %s, the %s", alias, room.room_id, self._describe(marker))
        elif target != room.room_id:
            other = index.rooms.get(target)
            bot_id = self.config.synapse_server.bot_user_id
            foreign = other is not None and other.creator is not None and other.creator != bot_id
            await self._report_once(
                f"alias|{room.room_id}|{alias}|{target}",
                f"the alias {alias} of room {room.room_id}, the {self._describe(marker)}, belongs to room "
                f"{target}{', which the bot did not create' if foreign else ''}. The bot leaves it there; "
                "the room keeps working without it.",
            )
            return
        if room.canonical_alias is None:
            current = await self.effectors.get_room_state(room.room_id, CANONICAL_ALIAS_EVENT_TYPE) or {}
            if not current.get("alias"):
                await self.effectors.put_room_state(
                    room.room_id, CANONICAL_ALIAS_EVENT_TYPE, {**current, "alias": alias}
                )
                log.info("restored the main address %s of room %s", alias, room.room_id)

    async def _repair_space_link(self, room: MatrixRoom, space_id: str, *, suggested: bool) -> None:
        """Link the room into the space (``m.space.child`` there, ``m.space.parent`` here).

        Covers a creation cut off between ``createRoom`` and the child link, rooms from before the
        space existed, a recreated space, and a lobby whose feature was switched on again.
        """
        child = await self.effectors.get_room_state(space_id, SPACE_CHILD_EVENT_TYPE, room.room_id)
        if not space_child_ok(child, suggested=suggested):
            await self.effectors.put_room_state(
                space_id,
                SPACE_CHILD_EVENT_TYPE,
                space_child_content(self.server_name, suggested=suggested),
                state_key=room.room_id,
            )
            log.info("linked room %s into the space %s", room.room_id, space_id)
        parent = await self.effectors.get_room_state(room.room_id, SPACE_PARENT_EVENT_TYPE, space_id)
        if not space_parent_ok(parent):
            await self.effectors.put_room_state(
                room.room_id,
                SPACE_PARENT_EVENT_TYPE,
                space_parent_content(self.server_name),
                state_key=space_id,
            )
            log.info("set the parent space of room %s to %s", room.room_id, space_id)

    async def _report_once(self, key: str, text: str) -> None:
        """Log ``text`` at WARNING and tell the admin room, once per process for ``key``."""
        if key in self._reported_rooms:
            return
        self._reported_rooms.add(key)
        log.warning("rooms: %s", text)
        await self.events.emit(Signal.room_attention, text=text)

    # --- visitor lobbies that were switched off (REC-10) -----------------------------------------

    async def _converge_switched_off_lobbies(
        self,
        index: RoomIndex,
        group_maps: list[GroupRoomMap],
        space: MatrixRoom | None,
        report: PassReport,
        plan: PassPlan,
    ) -> None:
        """Close and detach the lobby of a live group that no longer wants one.

        ``chatroom_visitor_lobby: false`` on the group, or ``visitor_lobby_enabled`` switched off,
        used to drop the lobby out of management while it stayed open and suggested in the space.
        Now its join rule becomes ``invite`` and it leaves the space. Members stay (nobody is
        kicked); closing counts as a reversible destructive action and goes through the guard (D19).
        A lobby that is closed and detached already costs two reads. A lobby of a group that is gone
        is torn down instead (:meth:`_converge_obsolete_rooms`).
        """
        switched_off = {gm.group_pk for gm in group_maps if gm.lobby_desired is None}
        if not switched_off:
            return
        for identity in index.trusted((OnbotRoomType.visitor_lobby,)):
            assert identity.key is not None and identity.key.group_id is not None
            if identity.key.group_id not in switched_off or index.superseded(identity):
                continue
            await self._guarded(
                report,
                room_kind=OnbotRoomType.visitor_lobby,
                room_id=identity.room_id,
                group_pk=identity.key.group_id,
                step="close",
                work=self._plan_lobby_close(identity.room_id, identity.key.group_id, space, report, plan),
            )

    async def _plan_lobby_close(
        self, room_id: str, group_pk: str, space: MatrixRoom | None, report: PassReport, plan: PassPlan
    ) -> None:
        join_rules = await self.effectors.get_room_state(room_id, JOIN_RULES_EVENT_TYPE) or {}
        still_open = not is_closed(join_rules)
        attached = False
        if space is not None:
            child = await self.effectors.get_room_state(space.room_id, SPACE_CHILD_EVENT_TYPE, room_id)
            attached = space_child_attached(child)
        if not still_open and not attached:
            return
        action = DestructiveAction(
            kind=ActionKind.room_close, target=room_id, room_id=room_id, group_pk=group_pk
        )

        async def close() -> None:
            if still_open:
                await self.effectors.put_room_state(room_id, JOIN_RULES_EVENT_TYPE, closed_join_rules())
            if attached and space is not None:
                # An empty content removes a state event (the spec's way to delete one).
                await self.effectors.put_room_state(
                    space.room_id, SPACE_CHILD_EVENT_TYPE, {}, state_key=room_id
                )
                await self.effectors.put_room_state(
                    room_id, SPACE_PARENT_EVENT_TYPE, {}, state_key=space.room_id
                )
            log.info("closed the visitor lobby %s of group %s (switched off)", room_id, group_pk)

        async def run(allowed: frozenset[str]) -> None:
            await self._guarded(
                report,
                room_kind=OnbotRoomType.visitor_lobby,
                room_id=room_id,
                group_pk=group_pk,
                step="close",
                work=close(),
            )

        plan.defer([action], run)

    def _report_stray_rooms(
        self, index: RoomIndex, group_maps: list[GroupRoomMap], report: PassReport
    ) -> None:
        """Report the bot's marked rooms that nobody adopted (D20).

        A stray room of a live group, the space or the control room is reported on every pass, like a
        room that did not converge (so the admin room hears of it once). A stray room of a group that
        is no longer synced is only logged, once per process: it is history, not a problem.
        """
        live = {gm.group_pk for gm in group_maps}
        for identity in stray_claims(index):
            key = identity.key
            message = describe_stray(identity)
            if key is not None and key.group_id is not None and key.group_id not in live:
                self.rooms.log_once(identity.room_id, f"room {identity.room_id} {message}")
                continue
            report.add(
                room_kind=key.kind.value if key is not None else "unmanaged_room",
                room_id=identity.room_id,
                group_pk=key.group_id if key is not None else None,
                problem=message,
            )

    async def _converge_obsolete_rooms(
        self, index: RoomIndex, group_maps: list[GroupRoomMap], report: PassReport, plan: PassPlan
    ) -> None:
        """Tear down rooms whose mapped Authentik group disappeared (G2.3, the inverse of G2.2).

        A room is obsolete when it is a group room or visitor lobby the bot can vouch for (it created
        the room, and its locked marker names the group, see :mod:`onbot.rooms.identity`) and that
        group no longer survives the sync filters: deleted upstream, or it lost the
        attribute/prefix/parent that opted it into chat. The lobby is torn down with its group room
        (ADR-0012): a blocked group room beside a live, joinable lobby is the worst of both.

        Rooms the bot did not create, rooms without a marker, and marked rooms from older versions
        that were never adopted are never touched. When the state of some bot room could not be read,
        the whole teardown waits for a pass that can see everything.
        """
        settings = self.config.sync_matrix_rooms_based_on_authentik_groups
        if not settings.enabled or not settings.disable_rooms_when_mapped_authentik_group_disappears:
            return
        if not index.complete:
            log.warning(
                "reconcile: obsolete-room teardown skipped this pass: %s", index.describe_incomplete()
            )
            return
        live_group_pks = {gm.group_pk for gm in group_maps}
        for identity in index.trusted((OnbotRoomType.group_room, OnbotRoomType.visitor_lobby)):
            assert identity.key is not None and identity.key.group_id is not None
            if identity.key.group_id in live_group_pks:
                continue
            room = index.rooms.get(identity.room_id) or MatrixRoom(room_id=identity.room_id)
            await self._guarded(
                report,
                room_kind=identity.key.kind,
                room_id=room.room_id,
                group_pk=identity.key.group_id,
                step="teardown",
                work=self._plan_teardown(
                    room, identity.key.group_id, settings, report, identity.key.kind, plan
                ),
            )

    async def _closed_by_the_bot(self, room: MatrixRoom) -> bool:
        """Whether an obsolete room is torn down already: blocked by the bot, nobody but the bot in it.

        Such a room needs no teardown again. Planning one on every pass made every room ever closed
        count against the guard's layer 2 limit forever, so from eleven closed rooms on every kick and
        teardown of every pass waited for a confirmation.
        """
        block = await self.admin.get_room_block(room.room_id)
        bot_id = self.config.synapse_server.bot_user_id
        if not block.blocked or block.by != bot_id:
            return False
        return all(member == bot_id for member in await self.admin.list_room_members(room.room_id))

    async def _plan_teardown(
        self,
        room: MatrixRoom,
        group_id: str,
        settings: SyncMatrixRoomsBasedOnAuthentikGroups,
        report: PassReport,
        room_type: OnbotRoomType,
        plan: PassPlan,
    ) -> None:
        closed = await self._closed_by_the_bot(room)
        if closed and not settings.delete_disabled_rooms:
            return
        self._defer_teardown(room, group_id, settings, report, room_type, plan, closed=closed)

    def _defer_teardown(
        self,
        room: MatrixRoom,
        group_id: str,
        settings: SyncMatrixRoomsBasedOnAuthentikGroups,
        report: PassReport,
        room_type: OnbotRoomType,
        plan: PassPlan,
        *,
        closed: bool = False,
    ) -> None:
        """Record the teardown of an obsolete room, and its deletion when configured.

        The teardown (block, kick) is reversible (guard layer 2), the deletion is not (layer 3, D19).
        They run once the guard permits them, the teardown first; the guard never permits the
        deletion in a pass whose teardown of the same room it holds, and a teardown that fails skips
        the deletion as well, so a room is never deleted with its members still in it unguarded. A
        room the bot ``closed`` in an earlier pass (blocked, empty) gets only the deletion.
        """
        teardown = DestructiveAction(
            kind=ActionKind.room_teardown, target=room.room_id, room_id=room.room_id, group_pk=group_id
        )
        deletion = DestructiveAction(
            kind=ActionKind.room_delete, target=room.room_id, room_id=room.room_id, group_pk=group_id
        )
        actions = [teardown, deletion] if settings.delete_disabled_rooms else [teardown]
        if closed:
            actions = [deletion]

        async def run(allowed: frozenset[str]) -> None:
            if closed:
                torn_down = True
            elif teardown.key not in allowed:
                return  # (the guard holds the deletion as well)
            else:
                torn_down = await self._guarded(
                    report,
                    room_kind=room_type,
                    room_id=room.room_id,
                    group_pk=group_id,
                    step="teardown",
                    work=self._disable_room(room, group_id, report, room_type),
                )
            if torn_down and deletion.key in allowed:
                await self._guarded(
                    report,
                    room_kind=room_type,
                    room_id=room.room_id,
                    group_pk=group_id,
                    step="deletion",
                    work=self._delete_room(room),
                )

        plan.defer(actions, run)

    def _teardown_reason(self) -> str:
        # Without the Authentik URL: it is often internal, and a kick reason is shown to everyone (SEC-11).
        return "The group mapped to this room is no longer synced from the central user directory."

    async def _disable_room(
        self,
        room: MatrixRoom,
        group_id: str,
        report: PassReport,
        room_type: OnbotRoomType,
    ) -> None:
        """Block an obsolete room and clear it of users (reversible; the deletion is separate).

        A member the server refuses to kick (a creator, or a user at the bot's level in a pre-v12
        room) is reported and skipped, so the rest of the room is still cleared.
        """
        reason = self._teardown_reason()
        log.info("disabling room %s: authentik group %s disappeared", room.room_id, group_id)
        block = await self.admin.get_room_block(room.room_id)
        bot_id = self.config.synapse_server.bot_user_id
        if not block.blocked:
            await self.admin.room_set_blocked(room.room_id, blocked=True)
        elif block.by != bot_id:
            # Blocking again would record the bot as the blocker, and a group that comes back would
            # then unblock a room a server admin blocked (REC-11).
            log.info("room %s is blocked by %s already; leaving that block as it is", room.room_id, block.by)

        problems: list[str] = []
        members = [m for m in await self.admin.list_room_members(room.room_id) if m != bot_id]
        await self._kick_members(
            room.room_id, await self._kickable(room, members, problems), reason, problems
        )
        report.extend(room_kind=room_type, room_id=room.room_id, group_pk=group_id, problems=problems)

    async def _delete_room(self, room: MatrixRoom) -> None:
        """Delete an obsolete room for good (``delete_disabled_rooms``); irreversible."""
        log.warning("deleting room %s (delete_disabled_rooms is enabled)", room.room_id)
        await self.admin.delete_room(room.room_id, block=True, purge=True, message=self._teardown_reason())

    async def _converge_space(self, space: MatrixRoom, users: list[MappedUser], index: RoomIndex) -> None:
        cfg = self.config.create_matrix_rooms_in_a_matrix_space
        await self._converge_room_upkeep(
            space,
            SpaceRoomState(configured_alias=cfg.alias),
            alias=build_canonical(cfg.alias, self.server_name, "#"),
            alias_usable=True,
            avatar_source_url=cfg.create_matrix_space_if_not_exists.avatar_url,
            index=index,
        )
        if not self.config.sync_authentik_users_with_matrix_rooms.enabled:
            return  # user sync off: membership is not managed anywhere (REC-02)
        problems: list[str] = []
        members = await self.admin.list_room_members(space.room_id)
        diff = diff_space_membership(users, members, always_members=self.global_admins.mxids)
        await self._add_members(space.room_id, diff.to_add, problems)
        if problems:
            raise _RoomStepProblems(problems)

    async def _add_members(self, room_id: str, mxids: list[str], problems: list[str]) -> None:
        """Join each user; a refusal (banned, room gone, no account) skips only that user."""
        for mxid in mxids:
            try:
                await self.admin.add_user_to_room(room_id, mxid)
            except ApiError as exc:
                problems.append(f"could not join {mxid}: {describe_error(exc)}")

    async def _kick_members(self, room_id: str, mxids: list[str], reason: str, problems: list[str]) -> None:
        """Kick each user; a refusal (a creator, a user at the bot's level) skips only that user."""
        for mxid in mxids:
            try:
                await self.effectors.kick_user(room_id, mxid, reason)
            except ApiError as exc:
                problems.append(f"could not kick {mxid}: {describe_error(exc)}")

    async def _converge_room_membership_and_levels(
        self,
        group_maps: list[GroupRoomMap],
        users: list[MappedUser],
        space: MatrixRoom | None,
        report: PassReport,
        plan: PassPlan,
    ) -> None:
        room_cfg = self.config.sync_matrix_rooms_based_on_authentik_groups
        # `enabled: false` means "do not manage membership", never "everyone is unwanted" (REC-02,
        # MNT-01): with user sync off the mapped set is empty by construction, and projecting it would
        # kick every member of every room. Rooms themselves (name, topic, lobby join rule) still converge.
        sync_cfg = self.config.sync_authentik_users_with_matrix_rooms
        manage_members = sync_cfg.enabled and self.identity.unavailable is None
        pl_groups: list[PowerLevelGroup] = []
        kick_enabled = False
        if manage_members:
            pl_groups = extract_power_level_groups(
                await self.authentik.list_groups(
                    filter_has_non_empty_attributes=[room_cfg.authentik_group_attr_for_matrix_power_level]
                ),
                room_cfg.authentik_group_attr_for_matrix_power_level,
            )
            # Kicks are planned here and decided on by the guard (D4, D13) before any of them runs.
            kick_enabled = sync_cfg.kick_matrix_room_members_not_in_mapped_authentik_group_anymore
        else:
            log.info("reconcile: user sync is disabled; room membership and power levels are left alone")

        for gm in group_maps:
            if gm.room is None:  # its creation failed this pass (already reported)
                continue
            room = gm.room
            steps: list[tuple[str, Awaitable[None]]] = []
            if room.room_id in self._blocked_by_others:
                pass  # a server admin blocked it: left alone (REC-11, reported once)
            elif manage_members:
                steps.append(
                    (
                        "membership",
                        self._converge_group_room_membership(gm, room, users, kick_enabled, report, plan),
                    )
                )
                steps.append(
                    (
                        "power levels",
                        self._converge_power_levels(
                            room, gm.group_pk, users, pl_groups, room_cfg, report, plan
                        ),
                    )
                )
            if room.room_id not in self._blocked_by_others:
                steps.append(
                    ("name and topic", self._converge_room_attributes(room, gm.desired, gm.group_pk))
                )
            await self._run_room_steps(report, OnbotRoomType.group_room, room.room_id, gm.group_pk, steps)

            if gm.lobby is not None and gm.lobby.room_id not in self._blocked_by_others:
                lobby = gm.lobby
                assert gm.lobby_desired is not None  # a lobby only exists where one was computed
                lobby_steps: list[tuple[str, Awaitable[None]]] = []
                if manage_members:
                    lobby_steps.append(("membership", self._converge_lobby_membership(gm, lobby, users)))
                    lobby_steps.append(
                        ("power levels", self._converge_lobby_power_levels(gm, lobby, report, plan))
                    )
                lobby_steps.append(("join rule", self._converge_lobby_join_rules(lobby, space, gm.group_pk)))
                lobby_steps.append(
                    ("name and topic", self._converge_room_attributes(lobby, gm.lobby_desired, gm.group_pk))
                )
                await self._run_room_steps(
                    report, OnbotRoomType.visitor_lobby, lobby.room_id, gm.group_pk, lobby_steps
                )

    async def _run_room_steps(
        self,
        report: PassReport,
        room_kind: OnbotRoomType,
        room_id: str,
        group_pk: str,
        steps: list[tuple[str, Awaitable[None]]],
    ) -> None:
        """Run a room's independent steps, each isolated: a failed step does not skip the next."""
        for step, work in steps:
            await self._guarded(
                report, room_kind=room_kind, room_id=room_id, group_pk=group_pk, step=step, work=work
            )

    async def _converge_group_room_membership(
        self,
        gm: GroupRoomMap,
        room: MatrixRoom,
        users: list[MappedUser],
        kick_enabled: bool,
        report: PassReport,
        plan: PassPlan,
    ) -> None:
        bot_id = self.config.synapse_server.bot_user_id
        global_admins = self.global_admins.mxids
        actual_members = await self.admin.list_room_members(room.room_id)
        desired_mxids = desired_room_members(gm.group_pk, users, always_members=global_admins)

        mdiff = diff_room_membership(
            desired_mxids,
            actual_members,
            kick_enabled=kick_enabled,
            # `matrix_user_ignore_list` promises these accounts are never touched: server admins,
            # bridges and other bots that sit in group rooms without an Authentik counterpart (REC-07).
            # An account several Authentik users resolve to is left alone until an admin decides (D8).
            protected_ids=[bot_id, *global_admins, *self.config.matrix_user_ignore_list, *self._held_mxids],
        )
        problems: list[str] = []
        await self._add_members(room.room_id, mdiff.to_add, problems)
        kickable = await self._kickable(room, mdiff.to_kick, problems)
        self._defer_kicks(room.room_id, gm.group_pk, kickable, report, plan)
        if problems:
            raise _RoomStepProblems(problems)

    def _defer_kicks(
        self, room_id: str, group_pk: str, mxids: list[str], report: PassReport, plan: PassPlan
    ) -> None:
        """Record the kicks of one group room; the permitted ones run after the guard's verdict."""
        actions = [
            DestructiveAction(kind=ActionKind.kick, target=mxid, room_id=room_id, group_pk=group_pk)
            for mxid in mxids
        ]
        by_key = {a.key: a.target for a in actions}

        async def run(allowed: frozenset[str]) -> None:
            async def kick() -> None:
                problems: list[str] = []
                await self._kick_members(
                    room_id,
                    sorted(by_key[k] for k in allowed),
                    "Removed: missing/revoked group membership in the central user directory.",
                    problems,
                )
                if problems:
                    raise _RoomStepProblems(problems)

            await self._guarded(
                report,
                room_kind=OnbotRoomType.group_room,
                room_id=room_id,
                group_pk=group_pk,
                step="membership",
                work=kick(),
            )

        plan.defer(actions, run)

    async def _converge_lobby_membership(
        self, gm: GroupRoomMap, lobby: MatrixRoom, users: list[MappedUser]
    ) -> None:
        """Converge a lobby's membership, add-only (ADR-0012).

        A lobby is **add-only**: ``kick_enabled=False``, so nobody is ever removed — a user kicked
        from the group room keeps their seat here, and visitors who joined on purpose stay. When
        ``visitor_lobby_inject_group_members`` is set (the default) the group's members are seeded in;
        visitors are never injected anywhere. Global room admins are seeded regardless of that switch:
        it governs whether the *group* is mirrored here, and they are not seeded as group members.
        """
        bot_id = self.config.synapse_server.bot_user_id
        settings = resolve_room_settings(gm.group_pk, self.config)

        global_admins = self.global_admins.mxids
        desired_mxids = desired_room_members(
            gm.group_pk,
            users if settings.visitor_lobby_inject_group_members else [],
            always_members=global_admins,
        )
        actual_members = await self.admin.list_room_members(lobby.room_id)
        mdiff = diff_room_membership(
            desired_mxids, actual_members, kick_enabled=False, protected_ids=[bot_id, *global_admins]
        )
        problems: list[str] = []
        await self._add_members(lobby.room_id, mdiff.to_add, problems)
        if problems:
            raise _RoomStepProblems(problems)

    async def _converge_lobby_power_levels(
        self, gm: GroupRoomMap, lobby: MatrixRoom, report: PassReport, plan: PassPlan
    ) -> None:
        # Only the global admins get a level here: a lobby is not a projection of the group, so the
        # group's power-level attributes deliberately do not reach into it.
        await self._converge_levels(
            lobby,
            OnbotRoomType.visitor_lobby,
            VisitorLobbyRoomState(group_id=gm.group_pk),
            gm.group_pk,
            {},
            report,
            plan,
        )

    async def _converge_lobby_join_rules(
        self, lobby: MatrixRoom, space: MatrixRoom | None, group_pk: str
    ) -> None:
        """Converge a lobby's ``restricted`` join rule (ADR-0012), except where a human closed it (D16).

        The join-rule pass runs over existing lobbies as well as new ones and rewrites the rule only
        on a difference (a no-op tick sends nothing). The bot is PL 100 with ``state_default`` 100 so
        it may write the rule, but a hand-edited room can still 403: that is reported for the lobby
        and the pass carries on.

        A lobby somebody other than the bot closed (``invite``, ``knock``, ``private``) stays closed:
        the rule's sender tells whose change it was, so this needs no record of its own and survives
        restarts. It is logged at WARNING and reported once; ``!lobby reopen`` hands the lobby back to
        the bot (:meth:`reopen_lobby`). A closed rule the bot wrote (a lobby it closed while its
        feature was off) is reopened, and so is any rule that leaves the lobby open in another way.
        """
        if space is None:  # cannot express `restricted` without a space to restrict to
            return
        desired = desired_join_rules(OnbotRoomType.visitor_lobby, space.room_id)
        current = await self.effectors.get_room_state(lobby.room_id, JOIN_RULES_EVENT_TYPE) or {}
        change = join_rules_change(current, desired)
        if change is None:
            return
        if is_closed(current):
            event = await self.effectors.get_room_state_event(lobby.room_id, JOIN_RULES_EVENT_TYPE) or {}
            sender = event.get("sender")
            if sender != self.config.synapse_server.bot_user_id:
                await self._report_once(
                    f"closed|{lobby.room_id}|{event.get('event_id')}",
                    f"the visitor lobby {lobby.room_id} of group {group_pk} was closed by "
                    f"{sender or 'an unknown account'} (join rule {current.get('join_rule')!r}). It stays "
                    f"closed; an admin reopens it with `!lobby reopen {group_pk}` in the admin control room.",
                )
                return
        await self.effectors.put_room_state(lobby.room_id, JOIN_RULES_EVENT_TYPE, change)

    async def reopen_lobby(self, target: str) -> str:
        """``!lobby reopen <group, room id or alias>``: withdraw a manual close (D16); returns the reply.

        Sets the lobby's join rule back to ``restricted`` as the bot, so from then on the bot manages
        it from the configuration again. Only a lobby the last pass managed with its feature on can be
        reopened; one switched off in the configuration stays closed.
        """
        target = target.strip()
        if not target:
            return "Usage: !lobby reopen <group pk, lobby room id or lobby alias>"
        room_id: str | None = None
        if target.startswith("!"):
            room_id = target
        elif target.startswith("#"):
            room_id = await self.effectors.resolve_room_alias(target)
        else:
            room_id = next((r for r, g in self.active_lobbies.items() if g == target), None)
        space_id = self.rooms.space_id
        if room_id is None or room_id not in self.active_lobbies or space_id is None:
            return (
                f"{target} is not a visitor lobby the bot manages right now (unknown, switched off in "
                "the configuration, or no reconcile pass has finished yet). Nothing was changed."
            )
        group_pk = self.active_lobbies[room_id]
        desired = desired_join_rules(OnbotRoomType.visitor_lobby, space_id)
        event = await self.effectors.get_room_state_event(room_id, JOIN_RULES_EVENT_TYPE) or {}
        current = event.get("content") or {}
        if not is_closed(current) or event.get("sender") == self.config.synapse_server.bot_user_id:
            return (
                f"The visitor lobby {room_id} of group {group_pk} is not closed by hand "
                f"(join rule {current.get('join_rule')!r}); the bot manages it already."
            )
        await self.effectors.put_room_state(room_id, JOIN_RULES_EVENT_TYPE, dict(desired or {}))
        log.warning("reopened the visitor lobby %s of group %s on an admin's request", room_id, group_pk)
        self.trigger()
        return (
            f"Reopened the visitor lobby {room_id} of group {group_pk}: members of the space can join it "
            "again, and the bot manages it from the configuration."
        )

    async def _converge_power_levels(
        self,
        room: MatrixRoom,
        group_pk: str,
        users: list[MappedUser],
        pl_groups: list[PowerLevelGroup],
        room_cfg: SyncMatrixRoomsBasedOnAuthentikGroups,
        report: PassReport,
        plan: PassPlan,
    ) -> None:
        members_in_room = [u for u in users if group_pk in u.group_pks]
        desired = compute_desired_user_levels(
            members_in_room,
            pl_groups,
            make_superusers_admin=room_cfg.make_authentik_superusers_matrix_room_admin,
        )
        await self._converge_levels(
            room,
            OnbotRoomType.group_room,
            GroupRoomState(group_id=group_pk),
            group_pk,
            desired,
            report,
            plan,
        )

    async def _converge_levels(
        self,
        room: MatrixRoom,
        room_type: OnbotRoomType,
        fresh_state: AnyRoomState,
        group_pk: str,
        desired: dict[str, int],
        report: PassReport,
        plan: PassPlan,
    ) -> None:
        """Grant now; record the withdrawals (D10) for the guard and carry out the permitted ones later.

        A user whose level the bot may not change does not hold up the others: their problem is
        reported, and the other withdrawals still go to the guard.
        """
        problems: _RoomStepProblems | None = None
        try:
            held = await self._write_power_levels(room, room_type, fresh_state, desired=desired)
        except _RoomStepProblems as exc:
            problems, held = exc, exc.held
        # A held withdrawal of an account several Authentik users resolve to is never proposed: its
        # level stays until an admin resolves the conflict (D8).
        actions = [
            DestructiveAction(
                kind=ActionKind.withdraw_power_level, target=mxid, room_id=room.room_id, group_pk=group_pk
            )
            for mxid in held
            if mxid not in self._held_mxids
        ]
        by_key = {a.key: a.target for a in actions}

        async def run(allowed: frozenset[str]) -> None:
            await self._guarded(
                report,
                room_kind=room_type,
                room_id=room.room_id,
                group_pk=group_pk,
                step="power levels",
                work=self._write_power_levels(
                    room,
                    room_type,
                    fresh_state,
                    desired=desired,
                    withdraw=frozenset(by_key[k] for k in allowed),
                ),
            )

        plan.defer(actions, run)
        if problems is not None:
            raise problems

    async def _write_power_levels(
        self,
        room: MatrixRoom,
        room_type: OnbotRoomType,
        fresh_state: AnyRoomState,
        *,
        desired: dict[str, int],
        withdraw: Collection[str] = frozenset(),
    ) -> list[str]:
        """Apply ``desired`` levels plus the global room admins at PL 100, and withdraw stale grants.

        Withdrawal follows D10: only entries the bot itself wrote are taken back. They are recorded,
        with the level written, in the bot's per-room account data (:class:`GrantedPowerLevels`),
        which no room member can edit. The ``global_admins`` list in the room's onbot state event
        predates that record; it is still read as a withdrawal source (without a level) and kept up
        to date, so rooms from older versions lose a removed admin's level as before.

        What the bot may not change (a creator, a user at or above its level in a pre-v12 room) is
        skipped and reported instead of failing the whole write (REC-05), and grants are capped one
        below the bot's own level where that level is finite. A steady state costs one state read, one
        account data read and one power-level read, and no writes.

        A withdrawal or demotion is destructive and goes through the guard (D13): only the users in
        ``withdraw`` get a lower level here. Every other one is held: the entry stays at its level
        (see :func:`hold_withdrawals`), and its MXID is returned so the caller can ask the guard.
        """
        room_id = room.room_id
        bot_id = self.config.synapse_server.bot_user_id
        global_admins = self.global_admins.mxids
        event_type = event_type_name(self.server_name, room_type)
        raw = await self.effectors.get_room_state(room_id, event_type)
        state = parse_room_state(room_type, raw) if raw else fresh_state
        legacy_admins = frozenset(state.global_admins)
        record = await self._load_grant_record(room_id)
        recorded: dict[str, int | None] = {**dict.fromkeys(legacy_admins - record.keys()), **record}

        desired = apply_global_room_admins(desired, global_admins)
        if not desired and not recorded:
            return []
        power = await self.effectors.get_room_power_levels(room_id)
        current_users = dict(power.get("users") or {})
        authority = await self._room_authority(room, power)
        plan = hold_withdrawals(
            plan_power_levels(current_users, desired, recorded, authority, bot_id=bot_id),
            current_users,
            recorded,
            withdraw,
            users_default=authority.users_default,
        )

        for mxid in plan.released:
            log.info(
                "power level of %s in %s was changed by someone else since the bot set it; "
                "leaving it to them",
                mxid,
                room_id,
            )
        if plan.users != current_users:
            if plan.capped:
                log.warning(
                    "room %s (version %s): the bot is at power level %s here, so it grants at most %s: %s",
                    room_id,
                    room.room_version,
                    authority.bot_level,
                    authority.max_grant,
                    ", ".join(f"{m} (wanted {lvl})" for m, lvl in sorted(plan.capped.items())),
                )
            # Record new grants before writing them: a crash between the two must never leave a
            # level the bot set but does not know it owns.
            interim = {**record, **plan.granted}
            if interim != record:
                await self._save_grant_record(room_id, interim)
                record = interim
            await self.effectors.set_room_power_levels(room_id, {**power, "users": plan.users})
        if plan.granted != record:
            await self._save_grant_record(room_id, plan.granted)

        if legacy_admins != global_admins:
            state.global_admins = sorted(global_admins)
            await self.effectors.put_room_state(room_id, event_type, dump_room_state(state))

        if plan.blocked:
            raise _RoomStepProblems(
                [f"cannot change power level of {mxid}: {why}" for mxid, why in sorted(plan.blocked.items())],
                held=plan.held,
            )
        return list(plan.held)

    async def _load_grant_record(self, room_id: str) -> dict[str, int]:
        bot_id = self.config.synapse_server.bot_user_id
        raw = await self.effectors.get_room_account_data(
            bot_id, room_id, granted_power_levels_type(self.server_name)
        )
        if not raw:
            return {}
        try:
            return dict(GrantedPowerLevels.model_validate(raw).users)
        except ValidationError:
            # Withdrawing on a guess is the destructive direction; an unreadable record withdraws
            # nothing and is replaced by the next write.
            log.warning("unreadable power-level record for %s; treating it as empty", room_id)
            return {}

    async def _save_grant_record(self, room_id: str, users: dict[str, int]) -> None:
        bot_id = self.config.synapse_server.bot_user_id
        await self.effectors.put_room_account_data(
            bot_id,
            room_id,
            granted_power_levels_type(self.server_name),
            GrantedPowerLevels(users=dict(sorted(users.items()))).model_dump(mode="json"),
        )

    async def _room_authority(self, room: MatrixRoom, power_levels: dict[str, Any]) -> RoomAuthority:
        """What the bot may change in ``room``, from its power levels and (cached) create event."""
        room_version, creators = await self._create_info(room)
        return room_authority(
            power_levels,
            self.config.synapse_server.bot_user_id,
            room_version=room_version,
            creators=creators,
        )

    async def _create_info(self, room: MatrixRoom) -> tuple[str | None, frozenset[str]]:
        """A room's version and creators. Immutable per room id, so read once per process.

        The version and the creator come with the admin room listing; a room created during this pass
        is looked up once. Only rooms with privileged creators (v12+) need the create event itself,
        for ``additional_creators`` (who may arrive through ``chatroom_params``, D6).
        """
        cached = self._create_info_cache.get(room.room_id)
        if cached is not None:
            return cached
        room_version, creator = room.room_version, room.creator
        if room_version is None or creator is None:
            details = await self.admin.get_room_details(room.room_id)
            version = details.get("version")
            room_version = room_version or (str(version) if version is not None else None)
            creator = creator or details.get("creator")
        creators: set[str] = {creator} if creator else set()
        if creators_are_privileged(room_version):
            content = await self.effectors.get_room_state(room.room_id, CREATE_EVENT_TYPE) or {}
            extra = content.get("additional_creators")
            if isinstance(extra, list):
                creators |= {c for c in extra if isinstance(c, str)}
        info = (room_version, frozenset(creators))
        self._create_info_cache[room.room_id] = info
        return info

    async def _kickable(self, room: MatrixRoom, mxids: list[str], problems: list[str]) -> list[str]:
        """The subset of ``mxids`` the bot is allowed to kick; the rest are reported (REC-05, D6).

        Kicking needs a level strictly above the target's, and nobody can kick a v12 creator. Trying
        anyway only earns a 403 on every pass.
        """
        if not mxids:
            return []
        power = await self.effectors.get_room_power_levels(room.room_id)
        authority = await self._room_authority(room, power)
        users = power.get("users") or {}
        kickable: list[str] = []
        for mxid in mxids:
            why = authority.why_untouchable(mxid, authority.level_of(users, mxid))
            if why is None:
                kickable.append(mxid)
            else:
                problems.append(f"cannot kick {mxid}: {why}")
        return kickable

    async def _converge_room_attributes(
        self, room: MatrixRoom, desired: RoomCreateAttributes, group_pk: str
    ) -> None:
        """Repair a room's name and topic when they drift from Authentik (G6.*).

        Runs for a group room and for its visitor lobby alike: a lobby's name and topic are derived
        from the group room's (see :func:`~onbot.reconciler.rooms.compute_lobby_attributes`), so a
        group renamed in Authentik has to carry through to both or the pair stops reading as one place.

        The name comes free with the room listing, but the **topic does not**: the admin List Room API
        returns ``name`` and ``canonical_alias`` and no topic at all (only the per-room Room Details
        API carries one). So the topic is read from room state before writing, the same way the admin
        control room repairs its own — otherwise it is compared against a field that is always
        ``None``, and every tick rewrites every topic.

        The switch is read per group, through :func:`resolve_room_settings` like every other field of
        ``MatrixDynamicRoomSettings``, so a group override applies to the group room and its lobby.
        """
        if not resolve_room_settings(group_pk, self.config).keep_updating_matrix_attributes_from_authentik:
            return
        if desired.name is not None and room.name != desired.name:
            await self.effectors.set_room_name(room.room_id, desired.name)
            log.info("renamed %s: %r -> %r", room.room_id, room.name, desired.name)
        if desired.topic is None:  # no topic wanted: leave whatever the room carries alone
            return
        current = await self.effectors.get_room_state(room.room_id, TOPIC_EVENT_TYPE) or {}
        if current.get("topic") != desired.topic:
            await self.effectors.set_room_topic(room.room_id, desired.topic)
            log.info("set topic of %s from Authentik", room.room_id)

    # --- lifecycle (AD-5, G9.*): quarantined, invoked only from the reconcile result ---

    async def _plan_lifecycle(
        self,
        matrix_users: list[dict[str, Any]],
        active_mxids: set[str],
        plan: PassPlan,
        inputs: _PassInputs,
    ) -> DirectorySnapshot | None:
        """Read the directory for the lifecycle and add its locks and erasures to ``plan``.

        Returns the snapshot the lifecycle runs on after the guard's verdict, or ``None`` when it does
        not run this pass.
        """
        if self.lifecycle is None:
            return None
        sync_cfg = self.config.sync_authentik_users_with_matrix_rooms
        if not sync_cfg.deactivate_disabled_authentik_users_in_matrix.enabled:
            return None
        snapshot = await self._gather_lifecycle_snapshot(matrix_users, active_mxids)
        if snapshot is None:
            return None
        if inputs.users_in_scope is None:
            # User sync is off, so the room stage did not count: the same scope, counted here.
            inputs.users_in_scope = len(
                {u.mxid for u in snapshot.users if u.is_active and u.in_scope and not u.ignored and u.mxid}
            )
        steps = await self.lifecycle.plan_accounts(snapshot)
        if steps is None:
            inputs.lifecycle_refused = True  # reconcile_accounts aborts the step and reports it
            return snapshot
        plan.note(
            [DestructiveAction(kind=_LIFECYCLE_ACTION_KINDS[step.action], target=step.mxid) for step in steps]
        )
        return snapshot

    async def _run_lifecycle(self, snapshot: DirectorySnapshot, decision: GuardDecision) -> None:
        """Run the lifecycle; it takes a lock or erasure only when the guard permitted it."""
        assert self.lifecycle is not None

        def permit(mxid: str, action: LifecycleAction) -> bool:
            if action is LifecycleAction.forget:
                return not decision.input_implausible  # "gone from Authentik" must be trusted
            kind = _LIFECYCLE_ACTION_KINDS.get(action)
            return kind is not None and decision.permits(DestructiveAction(kind=kind, target=mxid))

        for outcome in await self.lifecycle.reconcile_accounts(snapshot, permit=permit):
            if outcome.needs_attention:
                await self.events.emit(
                    Signal.lifecycle_attention,
                    mxid=outcome.mxid,
                    action=outcome.action.value,
                    detail=outcome.detail,
                )

    async def _gather_lifecycle_snapshot(
        self, matrix_users: list[dict[str, Any]], active_mxids: set[str]
    ) -> DirectorySnapshot | None:
        """The Authentik directory and the Matrix accounts, as the lifecycle needs them (G9.1).

        Every Authentik user, active or not and in scope or not, so the lifecycle can tell "disabled",
        "deleted", "out of scope" and "active elsewhere" apart; the scope comes from the same query
        helper the room sync uses. The lifecycle itself decides what to do with each account and only
        ever acts on accounts it can tie to an Authentik user (see ``onbot.lifecycle.accounts``).

        Returns ``None``, and the step is skipped, when Authentik answers with no users at all: a
        token that lost its view permission gets an empty ``200``, and judging every account on that
        would be wrong. (The destructive-action guard catches a partial answer, see ``onbot.guard``.)
        """
        everyone = await self.authentik.list_users(filter_is_active=None)
        if not everyone:
            log.warning(EMPTY_DIRECTORY_MESSAGE)
            await self.events.emit(
                Signal.lifecycle_attention, mxid=None, action="abort", detail=EMPTY_DIRECTORY_MESSAGE
            )
            return None
        in_scope = {str(u["pk"]) for u in await self._list_scoped_users(filter_is_active=None)}
        ignored = set(self.config.authentik_user_ignore_list)
        resolution: UserResolution
        if self.identity.unavailable is not None:
            # The lifecycle reads the same record and refuses its step; the mapping attribute only
            # sizes the directory for the guard's plausibility check here.
            resolution = UserResolution(
                mxids={str(u["pk"]): m for u in everyone if (m := attribute_mxid(u, self.config)) is not None}
            )
        else:
            resolution = await self.identity.resolve(
                everyone,
                bind=[
                    str(u["pk"])
                    for u in everyone
                    if u.get("is_active") is True
                    and str(u["pk"]) in in_scope
                    and not is_ignored(u, self.config)
                ],
                matrix_mxids={u["name"] for u in matrix_users},
            )
        users: list[DirectoryUser] = []
        for user in everyone:
            pk = str(user["pk"])
            is_active = user.get("is_active") is True
            # A user in a conflict gets no MXID here; the account itself is held below (D8).
            mxid = resolution.mxid(pk)
            users.append(
                DirectoryUser(
                    pk=pk,
                    username=str(user.get("username", "")),
                    mxid=mxid,
                    is_active=is_active,
                    in_scope=pk in in_scope,
                    ignored=user.get("username") in ignored,
                )
            )
        return DirectorySnapshot(
            users=tuple(users),
            matrix_mxids=frozenset(u["name"] for u in matrix_users),
            excluded_mxids=frozenset(
                {self.config.synapse_server.bot_user_id, *self.config.matrix_user_ignore_list}
            ),
            synced_mxids=frozenset(active_mxids),
            ignored_usernames=frozenset(ignored),
            held_mxids=resolution.held_mxids | self._held_mxids,
        )
