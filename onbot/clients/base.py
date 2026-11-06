"""Async HTTP base client shared by the Authentik, Synapse-admin, MAS-admin and Matrix CS clients.

AD-7: one pooled ``httpx.AsyncClient`` with bearer-auth injection, retries on transient failures
(tenacity), a generic pagination helper, and typed errors. This replaces the legacy per-call
``requests`` churn and the ``access_token.lstrip("Bearer ")`` token-corruption bug (BATTLE_PLAN §3):
tokens are stored bare and the ``Bearer`` prefix is added exactly once here.

Retry policy (review findings RUN-05, RUN-06, RUN-10):

* **Idempotent requests** (GET, PUT, DELETE, and any call that says ``idempotent=True``) are retried
  on 429, 5xx and every transport error: sending them twice has the same effect as once.
* **Non-idempotent requests** (POST by default) are retried only when the failure proves the server
  did not act on them: the connection was never made (``ConnectError``, ``ConnectTimeout``,
  ``PoolTimeout``), the server rate-limited the request (429), or it answered 503 with a
  ``Retry-After``. A read timeout, a dropped connection or a 500/502/504 may come after the server
  already did the work, so resending could create a second room or turn a done kick into an error.
  Such a failure is raised; the caller decides (see ``ApiClientMatrix.create_room`` for a call that
  checks what happened before it gives up).
* **Waits** follow the server when it says how long (``retry_after_ms`` in a Matrix error body, the
  ``Retry-After`` header), capped at :data:`MAX_RETRY_AFTER_SEC`; otherwise exponential backoff.
* **Auth:** a token the provider cannot supply for a transient reason (MAS restarting, MAS
  unreachable) is retried for every method, since nothing was sent yet. A 401 makes the provider
  drop the rejected token; when it can mint a new one, the request is resent once with it.
"""

from __future__ import annotations

import email.utils
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Mapping
from types import TracebackType
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
)

from onbot.auth.token_provider import OAuth2TokenError, StaticTokenProvider, TokenProvider
from onbot.logging import get_logger

log = get_logger(__name__)

# Transient HTTP statuses worth retrying for idempotent requests (rate-limit + gateway/server errors).
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Methods that are idempotent by HTTP semantics (RFC 9110 9.2.2). Everything else (POST) is not,
# unless the call site says so.
IDEMPOTENT_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# Transport failures that happen before a single byte of the request reaches the server.
_NOT_SENT_ERRORS: tuple[type[httpx.TransportError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)

DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_MAX_RETRY_ATTEMPTS = 4
# Upper bound for a wait the server asks for: a throttle longer than this is better reported than
# waited out inside one call (the reconcile pass and the broadcast both retry on their next round).
MAX_RETRY_AFTER_SEC = 30.0
_BACKOFF_INITIAL_SEC = 0.5
_BACKOFF_MAX_SEC = 10.0


class ApiError(Exception):
    """A non-2xx API response, carrying enough context to debug without re-reading logs."""

    def __init__(
        self,
        method: str,
        url: str,
        status_code: int,
        payload: Any = None,
        *,
        retry_after: float | None = None,
    ) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.payload = payload
        # Seconds the server asked us to wait before trying again, if it said so.
        self.retry_after = retry_after
        super().__init__(f"{method} {url} -> HTTP {status_code}: {payload!r}")

    @property
    def is_retryable(self) -> bool:
        return self.status_code in RETRYABLE_STATUS_CODES

    @property
    def errcode(self) -> str | None:
        """The Matrix ``errcode`` of the body, ``None`` when the body is not a Matrix error."""
        if isinstance(self.payload, dict):
            code = self.payload.get("errcode")
            if isinstance(code, str) and code:
                return code
        return None


class ListingChangedError(Exception):
    """A paginated listing changed while it was read, so the result may be missing entries (RUN-14).

    Raised after a re-read did not give a consistent result either. Acting on such a list (for
    example removing everybody who is not in it) is unsafe; the caller should fail this round.
    """

    def __init__(self, path: str, *, reported: set[int], collected: int) -> None:
        self.path = path
        self.reported = reported
        self.collected = collected
        totals = ", ".join(str(n) for n in sorted(reported))
        super().__init__(
            f"listing {path} changed while it was read (server reported {totals} entries, "
            f"{collected} distinct ones were collected)"
        )


def request_was_not_processed(exc: BaseException) -> bool:
    """Whether ``exc`` proves the server did not act on the request (so resending is safe)."""
    if isinstance(exc, OAuth2TokenError):
        return True  # no token, nothing sent
    if isinstance(exc, ApiError):
        if exc.status_code == 429:
            return True
        if exc.status_code == 503 and exc.retry_after is not None:
            return True
        return 400 <= exc.status_code < 500  # a definite refusal; nothing happened
    return isinstance(exc, _NOT_SENT_ERRORS)


def may_have_been_processed(exc: BaseException) -> bool:
    """Whether ``exc`` is an HTTP failure that can hide a success (timeout, dropped connection, 5xx)."""
    return isinstance(exc, ApiError | httpx.TransportError) and not request_was_not_processed(exc)


async def resend_unless_done(
    exc: BaseException,
    *,
    done: Callable[[], Awaitable[bool]],
    resend: Callable[[], Awaitable[Any]],
    what: str,
) -> None:
    """Recover a non-idempotent call that failed with ``exc``, without doing it twice (RUN-05).

    Must be called from the ``except`` block that caught ``exc``. When ``exc`` may hide a success,
    ``done`` checks whether the effect is already there (for a kick: the user is gone); if so, the
    call counts as successful. If not, the call is sent once more. A failure that proves nothing
    happened, or a check that cannot be made, re-raises ``exc``.
    """
    if not may_have_been_processed(exc):
        raise exc
    try:
        effect_present = await done()
    except (ApiError, httpx.TransportError) as check_exc:
        log.warning("could not check whether %s took effect: %s", what, check_exc)
        raise exc from None
    if effect_present:
        log.warning("%s failed (%s), but it had taken effect", what, exc)
        return
    log.warning("%s failed (%s) and did not take effect; sending it once more", what, exc)
    await resend()


def should_retry(exc: BaseException, *, idempotent: bool) -> bool:
    """The retry policy described in the module docstring."""
    if isinstance(exc, OAuth2TokenError):
        return exc.is_transient
    if isinstance(exc, ApiError):
        if not exc.is_retryable:
            return False
        return idempotent or request_was_not_processed(exc)
    if isinstance(exc, httpx.TransportError):
        return idempotent or isinstance(exc, _NOT_SENT_ERRORS)
    return False


def _retry_wait(retry_state: RetryCallState) -> float:
    """Wait as long as the server asked (capped), else back off exponentially (0.5 s, 1 s, 2 s, ...)."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    if isinstance(exc, ApiError) and exc.retry_after is not None:
        return min(max(exc.retry_after, 0.0), MAX_RETRY_AFTER_SEC)
    backoff: float = _BACKOFF_INITIAL_SEC * 2 ** (retry_state.attempt_number - 1)
    return min(backoff, _BACKOFF_MAX_SEC)


def _retry_after(response: httpx.Response, payload: Any) -> float | None:
    """Seconds to wait from a Matrix ``retry_after_ms`` or a ``Retry-After`` header, if given."""
    if isinstance(payload, dict):
        ms = payload.get("retry_after_ms")
        if isinstance(ms, int | float) and not isinstance(ms, bool) and ms >= 0:
            return float(ms) / 1000.0
    header = response.headers.get("retry-after")
    if not header:
        return None
    header = header.strip()
    if header.isdigit():
        return float(header)
    try:
        when = email.utils.parsedate_to_datetime(header)
    except TypeError, ValueError:
        return None
    return max(when.timestamp() - time.time(), 0.0)


class BaseApiClient:
    """Thin async wrapper over ``httpx.AsyncClient`` with auth, retries and pagination."""

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        *,
        token_provider: TokenProvider | None = None,
        max_retry_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._max_retry_attempts = max_retry_attempts
        # Auth is resolved per request (AD-6): a static token or an OAuth2 provider that may
        # rotate the token underneath this long-lived client. A bare token is just the static case.
        if token_provider is None:
            if auth_token is None:
                raise ValueError("BaseApiClient needs either auth_token or token_provider")
            token_provider = StaticTokenProvider(auth_token)
        self._token_provider = token_provider
        headers = {"Accept": "application/json"}
        self._client = client or httpx.AsyncClient(headers=headers, timeout=timeout)
        # When an external client is injected (tests), make sure the Accept header is present.
        if client is not None:
            self._client.headers.update(headers)
        # Injectable so tests can observe the waits without sleeping; ``None`` is tenacity's default.
        self._sleep = sleep

    async def __aenter__(self) -> BaseApiClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._token_provider.aclose()

    def _build_url(self, path: str) -> str:
        # Absolute URLs pass through unchanged so callers can reach sibling APIs off the same
        # host (e.g. the Matrix media repo at /_matrix/media, outside this client's base path).
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}{path.lstrip('/')}"

    async def _with_retry(self, do: Callable[[], Awaitable[Any]], *, idempotent: bool) -> Any:
        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retry_attempts),
            wait=_retry_wait,
            retry=retry_if_exception(lambda exc: should_retry(exc, idempotent=idempotent)),
            reraise=True,
            **kwargs,
        ):
            with attempt:
                return await do()
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        """The one request path: auth, error mapping and the retry policy. Returns a 2xx response.

        ``idempotent`` defaults to the HTTP semantics of ``method``. Pass ``True`` for a POST that is
        safe to repeat (a read such as the sync long-poll, or an upsert). ``timeout`` overrides the
        client's default for this call only.
        """
        method = method.upper()
        url = self._build_url(path)
        # Drop ``None`` query params so callers can pass optional filters uniformly.
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        if idempotent is None:
            idempotent = method in IDEMPOTENT_METHODS
        request_timeout: Any = timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT

        async def _attempt(token: str) -> httpx.Response:
            req_headers = {"Authorization": f"Bearer {token}"}
            if headers:
                req_headers.update(headers)
            return await self._client.request(
                method,
                url,
                params=clean_params or None,
                json=json_body,
                content=content,
                headers=req_headers,
                timeout=request_timeout,
            )

        async def _do() -> httpx.Response:
            token = await self._token_provider.get_token()
            response = await _attempt(token)
            if response.status_code == 401 and await self._token_provider.invalidate(token):
                # The token was revoked or expired early. A 401 means the server did nothing, so
                # resending is safe for any method (RUN-06).
                log.info("%s %s was rejected with 401; retrying once with a fresh token", method, url)
                response = await _attempt(await self._token_provider.get_token())
            if response.status_code >= 400:
                payload = _safe_payload(response)
                raise ApiError(
                    method, url, response.status_code, payload, retry_after=_retry_after(response, payload)
                )
            return response

        response: httpx.Response = await self._with_retry(_do, idempotent=idempotent)
        return response

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """Perform a request with retries, returning decoded JSON (``None`` for empty bodies)."""
        response = await self._send(
            method, path, params=params, json_body=json_body, timeout=timeout, idempotent=idempotent
        )
        if not response.content:
            return None
        return response.json()

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        parse_json: bool = True,
        timeout: float | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """Like :meth:`request_json` but for raw byte bodies (media upload/download, MSC3916).

        Sends ``content`` verbatim with the caller's ``headers`` (e.g. ``Content-Type``). Returns
        decoded JSON when ``parse_json`` is true (upload responses), else the raw response bytes
        (authenticated media downloads).
        """
        response = await self._send(
            method,
            path,
            params=params,
            content=content,
            headers=headers,
            timeout=timeout,
            idempotent=idempotent,
        )
        if not parse_json:
            return response.content
        if not response.content:
            return None
        return response.json()

    async def get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return await self.request_json("GET", path, params=params)

    async def post_json(self, path: str, *, json_body: Any = None, idempotent: bool | None = None) -> Any:
        return await self.request_json("POST", path, json_body=json_body, idempotent=idempotent)

    async def put_json(self, path: str, *, json_body: Any = None) -> Any:
        return await self.request_json("PUT", path, json_body=json_body)

    async def delete_json(self, path: str, *, json_body: Any = None) -> Any:
        return await self.request_json("DELETE", path, json_body=json_body)

    async def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        extract_items: Callable[[Any], list[Any]],
        next_params: Callable[[Any, dict[str, Any]], dict[str, Any] | None],
    ) -> AsyncIterator[Any]:
        """Yield items across all pages (fixes the legacy 'no pagination → silent truncation' bug).

        ``extract_items`` pulls the item list out of a page; ``next_params`` returns the query params
        for the next page given the current page and current params, or ``None`` when exhausted.
        """
        async for page in self._pages(path, params=params, next_params=next_params):
            for item in extract_items(page):
                yield item

    async def _pages(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        next_params: Callable[[Any, dict[str, Any]], dict[str, Any] | None],
    ) -> AsyncIterator[Any]:
        current: dict[str, Any] = dict(params or {})
        while True:
            page = await self.get_json(path, params=current)
            yield page
            follow = next_params(page, current)
            if follow is None:
                return
            current = follow

    async def paginate_collect(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        extract_items: Callable[[Any], list[Any]],
        next_params: Callable[[Any, dict[str, Any]], dict[str, Any] | None],
        total: Callable[[Any], int | None] | None = None,
        item_key: Callable[[Any], Hashable] | None = None,
    ) -> list[Any]:
        """Collect every item of a listing, checked for consistency when the API reports a total.

        The APIs page by offset, so an entry that disappears before the current offset while the
        listing is read shifts every later entry up by one and the first entry of the next page is
        never seen (RUN-14); an entry that appears shifts them down and one is read twice. With
        ``total`` (the entry count a page reports) and ``item_key`` (an item's identity), a listing
        that spans several pages is accepted only when every page reported the same total and that
        many distinct items were collected. Otherwise it is read once more, and if that is still
        inconsistent, :class:`ListingChangedError` is raised rather than returning a list with holes.
        Duplicates are dropped.
        """
        reported: set[int] = set()
        collected = 0
        for read in range(2):
            items: list[Any] = []
            reported = set()
            pages = 0
            async for page in self._pages(path, params=params, next_params=next_params):
                pages += 1
                items.extend(extract_items(page))
                if total is not None:
                    count = total(page)
                    if count is not None:
                        reported.add(count)
            if item_key is None or pages <= 1 or not reported:
                return items  # nothing to check against (one page, or the API reports no total)
            unique = _dedupe(items, item_key)
            collected = len(unique)
            if reported == {collected}:
                return unique
            if read == 0:
                log.warning(
                    "listing %s changed while it was read (reported %s, collected %d); reading it again",
                    path,
                    sorted(reported),
                    collected,
                )
        raise ListingChangedError(path, reported=reported, collected=collected)


def _dedupe(items: list[Any], key: Callable[[Any], Hashable]) -> list[Any]:
    seen: set[Hashable] = set()
    unique: list[Any] = []
    for item in items:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        unique.append(item)
    return unique


def _safe_payload(response: httpx.Response) -> Any:
    """Best-effort decode of an error body for diagnostics (APIs often embed helpful detail)."""
    try:
        return response.json()
    except ValueError, UnicodeDecodeError:
        return response.text
