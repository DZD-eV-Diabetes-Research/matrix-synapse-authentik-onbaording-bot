"""Unit tests for the versioned onbot room-state schemas."""

from onbot.reconciler.state import (
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
    state = GroupRoomState(group_id="pk-1", authentik_server="https://authentik/")
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
