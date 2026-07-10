"""Unit tests for the Authentik discovery poll: what moves the fingerprint, what must not, and when
a change triggers an out-of-band reconcile."""

from __future__ import annotations

import asyncio
from typing import Any

from onbot.config import AuthentikServer, OnbotConfig, SynapseServer
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
        self.calls = 0

    async def list_users(self, **_: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self.users)

    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        self.calls += 1
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


async def test_poll_touches_only_authentik_and_only_twice() -> None:
    """The whole point: one poll is two Authentik requests and nothing against Synapse."""
    authentik = FakeAuthentik()
    poller = DiscoveryPoller(authentik, _config(), lambda: None)  # type: ignore[arg-type]

    await poller.poll_once()

    assert authentik.calls == 2  # list_users + list_groups


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
