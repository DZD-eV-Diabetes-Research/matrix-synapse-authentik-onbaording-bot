"""Group membership → room membership (pure diff logic).

Ported from legacy ``Bot.sync_users_and_rooms`` / ``sync_users_and_space``. Fixes the BATTLE_PLAN §3
bug where membership was tested with substring ``in`` (``grp["pk"] in room...["pk"]``) instead of
set membership — here it is exact set containment.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from onbot.models import MappedUser


@dataclass(frozen=True, slots=True)
class MembershipDiff:
    to_add: list[str] = field(default_factory=list)
    to_kick: list[str] = field(default_factory=list)


def desired_room_members(group_pk: str, mapped_users: Iterable[MappedUser]) -> set[str]:
    """MXIDs that should be in the room mapped to ``group_pk`` (members of that Authentik group)."""
    return {u.mxid for u in mapped_users if group_pk in u.group_pks}


def diff_room_membership(
    desired_mxids: set[str],
    actual_member_ids: Iterable[str],
    *,
    kick_enabled: bool,
    protected_ids: Iterable[str] = (),
) -> MembershipDiff:
    """Compute adds/kicks to converge actual room membership onto ``desired_mxids``.

    ``kick_enabled`` gates removals (G3.2 is toggleable). ``protected_ids`` (e.g. the bot itself) are
    never kicked.
    """
    actual = set(actual_member_ids)
    protected = set(protected_ids)
    to_add = sorted(desired_mxids - actual)
    to_kick: list[str] = []
    if kick_enabled:
        to_kick = sorted((actual - desired_mxids) - protected)
    return MembershipDiff(to_add=to_add, to_kick=to_kick)


def diff_space_membership(
    mapped_users: Iterable[MappedUser], actual_member_ids: Iterable[str]
) -> MembershipDiff:
    """All synced users should be in the parent space (G1.3); we only ever add, never kick."""
    desired = {u.mxid for u in mapped_users}
    to_add = sorted(desired - set(actual_member_ids))
    return MembershipDiff(to_add=to_add)
