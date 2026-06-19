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

Both satisfy the :class:`TokenProvider` protocol — a single ``async get_token()`` —
which :class:`~onbot.clients.base.BaseApiClient` calls per request, so a token can
rotate underneath a long-lived client without reconstructing it.
"""

from __future__ import annotations

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

    async def aclose(self) -> None: ...


class StaticTokenProvider:
    """A fixed bearer token (compat token or legacy Synapse token). Never expires."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("StaticTokenProvider requires a non-empty token")
        self._token = token

    async def get_token(self) -> str:
        return self._token

    async def aclose(self) -> None:  # symmetry with the OAuth2 provider; nothing to close
        return None


class OAuth2ClientCredentialsTokenProvider:
    """Mints + refreshes MAS access tokens via the OAuth2 ``client_credentials`` grant.

    The first :meth:`get_token` performs a ``client_credentials`` exchange and caches the
    access token together with its expiry. Subsequent calls return the cached token until
    it is within :data:`_EXPIRY_MARGIN_SEC` of expiring, then refresh it — via the
    ``refresh_token`` grant when one was issued, else a fresh ``client_credentials``
    exchange. Client authentication uses HTTP Basic per RFC 6749 §2.3.1.
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

    async def get_token(self) -> str:
        if self._access_token is not None and time.monotonic() < self._expires_at - _EXPIRY_MARGIN_SEC:
            return self._access_token
        return await self._refresh()

    async def _refresh(self) -> str:
        if self._refresh_token is not None:
            data = {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        else:
            data = {"grant_type": "client_credentials"}
            if self._scope:
                data["scope"] = self._scope
        payload = await self._token_request(data)
        self._store(payload)
        assert self._access_token is not None  # _store guarantees it or raised
        return self._access_token

    async def _token_request(self, data: dict[str, str]) -> dict[str, object]:
        response = await self._http.post(
            self._token_endpoint,
            data=data,
            auth=(self._client_id, self._client_secret),
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            # A failed refresh may mean the refresh token was revoked; drop it so the next
            # attempt falls back to a fresh client_credentials exchange.
            self._refresh_token = None
            raise OAuth2TokenError(response.status_code, _safe_json(response))
        result: dict[str, object] = response.json()
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
    """The MAS token endpoint rejected a token request."""

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"OAuth2 token request failed (HTTP {status_code}): {payload!r}")


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text
