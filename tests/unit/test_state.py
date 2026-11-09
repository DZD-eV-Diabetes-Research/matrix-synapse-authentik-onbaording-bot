"""Unit tests for the versioned onbot room-state schemas."""

from onbot.state import (
    SCHEMA_VERSION,
    DirectRoomState,
    GroupRoomState,
    OnbotRoomType,
    dump_room_state,
    event_type_name,
    parse_room_state,
)


def test_event_type_name_reverses_domain() -> None:
    assert event_type_name("company.org", OnbotRoomType.group_room) == "org.company.onbot.group_room"
    assert event_type_name("a.b.c", "space") == "c.b.a.onbot.space"


def test_group_room_state_roundtrip() -> None:
    state = GroupRoomState(group_id="pk-1")
    content = dump_room_state(state)
    assert content["schema_version"] == SCHEMA_VERSION
    assert content["room_type"] == "group_room"

    parsed = parse_room_state(OnbotRoomType.group_room, content)
    assert isinstance(parsed, GroupRoomState)
    assert parsed.group_id == "pk-1"


def test_direct_room_state_defaults() -> None:
    state = DirectRoomState(user_id="@alice:company.org")
    assert state.welcome_messages_sent == {}
    assert state.disabled_user_timestamp is None
    parsed = parse_room_state(OnbotRoomType.direct_room, dump_room_state(state))
    assert isinstance(parsed, DirectRoomState)
    assert parsed.user_id == "@alice:company.org"


def test_marker_no_longer_carries_the_authentik_url() -> None:
    """SEC-11: the (often internal) Authentik URL was readable by every room member."""
    legacy = {
        "schema_version": 1,
        "room_type": "group_room",
        "group_id": "g1",
        "authentik_server": "http://ak:9000",
    }
    parsed = parse_room_state(OnbotRoomType.group_room, legacy)
    assert "authentik_server" not in dump_room_state(parsed)


def test_direct_room_setup_complete_defaults_for_legacy_markers() -> None:
    legacy = {"schema_version": 1, "room_type": "direct_room", "user_id": "@a:company.org"}
    parsed = parse_room_state(OnbotRoomType.direct_room, legacy)
    assert isinstance(parsed, DirectRoomState)
    assert parsed.setup_complete is True
