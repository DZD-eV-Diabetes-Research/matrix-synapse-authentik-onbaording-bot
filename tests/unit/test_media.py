"""Tests for MediaUploader: fetch a remote URL, upload, and dedupe by source URL (G10.1/G10.2)."""

from __future__ import annotations

import httpx
import respx

from onbot.clients.matrix import ApiClientMatrix
from onbot.media import MediaUploader

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
