"""MAS admin API client (async).

Under the MAS topology (ADR-0006) the Matrix access token is owned by MAS, not Synapse. The Phase 7b
integration experiment (§7 Q1) proved that the Synapse admin API cannot revoke a live session —
deleting devices or deactivating the account leaves the MAS-issued token valid. Effective lockout
must go through MAS itself. This client wraps the MAS admin API operations the lifecycle module needs:

* lock / unlock a user (reversible session revocation — the cooldown ``logout`` stage),
* deactivate a user (irreversible — the ``erase`` stage).

Auth is an OAuth2 ``client_credentials`` token carrying the ``urn:mas:admin`` scope (the bot's MAS
admin client must be listed in MAS ``policy.data.admin_clients``).
"""

from __future__ import annotations

from typing import Any

from onbot.auth.token_provider import TokenProvider
from onbot.clients.base import ApiError, BaseApiClient
from onbot.logging import get_logger

log = get_logger(__name__)


def mxid_localpart(mxid: str) -> str:
    """Extract the localpart from a full MXID (``@local:server`` -> ``local``)."""
    return mxid.removeprefix("@").split(":", 1)[0]


class ApiClientMasAdmin(BaseApiClient):
    """The subset of the MAS admin API onbot's lifecycle module uses."""

    def __init__(
        self,
        mas_url: str,
        access_token: str | None = None,
        *,
        token_provider: TokenProvider | None = None,
        **kwargs: Any,
    ) -> None:
        base = f"{mas_url.rstrip('/')}/api/admin/v1"
        super().__init__(base_url=base, auth_token=access_token, token_provider=token_provider, **kwargs)

    async def get_user_id_by_username(self, username: str) -> str | None:
        """Resolve a MAS user ULID from its username (localpart), or ``None`` if unknown."""
        try:
            result = await self.get_json(f"users/by-username/{username}")
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        user_id: str = result["data"]["id"]
        return user_id

    async def lock_user(self, user_id: str) -> None:
        """Lock the user: revokes active sessions and blocks new logins (reversible)."""
        await self.post_json(f"users/{user_id}/lock")

    async def unlock_user(self, user_id: str) -> None:
        await self.post_json(f"users/{user_id}/unlock")

    async def deactivate_user(self, user_id: str) -> None:
        """Deactivate the user: revokes sessions and permanently disables the account."""
        await self.post_json(f"users/{user_id}/deactivate")
