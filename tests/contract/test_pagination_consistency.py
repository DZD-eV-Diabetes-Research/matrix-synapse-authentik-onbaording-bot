"""Offset pagination that changes while it is read (RUN-14).

Authentik pages by page number and Synapse by numeric offset. An entry that disappears before the
current offset between two page reads shifts every later entry up by one, so the first entry of the
next page is never seen; with membership sync on, that user would be kicked for one pass.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from onbot.clients.authentik import ApiClientAuthentik
from onbot.clients.base import ListingChangedError
from onbot.clients.synapse_admin import ApiClientSynapseAdmin

USERS = "https://authentik.test/api/v3/core/users/"
SYNAPSE_USERS = "https://matrix.test/_synapse/admin/v2/users"


def _ak_page(count: int, nxt: int, pks: list[int]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "pagination": {"next": nxt, "count": count},
            "results": [{"pk": pk, "username": f"u{pk}"} for pk in pks],
        },
    )


def _authentik() -> ApiClientAuthentik:
    return ApiClientAuthentik(url="https://authentik.test", api_key="k")


@respx.mock
async def test_a_user_skipped_by_a_shifting_listing_is_found_by_the_reread() -> None:
    """u2 is deleted after page 1 was read, so u3 moves onto page 1 and is never seen. Before the fix
    the bot returned [u1, u2, u4] and treated u3 as gone."""
    route = respx.get(USERS).mock(
        side_effect=[
            _ak_page(4, 2, [1, 2]),
            _ak_page(3, 0, [4]),
            # The re-read sees the settled listing.
            _ak_page(3, 2, [1, 3]),
            _ak_page(3, 0, [4]),
        ]
    )
    client = _authentik()
    try:
        users = await client.list_users()
    finally:
        await client.aclose()
    assert [u["pk"] for u in users] == [1, 3, 4]
    assert route.call_count == 4


@respx.mock
async def test_a_listing_that_keeps_changing_raises_instead_of_returning_holes() -> None:
    respx.get(USERS).mock(
        side_effect=[
            _ak_page(4, 2, [1, 2]),
            _ak_page(3, 0, [4]),
            _ak_page(3, 2, [1, 3]),
            _ak_page(2, 0, []),
        ]
    )
    client = _authentik()
    try:
        with pytest.raises(ListingChangedError):
            await client.list_users()
    finally:
        await client.aclose()


@respx.mock
async def test_an_entry_read_twice_is_returned_once() -> None:
    """A user added before the offset pushes u2 onto page 2 as well; the count still matches."""
    respx.get(USERS).mock(side_effect=[_ak_page(3, 2, [1, 2]), _ak_page(3, 0, [2, 3])])
    client = _authentik()
    try:
        users = await client.list_users()
    finally:
        await client.aclose()
    assert [u["pk"] for u in users] == [1, 2, 3]


@respx.mock
async def test_a_consistent_listing_is_read_once() -> None:
    route = respx.get(USERS).mock(side_effect=[_ak_page(3, 2, [1, 2]), _ak_page(3, 0, [3])])
    client = _authentik()
    try:
        users = await client.list_users()
    finally:
        await client.aclose()
    assert [u["pk"] for u in users] == [1, 2, 3]
    assert route.call_count == 2


@respx.mock
async def test_synapse_user_listing_is_checked_against_its_total() -> None:
    def page(total: int, names: list[str], next_token: str | None) -> httpx.Response:
        body: dict[str, object] = {"users": [{"name": n} for n in names], "total": total}
        if next_token is not None:
            body["next_token"] = next_token
        return httpx.Response(200, json=body)

    route = respx.get(SYNAPSE_USERS).mock(
        side_effect=[
            page(3, ["@a:x", "@b:x"], "2"),
            page(2, [], None),  # @b was deactivated: @c moved to offset 1 and was skipped
            page(2, ["@a:x", "@c:x"], "2"),
            page(2, [], None),
        ]
    )
    admin = ApiClientSynapseAdmin(server_url="https://matrix.test", access_token="tok")
    try:
        users = await admin.list_users()
    finally:
        await admin.aclose()
    assert [u["name"] for u in users] == ["@a:x", "@c:x"]
    assert route.call_count == 4
