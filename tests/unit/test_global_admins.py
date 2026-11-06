"""Unit tests for resolving ``global_room_admins`` (every drop path is a fail-closed guarantee)."""

from typing import Any

import pytest

from onbot.config import OnbotConfig
from onbot.models import MappedUser
from onbot.reconciler.global_admins import GlobalRoomAdminResolver

_BASE = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
    "admin_room": {"enabled": False},
}

ALL_MATRIX = {"@alice:company.org", "@bob:company.org", "@bot:company.org"}


class FakeAuthentik:
    """Answers with everyone, ignoring the filters — the case the resolver must not trust."""

    def __init__(self, users: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.users = users if users is not None else [{"username": "bob", "pk": "bob-pk"}]
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("authentik down")
        return self.users


def _config(**overrides: Any) -> OnbotConfig:
    return OnbotConfig.model_validate({**_BASE, **overrides})


def _mapped(username: str, mxid: str) -> MappedUser:
    return MappedUser(authentik_obj={"username": username, "pk": f"{username}-pk"}, mxid=mxid)


async def test_empty_config_resolves_to_nothing_without_calling_authentik() -> None:
    api = FakeAuthentik()
    resolver = GlobalRoomAdminResolver(api, _config())  # type: ignore[arg-type]
    assert await resolver.resolve([_mapped("alice", "@alice:company.org")], ALL_MATRIX) == frozenset()
    assert api.calls == []


async def test_admin_already_in_the_synced_set_costs_no_extra_request() -> None:
    api = FakeAuthentik()
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["alice"]))  # type: ignore[arg-type]
    result = await resolver.resolve([_mapped("alice", "@alice:company.org")], ALL_MATRIX)
    assert result == frozenset({"@alice:company.org"})
    assert api.calls == []


async def test_admin_outside_the_synced_set_is_looked_up() -> None:
    api = FakeAuthentik([{"username": "bob", "pk": "bob-pk"}])
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["bob"]))  # type: ignore[arg-type]
    result = await resolver.resolve([_mapped("alice", "@alice:company.org")], ALL_MATRIX)
    assert result == frozenset({"@bob:company.org"})
    assert api.calls == [{"filter_by_username": "bob", "filter_is_active": True}]


async def test_username_is_rechecked_locally_when_the_server_ignored_the_filter() -> None:
    """A server that drops the `username` param answers with everyone; we must not take the first."""
    api = FakeAuthentik([{"username": "alice", "pk": "a"}, {"username": "bob", "pk": "b"}])
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["bob"]))  # type: ignore[arg-type]
    assert await resolver.resolve([], ALL_MATRIX) == frozenset({"@bob:company.org"})


async def test_unknown_username_grants_nothing() -> None:
    api = FakeAuthentik([{"username": "alice", "pk": "a"}])
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["nobody"]))  # type: ignore[arg-type]
    assert await resolver.resolve([], ALL_MATRIX) == frozenset()


async def test_ambiguous_username_grants_nothing() -> None:
    api = FakeAuthentik([{"username": "bob", "pk": "b1"}, {"username": "bob", "pk": "b2"}])
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["bob"]))  # type: ignore[arg-type]
    assert await resolver.resolve([], ALL_MATRIX) == frozenset()


async def test_ignore_listed_username_grants_nothing() -> None:
    api = FakeAuthentik()
    config = _config(global_room_admins=["alice"], authentik_user_ignore_list=["alice"])
    resolver = GlobalRoomAdminResolver(api, config)  # type: ignore[arg-type]
    assert await resolver.resolve([_mapped("alice", "@alice:company.org")], ALL_MATRIX) == frozenset()
    assert api.calls == []


async def test_admin_without_a_matrix_account_is_skipped() -> None:
    api = FakeAuthentik()
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["alice"]))  # type: ignore[arg-type]
    # alice exists in Authentik but has never logged in, so MAS never provisioned her account.
    assert (
        await resolver.resolve([_mapped("alice", "@alice:company.org")], {"@bob:company.org"}) == frozenset()
    )


async def test_bot_itself_is_never_a_global_admin() -> None:
    api = FakeAuthentik()
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["bot"]))  # type: ignore[arg-type]
    assert await resolver.resolve([_mapped("bot", "@bot:company.org")], ALL_MATRIX) == frozenset()


async def test_unmappable_user_grants_nothing() -> None:
    api = FakeAuthentik([{"pk": "b", "username": "bob"}])
    config = _config(
        global_room_admins=["bob"],
        sync_authentik_users_with_matrix_rooms={"authentik_username_mapping_attribute": "attributes.mx"},
    )
    resolver = GlobalRoomAdminResolver(api, config)  # type: ignore[arg-type]
    assert await resolver.resolve([], ALL_MATRIX) == frozenset()


async def test_authentik_failure_drops_only_the_unresolvable_name() -> None:
    api = FakeAuthentik(fail=True)
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["alice", "bob"]))  # type: ignore[arg-type]
    # alice comes from the synced set; only bob needs the lookup that fails.
    result = await resolver.resolve([_mapped("alice", "@alice:company.org")], ALL_MATRIX)
    assert result == frozenset({"@alice:company.org"})


async def test_a_failed_lookup_keeps_the_previous_resolution_of_that_name() -> None:
    """REC-06: one Authentik 503 must not kick and demote a global admin in every room."""
    api = FakeAuthentik([{"username": "bob", "pk": "bob-pk"}])
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["bob"]))  # type: ignore[arg-type]
    assert await resolver.resolve([], ALL_MATRIX) == frozenset({"@bob:company.org"})

    api.fail = True
    assert await resolver.resolve([], ALL_MATRIX) == frozenset({"@bob:company.org"})


async def test_a_failed_lookup_does_not_keep_a_name_that_left_the_config() -> None:
    api = FakeAuthentik([{"username": "bob", "pk": "bob-pk"}])
    config = _config(global_room_admins=["bob"])
    resolver = GlobalRoomAdminResolver(api, config)  # type: ignore[arg-type]
    await resolver.resolve([], ALL_MATRIX)

    config.global_room_admins = ["carol"]
    api.fail = True
    assert await resolver.resolve([], ALL_MATRIX) == frozenset()


async def test_a_name_that_resolved_to_nothing_is_not_revived_by_a_later_failure() -> None:
    """Only a transient failure keeps the old answer; a real "not found" is an answer too."""
    api = FakeAuthentik([{"username": "bob", "pk": "bob-pk"}])
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["bob"]))  # type: ignore[arg-type]
    await resolver.resolve([], ALL_MATRIX)
    api.users = []  # bob was disabled or deleted upstream
    assert await resolver.resolve([], ALL_MATRIX) == frozenset()
    api.fail = True
    assert await resolver.resolve([], ALL_MATRIX) == frozenset()


async def test_mxids_property_exposes_the_last_resolved_set() -> None:
    api = FakeAuthentik()
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=["alice"]))  # type: ignore[arg-type]
    assert resolver.mxids == frozenset()
    await resolver.resolve([_mapped("alice", "@alice:company.org")], ALL_MATRIX)
    assert resolver.mxids == frozenset({"@alice:company.org"})


@pytest.mark.parametrize("names", [["alice"], []])
async def test_resolve_is_idempotent(names: list[str]) -> None:
    api = FakeAuthentik()
    resolver = GlobalRoomAdminResolver(api, _config(global_room_admins=names))  # type: ignore[arg-type]
    users = [_mapped("alice", "@alice:company.org")]
    first = await resolver.resolve(users, ALL_MATRIX)
    assert await resolver.resolve(users, ALL_MATRIX) == first
