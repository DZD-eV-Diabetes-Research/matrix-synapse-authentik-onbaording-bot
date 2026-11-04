"""Versioned onbot room-state schemas.

The bot has no database; it persists its own bookkeeping as **custom Matrix room-state events**
(AD-1 keeps this idea). Event types are namespaced with the reversed server name, e.g.
``org.company.onbot.group_room``. Legacy stored these as bare, unversioned dicts; we give them
explicit, validated pydantic schemas carrying a ``schema_version`` so future migrations are possible.

This module is pure (no I/O): the reconciler reads/writes these via the Matrix client (Phase 4).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class OnbotRoomType(StrEnum):
    space = "space"
    group_room = "group_room"
    direct_room = "direct_room"
    admin_room = "admin_room"
    visitor_lobby = "visitor_lobby"


def event_type_name(server_name: str, room_type: OnbotRoomType | str) -> str:
    """Fully-qualified custom state event type, e.g. ``org.company.onbot.group_room``.

    Mirrors the legacy reversed-domain scheme so existing rooms keep matching.
    """
    rt = room_type.value if isinstance(room_type, OnbotRoomType) else room_type
    reversed_domain = ".".join(reversed(server_name.split(".")))
    return f"{reversed_domain}.onbot.{rt}"


class _OnbotRoomState(BaseModel):
    schema_version: int = SCHEMA_VERSION
    authentik_server: str | None = None
    avatar_source_url: str | None = None
    # The `global_room_admins` the bot last seated at PL 100 in this room. Recorded because the
    # power-level pass can only withdraw a level it knows it granted: a global admin is usually not a
    # member of the room's Authentik group, so once their name leaves the config there is nothing
    # left to derive them from, and their PL 100 entry would outlive their membership forever. That
    # matters most in a visitor lobby, which anyone in the space may join: a stale entry there is a
    # standing invitation to walk back in as a room admin.
    global_admins: list[str] = Field(default_factory=list)


class SpaceRoomState(_OnbotRoomState):
    room_type: Literal[OnbotRoomType.space] = OnbotRoomType.space


class GroupRoomState(_OnbotRoomState):
    room_type: Literal[OnbotRoomType.group_room] = OnbotRoomType.group_room
    group_id: str


class DirectRoomState(_OnbotRoomState):
    room_type: Literal[OnbotRoomType.direct_room] = OnbotRoomType.direct_room
    user_id: str
    marked_for_disabling_timestamp: float | None = None
    disabled_user_timestamp: float | None = None
    welcome_messages_sent: dict[str, str] = Field(default_factory=dict)
    # Set once, when the bot force-joined the user at room creation. Its presence is what stops a
    # user who deliberately left the notice board from being dragged back in on every reconcile.
    # Whole seconds, not a float: Matrix's canonical JSON has no floats and Synapse rejects a state
    # event carrying one with M_BAD_JSON.
    # https://spec.matrix.org/latest/appendices/#canonical-json
    force_joined_at: int | None = None


class VisitorLobbyRoomState(_OnbotRoomState):
    """Marks a room as a group's visitor lobby, not the group room itself.

    Carries the same ``group_id`` as the group room it fronts, so obsolete-room teardown can take the
    lobby down with the group room when the Authentik group disappears. The distinct ``room_type`` is
    the safety rail: without it discovery would read the lobby as a group room, project Authentik
    membership onto it, and kick every visitor. A lobby's membership is deliberately *not* a mirror of
    the group (ADR-0012).
    """

    room_type: Literal[OnbotRoomType.visitor_lobby] = OnbotRoomType.visitor_lobby
    group_id: str


class AdminRoomState(_OnbotRoomState):
    """Marks the operator control room as bot-managed, and remembers what it pinned there.

    ``help_text_hash`` makes the pinned help idempotent: the bot re-posts and re-pins only when the
    text actually changed, instead of leaving another copy behind on every restart.
    """

    room_type: Literal[OnbotRoomType.admin_room] = OnbotRoomType.admin_room
    help_text_hash: str | None = None
    help_event_id: str | None = None


class GrantedPowerLevels(BaseModel):
    """The power-level entries the bot itself wrote into one room, and the level it wrote (D10).

    Lives in the bot's **per-room account data**, not in room state: only the bot can read or write
    it, so a room moderator cannot add a name and have the bot demote somebody for them, or remove one
    to keep a level forever. The power-level pass withdraws an entry only if it is recorded here *and*
    still holds the recorded level; one a human changed since is theirs (see
    :func:`~onbot.reconciler.power_levels.plan_power_levels`).
    """

    schema_version: int = SCHEMA_VERSION
    users: dict[str, int] = Field(default_factory=dict)


def granted_power_levels_type(server_name: str) -> str:
    """Account data type of :class:`GrantedPowerLevels`, e.g. ``org.company.onbot.granted_power_levels``."""
    return event_type_name(server_name, "granted_power_levels")


AnyRoomState = SpaceRoomState | GroupRoomState | DirectRoomState | AdminRoomState | VisitorLobbyRoomState

_STATE_MODEL_BY_TYPE: dict[OnbotRoomType, type[AnyRoomState]] = {
    OnbotRoomType.space: SpaceRoomState,
    OnbotRoomType.group_room: GroupRoomState,
    OnbotRoomType.direct_room: DirectRoomState,
    OnbotRoomType.admin_room: AdminRoomState,
    OnbotRoomType.visitor_lobby: VisitorLobbyRoomState,
}


def parse_room_state(room_type: OnbotRoomType, content: dict[str, Any]) -> AnyRoomState:
    """Validate raw state-event content into the matching schema for ``room_type``."""
    model = _STATE_MODEL_BY_TYPE[room_type]
    return model.model_validate(content)


def dump_room_state(state: AnyRoomState) -> dict[str, Any]:
    """Serialise a room-state model to JSON-able content for a Matrix state event."""
    return state.model_dump(mode="json")
