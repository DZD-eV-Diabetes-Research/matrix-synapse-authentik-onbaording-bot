"""Remote-URL → Matrix media upload with deduplication (G10.1, G10.2, Phase 6).

Avatars (bot, rooms, the space) are configured as plain HTTP(S) URLs. To use them in Matrix they
must be fetched and uploaded to the homeserver's media repo, which returns an ``mxc://`` URI. This
helper does that and **deduplicates by source URL within a run**, so the same avatar URL is fetched
and uploaded at most once (the legacy bot re-uploaded on every tick). Uploads go through the
authenticated media endpoint on :class:`~onbot.clients.matrix.ApiClientMatrix` (MSC3916).
"""

from __future__ import annotations

import httpx

from onbot.clients.matrix import ApiClientMatrix
from onbot.logging import get_logger

log = get_logger(__name__)

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


class MediaUploader:
    def __init__(self, client: ApiClientMatrix, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.client = client
        self._http = http_client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._owns_http = http_client is None
        # Source URL -> mxc URI, so a repeated URL uploads only once (G10.2).
        self._cache: dict[str, str] = {}

    async def upload_from_url(self, url: str) -> str:
        """Fetch ``url`` and upload it, returning the ``mxc://`` URI (cached per source URL)."""
        cached = self._cache.get(url)
        if cached is not None:
            return cached
        content, content_type = await self.fetch(url)
        return await self.upload(url, content, content_type)

    async def fetch(self, url: str) -> tuple[bytes, str]:
        """Download the source image, returning its bytes and content type (no upload, no cache).

        Split out of :meth:`upload_from_url` so a caller can compare the bytes against what the
        homeserver already holds before deciding to upload them again (see :mod:`onbot.avatar`).
        """
        response = await self._http.get(url)
        response.raise_for_status()
        raw_type = response.headers.get("content-type", _DEFAULT_CONTENT_TYPE).split(";")[0].strip()
        return response.content, raw_type or _DEFAULT_CONTENT_TYPE

    async def upload(self, url: str, content: bytes, content_type: str) -> str:
        """Upload already-fetched bytes, caching the result under their source ``url``."""
        mxc = await self.client.upload_media(content, content_type=content_type)
        self.remember(url, mxc)
        log.info("uploaded avatar %s -> %s", url, mxc)
        return mxc

    def remember(self, url: str, mxc: str) -> None:
        """Record that ``url`` is already on the homeserver as ``mxc``.

        Lets a caller that skipped an upload (because the media is unchanged) still hand the existing
        mxc to everything else wearing the same source URL, instead of re-uploading it.
        """
        self._cache[url] = mxc

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
