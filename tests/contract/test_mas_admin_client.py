"""Contract tests for the MAS admin client (paths, the not-found contract, LIF-10, MNT-05)."""

from __future__ import annotations

import httpx
import pytest
import respx

from onbot.clients.base import ApiError
from onbot.clients.mas_admin import ApiClientMasAdmin, mxid_localpart

BASE = "https://mas.test/api/admin/v1"


def _client() -> ApiClientMasAdmin:
    return ApiClientMasAdmin(mas_url="https://mas.test/", access_token="adm", max_retry_attempts=1)


def test_mxid_localpart() -> None:
    assert mxid_localpart("@alice:company.org") == "alice"


@respx.mock
async def test_lookup_returns_the_ulid() -> None:
    respx.get(f"{BASE}/users/by-username/alice").mock(
        return_value=httpx.Response(200, json={"data": {"type": "user", "id": "01ULID", "attributes": {}}})
    )
    client = _client()
    try:
        assert await client.get_user_id_by_username("alice") == "01ULID"
    finally:
        await client.aclose()


@respx.mock
async def test_lookup_of_an_unknown_user_returns_none() -> None:
    respx.get(f"{BASE}/users/by-username/ghost").mock(
        return_value=httpx.Response(404, json={"errors": [{"title": 'User with username "ghost" not found'}]})
    )
    client = _client()
    try:
        assert await client.get_user_id_by_username("ghost") is None
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),  # the listener does not mount the adminapi resource
        httpx.Response(404, text="<html>Not Found</html>"),  # a reverse proxy does not route it
        httpx.Response(404, json={"detail": "not found"}),  # some other JSON, not a MAS error document
    ],
)
@respx.mock
async def test_a_route_404_is_an_error_not_a_missing_user(response: httpx.Response) -> None:
    respx.get(f"{BASE}/users/by-username/alice").mock(return_value=response)
    client = _client()
    try:
        with pytest.raises(ApiError) as exc_info:
            await client.get_user_id_by_username("alice")
    finally:
        await client.aclose()
    assert exc_info.value.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [("lock_user", "lock"), ("unlock_user", "unlock"), ("deactivate_user", "deactivate")],
)
@respx.mock
async def test_user_operations_post_to_the_documented_paths(method: str, path: str) -> None:
    route = respx.post(f"{BASE}/users/01ULID/{path}").mock(
        return_value=httpx.Response(200, json={"data": {"type": "user", "id": "01ULID"}})
    )
    client = _client()
    try:
        await getattr(client, method)("01ULID")
    finally:
        await client.aclose()
    assert route.called
    assert route.calls[0].request.headers["Authorization"] == "Bearer adm"


@respx.mock
async def test_operation_failure_raises() -> None:
    respx.post(f"{BASE}/users/01ULID/lock").mock(return_value=httpx.Response(403, json={"errors": []}))
    client = _client()
    try:
        with pytest.raises(ApiError):
            await client.lock_user("01ULID")
    finally:
        await client.aclose()
