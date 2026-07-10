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


class AdminRoomState(_OnbotRoomState):
    """Marks the operator control room as bot-managed, and remembers what it pinned there.

    ``help_text_hash`` makes the pinned help idempotent: the bot re-posts and re-pins only when the
    text actually changed, instead of leaving another copy behind on every restart.
    """

    room_type: Literal[OnbotRoomType.admin_room] = OnbotRoomType.admin_room
    help_text_hash: str | None = None
    help_event_id: str | None = None


AnyRoomState = SpaceRoomState | GroupRoomState | DirectRoomState | AdminRoomState

_STATE_MODEL_BY_TYPE: dict[OnbotRoomType, type[AnyRoomState]] = {
    OnbotRoomType.space: SpaceRoomState,
    OnbotRoomType.group_room: GroupRoomState,
    OnbotRoomType.direct_room: DirectRoomState,
    OnbotRoomType.admin_room: AdminRoomState,
}


def parse_room_state(room_type: OnbotRoomType, content: dict[str, Any]) -> AnyRoomState:
    """Validate raw state-event content into the matching schema for ``room_type``."""
    model = _STATE_MODEL_BY_TYPE[room_type]
    return model.model_validate(content)


def dump_room_state(state: AnyRoomState) -> dict[str, Any]:
    """Serialise a room-state model to JSON-able content for a Matrix state event."""
    return state.model_dump(mode="json")
