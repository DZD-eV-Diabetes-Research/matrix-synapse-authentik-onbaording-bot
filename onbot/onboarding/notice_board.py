"""Power-level rules that make the bot's per-user DM a read-only notice board (pure logic).

The DM the bot opens with each user is not a conversation: it carries the welcome messages and,
later, broadcast announcements. Only the bot posts. That is expressed entirely through
``m.room.power_levels``: the bot sits at 100 (room creator), the user at ``users_default: 0``, and
every action worth having — sending a message, changing state, inviting, kicking — costs 50. Element
then replaces the composer with a "you do not have permission to post in this room" banner.

Nothing here does I/O, so the rules can be exercised without a homeserver.

**Rooms created before this existed cannot be repaired.** ``trusted_private_chat`` gave the user 100,
the same as the bot, and the spec forbids changing a power-level entry greater than or equal to your
own (https://spec.matrix.org/latest/client-server-api/#mroompower_levels) — so the bot can neither
demote the user nor outrank them (Synapse's ``make_room_admin`` tops out at 100 as well). Such rooms
stay writable by their user forever; the only way out is to destroy and recreate them. See
``docs/troubleshooting.md``.
"""

from __future__ import annotations

from typing import Any

BOT_LEVEL = 100
USER_LEVEL = 0
# Every privileged action in the notice board costs this much; the user at 0 can do none of them.
GATED_LEVEL = 50

_GATED_KEYS = ("events_default", "state_default", "invite", "kick", "ban", "redact")


def notice_board_power_levels(bot_user_id: str) -> dict[str, Any]:
    """The ``power_level_content_override`` for a freshly created notice-board DM.

    Passed to ``POST /createRoom``, where it overrides the corresponding top-level keys of the
    preset's default power levels and leaves the rest (``events``, ``notifications``, …) alone.
    """
    return {
        "users": {bot_user_id: BOT_LEVEL},
        "users_default": USER_LEVEL,
        **dict.fromkeys(_GATED_KEYS, GATED_LEVEL),
    }


def power_level_drift(current: dict[str, Any], bot_user_id: str) -> dict[str, Any] | None:
    """Return the content that re-applies the notice-board rules to ``current``, or ``None``.

    ``None`` means the room already matches and no state event should be written. Keys the bot does
    not care about are preserved, as are power levels granted to other users (a second admin in the
    room is somebody's deliberate choice, not drift).
    """
    desired = notice_board_power_levels(bot_user_id)
    merged = dict(current)
    merged["users"] = {**(current.get("users") or {}), bot_user_id: BOT_LEVEL}
    for key in ("users_default", *_GATED_KEYS):
        merged[key] = desired[key]
    return None if merged == current else merged
