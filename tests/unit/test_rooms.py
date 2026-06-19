"""Unit tests for the group→room projection (pure mapping rules)."""

from onbot.config import OnbotConfig
from onbot.models import MatrixRoom
from onbot.reconciler.rooms import (
    build_group_room_maps,
    compute_room_attributes,
    filter_synced_groups,
)

_BASE = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
}


def _config(**overrides: object) -> OnbotConfig:
    return OnbotConfig.model_validate(_BASE | overrides)


def test_alias_strips_dashes_and_builds_canonical() -> None:
    cfg = _config()
    group = {"pk": "abc-def-123", "name": "Team", "attributes": {}}
    attrs = compute_room_attributes(group, cfg, "company.org")
    assert attrs.alias == "abcdef123"
    assert attrs.canonical_alias == "#abcdef123:company.org"
    assert attrs.name == "Team"
    assert attrs.encrypted is True


def test_topic_and_room_params_from_attributes() -> None:
    cfg = _config()
    group = {
        "pk": "g1",
        "name": "Team",
        "attributes": {
            "chatroom_topic": "hello",
            "chatroom_params": '{"federate": false}',
        },
    }
    attrs = compute_room_attributes(group, cfg, "company.org")
    assert attrs.topic == "hello"
    # default params merged with the per-group JSON (the legacy split-path bug is fixed)
    assert attrs.room_params["federate"] is False
    assert attrs.room_params["preset"] == "private_chat"


def test_name_prefix_applied_once() -> None:
    cfg = _config(matrix_room_default_settings={"name_prefix": "DZD-"})
    group = {"pk": "g1", "name": "Team", "attributes": {}}
    assert compute_room_attributes(group, cfg, "company.org").name == "DZD-Team"


def test_filter_synced_groups_rules() -> None:
    cfg = _config(
        authentik_group_id_ignore_list=["ignore-me"],
        sync_matrix_rooms_based_on_authentik_groups={"only_for_groupnames_starting_with": "chat-"},
    )
    groups = [
        {"pk": "ignore-me", "name": "chat-a"},
        {"pk": "g2", "name": "chat-b"},
        {"pk": "g3", "name": "other"},
    ]
    result = filter_synced_groups(groups, cfg)
    assert [g["pk"] for g in result] == ["g2"]


def test_build_maps_matches_existing_room_by_alias() -> None:
    cfg = _config()
    groups = [{"pk": "g1", "name": "Team", "attributes": {}}]
    rooms = [MatrixRoom(room_id="!r:company.org", canonical_alias="#g1:company.org", name="Team")]
    maps = build_group_room_maps(groups, rooms, cfg, "company.org")
    assert len(maps) == 1
    assert maps[0].room is not None
    assert maps[0].room.room_id == "!r:company.org"


def test_build_maps_no_match_leaves_room_none() -> None:
    cfg = _config()
    groups = [{"pk": "g1", "name": "Team", "attributes": {}}]
    maps = build_group_room_maps(groups, [], cfg, "company.org")
    assert maps[0].room is None
