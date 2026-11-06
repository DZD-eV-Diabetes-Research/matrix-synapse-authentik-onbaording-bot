"""Every value in a URL path is one encoded segment (SEC-08).

`/` and `.` are legal in Matrix localparts. Before the fix a mapped localpart `x/../victim` sent
`users/by-username/x/../victim`, which the HTTP client normalised to `users/by-username/victim`: the
lookup returned the victim's MAS user, and the lifecycle locked the wrong account. These tests look
at the raw bytes of the request path, which is what the server sees.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from onbot.clients.base import BaseApiClient, path_segment
from onbot.clients.mas_admin import ApiClientMasAdmin
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin

HOSTILE_USER = "@x/../victim:matrix.test"
HOSTILE_LOCALPART = "x/../victim"
HOSTILE_ROOM = "!a/../b?c#d:matrix.test"


def test_path_segment_encodes_every_reserved_character() -> None:
    assert path_segment("x/../victim") == "x%2F..%2Fvictim"
    assert path_segment("@alice:matrix.test") == "%40alice%3Amatrix.test"
    assert path_segment("!r?x#y%z") == "%21r%3Fx%23y%25z"


@pytest.mark.parametrize("value", ["", ".", ".."])
def test_path_segment_refuses_dot_segments(value: str) -> None:
    with pytest.raises(ValueError):
        path_segment(value)


def _mas() -> ApiClientMasAdmin:
    return ApiClientMasAdmin(mas_url="https://mas.test", access_token="adm", max_retry_attempts=1)


def _synapse() -> ApiClientSynapseAdmin:
    return ApiClientSynapseAdmin(server_url="https://matrix.test", access_token="adm", max_retry_attempts=1)


def _matrix() -> ApiClientMatrix:
    return ApiClientMatrix(
        server_url="https://matrix.test", access_token="tok", server_name="matrix.test", max_retry_attempts=1
    )


Call = Callable[[Any], Awaitable[Any]]

# (client factory, call, the raw path the server must receive)
CASES: list[tuple[Callable[[], BaseApiClient], Call, str]] = [
    (
        _mas,
        lambda c: c.get_user_id_by_username(HOSTILE_LOCALPART),
        "/api/admin/v1/users/by-username/x%2F..%2Fvictim",
    ),
    (_mas, lambda c: c.lock_user("01/../02"), "/api/admin/v1/users/01%2F..%2F02/lock"),
    (_mas, lambda c: c.unlock_user("01/../02"), "/api/admin/v1/users/01%2F..%2F02/unlock"),
    (_mas, lambda c: c.deactivate_user("01/../02"), "/api/admin/v1/users/01%2F..%2F02/deactivate"),
    (
        _mas,
        lambda c: c.finish_session("compat-sessions", "s/../t"),
        "/api/admin/v1/compat-sessions/s%2F..%2Ft/finish",
    ),
    (
        _synapse,
        lambda c: c.logout_account(HOSTILE_USER),
        "/_synapse/admin/v2/users/%40x%2F..%2Fvictim%3Amatrix.test/devices",
    ),
    (
        _synapse,
        lambda c: c.deactivate_account(HOSTILE_USER, erase=False),
        "/_synapse/admin/v1/deactivate/%40x%2F..%2Fvictim%3Amatrix.test",
    ),
    (
        _synapse,
        lambda c: c.count_user_media(HOSTILE_USER),
        "/_synapse/admin/v1/users/%40x%2F..%2Fvictim%3Amatrix.test/media",
    ),
    (
        _synapse,
        lambda c: c.list_room_members(HOSTILE_ROOM),
        "/_synapse/admin/v1/rooms/%21a%2F..%2Fb%3Fc%23d%3Amatrix.test/members",
    ),
    (
        _synapse,
        lambda c: c.room_set_blocked(HOSTILE_ROOM, blocked=True),
        "/_synapse/admin/v1/rooms/%21a%2F..%2Fb%3Fc%23d%3Amatrix.test/block",
    ),
    (
        _synapse,
        lambda c: c.get_room_state_events(HOSTILE_ROOM),
        "/_synapse/admin/v1/rooms/%21a%2F..%2Fb%3Fc%23d%3Amatrix.test/state",
    ),
    (
        _matrix,
        lambda c: c.invite_user(HOSTILE_ROOM, HOSTILE_USER),
        "/_matrix/client/v3/rooms/%21a%2F..%2Fb%3Fc%23d%3Amatrix.test/invite",
    ),
    (
        _matrix,
        lambda c: c.put_room_state_event("!r:x", "m.room.member", {}, state_key=HOSTILE_USER),
        "/_matrix/client/v3/rooms/%21r%3Ax/state/m.room.member/%40x%2F..%2Fvictim%3Amatrix.test",
    ),
    (
        _matrix,
        lambda c: c.put_room_state_event("!r:x", "evil/../type", {}),
        "/_matrix/client/v3/rooms/%21r%3Ax/state/evil%2F..%2Ftype",
    ),
    (
        _matrix,
        lambda c: c.set_user_avatar(HOSTILE_USER, "mxc://x/y"),
        "/_matrix/client/v3/profile/%40x%2F..%2Fvictim%3Amatrix.test/avatar_url",
    ),
    (
        _matrix,
        lambda c: c.set_account_data(HOSTILE_USER, "org.x/../m.direct", {}),
        "/_matrix/client/v3/user/%40x%2F..%2Fvictim%3Amatrix.test/account_data/org.x%2F..%2Fm.direct",
    ),
    (
        _matrix,
        lambda c: c.set_room_account_data("@bot:x", HOSTILE_ROOM, "t", {}),
        "/_matrix/client/v3/user/%40bot%3Ax/rooms/%21a%2F..%2Fb%3Fc%23d%3Amatrix.test/account_data/t",
    ),
    (
        _matrix,
        lambda c: c.resolve_room_alias("#a/../b:matrix.test"),
        "/_matrix/client/v3/directory/room/%23a%2F..%2Fb%3Amatrix.test",
    ),
]


@pytest.mark.parametrize(("make", "call", "raw_path"), CASES)
@respx.mock
async def test_each_path_value_stays_one_segment(
    make: Callable[[], BaseApiClient], call: Call, raw_path: str
) -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode().split("?", 1)[0])
        if "/devices" in seen[-1]:
            return httpx.Response(200, json={"devices": []})
        if "/members" in seen[-1]:
            return httpx.Response(200, json={"members": []})
        if "/directory/room/" in seen[-1]:
            return httpx.Response(200, json={"room_id": "!r:x"})
        if "by-username" in seen[-1]:
            return httpx.Response(200, json={"data": {"id": "01ULID"}})
        return httpx.Response(200, json={"total": 0})

    respx.route().mock(side_effect=record)
    client = make()
    try:
        await call(client)
    finally:
        await client.aclose()
    assert seen[0] == raw_path


@respx.mock
async def test_an_absolute_url_is_followed_only_to_the_clients_own_origin() -> None:
    respx.get("https://matrix.test/_matrix/media/v3/config").mock(return_value=httpx.Response(200, json={}))
    client = _matrix()
    try:
        assert await client.get_json("https://matrix.test/_matrix/media/v3/config") == {}
        with pytest.raises(ValueError, match=r"evil\.test"):
            await client.get_json("https://evil.test/_matrix/media/v3/config")
        with pytest.raises(ValueError):
            await client.get_json("http://matrix.test/_matrix/media/v3/config")  # scheme downgrade
    finally:
        await client.aclose()
