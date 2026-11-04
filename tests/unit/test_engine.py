"""Integration-style tests for the reconciler engine using in-memory fakes."""

import asyncio
from typing import Any

import pytest

from onbot.clients.base import ApiError
from onbot.config import OnbotConfig
from onbot.events import EventBus, Signal
from onbot.lifecycle.accounts import DirectorySnapshot, DirectoryUser
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
        self.deleted: list[str] = []

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

    async def delete_room(self, room_id: str, **_: Any) -> dict[str, Any]:
        self.deleted.append(room_id)
        return {}

    async def get_room_details(self, room_id: str) -> dict[str, Any]:
        # Every fake room is a v12 room the bot created, so the bot is an infinite creator there.
        return {"room_id": room_id, "version": "12", "creator": "@bot:company.org"}


class RecordingEffectors(DryRunEffectors):
    def __init__(self) -> None:
        self.kicks: list[tuple[str, str]] = []
        self.power_levels: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[str] = []
        self.avatars: list[tuple[str, str]] = []
        self.state_store: dict[tuple[str, str], dict[str, Any]] = {}
        self.lobbies_created: list[tuple[str, str, dict[str, Any]]] = []
        self.state_writes: list[tuple[str, str, dict[str, Any]]] = []
        self.names: list[tuple[str, str]] = []
        self.topics: list[tuple[str, str]] = []
        self.account_data: dict[tuple[str, str], dict[str, Any]] = {}

    async def kick_user(self, room_id: str, user_id: str, reason: str | None = None) -> None:
        self.kicks.append((room_id, user_id))

    async def set_room_name(self, room_id: str, name: str) -> None:
        self.names.append((room_id, name))

    async def set_room_topic(self, room_id: str, topic: str) -> None:
        self.topics.append((room_id, topic))
        self.state_store[(room_id, "m.room.topic")] = {"topic": topic}

    async def create_lobby_room(
        self, attrs: Any, parent_space_id: str, join_rules_content: dict[str, Any]
    ) -> str:
        self.lobbies_created.append((attrs.canonical_alias, parent_space_id, join_rules_content))
        return "!newlobby:company.org"

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
        self.state_writes.append((room_id, event_type, content))

    async def get_room_account_data(
        self, user_id: str, room_id: str, data_type: str
    ) -> dict[str, Any] | None:
        return self.account_data.get((room_id, data_type))

    async def put_room_account_data(
        self, user_id: str, room_id: str, data_type: str, content: dict[str, Any]
    ) -> None:
        self.account_data[(room_id, data_type)] = content


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
        return [
            {
                **_GROUP_G1,
                "attributes": {**_GROUP_G1["attributes"], "chatroom_avatar_url": "https://cdn/t.png"},
            }
        ]


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


# --- room name + topic convergence -------------------------------------------------


class TopicAuthentik(FakeAuthentik):
    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        return [{**_GROUP_G1, "attributes": {**_GROUP_G1["attributes"], "chatroom_topic": "Low traffic."}}]


def _topic_engine() -> tuple[ReconcilerEngine, RecordingEffectors]:
    config = OnbotConfig.model_validate(_BASE)  # default topic attribute == "attributes.chatroom_topic"
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(config, TopicAuthentik(), FakeAdmin(), effectors)  # type: ignore[arg-type]
    return engine, effectors


async def test_topic_is_set_on_a_room_that_was_created_without_one_and_then_left_alone() -> None:
    """A group that gains `chatroom_topic` later gets it applied, exactly once.

    The room's actual topic lives in room state: the admin List Room API never returns one, so a
    pass that trusted the listing rewrote the topic on every tick.
    """
    engine, effectors = _topic_engine()

    await engine.reconcile_once()
    assert effectors.topics == [("!room1:company.org", "Low traffic.")]

    await engine.reconcile_once()
    assert effectors.topics == [("!room1:company.org", "Low traffic.")]  # already correct; no rewrite


async def test_topic_drift_is_repaired() -> None:
    engine, effectors = _topic_engine()
    effectors.state_store[("!room1:company.org", "m.room.topic")] = {"topic": "somebody changed this"}

    await engine.reconcile_once()
    assert effectors.topics == [("!room1:company.org", "Low traffic.")]


async def test_a_group_without_a_topic_leaves_the_rooms_topic_alone() -> None:
    engine, _, effectors = _engine()  # _GROUP_G1 carries no chatroom_topic
    effectors.state_store[("!room1:company.org", "m.room.topic")] = {"topic": "set by hand"}

    await engine.reconcile_once()
    assert effectors.topics == []


class RenamedAuthentik(FakeAuthentik):
    """The g1 group gained a `chatroom_name` after its room was created as "Team"."""

    async def list_groups(self, **_: Any) -> list[dict[str, Any]]:
        return [{**_GROUP_G1, "attributes": {**_GROUP_G1["attributes"], "chatroom_name": "Team Chat"}}]


async def test_name_added_to_a_group_later_renames_the_existing_room() -> None:
    config = OnbotConfig.model_validate(_BASE)  # default name attribute == "attributes.chatroom_name"
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(config, RenamedAuthentik(), FakeAdmin(), effectors)  # type: ignore[arg-type]

    await engine.reconcile_once()
    assert effectors.names == [("!room1:company.org", "Team Chat")]


async def test_a_group_without_the_name_attribute_keeps_the_group_name() -> None:
    """The fallback must not fight the listing: the room is already named after the group."""
    engine, _, effectors = _engine()  # _GROUP_G1 is named "Team", as is the room

    await engine.reconcile_once()
    assert effectors.names == []


async def test_attributes_are_left_alone_when_keep_updating_is_off() -> None:
    config = OnbotConfig.model_validate(
        {**_BASE, "matrix_room_default_settings": {"keep_updating_matrix_attributes_from_authentik": False}}
    )
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(config, TopicAuthentik(), FakeAdmin(), effectors)  # type: ignore[arg-type]

    await engine.reconcile_once()
    assert effectors.topics == []
    assert effectors.names == []


# --- obsolete room teardown (disable_rooms_when_mapped_authentik_group_disappears) ---

_GROUP_ROOM_EVENT = "org.company.onbot.group_room"
_ORPHAN = "!orphan:company.org"


class OrphanAdmin(FakeAdmin):
    """Serves an extra room besides the live g1 room, for the obsolete-room paths."""

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return [
            *await super().list_non_space_rooms(),
            {"room_id": _ORPHAN, "canonical_alias": "#gone:company.org", "name": "Gone"},
        ]


def _orphan_engine(
    *, disable: bool = True, delete: bool = False, orphan_state: dict[str, Any] | None = None
) -> tuple[ReconcilerEngine, OrphanAdmin, RecordingEffectors]:
    config = OnbotConfig.model_validate(_BASE)
    room_cfg = config.sync_matrix_rooms_based_on_authentik_groups
    room_cfg.disable_rooms_when_mapped_authentik_group_disappears = disable
    room_cfg.delete_disabled_rooms = delete

    admin = OrphanAdmin()
    effectors = RecordingEffectors()
    if orphan_state is not None:
        effectors.state_store[(_ORPHAN, _GROUP_ROOM_EVENT)] = orphan_state
    engine = ReconcilerEngine(config, FakeAuthentik(), admin, effectors)  # type: ignore[arg-type]
    return engine, admin, effectors


def _state(group_id: str) -> dict[str, Any]:
    return {"schema_version": 1, "room_type": "group_room", "group_id": group_id}


async def test_obsolete_room_is_blocked_and_cleared() -> None:
    # The orphan room records group "g-gone", which Authentik no longer returns.
    engine, admin, effectors = _orphan_engine(orphan_state=_state("g-gone"))
    await engine.reconcile_once()

    assert (_ORPHAN, True) in admin.blocked_changes
    # every member except the bot is kicked out of the obsolete room
    kicked = {mxid for room, mxid in effectors.kicks if room == _ORPHAN}
    assert kicked == {"@alice:company.org", "@stale:company.org"}
    assert admin.deleted == []  # delete_disabled_rooms is off


async def test_obsolete_room_deleted_when_delete_disabled_rooms_enabled() -> None:
    engine, admin, _ = _orphan_engine(delete=True, orphan_state=_state("g-gone"))
    await engine.reconcile_once()

    assert (_ORPHAN, True) in admin.blocked_changes
    assert admin.deleted == [_ORPHAN]


async def test_obsolete_room_untouched_when_disable_flag_off() -> None:
    engine, admin, effectors = _orphan_engine(disable=False, orphan_state=_state("g-gone"))
    await engine.reconcile_once()

    assert admin.blocked_changes == []
    assert admin.deleted == []
    assert all(room != _ORPHAN for room, _ in effectors.kicks)


async def test_room_without_onbot_state_is_never_touched() -> None:
    # No group_room state event on the orphan -> not a room we manage.
    engine, admin, effectors = _orphan_engine(delete=True, orphan_state=None)
    await engine.reconcile_once()

    assert admin.blocked_changes == []
    assert admin.deleted == []
    assert all(room != _ORPHAN for room, _ in effectors.kicks)


async def test_room_of_live_group_survives_an_alias_change() -> None:
    # The orphan's alias no longer matches what the group projects to, so it is unmapped this pass —
    # but its recorded group_id is still live, so it must not be torn down.
    engine, admin, effectors = _orphan_engine(delete=True, orphan_state=_state("g1"))
    await engine.reconcile_once()

    assert admin.blocked_changes == []
    assert admin.deleted == []
    assert all(room != _ORPHAN for room, _ in effectors.kicks)


async def test_reconcile_emits_user_synced_events() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def handler(event: Any) -> None:
        seen.append(event.payload["mxid"])

    bus.subscribe(Signal.user_synced, handler)
    engine, _, _ = _engine(events=bus)
    await engine.reconcile_once()
    assert set(seen) == {"@alice:company.org", "@bob:company.org", "@carol:company.org"}


async def test_reconcile_emits_reconcile_completed_once_the_pass_is_done() -> None:
    # The admin room's invite pass hangs off this signal (ADR-0010).
    bus = EventBus()
    engine, _, _ = _engine(events=bus)
    seen_at: list[float | None] = []

    async def handler(_event: Any) -> None:
        seen_at.append(engine.last_reconcile_at)

    bus.subscribe(Signal.reconcile_completed, handler)
    await engine.reconcile_once()

    assert len(seen_at) == 1
    assert seen_at[0] is not None  # emitted after the pass is recorded as done, not before


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
    """Captures the directory snapshot the engine feeds the lifecycle manager."""

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.calls: list[DirectorySnapshot] = []
        self.outcomes = outcomes or []

    async def reconcile_accounts(self, snapshot: DirectorySnapshot) -> list[Any]:
        self.calls.append(snapshot)
        return self.outcomes


_DISABLED = [
    {"username": "dave", "pk": "dave-pk", "is_active": False},  # disabled, has a Matrix account
    {"username": "eve", "pk": "eve-pk", "is_active": False},  # disabled, no Matrix account
]


class LifecycleAuthentik(FakeAuthentik):
    """Active users from :class:`FakeAuthentik` plus two disabled ones, honouring ``filter_is_active``."""

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        active = [dict(u, is_active=True) for u in await super().list_users(**kwargs)]
        flag = kwargs.get("filter_is_active", True)
        if flag is True:
            return active
        if flag is False:
            return list(_DISABLED)
        return active + list(_DISABLED)


def _lifecycle_engine(
    config: OnbotConfig, admin: FakeAdmin, lifecycle: FakeLifecycle, authentik: Any = None
) -> ReconcilerEngine:
    return ReconcilerEngine(
        config,
        authentik or LifecycleAuthentik(),
        admin,  # type: ignore[arg-type]
        RecordingEffectors(),
        lifecycle=lifecycle,  # type: ignore[arg-type]
    )


async def test_lifecycle_gets_the_whole_directory_and_the_matrix_accounts() -> None:
    config = OnbotConfig.model_validate(_BASE)
    config.authentik_user_ignore_list = ["eve"]
    config.matrix_user_ignore_list = ["@carol:company.org"]
    admin = FakeAdmin()
    admin.list_users = lambda: _async(  # type: ignore[method-assign]
        [{"name": f"@{u}:company.org"} for u in ("alice", "bob", "carol", "dave")]
    )
    lifecycle = FakeLifecycle()
    await _lifecycle_engine(config, admin, lifecycle).reconcile_once()

    [snap] = lifecycle.calls
    by_name = {u.username: u for u in snap.users}
    assert set(by_name) == {"alice", "bob", "carol", "dave", "eve"}
    assert by_name["dave"] == DirectoryUser(
        pk="dave-pk", username="dave", mxid="@dave:company.org", is_active=False, in_scope=True, ignored=False
    )
    assert by_name["eve"].ignored is True
    assert snap.matrix_mxids == {f"@{u}:company.org" for u in ("alice", "bob", "carol", "dave")}
    assert snap.excluded_mxids == {"@bot:company.org", "@carol:company.org"}
    assert snap.synced_mxids == {"@alice:company.org", "@bob:company.org"}


async def test_lifecycle_scope_comes_from_the_same_filters_as_the_room_sync() -> None:
    """LIF-05 / REC-14: path, attribute and group filters decide who is in scope, for both sides."""

    class PathAuthentik(LifecycleAuthentik):
        def __init__(self) -> None:
            self.queries: list[dict[str, Any]] = []

        async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
            self.queries.append(kwargs)
            users = await super().list_users(**kwargs)
            if kwargs.get("filter_by_path") == "staff":
                return [u for u in users if u["username"] in ("alice", "dave")]
            return users

    config = OnbotConfig.model_validate(_BASE)
    sync = config.sync_authentik_users_with_matrix_rooms
    sync.sync_only_users_in_authentik_pathes = ["staff"]
    sync.sync_only_users_with_authentik_attributes = {"chat": True}
    sync.sync_only_users_of_groups_with_id = ["g1"]
    authentik = PathAuthentik()
    lifecycle = FakeLifecycle()
    await _lifecycle_engine(config, FakeAdmin(), lifecycle, authentik).reconcile_once()

    [snap] = lifecycle.calls
    assert {u.username for u in snap.users if u.in_scope} == {"alice", "dave"}
    scoped = [q for q in authentik.queries if q.get("filter_by_path") == "staff"]
    assert {q["filter_is_active"] for q in scoped} == {True, None}
    for query in scoped:
        assert query["filter_by_attribute"] == {"chat": True}
        assert query["filter_groups_by_pk"] == ["g1"]


async def test_empty_authentik_answer_skips_the_lifecycle_and_reports() -> None:
    class Blind(LifecycleAuthentik):
        async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
            return []

    events = EventBus()
    reports: list[Any] = []

    async def collect(event: Any) -> None:
        reports.append(event.payload)

    events.subscribe(Signal.lifecycle_attention, collect)
    config = OnbotConfig.model_validate(_BASE)
    lifecycle = FakeLifecycle()
    engine = _lifecycle_engine(config, FakeAdmin(), lifecycle, Blind())
    engine.events = events
    await engine.reconcile_once()
    assert lifecycle.calls == []
    assert [r["action"] for r in reports] == ["abort"]


async def test_lifecycle_outcomes_that_need_attention_are_emitted() -> None:
    from onbot.lifecycle.accounts import LifecycleAction, LifecycleOutcome

    outcomes = [
        LifecycleOutcome(mxid="@a:company.org", action=LifecycleAction.mark, dry_run=False),
        LifecycleOutcome(mxid="@b:company.org", action=LifecycleAction.freeze, dry_run=False, detail="why"),
        LifecycleOutcome(
            mxid="@c:company.org", action=LifecycleAction.logout, dry_run=False, failed=True, detail="boom"
        ),
    ]
    events = EventBus()
    reports: list[Any] = []

    async def collect(event: Any) -> None:
        reports.append(event.payload)

    events.subscribe(Signal.lifecycle_attention, collect)
    engine = _lifecycle_engine(OnbotConfig.model_validate(_BASE), FakeAdmin(), FakeLifecycle(outcomes))
    engine.events = events
    await engine.reconcile_once()
    assert reports == [
        {"mxid": "@b:company.org", "action": "freeze", "detail": "why"},
        {"mxid": "@c:company.org", "action": "logout", "detail": "boom"},
    ]


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


# --- visitor lobby (ADR-0012) ---

_JOIN_RULES = "m.room.join_rules"
_LOBBY_EVENT = "org.company.onbot.visitor_lobby"
_LOBBY_ID = "!lobby1:company.org"


def _lobby_config() -> OnbotConfig:
    config = OnbotConfig.model_validate(_BASE)
    config.matrix_room_default_settings.visitor_lobby_enabled = True
    return config


class LobbyAdmin(FakeAdmin):
    """Serves the g1 room plus its lobby, with a visitor in the lobby who is not in the group."""

    def __init__(self, *, with_lobby_room: bool) -> None:
        super().__init__()
        self._with_lobby_room = with_lobby_room

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        rooms = await super().list_non_space_rooms()
        if self._with_lobby_room:
            rooms = [
                *rooms,
                {"room_id": _LOBBY_ID, "canonical_alias": "#g1-lobby:company.org", "name": "Team (Lobby)"},
            ]
        return rooms

    async def list_room_members(self, room_id: str) -> list[str]:
        if room_id == _LOBBY_ID:
            # A visitor who joined on purpose and is NOT in the Authentik group, plus the bot.
            return ["@visitor:company.org", "@bot:company.org"]
        return await super().list_room_members(room_id)


async def test_lobby_created_stamped_and_join_rule_set() -> None:
    engine = ReconcilerEngine(
        _lobby_config(), FakeAuthentik(), LobbyAdmin(with_lobby_room=False), RecordingEffectors()
    )  # type: ignore[arg-type]
    effectors = engine.effectors
    assert isinstance(effectors, RecordingEffectors)
    await engine.reconcile_once()

    # A lobby was created for g1, restricted to the parent space, and stamped visitor_lobby (never
    # group_room), so no later pass mistakes it for the group room.
    assert len(effectors.lobbies_created) == 1
    alias, space_id, join_rules = effectors.lobbies_created[0]
    assert alias == "#g1-lobby:company.org"
    assert space_id == "!space:company.org"
    assert join_rules == {
        "join_rule": "restricted",
        "allow": [{"type": "m.room_membership", "room_id": "!space:company.org"}],
    }
    stamped = effectors.state_store[("!newlobby:company.org", _LOBBY_EVENT)]
    assert stamped["room_type"] == "visitor_lobby"
    assert stamped["group_id"] == "g1"


async def test_lobby_is_add_only_and_never_kicks_a_visitor() -> None:
    """The single most dangerous bug: a lobby mistaken for a group room, projecting membership and
    kicking every visitor. The visitor must survive; the lobby must never appear in a kick."""
    engine = ReconcilerEngine(
        _lobby_config(), FakeAuthentik(), LobbyAdmin(with_lobby_room=True), RecordingEffectors()
    )  # type: ignore[arg-type]
    effectors = engine.effectors
    admin = engine.admin
    assert isinstance(effectors, RecordingEffectors)
    assert isinstance(admin, LobbyAdmin)
    await engine.reconcile_once()

    # The visitor (not in the group) is NOT kicked from the lobby — the lobby never enters a kick.
    assert all(room != _LOBBY_ID for room, _ in effectors.kicks)
    # Meanwhile the group room DID kick its stale member: lobby membership ≠ group-room membership.
    assert ("!room1:company.org", "@stale:company.org") in effectors.kicks
    # Group members are seeded into the lobby (inject default), visitors are left in place.
    added_to_lobby = {mxid for room, mxid in admin.added if room == _LOBBY_ID}
    assert "@bob:company.org" in added_to_lobby  # a group member injected
    assert "@visitor:company.org" not in added_to_lobby  # visitor already there, untouched


async def test_lobby_join_rule_is_a_noop_when_already_restricted() -> None:
    admin = LobbyAdmin(with_lobby_room=True)
    effectors = RecordingEffectors()
    # The lobby already carries the exact restricted rule the bot wants.
    effectors.state_store[(_LOBBY_ID, _JOIN_RULES)] = {
        "join_rule": "restricted",
        "allow": [{"type": "m.room_membership", "room_id": "!space:company.org"}],
    }
    engine = ReconcilerEngine(_lobby_config(), FakeAuthentik(), admin, effectors)  # type: ignore[arg-type]
    await engine.reconcile_once()
    # A no-op tick sends no join-rules state event.
    assert not any(evt == _JOIN_RULES for _room, evt, _content in effectors.state_writes)


async def test_lobby_avatar_follows_the_group_rooms_avatar() -> None:
    admin = LobbyAdmin(with_lobby_room=True)
    effectors = RecordingEffectors()
    # AvatarAuthentik serves g1 with a chatroom_avatar_url attribute.
    engine = ReconcilerEngine(_lobby_config(), AvatarAuthentik(), admin, effectors)  # type: ignore[arg-type]

    await engine.reconcile_once()
    # Group room and lobby both get the icon, from the one source URL on the group. (Both go through
    # the same MediaUploader in production, whose per-URL cache turns this into a single fetch; this
    # fake effector has no such cache, hence the repeated entry here.)
    assert effectors.uploads == ["https://cdn/t.png", "https://cdn/t.png"]
    assert effectors.avatars == [
        ("!room1:company.org", "mxc://company.org/1"),
        (_LOBBY_ID, "mxc://company.org/2"),
    ]
    # The lobby's recorded source URL lives on its own visitor_lobby state event, not group_room.
    assert effectors.state_store[(_LOBBY_ID, _LOBBY_EVENT)]["avatar_source_url"] == "https://cdn/t.png"

    # Second pass: URL unchanged -> neither room re-uploads or re-sets.
    await engine.reconcile_once()
    assert len(effectors.uploads) == 2
    assert len(effectors.avatars) == 2


async def test_lobby_name_and_topic_follow_the_group_room() -> None:
    """A lobby's name/topic are derived from the group's, so a rename has to reach it too.

    Otherwise the lobby keeps the name it was created with for the life of the room, and the pair
    stops reading as one place in a client's room list.
    """
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(
        _lobby_config(), RenamedAuthentik(), LobbyAdmin(with_lobby_room=True), effectors
    )  # type: ignore[arg-type]

    await engine.reconcile_once()

    assert (_LOBBY_ID, "Team Chat (Lobby)") in effectors.names
    assert ("!room1:company.org", "Team Chat") in effectors.names
    assert [room for room, _ in effectors.topics] == [_LOBBY_ID]  # group has no topic; the lobby does

    effectors.names.clear()
    effectors.topics.clear()
    # The listing still reports the old name (the fake admin is static), but the topic is now correct
    # in room state, so a second pass must not rewrite it.
    await engine.reconcile_once()
    assert effectors.topics == []


async def test_lobby_inject_disabled_seeds_nobody() -> None:
    config = _lobby_config()
    config.matrix_room_default_settings.visitor_lobby_inject_group_members = False
    admin = LobbyAdmin(with_lobby_room=True)
    engine = ReconcilerEngine(config, FakeAuthentik(), admin, RecordingEffectors())  # type: ignore[arg-type]
    await engine.reconcile_once()
    assert all(room != _LOBBY_ID for room, _ in admin.added)  # no group members injected


_ORPHAN_LOBBY = "!orphanlobby:company.org"


class OrphanLobbyAdmin(FakeAdmin):
    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return [
            *await super().list_non_space_rooms(),
            {"room_id": _ORPHAN_LOBBY, "canonical_alias": "#gone-lobby:company.org", "name": "Gone (Lobby)"},
        ]


async def test_lobby_taken_down_with_its_group_when_group_disappears() -> None:
    # A lobby carries group_id "g-gone"; Authentik no longer returns that group, so the lobby is
    # obsolete and must be blocked — not left as a live, joinable room beside a blocked group room.
    config = _lobby_config()
    room_cfg = config.sync_matrix_rooms_based_on_authentik_groups
    room_cfg.disable_rooms_when_mapped_authentik_group_disappears = True
    admin = OrphanLobbyAdmin()
    effectors = RecordingEffectors()
    effectors.state_store[(_ORPHAN_LOBBY, _LOBBY_EVENT)] = {
        "schema_version": 1,
        "room_type": "visitor_lobby",
        "group_id": "g-gone",
    }
    engine = ReconcilerEngine(config, FakeAuthentik(), admin, effectors)  # type: ignore[arg-type]
    await engine.reconcile_once()
    assert (_ORPHAN_LOBBY, True) in admin.blocked_changes


# --- global room admins ---

_GROUP_EVENT = "org.company.onbot.group_room"


def _global_admin_config(*names: str) -> OnbotConfig:
    return OnbotConfig.model_validate({**_BASE, "global_room_admins": list(names)})


async def test_global_admin_is_seeded_into_a_room_of_a_group_they_are_not_in() -> None:
    # carol is in g2; !room1 is the g1 room, so nothing about her group membership puts her there.
    admin = FakeAdmin()
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(_global_admin_config("carol"), FakeAuthentik(), admin, effectors)  # type: ignore[arg-type]
    await engine.reconcile_once()

    assert ("!room1:company.org", "@carol:company.org") in admin.added
    assert ("!space:company.org", "@carol:company.org") in admin.added
    # PL 100 for carol, alongside the group-derived 50 for alice.
    assert effectors.power_levels == [
        ("!room1:company.org", {"users": {"@alice:company.org": 50, "@carol:company.org": 100}})
    ]
    # ...and recorded in room state so a later pass can take it away again.
    assert effectors.state_store[("!room1:company.org", _GROUP_EVENT)]["global_admins"] == [
        "@carol:company.org"
    ]


async def test_global_admin_is_never_kicked_from_a_room_they_do_not_belong_to() -> None:
    class AdminAlreadyInRoom(FakeAdmin):
        async def list_room_members(self, room_id: str) -> list[str]:
            if room_id == "!space:company.org":
                return ["@alice:company.org"]
            return ["@alice:company.org", "@carol:company.org", "@stale:company.org", "@bot:company.org"]

    admin = AdminAlreadyInRoom()
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(_global_admin_config("carol"), FakeAuthentik(), admin, effectors)  # type: ignore[arg-type]
    await engine.reconcile_once()

    # The kick pass runs (it evicts @stale) but leaves the global admin alone.
    assert effectors.kicks == [("!room1:company.org", "@stale:company.org")]


async def test_global_admin_level_is_withdrawn_after_the_name_leaves_the_config() -> None:
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(_global_admin_config("carol"), FakeAuthentik(), FakeAdmin(), effectors)  # type: ignore[arg-type]
    await engine.reconcile_once()
    assert effectors.state_store[("!room1:company.org", _GROUP_EVENT)]["global_admins"] == [
        "@carol:company.org"
    ]

    # A second bot run with carol removed, reading the room state the first one left behind. Carol
    # is in no g1 group and in no config list, so this record is the only thing that still knows she
    # was ever granted 100 — without it the entry would outlive her membership forever.
    class CarolAtAdmin(RecordingEffectors):
        async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
            return {"users": {"@alice:company.org": 50, "@carol:company.org": 100}}

    second = CarolAtAdmin()
    second.state_store = effectors.state_store
    engine2 = ReconcilerEngine(_global_admin_config(), FakeAuthentik(), FakeAdmin(), second)  # type: ignore[arg-type]
    await engine2.reconcile_once()

    assert second.power_levels == [("!room1:company.org", {"users": {"@alice:company.org": 50}})]
    assert second.state_store[("!room1:company.org", _GROUP_EVENT)]["global_admins"] == []


async def test_global_admin_is_seeded_into_the_lobby_at_full_power() -> None:
    config = _lobby_config()
    config.global_room_admins = ["carol"]
    admin = LobbyAdmin(with_lobby_room=True)
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(config, FakeAuthentik(), admin, effectors)  # type: ignore[arg-type]
    await engine.reconcile_once()

    assert (_LOBBY_ID, "@carol:company.org") in admin.added
    assert (_LOBBY_ID, {"users": {"@carol:company.org": 100}}) in effectors.power_levels
    # The lobby stays add-only: seeding an admin must not turn the kick pass on for visitors.
    assert not any(room == _LOBBY_ID for room, _ in effectors.kicks)


async def test_lobby_gets_no_power_level_write_without_global_admins() -> None:
    # Regression guard: the new lobby PL pass must stay a no-op in the default configuration.
    effectors = RecordingEffectors()
    engine = ReconcilerEngine(_lobby_config(), FakeAuthentik(), LobbyAdmin(with_lobby_room=True), effectors)  # type: ignore[arg-type]
    await engine.reconcile_once()
    assert not any(room == _LOBBY_ID for room, _ in effectors.power_levels)


# --- server-wide room list: rooms the bot is not in ---


def _teardown_config() -> OnbotConfig:
    """Obsolete-room teardown on, as production has it: that is the pass that reads foreign rooms."""
    config = OnbotConfig.model_validate(_BASE)
    room_cfg = config.sync_matrix_rooms_based_on_authentik_groups
    room_cfg.disable_rooms_when_mapped_authentik_group_disappears = True
    return config


class ForeignRoomAdmin(FakeAdmin):
    """The admin room list is server-wide, so it also returns a user's own private room."""

    async def list_non_space_rooms(self) -> list[dict[str, Any]]:
        return [
            *await super().list_non_space_rooms(),
            {"room_id": "!private:company.org", "canonical_alias": None, "name": "Alice's room"},
        ]


class ForbiddenStateEffectors(RecordingEffectors):
    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None:
        if room_id == "!private:company.org":
            raise ApiError(
                "GET",
                f"http://synapse/_matrix/client/v3/rooms/{room_id}/state/{event_type}",
                403,
                {"errcode": "M_FORBIDDEN", "error": "User @bot:company.org not in room ..."},
            )
        return await super().get_room_state(room_id, event_type, state_key)


async def test_a_room_the_bot_is_not_in_does_not_abort_the_pass() -> None:
    """Regression: one user-created private room used to kill every reconcile.

    The 403 escaped `_converge_obsolete_rooms`, which runs *before* the space- and room-membership
    passes, so nobody was added to anything for as long as that room existed. The symptom was
    invisible in the logs beyond a single traceback: onboarding still worked (a different code
    path), so users got their welcome room and no rooms.
    """
    admin = ForeignRoomAdmin()
    effectors = ForbiddenStateEffectors()
    engine = ReconcilerEngine(_teardown_config(), FakeAuthentik(), admin, effectors)  # type: ignore[arg-type]

    await engine.reconcile_once()

    # The pass ran to completion: membership converged and the timestamp was set.
    assert engine.last_reconcile_at is not None
    assert ("!space:company.org", "@bob:company.org") in admin.added
    assert ("!room1:company.org", "@bob:company.org") in admin.added
    # ...and the unreadable room was left completely alone: never blocked, never deleted.
    assert admin.blocked_changes == []
    assert admin.deleted == []


async def test_a_non_403_state_error_is_reported_not_skipped() -> None:
    """The 403 handler is narrow on purpose: a 500 from Synapse is still a real fault.

    Since S2 a fault no longer aborts the pass (REC-03), but it must not be mistaken for "not a
    managed room" either: the room is reported as not converged, which a 403 never is.
    """

    class BrokenStateEffectors(RecordingEffectors):
        async def get_room_state(
            self, room_id: str, event_type: str, state_key: str = ""
        ) -> dict[str, Any] | None:
            raise ApiError("GET", "http://synapse/whatever", 500, {"errcode": "M_UNKNOWN"})

    bus = EventBus()
    reported: list[str] = []

    async def handler(event: Any) -> None:
        reported.append(event.payload["room_id"])

    bus.subscribe(Signal.room_not_converged, handler)
    engine = ReconcilerEngine(
        _teardown_config(), FakeAuthentik(), ForeignRoomAdmin(), BrokenStateEffectors(), bus
    )  # type: ignore[arg-type]
    await engine.reconcile_once()
    assert "!private:company.org" in reported
