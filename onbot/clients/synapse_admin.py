"""Synapse Admin API client (async, paginated).

Ported from legacy ``onbot/api_client_synapse_admin.py`` onto the async :class:`BaseApiClient`,
fixing the BATTLE_PLAN §3 bugs:

* full pagination on users/rooms/media (legacy read only page 1, ``limit=None`` was a no-op);
* ``delete_room`` now actually sends its body (legacy built it then discarded it);
* ``set_room_admin`` / ``set_user_server_admin_state`` call the correct endpoints (legacy did a
  stray GET / raised ``NotImplementedError``);
* ``room_is_blocked`` returns a plain bool instead of string-matching.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from onbot.clients.base import BaseApiClient
from onbot.logging import get_logger

log = get_logger(__name__)

_DEFAULT_PAGE_SIZE = 100

NextParams = Callable[[Any, dict[str, Any]], dict[str, Any] | None]


def _next_token_params(token_key: str) -> NextParams:
    """Build a ``next_params`` callback for endpoints that page via an opaque ``next_token``."""

    def _next(page: Any, current: dict[str, Any]) -> dict[str, Any] | None:
        token = (page or {}).get(token_key)
        if token is None:
            return None
        return {**current, "from": token}

    return _next


class ApiClientSynapseAdmin(BaseApiClient):
    def __init__(
        self,
        server_url: str,
        access_token: str,
        *,
        admin_api_path: str = "_synapse/admin",
        **kwargs: Any,
    ) -> None:
        base = f"{server_url.rstrip('/')}/{admin_api_path.strip('/')}"
        super().__init__(base_url=base, auth_token=access_token, **kwargs)

    # --- reads (paginated) ---------------------------------------------------

    async def list_users(self) -> list[dict[str, Any]]:
        # https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#list-accounts
        return await self.paginate_collect(
            "v2/users",
            params={"limit": _DEFAULT_PAGE_SIZE},
            extract_items=lambda page: page["users"],
            next_params=_next_token_params("next_token"),
        )

    async def list_rooms(self, *, search_term: str | None = None) -> list[dict[str, Any]]:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#list-room-api
        return await self.paginate_collect(
            "v1/rooms",
            params={"limit": _DEFAULT_PAGE_SIZE, "search_term": search_term},
            extract_items=lambda page: page["rooms"],
            next_params=lambda page, cur: (
                {**cur, "from": page["next_batch"]} if page.get("next_batch") is not None else None
            ),
        )

    async def list_non_space_rooms(self, *, search_term: str | None = None) -> list[dict[str, Any]]:
        return [r for r in await self.list_rooms(search_term=search_term) if r.get("room_type") != "m.space"]

    async def list_spaces(self) -> list[dict[str, Any]]:
        return [r for r in await self.list_rooms() if r.get("room_type") == "m.space"]

    async def list_room_members(self, room_id: str) -> list[str]:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#room-members-api
        result = await self.get_json(f"v1/rooms/{room_id}/members")
        members: list[str] = result["members"]
        return members

    async def get_room_details(self, room_id: str) -> dict[str, Any]:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#room-details-api
        details: dict[str, Any] = await self.get_json(f"v1/rooms/{room_id}")
        return details

    async def list_user_media(self, user_id: str) -> list[dict[str, Any]]:
        # https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#list-media-uploaded-by-a-user
        return await self.paginate_collect(
            f"v1/users/{user_id}/media",
            params={"limit": _DEFAULT_PAGE_SIZE},
            extract_items=lambda page: page["media"],
            next_params=_next_token_params("next_token"),
        )

    # --- room membership / state --------------------------------------------

    async def add_user_to_room(self, room_id: str, user_id: str) -> None:
        # https://element-hq.github.io/synapse/latest/admin_api/room_membership.html
        await self.post_json(f"v1/join/{room_id}", json_body={"user_id": user_id})

    async def make_room_admin(self, room_id: str, user_id: str) -> None:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#make-room-admin-api
        await self.post_json(f"v1/rooms/{room_id}/make_room_admin", json_body={"user_id": user_id})

    async def set_user_server_admin_state(self, user_id: str, *, admin: bool) -> None:
        # https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#change-whether-a-user-is-a-server-administrator-or-not
        await self.put_json(f"v1/users/{user_id}/admin", json_body={"admin": admin})

    async def room_is_blocked(self, room_id: str) -> bool:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#get-block-status
        result = await self.get_json(f"v1/rooms/{room_id}/block")
        return bool(result.get("block"))

    async def room_set_blocked(self, room_id: str, *, blocked: bool) -> None:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#block-or-unblock-a-room
        await self.put_json(f"v1/rooms/{room_id}/block", json_body={"block": blocked})

    # --- lifecycle (used by Phase 5; correct now to avoid carrying broken stubs) ---

    async def deactivate_account(self, user_id: str, *, erase: bool) -> None:
        # https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#deactivate-account
        await self.post_json(f"v1/deactivate/{user_id}", json_body={"erase": erase})

    async def logout_account(self, user_id: str) -> None:
        # Revoke all sessions by deleting every device.
        devices = (await self.get_json(f"v2/users/{user_id}/devices"))["devices"]
        for device in devices:
            await self.delete_json(f"v2/users/{user_id}/devices/{device['device_id']}")

    async def delete_user_media(self, user_id: str) -> dict[str, Any]:
        # https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#delete-media-uploaded-by-a-user
        result: dict[str, Any] = await self.delete_json(f"v1/users/{user_id}/media")
        return result

    async def delete_room(
        self,
        room_id: str,
        *,
        block: bool = False,
        purge: bool = True,
        force_purge: bool = False,
        message: str | None = None,
    ) -> dict[str, Any]:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#delete-room-api
        body: dict[str, Any] = {"block": block, "purge": purge, "force_purge": force_purge}
        if message:
            body["message"] = message
        result: dict[str, Any] = await self.delete_json(f"v1/rooms/{room_id}", json_body=body)
        return result
