"""Matrix Client-Server API client (async) + the concrete :class:`MatrixEffectors`.

Phase 4. Drives the CS API over the shared async :class:`BaseApiClient` (AD-7) — *not* a Matrix
library: the library decision (``matrix-nio`` vs. raw httpx vs. ``mautrix``) is an explicit Phase 6
ADR (BATTLE_PLAN §5). Building on the base client keeps everything async, pooled and uniformly
authenticated, and lets the reconciler's :class:`~onbot.reconciler.effectors.MatrixEffectors` seam
get its concrete implementation here (the Phase 3 deferral).

Two things live here:

* :class:`ApiClientMatrix` — the CS-API operations the bot needs (room/space creation, kicks, power
  levels, room name/topic, custom state events, sending messages, account data, and the sync
  stream).
* :class:`CSApiEffectors` — adapts that client to the reconciler's ``MatrixEffectors`` protocol,
  replacing the dry-run effectors when the bot runs for real.

The sync transport is **Simplified Sliding Sync** (MSC4186, AD-3). Its endpoint is still unstable;
Phase 6 adds CS-API version negotiation. To keep that churn out of the onboarding listener, the
response is normalised to :class:`SyncResult` here, so the listener consumes membership events
without knowing the wire shape.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from onbot.clients.base import ApiError, BaseApiClient
from onbot.logging import get_logger
from onbot.models import RoomCreateAttributes

log = get_logger(__name__)

# MSC4186 Simplified Sliding Sync. Unstable path — re-validated/negotiated in Phase 6.
SLIDING_SYNC_PATH = "unstable/org.matrix.simplified_msc3575/sync"

ENCRYPTION_ALGORITHM = "m.megolm.v1.aes-sha2"


@dataclass(slots=True)
class RoomSync:
    """Normalised per-room slice of a sync response."""

    room_id: str
    timeline: list[dict[str, Any]] = field(default_factory=list)
    required_state: list[dict[str, Any]] = field(default_factory=list)

    def member_events(self) -> list[dict[str, Any]]:
        return [ev for ev in (*self.required_state, *self.timeline) if ev.get("type") == "m.room.member"]


@dataclass(slots=True)
class SyncResult:
    """Normalised sync response: the next stream position plus changed rooms."""

    pos: str | None
    rooms: list[RoomSync] = field(default_factory=list)


class ApiClientMatrix(BaseApiClient):
    """Matrix CS-API operations the bot performs as the configured bot user."""

    def __init__(
        self,
        server_url: str,
        access_token: str,
        server_name: str,
        **kwargs: Any,
    ) -> None:
        base = f"{server_url.rstrip('/')}/_matrix/client"
        # Sliding sync long-polls; allow a generous read timeout unless the caller overrides it.
        kwargs.setdefault("timeout", 60.0)
        super().__init__(base_url=base, auth_token=access_token, **kwargs)
        self.server_name = server_name

    # --- room / space creation ----------------------------------------------

    async def create_room(
        self,
        *,
        alias_localpart: str | None = None,
        name: str | None = None,
        topic: str | None = None,
        encrypted: bool = False,
        room_params: Mapping[str, Any] | None = None,
        parent_space_id: str | None = None,
        is_direct: bool = False,
        invite: list[str] | None = None,
    ) -> str:
        """Create a room (https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3createroom).

        Returns the new ``room_id``. ``room_params`` (preset/visibility/federation/…) are merged in
        verbatim; encryption and the space-parent link are added as ``initial_state`` events.
        """
        initial_state: list[dict[str, Any]] = []
        if parent_space_id:
            # https://spec.matrix.org/latest/client-server-api/#mspaceparent
            initial_state.append(
                {
                    "type": "m.space.parent",
                    "state_key": parent_space_id,
                    "content": {"canonical": True, "via": [self.server_name]},
                }
            )
        if encrypted:
            initial_state.append(
                {"type": "m.room.encryption", "content": {"algorithm": ENCRYPTION_ALGORITHM}}
            )

        body: dict[str, Any] = {**(room_params or {})}
        if alias_localpart:
            body["room_alias_name"] = alias_localpart
        if name is not None:
            body["name"] = name
        if topic is not None:
            body["topic"] = topic
        if is_direct:
            body["is_direct"] = True
        if invite:
            body["invite"] = invite
        if initial_state:
            body["initial_state"] = initial_state

        result = await self.post_json("v3/createRoom", json_body=body)
        room_id: str = result["room_id"]
        if parent_space_id:
            # https://spec.matrix.org/latest/client-server-api/#mspacechild
            await self.put_room_state_event(
                parent_space_id,
                "m.space.child",
                {"suggested": True, "via": [self.server_name]},
                state_key=room_id,
            )
        return room_id

    async def create_space(
        self, *, alias_localpart: str, name: str, topic: str, params: Mapping[str, Any]
    ) -> str:
        body: dict[str, Any] = {
            "creation_content": {"type": "m.space"},
            "room_alias_name": alias_localpart,
            "name": name,
            "topic": topic,
            **params,
        }
        result = await self.post_json("v3/createRoom", json_body=body)
        room_id: str = result["room_id"]
        return room_id

    async def create_direct_message_room(self, user_id: str) -> str:
        """Create a 1:1 invite-only DM room with ``user_id`` (G4.1)."""
        return await self.create_room(
            is_direct=True,
            invite=[user_id],
            room_params={"preset": "trusted_private_chat"},
        )

    # --- membership ----------------------------------------------------------

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
        # https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3roomsroomidkick
        body: dict[str, Any] = {"user_id": user_id}
        if reason:
            body["reason"] = reason
        await self.post_json(f"v3/rooms/{room_id}/kick", json_body=body)

    # --- room state ----------------------------------------------------------

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        """Fetch a single state event's content, or ``None`` if it is not set (HTTP 404).

        Targets the event directly (no full-state linear scan — the legacy §3 inefficiency).
        """
        path = f"v3/rooms/{room_id}/state/{event_type}"
        if state_key:
            path = f"{path}/{state_key}"
        try:
            content: dict[str, Any] = await self.get_json(path)
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return content

    async def put_room_state_event(
        self, room_id: str, event_type: str, content: Mapping[str, Any], state_key: str = ""
    ) -> None:
        path = f"v3/rooms/{room_id}/state/{event_type}"
        if state_key:
            path = f"{path}/{state_key}"
        await self.put_json(path, json_body=dict(content))

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        return await self.get_room_state_event(room_id, "m.room.power_levels") or {}

    async def set_room_power_levels(self, room_id: str, power_levels: Mapping[str, Any]) -> None:
        await self.put_room_state_event(room_id, "m.room.power_levels", power_levels)

    async def set_room_name(self, room_id: str, name: str) -> None:
        await self.put_room_state_event(room_id, "m.room.name", {"name": name})

    async def set_room_topic(self, room_id: str, topic: str) -> None:
        await self.put_room_state_event(room_id, "m.room.topic", {"topic": topic})

    # --- messaging -----------------------------------------------------------

    async def send_text_message(self, room_id: str, body: str) -> str:
        """Send a plain-text message; returns the event id. Uses a unique transaction id."""
        txn = uuid.uuid4().hex
        result = await self.put_json(
            f"v3/rooms/{room_id}/send/m.room.message/{txn}",
            json_body={"msgtype": "m.text", "body": body},
        )
        event_id: str = result["event_id"]
        return event_id

    # --- account data --------------------------------------------------------

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        try:
            data: dict[str, Any] = await self.get_json(f"v3/user/{user_id}/account_data/{data_type}")
        except ApiError as exc:
            if exc.status_code == 404:
                return {}
            raise
        return data

    async def set_account_data(self, user_id: str, data_type: str, content: Mapping[str, Any]) -> None:
        await self.put_json(f"v3/user/{user_id}/account_data/{data_type}", json_body=dict(content))

    # --- sync stream (Simplified Sliding Sync, MSC4186) ----------------------

    async def sliding_sync(self, pos: str | None = None, *, timeout_ms: int = 30000) -> SyncResult:
        """One Simplified Sliding Sync round-trip, normalised to :class:`SyncResult`.

        Long-polls server-side for up to ``timeout_ms``; pass the returned ``pos`` back to continue
        the stream. Subscribes to all rooms and asks for member state so the listener can react to
        joins (AD-3). The wire shape is unstable (Phase 6 negotiation) — kept behind this method.
        """
        params: dict[str, Any] = {"timeout": timeout_ms}
        if pos:
            params["pos"] = pos
        body = {
            "lists": {
                "onbot": {
                    "ranges": [[0, 1000]],
                    "required_state": [["m.room.member", "*"]],
                    "timeline_limit": 50,
                }
            }
        }
        data = await self.request_json("POST", SLIDING_SYNC_PATH, params=params, json_body=body)
        data = data or {}
        rooms = [
            RoomSync(
                room_id=room_id,
                timeline=list((room or {}).get("timeline", [])),
                required_state=list((room or {}).get("required_state", [])),
            )
            for room_id, room in (data.get("rooms") or {}).items()
        ]
        return SyncResult(pos=data.get("pos"), rooms=rooms)


class CSApiEffectors:
    """Concrete :class:`~onbot.reconciler.effectors.MatrixEffectors` backed by the CS API.

    Replaces ``DryRunEffectors`` when the bot runs for real (Phase 3 deferral resolved).
    """

    def __init__(self, client: ApiClientMatrix) -> None:
        self.client = client

    async def create_group_room(self, attrs: RoomCreateAttributes, parent_space_id: str | None) -> str:
        return await self.client.create_room(
            alias_localpart=attrs.alias,
            name=attrs.name,
            topic=attrs.topic,
            encrypted=attrs.encrypted,
            room_params=attrs.room_params,
            parent_space_id=parent_space_id,
        )

    async def create_space(self, *, alias: str, name: str, topic: str, params: dict[str, Any]) -> str:
        return await self.client.create_space(alias_localpart=alias, name=name, topic=topic, params=params)

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
        await self.client.kick_user(room_id, user_id, reason)

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        return await self.client.get_room_power_levels(room_id)

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        await self.client.set_room_power_levels(room_id, power_levels)

    async def set_room_name(self, room_id: str, name: str) -> None:
        await self.client.set_room_name(room_id, name)

    async def set_room_topic(self, room_id: str, topic: str) -> None:
        await self.client.set_room_topic(room_id, topic)

    async def put_room_state(self, room_id: str, event_type: str, content: dict[str, Any]) -> None:
        await self.client.put_room_state_event(room_id, event_type, content)
