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


@dataclass(slots=True)
class MatrixRoom:
    """Actual room as seen via the Synapse admin API."""

    room_id: str
    canonical_alias: str | None = None
    name: str | None = None
    topic: str | None = None
    is_space: bool = False

    @classmethod
    def from_admin_api(cls, obj: dict[str, Any], *, is_space: bool = False) -> MatrixRoom:
        return cls(
            room_id=obj["room_id"],
            canonical_alias=obj.get("canonical_alias"),
            name=obj.get("name"),
            topic=obj.get("topic"),
            is_space=is_space,
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
    """A desired group→room projection, paired with the actual room if one exists."""

    authentik_group: dict[str, Any]
    desired: RoomCreateAttributes
    room: MatrixRoom | None = None

    @property
    def group_pk(self) -> str:
        return str(self.authentik_group["pk"])
