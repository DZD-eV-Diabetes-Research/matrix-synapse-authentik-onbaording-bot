"""Tests for the idempotent bot avatar (G6.8).

The point of :mod:`onbot.avatar` is silence: a restart with an unchanged picture must not write the
profile, because that write shows up as "changed their profile picture" in every room the bot is in.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from onbot.avatar import bot_avatar_account_data_type, ensure_bot_avatar
from onbot.media import MediaUploader

AVATAR_URL = "https://cdn.test/face.png"
BOT = "@bot:company.org"
SERVER = "company.org"
DATA_TYPE = bot_avatar_account_data_type(SERVER)
IMAGE = b"the-picture"


class FakeClient:
    """Profile + account data + media, recording every write the bot makes."""

    def __init__(self, *, avatar: str | None = None, media: dict[str, bytes] | None = None) -> None:
        self.avatar = avatar
        self.account_data: dict[tuple[str, str], dict[str, Any]] = {}
        self.media: dict[str, bytes] = media or {}
        self.uploads: list[bytes] = []
        self.profile_writes: list[str] = []

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        return dict(self.account_data.get((user_id, data_type), {}))

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        self.account_data[(user_id, data_type)] = dict(content)

    async def get_user_avatar(self, user_id: str) -> str | None:
        return self.avatar

    async def set_user_avatar(self, user_id: str, mxc_uri: str) -> None:
        self.avatar = mxc_uri
        self.profile_writes.append(mxc_uri)

    async def download_media(self, mxc_uri: str) -> bytes:
        return self.media[mxc_uri]

    async def upload_media(self, content: bytes, *, content_type: str, filename: str | None = None) -> str:
        self.uploads.append(content)
        mxc = f"mxc://company.org/new{len(self.uploads)}"
        self.media[mxc] = content
        return mxc


def _remote() -> respx.Route:
    return respx.get(AVATAR_URL).mock(
        return_value=httpx.Response(200, content=IMAGE, headers={"content-type": "image/png"})
    )


async def _run(client: FakeClient, source_url: str | None = AVATAR_URL) -> MediaUploader:
    uploader = MediaUploader(client)  # type: ignore[arg-type]
    try:
        await ensure_bot_avatar(
            client,
            uploader,
            user_id=BOT,
            server_name=SERVER,
            source_url=source_url,
        )
    finally:
        await uploader.aclose()
    return uploader


class TestEnsureBotAvatar:
    @respx.mock
    async def test_unchanged_avatar_writes_nothing(self) -> None:
        """The restart case: record and profile agree, so no fetch, no upload, no profile write."""
        remote = _remote()
        client = FakeClient(avatar="mxc://company.org/old", media={"mxc://company.org/old": IMAGE})
        client.account_data[(BOT, DATA_TYPE)] = {"source_url": AVATAR_URL, "mxc": "mxc://company.org/old"}

        uploader = await _run(client)

        assert client.profile_writes == []
        assert client.uploads == []
        assert remote.call_count == 0
        # The mxc is still handed to anything else wearing the same source URL.
        assert await uploader.upload_from_url(AVATAR_URL) == "mxc://company.org/old"

    @respx.mock
    async def test_matching_bytes_are_adopted_without_touching_the_profile(self) -> None:
        """No record (first run after upgrade) but the picture is already right: record, stay quiet."""
        _remote()
        client = FakeClient(avatar="mxc://company.org/old", media={"mxc://company.org/old": IMAGE})

        await _run(client)

        assert client.profile_writes == []
        assert client.uploads == []
        assert client.account_data[(BOT, DATA_TYPE)]["mxc"] == "mxc://company.org/old"
        assert client.account_data[(BOT, DATA_TYPE)]["source_url"] == AVATAR_URL

    @respx.mock
    async def test_different_picture_is_uploaded_and_recorded(self) -> None:
        _remote()
        client = FakeClient(avatar="mxc://company.org/old", media={"mxc://company.org/old": b"other"})

        await _run(client)

        assert client.uploads == [IMAGE]
        assert client.profile_writes == [client.avatar]
        assert client.account_data[(BOT, DATA_TYPE)] == {
            "schema_version": 1,
            "source_url": AVATAR_URL,
            "mxc": client.avatar,
        }

    @respx.mock
    async def test_profile_without_avatar_is_set(self) -> None:
        _remote()
        client = FakeClient(avatar=None)

        await _run(client)

        assert client.uploads == [IMAGE]
        assert client.profile_writes == [client.avatar]

    @respx.mock
    async def test_stale_record_after_the_profile_changed_elsewhere(self) -> None:
        """Record says mxc A, profile carries mxc B holding another image: re-set from the source."""
        _remote()
        client = FakeClient(avatar="mxc://company.org/b", media={"mxc://company.org/b": b"other"})
        client.account_data[(BOT, DATA_TYPE)] = {"source_url": AVATAR_URL, "mxc": "mxc://company.org/a"}

        await _run(client)

        assert client.uploads == [IMAGE]
        assert client.profile_writes == [client.avatar]

    @respx.mock
    async def test_no_configured_url_is_a_noop(self) -> None:
        client = FakeClient(avatar="mxc://company.org/old")

        await _run(client, source_url=None)

        assert client.profile_writes == []
        assert client.account_data == {}

    @respx.mock
    async def test_fetch_failure_leaves_the_profile_alone_and_raises(self) -> None:
        # Raised, not swallowed: the startup supervisor in onbot.app retries it (RUN-02).
        respx.get(AVATAR_URL).mock(return_value=httpx.Response(500))
        client = FakeClient(avatar="mxc://company.org/old", media={"mxc://company.org/old": IMAGE})

        with pytest.raises(httpx.HTTPStatusError):
            await _run(client)

        assert client.profile_writes == []
        assert client.account_data == {}

    @respx.mock
    async def test_unreadable_current_avatar_falls_back_to_uploading(self) -> None:
        """A profile pointing at media the bot cannot read must not block the avatar being set."""
        _remote()
        client = FakeClient(avatar="mxc://company.org/gone")  # download_media raises KeyError

        await _run(client)

        assert client.uploads == [IMAGE]
        assert client.profile_writes == [client.avatar]
