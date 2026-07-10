"""Who may command the bot: the union of the config list and an Authentik group, and its refresh."""

from __future__ import annotations

from typing import Any

import pytest

from onbot.admin.admins import AdminResolver, resolve_admin_mxids
from onbot.config import AdminRoom, AuthentikServer, OnbotConfig, SynapseServer

BOT = "@bot:matrix.test"
ADMIN = "@admin:matrix.test"  # a Matrix-only account, named by hand
GROUP = "group-pk-1"


def _config(
    *,
    admin_user_ids: list[str] | None = None,
    group_pks: list[str] | None = None,
    authentik_user_ignore_list: list[str] | None = None,
    matrix_user_ignore_list: list[str] | None = None,
    username_attribute: str = "username",
) -> OnbotConfig:
    config = OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id=BOT,
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        admin_room=AdminRoom(
            enabled=True,
            admin_user_ids=admin_user_ids if admin_user_ids is not None else [ADMIN],
            authentik_group_pks_granting_bot_admin=group_pks or [],
        ),
        authentik_user_ignore_list=authentik_user_ignore_list or [],
        matrix_user_ignore_list=matrix_user_ignore_list or [],
    )
    config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute = username_attribute
    return config


def _user(username: str, **extra: Any) -> dict[str, Any]:
    return {"pk": username, "username": username, **extra}


class _FakeAuthentik:
    """Records the filters it was asked for; can be told to fail."""

    def __init__(self, *users: dict[str, Any]) -> None:
        self.users = list(users)
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("authentik is down")
        return list(self.users)


def _resolver(authentik: _FakeAuthentik, config: OnbotConfig, **kwargs: Any) -> AdminResolver:
    return AdminResolver(authentik, config, **kwargs)  # type: ignore[arg-type]


# --- the pure resolution ---------------------------------------------------


def test_the_admin_set_is_the_union_of_the_config_list_and_the_group() -> None:
    admins = resolve_admin_mxids(_config(), [_user("alice"), _user("bob")])

    assert admins == frozenset({ADMIN, "@alice:matrix.test", "@bob:matrix.test"})


def test_a_user_the_bot_cannot_map_is_dropped_rather_than_granted_or_raised() -> None:
    # An MXID we would have to guess at must never receive the bot's widest capability — and one
    # malformed Authentik account must not take the control room down with it.
    config = _config(username_attribute="attributes.matrix_name")
    members = [_user("alice", attributes={"matrix_name": "alice"}), _user("bob", attributes={})]

    admins = resolve_admin_mxids(config, members)

    assert admins == frozenset({ADMIN, "@alice:matrix.test"})


def test_an_ignored_authentik_user_parked_in_the_admin_group_is_not_an_admin() -> None:
    config = _config(authentik_user_ignore_list=["service-account"])

    admins = resolve_admin_mxids(config, [_user("service-account"), _user("alice")])

    assert admins == frozenset({ADMIN, "@alice:matrix.test"})


def test_the_matrix_ignore_list_does_not_apply_here() -> None:
    # matrix_user_ignore_list names the Matrix-only accounts admin_user_ids exists for; applying it
    # would make it impossible to name a break-glass admin the reconciler is told to leave alone.
    config = _config(matrix_user_ignore_list=[ADMIN])

    assert ADMIN in resolve_admin_mxids(config, [])


def test_an_empty_union_means_nobody_may_command_the_bot() -> None:
    assert resolve_admin_mxids(_config(admin_user_ids=[]), []) == frozenset()


# --- fetching and refreshing -----------------------------------------------


async def test_a_group_is_read_from_authentik_and_only_active_members_are_asked_for() -> None:
    authentik = _FakeAuthentik(_user("alice"))
    resolver = _resolver(authentik, _config(group_pks=[GROUP]))

    assert await resolver.admins() == frozenset({ADMIN, "@alice:matrix.test"})
    # A disabled Authentik account must not keep its commands; pinned so a change to list_users'
    # default cannot silently re-admit one.
    assert authentik.calls == [{"filter_groups_by_pk": [GROUP], "filter_is_active": True}]


async def test_without_a_configured_group_authentik_is_never_called() -> None:
    authentik = _FakeAuthentik()
    resolver = _resolver(authentik, _config())

    assert await resolver.admins() == frozenset({ADMIN})
    assert authentik.calls == []


async def test_the_explicit_list_still_works_with_authentik_unreachable() -> None:
    # The floor: admin_user_ids needs no Authentik call, so it survives the identity provider being
    # down — which is exactly when an operator needs the control room.
    authentik = _FakeAuthentik()
    authentik.fail = True
    resolver = _resolver(authentik, _config(group_pks=[GROUP]))

    assert await resolver.admins() == frozenset({ADMIN})


async def test_the_set_is_cached_for_the_ttl_and_refetched_after_it() -> None:
    now = 1000.0
    authentik = _FakeAuthentik(_user("alice"))
    resolver = _resolver(authentik, _config(group_pks=[GROUP]), ttl_sec=60, clock=lambda: now)

    await resolver.admins()
    await resolver.admins()
    assert len(authentik.calls) == 1

    now += 60
    await resolver.admins()
    assert len(authentik.calls) == 2


async def test_the_ttl_defaults_to_the_authentik_poll_interval() -> None:
    """Not the reconcile interval: that is a slow Matrix-side drift repair, and binding revocation
    of bot admin to it would let a removed admin keep issuing commands for minutes."""
    config = _config(group_pks=[GROUP])
    config.server_tick_rate_sec = 900
    config.authentik_poll_rate_sec = 42

    assert _resolver(_FakeAuthentik(), config).ttl_sec == 42


async def test_the_ttl_falls_back_to_the_reconcile_interval_without_a_poll() -> None:
    config = _config(group_pks=[GROUP])
    config.server_tick_rate_sec = 900
    config.authentik_poll_rate_sec = 0

    assert _resolver(_FakeAuthentik(), config).ttl_sec == 900


async def test_losing_group_membership_revokes_command_access_on_the_next_refresh() -> None:
    now = 1000.0
    authentik = _FakeAuthentik(_user("alice"))
    resolver = _resolver(authentik, _config(group_pks=[GROUP]), ttl_sec=60, clock=lambda: now)
    assert "@alice:matrix.test" in await resolver.admins()

    authentik.users = []  # alice is removed from the Authentik group
    now += 60

    assert await resolver.admins() == frozenset({ADMIN})


async def test_a_failed_refresh_keeps_the_previous_set() -> None:
    # Never fall open to a wider set, and never fall closed to an empty one: an Authentik blip must
    # not lock the operators out of their own control room.
    now = 1000.0
    authentik = _FakeAuthentik(_user("alice"))
    resolver = _resolver(authentik, _config(group_pks=[GROUP]), ttl_sec=60, clock=lambda: now)
    granted = await resolver.admins()

    authentik.fail = True
    now += 600

    assert await resolver.admins() == granted


async def test_a_recovered_authentik_resumes_refreshing() -> None:
    now = 1000.0
    authentik = _FakeAuthentik(_user("alice"))
    authentik.fail = True
    resolver = _resolver(authentik, _config(group_pks=[GROUP]), ttl_sec=60, clock=lambda: now)
    assert await resolver.admins() == frozenset({ADMIN})

    authentik.fail = False
    now += 60

    assert await resolver.admins() == frozenset({ADMIN, "@alice:matrix.test"})


@pytest.mark.parametrize("failing_first", [True, False])
async def test_the_set_is_never_wider_than_what_authentik_last_said(failing_first: bool) -> None:
    authentik = _FakeAuthentik(_user("alice"))
    authentik.fail = failing_first
    resolver = _resolver(authentik, _config(admin_user_ids=[], group_pks=[GROUP]))

    admins = await resolver.admins()

    assert admins == (frozenset() if failing_first else frozenset({"@alice:matrix.test"}))
