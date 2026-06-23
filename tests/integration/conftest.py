"""Fixtures for the Phase 7b live integration harness.

The expensive bit — the Synapse + MAS + Postgres + Authentik stack — is brought up once per session
via ``testcontainers`` (compose). On top of it sit the bot's MAS-issued credentials, a loaded
:class:`OnbotConfig` pointing at the containers, and ready API clients. Per-test state (Authentik
groups/users) is created with unique names so tests share the stack without colliding.

Set ``ONBOT_ITEST_KEEP=1`` to leave the stack running after the session (fast local iteration).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from testcontainers.compose import DockerCompose

from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import (
    AuthentikServer,
    MasAdmin,
    OnbotConfig,
    SynapseServer,
)
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration

STACK_DIR = Path(__file__).parent / "stack"
_READY_TIMEOUT_SEC = 480


def _wait_ready() -> None:
    """Poll the externally observable readiness signals of every service."""
    checks = {
        "synapse": f"{S.SYNAPSE_URL}/health",
        "mas-discovery": f"{S.MAS_URL}/.well-known/openid-configuration",
        # The Authentik OIDC discovery for app slug 'mas' only resolves once the blueprint applied.
        "authentik-upstream": f"{S.AUTHENTIK_URL}/application/o/mas/.well-known/openid-configuration",
    }
    deadline = time.time() + _READY_TIMEOUT_SEC
    pending = dict(checks)
    last_err: dict[str, str] = {}
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            try:
                if httpx.get(url, timeout=5.0).status_code == 200:
                    del pending[name]
            except Exception as exc:
                last_err[name] = str(exc)
        if pending:
            time.sleep(3.0)
    if pending:
        raise RuntimeError(f"stack not ready: {list(pending)} (last errors: {last_err})")


@pytest.fixture(scope="session")
def compose_stack() -> Iterator[DockerCompose]:
    if shutil.which("docker") is None:
        pytest.skip("docker not available; integration suite needs the live stack")
    compose = DockerCompose(str(STACK_DIR), compose_file_name="docker-compose.yml", pull=False, wait=False)
    try:
        compose.start()
    except Exception as exc:
        pytest.skip(f"could not start the integration stack: {exc}")
    try:
        _wait_ready()
        yield compose
    finally:
        if os.environ.get("ONBOT_ITEST_KEEP") != "1":
            compose.stop(down=True)


@pytest.fixture(scope="session")
def bot_token(compose_stack: DockerCompose) -> str:
    """Provision the bot user in MAS and issue an admin-scoped compatibility token (AD-6)."""
    common = ["--config", "/config/config.yaml"]
    with contextlib.suppress(Exception):  # idempotent: ignore "already exists" on a re-used stack
        compose_stack.exec_in_container(
            [
                "mas-cli",
                "manage",
                "register-user",
                "onbot",
                "--email",
                "onbot@onbot.test",
                "--yes",
                "--admin",
                "--ignore-password-complexity",
                "--display-name",
                "Onbot",
                *common,
            ],
            service_name="mas",
        )
    out, err, _ = compose_stack.exec_in_container(
        [
            "mas-cli",
            "manage",
            "issue-compatibility-token",
            "onbot",
            "--yes-i-want-to-grant-synapse-admin-privileges",
            *common,
        ],
        service_name="mas",
    )
    match = re.search(r"mct_[A-Za-z0-9._~+/=-]+", out + err)
    if not match:
        raise RuntimeError(f"could not parse compat token from mas-cli output:\n{out}\n{err}")
    token = match.group(0)
    # First authenticated call provisions the bot's Synapse account (lazy under MSC3861).
    httpx.get(
        f"{S.SYNAPSE_URL}/_matrix/client/v3/account/whoami",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ).raise_for_status()
    return token


def _build_config(token: str) -> OnbotConfig:
    config = OnbotConfig(
        synapse_server=SynapseServer(
            server_name=S.SERVER_NAME,
            server_url=S.SYNAPSE_URL,
            bot_user_id=S.BOT_USER_ID,
            bot_access_token=token,
            admin_api_path="_synapse/admin/",
        ),
        authentik_server=AuthentikServer(url=S.AUTHENTIK_URL, api_key=S.AUTHENTIK_TOKEN),
        mas_admin=MasAdmin(
            url=S.MAS_URL,
            client_id=S.MAS_ADMIN_CLIENT_ID,
            client_secret=S.MAS_ADMIN_CLIENT_SECRET,
        ),
        server_tick_rate_sec=1,
    )
    # Bot operates outside encrypted rooms (ADR-0009): plain rooms keep the harness simple.
    config.matrix_room_default_settings.end2end_encryption_enabled = False
    # Only mirror Authentik groups explicitly flagged for chat, so built-in groups are ignored.
    config.sync_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes = {"is_chatroom": True}
    # Each test opts into the parent space explicitly.
    config.create_matrix_rooms_in_a_matrix_space.enabled = False
    return config


@pytest.fixture(scope="session")
def onbot_config(bot_token: str) -> OnbotConfig:
    """A loaded OnbotConfig pointing at the live stack (the canonical session config)."""
    return _build_config(bot_token)


@pytest.fixture
def make_config(bot_token: str) -> Callable[[], OnbotConfig]:
    """Factory for a fresh, test-tunable OnbotConfig (mutate the returned model per test)."""
    return lambda: _build_config(bot_token)


@pytest.fixture(scope="session")
def authentik_admin(compose_stack: DockerCompose) -> Iterator[S.AuthentikAdmin]:
    admin = S.AuthentikAdmin()
    try:
        yield admin
    finally:
        admin.close()


@pytest_asyncio.fixture
async def matrix_client(onbot_config: OnbotConfig) -> AsyncIterator[ApiClientMatrix]:
    client = ApiClientMatrix(
        server_url=onbot_config.synapse_server.server_url,
        access_token=onbot_config.synapse_server.bot_access_token,
        server_name=onbot_config.synapse_server.server_name,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def admin_client(onbot_config: OnbotConfig) -> AsyncIterator[ApiClientSynapseAdmin]:
    client = ApiClientSynapseAdmin(
        server_url=onbot_config.synapse_server.server_url,
        access_token=onbot_config.synapse_server.bot_access_token,
        admin_api_path=onbot_config.synapse_server.admin_api_path,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def authentik_client(onbot_config: OnbotConfig) -> AsyncIterator[ApiClientAuthentik]:
    client = ApiClientAuthentik(
        url=onbot_config.authentik_server.url, api_key=onbot_config.authentik_server.api_key
    )
    try:
        yield client
    finally:
        await client.aclose()
