"""Contract tests for the async HTTP base client (auth, retries, errors, params)."""

import httpx
import pytest
import respx

from onbot.auth.token_provider import StaticTokenProvider, TokenProvider
from onbot.clients.base import ApiError, BaseApiClient


class _RotatingProvider:
    """A TokenProvider whose token changes per call, to prove auth is resolved per request."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_token(self) -> str:
        self.calls += 1
        return f"tok{self.calls}"

    async def aclose(self) -> None:
        return None


def test_requires_a_token_source() -> None:
    with pytest.raises(ValueError):
        BaseApiClient("https://api.test")


@respx.mock
async def test_injects_bearer_and_drops_none_params() -> None:
    route = respx.get("https://api.test/thing").mock(return_value=httpx.Response(200, json={"ok": 1}))
    client = BaseApiClient("https://api.test", "secret-token")
    try:
        result = await client.get_json("thing", params={"a": None, "b": 2})
    finally:
        await client.aclose()

    assert result == {"ok": 1}
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer secret-token"
    assert request.url.params.get("b") == "2"
    assert "a" not in request.url.params


@respx.mock
async def test_retries_transient_then_succeeds() -> None:
    route = respx.get("https://api.test/thing").mock(
        side_effect=[
            httpx.Response(503, json={"err": "busy"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = BaseApiClient("https://api.test", "t")
    try:
        result = await client.get_json("thing")
    finally:
        await client.aclose()
    assert result == {"ok": True}
    assert route.call_count == 2


@respx.mock
async def test_non_retryable_error_raises_apierror() -> None:
    respx.get("https://api.test/thing").mock(return_value=httpx.Response(400, json={"e": "bad"}))
    client = BaseApiClient("https://api.test", "t")
    try:
        with pytest.raises(ApiError) as exc:
            await client.get_json("thing")
    finally:
        await client.aclose()
    assert exc.value.status_code == 400
    assert exc.value.payload == {"e": "bad"}


@respx.mock
async def test_token_provider_resolves_auth_per_request() -> None:
    route = respx.get("https://api.test/thing").mock(return_value=httpx.Response(200, json={"ok": 1}))
    provider: TokenProvider = _RotatingProvider()
    client = BaseApiClient("https://api.test", token_provider=provider)
    try:
        await client.get_json("thing")
        await client.get_json("thing")
    finally:
        await client.aclose()
    # Each request resolves a fresh token (the provider rotated it).
    assert route.calls[0].request.headers["authorization"] == "Bearer tok1"
    assert route.calls[1].request.headers["authorization"] == "Bearer tok2"


@respx.mock
async def test_request_raw_returns_bytes_and_sends_content() -> None:
    route = respx.post("https://api.test/upload").mock(return_value=httpx.Response(200, content=b"pong"))
    client = BaseApiClient("https://api.test", token_provider=StaticTokenProvider("t"))
    try:
        data = await client.request_raw(
            "POST",
            "upload",
            content=b"ping",
            headers={"Content-Type": "application/octet-stream"},
            parse_json=False,
        )
    finally:
        await client.aclose()
    assert data == b"pong"
    assert route.calls[0].request.content == b"ping"
    assert route.calls[0].request.headers["content-type"] == "application/octet-stream"
