"""The HTTP layer's retry, idempotency, wait and auth policy (RUN-05, RUN-06, RUN-10).

See the module docstring of :mod:`onbot.clients.base` for the policy these tests pin down.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from onbot.auth.token_provider import OAuth2ClientCredentialsTokenProvider
from onbot.clients.base import MAX_RETRY_AFTER_SEC, ApiError, BaseApiClient
from onbot.clients.matrix import CREATION_NONCE_KEY, ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin

API = "https://api.test"
HS = "https://matrix.test"
TOKEN_ENDPOINT = "https://mas.test/oauth2/token"


class _Sleeps:
    """Records the waits between attempts instead of sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _client(sleeps: _Sleeps | None = None, **kwargs: object) -> BaseApiClient:
    return BaseApiClient(API, "t", sleep=sleeps or _Sleeps(), **kwargs)  # type: ignore[arg-type]


def _matrix(sleeps: _Sleeps | None = None) -> ApiClientMatrix:
    return ApiClientMatrix(
        server_url=HS, access_token="tok", server_name="matrix.test", sleep=sleeps or _Sleeps()
    )


# --- which failures are retried -----------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, json={"errcode": "M_UNKNOWN"}),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(504, text="gateway timeout"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=["500", "502", "504", "read-timeout", "dropped-connection"],
)
@respx.mock
async def test_post_is_not_resent_after_a_failure_that_may_hide_a_success(
    failure: httpx.Response | Exception,
) -> None:
    """Before the fix every POST was resent on these, so a createRoom that had worked ran twice."""
    route = respx.post(f"{API}/thing").mock(side_effect=[failure, httpx.Response(200, json={})])
    client = _client()
    try:
        with pytest.raises((ApiError, httpx.TransportError)):
            await client.post_json("thing", json_body={})
    finally:
        await client.aclose()
    assert route.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429, json={"errcode": "M_LIMIT_EXCEEDED"}),
        httpx.Response(503, headers={"Retry-After": "1"}, text="maintenance"),
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("no route"),
        httpx.PoolTimeout("pool full"),
    ],
    ids=["429", "503-retry-after", "connect-error", "connect-timeout", "pool-timeout"],
)
@respx.mock
async def test_post_is_resent_when_the_failure_proves_nothing_happened(
    failure: httpx.Response | Exception,
) -> None:
    route = respx.post(f"{API}/thing").mock(side_effect=[failure, httpx.Response(200, json={"ok": 1})])
    client = _client()
    try:
        assert await client.post_json("thing", json_body={}) == {"ok": 1}
    finally:
        await client.aclose()
    assert route.call_count == 2


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(502, text="bad gateway"), httpx.ReadTimeout("slow")],
    ids=["502", "read-timeout"],
)
@respx.mock
async def test_idempotent_requests_are_resent_after_any_transient_failure(
    failure: httpx.Response | Exception,
) -> None:
    get = respx.get(f"{API}/thing").mock(side_effect=[failure, httpx.Response(200, json={"ok": 1})])
    post = respx.post(f"{API}/sync").mock(side_effect=[failure, httpx.Response(200, json={"ok": 2})])
    client = _client()
    try:
        assert await client.get_json("thing") == {"ok": 1}
        assert await client.request_json("POST", "sync", idempotent=True) == {"ok": 2}
    finally:
        await client.aclose()
    assert get.call_count == 2
    assert post.call_count == 2


# --- how long to wait ---------------------------------------------------------------------------


@respx.mock
async def test_429_waits_as_long_as_the_server_asks() -> None:
    """Before the fix a 429 asking for 20 s gave up after 3.5 s of blind backoff (RUN-10)."""
    respx.put(f"{API}/send").mock(
        side_effect=[
            httpx.Response(429, json={"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 2500}),
            httpx.Response(429, headers={"Retry-After": "7"}, text="slow down"),
            httpx.Response(200, json={}),
        ]
    )
    sleeps = _Sleeps()
    client = _client(sleeps)
    try:
        await client.put_json("send", json_body={})
    finally:
        await client.aclose()
    assert sleeps.waits == [2.5, 7.0]


@respx.mock
async def test_a_server_requested_wait_is_capped() -> None:
    respx.put(f"{API}/send").mock(
        side_effect=[
            httpx.Response(429, json={"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 3_600_000}),
            httpx.Response(200, json={}),
        ]
    )
    sleeps = _Sleeps()
    client = _client(sleeps)
    try:
        await client.put_json("send", json_body={})
    finally:
        await client.aclose()
    assert sleeps.waits == [MAX_RETRY_AFTER_SEC]


@respx.mock
async def test_without_a_hint_the_backoff_is_exponential() -> None:
    respx.get(f"{API}/thing").mock(return_value=httpx.Response(503, text="busy"))
    sleeps = _Sleeps()
    client = _client(sleeps)
    try:
        with pytest.raises(ApiError) as exc:
            await client.get_json("thing")
    finally:
        await client.aclose()
    assert exc.value.retry_after is None
    assert sleeps.waits == [0.5, 1.0, 2.0]


# --- timeouts -----------------------------------------------------------------------------------


@respx.mock
async def test_matrix_calls_use_30s_and_only_sync_waits_longer() -> None:
    """Before the fix every Matrix call had the sync's 60 s timeout (RUN-10)."""
    whoami = respx.get(f"{HS}/_matrix/client/v3/account/whoami").mock(
        return_value=httpx.Response(200, json={"user_id": "@bot:matrix.test"})
    )
    sync = respx.post(f"{HS}/_matrix/client/unstable/org.matrix.simplified_msc3575/sync").mock(
        return_value=httpx.Response(200, json={"pos": "1"})
    )
    client = _matrix()
    try:
        await client.get_json("v3/account/whoami")
        await client.sliding_sync(timeout_ms=30000)
    finally:
        await client.aclose()
    assert whoami.calls[0].request.extensions["timeout"]["read"] == 30.0
    assert sync.calls[0].request.extensions["timeout"]["read"] == 45.0


# --- auth ---------------------------------------------------------------------------------------


def _oauth_client(**kwargs: object) -> BaseApiClient:
    provider = OAuth2ClientCredentialsTokenProvider(
        token_endpoint=TOKEN_ENDPOINT, client_id="bot", client_secret="s", scope="urn:mas:admin"
    )
    return BaseApiClient(API, token_provider=provider, sleep=_Sleeps(), **kwargs)  # type: ignore[arg-type]


@respx.mock
async def test_mas_5xx_on_the_token_endpoint_is_retried_even_for_a_post() -> None:
    """Before the fix a MAS restart failed the call at once (RUN-06 item 3). Nothing was sent yet, so
    retrying is safe for every method."""
    respx.post(TOKEN_ENDPOINT).mock(
        side_effect=[
            httpx.Response(503, text="restarting"),
            httpx.Response(200, json={"access_token": "at1", "expires_in": 3600}),
        ]
    )
    api = respx.post(f"{API}/lock").mock(return_value=httpx.Response(200, json={}))
    client = _oauth_client()
    try:
        await client.post_json("lock")
    finally:
        await client.aclose()
    assert api.call_count == 1
    assert api.calls[0].request.headers["authorization"] == "Bearer at1"


@respx.mock
async def test_bad_client_credentials_are_not_retried() -> None:
    token = respx.post(TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    client = _oauth_client()
    try:
        with pytest.raises(Exception, match="invalid_client"):
            await client.get_json("thing")
    finally:
        await client.aclose()
    assert token.call_count == 1


@respx.mock
async def test_a_401_replaces_the_token_and_resends_once() -> None:
    """Before the fix a revoked token was reused until its local expiry (RUN-06 item 4)."""
    respx.post(TOKEN_ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "revoked", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600}),
        ]
    )
    api = respx.post(f"{API}/kick").mock(
        side_effect=[
            httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"}),
            httpx.Response(200, json={}),
        ]
    )
    client = _oauth_client()
    try:
        await client.post_json("kick", json_body={})
    finally:
        await client.aclose()
    assert [c.request.headers["authorization"] for c in api.calls] == ["Bearer revoked", "Bearer fresh"]


@respx.mock
async def test_a_401_with_a_static_token_is_not_resent() -> None:
    api = respx.get(f"{API}/thing").mock(
        return_value=httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})
    )
    client = _client()
    try:
        with pytest.raises(ApiError) as exc:
            await client.get_json("thing")
    finally:
        await client.aclose()
    assert exc.value.status_code == 401
    assert api.call_count == 1


# --- createRoom: find what an ambiguous failure created (RUN-05) -------------------------------


def _nonce_of(request: httpx.Request) -> str:
    nonce: str = json.loads(request.content)["creation_content"][CREATION_NONCE_KEY]
    return nonce


@respx.mock
async def test_dm_created_despite_a_timeout_is_adopted_not_created_twice() -> None:
    """Before the fix the timeout made the client send createRoom again: a second DM room and a
    second invite for the user, the first one orphaned."""
    created: list[str] = []

    def create(request: httpx.Request) -> httpx.Response:
        created.append(_nonce_of(request))
        raise httpx.ReadTimeout("synapse was slow, but it did create the room")

    respx.post(f"{HS}/_matrix/client/v3/createRoom").mock(side_effect=create)
    respx.get(f"{HS}/_matrix/client/v3/joined_rooms").mock(
        side_effect=[
            httpx.Response(200, json={"joined_rooms": ["!old"]}),
            httpx.Response(200, json={"joined_rooms": ["!old", "!other", "!dm"]}),
        ]
    )
    respx.get(f"{HS}/_matrix/client/v3/rooms/!other/state/m.room.create").mock(
        return_value=httpx.Response(200, json={CREATION_NONCE_KEY: "someone-elses"})
    )
    respx.get(f"{HS}/_matrix/client/v3/rooms/!dm/state/m.room.create").mock(
        side_effect=lambda request: httpx.Response(200, json={CREATION_NONCE_KEY: created[0]})
    )
    client = _matrix()
    try:
        room_id = await client.create_room(is_direct=True, invite=["@u:matrix.test"])
    finally:
        await client.aclose()
    assert room_id == "!dm"
    assert len(created) == 1


@respx.mock
async def test_aliased_room_created_despite_a_502_is_adopted_and_linked_to_its_space() -> None:
    """Before the fix the resend failed with M_ROOM_IN_USE and the space link was skipped."""
    created: list[str] = []

    def create(request: httpx.Request) -> httpx.Response:
        created.append(_nonce_of(request))
        return httpx.Response(502, text="bad gateway")

    respx.post(f"{HS}/_matrix/client/v3/createRoom").mock(side_effect=create)
    respx.get(f"{HS}/_matrix/client/v3/directory/room/%23team%3Amatrix.test").mock(
        return_value=httpx.Response(200, json={"room_id": "!team"})
    )
    respx.get(f"{HS}/_matrix/client/v3/rooms/!team/state/m.room.create").mock(
        side_effect=lambda request: httpx.Response(200, json={CREATION_NONCE_KEY: created[0]})
    )
    link = respx.put(f"{HS}/_matrix/client/v3/rooms/!space/state/m.space.child/!team").mock(
        return_value=httpx.Response(200, json={})
    )
    client = _matrix()
    try:
        room_id = await client.create_room(alias_localpart="team", name="Team", parent_space_id="!space")
    finally:
        await client.aclose()
    assert room_id == "!team"
    assert len(created) == 1
    assert link.called


@respx.mock
async def test_a_room_that_was_not_created_raises_the_original_error() -> None:
    create = respx.post(f"{HS}/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(504, text="timeout")
    )
    respx.get(f"{HS}/_matrix/client/v3/directory/room/%23team%3Amatrix.test").mock(
        return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
    )
    client = _matrix()
    try:
        with pytest.raises(ApiError) as exc:
            await client.create_room(alias_localpart="team", name="Team")
    finally:
        await client.aclose()
    assert exc.value.status_code == 504
    assert create.call_count == 1


@respx.mock
async def test_an_alias_held_by_another_room_is_not_adopted() -> None:
    respx.post(f"{HS}/_matrix/client/v3/createRoom").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    respx.get(f"{HS}/_matrix/client/v3/directory/room/%23team%3Amatrix.test").mock(
        return_value=httpx.Response(200, json={"room_id": "!someone-elses"})
    )
    respx.get(f"{HS}/_matrix/client/v3/rooms/!someone-elses/state/m.room.create").mock(
        return_value=httpx.Response(200, json={"creator": "@human:matrix.test"})
    )
    client = _matrix()
    try:
        with pytest.raises(ApiError):
            await client.create_room(alias_localpart="team", name="Team")
    finally:
        await client.aclose()


# --- kick and join: check before doing it again (RUN-05) ---------------------------------------


@respx.mock
async def test_a_kick_that_landed_before_the_error_counts_as_done() -> None:
    """Before the fix the resend got 403 "not in the room" and a done kick was reported as failed."""
    kick = respx.post(f"{HS}/_matrix/client/v3/rooms/!r/kick").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    respx.get(f"{HS}/_matrix/client/v3/rooms/!r/state/m.room.member/@u:matrix.test").mock(
        return_value=httpx.Response(200, json={"membership": "leave"})
    )
    client = _matrix()
    try:
        await client.kick_user("!r", "@u:matrix.test", "bye")
    finally:
        await client.aclose()
    assert kick.call_count == 1


@respx.mock
async def test_a_kick_that_did_not_land_is_sent_once_more() -> None:
    kick = respx.post(f"{HS}/_matrix/client/v3/rooms/!r/kick").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={})]
    )
    respx.get(f"{HS}/_matrix/client/v3/rooms/!r/state/m.room.member/@u:matrix.test").mock(
        return_value=httpx.Response(200, json={"membership": "join"})
    )
    client = _matrix()
    try:
        await client.kick_user("!r", "@u:matrix.test")
    finally:
        await client.aclose()
    assert kick.call_count == 2


@respx.mock
async def test_a_kick_whose_outcome_cannot_be_checked_raises_the_original_error() -> None:
    respx.post(f"{HS}/_matrix/client/v3/rooms/!r/kick").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    respx.get(f"{HS}/_matrix/client/v3/rooms/!r/state/m.room.member/@u:matrix.test").mock(
        side_effect=httpx.ConnectError("down")
    )
    client = _matrix()
    try:
        with pytest.raises(ApiError) as exc:
            await client.kick_user("!r", "@u:matrix.test")
    finally:
        await client.aclose()
    assert exc.value.status_code == 502


@respx.mock
async def test_a_force_join_that_landed_before_the_error_counts_as_done() -> None:
    join = respx.post(f"{HS}/_synapse/admin/v1/join/!r").mock(
        return_value=httpx.Response(504, text="timeout")
    )
    respx.get(f"{HS}/_synapse/admin/v1/rooms/!r/members").mock(
        return_value=httpx.Response(200, json={"members": ["@bot:matrix.test", "@u:matrix.test"]})
    )
    admin = ApiClientSynapseAdmin(server_url=HS, access_token="tok", sleep=_Sleeps())
    try:
        await admin.add_user_to_room("!r", "@u:matrix.test")
    finally:
        await admin.aclose()
    assert join.call_count == 1


@respx.mock
async def test_a_refused_kick_is_raised_without_a_membership_check() -> None:
    kick = respx.post(f"{HS}/_matrix/client/v3/rooms/!r/kick").mock(
        return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
    )
    member = respx.get(f"{HS}/_matrix/client/v3/rooms/!r/state/m.room.member/@u:matrix.test")
    client = _matrix()
    try:
        with pytest.raises(ApiError):
            await client.kick_user("!r", "@u:matrix.test")
    finally:
        await client.aclose()
    assert kick.call_count == 1
    assert not member.called


@respx.mock
async def test_a_send_that_paces_itself_gets_the_429_at_once() -> None:
    """ONB-03: the broadcast waits for the server itself, so the client must not wait it out first."""
    route = respx.put(url__startswith=f"{HS}/_matrix/client/v3/rooms/").mock(
        side_effect=[
            httpx.Response(429, json={"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 4000}),
            httpx.Response(200, json={"event_id": "$e"}),
        ]
    )
    sleeps = _Sleeps()
    client = _matrix(sleeps)
    try:
        with pytest.raises(ApiError) as caught:
            await client.send_text_message("!r:matrix.test", "hi", retry_rate_limited=False)
        assert caught.value.status_code == 429
        assert caught.value.retry_after == 4.0
        assert route.call_count == 1
        assert sleeps.waits == []
        # Every other caller keeps the retry.
        assert await client.send_text_message("!r:matrix.test", "hi") == "$e"
    finally:
        await client.aclose()
