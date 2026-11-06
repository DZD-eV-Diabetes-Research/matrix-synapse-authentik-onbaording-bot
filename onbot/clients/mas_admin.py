"""MAS admin API client (async).

Under the MAS topology (ADR-0006) the Matrix access token is owned by MAS, not Synapse. The Phase 7b
integration experiment (§7 Q1) proved that the Synapse admin API cannot revoke a live session:
deleting devices or deactivating the account leaves the MAS-issued token valid. Effective lockout
must go through MAS itself. This client wraps the MAS admin API operations the lifecycle module needs:

* lock / unlock a user (the lock stage; a lock suspends sessions and blocks logins, an unlock brings
  the same sessions back),
* list and finish a user's sessions (compatibility, OAuth 2.0 and browser sessions), so an unlock
  does not revive them (LIF-07),
* deactivate a user (irreversible, the erase stage), with or without GDPR erasure (D7).

Older MAS releases lack some of these operations: 0.14 has no session finish endpoints and ignores
the deactivation body, so it always erases. :meth:`ApiClientMasAdmin.capabilities` reads the admin
API's own OpenAPI document to tell, so callers never send a request whose effect they cannot know.

Auth is an OAuth2 ``client_credentials`` token carrying the ``urn:mas:admin`` scope (the bot's MAS
admin client must be listed in MAS ``policy.data.admin_clients``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from onbot.auth.token_provider import TokenProvider
from onbot.clients.base import ApiError, BaseApiClient
from onbot.logging import get_logger

log = get_logger(__name__)

#: The session kinds a user can hold in MAS, as named in the admin API paths.
SESSION_KINDS: tuple[str, ...] = ("compat-sessions", "oauth2-sessions", "user-sessions")
_SESSION_PAGE_SIZE = 100
_DEACTIVATE_PATH = "/api/admin/v1/users/{id}/deactivate"


@dataclass(frozen=True, slots=True)
class MasCapabilities:
    """What this MAS admin API can do, read from its OpenAPI document (``/api/spec.json``)."""

    #: Session kinds (see :data:`SESSION_KINDS`) with a ``POST .../{id}/finish`` endpoint.
    finishable_sessions: frozenset[str] = frozenset()
    #: Whether ``POST users/{id}/deactivate`` takes ``{"skip_erase": ...}``. Without it MAS always
    #: asks the homeserver to GDPR-erase the user and silently ignores the body.
    deactivate_skip_erase: bool = False
    #: ``False`` when the document could not be read; every capability then counts as missing.
    known: bool = True

    @property
    def missing_finish(self) -> list[str]:
        return [kind for kind in SESSION_KINDS if kind not in self.finishable_sessions]

    def describe(self) -> str:
        if not self.known:
            return "unknown (the admin API description /api/spec.json could not be read)"
        missing = self.missing_finish
        finish = "all session kinds" if not missing else f"missing for {', '.join(missing)}"
        erase = "yes" if self.deactivate_skip_erase else "no (MAS always GDPR-erases)"
        return f"session finish: {finish}; deactivation without GDPR erase: {erase}"


def parse_capabilities(spec: Any) -> MasCapabilities:
    """Read :class:`MasCapabilities` from the admin API's OpenAPI document (pure)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        return MasCapabilities(known=False)
    paths: dict[str, Any] = spec["paths"]
    finishable = set()
    for kind in SESSION_KINDS:
        operations = paths.get(f"/api/admin/v1/{kind}/{{id}}/finish")
        if isinstance(operations, dict) and "post" in operations:
            finishable.add(kind)
    return MasCapabilities(
        finishable_sessions=frozenset(finishable),
        deactivate_skip_erase=_deactivate_takes_skip_erase(spec),
    )


def _deactivate_takes_skip_erase(spec: dict[str, Any]) -> bool:
    operations = spec["paths"].get(_DEACTIVATE_PATH)
    operation = operations.get("post") if isinstance(operations, dict) else None
    body = operation.get("requestBody") if isinstance(operation, dict) else None
    if not isinstance(body, dict):
        return False
    schema = body.get("content", {}).get("application/json", {}).get("schema", {})
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        schema = spec.get("components", {}).get("schemas", {}).get(ref.rsplit("/", 1)[-1], {})
    properties = schema.get("properties") if isinstance(schema, dict) else None
    return isinstance(properties, dict) and "skip_erase" in properties


def mxid_localpart(mxid: str) -> str:
    """Extract the localpart from a full MXID (``@local:server`` -> ``local``).

    MXIDs keep their ``:server`` component, so splitting on ``:`` is correct here. This must never be
    used on a room *ID*: since room version 12 a room ID is a domainless hash with no ``:`` to split
    (``docs/adr/0011-room-version-12.md``).
    """
    return mxid.removeprefix("@").split(":", 1)[0]


def _is_mas_error_body(payload: Any) -> bool:
    """Whether a response body is a MAS admin API error document (``{"errors": [{"title": ...}]}``)."""
    return isinstance(payload, dict) and isinstance(payload.get("errors"), list)


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
        self.spec_url = f"{mas_url.rstrip('/')}/api/spec.json"
        self._capabilities: MasCapabilities | None = None
        super().__init__(base_url=base, auth_token=access_token, token_provider=token_provider, **kwargs)

    async def capabilities(self) -> MasCapabilities:
        """What this MAS can do (cached after the first successful read).

        A document that cannot be read gives ``known=False`` (every capability missing) and is read
        again on the next call, so a transient error does not stick for the life of the process.
        """
        if self._capabilities is not None:
            return self._capabilities
        try:
            spec = await self.get_json(self.spec_url)
        except Exception as exc:
            log.warning("could not read the MAS admin API description at %s: %s", self.spec_url, exc)
            return MasCapabilities(known=False)
        caps = parse_capabilities(spec)
        if caps.known:
            self._capabilities = caps
        return caps

    async def get_user_id_by_username(self, username: str) -> str | None:
        """Resolve a MAS user ULID from its username (localpart), or ``None`` if MAS has no such user.

        Only MAS's own JSON "not found" answer (``{"errors": [...]}``) means "no such user". Any other
        404 (the listener does not mount the ``adminapi`` resource, or a reverse proxy does not route
        ``/api/admin``) is a deployment error and raises, so it can never pass for "nothing to do"
        (LIF-10).
        """
        try:
            result = await self.get_json(f"users/by-username/{username}")
        except ApiError as exc:
            if exc.status_code == 404 and _is_mas_error_body(exc.payload):
                return None
            raise
        user_id: str = result["data"]["id"]
        return user_id

    async def lock_user(self, user_id: str) -> None:
        """Lock the user: suspends active sessions and blocks new logins (reversible, idempotent).

        Per the MAS admin API a lock does not end sessions: they work again after :meth:`unlock_user`
        unless they were finished (:meth:`finish_session`).
        """
        # Locking a locked user (and unlocking an unlocked one) answers 200, so retries are safe.
        await self.post_json(f"users/{user_id}/lock", idempotent=True)

    async def unlock_user(self, user_id: str) -> None:
        await self.post_json(f"users/{user_id}/unlock", idempotent=True)

    async def count_locked_users(self) -> int:
        """How many MAS users are locked (``meta.count`` of the filtered user list)."""
        page = await self.get_json("users", params={"filter[status]": "locked", "page[first]": 1})
        return int(page["meta"]["count"])

    async def list_active_session_ids(self, kind: str, user_id: str) -> list[str]:
        """Ids of the user's active sessions of one kind (see :data:`SESSION_KINDS`), every page."""
        ids: list[str] = []
        params: dict[str, Any] = {
            "filter[user]": user_id,
            "filter[status]": "active",
            "page[first]": _SESSION_PAGE_SIZE,
        }
        while True:
            page = await self.get_json(kind, params=params)
            batch = [str(item["id"]) for item in page.get("data", [])]
            ids.extend(batch)
            if len(batch) < _SESSION_PAGE_SIZE:
                return ids
            params = {**params, "page[after]": batch[-1]}

    async def finish_session(self, kind: str, session_id: str) -> bool:
        """End one session for good; ``False`` when MAS says it was already finished (a race)."""
        try:
            await self.post_json(f"{kind}/{session_id}/finish")
        except ApiError as exc:
            if exc.status_code == 400 and _is_mas_error_body(exc.payload):
                return False
            raise
        return True

    async def deactivate_user(self, user_id: str, *, skip_erase: bool | None = None) -> None:
        """Deactivate the user: ends every session and permanently disables the account.

        ``skip_erase`` is sent only when given. A MAS without that option ignores the body and erases
        anyway, so callers check :meth:`capabilities` first.
        """
        body = None if skip_erase is None else {"skip_erase": skip_erase}
        await self.post_json(f"users/{user_id}/deactivate", json_body=body)
