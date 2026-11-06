"""Token provider under concurrency and failure (review findings RUN-06, SEC-10).

One provider serves every task of the process, so a stale token is met by many callers at once. These
tests drive it through an ``httpx.MockTransport`` whose handler yields to the event loop, so the
callers really do overlap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs

import httpx
import pytest

from onbot.auth.token_provider import OAuth2ClientCredentialsTokenProvider, OAuth2TokenError

TOKEN_ENDPOINT = "https://auth.test/oauth2/token"

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _provider(handler: Handler) -> OAuth2ClientCredentialsTokenProvider:
    return OAuth2ClientCredentialsTokenProvider(
        token_endpoint=TOKEN_ENDPOINT,
        client_id="bot",
        client_secret="s3cret",
        scope="urn:mas:admin",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


async def test_concurrent_callers_share_one_exchange() -> None:
    """Before the fix, 10 concurrent callers ran 10 exchanges (10 MAS sessions, RUN-06 item 1)."""
    grants: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        grants.append(_form(request)["grant_type"])
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"access_token": f"at{len(grants)}", "expires_in": 3600})

    provider = _provider(handler)
    tokens = await asyncio.gather(*(provider.get_token() for _ in range(10)))
    assert grants == ["client_credentials"]
    assert set(tokens) == {"at1"}


async def test_single_use_refresh_token_is_presented_once() -> None:
    """MAS rotates refresh tokens. Before the fix, 4 of 5 concurrent callers presented the used token,
    failed with invalid_grant, and the losers wiped the winner's new refresh token (RUN-06 item 2)."""
    valid_refresh = {"rt1"}
    seen: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        form = _form(request)
        seen.append(form)
        await asyncio.sleep(0.01)
        if form["grant_type"] == "client_credentials":
            return httpx.Response(200, json={"access_token": "at0", "expires_in": 0, "refresh_token": "rt1"})
        if form["refresh_token"] not in valid_refresh:
            return httpx.Response(400, json={"error": "invalid_grant"})
        valid_refresh.discard(form["refresh_token"])
        valid_refresh.add("rt2")
        return httpx.Response(200, json={"access_token": "at1", "expires_in": 3600, "refresh_token": "rt2"})

    provider = _provider(handler)
    assert await provider.get_token() == "at0"  # expires_in=0: stale at once
    tokens = await asyncio.gather(*(provider.get_token() for _ in range(5)))
    assert tokens == ["at1"] * 5
    assert [f["grant_type"] for f in seen] == ["client_credentials", "refresh_token"]
    assert provider._refresh_token == "rt2"


async def test_a_failed_refresh_keeps_the_refresh_token_unless_mas_rejected_it() -> None:
    """A MAS 503 says nothing about the refresh token, so it must survive for the next try."""
    responses = iter(
        [
            httpx.Response(200, json={"access_token": "at0", "expires_in": 0, "refresh_token": "rt1"}),
            httpx.Response(503, text="restarting"),
            httpx.Response(200, json={"access_token": "at1", "expires_in": 3600}),
        ]
    )
    seen: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_form(request))
        return next(responses)

    provider = _provider(handler)
    await provider.get_token()
    with pytest.raises(OAuth2TokenError) as exc:
        await provider.get_token()
    assert exc.value.is_transient
    assert await provider.get_token() == "at1"
    assert seen[2] == {"grant_type": "refresh_token", "refresh_token": "rt1"}


async def test_unreachable_token_endpoint_is_a_transient_token_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider = _provider(handler)
    with pytest.raises(OAuth2TokenError) as exc:
        await provider.get_token()
    assert exc.value.status_code is None
    assert exc.value.is_transient


@pytest.mark.parametrize(
    ("status", "body", "transient"),
    [
        (503, {"error": "temporarily_unavailable"}, True),
        (500, "boom", True),
        (429, {"error": "slow_down"}, True),
        (401, {"error": "invalid_client"}, False),
        (400, {"error": "invalid_scope"}, False),
    ],
)
def test_token_error_classification(status: int, body: object, transient: bool) -> None:
    assert OAuth2TokenError(status, body).is_transient is transient


async def test_invalidate_drops_only_the_rejected_token() -> None:
    count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200, json={"access_token": f"at{count}", "expires_in": 3600})

    provider = _provider(handler)
    assert await provider.get_token() == "at1"
    assert await provider.invalidate("at1") is True
    assert await provider.get_token() == "at2"
    # A caller that still holds the old token must not throw away the fresh one.
    await provider.invalidate("at1")
    assert await provider.get_token() == "at2"
    assert count == 2
