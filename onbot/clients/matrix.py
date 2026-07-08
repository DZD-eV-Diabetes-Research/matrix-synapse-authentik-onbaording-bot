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
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from onbot.media import MediaUploader

from onbot.auth.token_provider import TokenProvider
from onbot.clients.base import ApiError, BaseApiClient
from onbot.clients.versions import CLIENT_VERSIONS_PATH, ServerVersions
from onbot.logging import get_logger
from onbot.models import RoomCreateAttributes

log = get_logger(__name__)

# MSC4186 Simplified Sliding Sync. Unstable path — gated by version negotiation (Phase 6).
SLIDING_SYNC_PATH = "unstable/org.matrix.simplified_msc3575/sync"

ENCRYPTION_ALGORITHM = "m.megolm.v1.aes-sha2"


class SyncNotSupportedError(RuntimeError):
    """The homeserver does not advertise Simplified Sliding Sync (MSC4186)."""


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
        access_token: str | None = None,
        *,
        server_name: str,
        token_provider: TokenProvider | None = None,
        **kwargs: Any,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        base = f"{self.server_url}/_matrix/client"
        # Sliding sync long-polls; allow a generous read timeout unless the caller overrides it.
        kwargs.setdefault("timeout", 60.0)
        super().__init__(base_url=base, auth_token=access_token, token_provider=token_provider, **kwargs)
        self.server_name = server_name
        self._versions: ServerVersions | None = None

    # --- version negotiation (Phase 6) --------------------------------------

    async def negotiate_versions(self) -> ServerVersions:
        """Fetch + cache ``GET /_matrix/client/versions`` (the bot's capability source of truth)."""
        payload = await self.get_json(CLIENT_VERSIONS_PATH)
        self._versions = ServerVersions.from_payload(payload)
        log.info(
            "negotiated CS-API: sliding_sync=%s authenticated_media=%s",
            self._versions.supports_simplified_sliding_sync(),
            self._versions.supports_authenticated_media(),
        )
        return self._versions

    @property
    def versions(self) -> ServerVersions | None:
        """Cached negotiation result, or ``None`` until :meth:`negotiate_versions` has run."""
        return self._versions

    # --- device registration (MAS compatibility) ----------------------------

    async def ensure_device_registered(self) -> None:
        """Register the bot's device so it can send messages under MAS.

        A MAS-issued token (a ``mas-cli`` compatibility token or a client login) carries a device
        id, but Synapse only persists a ``devices`` row once the client uploads device keys. Until
        then, *sending a message* — which records the transaction id per device — fails with a
        foreign-key 500 (``event_txn_id_device_id``). State-only writes (room create, power levels,
        account data) are unaffected, which is why this only bites the welcome DM flow.

        The bot advertises **no** encryption algorithms (ADR-0009: it operates outside encrypted
        rooms), so this is purely the device-bookkeeping registration, not an e2ee opt-in. Verified
        against the Phase 7b live stack; idempotent. Best-effort: never blocks startup.
        """
        try:
            whoami = await self.get_json("v3/account/whoami")
            device_id = whoami.get("device_id")
            if not device_id:
                return
            await self.post_json(
                "v3/keys/upload",
                json_body={
                    "device_keys": {
                        "user_id": whoami["user_id"],
                        "device_id": device_id,
                        "algorithms": [],
                        "keys": {},
                        "signatures": {},
                    }
                },
            )
            log.info("registered bot device %s for message sending (MAS compatibility)", device_id)
        except Exception:
            log.exception("could not register bot device; welcome DM sends may fail under MAS")

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

    async def resolve_room_alias(self, alias: str) -> str | None:
        """Resolve a room alias (``#name:server``) to its ``room_id``, or ``None`` if unknown."""
        # https://spec.matrix.org/latest/client-server-api/#get_matrixclientv3directoryroomroomalias
        # The alias (e.g. "#room:server") must be percent-encoded — notably "#" and ":".
        try:
            result = await self.get_json(f"v3/directory/room/{quote(alias, safe='')}")
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        room_id: str = result["room_id"]
        return room_id

    async def link_room_to_space(self, space_id: str, room_id: str, *, suggested: bool = False) -> None:
        """Add ``room_id`` to ``space_id`` (m.space.child on the space + m.space.parent on the room)."""
        # https://spec.matrix.org/latest/client-server-api/#mspacechild
        await self.put_room_state_event(
            space_id,
            "m.space.child",
            {"suggested": suggested, "via": [self.server_name]},
            state_key=room_id,
        )
        await self.put_room_state_event(
            room_id,
            "m.space.parent",
            {"canonical": True, "via": [self.server_name]},
            state_key=space_id,
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

    async def set_room_avatar(self, room_id: str, mxc_uri: str) -> None:
        # https://spec.matrix.org/latest/client-server-api/#mroomavatar
        await self.put_room_state_event(room_id, "m.room.avatar", {"url": mxc_uri})

    async def set_user_avatar(self, user_id: str, mxc_uri: str) -> None:
        # https://spec.matrix.org/latest/client-server-api/#put_matrixclientv3profileuseridavatar_url
        await self.put_json(f"v3/profile/{user_id}/avatar_url", json_body={"avatar_url": mxc_uri})

    # --- media (authenticated, MSC3916) --------------------------------------

    async def upload_media(self, content: bytes, *, content_type: str, filename: str | None = None) -> str:
        """Upload bytes to the media repo, returning the ``mxc://`` URI (G10.1).

        Uploads are authenticated by the bot's token; the resulting ``mxc`` is later fetched via the
        authenticated download endpoint below.
        https://spec.matrix.org/latest/client-server-api/#post_matrixmediav3upload
        """
        params = {"filename": filename} if filename else None
        result = await self.request_raw(
            "POST",
            f"{self.server_url}/_matrix/media/v3/upload",
            params=params,
            content=content,
            headers={"Content-Type": content_type},
        )
        content_uri: str = result["content_uri"]
        return content_uri

    async def download_media(self, mxc_uri: str) -> bytes:
        """Download media by ``mxc://`` URI over the **authenticated** endpoint (MSC3916).

        https://spec.matrix.org/latest/client-server-api/#get_matrixclientv1mediadownloadservernamemediaid
        """
        server, media_id = _parse_mxc(mxc_uri)
        data: bytes = await self.request_raw(
            "GET", f"v1/media/download/{server}/{media_id}", parse_json=False
        )
        return data

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

        Raises :class:`SyncNotSupportedError` if version negotiation ran and the server does not
        advertise Simplified Sliding Sync, so the listener can fall back to the signal-only path.
        """
        if self._versions is not None and not self._versions.supports_simplified_sliding_sync():
            raise SyncNotSupportedError(
                "homeserver does not advertise org.matrix.simplified_msc3575 (MSC4186)"
            )
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


def _parse_mxc(mxc_uri: str) -> tuple[str, str]:
    """Split ``mxc://server/media_id`` into ``(server, media_id)``."""
    if not mxc_uri.startswith("mxc://"):
        raise ValueError(f"not an mxc URI: {mxc_uri!r}")
    server, _, media_id = mxc_uri.removeprefix("mxc://").partition("/")
    if not server or not media_id:
        raise ValueError(f"malformed mxc URI: {mxc_uri!r}")
    return server, media_id


class CSApiEffectors:
    """Concrete :class:`~onbot.reconciler.effectors.MatrixEffectors` backed by the CS API.

    Replaces ``DryRunEffectors`` when the bot runs for real (Phase 3 deferral resolved).
    """

    def __init__(self, client: ApiClientMatrix, *, media: MediaUploader | None = None) -> None:
        self.client = client
        self._owns_media = media is None
        if media is None:
            # Local import avoids a module-level cycle (onbot.media imports ApiClientMatrix).
            from onbot.media import MediaUploader

            media = MediaUploader(client)
        self._media = media

    async def aclose(self) -> None:
        if self._owns_media:
            await self._media.aclose()

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

    async def set_room_avatar(self, room_id: str, mxc_uri: str) -> None:
        await self.client.set_room_avatar(room_id, mxc_uri)

    async def upload_avatar(self, url: str) -> str:
        return await self._media.upload_from_url(url)

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        return await self.client.get_room_state_event(room_id, event_type, state_key)

    async def put_room_state(self, room_id: str, event_type: str, content: dict[str, Any]) -> None:
        await self.client.put_room_state_event(room_id, event_type, content)
