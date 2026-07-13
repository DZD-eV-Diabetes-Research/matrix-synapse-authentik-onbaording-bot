"""Contract tests for the Matrix CS client (room creation, state, messaging, account data, sync)."""

import json

import httpx
import pytest
import respx

from onbot.clients.base import ApiError
from onbot.clients.matrix import (
    ApiClientMatrix,
    CSApiEffectors,
    SyncNotSupportedError,
    _parse_mxc,
)
from onbot.models import RoomCreateAttributes


def _client() -> ApiClientMatrix:
    return ApiClientMatrix(server_url="https://matrix.test", access_token="tok", server_name="matrix.test")


def _client_pinned(room_version: str) -> ApiClientMatrix:
    return ApiClientMatrix(
        server_url="https://matrix.test",
        access_token="tok",
        server_name="matrix.test",
        room_version=room_version,
    )


@respx.mock
async def test_create_room_omits_room_version_when_unset() -> None:
    """Default: no room_version is sent, so new rooms inherit the server's default (spec: v12)."""
    route = respx.post("https://matrix.test/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(200, json={"room_id": "!new:matrix.test"})
    )
    client = _client()
    try:
        await client.create_room(alias_localpart="team")
    finally:
        await client.aclose()
    assert "room_version" not in json.loads(route.calls[0].request.content)


@respx.mock
async def test_create_room_and_space_pin_the_configured_room_version() -> None:
    room = respx.post("https://matrix.test/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(200, json={"room_id": "!new:matrix.test"})
    )
    client = _client_pinned("12")
    try:
        await client.create_room(alias_localpart="team")
        await client.create_space(alias_localpart="onbot", name="OnBot", topic="s", params={})
    finally:
        await client.aclose()
    assert json.loads(room.calls[0].request.content)["room_version"] == "12"
    assert json.loads(room.calls[1].request.content)["room_version"] == "12"


@respx.mock
async def test_room_params_may_override_the_pinned_room_version() -> None:
    """A per-call room_version (e.g. a test forcing a version) wins over the server-wide default."""
    route = respx.post("https://matrix.test/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(200, json={"room_id": "!new:matrix.test"})
    )
    client = _client_pinned("12")
    try:
        await client.create_room(alias_localpart="team", room_params={"room_version": "11"})
    finally:
        await client.aclose()
    assert json.loads(route.calls[0].request.content)["room_version"] == "11"


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


@respx.mock
async def test_create_direct_message_room_is_direct_and_invites() -> None:
    route = respx.post("https://matrix.test/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(200, json={"room_id": "!dm:matrix.test"})
    )
    client = _client()
    try:
        room_id = await client.create_direct_message_room(
            "@u:matrix.test",
            name="Announcements",
            topic="read-only",
            power_level_content_override={"users_default": 0},
        )
    finally:
        await client.aclose()
    assert room_id == "!dm:matrix.test"
    body = json.loads(route.calls[0].request.content)
    assert body["is_direct"] is True
    # The invite stands even though the bot force-joins the user: it is the fallback when the
    # force-join is disabled or refused.
    assert body["invite"] == ["@u:matrix.test"]
    # NOT trusted_private_chat, which would hand the user power level 100 and make the room's
    # read-only power levels impossible to enforce, then and forever after.
    assert body["preset"] == "private_chat"
    assert body["power_level_content_override"] == {"users_default": 0}
    assert body["name"] == "Announcements"
    assert body["topic"] == "read-only"
    assert "initial_state" not in body


@respx.mock
async def test_resolve_room_alias_returns_room_id() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/directory/room/%23team%3Amatrix.test").mock(
        return_value=httpx.Response(200, json={"room_id": "!r:matrix.test"})
    )
    client = _client()
    try:
        assert await client.resolve_room_alias("#team:matrix.test") == "!r:matrix.test"
    finally:
        await client.aclose()


@respx.mock
async def test_resolve_room_alias_reraises_non_404() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/directory/room/%23team%3Amatrix.test").mock(
        return_value=httpx.Response(500, json={"errcode": "M_UNKNOWN"})
    )
    client = _client()
    try:
        with pytest.raises(ApiError):
            await client.resolve_room_alias("#team:matrix.test")
    finally:
        await client.aclose()


@respx.mock
async def test_kick_user_includes_reason() -> None:
    route = respx.post("https://matrix.test/_matrix/client/v3/rooms/!r:x/kick").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _client()
    try:
        await client.kick_user("!r:x", "@u:x", "left group")
    finally:
        await client.aclose()
    assert json.loads(route.calls[0].request.content) == {"user_id": "@u:x", "reason": "left group"}


@respx.mock
async def test_set_room_name_topic_and_avatar() -> None:
    name = respx.put("https://matrix.test/_matrix/client/v3/rooms/!r:x/state/m.room.name").mock(
        return_value=httpx.Response(200, json={"event_id": "$n"})
    )
    topic = respx.put("https://matrix.test/_matrix/client/v3/rooms/!r:x/state/m.room.topic").mock(
        return_value=httpx.Response(200, json={"event_id": "$t"})
    )
    avatar = respx.put("https://matrix.test/_matrix/client/v3/rooms/!r:x/state/m.room.avatar").mock(
        return_value=httpx.Response(200, json={"event_id": "$a"})
    )
    client = _client()
    try:
        await client.set_room_name("!r:x", "Team")
        await client.set_room_topic("!r:x", "topic")
        await client.set_room_avatar("!r:x", "mxc://matrix.test/abc")
    finally:
        await client.aclose()
    assert json.loads(name.calls[0].request.content) == {"name": "Team"}
    assert json.loads(topic.calls[0].request.content) == {"topic": "topic"}
    assert json.loads(avatar.calls[0].request.content) == {"url": "mxc://matrix.test/abc"}


@respx.mock
async def test_set_account_data_puts_content() -> None:
    route = respx.put("https://matrix.test/_matrix/client/v3/user/@bot:x/account_data/m.direct").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _client()
    try:
        await client.set_account_data("@bot:x", "m.direct", {"@u:x": ["!dm:x"]})
    finally:
        await client.aclose()
    assert json.loads(route.calls[0].request.content) == {"@u:x": ["!dm:x"]}


@respx.mock
async def test_get_account_data_reraises_non_404() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/user/@bot:x/account_data/m.direct").mock(
        return_value=httpx.Response(500, json={"errcode": "M_UNKNOWN"})
    )
    client = _client()
    try:
        with pytest.raises(ApiError):
            await client.get_account_data("@bot:x", "m.direct")
    finally:
        await client.aclose()


def test_parse_mxc_rejects_malformed() -> None:
    assert _parse_mxc("mxc://server/media") == ("server", "media")
    with pytest.raises(ValueError):
        _parse_mxc("https://not-mxc/x")
    with pytest.raises(ValueError):
        _parse_mxc("mxc://server-only")


class _RecordingMatrixClient:
    """Records the CS-API calls the effectors delegate, without any HTTP."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def create_room(self, **kwargs: object) -> str:
        self.calls.append(("create_room", (), kwargs))
        return "!room:matrix.test"

    async def create_space(self, **kwargs: object) -> str:
        self.calls.append(("create_space", (), kwargs))
        return "!space:matrix.test"

    async def kick_user(self, *args: object) -> None:
        self.calls.append(("kick_user", args, {}))

    async def get_room_power_levels(self, room_id: str) -> dict[str, object]:
        self.calls.append(("get_room_power_levels", (room_id,), {}))
        return {"users": {}}

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, object]) -> None:
        self.calls.append(("set_room_power_levels", (room_id, power_levels), {}))

    async def set_room_name(self, room_id: str, name: str) -> None:
        self.calls.append(("set_room_name", (room_id, name), {}))

    async def set_room_topic(self, room_id: str, topic: str) -> None:
        self.calls.append(("set_room_topic", (room_id, topic), {}))

    async def put_room_state_event(self, room_id: str, event_type: str, content: dict[str, object]) -> None:
        self.calls.append(("put_room_state_event", (room_id, event_type, content), {}))


async def test_cs_api_effectors_delegate_to_client() -> None:
    client = _RecordingMatrixClient()
    effectors = CSApiEffectors(client)  # type: ignore[arg-type]

    attrs = RoomCreateAttributes(
        alias="team", canonical_alias="#team:matrix.test", name="Team", topic="t", encrypted=True
    )
    assert await effectors.create_group_room(attrs, "!space:matrix.test") == "!room:matrix.test"
    assert (
        await effectors.create_space(
            alias="onbot", name="OnBot", topic="space", params={"visibility": "private"}
        )
        == "!space:matrix.test"
    )
    await effectors.kick_user("!r:x", "@u:x", "gone")
    assert await effectors.get_room_power_levels("!r:x") == {"users": {}}
    await effectors.set_room_power_levels("!r:x", {"users": {"@a:x": 50}})
    await effectors.set_room_name("!r:x", "Team")
    await effectors.set_room_topic("!r:x", "topic")
    await effectors.put_room_state(["!r:x"][0], "org.onbot.state", {"v": 1})

    names = [c[0] for c in client.calls]
    assert names == [
        "create_room",
        "create_space",
        "kick_user",
        "get_room_power_levels",
        "set_room_power_levels",
        "set_room_name",
        "set_room_topic",
        "put_room_state_event",
    ]
    # create_group_room forwards the alias and parent space verbatim.
    create_kwargs = client.calls[0][2]
    assert create_kwargs["alias_localpart"] == "team"
    assert create_kwargs["parent_space_id"] == "!space:matrix.test"
