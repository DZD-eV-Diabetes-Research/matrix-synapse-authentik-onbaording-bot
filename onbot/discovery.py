"""Cheap change detection on Authentik, so the expensive reconcile can run rarely (AD-2).

One loop used to do two jobs with wildly different costs. A reconcile pass reads the whole Matrix
side — every managed room's members, power levels and state — while the thing it is usually reacting
to is a single new user in Authentik. Running it often enough to onboard people promptly meant
running it often enough to hammer Synapse; running it rarely enough to be polite meant new employees
waited.

The two jobs are separated here. :class:`DiscoveryPoller` polls *only* Authentik on a short interval,
fingerprints the answer, and calls the engine's existing on-demand
:meth:`~onbot.reconciler.engine.ReconcilerEngine.trigger` when the fingerprint moves. It touches
Synapse never and writes nothing. The full reconcile then drops to a slow drift-repair safety net
(``server_tick_rate_sec``), and new-user latency is set by ``authentik_poll_rate_sec`` instead.

The fingerprint covers exactly the Authentik facts a reconcile projects onto Matrix — who exists,
what they are called, which groups they are in, which groups map to rooms and how. It deliberately
excludes volatile fields (``last_login``, ``last_updated``): a user simply logging in changes nothing
about the desired Matrix state, and waking the reconciler for it would defeat the purpose.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Callable
from typing import Any

from onbot.clients.authentik import ApiClientAuthentik
from onbot.config import OnbotConfig
from onbot.logging import get_logger
from onbot.utils import get_nested_dict_val_by_path

log = get_logger(__name__)

ERROR_BACKOFF_SEC = 30.0


def _user_facts(user: dict[str, Any], username_attribute: str) -> tuple[Any, ...]:
    """The fields of an Authentik user that a reconcile pass can act on."""
    return (
        user.get("pk"),
        user.get("username"),
        # The MXID is derived from this dotted path, exactly as `identity.compute_mxid` reads it;
        # changing it moves the user to a different Matrix account. A user missing the attribute is
        # one the reconciler skips with a warning, so a poll must not raise over it either.
        get_nested_dict_val_by_path(user, username_attribute.split("."), fallback_val=None),
        user.get("is_active"),
        user.get("is_superuser"),  # drives room admin power levels
        tuple(sorted(g["pk"] for g in user.get("groups_obj") or [])),
    )


def _group_facts(group: dict[str, Any]) -> tuple[Any, ...]:
    """The fields of an Authentik group that a reconcile pass can act on."""
    return (
        group.get("pk"),
        group.get("name"),
        group.get("is_superuser"),
        # Attributes carry the room's alias, name, topic, avatar and power level.
        json.dumps(group.get("attributes") or {}, sort_keys=True),
    )


def fingerprint(users: list[dict[str, Any]], groups: list[dict[str, Any]], username_attribute: str) -> str:
    """A stable digest of everything in Authentik that the reconciler projects onto Matrix.

    Order-independent (Authentik does not promise a stable page order) and free of volatile fields,
    so an unchanged directory always yields an unchanged digest.
    """
    payload = {
        "users": sorted(repr(_user_facts(u, username_attribute)) for u in users),
        "groups": sorted(repr(_group_facts(g)) for g in groups),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class DiscoveryPoller:
    """Watch Authentik for changes worth reconciling, and trigger the engine when there are any."""

    def __init__(
        self,
        authentik: ApiClientAuthentik,
        config: OnbotConfig,
        trigger: Callable[[], None],
        *,
        error_backoff_sec: float = ERROR_BACKOFF_SEC,
    ) -> None:
        self.authentik = authentik
        self.config = config
        self.trigger = trigger
        self._error_backoff_sec = error_backoff_sec
        self._stop = asyncio.Event()
        self._fingerprint: str | None = None

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Poll until stopped, triggering a reconcile whenever the directory has moved."""
        interval = self.config.authentik_poll_rate_sec
        if interval <= 0:
            log.info("authentik discovery poll disabled; new users wait for the reconcile tick")
            return
        log.info("authentik discovery poll started; every %ss", interval)
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                log.exception("authentik discovery poll failed; backing off %.0fs", self._error_backoff_sec)
                await self._sleep(self._error_backoff_sec)
                continue
            await self._sleep(interval)
        log.info("authentik discovery poll stopped")

    async def poll_once(self) -> bool:
        """Fingerprint Authentik once. Returns whether it changed (and a reconcile was triggered).

        The very first poll establishes the baseline without triggering: the engine reconciles on
        startup anyway, and firing here would only make it run twice.
        """
        current = fingerprint(
            await self._list_users(),
            await self._list_groups(),
            self.config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute,
        )
        previous, self._fingerprint = self._fingerprint, current
        if previous is None or previous == current:
            return False
        log.info("authentik changed; triggering an out-of-band reconcile")
        self.trigger()
        return True

    async def _list_users(self) -> list[dict[str, Any]]:
        """The same user set the reconciler maps, under the same filters (see ``engine``)."""
        cfg = self.config.sync_authentik_users_with_matrix_rooms
        if not cfg.enabled:
            return []
        paths: list[str | None] = [*(cfg.sync_only_users_in_authentik_pathes or [])] or [None]
        users: list[dict[str, Any]] = []
        for path in paths:
            users.extend(
                await self.authentik.list_users(
                    filter_by_path=path,
                    filter_by_attribute=cfg.sync_only_users_with_authentik_attributes,
                    filter_groups_by_pk=cfg.sync_only_users_of_groups_with_id,
                    filter_is_active=True,
                )
            )
        return users

    async def _list_groups(self) -> list[dict[str, Any]]:
        settings = self.config.sync_matrix_rooms_based_on_authentik_groups
        if not settings.enabled:
            return []
        return await self.authentik.list_groups(filter_by_attribute=settings.only_groups_with_attributes)

    async def _sleep(self, seconds: float) -> None:
        # Sleep, but wake immediately on stop.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
