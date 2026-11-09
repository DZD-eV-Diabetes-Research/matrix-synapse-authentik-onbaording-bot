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
from onbot.rooms.provision import ManagedRoomSpec
from onbot.state import GroupRoomState


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
        await client.create_room(
            alias_localpart="onbot", room_params={"creation_content": {"type": "m.space"}}
        )
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
async def test_create_room_preserves_caller_supplied_initial_state() -> None:
    """A caller's initial_state event survives alongside the bot's own (regression: it was clobbered).

    The bot's events (space parent, encryption) come first; the caller's come last, so a same-typed
    event supplied by the caller wins.
    """
    create = respx.post("https://matrix.test/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(200, json={"room_id": "!new:matrix.test"})
    )
    respx.put(url__regex=r".*/state/m\.space\.child/.+").mock(
        return_value=httpx.Response(200, json={"event_id": "$e"})
    )
    client = _client()
    join_rule = {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "restricted"}}
    try:
        await client.create_room(
            alias_localpart="team",
            encrypted=True,
            parent_space_id="!space:matrix.test",
            room_params={"preset": "public_chat", "initial_state": [join_rule]},
        )
    finally:
        await client.aclose()

    body = json.loads(create.calls[0].request.content)
    assert body["preset"] == "public_chat"
    initial_state = body["initial_state"]
    types = [ev["type"] for ev in initial_state]
    # The bot's own events survive AND the caller's join-rule event is kept, appended last.
    assert "m.room.encryption" in types
    assert "m.space.parent" in types
    assert join_rule in initial_state
    assert initial_state[-1] == join_rule


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
async def test_get_room_state_event_with_sender_asks_for_the_whole_event() -> None:
    route = respx.get("https://matrix.test/_matrix/client/v3/rooms/!r:x/state/m.room.join_rules").mock(
        return_value=httpx.Response(
            200, json={"sender": "@mod:x", "event_id": "$e", "content": {"join_rule": "invite"}}
        )
    )
    client = _client()
    try:
        event = await client.get_room_state_event_with_sender("!r:x", "m.room.join_rules")
    finally:
        await client.aclose()
    assert event is not None and event["sender"] == "@mod:x"
    assert route.calls[0].request.url.params.get("format") == "event"


@respx.mock
async def test_get_room_state_event_with_sender_returns_none_on_404() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/rooms/!s:x/state/m.space.child/!r:x").mock(
        return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
    )
    client = _client()
    try:
        assert await client.get_room_state_event_with_sender("!s:x", "m.space.child", "!r:x") is None
    finally:
        await client.aclose()


@respx.mock
async def test_set_room_alias_puts_the_directory_entry() -> None:
    route = respx.put(url__regex=r".*/v3/directory/room/%23team%3Amatrix\.test$").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _client()
    try:
        await client.set_room_alias("#team:matrix.test", "!r:x")
    finally:
        await client.aclose()
    assert json.loads(route.calls[0].request.content) == {"room_id": "!r:x"}


@respx.mock
async def test_send_text_message_uses_unique_txn() -> None:
    route = respx.put(url__regex=r".*/v3/rooms/%21r%3Ax/send/m\.room\.message/.+").mock(
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
    assert versions.versions == ["v1.11"]


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
async def test_get_user_avatar_reads_profile() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/profile/@bot:matrix.test/avatar_url").mock(
        return_value=httpx.Response(200, json={"avatar_url": "mxc://matrix.test/abc"})
    )
    client = _client()
    try:
        assert await client.get_user_avatar("@bot:matrix.test") == "mxc://matrix.test/abc"
    finally:
        await client.aclose()


@respx.mock
async def test_get_user_avatar_is_none_when_unset() -> None:
    """Synapse answers an avatar-less profile as 404 or as 200 without the key; both mean "none"."""
    route = respx.get("https://matrix.test/_matrix/client/v3/profile/@bot:matrix.test/avatar_url")
    route.side_effect = [httpx.Response(404, json={"errcode": "M_NOT_FOUND"}), httpx.Response(200, json={})]
    client = _client()
    try:
        assert await client.get_user_avatar("@bot:matrix.test") is None
        assert await client.get_user_avatar("@bot:matrix.test") is None
    finally:
        await client.aclose()


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
async def test_room_account_data_round_trip() -> None:
    url = "https://matrix.test/_matrix/client/v3/user/@bot:x/rooms/!r:x/account_data/x.onbot.granted"
    put = respx.put(url).mock(return_value=httpx.Response(200, json={}))
    respx.get(url).mock(return_value=httpx.Response(200, json={"users": {"@a:x": 50}}))
    client = _client()
    try:
        await client.set_room_account_data("@bot:x", "!r:x", "x.onbot.granted", {"users": {"@a:x": 50}})
        assert await client.get_room_account_data("@bot:x", "!r:x", "x.onbot.granted") == {
            "users": {"@a:x": 50}
        }
    finally:
        await client.aclose()
    assert json.loads(put.calls[0].request.content) == {"users": {"@a:x": 50}}


@respx.mock
async def test_room_account_data_missing_is_empty() -> None:
    respx.get("https://matrix.test/_matrix/client/v3/user/@bot:x/rooms/!r:x/account_data/t").mock(
        return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
    )
    client = _client()
    try:
        assert await client.get_room_account_data("@bot:x", "!r:x", "t") == {}
    finally:
        await client.aclose()


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

    server_name = "matrix.test"

    async def get_room_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, object]:
        self.calls.append(("get_room_state_event", (room_id, event_type), {}))
        return {"room_version": "12"}

    async def resolve_room_alias(self, alias: str) -> str | None:
        self.calls.append(("resolve_room_alias", (alias,), {}))
        return "!r:matrix.test"

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

    async def put_room_state_event(
        self, room_id: str, event_type: str, content: dict[str, object], state_key: str = ""
    ) -> None:
        self.calls.append(("put_room_state_event", (room_id, event_type, content, state_key), {}))

    async def get_room_state_event_with_sender(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, object]:
        self.calls.append(("get_room_state_event_with_sender", (room_id, event_type, state_key), {}))
        return {"sender": "@bot:matrix.test", "content": {}}

    async def set_room_alias(self, alias: str, room_id: str) -> None:
        self.calls.append(("set_room_alias", (alias, room_id), {}))


async def test_cs_api_effectors_delegate_to_client() -> None:
    client = _RecordingMatrixClient()
    effectors = CSApiEffectors(client, bot_id="@bot:matrix.test")  # type: ignore[arg-type]

    spec = ManagedRoomSpec(
        marker=GroupRoomState(group_id="g1"), alias_localpart="team", name="Team", parent_space_id="!s:x"
    )
    assert await effectors.create_managed_room(spec) == "!room:matrix.test"
    assert await effectors.resolve_room_alias("#team:matrix.test") == "!r:matrix.test"
    await effectors.kick_user("!r:x", "@u:x", "gone")
    await effectors.set_room_name("!r:x", "Team")
    await effectors.set_room_topic("!r:x", "topic")
    await effectors.put_room_state(["!r:x"][0], "org.onbot.state", {"v": 1})
    await effectors.put_room_state("!s:x", "m.space.child", {"via": ["x"]}, state_key="!r:x")
    await effectors.get_room_state_event("!r:x", "m.room.join_rules")
    await effectors.set_room_alias("#team:matrix.test", "!r:x")

    names = [c[0] for c in client.calls]
    assert names == [
        # createRoom, then the marker lock: version, power levels, write.
        "create_room",
        "get_room_state_event",
        "get_room_power_levels",
        "set_room_power_levels",
        "resolve_room_alias",
        "kick_user",
        "set_room_name",
        "set_room_topic",
        "put_room_state_event",
        "put_room_state_event",
        "get_room_state_event_with_sender",
        "set_room_alias",
    ]
    assert client.calls[-3][1] == ("!s:x", "m.space.child", {"via": ["x"]}, "!r:x")
    create_kwargs = client.calls[0][2]
    assert create_kwargs["alias_localpart"] == "team"
    assert create_kwargs["parent_space_id"] == "!s:x"


async def test_cs_api_effectors_refuse_to_create_without_the_bot_id() -> None:
    effectors = CSApiEffectors(_RecordingMatrixClient())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="bot_id"):
        await effectors.create_managed_room(ManagedRoomSpec(marker=GroupRoomState(group_id="g1")))


# --- auth issuer (LIF-09) ------------------------------------------------------------------------

_UNRECOGNIZED = httpx.Response(404, json={"errcode": "M_UNRECOGNIZED", "error": "Unrecognized request"})


@pytest.mark.parametrize(
    ("stable", "unstable", "expected"),
    [
        (
            httpx.Response(200, json={"issuer": "https://auth.test/", "token_endpoint": "x"}),
            None,
            "https://auth.test/",
        ),
        (_UNRECOGNIZED, httpx.Response(200, json={"issuer": "https://old.test/"}), "https://old.test/"),
        (_UNRECOGNIZED, _UNRECOGNIZED, None),
    ],
)
@respx.mock
async def test_auth_issuer_tells_a_mas_backed_homeserver(
    stable: httpx.Response, unstable: httpx.Response | None, expected: str | None
) -> None:
    respx.get("https://matrix.test/_matrix/client/v1/auth_metadata").mock(return_value=stable)
    if unstable is not None:
        respx.get("https://matrix.test/_matrix/client/unstable/org.matrix.msc2965/auth_issuer").mock(
            return_value=unstable
        )
    client = _client()
    try:
        assert await client.get_auth_issuer() == expected
    finally:
        await client.aclose()


@respx.mock
async def test_auth_issuer_does_not_hide_a_server_error() -> None:
    respx.get("https://matrix.test/_matrix/client/v1/auth_metadata").mock(return_value=httpx.Response(502))
    client = ApiClientMatrix(
        server_url="https://matrix.test", access_token="tok", server_name="matrix.test", max_retry_attempts=1
    )
    try:
        with pytest.raises(ApiError):
            await client.get_auth_issuer()
    finally:
        await client.aclose()
