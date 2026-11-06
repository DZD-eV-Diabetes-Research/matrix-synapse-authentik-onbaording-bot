"""What the bot may change in a room's power levels (REC-05) and which entries it owns (D10, REC-08)."""

from __future__ import annotations

from typing import Any

import pytest

from onbot.reconciler.power_levels import (
    RoomAuthority,
    creators_are_privileged,
    hold_withdrawals,
    plan_power_levels,
    room_authority,
)

BOT = "@bot:x"
ALICE = "@alice:x"
BOB = "@bob:x"
MALLORY = "@mallory:x"

# A pre-v12 room as the bot created it: the bot is an ordinary `users` entry at 100.
LEGACY = RoomAuthority(bot_level=100, creators=frozenset(), users_default=0)
# A v12 room the bot created: the bot is an infinite creator; Mallory was planted as an extra creator.
V12 = RoomAuthority(bot_level=None, creators=frozenset({MALLORY}), users_default=0)


@pytest.mark.parametrize(
    ("version", "privileged"),
    [("1", False), ("10", False), ("11", False), ("12", True), ("13", True), ("org.matrix.hydra.11", True)],
)
def test_creators_are_privileged_from_room_version_12(version: str, privileged: bool) -> None:
    assert creators_are_privileged(version) is privileged


def test_unknown_room_version_is_treated_as_pre_v12() -> None:
    """The safe reading: a finite bot level only ever makes the bot grant less."""
    assert creators_are_privileged(None) is False
    assert creators_are_privileged("org.example.custom") is False


def test_authority_in_a_v12_room_the_bot_created() -> None:
    authority = room_authority({"users": {ALICE: 50}}, BOT, room_version="12", creators={BOT, MALLORY})
    assert authority.bot_level is None
    assert authority.creators == frozenset({MALLORY})
    assert authority.max_grant is None


def test_authority_in_a_legacy_room() -> None:
    authority = room_authority({"users": {BOT: 100, ALICE: 50}}, BOT, room_version="10", creators={BOT})
    assert authority.bot_level == 100
    assert authority.max_grant == 99
    assert authority.creators == frozenset()  # pre-v12 creators are ordinary entries


def test_authority_in_a_v12_room_somebody_else_created() -> None:
    authority = room_authority({"users": {BOT: 100}}, BOT, room_version="12", creators={MALLORY})
    assert authority.bot_level == 100
    assert authority.creators == frozenset({MALLORY})


def test_string_power_levels_of_old_rooms_are_read_as_numbers() -> None:
    authority = room_authority(
        {"users": {BOT: "100"}, "users_default": "0"}, BOT, room_version="4", creators=set()
    )
    assert authority.bot_level == 100


def _plan(
    current: dict[str, Any],
    desired: dict[str, int],
    recorded: dict[str, int | None] | None = None,
    authority: RoomAuthority = LEGACY,
) -> Any:
    return plan_power_levels(current, desired, recorded or {}, authority, bot_id=BOT)


# --- REC-05: never grant the bot's own level in a pre-v12 room, never fight what cannot change ---


def test_a_grant_at_the_bots_level_is_capped_one_below_in_a_legacy_room() -> None:
    plan = _plan({BOT: 100}, {ALICE: 100})
    assert plan.users == {BOT: 100, ALICE: 99}
    assert plan.capped == {ALICE: 100}
    assert plan.granted == {ALICE: 99}


def test_a_v12_room_grants_100_unchanged() -> None:
    plan = _plan({}, {ALICE: 100}, authority=V12)
    assert plan.users == {ALICE: 100}
    assert plan.capped == {}


def test_a_user_at_the_bots_level_is_not_demoted_but_reported() -> None:
    plan = _plan({BOT: 100, ALICE: 100}, {}, recorded={ALICE: 100})
    assert plan.users == {BOT: 100, ALICE: 100}
    assert ALICE in plan.blocked
    # Still recorded: once a human lowers the bot's rival, the withdrawal can happen.
    assert plan.granted == {ALICE: 100}


def test_a_user_at_the_bots_level_who_should_have_less_is_reported() -> None:
    plan = _plan({BOT: 100, ALICE: 100}, {ALICE: 50})
    assert plan.users[ALICE] == 100
    assert ALICE in plan.blocked


def test_a_user_at_the_bots_level_who_should_be_there_is_left_alone_quietly() -> None:
    """A superuser granted 100 by an older bot version: the level is what is wanted, nothing to do."""
    plan = _plan({BOT: 100, ALICE: 100}, {ALICE: 100})
    assert plan.users == {BOT: 100, ALICE: 100}
    assert plan.blocked == {}
    assert plan.granted == {ALICE: 100}  # owned now, so losing superuser later is reported


def test_an_extra_creator_is_never_named_in_a_v12_room() -> None:
    plan = _plan({}, {MALLORY: 50, ALICE: 50}, authority=V12)
    assert plan.users == {ALICE: 50}
    assert MALLORY in plan.blocked


# --- REC-12: the bot never lands in the users map ---


def test_the_bot_is_never_written() -> None:
    plan = _plan({}, {BOT: 100, ALICE: 50}, recorded={BOT: 100}, authority=V12)
    assert plan.users == {ALICE: 50}
    assert BOT not in plan.granted


# --- D10 / REC-08: withdraw only what the bot itself set ---


def test_a_level_the_bot_granted_is_withdrawn_when_no_longer_wanted() -> None:
    plan = _plan({ALICE: 50}, {}, recorded={ALICE: 50}, authority=V12)
    assert plan.users == {}
    assert plan.granted == {}


def test_a_level_a_human_set_is_never_withdrawn() -> None:
    plan = _plan({BOB: 50}, {}, recorded={}, authority=V12)
    assert plan.users == {BOB: 50}
    assert plan.blocked == {}


def test_a_bot_grant_that_a_human_changed_afterwards_becomes_theirs() -> None:
    plan = _plan({ALICE: 75}, {}, recorded={ALICE: 50}, authority=V12)
    assert plan.users == {ALICE: 75}
    assert plan.released == (ALICE,)
    assert plan.granted == {}


def test_a_recorded_entry_that_is_already_gone_is_forgotten() -> None:
    plan = _plan({}, {}, recorded={ALICE: 50}, authority=V12)
    assert plan.users == {}
    assert plan.granted == {}


def test_a_legacy_record_without_a_level_is_trusted() -> None:
    """``global_admins`` in the room state predates D10 and carries no level: the bot did set them."""
    plan = _plan({ALICE: 100}, {}, recorded={ALICE: None}, authority=V12)
    assert plan.users == {}


def test_a_wanted_level_is_enforced_over_a_human_edit() -> None:
    """D10 is about withdrawal; a user who qualifies for a level still converges to it."""
    plan = _plan({ALICE: 75}, {ALICE: 50}, recorded={ALICE: 50}, authority=V12)
    assert plan.users == {ALICE: 50}
    assert plan.granted == {ALICE: 50}


# --- D13: withdrawals wait for the destructive-action guard ----------------------------------------


def test_held_withdrawals_keep_the_entry_and_the_record() -> None:
    current = {BOT: 100, ALICE: 50, BOB: 100}
    # Alice was granted 50 by the bot; Bob is a global admin from before the record (level unknown).
    recorded: dict[str, int | None] = {ALICE: 50, BOB: None}
    plan = plan_power_levels(current, {}, recorded, V12, bot_id=BOT)
    assert ALICE not in plan.users and BOB not in plan.users

    held = hold_withdrawals(plan, current, recorded, withdraw=frozenset({ALICE}), users_default=0)
    assert held.held == (BOB,)
    assert held.users == {BOT: 100, BOB: 100}  # Alice goes, Bob stays at his level
    assert held.granted == {BOB: 100}  # and stays recorded, so it can be withdrawn once permitted


def test_nothing_held_leaves_the_plan_alone() -> None:
    current = {BOT: 100, ALICE: 50}
    plan = plan_power_levels(current, {}, {ALICE: 50}, V12, bot_id=BOT)
    assert hold_withdrawals(plan, current, {ALICE: 50}, withdraw={ALICE}, users_default=0) == plan
