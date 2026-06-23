"""Dependency health probe for ``onbot healthcheck`` (BATTLE_PLAN §5 Phase 8).

Probes the services the bot actually talks to — the Matrix CS API, the Synapse admin API, the
Authentik API, and (when configured) the MAS admin API — using the *real* configured credentials, so
the check verifies connectivity **and** authorization, not just that a port is open. Each probe is a
single lightweight authenticated request.

Exit code contract (suitable for a container ``HEALTHCHECK`` / orchestrator readiness probe):

* ``0`` — every required dependency answered successfully.
* ``1`` — at least one probe failed (unreachable, auth rejected, or unexpected response).

The bot user id from ``/whoami`` is compared against ``synapse_server.bot_user_id``; a mismatch is a
warning (the token authenticates as a different user than configured) but not a hard failure, since
the bot can still operate as whoever the token belongs to.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from onbot.app import build_matrix_token_provider
from onbot.auth.token_provider import OAuth2ClientCredentialsTokenProvider
from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.mas_admin import ApiClientMasAdmin, mxid_localpart
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ProbeResult:
    """Outcome of a single dependency probe."""

    name: str
    ok: bool
    detail: str


async def _probe_matrix(matrix: ApiClientMatrix, expected_user_id: str) -> ProbeResult:
    """Reach the CS API and confirm the bot token authenticates (``/whoami``)."""
    whoami = await matrix.get_json("v3/account/whoami")
    user_id = whoami.get("user_id", "")
    if user_id != expected_user_id:
        return ProbeResult(
            "matrix-cs",
            True,
            f"reachable, but token is @{user_id} (config expects {expected_user_id})",
        )
    return ProbeResult("matrix-cs", True, f"authenticated as {user_id}")


async def _probe_synapse_admin(admin: ApiClientSynapseAdmin) -> ProbeResult:
    """Confirm the admin token is authorized against the Synapse admin API."""
    # A minimal authenticated read; ``guests=false`` is required under MSC3861/MAS (synapse_admin.py).
    await admin.get_json("v2/users", params={"limit": 1, "guests": "false"})
    return ProbeResult("synapse-admin", True, "admin API authorized")


async def _probe_authentik(authentik: ApiClientAuthentik) -> ProbeResult:
    """Confirm the Authentik API token works."""
    await authentik.get_json("core/users/", params={"page_size": 1})
    return ProbeResult("authentik", True, "API token accepted")


async def _probe_mas_admin(mas: ApiClientMasAdmin, bot_user_id: str) -> ProbeResult:
    """Confirm the MAS admin client-credentials token works (lifecycle enforcement path, §7 Q1)."""
    # ``by-username`` returns the user or 404 (both prove auth); the lookup itself never mutates.
    await mas.get_user_id_by_username(mxid_localpart(bot_user_id))
    return ProbeResult("mas-admin", True, "admin API authorized")


async def run_healthcheck(config: OnbotConfig) -> int:
    """Probe every configured dependency, log a line per result, and return an exit code."""
    token_provider = build_matrix_token_provider(config.synapse_server)
    authentik = ApiClientAuthentik(url=config.authentik_server.url, api_key=config.authentik_server.api_key)
    admin = ApiClientSynapseAdmin(
        server_url=config.synapse_server.server_url,
        token_provider=token_provider,
        admin_api_path=config.synapse_server.admin_api_path,
    )
    matrix = ApiClientMatrix(
        server_url=config.synapse_server.server_url,
        token_provider=token_provider,
        server_name=config.synapse_server.server_name,
    )
    mas: ApiClientMasAdmin | None = None
    if config.mas_admin is not None:
        mas = ApiClientMasAdmin(
            mas_url=config.mas_admin.url,
            token_provider=OAuth2ClientCredentialsTokenProvider(
                token_endpoint=f"{config.mas_admin.url.rstrip('/')}/oauth2/token",
                client_id=config.mas_admin.client_id,
                client_secret=config.mas_admin.client_secret,
                scope="urn:mas:admin",
            ),
        )

    probes = [
        _probe_matrix(matrix, config.synapse_server.bot_user_id),
        _probe_synapse_admin(admin),
        _probe_authentik(authentik),
    ]
    names = ["matrix-cs", "synapse-admin", "authentik"]
    if mas is not None:
        probes.append(_probe_mas_admin(mas, config.synapse_server.bot_user_id))
        names.append("mas-admin")

    try:
        results = await asyncio.gather(*probes, return_exceptions=True)
    finally:
        await authentik.aclose()
        await admin.aclose()
        await matrix.aclose()
        if mas is not None:
            await mas.aclose()

    failed = False
    for name, result in zip(names, results, strict=True):
        if isinstance(result, ProbeResult):
            log.info("healthcheck %-14s OK   — %s", result.name, result.detail)
        else:
            failed = True
            log.error("healthcheck %-14s FAIL — %r", name, result)

    if failed:
        log.error("healthcheck: one or more dependencies are unhealthy")
        return 1
    log.info("healthcheck: all dependencies healthy")
    return 0
