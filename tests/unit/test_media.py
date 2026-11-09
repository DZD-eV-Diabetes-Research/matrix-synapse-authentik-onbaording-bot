"""Tests for MediaUploader: fetch a remote URL, upload, and dedupe by source URL (G10.1/G10.2)."""

from __future__ import annotations

import httpx
import pytest
import respx

from onbot.clients.matrix import ApiClientMatrix
from onbot.media import FAILED_FETCH_RETRY_SEC, MediaUploader, content_digest

AVATAR_URL = "https://cdn.test/face.png"


def _client() -> ApiClientMatrix:
    return ApiClientMatrix(server_url="https://matrix.test", access_token="tok", server_name="matrix.test")


@respx.mock
async def test_upload_from_url_uploads_once_and_caches() -> None:
    remote = respx.get(AVATAR_URL).mock(
        return_value=httpx.Response(200, content=b"img", headers={"content-type": "image/png"})
    )
    upload = respx.post("https://matrix.test/_matrix/media/v3/upload").mock(
        return_value=httpx.Response(200, json={"content_uri": "mxc://matrix.test/abc"})
    )
    client = _client()
    uploader = MediaUploader(client)
    try:
        first = await uploader.upload_from_url(AVATAR_URL)
        second = await uploader.upload_from_url(AVATAR_URL)
    finally:
        await uploader.aclose()
        await client.aclose()

    assert first == second == "mxc://matrix.test/abc"
    # Deduped: the remote fetch and the upload each happen exactly once.
    assert remote.call_count == 1
    assert upload.call_count == 1
    assert upload.calls[0].request.headers["content-type"] == "image/png"


class _Fetcher:
    def __init__(self, images: dict[str, bytes]) -> None:
        self.images = images
        self.calls: list[str] = []

    async def fetch(self, url: str) -> tuple[bytes, str]:
        self.calls.append(url)
        if url not in self.images:
            raise RuntimeError("not found")
        return self.images[url], "image/png"

    async def aclose(self) -> None:
        return None


class _Uploads:
    def __init__(self) -> None:
        self.uploads: list[bytes] = []

    async def upload_media(self, content: bytes, *, content_type: str, filename: str | None = None) -> str:
        self.uploads.append(content)
        return f"mxc://matrix.test/{len(self.uploads)}"


async def test_the_same_picture_from_two_urls_is_uploaded_once() -> None:
    fetcher = _Fetcher({"https://a.test/x.png": b"logo", "https://b.test/y.png": b"logo"})
    client = _Uploads()
    uploader = MediaUploader(client, fetcher=fetcher)  # type: ignore[arg-type]

    first = await uploader.upload_from_url("https://a.test/x.png")
    second = await uploader.upload_from_url("https://b.test/y.png")

    assert first == second
    assert client.uploads == [b"logo"]
    assert (await uploader.image("https://a.test/x.png")).sha256 == content_digest(b"logo")


async def test_a_failed_fetch_is_not_repeated_for_every_room_that_wears_the_picture() -> None:
    now = [0.0]
    fetcher = _Fetcher({})
    uploader = MediaUploader(_Uploads(), fetcher=fetcher, clock=lambda: now[0])  # type: ignore[arg-type]

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await uploader.image("https://down.test/x.png")
    assert fetcher.calls == ["https://down.test/x.png"]

    now[0] += FAILED_FETCH_RETRY_SEC
    fetcher.images["https://down.test/x.png"] = b"logo"
    assert (await uploader.image("https://down.test/x.png")).content == b"logo"
    assert len(fetcher.calls) == 2
