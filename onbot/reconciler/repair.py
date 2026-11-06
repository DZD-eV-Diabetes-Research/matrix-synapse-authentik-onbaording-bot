"""What a managed room should still look like after creation (pure logic, REC-09, REC-10, D16).

The reconciler used to shape a room only when it created it: the space link, the marker and the
alias were written once and never looked at again. A failure or a restart between two creation
requests, a room from before the space existed, a recreated space, or a moderator removing the alias
left the room in that state for good. These helpers say, from what a pass reads, whether a room still
has its shape and what to write when it does not. The engine (:mod:`onbot.reconciler.engine`) reads,
asks, and writes only on a difference, so a converged room costs reads and no writes.

* **Space link.** ``m.space.child`` in the space (with the room's ``suggested`` flag) and
  ``m.space.parent`` in the room.
* **Marker.** The onbot marker must be present, name the room's role, be complete, and have been
  written by the bot. The bot restores it from what it already knows about the room (a room it
  resolved by its locked, bot-written marker earlier); it never reads a role out of a marker somebody
  else wrote. Its lock is :func:`onbot.rooms.provision.markers_locked`.
* **Join rule of a visitor lobby (D16).** A lobby somebody closed by hand stays closed: a closed
  join rule written by anybody but the bot is left alone. A lobby whose feature was switched off is
  closed by the bot (REC-10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from onbot.reconciler.state import (
    AnyRoomState,
    GroupRoomState,
    OnbotRoomType,
    SpaceRoomState,
    VisitorLobbyRoomState,
    dump_room_state,
    parse_room_state,
)

SPACE_CHILD_EVENT_TYPE = "m.space.child"
SPACE_PARENT_EVENT_TYPE = "m.space.parent"
CANONICAL_ALIAS_EVENT_TYPE = "m.room.canonical_alias"

# Join rules under which a member of the space can no longer join a lobby unaided.
CLOSED_JOIN_RULES = frozenset({"invite", "knock", "private"})


# --- space link -----------------------------------------------------------------------------------


def space_child_content(server_name: str, *, suggested: bool) -> dict[str, Any]:
    """``m.space.child`` content for a room in the bot's space (``suggested`` for a lobby)."""
    return {"suggested": suggested, "via": [server_name]}


def space_parent_content(server_name: str) -> dict[str, Any]:
    return {"canonical": True, "via": [server_name]}


def _has_via(content: Mapping[str, Any] | None) -> bool:
    via = (content or {}).get("via")
    return isinstance(via, list) and bool(via)


def space_child_ok(content: Mapping[str, Any] | None, *, suggested: bool) -> bool:
    """Whether the space lists the room (a child without ``via`` is a removed link) as wanted."""
    return _has_via(content) and bool((content or {}).get("suggested", False)) is suggested


def space_parent_ok(content: Mapping[str, Any] | None) -> bool:
    return _has_via(content)


def space_child_attached(content: Mapping[str, Any] | None) -> bool:
    """Whether the space lists the room at all."""
    return _has_via(content)


# --- marker ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarkerRepair:
    """The marker to write, and why."""

    content: dict[str, Any]
    reason: str
    # Somebody else removed or rewrote it (as opposed to a marker from an older version that lacks
    # fields): logged at WARNING and reported to the admins.
    tampered: bool


def _same_role(current: AnyRoomState, expected: AnyRoomState) -> bool:
    if isinstance(expected, GroupRoomState | VisitorLobbyRoomState):
        return isinstance(current, type(expected)) and current.group_id == expected.group_id
    if isinstance(expected, SpaceRoomState):
        return isinstance(current, SpaceRoomState) and current.configured_alias == expected.configured_alias
    return type(current) is type(expected)


def marker_repair(
    event: Mapping[str, Any] | None, *, bot_id: str, expected: AnyRoomState
) -> MarkerRepair | None:
    """What to write to restore the marker of a room whose role is ``expected``; ``None``: intact.

    ``event`` is the current marker state event, with its sender. The bot decides the room's role
    from its own knowledge (``expected``), never from the marker it reads here: a marker that names
    another role or cannot be read is replaced by a fresh one. One that names the right role but was
    written by somebody else is rewritten by the bot with the same content, so a later scan (which
    trusts only markers the bot wrote, see :mod:`onbot.rooms.identity`) still recognises the room.
    """
    fresh = dump_room_state(expected)
    content = dict((event or {}).get("content") or {})
    if not content:
        return MarkerRepair(fresh, "the onbot marker was removed", tampered=True)
    room_type = OnbotRoomType(expected.room_type)
    try:
        current = parse_room_state(room_type, content)
    except ValidationError:
        return MarkerRepair(fresh, "the onbot marker could not be read", tampered=True)
    if not _same_role(current, expected):
        return MarkerRepair(fresh, "the onbot marker was rewritten to name another role", tampered=True)
    normalized = dump_room_state(current)
    sender = (event or {}).get("sender")
    if sender != bot_id:
        return MarkerRepair(normalized, f"the onbot marker was rewritten by {sender}", tampered=True)
    if normalized != content:
        # A marker from an older version: fields missing, or keys that are no longer written (the
        # Authentik URL, SEC-11).
        return MarkerRepair(normalized, "the onbot marker was incomplete", tampered=False)
    return None


# --- lobby join rule ------------------------------------------------------------------------------


def closed_join_rules() -> dict[str, Any]:
    """The join rule the bot gives a lobby whose feature was switched off (REC-10)."""
    return {"join_rule": "invite"}


def is_closed(content: Mapping[str, Any] | None) -> bool:
    return (content or {}).get("join_rule") in CLOSED_JOIN_RULES
