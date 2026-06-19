"""Contract tests for the Synapse admin client (pagination, fixed bodies, blocking)."""

import json

import httpx
import respx

from onbot.clients.synapse_admin import ApiClientSynapseAdmin


def _client() -> ApiClientSynapseAdmin:
    return ApiClientSynapseAdmin(server_url="https://matrix.test", access_token="adm")


@respx.mock
async def test_list_users_follows_next_token() -> None:
    route = respx.get("https://matrix.test/_synapse/admin/v2/users").mock(
        side_effect=[
            httpx.Response(200, json={"users": [{"name": "@a:x"}], "next_token": "100"}),
            httpx.Response(200, json={"users": [{"name": "@b:x"}]}),
        ]
    )
    client = _client()
    try:
        users = await client.list_users()
    finally:
        await client.aclose()
    assert [u["name"] for u in users] == ["@a:x", "@b:x"]
    assert route.call_count == 2
    assert route.calls[1].request.url.params.get("from") == "100"


@respx.mock
async def test_list_rooms_follows_next_batch_and_excludes_spaces() -> None:
    respx.get("https://matrix.test/_synapse/admin/v1/rooms").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "rooms": [
                        {"room_id": "!r1:x", "room_type": None},
                        {"room_id": "!s1:x", "room_type": "m.space"},
                    ],
                    "next_batch": "1",
                },
            ),
            httpx.Response(200, json={"rooms": [{"room_id": "!r2:x", "room_type": None}]}),
        ]
    )
    client = _client()
    try:
        rooms = await client.list_non_space_rooms()
    finally:
        await client.aclose()
    assert [r["room_id"] for r in rooms] == ["!r1:x", "!r2:x"]


@respx.mock
async def test_delete_room_sends_body() -> None:
    route = respx.delete("https://matrix.test/_synapse/admin/v1/rooms/!r:x").mock(
        return_value=httpx.Response(200, json={"delete_id": "d1"})
    )
    client = _client()
    try:
        await client.delete_room("!r:x", purge=True, message="bye")
    finally:
        await client.aclose()
    body = json.loads(route.calls[0].request.content)
    assert body == {"block": False, "purge": True, "force_purge": False, "message": "bye"}


@respx.mock
async def test_room_is_blocked_returns_bool() -> None:
    respx.get("https://matrix.test/_synapse/admin/v1/rooms/!r:x/block").mock(
        return_value=httpx.Response(200, json={"block": True})
    )
    client = _client()
    try:
        assert await client.room_is_blocked("!r:x") is True
    finally:
        await client.aclose()


@respx.mock
async def test_make_room_admin_hits_correct_endpoint() -> None:
    route = respx.post("https://matrix.test/_synapse/admin/v1/rooms/!r:x/make_room_admin").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _client()
    try:
        await client.make_room_admin("!r:x", "@u:x")
    finally:
        await client.aclose()
    assert json.loads(route.calls[0].request.content) == {"user_id": "@u:x"}
