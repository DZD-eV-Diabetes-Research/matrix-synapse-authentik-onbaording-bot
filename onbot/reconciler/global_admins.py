"""Accounts seeded into every managed room at power level 100 (``global_room_admins``).

This is the one place in the bot where a user's rooms are *not* a function of their Authentik group
membership. Everywhere else the group is the boundary: :func:`~onbot.reconciler.membership.
desired_room_members` projects exactly the members of the mapped group, and power levels are
computed only over users who are in that group. A global room admin is force-joined into every group
room and every visitor lobby, held at :data:`~onbot.reconciler.power_levels.ROOM_ADMIN_LEVEL`, and
never kicked.

That makes an entry here strictly more dangerous than an entry in any other list in the config, so
resolution **fails closed on every axis**: an entry that cannot be resolved to a live, mappable,
already-existing Matrix account grants nothing and is logged, rather than being guessed at or taken
as a reason to abort the pass. Concretely, a name is dropped when it

* matches no *active* Authentik user (the lookup filters ``is_active``, so a disabled account
  resolves to nothing and the account lifecycle offboards it as usual, being named here is not an
  exemption from that);
* is also on ``authentik_user_ignore_list`` (the same precedence
  :func:`~onbot.admin.admins.resolve_admin_mxids` applies: a name the operator has told the bot
  never to touch does not become the most powerful account in every room because a second list
  mentions it);
* resolves to no Matrix ID (no MAS link, no recorded binding and no
  ``authentik_username_mapping_attribute``), or to one other Authentik users resolve to as well
  (D8: the resolution goes through :class:`~onbot.identity.IdentityResolver`, like every other user);
* resolves to an account on ``matrix_user_ignore_list``, which promises that the bot never changes
  that account's power level (MNT-13);
* names the bot itself, which is the room creator and already outranks everyone (under room
  version 12 it must stay *absent* from the ``users`` map, so writing it in would be rejected);
* has no Matrix account yet. The bot never creates accounts (MAS provisions them on first login), so
  force-joining an MXID that does not exist fails per room, every room, every tick. Skipping is the
  same end state without the error storm, and the admin is picked up automatically once they log in.

A lookup failure against Authentik is different in kind from a name that resolved to nothing: it is
transient and says nothing about the operator's intent. It keeps that name's *previous* resolution
(like :class:`~onbot.admin.admins.AdminResolver`) rather than dropping it, which would otherwise kick
the admin from every room they are not a group member of and strip their power level on one bad
tick, then put both back on the next (REC-06). A name that never resolved before stays unresolved.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from onbot.clients.authentik import ApiClientAuthentik
from onbot.config import OnbotConfig
from onbot.identity import IdentityResolver
from onbot.logging import get_logger
from onbot.models import MappedUser

log = get_logger(__name__)


class _LookupFailed(Exception):
    """Authentik could not be asked about one global admin (transient, not "no such user")."""


class GlobalRoomAdminResolver:
    """Resolves ``global_room_admins`` usernames to MXIDs, once per reconcile pass."""

    def __init__(
        self, authentik: ApiClientAuthentik, config: OnbotConfig, *, identity: IdentityResolver | None = None
    ) -> None:
        self.authentik = authentik
        self.identity = identity or IdentityResolver(config)
        self.config = config
        self._mxids: frozenset[str] = frozenset()
        # name -> MXID of the last pass that resolved it; what a transient lookup failure falls back to.
        self._previous: dict[str, str] = {}

    @property
    def mxids(self) -> frozenset[str]:
        """The last resolved set. Empty until :meth:`resolve` has run."""
        return self._mxids

    async def resolve(self, mapped_users: Iterable[MappedUser], matrix_mxids: set[str]) -> frozenset[str]:
        """Resolve the configured usernames against Authentik and the known Matrix accounts.

        ``mapped_users`` are the users this pass already read from Authentik; a global admin who is
        an ordinary synced user is found there and costs no extra request. Only the remainder (an
        admin outside ``sync_only_users_in_authentik_pathes``, or in no synced group at all) is
        looked up individually.
        """
        names = self.config.global_room_admins
        if not names:
            self._mxids = frozenset()
            self._previous = {}
            return self._mxids

        # Already resolved this pass (through the binding or MAS, D8): no request, no second opinion.
        known: dict[str, MappedUser] = {str(u.authentik_obj.get("username")): u for u in mapped_users}
        resolved: set[str] = set()
        by_name: dict[str, str] = {}
        for name in names:
            if name in self.config.authentik_user_ignore_list:
                log.warning(
                    "global_room_admins names %r, which is also on authentik_user_ignore_list; "
                    "not granting it room admin anywhere",
                    name,
                )
                continue
            mapped = known.get(name)
            user = mapped.authentik_obj if mapped is not None else None
            if user is None:
                try:
                    user = await self._lookup(name)
                except _LookupFailed:
                    previous = self._previous.get(name)
                    if previous is not None and previous in matrix_mxids:
                        log.warning(
                            "could not look up global room admin %r in Authentik; keeping %s from the "
                            "previous pass",
                            name,
                            previous,
                        )
                        resolved.add(previous)
                        by_name[name] = previous
                    continue
            if user is None:
                continue
            mxid = mapped.mxid if mapped is not None else await self._mxid_for(user, name)
            if mxid is None:
                continue
            if mxid in self.config.matrix_user_ignore_list:
                log.warning(
                    "global_room_admins names %r, whose account %s is on matrix_user_ignore_list; "
                    "not granting it room admin anywhere",
                    name,
                    mxid,
                )
                continue
            if mxid == self.config.synapse_server.bot_user_id:
                log.warning("global_room_admins names the bot itself (%r); ignoring", name)
                continue
            if mxid not in matrix_mxids:
                log.warning(
                    "global room admin %r has no Matrix account yet (%s); skipping until they log in "
                    "once and MAS provisions it",
                    name,
                    mxid,
                )
                continue
            resolved.add(mxid)
            by_name[name] = mxid

        self._mxids = frozenset(resolved)
        self._previous = by_name
        if len(resolved) < len(names):
            log.info("resolved %d of %d configured global room admins", len(resolved), len(names))
        return self._mxids

    async def _lookup(self, name: str) -> dict[str, Any] | None:
        """Fetch one active Authentik user by exact username, or ``None`` when there is none.

        Raises :class:`_LookupFailed` when Authentik could not be asked at all: that is not an answer
        about the user, and the caller falls back to the previous pass for this name.
        """
        try:
            candidates = await self.authentik.list_users(filter_by_username=name, filter_is_active=True)
        except Exception as exc:
            log.warning("could not look up global room admin %r in Authentik", name, exc_info=True)
            raise _LookupFailed(name) from exc
        # Re-check the username locally: `username` is a narrowing hint the server is free to ignore,
        # and a server that ignored it answers with every user. Matching the first result blindly
        # would then make an arbitrary account a room admin everywhere.
        exact = [u for u in candidates if u.get("username") == name]
        if not exact:
            log.warning("global_room_admins names %r, which is not an active Authentik user; ignoring", name)
            return None
        if len(exact) > 1:
            log.warning(
                "global_room_admins name %r matches %d Authentik users; ignoring it rather than picking one",
                name,
                len(exact),
            )
            return None
        return exact[0]

    async def _mxid_for(self, user: dict[str, Any], name: str) -> str | None:
        mxid = (await self.identity.resolve([user])).mxid(user["pk"])
        if mxid is None:
            log.warning(
                "cannot resolve a Matrix ID for global room admin %r (no MAS link, recorded binding or "
                "%r on the Authentik user, or another user resolves to the same account); not granting "
                "it room admin anywhere",
                name,
                self.config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute,
            )
        return mxid
