"""The HTTP layer against the live stack (review session S11).

* RUN-05: a non-idempotent call whose response is lost after the server acted is not done twice.
  A transport wrapper forwards each request to the real server and then throws the answer away,
  which is what a proxy timeout or a dropped connection looks like to the bot.
* The calls marked idempotent really are: Synapse and MAS answer a repeat with success.
* RUN-14: the totals the pagination check relies on are present in the real Authentik and Synapse
  answers, and a listing read one entry per page passes the check.
* LIF-10: the deep healthcheck resolves the bot itself in MAS.

``synapse_server.oauth2`` was checked here too, and removed after MAS 0.14 refused it (RUN-11): MAS
does not grant the Matrix API or Synapse admin scopes to ``client_credentials``, even for a client in
``admin_clients``, and Synapse answers a client token without them with ``401 M_UNKNOWN_TOKEN``. The
config now rejects the option (``tests/unit/test_config_validation.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from onbot.auth.token_provider import OAuth2ClientCredentialsTokenProvider
from onbot.clients.authentik import ApiClientAuthentik, _next_page_params, _total
from onbot.clients.mas_admin import ApiClientMasAdmin, mxid_localpart
from onbot.clients.matrix import CREATION_NONCE_KEY, ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin, _next_token_params
from onbot.config import OnbotConfig
from onbot.healthcheck import run_healthcheck
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


class _LoseResponse(httpx.AsyncBaseTransport):
    """Forward every request; for the ones ``lose`` picks, drop the answer the server sent."""

    def __init__(self, lose: Callable[[httpx.Request], bool], times: int = 1) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self._lose = lose
        self._left = times
        self.lost: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if self._left > 0 and self._lose(request):
            self._left -= 1
            await response.aread()
            self.lost.append(f"{request.method} {request.url.path}")
            raise httpx.ReadTimeout("response lost on the way back", request=request)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _token(config: OnbotConfig) -> str:
    token = config.synapse_server.bot_access_token
    assert token is not None
    return token.get_secret_value()


def _matrix(config: OnbotConfig, transport: httpx.AsyncBaseTransport | None = None) -> ApiClientMatrix:
    return ApiClientMatrix(
        server_url=config.synapse_server.server_url,
        access_token=_token(config),
        server_name=config.synapse_server.server_name,
        client=httpx.AsyncClient(transport=transport) if transport is not None else None,
    )


def _admin(config: OnbotConfig, transport: httpx.AsyncBaseTransport | None = None) -> ApiClientSynapseAdmin:
    return ApiClientSynapseAdmin(
        server_url=config.synapse_server.server_url,
        access_token=_token(config),
        admin_api_path=config.synapse_server.admin_api_path,
        client=httpx.AsyncClient(transport=transport) if transport is not None else None,
    )


def _is_create_room(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path.endswith("/v3/createRoom")


# --- RUN-05: a lost response does not double the effect -----------------------------------------


async def test_dm_room_whose_response_was_lost_is_adopted_not_created_twice(
    onbot_config: OnbotConfig, matrix_client: ApiClientMatrix, authentik_admin: S.AuthentikAdmin
) -> None:
    _user, login = S.provision(authentik_admin, S.uniq("s11dm"))
    target = login.mxid
    lossy = _LoseResponse(_is_create_room)
    client = _matrix(onbot_config, lossy)
    try:
        before = set(await matrix_client.joined_rooms())
        room_id = await client.create_direct_message_room(target, name="S11 lost response")
    finally:
        await client.aclose()
    assert lossy.lost == ["POST /_matrix/client/v3/createRoom"]
    new_rooms = set(await matrix_client.joined_rooms()) - before
    assert new_rooms == {room_id}, "exactly one room was created and it is the one returned"
    create = await matrix_client.get_room_state_event(room_id, "m.room.create")
    assert create is not None and CREATION_NONCE_KEY in create


async def test_aliased_room_whose_response_was_lost_is_adopted_and_linked(
    onbot_config: OnbotConfig, matrix_client: ApiClientMatrix
) -> None:
    space_id = await matrix_client.create_space(
        alias_localpart=S.uniq("s11space"), name="S11 space", topic="", params={}
    )
    alias = S.uniq("s11room")
    lossy = _LoseResponse(_is_create_room)
    client = _matrix(onbot_config, lossy)
    try:
        room_id = await client.create_room(alias_localpart=alias, name="S11 room", parent_space_id=space_id)
    finally:
        await client.aclose()
    assert lossy.lost
    assert await matrix_client.resolve_room_alias(f"#{alias}:{S.SERVER_NAME}") == room_id
    child = await matrix_client.get_room_state_event(space_id, "m.space.child", room_id)
    assert child is not None and child.get("via")


async def test_kick_whose_response_was_lost_counts_as_done(
    onbot_config: OnbotConfig,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    authentik_admin: S.AuthentikAdmin,
) -> None:
    _user, login = S.provision(authentik_admin, S.uniq("s11kick"))
    room_id = await matrix_client.create_room(name="S11 kick")
    await admin_client.add_user_to_room(room_id, login.mxid)
    lossy = _LoseResponse(lambda r: r.method == "POST" and r.url.path.endswith("/kick"))
    client = _matrix(onbot_config, lossy)
    try:
        await client.kick_user(room_id, login.mxid, "S11 test")
    finally:
        await client.aclose()
    assert lossy.lost
    assert await matrix_client.get_membership(room_id, login.mxid) == "leave"


async def test_force_join_whose_response_was_lost_counts_as_done(
    onbot_config: OnbotConfig, matrix_client: ApiClientMatrix, authentik_admin: S.AuthentikAdmin
) -> None:
    _user, login = S.provision(authentik_admin, S.uniq("s11join"))
    room_id = await matrix_client.create_room(name="S11 join")
    lossy = _LoseResponse(lambda r: r.method == "POST" and "/v1/join/" in r.url.path)
    admin = _admin(onbot_config, lossy)
    try:
        await admin.add_user_to_room(room_id, login.mxid)
        assert login.mxid in await admin.list_room_members(room_id)
    finally:
        await admin.aclose()
    assert lossy.lost


# --- calls marked idempotent are safe to repeat ---------------------------------------------------


async def test_calls_marked_idempotent_succeed_when_repeated(
    onbot_config: OnbotConfig,
    matrix_client: ApiClientMatrix,
    admin_client: ApiClientSynapseAdmin,
    authentik_admin: S.AuthentikAdmin,
) -> None:
    _user, login = S.provision(authentik_admin, S.uniq("s11idem"))
    room_id = await matrix_client.create_room(name="S11 idempotent")
    for _ in range(2):
        await matrix_client.invite_user(room_id, login.mxid)
        await admin_client.override_ratelimit(onbot_config.synapse_server.bot_user_id)
        await matrix_client.ensure_device_registered()

    assert onbot_config.mas_admin is not None
    mas = ApiClientMasAdmin(
        mas_url=onbot_config.mas_admin.url,
        token_provider=OAuth2ClientCredentialsTokenProvider(
            token_endpoint=f"{S.MAS_URL}/oauth2/token",
            client_id=S.MAS_ADMIN_CLIENT_ID,
            client_secret=S.MAS_ADMIN_CLIENT_SECRET,
            scope="urn:mas:admin",
        ),
    )
    try:
        mas_id = await mas.get_user_id_by_username(mxid_localpart(login.mxid))
        assert mas_id is not None
        for _ in range(2):
            await mas.lock_user(mas_id)
        for _ in range(2):
            await mas.unlock_user(mas_id)
    finally:
        await mas.aclose()


# --- RUN-14: the totals exist and a one-per-page listing passes the check -----------------------


async def test_listings_read_one_per_page_match_the_reported_totals(
    authentik_client: ApiClientAuthentik, admin_client: ApiClientSynapseAdmin
) -> None:
    whole: list[dict[str, Any]] = await authentik_client.list_users(filter_is_active=None)
    page = await authentik_client.get_json("core/users/", params={"page_size": 1})
    assert _total(page) == len(whole)
    by_one = await authentik_client.paginate_collect(
        "core/users/",
        params={"page_size": 1},
        extract_items=lambda p: p["results"],
        next_params=_next_page_params,
        total=_total,
        item_key=lambda u: u["pk"],
    )
    assert sorted(u["pk"] for u in by_one) == sorted(u["pk"] for u in whole)

    synapse_users = await admin_client.list_users()
    first = await admin_client.get_json("v2/users", params={"limit": 1, "guests": "false"})
    assert first["total"] == len(synapse_users)
    one_by_one = await admin_client.paginate_collect(
        "v2/users",
        params={"limit": 1, "guests": "false"},
        extract_items=lambda p: p["users"],
        next_params=_next_token_params("next_token"),
        total=lambda p: p.get("total"),
        item_key=lambda u: u["name"],
    )
    assert len(one_by_one) == len(synapse_users) >= 2

    rooms = await admin_client.get_json("v1/rooms", params={"limit": 1})
    assert isinstance(rooms.get("total_rooms"), int)


# --- LIF-10: deep healthcheck ---------------------------------------------------------------------


async def test_deep_healthcheck_passes_and_resolves_the_bot_in_mas(onbot_config: OnbotConfig) -> None:
    assert onbot_config.mas_admin is not None
    assert await run_healthcheck(onbot_config) == 0


async def test_deep_healthcheck_fails_when_mas_does_not_know_the_bot(
    make_config: Callable[[], OnbotConfig],
) -> None:
    config = make_config()
    config.synapse_server.bot_user_id = "@not-a-mas-user:onbot.test"
    assert await run_healthcheck(config) == 1
