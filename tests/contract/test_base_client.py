"""Contract tests for the async HTTP base client (auth, retries, errors, params)."""

import httpx
import pytest
import respx

from onbot.clients.base import ApiError, BaseApiClient


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
