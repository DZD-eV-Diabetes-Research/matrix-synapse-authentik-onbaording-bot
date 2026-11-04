"""Unit tests for power-level computation, incl. withdrawal (G8.4)."""

from typing import Any

from onbot.models import MappedUser
from onbot.reconciler.power_levels import (
    PowerLevelGroup,
    apply_global_room_admins,
    compute_desired_user_levels,
    extract_power_level_groups,
    legacy_user_matches_or_outranks_creator,
)

BOT = "@bot:matrix.test"
USER = "@alice:matrix.test"


def _user(pk: str, mxid: str, *, superuser: bool = False) -> MappedUser:
    return MappedUser(authentik_obj={"pk": pk, "is_superuser": superuser}, mxid=mxid)


def test_extract_power_level_groups_reads_attribute() -> None:
    groups: list[dict[str, Any]] = [
        {"users": ["1", "2"], "attributes": {"chat-powerlevel": 50}},
        {"users": ["3"], "attributes": {"chat-powerlevel": "nope"}},  # non-int ignored
        {"users": ["4"], "attributes": {}},  # missing ignored
    ]
    result = extract_power_level_groups(groups, "chat-powerlevel")
    assert len(result) == 1
    assert result[0].level == 50
    assert result[0].member_pks == {"1", "2"}


def test_highest_level_wins_and_superuser_is_admin() -> None:
    members = [_user("u1", "@a:x"), _user("u2", "@b:x", superuser=True)]
    groups = [
        PowerLevelGroup(member_pks={"u1"}, level=25),
        PowerLevelGroup(member_pks={"u1"}, level=50),
    ]
    desired = compute_desired_user_levels(members, groups, make_superusers_admin=True)
    assert desired == {"@a:x": 50, "@b:x": 100}


def test_superuser_admin_can_be_disabled() -> None:
    members = [_user("u2", "@b:x", superuser=True)]
    desired = compute_desired_user_levels(members, [], make_superusers_admin=False)
    assert desired == {}


# --- v12 creator authority (the single rule the room-version audit enforces) ------------------


def test_v12_room_creator_absent_from_users_is_never_matched() -> None:
    # A v12 DM: the bot created it, so it is the infinite-power creator and is deliberately absent
    # from `users`. Even a user sitting at 100 does not match it — the bot stays authoritative, and
    # the room never qualifies for the destructive recreate-dm-rooms migration.
    v12 = {"users": {USER: 100}, "users_default": 0}
    assert legacy_user_matches_or_outranks_creator(v12, USER, BOT) is False


def test_v12_room_with_empty_users_treats_creator_as_infinite() -> None:
    assert legacy_user_matches_or_outranks_creator({"users": {}, "users_default": 0}, USER, BOT) is False


def test_legacy_room_where_user_equals_the_bot_is_flagged() -> None:
    # A pre-v12 trusted_private_chat room: both bot and user seated at 100. The bot cannot demote the
    # user (spec forbids touching an entry >= your own), so this is exactly the room the migration
    # must recreate.
    legacy = {"users": {BOT: 100, USER: 100}, "users_default": 0}
    assert legacy_user_matches_or_outranks_creator(legacy, USER, BOT) is True


def test_legacy_room_where_user_is_below_the_bot_is_healthy() -> None:
    legacy = {"users": {BOT: 100, USER: 0}, "users_default": 0}
    assert legacy_user_matches_or_outranks_creator(legacy, USER, BOT) is False


def test_legacy_room_reads_an_unlisted_user_at_users_default() -> None:
    legacy = {"users": {BOT: 50}, "users_default": 100}
    assert legacy_user_matches_or_outranks_creator(legacy, USER, BOT) is True


def test_global_admins_override_their_group_level() -> None:
    # Not a max(): a global admin in a level-50 group is still an admin, and would silently drop to
    # 50 if this merged the two the way multiple power-level groups are merged.
    members = [_user("u1", "@a:x")]
    groups = [PowerLevelGroup(member_pks={"u1"}, level=50)]
    desired = compute_desired_user_levels(members, groups, make_superusers_admin=False)
    assert apply_global_room_admins(desired, ["@a:x"]) == {"@a:x": 100}


def test_global_admins_get_a_level_without_being_group_members() -> None:
    assert apply_global_room_admins({}, ["@admin:x"]) == {"@admin:x": 100}
    assert apply_global_room_admins({"@a:x": 50}, []) == {"@a:x": 50}
