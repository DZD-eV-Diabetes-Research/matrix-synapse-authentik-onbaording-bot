"""Integration-style tests for the reconciler engine using in-memory fakes."""

import asyncio
from typing import Any

import pytest

from onbot.config import OnbotConfig
from onbot.events import EventBus, Signal
from onbot.reconciler.effectors import DryRunEffectors
from onbot.reconciler.engine import ReconcilerEngine

_BASE = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
}

_GROUP_G1 = {
    "pk": "g1",
    "name": "Team",
    "attributes": {"chat-systemwide-powerlevel": 50},
    "users": ["alice-pk"],
}


class FakeAuthentik:
    async def list_users(self, **_: Any) -> list[dict[str, Any]]:
        return [
            {"username": "alice", "pk": "alice-pk", "is_superuser": False, "groups_obj": [{"pk": "g1"}]},
            {"username": "bob", "pk": "bob-pk", "is_superuser": False, "groups_obj": [{"pk": "g1"}]},
            {"username": "carol", "pk": "carol-pk", "is_superuser": False, "groups_obj": [{"pk": "g2"}]},
        ]

    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        return [_GROUP_G1]


class FakeAdmin:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []
        self.blocked_changes: list[tuple[str, bool]] = []

    async def list_users(self) -> list[dict[str, Any]]:
        return [{"name": f"@{u}:company.org"} for u in ("alice", "bob", "carol")]

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return [{"room_id": "!room1:company.org", "canonical_alias": "#g1:company.org", "name": "Team"}]

    async def list_spaces(self) -> list[dict[str, Any]]:
        return [{"room_id": "!space:company.org", "canonical_alias": "#OnBotSpace:company.org"}]

    async def list_room_members(self, room_id: str) -> list[str]:
        if room_id == "!space:company.org":
            return ["@alice:company.org"]
        return ["@alice:company.org", "@stale:company.org", "@bot:company.org"]

    async def room_is_blocked(self, room_id: str) -> bool:
        return False

    async def room_set_blocked(self, room_id: str, *, blocked: bool) -> None:
        self.blocked_changes.append((room_id, blocked))

    async def add_user_to_room(self, room_id: str, user_id: str) -> None:
        self.added.append((room_id, user_id))


class RecordingEffectors(DryRunEffectors):
    def __init__(self) -> None:
        self.kicks: list[tuple[str, str]] = []
        self.power_levels: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[str] = []
        self.avatars: list[tuple[str, str]] = []
        self.state_store: dict[tuple[str, str], dict[str, Any]] = {}

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
        self.kicks.append((room_id, user_id))

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None:
        self.power_levels.append((room_id, power_levels))

    async def upload_avatar(self, url: str) -> str:
        self.uploads.append(url)
        return f"mxc://company.org/{len(self.uploads)}"

    async def set_room_avatar(self, room_id: str, mxc_uri: str) -> None:
        self.avatars.append((room_id, mxc_uri))

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        return self.state_store.get((room_id, event_type))

    async def put_room_state(self, room_id: str, event_type: str, content: dict[str, Any]) -> None:
        self.state_store[(room_id, event_type)] = content


def _engine(events: EventBus | None = None) -> tuple[ReconcilerEngine, FakeAdmin, RecordingEffectors]:
    config = OnbotConfig.model_validate(_BASE)
    admin = FakeAdmin()
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(config, FakeAuthentik(), admin, effectors, events)  # type: ignore[arg-type]
    return engine, admin, effectors


async def test_reconcile_once_converges() -> None:
    engine, admin, effectors = _engine()
    await engine.reconcile_once()

    # space membership: bob & carol added (alice already present)
    assert ("!space:company.org", "@bob:company.org") in admin.added
    assert ("!space:company.org", "@carol:company.org") in admin.added

    # room membership: bob added to g1 room; stale kicked; bot protected
    assert ("!room1:company.org", "@bob:company.org") in admin.added
    assert effectors.kicks == [("!room1:company.org", "@stale:company.org")]

    # power levels: alice gets 50 in the g1 room
    assert effectors.power_levels == [("!room1:company.org", {"users": {"@alice:company.org": 50}})]


async def test_space_avatar_set_and_deduplicated() -> None:
    config = OnbotConfig.model_validate(
        {
            **_BASE,
            "create_matrix_rooms_in_a_matrix_space": {
                "create_matrix_space_if_not_exists": {"avatar_url": "https://cdn/icon.png"}
            },
        }
    )
    admin = FakeAdmin()
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(config, FakeAuthentik(), admin, effectors)  # type: ignore[arg-type]

    # First pass: no stored avatar -> upload + set + record the source URL in onbot state.
    await engine.reconcile_once()
    assert effectors.uploads == ["https://cdn/icon.png"]
    assert effectors.avatars == [("!space:company.org", "mxc://company.org/1")]

    # Second pass: URL unchanged -> no re-upload, no re-set.
    await engine.reconcile_once()
    assert effectors.uploads == ["https://cdn/icon.png"]
    assert len(effectors.avatars) == 1


async def test_space_avatar_skipped_when_unset() -> None:
    engine, _, effectors = _engine()  # default config has no avatar_url
    await engine.reconcile_once()
    assert effectors.uploads == []
    assert effectors.avatars == []


class AvatarAuthentik(FakeAuthentik):
    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        return [{**_GROUP_G1, "attributes": {**_GROUP_G1["attributes"], "chatroom_avatar_url": "https://cdn/t.png"}}]


async def test_group_room_avatar_set_and_deduplicated() -> None:
    config = OnbotConfig.model_validate(_BASE)  # default room_avatar_url_attribute == "chatroom_avatar_url"
    admin = FakeAdmin()
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(config, AvatarAuthentik(), admin, effectors)  # type: ignore[arg-type]

    # First pass: the existing g1 room gets its avatar from the group attribute.
    await engine.reconcile_once()
    assert effectors.uploads == ["https://cdn/t.png"]
    assert effectors.avatars == [("!room1:company.org", "mxc://company.org/1")]

    # Second pass: URL unchanged -> no re-upload, no re-set.
    await engine.reconcile_once()
    assert effectors.uploads == ["https://cdn/t.png"]
    assert len(effectors.avatars) == 1


async def test_reconcile_emits_user_synced_events() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def handler(event: Any) -> None:
        seen.append(event.payload["mxid"])

    bus.subscribe(Signal.user_synced, handler)
    engine, _, _ = _engine(events=bus)
    await engine.reconcile_once()
    assert set(seen) == {"@alice:company.org", "@bob:company.org", "@carol:company.org"}


async def test_run_stops_gracefully_after_trigger() -> None:
    engine, _, _ = _engine()
    calls = 0

    async def one_pass() -> None:
        nonlocal calls
        calls += 1
        engine.request_stop()

    engine.reconcile_once = one_pass  # type: ignore[method-assign]
    await asyncio.wait_for(engine.run(), timeout=2)
    assert calls == 1


async def _async(value: Any) -> Any:
    return value


class FakeLifecycle:
    """Captures the orphaned set the engine feeds the lifecycle manager."""

    def __init__(self) -> None:
        self.calls: list[set[str]] = []

    async def reconcile_accounts(self, orphaned_mxids: set[str]) -> list[Any]:
        self.calls.append(set(orphaned_mxids))
        return []


class LifecycleAuthentik(FakeAuthentik):
    """Returns active users for is_active=True and one disabled user otherwise."""

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs.get("filter_is_active") is False:
            return [
                {"username": "dave", "pk": "dave-pk"},  # disabled, has a Matrix account → orphan
                {"username": "eve", "pk": "eve-pk"},  # disabled, but no Matrix account → ignored
            ]
        return await super().list_users(**kwargs)


def _lifecycle_engine(config: OnbotConfig, admin: FakeAdmin, lifecycle: FakeLifecycle) -> ReconcilerEngine:
    return ReconcilerEngine(config, LifecycleAuthentik(), admin, RecordingEffectors(), lifecycle=lifecycle)  # type: ignore[arg-type]


async def test_lifecycle_invoked_with_scoped_orphans() -> None:
    config = OnbotConfig.model_validate(_BASE)
    admin = FakeAdmin()
    # @dave exists in Matrix and is disabled upstream; @eve does not exist in Matrix.
    admin.list_users = lambda: _async(  # type: ignore[method-assign]
        [{"name": f"@{u}:company.org"} for u in ("alice", "bob", "carol", "dave")]
    )
    lifecycle = FakeLifecycle()
    await _lifecycle_engine(config, admin, lifecycle).reconcile_once()

    assert lifecycle.calls == [{"@dave:company.org"}]


async def test_lifecycle_skipped_when_disabled() -> None:
    config = OnbotConfig.model_validate(_BASE)
    lc = config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
    lc.enabled = False
    lifecycle = FakeLifecycle()
    await _lifecycle_engine(config, FakeAdmin(), lifecycle).reconcile_once()
    assert lifecycle.calls == []


async def test_run_survives_a_failing_pass(caplog: pytest.LogCaptureFixture) -> None:
    engine, _, _ = _engine()
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        engine.request_stop()

    engine.reconcile_once = flaky  # type: ignore[method-assign]
    engine.config.server_tick_rate_sec = 0  # don't actually wait between passes
    await asyncio.wait_for(engine.run(), timeout=2)
    assert calls == 2  # first pass failed but the loop continued
