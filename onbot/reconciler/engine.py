"""Reconciler engine (AD-2): level-triggered, idempotent convergence.

Computes *desired* state (from Authentik) vs *actual* state (Synapse) and applies the diff. Runs on
a schedule **and** on demand (replacing the legacy ``while True: sleep`` tick loop), and shuts down
gracefully on SIGINT/SIGTERM. A single ``reconcile_once`` pass is fully re-runnable.

The pure decision logic lives in the sibling modules (``rooms``, ``membership``, ``power_levels``);
reads go through the Authentik + Synapse-admin clients; writes go through the Synapse-admin client
(membership/block) and the :class:`MatrixEffectors` seam (CS-API operations, Phase 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig, SyncMatrixRoomsBasedOnAuthentikGroups
from onbot.events import EventBus, Signal
from onbot.identity import build_canonical, compute_mxid
from onbot.lifecycle.accounts import AccountLifecycleManager
from onbot.logging import get_logger
from onbot.models import GroupRoomMap, MappedUser, MatrixRoom
from onbot.reconciler.effectors import DryRunEffectors, MatrixEffectors
from onbot.reconciler.membership import (
    desired_room_members,
    diff_room_membership,
    diff_space_membership,
)
from onbot.reconciler.power_levels import (
    PowerLevelGroup,
    compute_desired_user_levels,
    extract_power_level_groups,
    merge_power_levels,
)
from onbot.reconciler.rooms import build_group_room_maps
from onbot.reconciler.state import (
    GroupRoomState,
    OnbotRoomType,
    SpaceRoomState,
    dump_room_state,
    event_type_name,
)

log = get_logger(__name__)


class ConfigurationError(RuntimeError):
    """Raised when the configuration cannot be satisfied (e.g. required space missing)."""


class ReconcilerEngine:
    def __init__(
        self,
        config: OnbotConfig,
        authentik: ApiClientAuthentik,
        admin: ApiClientSynapseAdmin,
        effectors: MatrixEffectors | None = None,
        events: EventBus | None = None,
        lifecycle: AccountLifecycleManager | None = None,
    ) -> None:
        self.config = config
        self.authentik = authentik
        self.admin = admin
        self.effectors: MatrixEffectors = effectors or DryRunEffectors()
        self.events = events or EventBus()
        self.lifecycle = lifecycle
        self.server_name = config.synapse_server.server_name
        self._stop = asyncio.Event()
        self._trigger = asyncio.Event()

    # --- runtime loop --------------------------------------------------------

    def trigger(self) -> None:
        """Request an out-of-band reconcile (on-demand) before the next scheduled tick."""
        self._trigger.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._trigger.set()  # unblock the wait so we exit promptly

    async def run(self) -> None:
        """Run scheduled + on-demand reconciles until stopped (SIGINT/SIGTERM)."""
        self._install_signal_handlers()
        log.info("reconciler started; tick=%ss", self.config.server_tick_rate_sec)
        while not self._stop.is_set():
            # Clear before the pass so a trigger raised *during* it is preserved for the next wait.
            self._trigger.clear()
            try:
                await self.reconcile_once()
            except Exception:
                log.exception("reconcile pass failed; will retry next tick")
            if self._stop.is_set():
                break
            await self._wait_for_next_tick()
        log.info("reconciler stopped")

    async def _wait_for_next_tick(self) -> None:
        # Wake on an on-demand trigger, or fall through on the scheduled timeout.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._trigger.wait(), timeout=self.config.server_tick_rate_sec)

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.request_stop)
        except NotImplementedError, RuntimeError:  # pragma: no cover - non-main thread / Windows
            log.debug("signal handlers unavailable in this environment")

    # --- one convergence pass ------------------------------------------------

    async def reconcile_once(self) -> None:
        log.info("reconcile: gathering desired (Authentik) and actual (Synapse) state")
        matrix_users = await self.admin.list_users()
        users = await self._gather_mapped_users(matrix_users)
        space = await self._resolve_space()
        group_maps = await self._gather_group_room_maps()

        await self._converge_rooms(group_maps, space)
        if space is not None:
            await self._converge_space_membership(space, users)
        await self._converge_room_membership_and_levels(group_maps, users)
        await self._converge_lifecycle(matrix_users, {u.mxid for u in users})
        log.info("reconcile: done (%d users, %d group rooms)", len(users), len(group_maps))

    async def _gather_mapped_users(self, matrix_users: list[dict[str, Any]]) -> list[MappedUser]:
        cfg = self.config.sync_authentik_users_with_matrix_rooms
        if not cfg.enabled:
            return []
        configured_paths = cfg.sync_only_users_in_authentik_pathes
        paths: list[str | None] = [*configured_paths] if configured_paths else [None]
        by_mxid: dict[str, dict[str, Any]] = {}
        seen_pks: set[str] = set()
        for path in paths:
            for user in await self.authentik.list_users(
                filter_by_path=path,
                filter_by_attribute=cfg.sync_only_users_with_authentik_attributes,
                filter_groups_by_pk=cfg.sync_only_users_of_groups_with_id,
                filter_is_active=True,
            ):
                if user["username"] in self.config.authentik_user_ignore_list:
                    continue
                if user["pk"] in seen_pks:
                    continue
                seen_pks.add(user["pk"])
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

        mapped: list[MappedUser] = []
        for matrix_user in matrix_users:
            mxid = matrix_user["name"]
            if mxid in self.config.matrix_user_ignore_list:
                continue
            authentik_user = by_mxid.get(mxid)
            if authentik_user is not None:
                mapped.append(MappedUser(authentik_obj=authentik_user, mxid=mxid, matrix_obj=matrix_user))
                await self.events.emit(Signal.user_synced, mxid=mxid)
        return mapped

    async def _gather_group_room_maps(self) -> list[GroupRoomMap]:
        settings = self.config.sync_matrix_rooms_based_on_authentik_groups
        if not settings.enabled:
            return []
        groups = await self.authentik.list_groups(filter_by_attribute=settings.only_groups_with_attributes)
        rooms = [MatrixRoom.from_admin_api(r) for r in await self.admin.list_non_space_rooms()]
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

    async def _converge_rooms(self, group_maps: list[GroupRoomMap], space: MatrixRoom | None) -> None:
        parent_space_id = space.room_id if space else None
        for gm in group_maps:
            if gm.room is None:
                room_id = await self.effectors.create_group_room(gm.desired, parent_space_id)
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
                gm.room = MatrixRoom(room_id=room_id, canonical_alias=gm.desired.canonical_alias)
            elif await self.admin.room_is_blocked(gm.room.room_id):
                # G2.3: a previously blocked room whose group reappeared gets unblocked.
                log.info("unblocking room %s (group reappeared)", gm.room.room_id)
                await self.admin.room_set_blocked(gm.room.room_id, blocked=False)

    async def _converge_space_membership(self, space: MatrixRoom, users: list[MappedUser]) -> None:
        members = await self.admin.list_room_members(space.room_id)
        diff = diff_space_membership(users, members)
        for mxid in diff.to_add:
            await self.admin.add_user_to_room(space.room_id, mxid)

    async def _converge_room_membership_and_levels(
        self, group_maps: list[GroupRoomMap], users: list[MappedUser]
    ) -> None:
        sync_cfg = self.config.sync_authentik_users_with_matrix_rooms
        room_cfg = self.config.sync_matrix_rooms_based_on_authentik_groups
        bot_id = self.config.synapse_server.bot_user_id

        pl_groups = extract_power_level_groups(
            await self.authentik.list_groups(
                filter_has_non_empty_attributes=[room_cfg.authentik_group_attr_for_matrix_power_level]
            ),
            room_cfg.authentik_group_attr_for_matrix_power_level,
        )

        for gm in group_maps:
            if gm.room is None:  # freshly created in dry-run with synthetic id is still set; guard anyway
                continue
            room_id = gm.room.room_id
            actual_members = await self.admin.list_room_members(room_id)
            desired_mxids = desired_room_members(gm.group_pk, users)

            mdiff = diff_room_membership(
                desired_mxids,
                actual_members,
                kick_enabled=sync_cfg.kick_matrix_room_members_not_in_mapped_authentik_group_anymore,
                protected_ids=[bot_id],
            )
            for mxid in mdiff.to_add:
                await self.admin.add_user_to_room(room_id, mxid)
            for mxid in mdiff.to_kick:
                await self.effectors.kick_user(
                    room_id,
                    mxid,
                    "Removed: missing/revoked group membership in the central user directory.",
                )

            await self._converge_power_levels(room_id, gm.group_pk, users, pl_groups, room_cfg)
            await self._converge_room_attributes(gm)

    async def _converge_power_levels(
        self,
        room_id: str,
        group_pk: str,
        users: list[MappedUser],
        pl_groups: list[PowerLevelGroup],
        room_cfg: SyncMatrixRoomsBasedOnAuthentikGroups,
    ) -> None:
        members_in_room = [u for u in users if group_pk in u.group_pks]
        managed = {u.mxid for u in members_in_room}
        if not managed:
            return
        desired = compute_desired_user_levels(
            members_in_room,
            pl_groups,
            make_superusers_admin=room_cfg.make_authentik_superusers_matrix_room_admin,
        )
        current = await self.effectors.get_room_power_levels(room_id)
        current_users = dict(current.get("users", {}))
        merged = merge_power_levels(current_users, desired, managed)
        if merged != current_users:
            new_levels = {**current, "users": merged}
            await self.effectors.set_room_power_levels(room_id, new_levels)

    async def _converge_room_attributes(self, gm: GroupRoomMap) -> None:
        if not self.config.matrix_room_default_settings.keep_updating_matrix_attributes_from_authentik:
            return
        assert gm.room is not None
        if gm.desired.name is not None and gm.room.name != gm.desired.name:
            await self.effectors.set_room_name(gm.room.room_id, gm.desired.name)
        if gm.desired.topic is not None and gm.room.topic != gm.desired.topic:
            await self.effectors.set_room_topic(gm.room.room_id, gm.desired.topic)

    # --- lifecycle (AD-5, G9.*): quarantined, invoked only from the reconcile result ---

    async def _converge_lifecycle(self, matrix_users: list[dict[str, Any]], active_mxids: set[str]) -> None:
        if self.lifecycle is None:
            return
        sync_cfg = self.config.sync_authentik_users_with_matrix_rooms
        if not sync_cfg.deactivate_disabled_authentik_users_in_matrix.enabled:
            return
        orphaned = await self._gather_orphaned_mxids(matrix_users, active_mxids)
        await self.lifecycle.reconcile_accounts(orphaned)

    async def _gather_orphaned_mxids(
        self, matrix_users: list[dict[str, Any]], active_mxids: set[str]
    ) -> set[str]:
        """MXIDs whose Authentik account is disabled but a Matrix account still exists (G9.1).

        Scoped to *disabled* Authentik users we can positively map — never sweeping arbitrary Matrix
        accounts that simply lack an Authentik counterpart (e.g. admin/service users), so the
        destructive path can only ever touch accounts we provisioned from a now-disabled directory
        entry. The bot user and the ignore lists (G12.1) are always excluded.
        """
        cfg = self.config.sync_authentik_users_with_matrix_rooms
        disabled_users = await self.authentik.list_users(
            filter_by_attribute=cfg.sync_only_users_with_authentik_attributes,
            filter_is_active=False,
        )
        matrix_mxids = {u["name"] for u in matrix_users}
        bot_id = self.config.synapse_server.bot_user_id
        orphaned: set[str] = set()
        for user in disabled_users:
            if user.get("username") in self.config.authentik_user_ignore_list:
                continue
            try:
                mxid = compute_mxid(
                    user,
                    username_attribute=cfg.authentik_username_mapping_attribute,
                    server_name=self.server_name,
                )
            except KeyError:
                continue
            if mxid == bot_id or mxid in self.config.matrix_user_ignore_list:
                continue
            if mxid in active_mxids or mxid not in matrix_mxids:
                continue
            orphaned.add(mxid)
        return orphaned
