"""Remote-URL → Matrix media upload with deduplication (G10.1, G10.2, Phase 6, ONB-15).

Avatars (bot, rooms, the space, the notice boards) are configured as plain HTTP(S) URLs. To use them
in Matrix they must be fetched and uploaded to the homeserver's media repo, which returns an
``mxc://`` URI. This helper does that in two steps:

* :meth:`MediaUploader.image` fetches a URL and knows its SHA-256 from then on. The digest is what the
  bot records next to every avatar it sets, so a picture replaced behind an unchanged URL
  (``https://company.org/logo.png`` after a rebrand) is noticed (ONB-15). A URL is looked at again
  once :data:`RECHECK_INTERVAL_SEC` (a day) has passed, with a conditional request (``If-None-Match``
  and ``If-Modified-Since`` from the last answer); a server without validators, or one that ignores
  them, sends the whole image, which is compared by its digest. A changed picture then reaches the
  bot's profile, the rooms, the lobbies, the space and the notice boards without a restart. A failed
  re-check keeps the picture known so far and tries again after :data:`FAILED_FETCH_RETRY_SEC`.
* :meth:`MediaUploader.upload` uploads an image **once per content**: the same bytes, from one URL
  or several, end up as one ``mxc://`` URI. A caller that found the content already on the
  homeserver (the bot's profile picture) hands its mxc over with :meth:`MediaUploader.remember`.

Only the digest, content type and validators of a URL are kept, never its bytes (W5R-05): the image
a caller uploads is the one it just fetched, handed over with its bytes. In the rare case it only has
the digest (a new room wearing a known picture that was never uploaded) the URL is fetched once more,
under the same lock and failure backoff as every other fetch of it. Should the picture have changed
meanwhile, the new one is learnt and uploaded, and :class:`UploadedImage` says which one it is, so the
caller records the digest of what it set (W6R-02).

A mxc is remembered for a digest only when the bot uploaded those bytes itself or knows the media to
be its own (the bot's profile picture); :meth:`MediaUploader.holds` only compares, because the media
a human set in a room may live on another server or be removed again (W6R-06).

A failed fetch is remembered for :data:`FAILED_FETCH_RETRY_SEC` with its reason (not the exception
object, whose traceback would grow with every re-raise), so a host that is down costs one attempt
(and one warning) rather than one per room that wears its picture.

Uploads go through the authenticated media endpoint on :class:`~onbot.clients.matrix.ApiClientMatrix`
(MSC3916). Downloads go through :class:`~onbot.safe_fetch.SafeImageFetcher`, because a room avatar
URL is chosen by whoever may edit an Authentik group, not by the operator (SEC-03); it refuses
anything that is not an image, and its limits apply to the re-checks too.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from onbot.clients.matrix import ApiClientMatrix
from onbot.logging import get_logger
from onbot.safe_fetch import FetchedImage, MediaFetchError, MediaFetchRejected, SafeImageFetcher

log = get_logger(__name__)

# How long a failed fetch of a URL is answered from memory before the URL is tried again.
FAILED_FETCH_RETRY_SEC = 300.0
# How often a URL whose picture is known is looked at again (ONB-15): about once a day.
RECHECK_INTERVAL_SEC = 24 * 60 * 60.0


def content_digest(content: bytes) -> str:
    """The SHA-256 of an image, as recorded next to the avatars the bot sets."""
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceImage:
    """An avatar image as fetched from its source URL.

    ``content`` is ``None`` when the image comes from memory: only freshly fetched bytes are handed
    out, none are kept (W5R-05).
    """

    url: str
    content: bytes | None
    content_type: str
    sha256: str


@dataclass(frozen=True, slots=True)
class UploadedImage:
    """The ``mxc://`` URI of an uploaded avatar and the digest of the bytes it holds."""

    mxc: str
    sha256: str


@dataclass(slots=True)
class _Known:
    """What is remembered of a URL: its picture's digest and type, and when and how to ask again."""

    sha256: str
    content_type: str
    checked_at: float
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class _Failure:
    at: float
    reason: str
    rejected: bool


class MediaUploader:
    def __init__(
        self,
        client: ApiClientMatrix,
        *,
        fetcher: SafeImageFetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
        recheck_interval_sec: float = RECHECK_INTERVAL_SEC,
    ) -> None:
        self.client = client
        self._fetcher = fetcher or SafeImageFetcher()
        self._owns_fetcher = fetcher is None
        self._clock = clock
        self.recheck_interval_sec = recheck_interval_sec
        # Source URL -> what is known of its picture (ONB-15).
        self._known: dict[str, _Known] = {}
        # Source URL -> when and why its last fetch failed.
        self._failed: dict[str, _Failure] = {}
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        # Content digest -> mxc URI, so the same bytes are uploaded once (G10.2).
        self._mxc_by_digest: dict[str, str] = {}

    async def image(self, url: str) -> SourceImage:
        """The image at ``url``: from memory, or fetched when it is new or due for its re-check.

        A URL is fetched the first time, then again about once a day (conditionally), and not again
        soon after a failure. A failed re-check keeps the known picture. Raises
        :class:`~onbot.safe_fetch.MediaFetchRejected` when the fetch policy refuses the URL, or another
        error when a URL whose picture is not known yet cannot be fetched.
        """
        async with self._fetch_locks.setdefault(url, asyncio.Lock()):
            now = self._clock()
            known = self._known.get(url)
            if known is not None and now - known.checked_at < self.recheck_interval_sec:
                return SourceImage(url, None, known.content_type, known.sha256)
            failed = self._failed.get(url)
            if failed is not None and now - failed.at < FAILED_FETCH_RETRY_SEC:
                if known is not None:
                    return SourceImage(url, None, known.content_type, known.sha256)
                self._raise_failed(url, failed, now)
            try:
                fetched = await self._fetcher.fetch_if_changed(
                    url,
                    etag=known.etag if known else None,
                    last_modified=known.last_modified if known else None,
                )
            except Exception as exc:
                reason = self._record_failure(url, exc, now)
                if known is None:
                    log.warning("could not fetch the avatar image %s: %s", url, reason)
                    raise
                log.warning(
                    "could not check the avatar image %s for a new picture (%s); keeping the known one",
                    url,
                    reason,
                )
                return SourceImage(url, None, known.content_type, known.sha256)
            self._failed.pop(url, None)
            if fetched is None:  # 304: not modified
                assert known is not None
                known.checked_at = now
                log.debug("the avatar image %s is unchanged", url)
                return SourceImage(url, None, known.content_type, known.sha256)
            return self._learn(url, fetched, now, previous=known)

    def _record_failure(self, url: str, exc: Exception, now: float) -> str:
        reason = str(exc) or type(exc).__name__
        self._failed[url] = _Failure(now, reason, isinstance(exc, MediaFetchRejected))
        return reason

    @staticmethod
    def _raise_failed(url: str, failed: _Failure, now: float) -> None:
        error = MediaFetchRejected if failed.rejected else MediaFetchError
        raise error(f"fetching {url} failed {now - failed.at:.0f}s ago: {failed.reason}")

    async def _refetch(self, url: str) -> SourceImage:
        """Fetch ``url`` again for its bytes, under its lock and failure backoff (W6R-02).

        Unconditional, since the bytes are wanted. A different picture than the known one is learnt
        like a re-check that found a change.
        """
        async with self._fetch_locks.setdefault(url, asyncio.Lock()):
            now = self._clock()
            failed = self._failed.get(url)
            if failed is not None and now - failed.at < FAILED_FETCH_RETRY_SEC:
                self._raise_failed(url, failed, now)
            try:
                fetched = await self._fetcher.fetch_if_changed(url)
            except Exception as exc:
                log.warning(
                    "could not fetch the avatar image %s: %s", url, self._record_failure(url, exc, now)
                )
                raise
            self._failed.pop(url, None)
            assert fetched is not None  # an unconditional request is never answered with 304
            return self._learn(url, fetched, now, previous=self._known.get(url))

    def _learn(self, url: str, fetched: FetchedImage, now: float, *, previous: _Known | None) -> SourceImage:
        digest = content_digest(fetched.content)
        if previous is not None and previous.sha256 != digest:
            log.info("the avatar image behind %s changed; it is set wherever it is worn", url)
        self._known[url] = _Known(digest, fetched.content_type, now, fetched.etag, fetched.last_modified)
        return SourceImage(url, fetched.content, fetched.content_type, digest)

    def known_digest(self, url: str) -> str | None:
        """The digest of the picture last fetched from ``url``, without fetching anything."""
        known = self._known.get(url)
        return None if known is None else known.sha256

    async def upload(self, image: SourceImage) -> UploadedImage:
        """The ``mxc://`` URI holding ``image``'s content, uploading it the first time.

        An image from memory (no bytes) whose content was never uploaded is fetched once more. Should
        the picture have changed meanwhile, the new one is uploaded and its digest returned: the caller
        records what it set, not what it asked for (W6R-02).
        """
        known = self._mxc_by_digest.get(image.sha256)
        if known is not None:
            return UploadedImage(known, image.sha256)
        if image.content is None:
            image = await self._refetch(image.url)
            known = self._mxc_by_digest.get(image.sha256)
            if known is not None:
                return UploadedImage(known, image.sha256)
        assert image.content is not None
        mxc = await self.client.upload_media(image.content, content_type=image.content_type)
        self.remember(image.sha256, mxc)
        log.info("uploaded avatar %s -> %s", image.url, mxc)
        return UploadedImage(mxc, image.sha256)

    async def upload_from_url(self, url: str) -> str:
        """Fetch ``url`` (see :meth:`image`) and return the ``mxc://`` URI of its content."""
        return (await self.upload(await self.image(url))).mxc

    def known_mxc(self, digest: str) -> str | None:
        """The mxc this process knows to hold the content with ``digest``, if any."""
        return self._mxc_by_digest.get(digest)

    def remember(self, digest: str, mxc: str) -> None:
        """Record that the content with ``digest`` is already on the homeserver as ``mxc``.

        Lets a caller that skipped an upload (because the media is unchanged) still hand the existing
        mxc to everything else wearing the same picture, instead of re-uploading it.
        """
        self._mxc_by_digest[digest] = mxc

    async def holds(self, mxc: str, digest: str) -> bool:
        """Whether the media at ``mxc`` has the content ``digest`` (``False`` when it cannot be read).

        Only compares: the mxc is not remembered for the digest (W6R-06). A room's avatar may have
        been set by a human, from media on another server or media that is removed later, and
        remembering it would hand that mxc to every room wearing the same picture.
        """
        if self.known_mxc(digest) == mxc:
            return True
        try:
            return content_digest(await self.client.download_media(mxc)) == digest
        except Exception:
            log.debug("could not read the media %s", mxc, exc_info=True)
            return False

    async def aclose(self) -> None:
        if self._owns_fetcher:
            await self._fetcher.aclose()
