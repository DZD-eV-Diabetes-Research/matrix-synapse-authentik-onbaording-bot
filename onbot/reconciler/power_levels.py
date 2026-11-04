"""Per-room power-level computation (pure logic).

Ported from legacy ``power_level_manager.py`` and made side-effect-free. Two improvements over
legacy:

* **Withdrawal (G8.4):** legacy left elevated power levels sticky forever. Here we compute the full
  *desired* level for every managed room member and :func:`merge_power_levels` removes the explicit
  entry for any managed user who no longer qualifies — converging both up and down (AD-2).
* **Highest wins (G8.3):** when a user is in several power-level groups we take the max; superuser
  room-admin (G8.2) overrides group levels.

What the engine writes is planned by :func:`plan_power_levels`, which adds two rules on top:

* **Only the bot's own grants are withdrawn (D10, REC-08).** The bot records every entry it wrote;
  when the user stops qualifying, only a recorded entry whose value is still what the bot wrote is
  removed. Levels set by humans, and bot grants a human has since changed, stay untouched.
* **Never grant what cannot be taken back (REC-05).** In rooms where the bot is not an infinite
  creator (every pre-v12 room) it never grants its own level or more, and it does not try to change
  users at or above its level, or extra creators: those are reported instead.

The bot itself is never written (ADR-0011, REC-12).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from onbot.models import MappedUser
from onbot.utils import get_nested_dict_val_by_path

ROOM_ADMIN_LEVEL = 100


@dataclass(frozen=True, slots=True)
class PowerLevelGroup:
    """An Authentik group that confers a Matrix power level on its members."""

    member_pks: set[str]
    level: int


def extract_power_level_groups(
    groups: Iterable[dict[str, Any]], attribute_path: str
) -> list[PowerLevelGroup]:
    """Build :class:`PowerLevelGroup` entries from Authentik groups carrying the power-level attr.

    ``attribute_path`` is the attribute name/path *within* the group's ``attributes`` object (the
    config value of ``authentik_group_attr_for_matrix_power_level``).
    """
    key_path = ("attributes." + attribute_path).split(".")
    result: list[PowerLevelGroup] = []
    for group in groups:
        level = get_nested_dict_val_by_path(group, key_path, fallback_val=None)
        if not isinstance(level, int) or isinstance(level, bool):
            continue
        result.append(PowerLevelGroup(member_pks=set(group.get("users", [])), level=level))
    return result


def compute_desired_user_levels(
    members: Iterable[MappedUser],
    power_level_groups: Iterable[PowerLevelGroup],
    *,
    make_superusers_admin: bool,
    admin_level: int = ROOM_ADMIN_LEVEL,
) -> dict[str, int]:
    """Desired explicit power level per managed member who qualifies for one (> default).

    Members with no group level and no superuser admin are omitted (they should sit at the room
    default); :func:`merge_power_levels` turns that omission into a withdrawal.
    """
    groups = list(power_level_groups)
    desired: dict[str, int] = {}
    for member in members:
        level: int | None = None
        member_pk = member.authentik_obj.get("pk")
        for group in groups:
            if member_pk in group.member_pks:
                level = group.level if level is None else max(level, group.level)
        if make_superusers_admin and member.is_superuser:
            level = admin_level
        if level is not None:
            desired[member.mxid] = level
    return desired


def apply_global_room_admins(
    desired: dict[str, int],
    global_admin_mxids: Iterable[str],
    *,
    level: int = ROOM_ADMIN_LEVEL,
) -> dict[str, int]:
    """Seat every global room admin at ``level``, overriding whatever their groups computed.

    Deliberately an override and not a ``max``: the point of ``global_room_admins`` is that the
    level does not depend on group membership, so a global admin who is *also* in a power-level
    group carrying 50 still ends up at 100 rather than at the group's number.

    The returned mxids must be added to ``managed_mxids`` for :func:`merge_power_levels`, otherwise
    the entries are computed and then dropped on the floor. Removing a name from the config
    withdraws the level on the next pass by the same route: it is no longer desired, but it is still
    managed while it remains in the room.
    """
    return {**desired, **dict.fromkeys(global_admin_mxids, level)}


def merge_power_levels(
    current_users: dict[str, int],
    desired: dict[str, int],
    managed_mxids: set[str],
) -> dict[str, int]:
    """Merge desired levels into the current ``users`` map, withdrawing stale managed entries.

    Only ``managed_mxids`` are added/updated/removed; everyone else is left untouched. Returns a new
    dict (the caller decides whether it changed before writing).

    Note this never adds the bot: the bot is the room *creator* and, under room version 12, is absent
    from ``users`` by design (see :func:`legacy_user_matches_or_outranks_creator`). It is not in
    ``managed_mxids``, so its absence here is correct, not a demotion.
    """
    merged = dict(current_users)
    for mxid in managed_mxids:
        if mxid in desired:
            merged[mxid] = desired[mxid]
        else:
            merged.pop(mxid, None)
    return merged


def legacy_user_matches_or_outranks_creator(
    power_levels: dict[str, Any], user_id: str, creator_id: str
) -> bool:
    """Is ``user_id`` seated at or above the room creator's power level? (v12-aware.)

    This is the single rule the v12 audit enforces on every "is this user ≥ the bot?" read, and the
    selection predicate for the destructive ``recreate-dm-rooms`` migration: the bot creates the DM,
    so ``creator_id`` is the bot.

    Under **room version 12** the creator holds an infinite, immutable power level and is
    deliberately **absent** from ``m.room.power_levels`` — the auth rules reject naming a creator in
    the ``users`` map. So an absent creator is not a powerless one: it is infinite, and *nobody* can
    match it. When ``creator_id`` is not in ``users`` we return ``False`` unconditionally. This is
    what makes a v12 DM immune to the un-demotable-user trap the migration exists to repair.

    On **older room versions** the creator is an ordinary ``users`` entry (``trusted_private_chat``
    seated both the bot and the invitee at 100). There the creator *can* be matched, and a user at or
    above it is the legacy room the migration must recreate — so we compare levels, reading an
    unlisted user at ``users_default``.

    Reading ``creator_id`` as absent-means-powerless (level ``users_default``) — the pre-v12
    assumption baked into naive power-level code — would wrongly flag every healthy v12 room as
    needing migration. Do not reintroduce it.
    """
    users = power_levels.get("users") or {}
    if creator_id not in users:
        return False
    creator_level = users[creator_id]
    user_level = users.get(user_id, power_levels.get("users_default", 0))
    return bool(user_level >= creator_level)


# Room versions in which the creators hold an infinite, immutable power level and may not appear in
# `m.room.power_levels` (ADR-0011). Every stable version from 12 on keeps the rule; the unstable
# predecessor of v12 had it too.
_PRIVILEGED_CREATOR_UNSTABLE_VERSIONS = frozenset({"org.matrix.hydra.11"})


def creators_are_privileged(room_version: str | None) -> bool:
    """Whether room creators are infinite in this room version (v12 and later).

    An unknown version reads as pre-v12. That is the safe side: it gives the bot a finite level, so
    it only ever grants less and refuses more, never the reverse.
    """
    if room_version is None:
        return False
    if room_version.isdigit():
        return int(room_version) >= 12
    return room_version in _PRIVILEGED_CREATOR_UNSTABLE_VERSIONS


def _as_level(value: Any, default: int) -> int:
    """A power level as an int. Rooms before v10 may store levels as strings."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


@dataclass(frozen=True, slots=True)
class RoomAuthority:
    """What the bot may change in one room.

    ``bot_level`` is ``None`` when the bot is an infinite creator (a v12 room it created): it may then
    change and kick everyone except other creators. Otherwise it is the bot's ordinary level, and the
    pre-v12 auth rules apply: the bot may only change or kick users strictly below it.
    ``creators`` are the *other* privileged creators (v12 ``additional_creators``, or the creator of
    a v12 room the bot did not create): untouchable, and never to be named in ``users``.
    """

    bot_level: int | None
    creators: frozenset[str]
    users_default: int

    @property
    def max_grant(self) -> int | None:
        """The highest level the bot hands out here, or ``None`` for no limit.

        One below its own: a user at the bot's level can never be demoted or kicked by the bot again.
        """
        return None if self.bot_level is None else self.bot_level - 1

    def level_of(self, users: Mapping[str, Any], mxid: str) -> int:
        return _as_level(users.get(mxid), self.users_default)

    def why_untouchable(self, mxid: str, level: int) -> str | None:
        """Why the bot cannot change this user's level or kick them, or ``None`` if it can."""
        if mxid in self.creators:
            return "is a room creator (infinite power level)"
        if self.bot_level is not None and level >= self.bot_level:
            return f"is at power level {level}, not below the bot's own {self.bot_level}"
        return None


def room_authority(
    power_levels: Mapping[str, Any],
    bot_id: str,
    *,
    room_version: str | None,
    creators: Iterable[str],
) -> RoomAuthority:
    """Derive the bot's :class:`RoomAuthority` from a room's power levels and create event."""
    users = power_levels.get("users") or {}
    users_default = _as_level(power_levels.get("users_default"), 0)
    creator_set = frozenset(creators)
    if not creators_are_privileged(room_version):
        # Pre-v12: creators are ordinary `users` entries, the bot included.
        return RoomAuthority(_as_level(users.get(bot_id), users_default), frozenset(), users_default)
    bot_level = None if bot_id in creator_set else _as_level(users.get(bot_id), users_default)
    return RoomAuthority(bot_level, creator_set - {bot_id}, users_default)


@dataclass(frozen=True, slots=True)
class PowerLevelPlan:
    """The outcome of :func:`plan_power_levels` for one room."""

    users: dict[str, Any]
    """The ``users`` map to write (equal to the current one when nothing changes)."""
    granted: dict[str, int]
    """The bot's record of the entries it owns after this write (D10)."""
    capped: dict[str, int]
    """Users granted less than wanted (the wanted level), because the bot must stay above them."""
    blocked: dict[str, str]
    """Users whose level should change but cannot, with the reason. Reported, never retried in-pass."""
    released: tuple[str, ...]
    """Users whose bot-granted entry a human changed: dropped from the record, left alone."""


def plan_power_levels(
    current_users: Mapping[str, Any],
    desired: Mapping[str, int],
    recorded: Mapping[str, int | None],
    authority: RoomAuthority,
    *,
    bot_id: str,
) -> PowerLevelPlan:
    """Plan the ``users`` map for one room.

    * Every ``desired`` user the bot may change is set to the desired level, capped at
      :attr:`RoomAuthority.max_grant`, and recorded as the bot's grant. A human edit to such a user is
      overwritten: they qualify for exactly this level.
    * A ``recorded`` user who is no longer desired loses the entry, but only while it still holds the
      level the bot wrote (``None`` in ``recorded`` means "trust it", for records that predate levels).
      If a human changed it since, the entry is theirs: it stays and leaves the record.
    * Nobody else is touched, so a level a human set for anyone is never withdrawn.
    * A change the bot may not make (a creator, a user at or above a finite bot level) is skipped and
      reported in ``blocked``; a recorded entry stays recorded so it can be withdrawn later.
    """
    merged: dict[str, Any] = dict(current_users)
    granted: dict[str, int] = {}
    capped: dict[str, int] = {}
    blocked: dict[str, str] = {}
    released: list[str] = []

    for mxid, wanted in sorted(desired.items()):
        if mxid == bot_id:
            continue
        current = current_users.get(mxid)
        why = authority.why_untouchable(mxid, authority.level_of(current_users, mxid))
        if why is not None:
            if current is not None and _as_level(current, authority.users_default) == wanted:
                granted[mxid] = wanted  # already exactly what is wanted: own it, nothing to write
            else:
                blocked[mxid] = f"wanted at {wanted}, but {why}"
            continue
        level = wanted
        if authority.max_grant is not None and wanted > authority.max_grant:
            level = authority.max_grant
            capped[mxid] = wanted
        merged[mxid] = level
        granted[mxid] = level

    for mxid, recorded_level in sorted(recorded.items()):
        if mxid in desired or mxid == bot_id:
            continue
        current = current_users.get(mxid)
        if current is None:
            continue  # already gone; forget it
        level = _as_level(current, authority.users_default)
        if recorded_level is not None and level != recorded_level:
            released.append(mxid)
            continue
        why = authority.why_untouchable(mxid, level)
        if why is not None:
            blocked[mxid] = f"should lose power level {level}, but {why}"
            granted[mxid] = level if recorded_level is None else recorded_level
            continue
        del merged[mxid]

    return PowerLevelPlan(
        users=merged,
        granted=granted,
        capped=capped,
        blocked=blocked,
        released=tuple(released),
    )
