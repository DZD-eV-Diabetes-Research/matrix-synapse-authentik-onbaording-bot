"""Tests for the ``onbot healthcheck`` dependency probe (Phase 8)."""

from __future__ import annotations

import httpx
import respx

from onbot.config import AuthentikServer, MasAdmin, OnbotConfig, SynapseServer
from onbot.healthcheck import run_healthcheck


def _config(*, with_mas: bool = False) -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="example.test",
            server_url="https://matrix.test",
            bot_user_id="@bot:example.test",
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="ak"),
        mas_admin=(
            MasAdmin(url="https://mas.test", client_id="cid", client_secret="secret") if with_mas else None
        ),
    )


def _mock_core_endpoints(*, whoami_user: str = "@bot:example.test") -> None:
    respx.get("https://matrix.test/_matrix/client/v3/account/whoami").mock(
        return_value=httpx.Response(200, json={"user_id": whoami_user, "device_id": "DEV"})
    )
    respx.get("https://matrix.test/_synapse/admin/v2/users").mock(
        return_value=httpx.Response(200, json={"users": []})
    )
    respx.get("https://authentik.test/api/v3/core/users/").mock(
        return_value=httpx.Response(200, json={"results": [], "pagination": {}})
    )


@respx.mock
async def test_healthcheck_all_ok() -> None:
    _mock_core_endpoints()
    assert await run_healthcheck(_config()) == 0


@respx.mock
async def test_healthcheck_user_mismatch_is_not_fatal() -> None:
    # A token authenticating as a different user is a warning, not a failure (exit 0).
    _mock_core_endpoints(whoami_user="@someone-else:example.test")
    assert await run_healthcheck(_config()) == 0


@respx.mock
async def test_healthcheck_failure_returns_nonzero() -> None:
    _mock_core_endpoints()
    # 401 is not retryable, so this fails fast rather than looping on backoff.
    respx.get("https://authentik.test/api/v3/core/users/").mock(
        return_value=httpx.Response(401, json={"detail": "bad token"})
    )
    assert await run_healthcheck(_config()) == 1


@respx.mock
async def test_healthcheck_includes_mas_admin_when_configured() -> None:
    _mock_core_endpoints()
    respx.post("https://mas.test/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "mas-tok", "expires_in": 3600})
    )
    by_username = respx.get("https://mas.test/api/admin/v1/users/by-username/bot").mock(
        return_value=httpx.Response(404)  # 404 → None, still proves auth worked
    )
    assert await run_healthcheck(_config(with_mas=True)) == 0
    assert by_username.called


@respx.mock
async def test_healthcheck_mas_admin_failure_is_fatal() -> None:
    _mock_core_endpoints()
    respx.post("https://mas.test/oauth2/token").mock(
        return_value=httpx.Response(403, json={"error": "unauthorized_client"})
    )
    assert await run_healthcheck(_config(with_mas=True)) == 1
