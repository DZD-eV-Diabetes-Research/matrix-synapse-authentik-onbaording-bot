"""Contract tests for the Matrix CS client (room creation, state, messaging, account data, sync)."""

import json

import httpx
import pytest
import respx

from onbot.clients.matrix import ApiClientMatrix, SyncNotSupportedError


def _client() -> ApiClientMatrix:
    return ApiClientMatrix(server_url="https://matrix.test", access_token="tok", server_name="matrix.test")


@respx.mock
async def test_create_room_sets_encryption_and_space_parent() -> None:
    create = respx.post("https://matrix.test/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(200, json={"room_id": "!new:matrix.test"})
    )
    child = respx.put(
        "https://matrix.test/_matrix/client/v3/rooms/!space:matrix.test/state/m.space.child/!new:matrix.test"
    ).mock(return_value=httpx.Response(200, json={"event_id": "$e"}))
    client = _client()
    try:
        room_id = await client.create_room(
            alias_localpart="team",
            name="Team",
            topic="t",
            encrypted=True,
            parent_space_id="!space:matrix.test",
        )
    finally:
        await client.aclose()

    assert room_id == "!new:matrix.test"
    body = json.loads(create.calls[0].request.content)
    assert body["room_alias_name"] == "team"
    types = {ev["type"] for ev in body["initial_state"]}
    assert types == {"m.room.encryption", "m.space.parent"}
    assert child.called


@respx.mock
async def test_create_space_marks_creation_content() -> None:
    route = respx.post("https://matrix.test/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(200, json={"room_id": "!s:matrix.test"})
    )
    client = _client()
    try:
        space_id = await client.create_space(
            alias_localpart="onbot", name="OnBot", topic="space", params={"visibility": "private"}
        )
    finally:
        await client.aclose()
    assert space_id == "!s:matrix.test"
    body = json.loads(route.calls[0].request.content)
    assert body["creation_content"] == {"type": "m.space"}
    assert body["visibility"] == "private"


@respx.mock
async def test_get_room_state_event_returns_none_on_404() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/rooms/!r:x/state/m.room.power_levels").mock(
        return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
    )
    client = _client()
    try:
        assert await client.get_room_power_levels("!r:x") == {}
    finally:
        await client.aclose()


@respx.mock
async def test_send_text_message_uses_unique_txn() -> None:
    route = respx.put(url__regex=r".*/v3/rooms/!r:x/send/m\.room\.message/.+").mock(
        return_value=httpx.Response(200, json={"event_id": "$msg"})
    )
    client = _client()
    try:
        ev = await client.send_text_message("!r:x", "hello")
    finally:
        await client.aclose()
    assert ev == "$msg"
    assert json.loads(route.calls[0].request.content) == {"msgtype": "m.text", "body": "hello"}


@respx.mock
async def test_account_data_get_missing_is_empty() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/user/@bot:x/account_data/m.direct").mock(
        return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
    )
    client = _client()
    try:
        assert await client.get_account_data("@bot:x", "m.direct") == {}
    finally:
        await client.aclose()


@respx.mock
async def test_sliding_sync_normalises_rooms_and_pos() -> None:
    respx.post(url__regex=r".*/unstable/org\.matrix\.simplified_msc3575/sync.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "pos": "s2",
                "rooms": {
                    "!r:x": {
                        "timeline": [
                            {
                                "type": "m.room.member",
                                "state_key": "@a:x",
                                "content": {"membership": "join"},
                            }
                        ],
                        "required_state": [],
                    }
                },
            },
        )
    )
    client = _client()
    try:
        result = await client.sliding_sync(None)
    finally:
        await client.aclose()
    assert result.pos == "s2"
    assert len(result.rooms) == 1
    assert result.rooms[0].member_events()[0]["state_key"] == "@a:x"


@respx.mock
async def test_negotiate_versions_reports_capabilities() -> None:
    respx.get("https://matrix.test/_matrix/client/versions").mock(
        return_value=httpx.Response(
            200,
            json={
                "versions": ["v1.11"],
                "unstable_features": {"org.matrix.simplified_msc3575": True},
            },
        )
    )
    client = _client()
    try:
        versions = await client.negotiate_versions()
    finally:
        await client.aclose()
    assert versions.supports_simplified_sliding_sync()
    assert versions.supports_authenticated_media()
    assert client.versions is versions


@respx.mock
async def test_sliding_sync_raises_when_unsupported_after_negotiation() -> None:
    respx.get("https://matrix.test/_matrix/client/versions").mock(
        return_value=httpx.Response(200, json={"versions": ["v1.10"], "unstable_features": {}})
    )
    client = _client()
    try:
        await client.negotiate_versions()
        with pytest.raises(SyncNotSupportedError):
            await client.sliding_sync(None)
    finally:
        await client.aclose()


@respx.mock
async def test_upload_media_returns_content_uri() -> None:
    route = respx.post("https://matrix.test/_matrix/media/v3/upload").mock(
        return_value=httpx.Response(200, json={"content_uri": "mxc://matrix.test/abc"})
    )
    client = _client()
    try:
        mxc = await client.upload_media(b"\x89PNG", content_type="image/png", filename="a.png")
    finally:
        await client.aclose()
    assert mxc == "mxc://matrix.test/abc"
    req = route.calls[0].request
    assert req.headers["content-type"] == "image/png"
    assert req.url.params["filename"] == "a.png"
    assert req.content == b"\x89PNG"


@respx.mock
async def test_download_media_uses_authenticated_endpoint() -> None:
    route = respx.get("https://matrix.test/_matrix/client/v1/media/download/matrix.test/abc").mock(
        return_value=httpx.Response(200, content=b"bytes")
    )
    client = _client()
    try:
        data = await client.download_media("mxc://matrix.test/abc")
    finally:
        await client.aclose()
    assert data == b"bytes"
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"


@respx.mock
async def test_set_user_avatar_puts_profile() -> None:
    route = respx.put("https://matrix.test/_matrix/client/v3/profile/@bot:matrix.test/avatar_url").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _client()
    try:
        await client.set_user_avatar("@bot:matrix.test", "mxc://matrix.test/abc")
    finally:
        await client.aclose()
    assert json.loads(route.calls[0].request.content) == {"avatar_url": "mxc://matrix.test/abc"}


@respx.mock
async def test_resolve_room_alias_returns_none_on_404() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/directory/room/%23nope%3Amatrix.test").mock(
        return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
    )
    client = _client()
    try:
        assert await client.resolve_room_alias("#nope:matrix.test") is None
    finally:
        await client.aclose()


@respx.mock
async def test_link_room_to_space_writes_child_and_parent() -> None:
    child = respx.put(
        "https://matrix.test/_matrix/client/v3/rooms/!space:matrix.test/state/m.space.child/!r:matrix.test"
    ).mock(return_value=httpx.Response(200, json={"event_id": "$c"}))
    parent = respx.put(
        "https://matrix.test/_matrix/client/v3/rooms/!r:matrix.test/state/m.space.parent/!space:matrix.test"
    ).mock(return_value=httpx.Response(200, json={"event_id": "$p"}))
    client = _client()
    try:
        await client.link_room_to_space("!space:matrix.test", "!r:matrix.test")
    finally:
        await client.aclose()
    assert child.called and parent.called
