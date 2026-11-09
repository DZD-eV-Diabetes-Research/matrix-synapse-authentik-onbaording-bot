"""Unit tests for the Authentik discovery poll: what moves the fingerprint, what must not, and when
a change triggers an out-of-band reconcile."""

from __future__ import annotations

import asyncio
from typing import Any

from onbot.config import AdminRoom, AuthentikServer, OnbotConfig, SynapseServer
from onbot.discovery import DiscoveryPoller, fingerprint

ATTR = "username"


def _config(*, poll_sec: int = 15) -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id="@bot:matrix.test",
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        admin_room=AdminRoom(enabled=False),
        authentik_poll_rate_sec=poll_sec,
    )


def _user(pk: str = "1", **over: Any) -> dict[str, Any]:
    return {"pk": pk, "username": f"u{pk}", "is_active": True, "is_superuser": False, "groups_obj": []} | over


def _group(pk: str = "g1", **over: Any) -> dict[str, Any]:
    return {"pk": pk, "name": "chat", "is_superuser": False, "attributes": {"is_chatroom": True}} | over


class FakeAuthentik:
    """Serves whatever user/group lists the test sets, and counts the requests."""

    def __init__(self) -> None:
        self.users: list[dict[str, Any]] = []
        self.groups: list[dict[str, Any]] = []
        # Groups that grant a power level (the engine's second group query), and the users a
        # username lookup finds (the global room admins).
        self.power_level_groups: list[dict[str, Any]] = []
        self.by_username: dict[str, list[dict[str, Any]]] = {}
        self.calls = 0

    async def list_users(self, *, filter_by_username: str | None = None, **_: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if filter_by_username is not None:
            return list(self.by_username.get(filter_by_username, []))
        return list(self.users)

    async def list_groups(
        self, *, filter_has_non_empty_attributes: Any = None, **_: Any
    ) -> list[dict[str, Any]]:
        self.calls += 1
        if filter_has_non_empty_attributes is not None:
            return list(self.power_level_groups)
        return list(self.groups)


def test_fingerprint_is_order_independent() -> None:
    users = [_user("1"), _user("2")]
    groups = [_group("g1"), _group("g2")]
    assert fingerprint(users, groups, ATTR) == fingerprint(users[::-1], groups[::-1], ATTR)


def test_fingerprint_ignores_volatile_fields() -> None:
    """Somebody logging in changes nothing about the desired Matrix state."""
    before = fingerprint([_user("1")], [], ATTR)
    after = fingerprint([_user("1", last_login="2026-07-10T09:00:00Z")], [], ATTR)
    assert before == after


def test_fingerprint_moves_on_a_new_user() -> None:
    assert fingerprint([_user("1")], [], ATTR) != fingerprint([_user("1"), _user("2")], [], ATTR)


def test_fingerprint_moves_on_a_group_membership_change() -> None:
    before = fingerprint([_user("1")], [], ATTR)
    after = fingerprint([_user("1", groups_obj=[{"pk": "g1"}])], [], ATTR)
    assert before != after


def test_fingerprint_moves_on_a_superuser_promotion() -> None:
    """Superuser status drives room admin power levels."""
    assert fingerprint([_user("1")], [], ATTR) != fingerprint([_user("1", is_superuser=True)], [], ATTR)


def test_fingerprint_moves_on_a_renamed_mxid_attribute() -> None:
    assert fingerprint([_user("1")], [], ATTR) != fingerprint([_user("1", username="renamed")], [], ATTR)


def test_fingerprint_tolerates_a_user_missing_the_mxid_attribute() -> None:
    """The reconciler skips such a user with a warning; the poll must not raise over it."""
    unmappable = {"pk": "1", "groups_obj": []}
    assert fingerprint([unmappable], [], "attributes.matrix_name")


def test_fingerprint_moves_on_group_attributes() -> None:
    """Group attributes carry the room's alias, name, topic, avatar and power level."""
    before = fingerprint([], [_group()], ATTR)
    after = fingerprint([], [_group(attributes={"is_chatroom": True, "room_name": "New"})], ATTR)
    assert before != after


async def test_first_poll_establishes_a_baseline_without_triggering() -> None:
    """The engine reconciles on startup anyway; triggering here would only make it run twice."""
    authentik = FakeAuthentik()
    authentik.users = [_user("1")]
    triggered: list[None] = []
    poller = DiscoveryPoller(authentik, _config(), lambda: triggered.append(None))  # type: ignore[arg-type]

    assert await poller.poll_once() is False
    assert triggered == []


async def test_unchanged_authentik_does_not_trigger() -> None:
    authentik = FakeAuthentik()
    authentik.users = [_user("1")]
    triggered: list[None] = []
    poller = DiscoveryPoller(authentik, _config(), lambda: triggered.append(None))  # type: ignore[arg-type]

    await poller.poll_once()
    assert await poller.poll_once() is False
    assert triggered == []


async def test_a_new_user_triggers_a_reconcile() -> None:
    authentik = FakeAuthentik()
    authentik.users = [_user("1")]
    triggered: list[None] = []
    poller = DiscoveryPoller(authentik, _config(), lambda: triggered.append(None))  # type: ignore[arg-type]

    await poller.poll_once()
    authentik.users.append(_user("2"))

    assert await poller.poll_once() is True
    assert len(triggered) == 1
    # ...and the change is now the baseline, so it does not fire again.
    assert await poller.poll_once() is False
    assert len(triggered) == 1


async def test_a_deactivated_user_triggers_a_reconcile() -> None:
    """Deactivated users drop out of the filtered list, which moves the fingerprint (G9.1)."""
    authentik = FakeAuthentik()
    authentik.users = [_user("1"), _user("2")]
    triggered: list[None] = []
    poller = DiscoveryPoller(authentik, _config(), lambda: triggered.append(None))  # type: ignore[arg-type]

    await poller.poll_once()
    authentik.users.pop()

    assert await poller.poll_once() is True
    assert len(triggered) == 1


async def test_poll_touches_only_authentik_and_only_three_times() -> None:
    """The whole point: one poll is a few Authentik requests and nothing against Synapse."""
    authentik = FakeAuthentik()
    poller = DiscoveryPoller(authentik, _config(), lambda: None)  # type: ignore[arg-type]

    await poller.poll_once()

    assert authentik.calls == 3  # list_users + list_groups (room groups, power-level groups)


async def test_a_disabled_poll_returns_immediately() -> None:
    authentik = FakeAuthentik()
    poller = DiscoveryPoller(authentik, _config(poll_sec=0), lambda: None)  # type: ignore[arg-type]

    await asyncio.wait_for(poller.run(), timeout=1)

    assert authentik.calls == 0


async def test_run_stops_promptly_and_keeps_polling_after_an_error() -> None:
    authentik = FakeAuthentik()
    poller = DiscoveryPoller(
        authentik,  # type: ignore[arg-type]
        _config(poll_sec=1),
        lambda: None,
        error_backoff_sec=0.01,
    )

    failures: list[int] = []

    async def _fail_once(**_: Any) -> list[dict[str, Any]]:
        failures.append(1)
        if len(failures) == 1:
            raise RuntimeError("authentik is down")
        return []

    authentik.list_users = _fail_once  # type: ignore[method-assign]

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.request_stop()
    await asyncio.wait_for(task, timeout=1)

    assert len(failures) >= 2  # recovered and polled again rather than dying


# --- REC-15: the fingerprint covers every Authentik input the engine acts on ----------------------


async def _polls_until_changed(
    authentik: FakeAuthentik, change: Any, config: OnbotConfig | None = None
) -> bool:
    triggered: list[None] = []
    poller = DiscoveryPoller(
        authentik,  # type: ignore[arg-type]
        config or _config(),
        lambda: triggered.append(None),
    )
    await poller.poll_once()
    change()
    return await poller.poll_once() and len(triggered) == 1


async def test_a_power_level_group_outside_the_room_groups_triggers_a_reconcile() -> None:
    """The documented "Moderators" group carries the level, not the chat opt-in attribute."""
    authentik = FakeAuthentik()
    authentik.users = [_user("1", groups_obj=[{"pk": "g1"}, {"pk": "mods"}])]
    authentik.groups = [_group("g1")]
    moderators = {
        "pk": "mods",
        "name": "Moderators",
        "attributes": {"matrix-power-level": 50},
        "users": ["1"],
    }
    authentik.power_level_groups = [moderators]

    def raise_the_level() -> None:
        moderators["attributes"] = {"matrix-power-level": 100}

    assert await _polls_until_changed(authentik, raise_the_level)


async def test_a_new_member_of_a_power_level_group_triggers_a_reconcile() -> None:
    authentik = FakeAuthentik()
    moderators = {
        "pk": "mods",
        "name": "Moderators",
        "attributes": {"matrix-power-level": 50},
        "users": ["1"],
    }
    authentik.power_level_groups = [moderators]

    def add_member() -> None:
        moderators["users"] = ["1", "2"]

    assert await _polls_until_changed(authentik, add_member)


def test_fingerprint_moves_on_a_new_group_parent() -> None:
    """`only_for_children_of_groups_with_uid` selects room groups by their parent."""
    before = fingerprint([], [_group(parent="p1")], ATTR)
    after = fingerprint([], [_group(parent="p2")], ATTR)
    assert before != after


async def test_a_global_room_admin_outside_the_scope_triggers_a_reconcile() -> None:
    config = _config()
    config.global_room_admins = ["helpdesk"]
    authentik = FakeAuthentik()
    authentik.users = [_user("1")]
    helpdesk = _user("9", username="helpdesk")

    def activate() -> None:
        authentik.by_username["helpdesk"] = [helpdesk]

    assert await _polls_until_changed(authentik, activate, config)


async def test_a_global_admin_lookup_ignores_users_a_server_returns_for_another_name() -> None:
    """The username filter is only a hint, like in the engine's lookup."""
    config = _config()
    config.global_room_admins = ["helpdesk"]
    authentik = FakeAuthentik()

    def server_ignores_the_filter() -> None:
        authentik.by_username["helpdesk"] = [_user("1", username="someone-else")]

    assert not await _polls_until_changed(authentik, server_ignores_the_filter, config)


# --- S18: the group scope is re-checked like in the engine (SEC-09) -------------------------------


def _group_scoped_config() -> OnbotConfig:
    config = _config()
    config.sync_authentik_users_with_matrix_rooms.sync_only_users_of_groups_with_id = ["staff"]
    return config


async def test_users_outside_the_sync_groups_do_not_move_the_fingerprint() -> None:
    """An Authentik that ignores `groups_by_pk` answers with every user; the strays are dropped."""
    authentik = FakeAuthentik()
    member = _user("1", groups_obj=[{"pk": "staff"}])
    stray = _user("2", groups_obj=[{"pk": "other"}])
    authentik.users = [member, stray]

    def stray_changes() -> None:
        stray["is_superuser"] = True
        authentik.users.append(_user("3"))

    assert not await _polls_until_changed(authentik, stray_changes, _group_scoped_config())


async def test_a_user_joining_a_sync_group_triggers_a_reconcile() -> None:
    authentik = FakeAuthentik()
    newcomer = _user("2", groups_obj=[{"pk": "other"}])
    authentik.users = [_user("1", groups_obj=[{"pk": "staff"}]), newcomer]

    def join() -> None:
        newcomer["groups_obj"] = [{"pk": "other"}, {"pk": "staff"}]

    assert await _polls_until_changed(authentik, join, _group_scoped_config())


async def test_the_group_listings_leave_out_the_member_objects() -> None:
    """W8R-05: every poll listed all groups with every member's user object embedded, although only
    the members' pks are fingerprinted."""

    class Recording(FakeAuthentik):
        def __init__(self) -> None:
            super().__init__()
            self.group_kwargs: list[dict[str, Any]] = []

        async def list_groups(self, **kwargs: Any) -> list[dict[str, Any]]:
            self.group_kwargs.append(kwargs)
            return await super().list_groups(**kwargs)

    authentik = Recording()
    poller = DiscoveryPoller(authentik, _config(), lambda: None)  # type: ignore[arg-type]

    await poller.poll_once()

    assert len(authentik.group_kwargs) == 2  # the room groups and the power level groups
    assert all(kwargs.get("include_users") is False for kwargs in authentik.group_kwargs)


def test_the_power_level_fingerprint_reads_only_the_member_pks() -> None:
    group = {"pk": "mods", "attributes": {"matrix-power-level": 50}, "users": ["1"]}
    with_objects = group | {"users_obj": [{"pk": "1", "last_login": "2026-11-09T10:00:00Z"}]}
    assert fingerprint([], [], ATTR, power_level_groups=[group]) == fingerprint(
        [], [], ATTR, power_level_groups=[with_objects]
    )
