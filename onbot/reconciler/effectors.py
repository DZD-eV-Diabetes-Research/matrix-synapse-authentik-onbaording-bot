"""Matrix-side effectors — the write seam between the reconciler and the Matrix CS API.

The reconciler decides *what* should change (pure logic in the sibling modules); these effectors
perform the Matrix Client-Server operations that the Synapse admin API cannot do (room/space
creation, kicks, power levels, room name/topic, custom state events).

The concrete CS-API implementation lands in Phase 4 (clients/matrix.py) once the Matrix-library
decision is made (AD / Phase 6). For Phase 3 we ship the :class:`MatrixEffectors` protocol plus a
:class:`DryRunEffectors` that logs intended actions — so ``reconcile-once`` runs end-to-end against
real read APIs without mutating anything (the dry-run-by-default safety principle, Q6).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

from onbot.logging import get_logger
from onbot.models import RoomCreateAttributes

log = get_logger(__name__)


@runtime_checkable
class MatrixEffectors(Protocol):
    async def create_group_room(self, attrs: RoomCreateAttributes, parent_space_id: str | None) -> str: ...

    async def create_space(self, *, alias: str, name: str, topic: str, params: dict[str, Any]) -> str: ...

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None: ...

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]: ...

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None: ...

    async def set_room_name(self, room_id: str, name: str) -> None: ...

    async def set_room_topic(self, room_id: str, topic: str) -> None: ...

    async def put_room_state(self, room_id: str, event_type: str, content: dict[str, Any]) -> None: ...


class DryRunEffectors:
    """Logs every intended write and mutates nothing. Default until Phase 4 wires the CS client."""

    def _synthetic_room_id(self, server_name: str = "dry-run") -> str:
        return f"!dryrun-{uuid.uuid4().hex[:12]}:{server_name}"

    async def create_group_room(self, attrs: RoomCreateAttributes, parent_space_id: str | None) -> str:
        log.info(
            "[dry-run] would create group room alias=%s name=%r in space=%s",
            attrs.canonical_alias,
            attrs.name,
            parent_space_id,
        )
        return self._synthetic_room_id()

    async def create_space(self, *, alias: str, name: str, topic: str, params: dict[str, Any]) -> str:
        log.info("[dry-run] would create space alias=%s name=%r", alias, name)
        return self._synthetic_room_id()

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
        log.info("[dry-run] would kick %s from %s (%s)", user_id, room_id, reason)

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        return {}

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        log.info("[dry-run] would set power levels in %s -> %s", room_id, power_levels)

    async def set_room_name(self, room_id: str, name: str) -> None:
        log.info("[dry-run] would set name of %s -> %r", room_id, name)

    async def set_room_topic(self, room_id: str, topic: str) -> None:
        log.info("[dry-run] would set topic of %s -> %r", room_id, topic)

    async def put_room_state(self, room_id: str, event_type: str, content: dict[str, Any]) -> None:
        log.info("[dry-run] would set state %s on %s", event_type, room_id)
