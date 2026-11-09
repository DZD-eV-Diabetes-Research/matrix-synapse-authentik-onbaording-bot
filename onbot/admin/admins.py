"""Who may command the bot: the union of a hand-written MXID list and an Authentik group (ADR-0010).

The allowlist is the only gate on `!announce`, a command that writes into every user's notice board,
so this module is written to fail *closed* in every direction it can fail:

* **Nobody is granted anything by default.** An empty union means every command is refused. There is
  deliberately no fallback to Authentik superusers — a superuser administers an identity provider,
  which is not the same job as paging the company, and a set derived from that role would widen
  silently the next time somebody is granted it. Somebody must create a group and put people in it.
* **A user the bot cannot map to an MXID is dropped**, not guessed at and not fatal. Likewise a user
  on ``authentik_user_ignore_list``: a service account parked in the admin group is not an admin,
  and a user whose MXID another Authentik user resolves to as well (D8): the right goes to nobody
  rather than to whichever account the mapping attribute happens to name.
  (``matrix_user_ignore_list`` is *not* applied — those are exactly the Matrix-only accounts that
  ``admin_user_ids`` exists to name.)
* **Authentik's filter is a hint, not the gate** (SEC-09). The admin groups are asked for with
  ``groups_by_pk``, but a server that does not recognise a filter drops it and answers with every
  user (see :meth:`~onbot.clients.authentik.ApiClientAuthentik.list_users`). Every returned user is
  therefore re-checked here: only one whose own ``groups`` name an admin group (and who is not
  disabled) counts. Anybody else in the answer is dropped and logged as an error.
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
from onbot.identity import IdentityResolver, Resolution, attribute_mxid, is_ignored
from onbot.logging import get_logger
from onbot.utils import normalize_group_pk

log = get_logger(__name__)


def resolve_admin_mxids(
    config: OnbotConfig, group_members: Iterable[dict[str, Any]], resolution: Resolution | None = None
) -> frozenset[str]:
    """Union the configured MXIDs with the resolvable, non-ignored members of the admin groups.

    Pure: ``group_members`` are the raw Authentik user dicts as
    :meth:`~onbot.clients.authentik.ApiClientAuthentik.list_users` returns them, ``resolution`` is
    what the :class:`~onbot.identity.IdentityResolver` made of them (without one, the mapping
    attribute names each MXID).
    """
    mxids = set(config.admin_room.admin_user_ids)
    for user in group_members:
        if is_ignored(user, config):
            log.debug("admin group member %r is on the ignore list; not an admin", user.get("username"))
            continue
        mxid = resolution.mxid(user["pk"]) if resolution is not None else attribute_mxid(user, config)
        if mxid is None:
            # Granting the bot's most dangerous capability to a user whose Matrix identity we had to
            # guess at is not an option; neither is taking the bot down over one malformed account.
            log.warning(
                "cannot resolve an MXID for Authentik user %r in an admin group (unmappable, or another "
                "user resolves to the same account); not granting bot admin",
                user.get("pk"),
            )
            continue
        mxids.add(mxid)
    return frozenset(mxids)


def _group_pks_of(user: dict[str, Any]) -> set[str]:
    """The group pks an Authentik user object names (``groups``, ``groups_obj``), normalised (W5R-02)."""
    pks: set[str] = set()
    groups = user.get("groups")
    if isinstance(groups, list):
        pks.update(normalize_group_pk(pk) for pk in groups if isinstance(pk, str | int))
    groups_obj = user.get("groups_obj")
    if isinstance(groups_obj, list):
        pks.update(normalize_group_pk(g["pk"]) for g in groups_obj if isinstance(g, dict) and "pk" in g)
    return pks


def in_groups(users: Iterable[dict[str, Any]], group_pks: Iterable[str]) -> list[dict[str, Any]]:
    """The ``users`` whose own user object names one of ``group_pks``, active or not (SEC-09).

    The server-side ``groups_by_pk`` filter only narrows the answer; it is not trusted to have done
    so. A user object that names none of the groups, or names none at all, is not a member. Both
    sides are compared as UUIDs where they are ones: Authentik names the groups hyphenated, the
    configuration may name them as 32 hex digits or in upper case (W5R-02).
    """
    wanted = {normalize_group_pk(pk) for pk in group_pks}
    return [u for u in users if _group_pks_of(u) & wanted]


def names_no_groups(user: dict[str, Any]) -> bool:
    """Whether a user object carries no group membership at all (neither ``groups`` nor ``groups_obj``)."""
    return not _group_pks_of(user)


def members_of(users: Iterable[dict[str, Any]], group_pks: Iterable[str]) -> list[dict[str, Any]]:
    """The ``users`` that are active members of one of ``group_pks``, by their own user object (SEC-09).

    See :func:`in_groups`; a user object that says the account is disabled is not a member either.
    """
    return [u for u in in_groups(users, group_pks) if u.get("is_active") is not False]


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
        identity: IdentityResolver | None = None,
    ) -> None:
        self.authentik = authentik
        self.config = config
        # The reconciler's resolver (D8): an admin renamed in Authentik keeps the rights on their
        # account, and a member whose MXID is somebody else's gets none.
        self.identity = identity
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
        verified = members_of(members, self.group_pks)
        if len(verified) != len(members):
            strays = sorted(str(u.get("username", u.get("pk"))) for u in members if u not in verified)
            log.error(
                "Authentik answered the bot-admin group query with %d user(s) who are not active members "
                "of %s according to their own user object (%s). Did the server ignore the groups_by_pk "
                "filter? Not granting them bot admin.",
                len(strays),
                ", ".join(self.group_pks),
                ", ".join(strays[:10]) + (", ..." if len(strays) > 10 else ""),
            )
        members = verified
        resolution: Resolution | None = None
        if self.identity is not None:
            try:
                resolution = await self.identity.resolve(m for m in members if not is_ignored(m, self.config))
            except Exception:
                log.warning(
                    "could not resolve the bot-admin group members; keeping the previous %d admins",
                    len(self._admins),
                    exc_info=True,
                )
                return self._admins
        self._admins = resolve_admin_mxids(self.config, members, resolution)
        self._fetched_at = self._clock()
        log.debug("resolved %d bot admins", len(self._admins))
        return self._admins
