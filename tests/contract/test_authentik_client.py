"""Contract tests for the Authentik client (pagination + client-side filtering)."""

import httpx
import respx

from onbot.clients.authentik import ApiClientAuthentik


@respx.mock
async def test_list_users_follows_pagination() -> None:
    route = respx.get("https://authentik.test/api/v3/core/users/").mock(
        side_effect=[
            httpx.Response(200, json={"pagination": {"next": 2}, "results": [{"username": "a"}]}),
            httpx.Response(200, json={"pagination": {"next": 0}, "results": [{"username": "b"}]}),
        ]
    )
    client = ApiClientAuthentik(url="https://authentik.test", api_key="k")
    try:
        users = await client.list_users()
    finally:
        await client.aclose()
    assert [u["username"] for u in users] == ["a", "b"]
    assert route.call_count == 2
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
async def test_list_groups_strips_inactive_and_filters_attributes() -> None:
    respx.get("https://authentik.test/api/v3/core/groups/").mock(
        return_value=httpx.Response(
            200,
            json={
                "pagination": {"next": 0},
                "results": [
                    {
                        "pk": "g1",
                        "name": "Team",
                        "attributes": {"chat-powerlevel": 50},
                        "users_obj": [
                            {"pk": 1, "is_active": True},
                            {"pk": 2, "is_active": False},
                        ],
                    },
                    {"pk": "g2", "name": "Other", "attributes": {}, "users_obj": []},
                ],
            },
        )
    )
    client = ApiClientAuthentik(url="https://authentik.test", api_key="k")
    try:
        groups = await client.list_groups(filter_has_non_empty_attributes=["chat-powerlevel"])
    finally:
        await client.aclose()

    assert [g["pk"] for g in groups] == ["g1"]  # g2 lacks the attribute
    assert groups[0]["users_obj"] == [{"pk": 1, "is_active": True}]  # inactive removed
