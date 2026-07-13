"""The pure power-level rules behind the read-only onboarding room."""

from __future__ import annotations

from onbot.onboarding.notice_board import notice_board_power_levels, power_level_drift

BOT = "@bot:matrix.test"


def test_the_user_is_mute_and_the_bot_creator_is_not_named() -> None:
    content = notice_board_power_levels()

    # The bot creates the DM, so it is the room creator: under room version 12 it holds an infinite
    # power level and MUST NOT appear in m.room.power_levels (the auth rules reject a creator in the
    # `users` map), and on older versions the server seats the creator at 100 for us. So the override
    # omits `users` entirely — absent, not powerless.
    assert "users" not in content
    assert content["users_default"] == 0
    # Sending a message costs 50, so the user at 0 cannot post; nor can they kick the bot out.
    for key in ("events_default", "state_default", "invite", "kick", "ban", "redact"):
        assert content[key] == 50


def test_no_drift_reported_for_a_room_that_already_matches() -> None:
    assert power_level_drift(notice_board_power_levels()) is None


def test_no_drift_when_a_v12_room_omits_the_creator_from_users() -> None:
    # A v12 room the bot created: the bot (creator) is absent from `users` by design. Drift detection
    # must not read that absence as something to repair — it would try to write the creator back in
    # and be rejected on every welcome tick.
    v12_room = {**notice_board_power_levels(), "users": {}}
    assert power_level_drift(v12_room) is None


def test_drift_is_repaired_without_disturbing_unrelated_keys() -> None:
    current = {
        **notice_board_power_levels(),
        "events_default": 0,  # somebody re-opened the composer
        "notifications": {"room": 50},  # not ours to touch
        "events": {"m.room.name": 50},
    }

    repaired = power_level_drift(current)

    assert repaired is not None
    assert repaired["events_default"] == 50
    assert repaired["notifications"] == {"room": 50}
    assert repaired["events"] == {"m.room.name": 50}


def test_drift_leaves_the_users_map_exactly_as_found() -> None:
    # Whatever is in `users` — a legacy v11 bot entry, a second admin somebody deliberately added, or
    # nothing at all for a v12 room — is not the notice board's business. Drift repairs only the
    # gated keys and never touches `users`, so it never tries to (re)seat the creator.
    current = {**notice_board_power_levels(), "users_default": 50, "users": {"@carol:matrix.test": 100}}

    repaired = power_level_drift(current)

    assert repaired is not None
    assert repaired["users_default"] == 0
    assert repaired["users"] == {"@carol:matrix.test": 100}
