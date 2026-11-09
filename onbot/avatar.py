"""The bot's own profile picture, set once and then left alone (G6.8).

Setting an avatar is not idempotent on the wire: every upload of the same image yields a *new*
``mxc://`` URI, and writing a new URI into the profile makes Synapse fan a ``m.room.member`` event
into every room the bot shares with anyone, which clients render as "the bot changed their profile
picture". Restarting the bot must not do that.

So the bot records what it set: an account-data blob (``<reversed server>.onbot.bot_avatar``, the
same no-database bookkeeping as the room-state events in :mod:`onbot.reconciler.state`) holding the
configured source URL, the SHA-256 of the image and the mxc it produced. On start the image is
fetched once and the profile is touched only when the record does not match the configured URL, the
image's content and the avatar the profile actually carries. Keying on the content as well as the URL
means a picture replaced behind an unchanged URL reaches the profile on the next start (ONB-15).

When the record has no digest or is missing (first run after an upgrade, or an operator reset it) we
fall back to comparing bytes: download whatever the profile points at and compare it with the source
image. Equal bytes means the picture is already right, so we adopt it and write the record instead of
re-setting the profile. That keeps the upgrade quiet too.

When the source cannot be fetched but the record says the profile already wears a picture from that
URL, the profile is left as it is and the start goes on: an avatar host that is down must not keep a
startup step failing.
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
    is up rather than on the next restart. A failed fetch while the profile already wears a picture
    from ``source_url`` is not a failure: the picture stays.
    """
    if not source_url:
        return
    data_type = bot_avatar_account_data_type(server_name)
    record = await client.get_account_data(user_id, data_type)
    current = await client.get_user_avatar(user_id)
    recorded = bool(current) and record.get("source_url") == source_url and record.get("mxc") == current
    try:
        image = await media.image(source_url)
    except Exception:
        if not recorded:
            raise
        assert current is not None
        log.warning(
            "could not fetch %s to check the bot avatar for a new picture; keeping the current one",
            source_url,
        )
        digest = record.get("sha256")
        if isinstance(digest, str):
            media.remember(digest, current)
        return
    if recorded and record.get("sha256") == image.sha256:
        # Unchanged: reuse the mxc for every room that wears the same picture, and stay silent.
        assert current is not None
        media.remember(image.sha256, current)
        log.debug("bot avatar already set from %s (%s)", source_url, current)
        return
    if current and await media.holds(current, image.sha256):
        # The bot's own profile picture: only the bot sets it, so its media may be reused.
        media.remember(image.sha256, current)
        await _write_record(client, user_id, data_type, source_url, image.sha256, current)
        log.info("bot avatar already matches %s; recorded %s without re-setting it", source_url, current)
        return
    uploaded = await media.upload(image)
    await client.set_user_avatar(user_id, uploaded.mxc)
    # The digest of what was set: a picture that changed during the upload is recorded as the new one.
    await _write_record(client, user_id, data_type, source_url, uploaded.sha256, uploaded.mxc)
    log.info("set bot avatar from %s", source_url)


async def _write_record(
    client: AvatarClient, user_id: str, data_type: str, source_url: str, digest: str, mxc: str
) -> None:
    await client.set_account_data(
        user_id,
        data_type,
        {"schema_version": SCHEMA_VERSION, "source_url": source_url, "sha256": digest, "mxc": mxc},
    )
