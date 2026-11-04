"""Domain models shared across the reconciler.

These are lightweight typed views over the raw Authentik / Synapse-admin API dicts (which stay as
``dict`` since their shapes are owned upstream). Replaces the legacy ``UserMap`` / ``Group2RoomMap``
god-objects with plain, side-effect-free dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RoomCreateAttributes:
    """Desired Matrix room attributes computed from an Authentik group."""

    alias: str
    canonical_alias: str
    name: str | None = None
    topic: str | None = None
    room_params: dict[str, Any] = field(default_factory=dict)
    encrypted: bool = True
    avatar_source_url: str | None = None


@dataclass(slots=True)
class MatrixRoom:
    """Actual room as seen via the Synapse admin API.

    Carries only what the admin *List Room* API actually returns, because that is where every
    instance comes from. Notably there is **no topic**: the list response has ``name`` and
    ``canonical_alias`` but no topic (only the per-room Room Details API has one), so a ``topic``
    field here would read ``None`` for every room and silently break any comparison against it. Code
    that needs the topic reads ``m.room.topic`` from room state.
    """

    room_id: str
    canonical_alias: str | None = None
    name: str | None = None
    is_space: bool = False
    # Both come with the listing (``version``, ``creator``) and decide what the bot may change in the
    # room's power levels: room version 12 makes creators infinite (ADR-0011, REC-05).
    room_version: str | None = None
    creator: str | None = None

    @classmethod
    def from_admin_api(cls, obj: dict[str, Any], *, is_space: bool = False) -> MatrixRoom:
        version = obj.get("version")
        return cls(
            room_id=obj["room_id"],
            canonical_alias=obj.get("canonical_alias"),
            name=obj.get("name"),
            is_space=is_space,
            room_version=str(version) if version is not None else None,
            creator=obj.get("creator"),
        )


@dataclass(slots=True)
class MappedUser:
    """An Authentik user resolved to its (MAS-provisioned) Matrix account."""

    authentik_obj: dict[str, Any]
    mxid: str
    matrix_obj: dict[str, Any] | None = None

    @property
    def is_superuser(self) -> bool:
        return bool(self.authentik_obj.get("is_superuser"))

    @property
    def group_pks(self) -> set[str]:
        return {g["pk"] for g in self.authentik_obj.get("groups_obj", [])}


@dataclass(slots=True)
class GroupRoomMap:
    """A desired group→room projection, paired with the actual room if one exists.

    A group may also have a *lobby* — an open room beside the private group room (ADR-0012). When the
    lobby is enabled for this group, ``lobby_desired`` holds its computed attributes and ``lobby``
    holds the actual lobby room if one exists yet. Both are ``None`` when the group has no lobby.
    """

    authentik_group: dict[str, Any]
    desired: RoomCreateAttributes
    room: MatrixRoom | None = None
    lobby_desired: RoomCreateAttributes | None = None
    lobby: MatrixRoom | None = None

    @property
    def group_pk(self) -> str:
        return str(self.authentik_group["pk"])
