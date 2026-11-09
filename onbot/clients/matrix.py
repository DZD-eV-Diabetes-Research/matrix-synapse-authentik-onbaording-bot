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

import httpx

if TYPE_CHECKING:
    from onbot.media import MediaUploader

from onbot.auth.token_provider import TokenProvider
from onbot.clients.base import (
    ApiError,
    BaseApiClient,
    may_have_been_processed,
    path_segment,
    resend_unless_done,
)
from onbot.clients.versions import CLIENT_VERSIONS_PATH, ServerVersions
from onbot.logging import get_logger
from onbot.rooms.provision import ManagedRoomSpec, create_managed_room

log = get_logger(__name__)

# MSC4186 Simplified Sliding Sync. Unstable path — gated by version negotiation (Phase 6).
SLIDING_SYNC_PATH = "unstable/org.matrix.simplified_msc3575/sync"

ENCRYPTION_ALGORITHM = "m.megolm.v1.aes-sha2"

# Extra client-side wait on top of the server-side long-poll timeout of a sliding sync request.
SYNC_TIMEOUT_MARGIN_SEC = 15.0

# Key in ``m.room.create`` content that carries the bot's per-call creation nonce (RUN-05).
CREATION_NONCE_KEY = "onbot.creation_nonce"


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
        room_version: str | None = None,
        token_provider: TokenProvider | None = None,
        **kwargs: Any,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        base = f"{self.server_url}/_matrix/client"
        # Ordinary calls keep the base client's 30 s timeout; only the sync long-poll gets a longer
        # one, per request (RUN-10).
        super().__init__(base_url=base, auth_token=access_token, token_provider=token_provider, **kwargs)
        self.server_name = server_name
        # Server-wide room version for createRoom; None leaves rooms to inherit the server default
        # (spec now says v12). Applied uniformly to every room/space the bot creates. See
        # SynapseServer.room_version.
        self.room_version = room_version
        self._versions: ServerVersions | None = None

    # --- version negotiation (Phase 6) --------------------------------------

    async def negotiate_versions(self) -> ServerVersions:
        """Fetch + cache ``GET /_matrix/client/versions`` (the bot's capability source of truth)."""
        payload = await self.get_json(CLIENT_VERSIONS_PATH)
        self._versions = ServerVersions.from_payload(payload)
        log.info("negotiated CS-API: sliding_sync=%s", self._versions.supports_simplified_sliding_sync())
        return self._versions

    # --- device registration (MAS compatibility) ----------------------------

    async def get_auth_issuer(self) -> str | None:
        """The OAuth 2.0 issuer the homeserver delegates authentication to (MAS), or ``None``.

        Reads ``GET /_matrix/client/v1/auth_metadata`` and falls back to the unstable MSC2965
        ``auth_issuer`` endpoint of older servers. A 404 (or ``M_UNRECOGNIZED``) on both means the
        homeserver handles logins itself.
        """
        for path in ("v1/auth_metadata", "unstable/org.matrix.msc2965/auth_issuer"):
            try:
                result = await self.get_json(path)
            except ApiError as exc:
                if exc.status_code in (400, 404):
                    continue
                raise
            if isinstance(result, dict) and isinstance(result.get("issuer"), str):
                return str(result["issuer"])
        return None

    async def ensure_device_registered(self) -> None:
        """Register the bot's device so it can send messages under MAS.

        A MAS-issued token (a ``mas-cli`` compatibility token or a client login) carries a device
        id, but Synapse only persists a ``devices`` row once the client uploads device keys. Until
        then, *sending a message* — which records the transaction id per device — fails with a
        foreign-key 500 (``event_txn_id_device_id``). State-only writes (room create, power levels,
        account data) are unaffected, which is why this only bites the welcome DM flow.

        The bot advertises **no** encryption algorithms (ADR-0009: it operates outside encrypted
        rooms), so this is purely the device-bookkeeping registration, not an e2ee opt-in. Verified
        against the Phase 7b live stack; idempotent. Raises on failure: the caller retries until it
        succeeds, because a failure during a cold start would otherwise break every welcome send for
        the lifetime of the process (RUN-02, see ``onbot.app``).
        """
        whoami = await self.get_json("v3/account/whoami")
        device_id = whoami.get("device_id")
        if not device_id:
            return
        # Uploading the same (empty) device keys twice is harmless, so a retry after an ambiguous
        # failure is safe.
        await self.post_json(
            "v3/keys/upload",
            idempotent=True,
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
        suggested: bool = False,
        is_direct: bool = False,
        invite: list[str] | None = None,
    ) -> str:
        """Create a room (https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3createroom).

        Returns the new ``room_id``. ``room_params`` (preset/visibility/federation/…) are merged in
        verbatim; encryption and the space-parent link are added as ``initial_state`` events. Any
        ``initial_state`` a caller passes under ``room_params`` is preserved and appended *after* the
        bot's own events, so an operator-supplied state event (e.g. a lobby's join rule) survives and
        wins over a same-typed default rather than being silently dropped.

        The bot is the *creator* of every room it makes. Under room version 12 (the spec default) the
        creator holds an infinite, immutable power level and is deliberately **absent** from
        ``m.room.power_levels`` — naming it in a ``power_level_content_override``'s ``users`` map is
        rejected by the auth rules — so callers must not put the bot there (see
        :mod:`onbot.onboarding.notice_board`, :mod:`onbot.rooms.admin`). The returned ``room_id`` is a
        hash with no ``:domain`` component under v12; treat it as an opaque token, never split it.
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
        # Merge, don't clobber: keep whatever initial_state the caller passed and append it after the
        # bot's own events (space parent, encryption), so the caller's events win on a type/state_key
        # collision. Assigning body["initial_state"] outright would drop them.
        caller_initial_state = list(body.pop("initial_state", None) or [])
        if self.room_version is not None and "room_version" not in body:
            body["room_version"] = self.room_version
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
        combined_initial_state = [*initial_state, *caller_initial_state]
        if combined_initial_state:
            body["initial_state"] = combined_initial_state

        room_id = await self._post_create_room(body)
        if parent_space_id:
            # https://spec.matrix.org/latest/client-server-api/#mspacechild
            # `suggested` nudges clients to surface the room to space members: true for a lobby (a room
            # a wandering space member should join), false for a private group room (they cannot).
            await self.put_room_state_event(
                parent_space_id,
                "m.space.child",
                {"suggested": suggested, "via": [self.server_name]},
                state_key=room_id,
            )
        return room_id

    async def _post_create_room(self, body: dict[str, Any]) -> str:
        """``POST createRoom`` without ever creating the room twice (RUN-05).

        createRoom is not idempotent, so the base client does not resend it after a failure that may
        come after the server created the room (a read timeout, a dropped connection, a 502 or 504
        from the reverse proxy). Instead, each call tags the room with a random nonce in its
        ``m.room.create`` content, and after such a failure this looks for a room carrying that
        nonce: by its alias when it has one, else among the rooms the bot joined since just before
        the call (a one-off read, only for rooms without an alias, which are the onboarding rooms).
        A room found that way is returned as if the call had succeeded; otherwise the original error
        is raised, and the caller's next attempt creates the room with a new nonce.

        Remaining gap: a server still working on the request when the check runs finishes the room
        after it; that room is then left behind unused.
        """
        nonce = uuid.uuid4().hex
        creation_content = {**(body.get("creation_content") or {}), CREATION_NONCE_KEY: nonce}
        body = {**body, "creation_content": creation_content}
        alias_localpart: str | None = body.get("room_alias_name")
        joined_before: set[str] | None = None
        if not alias_localpart:
            joined_before = set(await self.joined_rooms())
        try:
            result = await self.post_json("v3/createRoom", json_body=body)
        except (ApiError, httpx.TransportError) as exc:
            if not may_have_been_processed(exc):
                raise
            room_id = await self._find_created_room(nonce, alias_localpart, joined_before)
            if room_id is None:
                raise
            log.warning(
                "createRoom failed (%s), but the server had created the room; continuing with %s",
                exc,
                room_id,
            )
            return room_id
        created: str = result["room_id"]
        return created

    async def _find_created_room(
        self, nonce: str, alias_localpart: str | None, joined_before: set[str] | None
    ) -> str | None:
        """The room created with ``nonce``, if the server created it after all."""
        try:
            if alias_localpart:
                found = await self.resolve_room_alias(f"#{alias_localpart}:{self.server_name}")
                candidates = [found] if found else []
            else:
                before = joined_before or set()
                candidates = [r for r in await self.joined_rooms() if r not in before]
            for room_id in candidates:
                create = await self.get_room_state_event(room_id, "m.room.create") or {}
                if create.get(CREATION_NONCE_KEY) == nonce:
                    return room_id
        except ApiError, httpx.TransportError:
            log.warning("could not check whether a failed createRoom created the room", exc_info=True)
        return None

    async def joined_rooms(self) -> list[str]:
        """The rooms the bot is joined to.

        https://spec.matrix.org/latest/client-server-api/#get_matrixclientv3joined_rooms
        """
        result = await self.get_json("v3/joined_rooms")
        rooms: list[str] = list((result or {}).get("joined_rooms") or [])
        return rooms

    async def resolve_room_alias(self, alias: str) -> str | None:
        """Resolve a room alias (``#name:server``) to its ``room_id``, or ``None`` if unknown."""
        # https://spec.matrix.org/latest/client-server-api/#get_matrixclientv3directoryroomroomalias
        # The alias (e.g. "#room:server") must be percent-encoded — notably "#" and ":".
        try:
            result = await self.get_json(f"v3/directory/room/{path_segment(alias)}")
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
        path = f"v3/rooms/{path_segment(room_id)}/kick"
        try:
            await self.post_json(path, json_body=body)
        except (ApiError, httpx.TransportError) as exc:
            # Kicking twice fails with 403 "not in the room", so a resend is only safe once a look at
            # the membership shows the first kick did not land (RUN-05).
            async def gone() -> bool:
                return await self.get_membership(room_id, user_id) not in ("join", "invite", "knock")

            await resend_unless_done(
                exc,
                done=gone,
                resend=lambda: self.post_json(path, json_body=body),
                what=f"kicking {user_id} from {room_id}",
            )

    async def invite_user(self, room_id: str, user_id: str) -> None:
        # https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3roomsroomidinvite
        # Re-inviting an invited user succeeds (checked against Synapse), so a retry is safe.
        await self.post_json(
            f"v3/rooms/{path_segment(room_id)}/invite", json_body={"user_id": user_id}, idempotent=True
        )

    async def get_membership(self, room_id: str, user_id: str) -> str | None:
        """A user's membership in a room (``join``/``invite``/``leave``/…), or ``None`` if never set."""
        content = await self.get_room_state_event(room_id, "m.room.member", user_id)
        if content is None:
            return None
        membership: str | None = content.get("membership")
        return membership

    # --- room state ----------------------------------------------------------

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        """Fetch a single state event's content, or ``None`` if it is not set (HTTP 404).

        Targets the event directly (no full-state linear scan — the legacy §3 inefficiency).
        """
        path = f"v3/rooms/{path_segment(room_id)}/state/{path_segment(event_type)}"
        if state_key:
            path = f"{path}/{path_segment(state_key)}"
        try:
            content: dict[str, Any] = await self.get_json(path)
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return content

    async def get_room_state_event_with_sender(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        """The whole current state event (``sender``, ``event_id``, ``content``), or ``None`` (404).

        The spec'd endpoint returns only the content; Synapse returns the event with ``format=event``.
        The bot needs the sender to tell its own writes from a human's (its onbot markers, a lobby's
        join rule, D16).
        """
        path = f"v3/rooms/{path_segment(room_id)}/state/{path_segment(event_type)}"
        if state_key:
            path = f"{path}/{path_segment(state_key)}"
        try:
            event: dict[str, Any] = await self.get_json(path, params={"format": "event"})
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return event

    async def set_room_alias(self, alias: str, room_id: str) -> None:
        """Point the local alias ``alias`` (``#name:server``) at ``room_id`` in the room directory.

        https://spec.matrix.org/latest/client-server-api/#put_matrixclientv3directoryroomroomalias
        """
        await self.put_json(f"v3/directory/room/{path_segment(alias)}", json_body={"room_id": room_id})

    async def put_room_state_event(
        self, room_id: str, event_type: str, content: Mapping[str, Any], state_key: str = ""
    ) -> None:
        path = f"v3/rooms/{path_segment(room_id)}/state/{path_segment(event_type)}"
        if state_key:
            path = f"{path}/{path_segment(state_key)}"
        await self.put_json(path, json_body=dict(content))

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        return await self.get_room_state_event(room_id, "m.room.power_levels") or {}

    async def room_is_encrypted(self, room_id: str) -> bool:
        """Whether ``room_id`` has ``m.room.encryption`` (which can never be removed again)."""
        return await self.get_room_state_event(room_id, "m.room.encryption") is not None

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
        await self.put_json(
            f"v3/profile/{path_segment(user_id)}/avatar_url", json_body={"avatar_url": mxc_uri}
        )

    async def get_user_avatar(self, user_id: str) -> str | None:
        """The user's current profile avatar as an ``mxc://`` URI, or ``None`` if unset.

        Synapse answers a profile with no avatar as either 404 or 200 with the key missing.
        https://spec.matrix.org/latest/client-server-api/#get_matrixclientv3profileuseridavatar_url
        """
        try:
            content: dict[str, Any] = await self.get_json(f"v3/profile/{path_segment(user_id)}/avatar_url")
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        avatar_url: str | None = content.get("avatar_url")
        return avatar_url or None

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
            # A second upload after an ambiguous failure only leaves an unused copy of the file in
            # the media store; retrying is worth that.
            idempotent=True,
        )
        content_uri: str = result["content_uri"]
        return content_uri

    async def download_media(self, mxc_uri: str) -> bytes:
        """Download media by ``mxc://`` URI over the **authenticated** endpoint (MSC3916).

        https://spec.matrix.org/latest/client-server-api/#get_matrixclientv1mediadownloadservernamemediaid
        """
        server, media_id = _parse_mxc(mxc_uri)
        data: bytes = await self.request_raw(
            "GET", f"v1/media/download/{path_segment(server)}/{path_segment(media_id)}", parse_json=False
        )
        return data

    # --- messaging -----------------------------------------------------------

    async def send_text_message(
        self,
        room_id: str,
        body: str,
        *,
        msgtype: str = "m.text",
        formatted_body: str | None = None,
        txn_id: str | None = None,
        retry_rate_limited: bool = True,
    ) -> str:
        """Send a textual message; returns the event id.

        ``txn_id`` defaults to a new random id per call, which the HTTP retries of this one call
        reuse. A caller that may repeat the same send in a later call (the welcome flow, ONB-09)
        passes a deterministic one: Synapse answers a repeated transaction id of the same device with
        the event it already created instead of posting the message again.
        https://spec.matrix.org/latest/client-server-api/#transaction-identifiers

        ``msgtype`` is ``m.text`` for a plain message and ``m.notice`` for one the client should not
        auto-reply to or notify on as loudly — the convention for bot-originated messages, used by
        the broadcast fan-out.
        https://spec.matrix.org/latest/client-server-api/#mnotice

        ``formatted_body`` is the rendered HTML of ``body`` (see :mod:`onbot.formatting`). Clients
        display it in place of ``body``, which stays as the fallback for those that do not render
        HTML — so it must say the same thing, not more.
        https://spec.matrix.org/latest/client-server-api/#mroommessage-msgtypes

        ``retry_rate_limited=False`` raises a 429 at once instead of waiting it out: the broadcast
        paces its fan-out by the server's answers itself (ONB-03).
        """
        txn = txn_id or uuid.uuid4().hex
        content: dict[str, Any] = {"msgtype": msgtype, "body": body}
        if formatted_body is not None:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = formatted_body
        result = await self.request_json(
            "PUT",
            f"v3/rooms/{path_segment(room_id)}/send/m.room.message/{path_segment(txn)}",
            json_body=content,
            retry_rate_limited=retry_rate_limited,
        )
        event_id: str = result["event_id"]
        return event_id

    # --- account data --------------------------------------------------------

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        try:
            data: dict[str, Any] = await self.get_json(
                f"v3/user/{path_segment(user_id)}/account_data/{path_segment(data_type)}"
            )
        except ApiError as exc:
            if exc.status_code == 404:
                return {}
            raise
        return data

    async def set_account_data(self, user_id: str, data_type: str, content: Mapping[str, Any]) -> None:
        await self.put_json(
            f"v3/user/{path_segment(user_id)}/account_data/{path_segment(data_type)}", json_body=dict(content)
        )

    async def get_room_account_data(self, user_id: str, room_id: str, data_type: str) -> dict[str, Any]:
        """The user's private per-room account data of ``data_type``, ``{}`` when unset.

        Only the user can read or write it; no room member can, whatever their power level. That makes
        it the place for bookkeeping that must not be editable from inside the room.
        https://spec.matrix.org/latest/client-server-api/#get_matrixclientv3useruseridroomsroomidaccount_datatype
        """
        try:
            data: dict[str, Any] = await self.get_json(
                f"v3/user/{path_segment(user_id)}/rooms/{path_segment(room_id)}/account_data/{path_segment(data_type)}"
            )
        except ApiError as exc:
            if exc.status_code == 404:
                return {}
            raise
        return data

    async def set_room_account_data(
        self, user_id: str, room_id: str, data_type: str, content: Mapping[str, Any]
    ) -> None:
        await self.put_json(
            f"v3/user/{path_segment(user_id)}/rooms/{path_segment(room_id)}/account_data/{path_segment(data_type)}",
            json_body=dict(content),
        )

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
        # A read, safe to repeat with the same ``pos``. The server holds the request for up to
        # ``timeout_ms``, so the client waits that long plus a margin, and only here (RUN-10).
        data = await self.request_json(
            "POST",
            SLIDING_SYNC_PATH,
            params=params,
            json_body=body,
            timeout=timeout_ms / 1000 + SYNC_TIMEOUT_MARGIN_SEC,
            idempotent=True,
        )
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

    def __init__(
        self, client: ApiClientMatrix, *, media: MediaUploader | None = None, bot_id: str | None = None
    ) -> None:
        self.client = client
        # The bot's MXID, needed to lock the marker of the rooms it creates (onbot.rooms.provision).
        self._bot_id = bot_id
        self._owns_media = media is None
        if media is None:
            # Local import avoids a module-level cycle (onbot.media imports ApiClientMatrix).
            from onbot.media import MediaUploader

            media = MediaUploader(client)
        self._media = media

    async def aclose(self) -> None:
        if self._owns_media:
            await self._media.aclose()

    async def create_managed_room(self, spec: ManagedRoomSpec) -> str:
        if self._bot_id is None:
            raise RuntimeError("CSApiEffectors needs bot_id to create managed rooms")
        return await create_managed_room(self.client, spec, bot_id=self._bot_id)

    async def resolve_room_alias(self, alias: str) -> str | None:
        return await self.client.resolve_room_alias(alias)

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

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        return await self.client.get_room_state_event_with_sender(room_id, event_type, state_key)

    async def put_room_state(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None:
        await self.client.put_room_state_event(room_id, event_type, content, state_key)

    async def set_room_alias(self, alias: str, room_id: str) -> None:
        await self.client.set_room_alias(alias, room_id)

    async def get_room_account_data(
        self, user_id: str, room_id: str, data_type: str
    ) -> dict[str, Any] | None:
        return await self.client.get_room_account_data(user_id, room_id, data_type) or None

    async def put_room_account_data(
        self, user_id: str, room_id: str, data_type: str, content: dict[str, Any]
    ) -> None:
        await self.client.set_room_account_data(user_id, room_id, data_type, content)
