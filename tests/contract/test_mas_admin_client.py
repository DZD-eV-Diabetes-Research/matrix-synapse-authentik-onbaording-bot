"""Contract tests for the MAS admin client (paths, the not-found contract, LIF-10, MNT-05)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from onbot.clients.base import ApiError
from onbot.clients.mas_admin import SESSION_KINDS, ApiClientMasAdmin, mxid_localpart, parse_capabilities

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


# --- session finish and capabilities (LIF-07, D7) ------------------------------------------------

# The relevant part of ``/api/spec.json`` of MAS 0.14.1 (the integration stack, read live 2026-11-06):
# sessions can be listed but not finished, and deactivation takes no body.
SPEC_0_14 = {
    "paths": {
        "/api/admin/v1/compat-sessions": {"get": {}},
        "/api/admin/v1/oauth2-sessions": {"get": {}},
        "/api/admin/v1/user-sessions": {"get": {}},
        "/api/admin/v1/users/{id}/deactivate": {"post": {"parameters": []}},
        "/api/admin/v1/users/{id}/lock": {"post": {}},
    }
}
# The same part of a current MAS (upstream docs/api/spec.json): finish endpoints and ``skip_erase``.
SPEC_CURRENT = {
    "paths": {
        **{f"/api/admin/v1/{kind}/{{id}}/finish": {"post": {}} for kind in SESSION_KINDS},
        "/api/admin/v1/users/{id}/deactivate": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/DeactivateUserRequest"}}
                    }
                }
            }
        },
    },
    "components": {
        "schemas": {
            "DeactivateUserRequest": {
                "type": "object",
                "properties": {"skip_erase": {"type": "boolean", "default": False}},
            }
        }
    },
}


def test_capabilities_of_mas_0_14() -> None:
    caps = parse_capabilities(SPEC_0_14)
    assert caps.known
    assert caps.finishable_sessions == frozenset()
    assert caps.missing_finish == list(SESSION_KINDS)
    assert not caps.deactivate_skip_erase


def test_capabilities_of_a_current_mas() -> None:
    caps = parse_capabilities(SPEC_CURRENT)
    assert caps.finishable_sessions == frozenset(SESSION_KINDS)
    assert caps.deactivate_skip_erase
    assert "all session kinds" in caps.describe()


@pytest.mark.parametrize("spec", [None, [], {"openapi": "3.1.0"}])
def test_capabilities_of_an_unreadable_document(spec: object) -> None:
    caps = parse_capabilities(spec)
    assert not caps.known and not caps.deactivate_skip_erase and caps.finishable_sessions == frozenset()


@respx.mock
async def test_capabilities_are_read_once_and_a_failure_is_retried() -> None:
    route = respx.get("https://mas.test/api/spec.json").mock(
        side_effect=[httpx.Response(404), httpx.Response(200, json=SPEC_CURRENT)]
    )
    client = _client()
    try:
        assert not (await client.capabilities()).known
        assert (await client.capabilities()).deactivate_skip_erase
        assert (await client.capabilities()).deactivate_skip_erase
    finally:
        await client.aclose()
    assert route.call_count == 2


@respx.mock
async def test_active_sessions_are_listed_across_pages() -> None:
    first = [{"id": f"S{i:03d}"} for i in range(100)]
    route = respx.get(f"{BASE}/oauth2-sessions").mock(
        side_effect=[
            httpx.Response(200, json={"meta": {"count": 101}, "data": first}),
            httpx.Response(200, json={"meta": {"count": 101}, "data": [{"id": "S100"}]}),
        ]
    )
    client = _client()
    try:
        ids = await client.list_active_session_ids("oauth2-sessions", "01ULID")
    finally:
        await client.aclose()
    assert len(ids) == 101 and ids[-1] == "S100"
    params = [dict(call.request.url.params) for call in route.calls]
    assert params[0] == {"filter[user]": "01ULID", "filter[status]": "active", "page[first]": "100"}
    assert params[1]["page[after]"] == "S099"


@respx.mock
async def test_finish_session_posts_and_tolerates_an_already_finished_one() -> None:
    ok = respx.post(f"{BASE}/compat-sessions/S1/finish").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    respx.post(f"{BASE}/compat-sessions/S2/finish").mock(
        return_value=httpx.Response(400, json={"errors": [{"title": "Session is already finished"}]})
    )
    respx.post(f"{BASE}/compat-sessions/S3/finish").mock(
        return_value=httpx.Response(404, text="<!DOCTYPE html>")  # what MAS 0.14.1 answers: no such route
    )
    client = _client()
    try:
        assert await client.finish_session("compat-sessions", "S1") is True
        assert await client.finish_session("compat-sessions", "S2") is False
        with pytest.raises(ApiError):
            await client.finish_session("compat-sessions", "S3")
    finally:
        await client.aclose()
    assert ok.called


@pytest.mark.parametrize(
    ("skip_erase", "body"), [(None, b""), (True, {"skip_erase": True}), (False, {"skip_erase": False})]
)
@respx.mock
async def test_deactivate_sends_skip_erase_only_when_given(skip_erase: bool | None, body: object) -> None:
    route = respx.post(f"{BASE}/users/01ULID/deactivate").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    client = _client()
    try:
        await client.deactivate_user("01ULID", skip_erase=skip_erase)
    finally:
        await client.aclose()
    sent = route.calls[0].request.content
    assert (sent if body == b"" else json.loads(sent)) == body


@respx.mock
async def test_count_locked_users_reads_the_filtered_count() -> None:
    route = respx.get(f"{BASE}/users").mock(
        return_value=httpx.Response(200, json={"meta": {"count": 7}, "data": []})
    )
    client = _client()
    try:
        assert await client.count_locked_users() == 7
    finally:
        await client.aclose()
    assert dict(route.calls[0].request.url.params) == {"filter[status]": "locked", "page[first]": "1"}
