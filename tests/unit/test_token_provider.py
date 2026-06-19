"""Tests for the MAS-aware token providers (static + OAuth2 client-credentials/refresh)."""

from __future__ import annotations

import httpx
import pytest
import respx

from onbot.auth.token_provider import (
    OAuth2ClientCredentialsTokenProvider,
    OAuth2TokenError,
    StaticTokenProvider,
)

TOKEN_ENDPOINT = "https://auth.test/oauth2/token"


async def test_static_provider_returns_token() -> None:
    provider = StaticTokenProvider("syt_abc")
    assert await provider.get_token() == "syt_abc"
    await provider.aclose()


def test_static_provider_rejects_empty() -> None:
    with pytest.raises(ValueError):
        StaticTokenProvider("")


def _provider() -> OAuth2ClientCredentialsTokenProvider:
    return OAuth2ClientCredentialsTokenProvider(
        token_endpoint=TOKEN_ENDPOINT,
        client_id="bot",
        client_secret="s3cret",
        scope="urn:synapse:admin:*",
    )


@respx.mock
async def test_oauth2_client_credentials_grant_and_caches() -> None:
    route = respx.post(TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"access_token": "at1", "expires_in": 3600})
    )
    provider = _provider()
    try:
        assert await provider.get_token() == "at1"
        # Cached: second call does not hit the endpoint again.
        assert await provider.get_token() == "at1"
    finally:
        await provider.aclose()

    assert route.call_count == 1
    req = route.calls[0].request
    assert b"grant_type=client_credentials" in req.content
    assert b"scope=" in req.content
    # Client auth is HTTP Basic (RFC 6749 §2.3.1).
    assert req.headers["authorization"].startswith("Basic ")


@respx.mock
async def test_oauth2_refreshes_with_refresh_token_when_expired() -> None:
    route = respx.post(TOKEN_ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "at1", "expires_in": 0, "refresh_token": "rt1"}),
            httpx.Response(200, json={"access_token": "at2", "expires_in": 3600}),
        ]
    )
    provider = _provider()
    try:
        assert await provider.get_token() == "at1"
        # expires_in=0 means the first token is already stale, so the next call refreshes.
        assert await provider.get_token() == "at2"
    finally:
        await provider.aclose()

    assert route.call_count == 2
    assert b"grant_type=refresh_token" in route.calls[1].request.content
    assert b"refresh_token=rt1" in route.calls[1].request.content


@respx.mock
async def test_oauth2_raises_on_rejection() -> None:
    respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    provider = _provider()
    try:
        with pytest.raises(OAuth2TokenError) as exc:
            await provider.get_token()
    finally:
        await provider.aclose()
    assert exc.value.status_code == 401


@respx.mock
async def test_oauth2_failed_refresh_falls_back_to_client_credentials() -> None:
    route = respx.post(TOKEN_ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "at1", "expires_in": 0, "refresh_token": "rt1"}),
            httpx.Response(400, json={"error": "invalid_grant"}),  # refresh rejected
            httpx.Response(200, json={"access_token": "at3", "expires_in": 3600}),
        ]
    )
    provider = _provider()
    try:
        assert await provider.get_token() == "at1"
        with pytest.raises(OAuth2TokenError):
            await provider.get_token()  # refresh fails, drops refresh token
        assert await provider.get_token() == "at3"  # falls back to client_credentials
    finally:
        await provider.aclose()

    assert b"grant_type=client_credentials" in route.calls[2].request.content
