"""Unit tests for the group→room projection (pure mapping rules)."""

from onbot.config import OnbotConfig
from onbot.models import MatrixRoom
from onbot.reconciler.rooms import (
    build_group_room_maps,
    compute_lobby_attributes,
    compute_room_attributes,
    filter_synced_groups,
    lobby_enabled_for_group,
    resolve_room_settings,
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


def test_room_avatar_url_from_group_attribute() -> None:
    cfg = _config()  # default room_avatar_url_attribute == "chatroom_avatar_url"
    group = {"pk": "g1", "name": "Team", "attributes": {"chatroom_avatar_url": "https://cdn/team.png"}}
    assert compute_room_attributes(group, cfg, "company.org").avatar_source_url == "https://cdn/team.png"


def test_room_avatar_url_absent_is_none() -> None:
    cfg = _config()
    group = {"pk": "g1", "name": "Team", "attributes": {}}
    assert compute_room_attributes(group, cfg, "company.org").avatar_source_url is None


def test_room_avatar_disabled_when_attribute_unset() -> None:
    cfg = _config(sync_matrix_rooms_based_on_authentik_groups={"room_avatar_url_attribute": None})
    group = {"pk": "g1", "name": "Team", "attributes": {"chatroom_avatar_url": "https://cdn/team.png"}}
    assert compute_room_attributes(group, cfg, "company.org").avatar_source_url is None


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


def test_per_group_override_inherits_unset_defaults() -> None:
    """An override naming one key inherits the rest from `matrix_room_default_settings`.

    Regression: the merge used to dump the whole override model (class defaults filling every omitted
    key), throwing the configured defaults away. Here the default's non-class values must survive an
    override that only sets `name_prefix`.
    """
    cfg = _config(
        matrix_room_default_settings={
            "name_prefix": "DZD-",
            "end2end_encryption_enabled": False,
            "topic_prefix": "Group — ",
        },
        per_authentik_group_pk_matrix_room_settings={"g1": {"name_prefix": "[Public] "}},
    )
    resolved = resolve_room_settings("g1", cfg)
    # The one overridden key wins:
    assert resolved.name_prefix == "[Public] "
    # …and every key the override omitted falls back to the configured default, not the class default
    # (class default for end2end_encryption_enabled is True; topic_prefix is None).
    assert resolved.end2end_encryption_enabled is False
    assert resolved.topic_prefix == "Group — "


# --- visitor lobby (ADR-0012) ---


def test_lobby_alias_keeps_its_dash_and_name_topic_from_group() -> None:
    cfg = _config(matrix_room_default_settings={"visitor_lobby_enabled": True})
    settings = cfg.matrix_room_default_settings
    group_attrs = compute_room_attributes(
        {"pk": "duesseldorf", "name": "Düsseldorf", "attributes": {}}, cfg, "company.org"
    )
    lobby = compute_lobby_attributes(group_attrs, settings, "company.org")
    # The group alias is dash-stripped, but the lobby's own -lobby suffix survives (appended after).
    assert lobby.alias == "duesseldorf-lobby"
    assert lobby.canonical_alias == "#duesseldorf-lobby:company.org"
    assert lobby.name == "Düsseldorf (Lobby)"
    assert "Düsseldorf" in lobby.topic
    # Lobbies default to unencrypted, weaker than the group room's default (see ADR-0012).
    assert lobby.encrypted is False
    # Visitors can talk; only the bot rewrites state.
    plco = lobby.room_params["power_level_content_override"]
    assert plco == {"users_default": 0, "events_default": 0, "state_default": 100}


def test_lobby_alias_survives_a_dashed_group_alias() -> None:
    # A group alias derived from a dashed source is stripped to "abcdef"; the lobby suffix still keeps
    # its dash, so "-lobby" is not collateral damage of the group's dash-stripping.
    cfg = _config(matrix_room_default_settings={"visitor_lobby_enabled": True})
    group_attrs = compute_room_attributes(
        {"pk": "ab-cd-ef", "name": "Team", "attributes": {}}, cfg, "company.org"
    )
    lobby = compute_lobby_attributes(group_attrs, cfg.matrix_room_default_settings, "company.org")
    assert lobby.alias == "abcdef-lobby"


def test_lobby_enable_defaults_to_config_flag() -> None:
    settings_on = _config(matrix_room_default_settings={"visitor_lobby_enabled": True})
    settings_off = _config()
    group = {"pk": "g1", "name": "Team", "attributes": {}}
    assert lobby_enabled_for_group(group, settings_on.matrix_room_default_settings) is True
    assert lobby_enabled_for_group(group, settings_off.matrix_room_default_settings) is False


def test_lobby_attribute_overrides_the_config_default() -> None:
    cfg = _config()  # visitor_lobby_enabled defaults to False
    settings = (
        cfg.matrix_room_default_settings
    )  # attribute path defaults to attributes.chatroom_visitor_lobby
    on = {"pk": "g1", "name": "Team", "attributes": {"chatroom_visitor_lobby": True}}
    off = {"pk": "g2", "name": "Team", "attributes": {"chatroom_visitor_lobby": False}}
    assert lobby_enabled_for_group(on, settings) is True
    assert lobby_enabled_for_group(off, settings) is False


def test_lobby_attribute_accepts_stringified_bool() -> None:
    settings = _config().matrix_room_default_settings
    group = {"pk": "g1", "name": "Team", "attributes": {"chatroom_visitor_lobby": "true"}}
    assert lobby_enabled_for_group(group, settings) is True


def test_lobby_invalid_attribute_falls_back_to_default() -> None:
    # Garbage in the attribute is ignored and the configured default (True here) applies.
    cfg = _config(matrix_room_default_settings={"visitor_lobby_enabled": True})
    settings = cfg.matrix_room_default_settings
    group = {"pk": "g1", "name": "Team", "attributes": {"chatroom_visitor_lobby": "yes-please"}}
    assert lobby_enabled_for_group(group, settings) is True


def test_build_maps_pairs_lobby_by_alias() -> None:
    cfg = _config(matrix_room_default_settings={"visitor_lobby_enabled": True})
    groups = [{"pk": "g1", "name": "Team", "attributes": {}}]
    rooms = [
        MatrixRoom(room_id="!r:company.org", canonical_alias="#g1:company.org", name="Team"),
        MatrixRoom(room_id="!l:company.org", canonical_alias="#g1-lobby:company.org", name="Team (Lobby)"),
    ]
    maps = build_group_room_maps(groups, rooms, cfg, "company.org")
    assert maps[0].room is not None and maps[0].room.room_id == "!r:company.org"
    assert maps[0].lobby_desired is not None
    assert maps[0].lobby is not None and maps[0].lobby.room_id == "!l:company.org"


def test_build_maps_no_lobby_when_disabled() -> None:
    cfg = _config()  # lobby off by default
    groups = [{"pk": "g1", "name": "Team", "attributes": {}}]
    maps = build_group_room_maps(groups, [], cfg, "company.org")
    assert maps[0].lobby_desired is None
    assert maps[0].lobby is None
