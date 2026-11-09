"""Synapse Admin API client (async, paginated).

Ported from legacy ``onbot/api_client_synapse_admin.py`` onto the async :class:`BaseApiClient`,
fixing the BATTLE_PLAN §3 bugs:

* full pagination on users/rooms/media (legacy read only page 1, ``limit=None`` was a no-op);
* ``delete_room`` now actually sends its body (legacy built it then discarded it), and uses the
  asynchronous v2 API, so a long purge is neither cut off by the client timeout nor repeated;
* ``room_is_blocked`` returns a plain bool instead of string-matching.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from onbot.auth.token_provider import TokenProvider
from onbot.clients.base import (
    ApiError,
    BaseApiClient,
    may_have_been_processed,
    path_segment,
    resend_unless_done,
)
from onbot.logging import get_logger

log = get_logger(__name__)

_DEFAULT_PAGE_SIZE = 100

NextParams = Callable[[Any, dict[str, Any]], dict[str, Any] | None]

# How often and how long ``delete_room`` waits for Synapse's background deletion.
DELETE_POLL_INTERVAL_SEC = 2.0
DELETE_WAIT_SEC = 600.0
# How often, and how far apart, the room's delete status is read again when it answers 404 right
# after a DELETE whose answer was lost. Synapse lists a room's deletions by status and leaves out the
# ones still "scheduled", so a deletion the scheduler has not started yet is invisible for a moment.
DELETE_STATUS_LOOKUPS = 5
DELETE_STATUS_RETRY_SEC = 1.0
# Delete statuses that end a deletion (the others: scheduled, active, and the older
# shutting_down and purging).
_DELETE_DONE = "complete"
_DELETE_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RoomBlock:
    """A room's block status: ``by`` is the account that set the block (``None`` when unknown)."""

    blocked: bool
    by: str | None = None


class RoomDeletionFailed(RuntimeError):
    """Synapse reported the room deletion as failed."""


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
        access_token: str | None = None,
        *,
        admin_api_path: str = "_synapse/admin",
        token_provider: TokenProvider | None = None,
        **kwargs: Any,
    ) -> None:
        base = f"{server_url.rstrip('/')}/{admin_api_path.strip('/')}"
        super().__init__(base_url=base, auth_token=access_token, token_provider=token_provider, **kwargs)

    # --- reads (paginated) ---------------------------------------------------

    async def list_users(self) -> list[dict[str, Any]]:
        # https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#list-accounts
        # ``guests=false`` is required under MAS/MSC3861 (Synapse rejects the endpoint's default
        # ``guests`` handling with M_INVALID_PARAM) and is harmless otherwise — there are no guest
        # accounts in the MAS topology (ADR-0006). Verified by the Phase 7b integration harness.
        return await self.paginate_collect(
            "v2/users",
            params={"limit": _DEFAULT_PAGE_SIZE, "guests": "false"},
            extract_items=lambda page: page["users"],
            next_params=_next_token_params("next_token"),
            total=lambda page: page.get("total"),
            item_key=lambda user: user["name"],
        )

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        """One account (``deactivated``, ``locked``, ...), ``None`` when Synapse does not know it.

        https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#query-user-account
        """
        try:
            user: dict[str, Any] = await self.get_json(f"v2/users/{path_segment(user_id)}")
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return user

    async def list_rooms(self, *, search_term: str | None = None) -> list[dict[str, Any]]:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#list-room-api
        return await self.paginate_collect(
            "v1/rooms",
            params={"limit": _DEFAULT_PAGE_SIZE, "search_term": search_term},
            extract_items=lambda page: page["rooms"],
            next_params=lambda page, cur: (
                {**cur, "from": page["next_batch"]} if page.get("next_batch") is not None else None
            ),
            total=lambda page: page.get("total_rooms"),
            item_key=lambda room: room["room_id"],
        )

    async def list_room_members(self, room_id: str) -> list[str]:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#room-members-api
        result = await self.get_json(f"v1/rooms/{path_segment(room_id)}/members")
        members: list[str] = result["members"]
        return members

    async def get_room_state_events(self, room_id: str) -> list[dict[str, Any]]:
        """Every current state event of a room, whether or not the bot is a member.

        https://element-hq.github.io/synapse/latest/admin_api/rooms.html#room-state-api
        """
        result = await self.get_json(f"v1/rooms/{path_segment(room_id)}/state")
        events: list[dict[str, Any]] = list((result or {}).get("state") or [])
        return events

    async def get_room_details(self, room_id: str) -> dict[str, Any]:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#room-details-api
        details: dict[str, Any] = await self.get_json(f"v1/rooms/{path_segment(room_id)}")
        return details

    # --- room membership / state --------------------------------------------

    async def add_user_to_room(self, room_id: str, user_id: str) -> None:
        # https://element-hq.github.io/synapse/latest/admin_api/room_membership.html
        path = f"v1/join/{path_segment(room_id)}"
        body = {"user_id": user_id}
        try:
            await self.post_json(path, json_body=body)
        except (ApiError, httpx.TransportError) as exc:
            # A second join of a joined user fails with 403 "already in the room", so after a failure
            # that may hide a success, look at the members before sending it again (RUN-05).
            async def joined() -> bool:
                return user_id in await self.list_room_members(room_id)

            await resend_unless_done(
                exc,
                done=joined,
                resend=lambda: self.post_json(path, json_body=body),
                what=f"joining {user_id} to {room_id}",
            )

    async def override_ratelimit(
        self, user_id: str, *, messages_per_second: int = 0, burst_count: int = 0
    ) -> None:
        """Replace a user's message send-rate limits. Both values ``0`` means "no limit at all".

        The bot fans a broadcast out across every managed direct room, which Synapse's per-user send
        limiter reads as one user flooding hundreds of rooms.
        https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#override-ratelimiting-for-users
        """
        await self.post_json(
            f"v1/users/{path_segment(user_id)}/override_ratelimit",
            json_body={"messages_per_second": messages_per_second, "burst_count": burst_count},
            idempotent=True,  # sets absolute values; repeating it changes nothing
        )

    async def room_is_blocked(self, room_id: str) -> bool:
        return (await self.get_room_block(room_id)).blocked

    async def get_room_block(self, room_id: str) -> RoomBlock:
        """Whether ``room_id`` is blocked, and by whom (REC-11).

        Synapse records the account that set the block and returns it as ``user_id``; the bot uses it
        to tell its own blocks (teardown) from a server admin's.
        https://element-hq.github.io/synapse/latest/admin_api/rooms.html#get-block-status
        """
        result = await self.get_json(f"v1/rooms/{path_segment(room_id)}/block")
        blocked = bool(result.get("block"))
        by = result.get("user_id")
        return RoomBlock(blocked=blocked, by=by if blocked and isinstance(by, str) else None)

    async def room_set_blocked(self, room_id: str, *, blocked: bool) -> None:
        # https://element-hq.github.io/synapse/latest/admin_api/rooms.html#block-or-unblock-a-room
        await self.put_json(f"v1/rooms/{path_segment(room_id)}/block", json_body={"block": blocked})

    # --- lifecycle (used by Phase 5; correct now to avoid carrying broken stubs) ---

    async def deactivate_account(self, user_id: str, *, erase: bool) -> None:
        # https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#deactivate-account
        await self.post_json(f"v1/deactivate/{path_segment(user_id)}", json_body={"erase": erase})

    async def logout_account(self, user_id: str) -> int:
        """Delete every device of the user, which ends their Synapse sessions; return the count.

        Under MAS this does not end the MAS session behind a token (§7 Q1).
        """
        devices = (await self.get_json(f"v2/users/{path_segment(user_id)}/devices"))["devices"]
        for device in devices:
            await self.delete_json(
                f"v2/users/{path_segment(user_id)}/devices/{path_segment(device['device_id'])}"
            )
        return len(devices)

    async def delete_user_media(self, user_id: str, *, limit: int = 100) -> dict[str, Any]:
        """Delete one batch (at most ``limit``) of the media the user uploaded.

        Synapse deletes only one page per request (100 files unless ``limit`` says otherwise), so a
        caller that wants everything gone loops (LIF-12, see :meth:`count_user_media`).
        https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#delete-media-uploaded-by-a-user
        """
        result: dict[str, Any] = await self.request_json(
            "DELETE", f"v1/users/{path_segment(user_id)}/media", params={"limit": limit}
        )
        return result

    async def count_user_media(self, user_id: str) -> int:
        """How many local media files the user uploaded (the ``total`` of the media list)."""
        # https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#list-media-uploaded-by-a-user
        result = await self.get_json(f"v1/users/{path_segment(user_id)}/media", params={"limit": 1})
        return int(result.get("total", 0))

    async def delete_room(
        self,
        room_id: str,
        *,
        block: bool = False,
        purge: bool = True,
        force_purge: bool = False,
        message: str | None = None,
        poll_interval_sec: float = DELETE_POLL_INTERVAL_SEC,
        wait_sec: float = DELETE_WAIT_SEC,
    ) -> dict[str, Any]:
        """Delete a room through the asynchronous v2 API and wait until Synapse reports the outcome.

        v1 ran the whole purge inside the request: a purge longer than the client timeout ended in a
        read timeout, and the automatic retry failed while the first deletion went on, so a done
        teardown was reported as failed. v2 answers at once with a delete id, which is polled.

        The DELETE is not retried blindly. When it fails in a way that may hide a success, or with
        400 because a deletion of this room is already running (an earlier pass, or a timed-out
        attempt), the room's own delete status is read and that deletion is awaited instead.
        Returns the final status; a deletion still running after ``wait_sec`` is left to Synapse
        (logged, not an error). Raises :class:`RoomDeletionFailed` when Synapse reports a failure.
        https://element-hq.github.io/synapse/latest/admin_api/rooms.html#version-2-new-version
        """
        body: dict[str, Any] = {"block": block, "purge": purge, "force_purge": force_purge}
        if message:
            body["message"] = message
        try:
            started = await self.request_json(
                "DELETE", f"v2/rooms/{path_segment(room_id)}", json_body=body, idempotent=False
            )
            delete_id = str(started["delete_id"])
        except (ApiError, httpx.TransportError) as exc:
            already_running = isinstance(exc, ApiError) and exc.status_code == 400
            if not (already_running or may_have_been_processed(exc)):
                raise
            existing = await self._existing_room_deletion(room_id)
            if existing is None:
                raise
            log.warning(
                "deleting room %s failed (%s), but a deletion of it is under way; waiting for it",
                room_id,
                exc,
            )
            delete_id = existing
        return await self._wait_for_room_deletion(room_id, delete_id, poll_interval_sec, wait_sec)

    async def _existing_room_deletion(self, room_id: str) -> str | None:
        """The delete id of a deletion of ``room_id`` that is running or done, if Synapse knows one.

        Synapse lists a room's deletions only once they are active, complete or failed, not while
        they are still scheduled. Right after a DELETE whose answer was lost, the deletion it started
        can be in exactly that moment, so a 404 ("No delete task for room_id") is asked again a few
        times, :data:`DELETE_STATUS_RETRY_SEC` apart, before it counts as "no deletion".
        """
        sleep = self._sleep or asyncio.sleep
        for attempt in range(DELETE_STATUS_LOOKUPS):
            try:
                result = await self.get_json(f"v2/rooms/{path_segment(room_id)}/delete_status")
                break
            except ApiError as exc:
                if exc.status_code != 404:
                    raise
                if attempt + 1 == DELETE_STATUS_LOOKUPS:
                    return None  # "No delete task for room_id ... found"
                await sleep(DELETE_STATUS_RETRY_SEC)
        for entry in (result or {}).get("results") or []:
            if entry.get("status") != _DELETE_FAILED and entry.get("delete_id"):
                return str(entry["delete_id"])
        return None

    async def _wait_for_room_deletion(
        self, room_id: str, delete_id: str, poll_interval_sec: float, wait_sec: float
    ) -> dict[str, Any]:
        sleep = self._sleep or asyncio.sleep
        deadline = time.monotonic() + wait_sec
        while True:
            status: dict[str, Any] = await self.get_json(f"v2/rooms/delete_status/{path_segment(delete_id)}")
            state = status.get("status")
            if state == _DELETE_DONE:
                return status
            if state == _DELETE_FAILED:
                raise RoomDeletionFailed(f"Synapse could not delete room {room_id}: {status.get('error')}")
            if time.monotonic() >= deadline:
                log.warning(
                    "deletion of room %s is still %s after %ds; Synapse finishes it on its own",
                    room_id,
                    state,
                    wait_sec,
                )
                return status
            await sleep(poll_interval_sec)
