"""Tests for MediaUploader: fetch a remote URL, upload, and dedupe by source URL (G10.1/G10.2)."""

from __future__ import annotations

import traceback

import httpx
import pytest
import respx

from onbot.clients.matrix import ApiClientMatrix
from onbot.media import FAILED_FETCH_RETRY_SEC, RECHECK_INTERVAL_SEC, MediaUploader, content_digest
from onbot.safe_fetch import FetchedImage

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
    """An avatar host with an ETag per image: a conditional request for the current one gets a 304."""

    def __init__(self, images: dict[str, bytes], *, etags: bool = True) -> None:
        self.images = images
        self.etags = etags
        self.calls: list[str] = []
        self.conditional: list[str | None] = []

    async def fetch_if_changed(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchedImage | None:
        self.calls.append(url)
        self.conditional.append(etag)
        if url not in self.images:
            raise RuntimeError("not found")
        current = content_digest(self.images[url])[:8] if self.etags else None
        if etag is not None and etag == current:
            return None
        return FetchedImage(self.images[url], "image/png", etag=current)

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

    raised: list[BaseException] = []
    for _ in range(3):
        with pytest.raises(Exception, match="not found") as caught:
            await uploader.image("https://down.test/x.png")
        raised.append(caught.value)
    assert fetcher.calls == ["https://down.test/x.png"]
    # W5R-05: the reason is kept, not the exception object, whose traceback grew with every re-raise.
    assert raised[1] is not raised[2]
    assert len(traceback.extract_tb(raised[2].__traceback__)) == len(
        traceback.extract_tb(raised[1].__traceback__)
    )

    now[0] += FAILED_FETCH_RETRY_SEC
    fetcher.images["https://down.test/x.png"] = b"logo"
    assert (await uploader.image("https://down.test/x.png")).content == b"logo"
    assert len(fetcher.calls) == 2


# --- ONB-15 (S18): a known picture is looked at again about once a day ------------------------------


async def test_a_known_picture_is_rechecked_after_a_day_with_a_conditional_request() -> None:
    now = [0.0]
    fetcher = _Fetcher({"https://a.test/x.png": b"logo"})
    uploader = MediaUploader(_Uploads(), fetcher=fetcher, clock=lambda: now[0])  # type: ignore[arg-type]
    first = await uploader.image("https://a.test/x.png")

    now[0] += RECHECK_INTERVAL_SEC - 1
    assert (await uploader.image("https://a.test/x.png")).sha256 == first.sha256
    assert len(fetcher.calls) == 1  # still fresh

    now[0] += 1
    unchanged = await uploader.image("https://a.test/x.png")
    assert unchanged.sha256 == first.sha256
    assert fetcher.conditional == [None, content_digest(b"logo")[:8]]  # If-None-Match was sent

    fetcher.images["https://a.test/x.png"] = b"rebrand"
    now[0] += RECHECK_INTERVAL_SEC
    changed = await uploader.image("https://a.test/x.png")
    assert changed.sha256 == content_digest(b"rebrand")
    assert changed.content == b"rebrand"


async def test_a_server_without_validators_is_compared_by_content() -> None:
    now = [0.0]
    fetcher = _Fetcher({"https://a.test/x.png": b"logo"}, etags=False)
    uploader = MediaUploader(_Uploads(), fetcher=fetcher, clock=lambda: now[0])  # type: ignore[arg-type]
    await uploader.image("https://a.test/x.png")
    fetcher.images["https://a.test/x.png"] = b"rebrand"
    now[0] += RECHECK_INTERVAL_SEC
    assert (await uploader.image("https://a.test/x.png")).sha256 == content_digest(b"rebrand")


async def test_a_failed_recheck_keeps_the_known_picture() -> None:
    now = [0.0]
    fetcher = _Fetcher({"https://a.test/x.png": b"logo"})
    uploader = MediaUploader(_Uploads(), fetcher=fetcher, clock=lambda: now[0])  # type: ignore[arg-type]
    await uploader.image("https://a.test/x.png")
    del fetcher.images["https://a.test/x.png"]  # the host is down
    now[0] += RECHECK_INTERVAL_SEC
    assert (await uploader.image("https://a.test/x.png")).sha256 == content_digest(b"logo")
    assert (await uploader.image("https://a.test/x.png")).sha256 == content_digest(b"logo")
    assert len(fetcher.calls) == 2  # one failed re-check, then the retry waits


async def test_no_image_bytes_are_kept_and_a_needed_upload_fetches_once_more() -> None:
    """W5R-05: the bytes of every avatar URL used to stay in memory for the life of the process."""
    fetcher = _Fetcher({"https://a.test/x.png": b"logo"})
    uploads = _Uploads()
    uploader = MediaUploader(uploads, fetcher=fetcher)  # type: ignore[arg-type]
    assert (await uploader.image("https://a.test/x.png")).content == b"logo"

    remembered = await uploader.image("https://a.test/x.png")
    assert remembered.content is None
    assert all(not isinstance(v, bytes) for known in uploader._known.values() for v in vars_of(known))

    mxc = await uploader.mxc_for(remembered)
    assert uploads.uploads == [b"logo"]
    assert len(fetcher.calls) == 2
    assert await uploader.mxc_for(remembered) == mxc
    assert len(fetcher.calls) == 2


def vars_of(obj: object) -> list[object]:
    return [getattr(obj, name) for name in obj.__slots__]  # type: ignore[attr-defined]
