"""Who may command the bot: the union of a hand-written MXID list and an Authentik group (ADR-0010).

The allowlist is the only gate on `!announce`, a command that writes into every user's notice board,
so this module is written to fail *closed* in every direction it can fail:

* **Nobody is granted anything by default.** An empty union means every command is refused. There is
  deliberately no fallback to Authentik superusers — a superuser administers an identity provider,
  which is not the same job as paging the company, and a set derived from that role would widen
  silently the next time somebody is granted it. Somebody must create a group and put people in it.
* **A user the bot cannot map to an MXID is dropped**, not guessed at and not fatal. Likewise a user
  on ``authentik_user_ignore_list``: a service account parked in the admin group is not an admin.
  (``matrix_user_ignore_list`` is *not* applied — those are exactly the Matrix-only accounts that
  ``admin_user_ids`` exists to name.)
* **Authentik being down never opens the gate, and never slams it either.** A failed refresh keeps
  the previous set: falling back to the empty set would be a self-inflicted outage of the control
  room, and falling back to anything wider would be the other kind of disaster. Before the first
  successful fetch the set is ``admin_user_ids`` alone — the explicit list is the floor, and it does
  not depend on Authentik being reachable.

The set is re-resolved on a TTL rather than frozen at startup, because a hand-maintained list that
you cannot revoke is at least honest about it, while a group membership that looks revocable and is
not would be worse than what it replaced. Authorise against a set that is at most one TTL stale;
never against one that is a process-lifetime stale.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from onbot.clients.authentik import ApiClientAuthentik
from onbot.config import OnbotConfig
from onbot.identity import compute_mxid
from onbot.logging import get_logger

log = get_logger(__name__)


def resolve_admin_mxids(config: OnbotConfig, group_members: Iterable[dict[str, Any]]) -> frozenset[str]:
    """Union the configured MXIDs with the mappable, non-ignored members of the admin groups.

    Pure: ``group_members`` are the raw Authentik user dicts as
    :meth:`~onbot.clients.authentik.ApiClientAuthentik.list_users` returns them.
    """
    mxids = set(config.admin_room.admin_user_ids)
    sync_cfg = config.sync_authentik_users_with_matrix_rooms
    for user in group_members:
        if user.get("username") in config.authentik_user_ignore_list:
            log.debug("admin group member %r is on the ignore list; not an admin", user.get("username"))
            continue
        try:
            mxids.add(
                compute_mxid(
                    user,
                    username_attribute=sync_cfg.authentik_username_mapping_attribute,
                    server_name=config.synapse_server.server_name,
                )
            )
        except KeyError:
            # Granting the bot's most dangerous capability to a user whose Matrix identity we had to
            # guess at is not an option; neither is taking the bot down over one malformed account.
            log.warning(
                "cannot map an MXID for Authentik user %r in an admin group; not granting bot admin",
                user.get("pk"),
            )
    return frozenset(mxids)


def _directory_freshness_sec(config: OnbotConfig) -> int:
    """How stale the bot's view of Authentik may get, in seconds."""
    return config.authentik_poll_rate_sec or config.server_tick_rate_sec


class AdminResolver:
    """The current admin set, refreshed from Authentik at most once per TTL."""

    def __init__(
        self,
        authentik: ApiClientAuthentik,
        config: OnbotConfig,
        *,
        ttl_sec: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.authentik = authentik
        self.config = config
        self.cfg = config.admin_room
        # The Authentik poll interval, reused rather than given its own knob: it is already the
        # answer to "how stale may this bot's view of Authentik be?", and an operator who tightens
        # that means it for the admin group too. Deliberately *not* the reconcile interval — that one
        # is now a slow Matrix-side drift repair (minutes), and binding revocation of the bot's most
        # dangerous capability to it would let a removed admin keep issuing commands for minutes.
        # Falls back to the reconcile interval only when the poll is switched off entirely.
        self.ttl_sec = ttl_sec if ttl_sec is not None else float(_directory_freshness_sec(config))
        self._clock = clock
        # The floor: available before the first fetch, and after a failed one.
        self._admins = frozenset(self.cfg.admin_user_ids)
        self._fetched_at: float | None = None

    @property
    def group_pks(self) -> Sequence[str]:
        return self.cfg.authentik_group_pks_granting_bot_admin

    async def admins(self) -> frozenset[str]:
        """The admin set, refreshing it first when the cached one has aged past the TTL."""
        if self._is_stale():
            await self.refresh()
        return self._admins

    def _is_stale(self) -> bool:
        if not self.group_pks:
            return False  # nothing to fetch; admin_user_ids is the whole answer
        if self._fetched_at is None:
            return True
        return self._clock() - self._fetched_at >= self.ttl_sec

    async def refresh(self) -> frozenset[str]:
        """Re-read the admin groups. On failure keep the previous set and say so."""
        if not self.group_pks:
            self._admins = frozenset(self.cfg.admin_user_ids)
            return self._admins
        try:
            # filter_is_active defaults to True: a disabled Authentik account must not keep its
            # commands. Passed explicitly so a change to that default cannot silently re-admit one.
            members = await self.authentik.list_users(
                filter_groups_by_pk=list(self.group_pks), filter_is_active=True
            )
        except Exception:
            log.warning(
                "could not read the bot-admin groups from Authentik; keeping the previous %d admins",
                len(self._admins),
                exc_info=True,
            )
            return self._admins
        self._admins = resolve_admin_mxids(self.config, members)
        self._fetched_at = self._clock()
        log.debug("resolved %d bot admins", len(self._admins))
        return self._admins
