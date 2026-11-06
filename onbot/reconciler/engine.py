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
from onbot.identity import build_canonical, compute_mxid
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
from onbot.reconciler.report import PassReport, describe_error
from onbot.reconciler.rooms import build_group_room_maps, resolve_room_settings
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

log = get_logger(__name__)

JOIN_RULES_EVENT_TYPE = "m.room.join_rules"
CREATE_EVENT_TYPE = "m.room.create"
TOPIC_EVENT_TYPE = "m.room.topic"


class ConfigurationError(RuntimeError):
    """Raised when the configuration cannot be satisfied (e.g. required space missing)."""


_LIFECYCLE_ACTION_KINDS = {LifecycleAction.logout: ActionKind.lock, LifecycleAction.erase: ActionKind.erase}


@dataclass(slots=True)
class _Deferred:
    """Destructive actions of one room step, and how to carry out the permitted ones later."""

    keys: frozenset[str]
    run: Callable[[frozenset[str]], Awaitable[object]]


@dataclass(slots=True)
class PassPlan:
    """Everything a pass would destroy, collected before anything is destroyed (D4, D13).

    The room stage records its kicks, power-level withdrawals and room teardowns here instead of
    carrying them out; the lifecycle adds its locks and erasures. The guard then decides on the whole
    list, and :meth:`execute` carries out the permitted part.
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
        self.global_admins = GlobalRoomAdminResolver(authentik, config)
        # Unix timestamp of the last pass that ran to completion; reported by the admin room's
        # `!status` command. ``None`` until the first pass finishes.
        self.last_reconcile_at: float | None = None
        # Passes the run loop has begun, so a caller can tell "not started yet" from "missed one".
        self.passes_started = 0
        # What the last pass could not converge, per room (REC-03). Empty report before the first pass.
        self.last_pass_report = PassReport()
        # room id -> (room version, creators). The create event never changes, so one read per room.
        self._create_info_cache: dict[str, tuple[str | None, frozenset[str]]] = {}
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
        users, users_in_scope = await self._gather_mapped_users(matrix_users)
        # Before any membership or power-level decision: every one of them consults this set, and an
        # admin that resolves halfway through would land in some rooms and not others.
        await self.global_admins.resolve(users, {u["name"] for u in matrix_users})

        # Rooms and offboarding are independent stages (SEC-04, LIF-04): a room the bot cannot
        # manage, or a room stage that fails as a whole (a missing space, a Synapse 500 on the room
        # list), must never keep disabled users signed in. Every room is isolated inside the stage;
        # a stage-wide failure is held until the rest has run, then fails the pass as before.
        #
        # Nothing destructive happens inside the stages (D4, D13): they record their kicks,
        # withdrawals, teardowns, locks and erasures in `plan`, the guard decides on all of them at
        # once, and only then is the permitted part carried out.
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
                await self.guard.record(decision, plan.actions, complete=not stage_errors)
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
                "%sguard: paused %d destructive action(s) this pass (%s): %s. An admin releases them "
                "with !confirm <batch id> in the admin control room (!pending lists the batches)",
                prefix,
                len(decision.held),
                summarize_actions(decision.held),
                "; ".join(decision.reasons),
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
        space = await self._resolve_space()
        rooms = await self._gather_group_rooms()
        group_maps = await self._gather_group_room_maps(rooms)
        if self.config.sync_matrix_rooms_based_on_authentik_groups.enabled:
            inputs.mapped_groups = len(group_maps)

        await self._converge_rooms(group_maps, space, report)
        await self._converge_obsolete_rooms(rooms, group_maps, report, plan)
        if space is not None:
            await self._guarded(
                report,
                room_kind=OnbotRoomType.space,
                room_id=space.room_id,
                group_pk=None,
                step="space",
                work=self._converge_space(space, users),
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
        if not cfg.enabled:
            return [], None
        by_mxid: dict[str, dict[str, Any]] = {}
        for user in await self._list_scoped_users(filter_is_active=True):
            if user["username"] in self.config.authentik_user_ignore_list:
                continue
            try:
                mxid = compute_mxid(
                    user,
                    username_attribute=cfg.authentik_username_mapping_attribute,
                    server_name=self.server_name,
                )
            except KeyError:
                log.warning("cannot map MXID for Authentik user %r; skipping", user.get("pk"))
                continue
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

    async def _gather_group_rooms(self) -> list[MatrixRoom]:
        if not self.config.sync_matrix_rooms_based_on_authentik_groups.enabled:
            return []
        return [MatrixRoom.from_admin_api(r) for r in await self.admin.list_non_space_rooms()]

    async def _gather_group_room_maps(self, rooms: list[MatrixRoom]) -> list[GroupRoomMap]:
        settings = self.config.sync_matrix_rooms_based_on_authentik_groups
        if not settings.enabled:
            return []
        groups = await self.authentik.list_groups(filter_by_attribute=settings.only_groups_with_attributes)
        return build_group_room_maps(groups, rooms, self.config, self.server_name)

    async def _resolve_space(self) -> MatrixRoom | None:
        cfg = self.config.create_matrix_rooms_in_a_matrix_space
        if not cfg.enabled:
            return None
        target_alias = build_canonical(cfg.alias, self.server_name, "#")
        for sp in await self.admin.list_spaces():
            if sp.get("canonical_alias") == target_alias:
                return MatrixRoom.from_admin_api(sp, is_space=True)
        if not cfg.create_matrix_space_if_not_exists.enabled:
            raise ConfigurationError(
                f"space {target_alias!r} not found and auto-creation is disabled "
                "(create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled)"
            )
        create = cfg.create_matrix_space_if_not_exists
        room_id = await self.effectors.create_space(
            alias=cfg.alias, name=create.name, topic=create.topic, params=create.space_params
        )
        await self.effectors.put_room_state(
            room_id,
            event_type_name(self.server_name, OnbotRoomType.space),
            dump_room_state(SpaceRoomState(authentik_server=self.config.authentik_server.url)),
        )
        return MatrixRoom(room_id=room_id, canonical_alias=target_alias, is_space=True)

    async def _converge_avatar(
        self, room_id: str, room_type: OnbotRoomType, desired: str | None, fresh_state: AnyRoomState
    ) -> None:
        """Set/update a room or space icon (``m.room.avatar``) from a source URL (G6.8).

        Re-uploads only when the configured URL differs from the one recorded in the room's onbot
        state event (``avatar_source_url``), so a stable URL costs one state read per tick and no
        upload. Applies to already-existing rooms too, not just freshly created ones. Best-effort:
        a fetch/upload failure is logged and retried next tick. Clearing the source URL stops future
        updates but leaves the current icon in place (removing avatars is out of scope). ``fresh_state``
        seeds the state event (its ``group_id``/``authentik_server``) when the room has none yet.
        """
        event_type = event_type_name(self.server_name, room_type)
        raw = await self.effectors.get_room_state(room_id, event_type)
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

    async def _converge_space_avatar(self, space: MatrixRoom) -> None:
        space_cfg = self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists
        await self._converge_avatar(
            space.room_id,
            OnbotRoomType.space,
            space_cfg.avatar_url,
            SpaceRoomState(authentik_server=self.config.authentik_server.url),
        )

    async def _converge_rooms(
        self, group_maps: list[GroupRoomMap], space: MatrixRoom | None, report: PassReport
    ) -> None:
        parent_space_id = space.room_id if space else None
        for gm in group_maps:
            await self._guarded(
                report,
                room_kind=OnbotRoomType.group_room,
                room_id=gm.room.room_id if gm.room else None,
                group_pk=gm.group_pk,
                step="room setup",
                work=self._converge_group_room_existence(gm, parent_space_id),
            )
            if gm.room is None or gm.lobby_desired is None:
                continue
            await self._guarded(
                report,
                room_kind=OnbotRoomType.visitor_lobby,
                room_id=gm.lobby.room_id if gm.lobby else None,
                group_pk=gm.group_pk,
                step="lobby setup",
                work=self._converge_lobby_existence(gm, parent_space_id),
            )

    async def _converge_group_room_existence(self, gm: GroupRoomMap, parent_space_id: str | None) -> None:
        if gm.room is None:
            room_id = await self.effectors.create_group_room(gm.desired, parent_space_id)
            # Set before stamping, so a failed stamp still leaves this pass converging membership.
            gm.room = MatrixRoom(room_id=room_id, canonical_alias=gm.desired.canonical_alias)
            await self.effectors.put_room_state(
                room_id,
                event_type_name(self.server_name, OnbotRoomType.group_room),
                dump_room_state(
                    GroupRoomState(
                        group_id=gm.group_pk,
                        authentik_server=self.config.authentik_server.url,
                    )
                ),
            )
        elif await self.admin.room_is_blocked(gm.room.room_id):
            # G2.3: a previously blocked room whose group reappeared gets unblocked.
            log.info("unblocking room %s (group reappeared)", gm.room.room_id)
            await self.admin.room_set_blocked(gm.room.room_id, blocked=False)

        await self._converge_avatar(
            gm.room.room_id,
            OnbotRoomType.group_room,
            gm.desired.avatar_source_url,
            GroupRoomState(group_id=gm.group_pk, authentik_server=self.config.authentik_server.url),
        )

    async def _converge_lobby_existence(self, gm: GroupRoomMap, parent_space_id: str | None) -> None:
        assert gm.lobby_desired is not None
        await self._converge_lobby_creation(gm, parent_space_id)
        if gm.lobby is not None:
            # Same source URL as the group room, so the uploader's per-URL cache makes this a state
            # read and a set, not a second fetch+upload.
            await self._converge_avatar(
                gm.lobby.room_id,
                OnbotRoomType.visitor_lobby,
                gm.lobby_desired.avatar_source_url,
                VisitorLobbyRoomState(
                    group_id=gm.group_pk, authentik_server=self.config.authentik_server.url
                ),
            )

    async def _converge_lobby_creation(self, gm: GroupRoomMap, parent_space_id: str | None) -> None:
        """Create the group's visitor lobby if it does not exist yet, and stamp it (ADR-0012).

        The lobby is stamped with ``OnbotRoomType.visitor_lobby`` — never ``group_room`` — so no later
        pass mistakes it for the group room, projects Authentik membership onto it, and kicks every
        visitor. Its join rule is set at creation (via the effector) from the runtime space id, so it
        is never briefly invite-only nor briefly open. Requires a space (the config validator enforces
        that when the lobby comes from config; this guard covers a lobby enabled by an Authentik
        attribute while no space is configured).
        """
        assert gm.lobby_desired is not None
        if gm.lobby is not None:
            return
        if parent_space_id is None:
            log.warning(
                "group %s requests a visitor lobby but no parent space is configured; skipping",
                gm.group_pk,
            )
            return
        join_rules_content = desired_join_rules(OnbotRoomType.visitor_lobby, parent_space_id)
        assert join_rules_content is not None
        room_id = await self.effectors.create_lobby_room(
            gm.lobby_desired, parent_space_id, join_rules_content
        )
        await self.effectors.put_room_state(
            room_id,
            event_type_name(self.server_name, OnbotRoomType.visitor_lobby),
            dump_room_state(
                VisitorLobbyRoomState(group_id=gm.group_pk, authentik_server=self.config.authentik_server.url)
            ),
        )
        gm.lobby = MatrixRoom(room_id=room_id, canonical_alias=gm.lobby_desired.canonical_alias)

    async def _converge_obsolete_rooms(
        self, rooms: list[MatrixRoom], group_maps: list[GroupRoomMap], report: PassReport, plan: PassPlan
    ) -> None:
        """Tear down rooms whose mapped Authentik group disappeared (G2.3, the inverse of G2.2).

        A room is obsolete when it carries our ``group_room`` *or* ``visitor_lobby`` state event but
        the ``group_id`` recorded there no longer names a group that survives the sync filters —
        because the group was deleted upstream, or lost the attribute/prefix/parent that opted it into
        chat. The lobby is torn down with its group room (ADR-0012): a blocked group room beside a
        live, joinable lobby is the worst of both.

        Identification is by the recorded ``group_id``, not by set-differencing room ids as legacy
        did: a room whose alias changed, or one created earlier in this very pass, must not read as
        obsolete. Rooms with neither of our state events are never touched, so unrelated and
        onboarding rooms are structurally out of reach.

        ``rooms`` is the *server-wide* admin room list, so it includes rooms the bot has never
        joined: anything a user created for themselves. Reading state in one of those answers 403,
        not 404, and that 403 is an answer rather than a fault (see the handler below).
        """
        settings = self.config.sync_matrix_rooms_based_on_authentik_groups
        if not settings.enabled or not settings.disable_rooms_when_mapped_authentik_group_disappears:
            return
        live_group_pks = {gm.group_pk for gm in group_maps}
        mapped_room_ids = {gm.room.room_id for gm in group_maps if gm.room is not None}
        mapped_room_ids |= {gm.lobby.room_id for gm in group_maps if gm.lobby is not None}
        for room in rooms:
            if room.room_id in mapped_room_ids:
                continue  # backed by a live group (room or lobby); skip the state read
            await self._guarded(
                report,
                room_kind="unmapped_room",
                room_id=room.room_id,
                group_pk=None,
                step="obsolete-room check",
                work=self._converge_obsolete_room(room, live_group_pks, settings, report, plan),
            )

    async def _converge_obsolete_room(
        self,
        room: MatrixRoom,
        live_group_pks: set[str],
        settings: SyncMatrixRoomsBasedOnAuthentikGroups,
        report: PassReport,
        plan: PassPlan,
    ) -> None:
        for room_type in (OnbotRoomType.group_room, OnbotRoomType.visitor_lobby):
            try:
                raw = await self.effectors.get_room_state(
                    room.room_id, event_type_name(self.server_name, room_type)
                )
            except ApiError as exc:
                if exc.status_code != 403:
                    raise
                # "Not in room, and room previews are disabled." The bot creates every room it
                # manages and never leaves one, so a room whose state it may not read is by
                # construction not a room this pass is about: skip it exactly like a room with
                # no onbot state event.
                #
                # Not reported either: the admin room list is server-wide, so this is every
                # user-created private room on the server, every pass.
                log.debug("bot is not in room %s; not an onbot-managed room, skipping", room.room_id)
                return
            if not raw:
                continue  # not this kind of managed room
            try:
                state = parse_room_state(room_type, raw)
            except ValidationError:
                log.warning("room %s has unreadable onbot state; leaving it alone", room.room_id)
                return
            assert isinstance(state, GroupRoomState | VisitorLobbyRoomState)
            if state.group_id not in live_group_pks:
                self._defer_teardown(room, state.group_id, settings, report, room_type, plan)
            return  # matched one managed type; do not re-check the other

    def _defer_teardown(
        self,
        room: MatrixRoom,
        group_id: str,
        settings: SyncMatrixRoomsBasedOnAuthentikGroups,
        report: PassReport,
        room_type: OnbotRoomType,
        plan: PassPlan,
    ) -> None:
        """Record the teardown of an obsolete room; it runs once the guard permits it."""
        action = DestructiveAction(
            kind=ActionKind.room_teardown, target=room.room_id, room_id=room.room_id, group_pk=group_id
        )

        async def run(_allowed: frozenset[str]) -> None:
            await self._guarded(
                report,
                room_kind=room_type,
                room_id=room.room_id,
                group_pk=group_id,
                step="teardown",
                work=self._disable_room(room, group_id, settings, report, room_type),
            )

        plan.defer([action], run)

    async def _disable_room(
        self,
        room: MatrixRoom,
        group_id: str,
        settings: SyncMatrixRoomsBasedOnAuthentikGroups,
        report: PassReport,
        room_type: OnbotRoomType,
    ) -> None:
        """Block an obsolete room and clear it of users; optionally delete it (irreversible).

        A member the server refuses to kick (a creator, or a user at the bot's level in a pre-v12
        room) is reported and skipped, so the rest of the room is still cleared.
        """
        reason = (
            f"The group mapped to this room is no longer synced from the central user directory "
            f"({self.config.authentik_server.url})."
        )
        log.info("disabling room %s: authentik group %s disappeared", room.room_id, group_id)
        await self.admin.room_set_blocked(room.room_id, blocked=True)

        bot_id = self.config.synapse_server.bot_user_id
        problems: list[str] = []
        members = [m for m in await self.admin.list_room_members(room.room_id) if m != bot_id]
        await self._kick_members(
            room.room_id, await self._kickable(room, members, problems), reason, problems
        )
        report.extend(room_kind=room_type, room_id=room.room_id, group_pk=group_id, problems=problems)

        if settings.delete_disabled_rooms:
            log.warning("deleting room %s (delete_disabled_rooms is enabled)", room.room_id)
            await self.admin.delete_room(room.room_id, block=True, purge=True, message=reason)

    async def _converge_space(self, space: MatrixRoom, users: list[MappedUser]) -> None:
        await self._converge_space_avatar(space)
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
        manage_members = sync_cfg.enabled
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
            if manage_members:
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
            steps.append(("name and topic", self._converge_room_attributes(room, gm.desired, gm.group_pk)))
            await self._run_room_steps(report, OnbotRoomType.group_room, room.room_id, gm.group_pk, steps)

            if gm.lobby is not None:
                lobby = gm.lobby
                assert gm.lobby_desired is not None  # a lobby only exists where one was computed
                lobby_steps: list[tuple[str, Awaitable[None]]] = []
                if manage_members:
                    lobby_steps.append(("membership", self._converge_lobby_membership(gm, lobby, users)))
                    lobby_steps.append(
                        ("power levels", self._converge_lobby_power_levels(gm, lobby, report, plan))
                    )
                lobby_steps.append(("join rule", self._converge_lobby_join_rules(lobby, space)))
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
            protected_ids=[bot_id, *global_admins, *self.config.matrix_user_ignore_list],
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
            VisitorLobbyRoomState(group_id=gm.group_pk, authentik_server=self.config.authentik_server.url),
            gm.group_pk,
            {},
            report,
            plan,
        )

    async def _converge_lobby_join_rules(self, lobby: MatrixRoom, space: MatrixRoom | None) -> None:
        """Converge a lobby's ``restricted`` join rule (ADR-0012).

        The join-rule pass runs over existing lobbies as well as new ones and rewrites the rule only
        on a difference (a no-op tick sends nothing). The bot is PL 100 with ``state_default`` 100 so
        it may write the rule, but a hand-edited room can still 403: that is reported for the lobby
        and the pass carries on.
        """
        if space is None:  # cannot express `restricted` without a space to restrict to
            return
        desired = desired_join_rules(OnbotRoomType.visitor_lobby, space.room_id)
        current = await self.effectors.get_room_state(lobby.room_id, JOIN_RULES_EVENT_TYPE) or {}
        change = join_rules_change(current, desired)
        if change is None:
            return
        await self.effectors.put_room_state(lobby.room_id, JOIN_RULES_EVENT_TYPE, change)

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
            GroupRoomState(group_id=group_pk, authentik_server=self.config.authentik_server.url),
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
        actions = [
            DestructiveAction(
                kind=ActionKind.withdraw_power_level, target=mxid, room_id=room.room_id, group_pk=group_pk
            )
            for mxid in held
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

        A withdrawal is destructive and goes through the guard (D13): only the users in ``withdraw``
        lose their level here. Every other withdrawal is held: the entry stays at its level and in the
        record, and its MXID is returned so the caller can ask the guard about it.
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
        plan.note(
            [
                DestructiveAction(kind=_LIFECYCLE_ACTION_KINDS[step.action], target=step.mxid)
                for step in await self.lifecycle.plan_accounts(snapshot)
            ]
        )
        return snapshot

    async def _run_lifecycle(self, snapshot: DirectorySnapshot, decision: GuardDecision) -> None:
        """Run the lifecycle; it takes a lock or erasure only when the guard permitted it."""
        assert self.lifecycle is not None

        def permit(mxid: str, action: LifecycleAction) -> bool:
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
        cfg = self.config.sync_authentik_users_with_matrix_rooms
        everyone = await self.authentik.list_users(filter_is_active=None)
        if not everyone:
            log.warning(EMPTY_DIRECTORY_MESSAGE)
            await self.events.emit(
                Signal.lifecycle_attention, mxid=None, action="abort", detail=EMPTY_DIRECTORY_MESSAGE
            )
            return None
        in_scope = {str(u["pk"]) for u in await self._list_scoped_users(filter_is_active=None)}
        ignored = set(self.config.authentik_user_ignore_list)
        users: list[DirectoryUser] = []
        for user in everyone:
            pk = str(user["pk"])
            is_active = user.get("is_active") is True
            try:
                mxid: str | None = compute_mxid(
                    user,
                    username_attribute=cfg.authentik_username_mapping_attribute,
                    server_name=self.server_name,
                )
            except KeyError:
                mxid = None
                if pk in in_scope and not is_active:
                    log.warning(
                        "cannot map MXID for disabled Authentik user %r; only a recorded binding can "
                        "offboard it",
                        pk,
                    )
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
        )
