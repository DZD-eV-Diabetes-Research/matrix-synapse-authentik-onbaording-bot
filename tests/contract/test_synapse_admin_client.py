"""Contract tests for the Synapse admin client (pagination, fixed bodies, blocking)."""

import json

import httpx
import pytest
import respx

from onbot.clients.base import ApiError
from onbot.clients.synapse_admin import ApiClientSynapseAdmin, RoomBlock, RoomDeletionFailed


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


ADMIN = "https://matrix.test/_synapse/admin"


def _deletion_done(delete_id: str = "d1") -> respx.Route:
    return respx.get(f"{ADMIN}/v2/rooms/delete_status/{delete_id}").mock(
        return_value=httpx.Response(200, json={"status": "complete"})
    )


@respx.mock
async def test_delete_room_sends_body() -> None:
    route = respx.delete(f"{ADMIN}/v2/rooms/!r:x").mock(
        return_value=httpx.Response(200, json={"delete_id": "d1"})
    )
    _deletion_done()
    client = _client()
    try:
        await client.delete_room("!r:x", purge=True, message="bye")
    finally:
        await client.aclose()
    body = json.loads(route.calls[0].request.content)
    assert body == {"block": False, "purge": True, "force_purge": False, "message": "bye"}


# --- delete_room: the asynchronous v2 API (wave 2 review) ---------------------------------------------


def _waiting_client() -> tuple[ApiClientSynapseAdmin, list[float]]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return ApiClientSynapseAdmin(server_url="https://matrix.test", access_token="adm", sleep=sleep), waits


@respx.mock
async def test_delete_room_waits_for_the_background_deletion() -> None:
    respx.delete(f"{ADMIN}/v2/rooms/!r:x").mock(return_value=httpx.Response(200, json={"delete_id": "d1"}))
    status = respx.get(f"{ADMIN}/v2/rooms/delete_status/d1").mock(
        side_effect=[
            httpx.Response(200, json={"status": "scheduled"}),
            httpx.Response(200, json={"status": "active"}),
            httpx.Response(200, json={"status": "complete", "shutdown_room": {"kicked_users": []}}),
        ]
    )
    client, waits = _waiting_client()
    try:
        result = await client.delete_room("!r:x", poll_interval_sec=2)
    finally:
        await client.aclose()
    assert result["status"] == "complete"
    assert status.call_count == 3
    assert waits == [2, 2]


@respx.mock
async def test_a_timed_out_delete_waits_for_the_deletion_it_started_instead_of_failing() -> None:
    """v1 ran the purge inside the request: a slow purge timed out and the retry reported a failure."""
    delete = respx.delete(f"{ADMIN}/v2/rooms/!r:x").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(f"{ADMIN}/v2/rooms/!r:x/delete_status").mock(
        return_value=httpx.Response(200, json={"results": [{"delete_id": "d7", "status": "active"}]})
    )
    _deletion_done("d7")
    client, _ = _waiting_client()
    try:
        result = await client.delete_room("!r:x")
    finally:
        await client.aclose()
    assert result["status"] == "complete"
    assert delete.call_count == 1  # never sent twice


@respx.mock
async def test_a_deletion_already_running_is_awaited() -> None:
    respx.delete(f"{ADMIN}/v2/rooms/!r:x").mock(
        return_value=httpx.Response(400, json={"errcode": "M_UNKNOWN", "error": "Purge already in progress"})
    )
    respx.get(f"{ADMIN}/v2/rooms/!r:x/delete_status").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"delete_id": "old", "status": "failed"},
                    {"delete_id": "d2", "status": "scheduled"},
                ]
            },
        )
    )
    _deletion_done("d2")
    client, _ = _waiting_client()
    try:
        assert (await client.delete_room("!r:x"))["status"] == "complete"
    finally:
        await client.aclose()


@respx.mock
async def test_a_refused_delete_without_a_deletion_under_way_raises() -> None:
    respx.delete(f"{ADMIN}/v2/rooms/!r:x").mock(
        return_value=httpx.Response(400, json={"errcode": "M_INVALID_PARAM", "error": "bad room"})
    )
    respx.get(f"{ADMIN}/v2/rooms/!r:x/delete_status").mock(
        return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
    )
    client, _ = _waiting_client()
    try:
        with pytest.raises(ApiError) as caught:
            await client.delete_room("!r:x")
    finally:
        await client.aclose()
    assert caught.value.status_code == 400


@respx.mock
async def test_a_failed_deletion_raises() -> None:
    respx.delete(f"{ADMIN}/v2/rooms/!r:x").mock(return_value=httpx.Response(200, json={"delete_id": "d1"}))
    respx.get(f"{ADMIN}/v2/rooms/delete_status/d1").mock(
        return_value=httpx.Response(200, json={"status": "failed", "error": "database is gone"})
    )
    client, _ = _waiting_client()
    try:
        with pytest.raises(RoomDeletionFailed, match="database is gone"):
            await client.delete_room("!r:x")
    finally:
        await client.aclose()


@respx.mock
async def test_a_deletion_still_running_after_the_wait_is_left_to_synapse() -> None:
    respx.delete(f"{ADMIN}/v2/rooms/!r:x").mock(return_value=httpx.Response(200, json={"delete_id": "d1"}))
    respx.get(f"{ADMIN}/v2/rooms/delete_status/d1").mock(
        return_value=httpx.Response(200, json={"status": "active"})
    )
    client, _ = _waiting_client()
    try:
        result = await client.delete_room("!r:x", wait_sec=0)
    finally:
        await client.aclose()
    assert result["status"] == "active"


@respx.mock
async def test_get_room_block_says_who_blocked_the_room() -> None:
    respx.get("https://matrix.test/_synapse/admin/v1/rooms/!r:x/block").mock(
        side_effect=[
            httpx.Response(200, json={"block": True, "user_id": "@admin:x"}),
            httpx.Response(200, json={"block": False}),
        ]
    )
    client = _client()
    try:
        assert await client.get_room_block("!r:x") == RoomBlock(blocked=True, by="@admin:x")
        assert await client.get_room_block("!r:x") == RoomBlock(blocked=False)
    finally:
        await client.aclose()


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
    route = respx.delete(f"{ADMIN}/v2/rooms/!r:x").mock(
        return_value=httpx.Response(200, json={"delete_id": "d1"})
    )
    _deletion_done()
    client = _client()
    try:
        await client.delete_room("!r:x", block=True)
    finally:
        await client.aclose()
    body = json.loads(route.calls[0].request.content)
    assert "message" not in body
    assert body == {"block": True, "purge": True, "force_purge": False}


@respx.mock
async def test_media_is_deleted_one_limited_page_at_a_time_and_counted() -> None:
    """LIF-12: Synapse deletes one page per request; the caller loops and checks what is left."""
    delete = respx.delete("https://matrix.test/_synapse/admin/v1/users/@u:x/media").mock(
        return_value=httpx.Response(200, json={"deleted_media": ["m"] * 100, "total": 100})
    )
    listing = respx.get("https://matrix.test/_synapse/admin/v1/users/@u:x/media").mock(
        return_value=httpx.Response(200, json={"media": [{"media_id": "m"}], "total": 42})
    )
    client = _client()
    try:
        result = await client.delete_user_media("@u:x", limit=100)
        remaining = await client.count_user_media("@u:x")
    finally:
        await client.aclose()
    assert result["total"] == 100
    assert dict(delete.calls[0].request.url.params) == {"limit": "100"}
    assert remaining == 42
    assert dict(listing.calls[0].request.url.params) == {"limit": "1"}


@respx.mock
async def test_logout_account_returns_the_number_of_devices() -> None:
    respx.get("https://matrix.test/_synapse/admin/v2/users/@u:x/devices").mock(
        return_value=httpx.Response(200, json={"devices": [{"device_id": "D1"}]})
    )
    respx.delete("https://matrix.test/_synapse/admin/v2/users/@u:x/devices/D1").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _client()
    try:
        assert await client.logout_account("@u:x") == 1
    finally:
        await client.aclose()
