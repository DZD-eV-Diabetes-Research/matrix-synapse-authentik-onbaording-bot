"""The bot's own profile picture, set once and then left alone (G6.8).

Setting an avatar is not idempotent on the wire: every upload of the same image yields a *new*
``mxc://`` URI, and writing a new URI into the profile makes Synapse fan a ``m.room.member`` event
into every room the bot shares with anyone, which clients render as "the bot changed their profile
picture". Restarting the bot must not do that.

So the bot records what it set: an account-data blob (``<reversed server>.onbot.bot_avatar``, the
same no-database bookkeeping as the room-state events in :mod:`onbot.reconciler.state`) holding the
configured source URL and the mxc it produced. On start we only touch the profile if that record
does not match both the configured URL and the avatar the profile actually carries.

When the record is missing (first run after an upgrade, or an operator reset it) we fall back to
comparing bytes: download whatever the profile points at and compare it with the source image. Equal
bytes means the picture is already right, so we adopt it and write the record instead of re-setting
the profile. That keeps this fix quiet on the very first restart too.
"""

from __future__ import annotations

from typing import Any, Protocol

from onbot.logging import get_logger
from onbot.media import MediaUploader
from onbot.reconciler.state import SCHEMA_VERSION, event_type_name

log = get_logger(__name__)


def bot_avatar_account_data_type(server_name: str) -> str:
    """Account-data type holding the avatar record, e.g. ``org.company.onbot.bot_avatar``."""
    return event_type_name(server_name, "bot_avatar")


class AvatarClient(Protocol):
    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]: ...

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None: ...

    async def get_user_avatar(self, user_id: str) -> str | None: ...

    async def set_user_avatar(self, user_id: str, mxc_uri: str) -> None: ...

    async def download_media(self, mxc_uri: str) -> bytes: ...


async def ensure_bot_avatar(
    client: AvatarClient,
    media: MediaUploader,
    *,
    user_id: str,
    server_name: str,
    source_url: str | None,
) -> None:
    """Make the bot's profile picture match ``source_url``, writing nothing if it already does.

    Raises on failure (a failed fetch, upload or profile write); the caller retries until it succeeds
    (``onbot.app``), so an avatar that could not be set during a cold start is set once the homeserver
    is up rather than on the next restart.
    """
    if not source_url:
        return
    data_type = bot_avatar_account_data_type(server_name)
    record = await client.get_account_data(user_id, data_type)
    current = await client.get_user_avatar(user_id)
    if current and record.get("source_url") == source_url and record.get("mxc") == current:
        # Unchanged: reuse the mxc for every room that wears the same picture, and stay silent.
        media.remember(source_url, current)
        log.debug("bot avatar already set from %s (%s)", source_url, current)
        return
    content, content_type = await media.fetch(source_url)
    if current and await _holds_same_bytes(client, current, content):
        media.remember(source_url, current)
        await _write_record(client, user_id, data_type, source_url, current)
        log.info("bot avatar already matches %s; recorded %s without re-setting it", source_url, current)
        return
    mxc = await media.upload(source_url, content, content_type)
    await client.set_user_avatar(user_id, mxc)
    await _write_record(client, user_id, data_type, source_url, mxc)
    log.info("set bot avatar from %s", source_url)


async def _holds_same_bytes(client: AvatarClient, mxc: str, content: bytes) -> bool:
    """Whether the media at ``mxc`` is byte-identical to ``content`` (``False`` if it is unreadable)."""
    try:
        return await client.download_media(mxc) == content
    except Exception:
        log.warning("could not read the current bot avatar %s; treating it as different", mxc, exc_info=True)
        return False


async def _write_record(
    client: AvatarClient, user_id: str, data_type: str, source_url: str, mxc: str
) -> None:
    await client.set_account_data(
        user_id,
        data_type,
        {"schema_version": SCHEMA_VERSION, "source_url": source_url, "mxc": mxc},
    )
