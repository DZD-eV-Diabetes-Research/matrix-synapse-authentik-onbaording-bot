"""Async HTTP base client shared by the Authentik, Synapse-admin and (later) Matrix CS clients.

AD-7: one pooled ``httpx.AsyncClient`` with bearer-auth injection, retries on transient failures
(tenacity), a generic pagination helper, and typed errors. This replaces the legacy per-call
``requests`` churn and the ``access_token.lstrip("Bearer ")`` token-corruption bug (BATTLE_PLAN §3):
tokens are stored bare and the ``Bearer`` prefix is added exactly once here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from onbot.auth.token_provider import StaticTokenProvider, TokenProvider
from onbot.logging import get_logger

log = get_logger(__name__)

# Transient HTTP statuses worth retrying (rate-limit + gateway/server errors).
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class ApiError(Exception):
    """A non-2xx API response, carrying enough context to debug without re-reading logs."""

    def __init__(
        self,
        method: str,
        url: str,
        status_code: int,
        payload: Any = None,
    ) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"{method} {url} -> HTTP {status_code}: {payload!r}")

    @property
    def is_retryable(self) -> bool:
        return self.status_code in RETRYABLE_STATUS_CODES


def _is_retryable_exc(exc: BaseException) -> bool:
    if isinstance(exc, ApiError):
        return exc.is_retryable
    # Connection resets, timeouts, etc. are worth retrying; bad requests are not.
    return isinstance(exc, httpx.TransportError)


class BaseApiClient:
    """Thin async wrapper over ``httpx.AsyncClient`` with auth, retries and pagination."""

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        *,
        token_provider: TokenProvider | None = None,
        max_retry_attempts: int = 4,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
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

    async def _auth_headers(self) -> dict[str, str]:
        """Per-request Authorization header (the token may have rotated; see AD-6)."""
        return {"Authorization": f"Bearer {await self._token_provider.get_token()}"}

    async def _with_retry(self, do: Callable[[], Awaitable[Any]]) -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retry_attempts),
            wait=wait_exponential(multiplier=0.5, max=10),
            retry=retry_if_exception(_is_retryable_exc),
            reraise=True,
        ):
            with attempt:
                return await do()
        raise AssertionError("unreachable")  # pragma: no cover

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Perform a request with retries, returning decoded JSON (``None`` for empty bodies)."""
        url = self._build_url(path)
        # Drop ``None`` query params so callers can pass optional filters uniformly.
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        async def _do() -> Any:
            headers = await self._auth_headers()
            response = await self._client.request(
                method, url, params=clean_params or None, json=json_body, headers=headers
            )
            if response.status_code >= 400:
                raise ApiError(method, url, response.status_code, _safe_payload(response))
            if not response.content:
                return None
            return response.json()

        return await self._with_retry(_do)

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        parse_json: bool = True,
    ) -> Any:
        """Like :meth:`request_json` but for raw byte bodies (media upload/download, MSC3916).

        Sends ``content`` verbatim with the caller's ``headers`` (e.g. ``Content-Type``). Returns
        decoded JSON when ``parse_json`` is true (upload responses), else the raw response bytes
        (authenticated media downloads).
        """
        url = self._build_url(path)
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        async def _do() -> Any:
            req_headers = await self._auth_headers()
            if headers:
                req_headers.update(headers)
            response = await self._client.request(
                method, url, params=clean_params or None, content=content, headers=req_headers
            )
            if response.status_code >= 400:
                raise ApiError(method, url, response.status_code, _safe_payload(response))
            if not parse_json:
                return response.content
            if not response.content:
                return None
            return response.json()

        return await self._with_retry(_do)

    async def get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return await self.request_json("GET", path, params=params)

    async def post_json(self, path: str, *, json_body: Any = None) -> Any:
        return await self.request_json("POST", path, json_body=json_body)

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
        current: dict[str, Any] = dict(params or {})
        while True:
            page = await self.get_json(path, params=current)
            for item in extract_items(page):
                yield item
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
    ) -> list[Any]:
        items: list[Any] = []
        async for item in self.paginate(
            path, params=params, extract_items=extract_items, next_params=next_params
        ):
            items.append(item)
        return items


def _safe_payload(response: httpx.Response) -> Any:
    """Best-effort decode of an error body for diagnostics (APIs often embed helpful detail)."""
    try:
        return response.json()
    except ValueError, UnicodeDecodeError:
        return response.text
