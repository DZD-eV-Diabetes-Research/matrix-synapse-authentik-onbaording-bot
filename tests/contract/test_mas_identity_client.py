"""Contract tests for the MAS listings the identity resolver reads (D8).

Shapes as MAS 1.28 answers them (checked live against the integration stack on 2026-11-06):
``GET /api/admin/v1/upstream-oauth-links`` with ``filter[provider]`` and cursor pages, and
``GET /api/admin/v1/users`` with every user, locked and deactivated ones included.
"""

from __future__ import annotations

from typing import Any

import httpx
import respx

from onbot.clients.mas_admin import ApiClientMasAdmin, UpstreamLink

BASE = "https://mas.test/api/admin/v1"


def _client() -> ApiClientMasAdmin:
    return ApiClientMasAdmin(mas_url="https://mas.test/", access_token="adm", max_retry_attempts=1)


def _link(i: int, *, user: str | None = "U") -> dict[str, Any]:
    return {
        "type": "upstream-oauth-link",
        "id": f"L{i:03d}",
        "attributes": {
            "provider_id": "P1",
            "subject": f"sub-{i}",
            "user_id": None if user is None else f"{user}{i}",
            "human_account_name": None,
        },
    }


@respx.mock
async def test_links_are_read_page_by_page_with_the_provider_filter() -> None:
    first = [_link(i) for i in range(100)]
    second = [_link(100), _link(101, user=None)]

    def answer(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("page[after]")
        data = first if after is None else second
        return httpx.Response(200, json={"meta": {"count": 102}, "data": data, "links": {}})

    route = respx.get(f"{BASE}/upstream-oauth-links").mock(side_effect=answer)
    client = _client()
    try:
        links = await client.list_upstream_links(provider_id="P1")
    finally:
        await client.aclose()
    assert len(links) == 102
    assert links[0] == UpstreamLink(provider_id="P1", subject="sub-0", user_id="U0")
    assert links[-1].user_id is None  # a link not attached to a user yet
    params = [dict(call.request.url.params) for call in route.calls]
    assert params[0] == {"page[first]": "100", "filter[provider]": "P1"}
    assert params[1]["page[after]"] == "L099"


@respx.mock
async def test_without_a_provider_every_link_is_read() -> None:
    route = respx.get(f"{BASE}/upstream-oauth-links").mock(
        return_value=httpx.Response(200, json={"meta": {"count": 1}, "data": [_link(1)], "links": {}})
    )
    client = _client()
    try:
        assert len(await client.list_upstream_links()) == 1
    finally:
        await client.aclose()
    assert "filter[provider]" not in route.calls[0].request.url.params


@respx.mock
async def test_usernames_map_every_mas_user_id_to_its_localpart() -> None:
    respx.get(f"{BASE}/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "meta": {"count": 2},
                "data": [
                    {"type": "user", "id": "U1", "attributes": {"username": "alice", "locked_at": None}},
                    {"type": "user", "id": "U2", "attributes": {"username": "bob", "locked_at": "2026"}},
                ],
                "links": {},
            },
        )
    )
    client = _client()
    try:
        assert await client.list_usernames() == {"U1": "alice", "U2": "bob"}
    finally:
        await client.aclose()
