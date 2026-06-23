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
    # guests=false is required under MAS/MSC3861 (Synapse rejects the default otherwise).
    assert route.calls[0].request.url.params.get("guests") == "false"


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


@respx.mock
async def test_list_spaces_keeps_only_space_rooms() -> None:
    respx.get("https://matrix.test/_synapse/admin/v1/rooms").mock(
        return_value=httpx.Response(
            200,
            json={
                "rooms": [
                    {"room_id": "!r:x", "room_type": None},
                    {"room_id": "!s:x", "room_type": "m.space"},
                ]
            },
        )
    )
    client = _client()
    try:
        spaces = await client.list_spaces()
    finally:
        await client.aclose()
    assert [r["room_id"] for r in spaces] == ["!s:x"]


@respx.mock
async def test_list_room_members_and_details() -> None:
    respx.get("https://matrix.test/_synapse/admin/v1/rooms/!r:x/members").mock(
        return_value=httpx.Response(200, json={"members": ["@a:x", "@b:x"]})
    )
    respx.get("https://matrix.test/_synapse/admin/v1/rooms/!r:x").mock(
        return_value=httpx.Response(200, json={"room_id": "!r:x", "name": "Room"})
    )
    client = _client()
    try:
        members = await client.list_room_members("!r:x")
        details = await client.get_room_details("!r:x")
    finally:
        await client.aclose()
    assert members == ["@a:x", "@b:x"]
    assert details["name"] == "Room"


@respx.mock
async def test_list_user_media_paginates() -> None:
    route = respx.get("https://matrix.test/_synapse/admin/v1/users/@u:x/media").mock(
        side_effect=[
            httpx.Response(200, json={"media": [{"media_id": "m1"}], "next_token": "1"}),
            httpx.Response(200, json={"media": [{"media_id": "m2"}]}),
        ]
    )
    client = _client()
    try:
        media = await client.list_user_media("@u:x")
    finally:
        await client.aclose()
    assert [m["media_id"] for m in media] == ["m1", "m2"]
    assert route.calls[1].request.url.params.get("from") == "1"


@respx.mock
async def test_add_user_to_room_posts_user_id() -> None:
    route = respx.post("https://matrix.test/_synapse/admin/v1/join/!r:x").mock(
        return_value=httpx.Response(200, json={"room_id": "!r:x"})
    )
    client = _client()
    try:
        await client.add_user_to_room("!r:x", "@u:x")
    finally:
        await client.aclose()
    assert json.loads(route.calls[0].request.content) == {"user_id": "@u:x"}


@respx.mock
async def test_set_user_server_admin_state_and_room_block() -> None:
    admin = respx.put("https://matrix.test/_synapse/admin/v1/users/@u:x/admin").mock(
        return_value=httpx.Response(200, json={})
    )
    block = respx.put("https://matrix.test/_synapse/admin/v1/rooms/!r:x/block").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _client()
    try:
        await client.set_user_server_admin_state("@u:x", admin=True)
        await client.room_set_blocked("!r:x", blocked=True)
    finally:
        await client.aclose()
    assert json.loads(admin.calls[0].request.content) == {"admin": True}
    assert json.loads(block.calls[0].request.content) == {"block": True}


@respx.mock
async def test_deactivate_and_delete_user_media() -> None:
    deact = respx.post("https://matrix.test/_synapse/admin/v1/deactivate/@u:x").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.delete("https://matrix.test/_synapse/admin/v1/users/@u:x/media").mock(
        return_value=httpx.Response(200, json={"deleted_media": ["m1"], "total": 1})
    )
    client = _client()
    try:
        await client.deactivate_account("@u:x", erase=True)
        result = await client.delete_user_media("@u:x")
    finally:
        await client.aclose()
    assert json.loads(deact.calls[0].request.content) == {"erase": True}
    assert result["total"] == 1


@respx.mock
async def test_logout_account_deletes_every_device() -> None:
    respx.get("https://matrix.test/_synapse/admin/v2/users/@u:x/devices").mock(
        return_value=httpx.Response(200, json={"devices": [{"device_id": "D1"}, {"device_id": "D2"}]})
    )
    d1 = respx.delete("https://matrix.test/_synapse/admin/v2/users/@u:x/devices/D1").mock(
        return_value=httpx.Response(200, json={})
    )
    d2 = respx.delete("https://matrix.test/_synapse/admin/v2/users/@u:x/devices/D2").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _client()
    try:
        await client.logout_account("@u:x")
    finally:
        await client.aclose()
    assert d1.called and d2.called


@respx.mock
async def test_delete_room_omits_message_when_absent() -> None:
    route = respx.delete("https://matrix.test/_synapse/admin/v1/rooms/!r:x").mock(
        return_value=httpx.Response(200, json={"delete_id": "d1"})
    )
    client = _client()
    try:
        await client.delete_room("!r:x", block=True)
    finally:
        await client.aclose()
    body = json.loads(route.calls[0].request.content)
    assert "message" not in body
    assert body == {"block": True, "purge": True, "force_purge": False}
