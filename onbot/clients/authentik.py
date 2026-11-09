"""Authentik API client (async, paginated).

Ported from legacy ``onbot/api_client_authentik.py`` onto the async :class:`BaseApiClient`.
Fixes carried over from BATTLE_PLAN §3: full pagination (legacy read only page 1), no stray
``print`` debugging, and typed errors via the base client.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from onbot.clients.base import BaseApiClient
from onbot.logging import get_logger
from onbot.utils import dict_has_nested_attr

log = get_logger(__name__)

_DEFAULT_PAGE_SIZE = 100


def _total(page: Any) -> int | None:
    """The entry count Authentik reports for the whole listing (``pagination.count``)."""
    count = ((page or {}).get("pagination") or {}).get("count")
    return count if isinstance(count, int) else None


def _next_page_params(page: Any, current: dict[str, Any]) -> dict[str, Any] | None:
    """Authentik paginates with ``pagination.next`` holding the next page number (0 = end)."""
    pagination = (page or {}).get("pagination", {})
    next_page = pagination.get("next")
    if not next_page:
        return None
    return {**current, "page": next_page}


class ApiClientAuthentik(BaseApiClient):
    def __init__(self, url: str, api_key: str, **kwargs: Any) -> None:
        super().__init__(base_url=f"{url.rstrip('/')}/api/v3", auth_token=api_key, **kwargs)

    async def list_users(
        self,
        *,
        filter_groups_by_name: str | Sequence[str] | None = None,
        filter_groups_by_pk: str | Sequence[str] | None = None,
        filter_by_path: str | None = None,
        filter_by_username: str | None = None,
        filter_by_attribute: str | dict[str, Any] | None = None,
        filter_is_superuser: bool | None = None,
        filter_is_active: bool | None = True,
    ) -> list[dict[str, Any]]:
        """List users (https://<authentik>/api/v3/#get-/core/users/), following all pages.

        ``filter_by_username`` is an exact match, and is only ever a *narrowing* hint: an Authentik
        that does not recognise the parameter drops it and answers with every user instead of none.
        Callers who act on the result (see :mod:`onbot.reconciler.global_admins`) must therefore
        re-check ``username`` themselves rather than trusting the server to have filtered.
        """
        if isinstance(filter_by_attribute, dict):
            filter_by_attribute = json.dumps(filter_by_attribute)
        params = {
            "groups_by_name": filter_groups_by_name,
            "groups_by_pk": filter_groups_by_pk,
            "attributes": filter_by_attribute,
            "is_superuser": filter_is_superuser,
            "is_active": filter_is_active,
            "path": filter_by_path,
            "username": filter_by_username,
            "page_size": _DEFAULT_PAGE_SIZE,
        }
        return await self.paginate_collect(
            "core/users/",
            params=params,
            extract_items=lambda page: page["results"],
            next_params=_next_page_params,
            total=_total,
            item_key=lambda user: user["pk"],
        )

    async def list_groups(
        self,
        *,
        filter_members_by_username: str | Sequence[str] | None = None,
        filter_members_by_pk: str | Sequence[str] | None = None,
        filter_by_attribute: dict[str, Any] | None = None,
        filter_is_superuser: bool | None = None,
        filter_has_attributes: Sequence[str] | None = None,
        filter_has_non_empty_attributes: Sequence[str] | None = None,
        include_inactive_users_obj: bool = False,
        include_users: bool = True,
    ) -> list[dict[str, Any]]:
        """List groups (https://<authentik>/api/v3/#get-/core/groups/), following all pages.

        ``filter_has_attributes`` / ``filter_has_non_empty_attributes`` are client-side filters on
        dotted attribute paths (Authentik's query API can't express them). Inactive users are
        stripped from each group's ``users_obj`` unless ``include_inactive_users_obj``.

        ``include_users=False`` asks Authentik to leave out ``users_obj`` (the embedded user objects of
        every member); ``users``, the members' pks, stays. A caller that only needs the pks saves most
        of the payload with it (W8R-05). An Authentik that does not know the parameter sends
        ``users_obj`` anyway, which is harmless.
        """
        params = {
            "members_by_username": filter_members_by_username,
            "members_by_pk": filter_members_by_pk,
            "attributes": (json.dumps(filter_by_attribute) if filter_by_attribute else None),
            "is_superuser": filter_is_superuser,
            "include_users": None if include_users else "false",
            "page_size": _DEFAULT_PAGE_SIZE,
        }
        groups: list[dict[str, Any]] = await self.paginate_collect(
            "core/groups/",
            params=params,
            extract_items=lambda page: page["results"],
            next_params=_next_page_params,
            total=_total,
            item_key=lambda group: group["pk"],
        )

        if not include_inactive_users_obj:
            for group in groups:
                self._remove_inactive_users_from_group(group)

        if filter_has_attributes:
            groups = [
                g
                for g in groups
                if any(
                    dict_has_nested_attr(g.get("attributes", {}), attr.split("."))
                    for attr in filter_has_attributes
                )
            ]
        if filter_has_non_empty_attributes:
            groups = [
                g
                for g in groups
                if any(
                    dict_has_nested_attr(g.get("attributes", {}), attr.split("."), must_have_val=True)
                    for attr in filter_has_non_empty_attributes
                )
            ]
        return groups

    @staticmethod
    def _remove_inactive_users_from_group(group: dict[str, Any]) -> None:
        # `users_obj` is null when the listing asked for no user objects (`include_users=false`).
        if group.get("users_obj") is not None:
            group["users_obj"] = [u for u in group["users_obj"] if u.get("is_active") is True]
