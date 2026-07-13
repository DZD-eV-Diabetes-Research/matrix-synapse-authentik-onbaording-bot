"""Power-level rules that make the bot's per-user DM a read-only notice board (pure logic).

The DM the bot opens with each user is not a conversation: it carries the welcome messages and,
later, broadcast announcements. Only the bot posts. That is expressed entirely through
``m.room.power_levels``: the user sits at ``users_default: 0`` and every action worth having —
sending a message, changing state, inviting, kicking — costs 50. Element then replaces the composer
with a "you do not have permission to post in this room" banner.

**The bot's own power is not in this content.** The bot creates the DM, so it is the room *creator*.
Under room version 12 (the spec default) the creator holds an infinite, immutable power level and is
deliberately absent from ``m.room.power_levels`` — the auth rules reject a power-levels event that
names a creator in its ``users`` map — so this override omits ``users`` entirely and lets the server
seat the creator. On older room versions the server's default power levels still put the creator at
100 in ``users``; either way the bot outranks the user without being named here. Absent ≠ powerless.
See ``docs/adr/0011-room-version-12.md``.

Nothing here does I/O, so the rules can be exercised without a homeserver.

**Rooms created before the notice-board change cannot be repaired.** ``trusted_private_chat`` gave the
user 100, the same as the bot, and the spec forbids changing a power-level entry greater than or equal
to your own (https://spec.matrix.org/latest/client-server-api/#mroompower_levels) — so the bot can
neither demote the user nor outrank them (Synapse's ``make_room_admin`` tops out at 100 as well). Such
rooms stay writable by their user forever; the only way out is to destroy and recreate them. See
``docs/troubleshooting.md``. Rooms created as v12 cannot fall into this trap: no user can ever match
the creator's infinite level (:func:`onbot.reconciler.power_levels.legacy_user_matches_or_outranks_creator`).
"""

from __future__ import annotations

from typing import Any

USER_LEVEL = 0
# Every privileged action in the notice board costs this much; the user at 0 can do none of them.
GATED_LEVEL = 50

_GATED_KEYS = ("events_default", "state_default", "invite", "kick", "ban", "redact")


def notice_board_power_levels() -> dict[str, Any]:
    """The ``power_level_content_override`` for a freshly created notice-board DM.

    Passed to ``POST /createRoom``, where it overrides the corresponding top-level keys of the
    preset's default power levels and leaves the rest (``events``, ``notifications``, and crucially
    ``users`` — which the server fills with the creator on older room versions) alone.
    """
    return {
        "users_default": USER_LEVEL,
        **dict.fromkeys(_GATED_KEYS, GATED_LEVEL),
    }


def power_level_drift(current: dict[str, Any]) -> dict[str, Any] | None:
    """Return the content that re-applies the notice-board rules to ``current``, or ``None``.

    ``None`` means the room already matches and no state event should be written. Keys the bot does
    not care about are preserved, and the ``users`` map is left exactly as found: the bot (creator)
    is authoritative whether or not it is listed there, and any level granted to another user is
    somebody's deliberate choice, not drift.
    """
    desired = notice_board_power_levels()
    merged = dict(current)
    for key in ("users_default", *_GATED_KEYS):
        merged[key] = desired[key]
    return None if merged == current else merged
