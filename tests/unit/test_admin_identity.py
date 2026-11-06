"""Bot admins and global room admins resolve through the identity resolver (D8, MNT-13, SEC-07)."""

from __future__ import annotations

from typing import Any

from onbot.admin.admins import AdminResolver
from onbot.config import OnbotConfig
from onbot.identity import IdentityResolver
from onbot.models import MappedUser
from onbot.reconciler.global_admins import GlobalRoomAdminResolver
from tests.unit.test_identity_resolver import MemoryBindings, mx

_BASE: dict[str, Any] = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
    "admin_room": {"enabled": True, "admin_user_ids": [], "authentik_group_pks_granting_bot_admin": ["g"]},
}


class FakeAuthentik:
    def __init__(self, *users: dict[str, Any]) -> None:
        self.users = list(users)

    async def list_users(self, **_: Any) -> list[dict[str, Any]]:
        return [dict(u) for u in self.users]


def _config(**overrides: Any) -> OnbotConfig:
    return OnbotConfig.model_validate({**_BASE, **overrides})


async def test_a_renamed_bot_admin_keeps_the_right_on_their_account() -> None:
    config = _config()
    identity = IdentityResolver(config, store=MemoryBindings({"1": mx("alice")}))
    api = FakeAuthentik({"pk": 1, "username": "alicia"})
    resolver = AdminResolver(api, config, identity=identity)  # type: ignore[arg-type]
    assert await resolver.refresh() == {mx("alice")}


async def test_an_admin_group_member_naming_somebody_elses_account_gets_nothing() -> None:
    """SEC-07: before, bob (renamed to "alice") was granted bot admin on alice's account."""
    config = _config()
    identity = IdentityResolver(config, store=MemoryBindings({"1": mx("alice")}))
    api = FakeAuthentik({"pk": 2, "username": "alice"})
    resolver = AdminResolver(api, config, identity=identity)  # type: ignore[arg-type]
    assert await resolver.refresh() == frozenset()


async def test_a_global_room_admin_looked_up_resolves_through_the_binding() -> None:
    config = _config(global_room_admins=["alicia"])
    identity = IdentityResolver(config, store=MemoryBindings({"1": mx("alice")}))
    api = FakeAuthentik({"pk": 1, "username": "alicia"})
    resolver = GlobalRoomAdminResolver(api, config, identity=identity)  # type: ignore[arg-type]
    assert await resolver.resolve([], {mx("alice")}) == {mx("alice")}


async def test_a_global_room_admin_in_the_synced_set_keeps_the_resolved_account() -> None:
    config = _config(global_room_admins=["alicia"])
    api = FakeAuthentik()
    resolver = GlobalRoomAdminResolver(api, config)  # type: ignore[arg-type]
    mapped = MappedUser(authentik_obj={"pk": 1, "username": "alicia"}, mxid=mx("alice"))
    assert await resolver.resolve([mapped], {mx("alice"), mx("alicia")}) == {mx("alice")}


async def test_a_global_room_admin_on_the_matrix_ignore_list_is_not_granted() -> None:
    """MNT-13: the ignore list promises the bot never changes that account's power level."""
    config = _config(global_room_admins=["alice"], matrix_user_ignore_list=[mx("alice")])
    api = FakeAuthentik({"pk": 1, "username": "alice"})
    resolver = GlobalRoomAdminResolver(api, config)  # type: ignore[arg-type]
    assert await resolver.resolve([], {mx("alice")}) == frozenset()
