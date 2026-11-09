"""Remote-URL → Matrix media upload with deduplication (G10.1, G10.2, Phase 6, ONB-15).

Avatars (bot, rooms, the space, the notice boards) are configured as plain HTTP(S) URLs. To use them
in Matrix they must be fetched and uploaded to the homeserver's media repo, which returns an
``mxc://`` URI. This helper does that in two steps:

* :meth:`MediaUploader.image` fetches a URL **once per process** and keeps the bytes with their
  SHA-256. The digest is what the bot records next to every avatar it sets, so a picture replaced
  behind an unchanged URL (``https://company.org/logo.png`` after a rebrand) is noticed on the next
  start instead of never (ONB-15).
* :meth:`MediaUploader.mxc_for` uploads an image **once per content**: the same bytes, from one URL
  or several, end up as one ``mxc://`` URI. A caller that found the content already on the
  homeserver (the bot's profile picture) hands its mxc over with :meth:`MediaUploader.remember`.

A failed fetch is remembered for :data:`FAILED_FETCH_RETRY_SEC`, so a host that is down costs one
attempt (and one warning) rather than one per room that wears its picture.

Uploads go through the authenticated media endpoint on :class:`~onbot.clients.matrix.ApiClientMatrix`
(MSC3916). Downloads go through :class:`~onbot.safe_fetch.SafeImageFetcher`, because a room avatar
URL is chosen by whoever may edit an Authentik group, not by the operator (SEC-03); it refuses
anything that is not an image.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from onbot.clients.matrix import ApiClientMatrix
from onbot.logging import get_logger
from onbot.safe_fetch import SafeImageFetcher

log = get_logger(__name__)

# How long a failed fetch of a URL is answered from memory before the URL is tried again.
FAILED_FETCH_RETRY_SEC = 300.0


def content_digest(content: bytes) -> str:
    """The SHA-256 of an image, as recorded next to the avatars the bot sets."""
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceImage:
    """An avatar image as fetched from its source URL."""

    url: str
    content: bytes
    content_type: str
    sha256: str


class MediaUploader:
    def __init__(
        self,
        client: ApiClientMatrix,
        *,
        fetcher: SafeImageFetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self._fetcher = fetcher or SafeImageFetcher()
        self._owns_fetcher = fetcher is None
        self._clock = clock
        # Source URL -> its image, fetched once per process (ONB-15).
        self._images: dict[str, SourceImage] = {}
        # Source URL -> (when, why) its last fetch failed.
        self._failed: dict[str, tuple[float, BaseException]] = {}
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        # Content digest -> mxc URI, so the same bytes are uploaded once (G10.2).
        self._mxc_by_digest: dict[str, str] = {}

    async def image(self, url: str) -> SourceImage:
        """The image at ``url``, fetched at most once per process (and not again soon after a failure).

        Raises :class:`~onbot.safe_fetch.MediaFetchRejected` when the fetch policy refuses the URL, or
        the fetch error.
        """
        async with self._fetch_locks.setdefault(url, asyncio.Lock()):
            cached = self._images.get(url)
            if cached is not None:
                return cached
            failed = self._failed.get(url)
            if failed is not None and self._clock() - failed[0] < FAILED_FETCH_RETRY_SEC:
                raise failed[1]
            try:
                content, content_type = await self._fetcher.fetch(url)
            except Exception as exc:
                log.warning("could not fetch the avatar image %s: %s", url, exc)
                self._failed[url] = (self._clock(), exc)
                raise
            self._failed.pop(url, None)
            image = SourceImage(url, content, content_type, content_digest(content))
            self._images[url] = image
            return image

    async def mxc_for(self, image: SourceImage) -> str:
        """The ``mxc://`` URI holding ``image``'s bytes, uploading them the first time."""
        known = self._mxc_by_digest.get(image.sha256)
        if known is not None:
            return known
        mxc = await self.client.upload_media(image.content, content_type=image.content_type)
        self.remember(image.sha256, mxc)
        log.info("uploaded avatar %s -> %s", image.url, mxc)
        return mxc

    async def upload_from_url(self, url: str) -> str:
        """Fetch ``url`` (once per process) and return the ``mxc://`` URI of its content."""
        return await self.mxc_for(await self.image(url))

    def known_mxc(self, digest: str) -> str | None:
        """The mxc this process knows to hold the content with ``digest``, if any."""
        return self._mxc_by_digest.get(digest)

    def remember(self, digest: str, mxc: str) -> None:
        """Record that the content with ``digest`` is already on the homeserver as ``mxc``.

        Lets a caller that skipped an upload (because the media is unchanged) still hand the existing
        mxc to everything else wearing the same picture, instead of re-uploading it.
        """
        self._mxc_by_digest[digest] = mxc

    async def aclose(self) -> None:
        if self._owns_fetcher:
            await self._fetcher.aclose()
