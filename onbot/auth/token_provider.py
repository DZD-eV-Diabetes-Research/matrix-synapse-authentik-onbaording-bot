"""MAS-aware access-token providers (AD-6, Phase 6).

The bot authenticates to the Synapse CS + admin APIs with a bearer token. Under the
MAS topology two ways of obtaining that token coexist, and the rest of the code must
not care which is in use:

* :class:`StaticTokenProvider` — a fixed token. This is the near-term path: a
  *compatibility token* issued by ``mas-cli manage issue-compatibility-token`` (or a
  legacy Synapse access token when not running MAS). Nothing expires; the token is
  returned verbatim.
* :class:`OAuth2ClientCredentialsTokenProvider` — the forward-looking path: the bot is
  a confidential OAuth2 client of MAS and mints short-lived access tokens via the
  ``client_credentials`` grant, transparently refreshing them before they expire
  (using the ``refresh_token`` grant when MAS returns a refresh token, otherwise a
  fresh ``client_credentials`` exchange).

Both satisfy the :class:`TokenProvider` protocol (``get_token()``, plus ``invalidate()`` for a
token the server rejected), which :class:`~onbot.clients.base.BaseApiClient` calls per request, so
a token can rotate underneath a long-lived client without reconstructing it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable

import httpx

from onbot.logging import get_logger

log = get_logger(__name__)

# Refresh a little before the server-stated expiry so an in-flight request never races
# the cutoff (clock skew + network latency margin).
_EXPIRY_MARGIN_SEC = 30.0


@runtime_checkable
class TokenProvider(Protocol):
    """Supplies a bearer token for the Authorization header; may refresh under the hood."""

    async def get_token(self) -> str: ...

    async def invalidate(self, token: str) -> bool:
        """Forget ``token`` after the server rejected it (HTTP 401).

        Returns ``True`` when the next :meth:`get_token` can supply a different token, so resending
        the rejected request makes sense; ``False`` when the provider has nothing else to offer.
        """
        ...

    async def aclose(self) -> None: ...


class StaticTokenProvider:
    """A fixed bearer token (compat token or legacy Synapse token). Never expires."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("StaticTokenProvider requires a non-empty token")
        self._token = token

    async def get_token(self) -> str:
        return self._token

    async def invalidate(self, token: str) -> bool:
        return False  # the same token is all there is; resending would only fail again

    async def aclose(self) -> None:  # symmetry with the OAuth2 provider; nothing to close
        return None


class OAuth2ClientCredentialsTokenProvider:
    """Mints + refreshes MAS access tokens via the OAuth2 ``client_credentials`` grant.

    The first :meth:`get_token` performs a ``client_credentials`` exchange and caches the
    access token together with its expiry. Subsequent calls return the cached token until
    it is within :data:`_EXPIRY_MARGIN_SEC` of expiring, then refresh it, via the
    ``refresh_token`` grant when one was issued, else a fresh ``client_credentials``
    exchange. Client authentication uses HTTP Basic per RFC 6749 §2.3.1.

    Concurrency (RUN-06, SEC-10): one provider serves every task of the process (reconcile pass,
    sync long-poll, control room, broadcast fan-out). A refresh is single-flight: callers that find
    the token stale wait for the one refresh in progress and reuse its result, instead of each
    running an exchange (one MAS session each) or presenting the same single-use refresh token.

    Failures: a refresh token MAS rejects (``invalid_grant``) is dropped and a ``client_credentials``
    exchange is tried right away. Any other failure keeps the refresh token and raises
    :class:`OAuth2TokenError`; its :attr:`~OAuth2TokenError.is_transient` tells the HTTP clients
    whether to retry (MAS restarting or unreachable) or give up (bad client credentials).
    """

    def __init__(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._http = client or httpx.AsyncClient(timeout=30.0)
        self._owns_http = client is None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _cached(self) -> str | None:
        if self._access_token is not None and time.monotonic() < self._expires_at - _EXPIRY_MARGIN_SEC:
            return self._access_token
        return None

    async def get_token(self) -> str:
        token = self._cached()
        if token is not None:
            return token
        async with self._lock:
            # Whoever held the lock before us may have refreshed already.
            token = self._cached()
            if token is not None:
                return token
            return await self._refresh()

    async def invalidate(self, token: str) -> bool:
        """Drop ``token`` if it is still the cached one (a concurrent caller may have replaced it)."""
        async with self._lock:
            if self._access_token == token:
                self._access_token = None
                self._expires_at = 0.0
        return True

    async def _refresh(self) -> str:
        payload: dict[str, object] | None = None
        if self._refresh_token is not None:
            try:
                payload = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
                )
            except OAuth2TokenError as exc:
                if not exc.is_invalid_grant:
                    raise
                # The refresh token was revoked, expired or already used: start over.
                log.info("MAS rejected the refresh token (invalid_grant); requesting a new token")
                self._refresh_token = None
        if payload is None:
            data = {"grant_type": "client_credentials"}
            if self._scope:
                data["scope"] = self._scope
            payload = await self._token_request(data)
        self._store(payload)
        assert self._access_token is not None  # _store guarantees it or raised
        return self._access_token

    async def _token_request(self, data: dict[str, str]) -> dict[str, object]:
        try:
            response = await self._http.post(
                self._token_endpoint,
                data=data,
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.TransportError as exc:
            # Reported as a token error so callers can tell "no token, nothing was sent" apart from
            # a failure of the request the token was for (see onbot.clients.base).
            raise OAuth2TokenError(None, f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise OAuth2TokenError(response.status_code, _safe_json(response))
        try:
            result = response.json()
        except ValueError as exc:
            raise OAuth2TokenError(response.status_code, response.text) from exc
        if not isinstance(result, dict):
            raise OAuth2TokenError(response.status_code, result)
        return result

    def _store(self, payload: dict[str, object]) -> None:
        token = payload.get("access_token")
        if not isinstance(token, str):
            raise OAuth2TokenError(200, payload)
        self._access_token = token
        refresh = payload.get("refresh_token")
        self._refresh_token = refresh if isinstance(refresh, str) else None
        expires_in = payload.get("expires_in")
        # Default to a conservative lifetime if the server omits expires_in.
        seconds = float(expires_in) if isinstance(expires_in, int | float) else 300.0
        self._expires_at = time.monotonic() + seconds

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()


class OAuth2TokenError(Exception):
    """The MAS token endpoint rejected a token request, or could not be reached.

    ``status_code`` is ``None`` when no HTTP answer arrived (connection refused, timeout).
    """

    def __init__(self, status_code: int | None, payload: object) -> None:
        self.status_code = status_code
        self.payload = payload
        where = f"HTTP {status_code}" if status_code is not None else "no response"
        super().__init__(f"OAuth2 token request failed ({where}): {payload!r}")

    @property
    def error(self) -> str | None:
        """The OAuth2 ``error`` code of the body (RFC 6749 5.2), if any."""
        if isinstance(self.payload, dict):
            code = self.payload.get("error")
            if isinstance(code, str):
                return code
        return None

    @property
    def is_invalid_grant(self) -> bool:
        return self.status_code == 400 and self.error == "invalid_grant"

    @property
    def is_transient(self) -> bool:
        """Whether trying again later can succeed: MAS unreachable, overloaded or restarting."""
        if self.status_code is None:
            return True
        return self.status_code == 429 or self.status_code >= 500


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text
