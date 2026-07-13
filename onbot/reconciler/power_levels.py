"""Per-room power-level computation (pure logic).

Ported from legacy ``power_level_manager.py`` and made side-effect-free. Two improvements over
legacy:

* **Withdrawal (G8.4):** legacy left elevated power levels sticky forever. Here we compute the full
  *desired* level for every managed room member and :func:`merge_power_levels` removes the explicit
  entry for any managed user who no longer qualifies — converging both up and down (AD-2).
* **Highest wins (G8.3):** when a user is in several power-level groups we take the max; superuser
  room-admin (G8.2) overrides group levels.

Non-managed users (e.g. the bot, manually-promoted accounts) are never touched.
"""

from __future__ import annotations

from collections.abc import Iterable
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
