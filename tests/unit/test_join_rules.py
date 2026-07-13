"""Unit tests for the pure join-rule computation (ADR-0012)."""

from onbot.reconciler.join_rules import desired_join_rules, join_rules_change
from onbot.reconciler.state import OnbotRoomType


def test_lobby_is_restricted_to_the_space() -> None:
    content = desired_join_rules(OnbotRoomType.visitor_lobby, "!space:company.org")
    assert content == {
        "join_rule": "restricted",
        "allow": [{"type": "m.room_membership", "room_id": "!space:company.org"}],
    }


def test_group_room_join_rule_is_not_governed() -> None:
    # The private group room keeps its invite rule; the bot has no opinion.
    assert desired_join_rules(OnbotRoomType.group_room, "!space:company.org") is None


def test_other_room_kinds_are_not_governed() -> None:
    for rt in (OnbotRoomType.space, OnbotRoomType.direct_room, OnbotRoomType.admin_room):
        assert desired_join_rules(rt, "!space:company.org") is None


def test_change_is_none_when_already_restricted() -> None:
    desired = desired_join_rules(OnbotRoomType.visitor_lobby, "!space:company.org")
    assert join_rules_change(desired, desired) is None  # no-op tick sends nothing


def test_change_returns_desired_when_current_differs() -> None:
    desired = desired_join_rules(OnbotRoomType.visitor_lobby, "!space:company.org")
    assert join_rules_change({"join_rule": "invite"}, desired) == desired


def test_change_is_none_when_bot_has_no_opinion() -> None:
    assert join_rules_change({"join_rule": "invite"}, None) is None
