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
* carries no ``authentik_username_mapping_attribute``, so its MXID would have to be guessed;
* names the bot itself, which is the room creator and already outranks everyone (under room
  version 12 it must stay *absent* from the ``users`` map, so writing it in would be rejected);
* has no Matrix account yet. The bot never creates accounts (MAS provisions them on first login), so
  force-joining an MXID that does not exist fails per room, every room, every tick. Skipping is the
  same end state without the error storm, and the admin is picked up automatically once they log in.

A lookup failure against Authentik is different in kind from a name that resolved to nothing: it is
transient and says nothing about the operator's intent. It leaves the *previously resolved* set in
place (like :class:`~onbot.admin.admins.AdminResolver`) rather than collapsing to empty, which would
otherwise strip the admins out of the power-level map on one bad tick and put them back on the next.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from onbot.clients.authentik import ApiClientAuthentik
from onbot.config import OnbotConfig
from onbot.identity import compute_mxid
from onbot.logging import get_logger
from onbot.models import MappedUser

log = get_logger(__name__)


class GlobalRoomAdminResolver:
    """Resolves ``global_room_admins`` usernames to MXIDs, once per reconcile pass."""

    def __init__(self, authentik: ApiClientAuthentik, config: OnbotConfig) -> None:
        self.authentik = authentik
        self.config = config
        self._mxids: frozenset[str] = frozenset()

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
            return self._mxids

        known: dict[str, dict[str, Any]] = {
            str(u.authentik_obj.get("username")): u.authentik_obj for u in mapped_users
        }
        resolved: set[str] = set()
        for name in names:
            if name in self.config.authentik_user_ignore_list:
                log.warning(
                    "global_room_admins names %r, which is also on authentik_user_ignore_list; "
                    "not granting it room admin anywhere",
                    name,
                )
                continue
            user = known.get(name)
            if user is None:
                user = await self._lookup(name)
            if user is None:
                continue
            mxid = self._mxid_for(user, name)
            if mxid is None:
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

        self._mxids = frozenset(resolved)
        if len(resolved) < len(names):
            log.info("resolved %d of %d configured global room admins", len(resolved), len(names))
        return self._mxids

    async def _lookup(self, name: str) -> dict[str, Any] | None:
        """Fetch one active Authentik user by exact username, or ``None``."""
        try:
            candidates = await self.authentik.list_users(filter_by_username=name, filter_is_active=True)
        except Exception:
            # Transient: say so and grant nothing *this* name, rather than failing the whole pass.
            log.warning("could not look up global room admin %r in Authentik", name, exc_info=True)
            return None
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

    def _mxid_for(self, user: dict[str, Any], name: str) -> str | None:
        cfg = self.config.sync_authentik_users_with_matrix_rooms
        try:
            return compute_mxid(
                user,
                username_attribute=cfg.authentik_username_mapping_attribute,
                server_name=self.config.synapse_server.server_name,
            )
        except KeyError:
            log.warning(
                "cannot map an MXID for global room admin %r (no %r on the Authentik user); "
                "not granting it room admin anywhere",
                name,
                cfg.authentik_username_mapping_attribute,
            )
            return None
