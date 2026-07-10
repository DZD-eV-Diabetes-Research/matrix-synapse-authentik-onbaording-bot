"""The pure power-level rules behind the read-only onboarding room."""

from __future__ import annotations

from onbot.onboarding.notice_board import notice_board_power_levels, power_level_drift

BOT = "@bot:matrix.test"


def test_bot_is_admin_and_everyone_else_is_mute() -> None:
    content = notice_board_power_levels(BOT)

    assert content["users"] == {BOT: 100}
    assert content["users_default"] == 0
    # Sending a message costs 50, so the user at 0 cannot post; nor can they kick the bot out.
    for key in ("events_default", "state_default", "invite", "kick", "ban", "redact"):
        assert content[key] == 50


def test_no_drift_reported_for_a_room_that_already_matches() -> None:
    assert power_level_drift(notice_board_power_levels(BOT), BOT) is None


def test_drift_is_repaired_without_disturbing_unrelated_keys() -> None:
    current = {
        **notice_board_power_levels(BOT),
        "events_default": 0,  # somebody re-opened the composer
        "notifications": {"room": 50},  # not ours to touch
        "events": {"m.room.name": 50},
    }

    repaired = power_level_drift(current, BOT)

    assert repaired is not None
    assert repaired["events_default"] == 50
    assert repaired["notifications"] == {"room": 50}
    assert repaired["events"] == {"m.room.name": 50}


def test_drift_restores_the_bot_and_keeps_other_admins() -> None:
    current = {**notice_board_power_levels(BOT), "users": {"@carol:matrix.test": 100}}

    repaired = power_level_drift(current, BOT)

    assert repaired is not None
    # The bot takes its level back; a second admin somebody deliberately added stays.
    assert repaired["users"] == {BOT: 100, "@carol:matrix.test": 100}


def test_drift_demotes_a_user_who_was_granted_a_default_voice() -> None:
    current = {**notice_board_power_levels(BOT), "users_default": 50}
    repaired = power_level_drift(current, BOT)
    assert repaired is not None and repaired["users_default"] == 0
