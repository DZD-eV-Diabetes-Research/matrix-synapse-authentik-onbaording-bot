"""Matrix-side effectors: the write seam between the reconciler and the Matrix CS API.

The reconciler decides *what* should change (pure logic in the sibling modules); these effectors
perform the Matrix Client-Server operations that the Synapse admin API cannot do (room/space
creation, kicks, power levels, room name/topic, custom state events). The production implementation
is :class:`CSApiEffectors`, over the CS API client.

``onbot reconcile-once --dry-run`` (review finding MNT-09) swaps in the two recording wrappers below:
:class:`DryRunEffectors` with the live effectors as its ``reads``, and :class:`DryRunSynapseAdmin`
around the Synapse admin client, whose joins, blocks and deletions would otherwise bypass this seam.
Reads go to the live servers, so the pass computes the real plan; every write is logged with a
``[dry-run]`` prefix and none is sent. Rooms the pass would create get a synthetic id, and reads of
those answer as an empty room would.

Without ``reads``, :class:`DryRunEffectors` answers every read as if nothing existed, which is what the
unit tests build on.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Protocol, runtime_checkable

from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import RoomBlock
from onbot.logging import get_logger
from onbot.media import MediaUploader, SourceImage, UploadedImage
from onbot.rooms.provision import ManagedRoomSpec, create_managed_room

log = get_logger(__name__)


@runtime_checkable
class MatrixEffectors(Protocol):
    async def create_managed_room(self, spec: ManagedRoomSpec) -> str:
        """Create a group room, lobby or space with its onbot marker (see :mod:`onbot.rooms.provision`)."""
        ...

    async def resolve_room_alias(self, alias: str) -> str | None: ...

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None: ...

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]: ...

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None: ...

    async def set_room_name(self, room_id: str, name: str) -> None: ...

    async def set_room_topic(self, room_id: str, topic: str) -> None: ...

    async def set_room_avatar(self, room_id: str, mxc_uri: str) -> None: ...

    async def avatar_image(self, url: str) -> SourceImage:
        """The image at ``url`` with its SHA-256, fetched again about once a day (ONB-15).

        Carries the bytes when it was just fetched, so :meth:`upload_avatar` needs no second download.
        """
        ...

    async def upload_avatar(self, image: SourceImage) -> UploadedImage:
        """The ``mxc://`` URI holding ``image`` (uploaded once per content), and the digest it holds.

        The digest differs from ``image``'s only when the picture changed before its bytes, which
        were not at hand, could be fetched again (W6R-02).
        """
        ...

    async def room_wears(self, room_id: str, digest: str) -> bool:
        """Whether the room's current ``m.room.avatar`` holds the image with SHA-256 ``digest``."""
        ...

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None: ...

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        """The whole state event, with its ``sender``, or ``None`` when it is not set."""
        ...

    async def put_room_state(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None: ...

    async def set_room_alias(self, alias: str, room_id: str) -> None:
        """Point the local ``alias`` at ``room_id`` in the room directory."""
        ...

    async def get_room_account_data(
        self, user_id: str, room_id: str, data_type: str
    ) -> dict[str, Any] | None:
        """``user_id``'s private account data for one room, or ``None`` when unset."""
        ...

    async def put_room_account_data(
        self, user_id: str, room_id: str, data_type: str, content: dict[str, Any]
    ) -> None: ...


SYNTHETIC_ROOM_PREFIX = "!dryrun-"


def is_synthetic_room(room_id: str) -> bool:
    """A room only a dry run believes in: it would have been created by this pass."""
    return room_id.startswith(SYNTHETIC_ROOM_PREFIX)


class DryRunEffectors:
    """Logs every intended write and mutates nothing; reads go to ``reads`` when given."""

    _reads: MatrixEffectors | None = None

    def __init__(self, reads: MatrixEffectors | None = None) -> None:
        self._reads = reads

    def _live(self, room_id: str) -> MatrixEffectors | None:
        return None if self._reads is None or is_synthetic_room(room_id) else self._reads

    def _synthetic_room_id(self, server_name: str = "dry-run") -> str:
        return f"{SYNTHETIC_ROOM_PREFIX}{uuid.uuid4().hex[:12]}:{server_name}"

    async def create_managed_room(self, spec: ManagedRoomSpec) -> str:
        log.info(
            "[dry-run] would create %s alias=%s name=%r in space=%s",
            spec.room_type.value.replace("_", " "),
            spec.alias_localpart,
            spec.name,
            spec.parent_space_id,
        )
        return self._synthetic_room_id()

    async def resolve_room_alias(self, alias: str) -> str | None:
        return None if self._reads is None else await self._reads.resolve_room_alias(alias)

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
        log.info("[dry-run] would kick %s from %s (%s)", user_id, room_id, reason)

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        live = self._live(room_id)
        return {} if live is None else await live.get_room_power_levels(room_id)

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        log.info("[dry-run] would set power levels in %s -> %s", room_id, power_levels)

    async def set_room_name(self, room_id: str, name: str) -> None:
        log.info("[dry-run] would set name of %s -> %r", room_id, name)

    async def set_room_topic(self, room_id: str, topic: str) -> None:
        log.info("[dry-run] would set topic of %s -> %r", room_id, topic)

    async def set_room_avatar(self, room_id: str, mxc_uri: str) -> None:
        log.info("[dry-run] would set avatar of %s -> %s", room_id, mxc_uri)

    async def avatar_image(self, url: str) -> SourceImage:
        # Fetching only reads, so a preview with live reads compares the real content. Without
        # them (the unit tests) the URL stands in for the image.
        if self._reads is not None:
            return await self._reads.avatar_image(url)
        return SourceImage(url, None, "image/png", hashlib.sha256(url.encode()).hexdigest())

    async def upload_avatar(self, image: SourceImage) -> UploadedImage:
        log.info("[dry-run] would upload avatar from %s", image.url)
        return UploadedImage(f"mxc://dry-run/{uuid.uuid4().hex[:12]}", image.sha256)

    async def room_wears(self, room_id: str, digest: str) -> bool:
        live = self._live(room_id)
        return False if live is None else await live.room_wears(room_id, digest)

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        live = self._live(room_id)
        return None if live is None else await live.get_room_state(room_id, event_type, state_key)

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        live = self._live(room_id)
        return None if live is None else await live.get_room_state_event(room_id, event_type, state_key)

    async def put_room_state(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None:
        target = f"{event_type}/{state_key}" if state_key else event_type
        log.info("[dry-run] would set state %s on %s", target, room_id)

    async def set_room_alias(self, alias: str, room_id: str) -> None:
        log.info("[dry-run] would point alias %s at %s", alias, room_id)

    async def get_room_account_data(
        self, user_id: str, room_id: str, data_type: str
    ) -> dict[str, Any] | None:
        live = self._live(room_id)
        return None if live is None else await live.get_room_account_data(user_id, room_id, data_type)

    async def put_room_account_data(
        self, user_id: str, room_id: str, data_type: str, content: dict[str, Any]
    ) -> None:
        log.info("[dry-run] would set account data %s for %s in %s", data_type, user_id, room_id)


class DryRunSynapseAdmin:
    """The Synapse admin client for a dry run: reads go through, writes are only logged."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        # Everything not overridden below is a read (listing users, rooms, spaces, media).
        return getattr(self._inner, name)

    async def list_room_members(self, room_id: str) -> list[str]:
        if is_synthetic_room(room_id):
            return []
        members: list[str] = await self._inner.list_room_members(room_id)
        return members

    async def get_room_details(self, room_id: str) -> dict[str, Any]:
        if is_synthetic_room(room_id):
            return {}
        details: dict[str, Any] = await self._inner.get_room_details(room_id)
        return details

    async def room_is_blocked(self, room_id: str) -> bool:
        if is_synthetic_room(room_id):
            return False
        return bool(await self._inner.room_is_blocked(room_id))

    async def get_room_block(self, room_id: str) -> RoomBlock:
        if is_synthetic_room(room_id):
            return RoomBlock(blocked=False)
        block: RoomBlock = await self._inner.get_room_block(room_id)
        return block

    async def add_user_to_room(self, room_id: str, user_id: str) -> None:
        log.info("[dry-run] would join %s to %s", user_id, room_id)

    async def room_set_blocked(self, room_id: str, *, blocked: bool) -> None:
        log.info("[dry-run] would %s room %s", "block" if blocked else "unblock", room_id)

    async def delete_room(self, room_id: str, **kwargs: Any) -> dict[str, Any]:
        log.info("[dry-run] would delete room %s (%s)", room_id, kwargs)
        return {}


class CSApiEffectors:
    """Concrete :class:`MatrixEffectors` backed by the CS API.

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

    async def avatar_image(self, url: str) -> SourceImage:
        return await self._media.image(url)

    async def upload_avatar(self, image: SourceImage) -> UploadedImage:
        return await self._media.upload(image)

    async def room_wears(self, room_id: str, digest: str) -> bool:
        content = await self.client.get_room_state_event(room_id, "m.room.avatar") or {}
        mxc = content.get("url")
        return isinstance(mxc, str) and bool(mxc) and await self._media.holds(mxc, digest)

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
