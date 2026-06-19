"""Unit tests for power-level computation, incl. withdrawal (G8.4)."""

from onbot.models import MappedUser
from onbot.reconciler.power_levels import (
    PowerLevelGroup,
    compute_desired_user_levels,
    extract_power_level_groups,
    merge_power_levels,
)


def _user(pk: str, mxid: str, *, superuser: bool = False) -> MappedUser:
    return MappedUser(authentik_obj={"pk": pk, "is_superuser": superuser}, mxid=mxid)


def test_extract_power_level_groups_reads_attribute() -> None:
    groups = [
        {"users": [1, 2], "attributes": {"chat-powerlevel": 50}},
        {"users": [3], "attributes": {"chat-powerlevel": "nope"}},  # non-int ignored
        {"users": [4], "attributes": {}},  # missing ignored
    ]
    result = extract_power_level_groups(groups, "chat-powerlevel")
    assert len(result) == 1
    assert result[0].level == 50
    assert result[0].member_pks == {1, 2}


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


def test_merge_withdraws_stale_managed_levels_but_keeps_others() -> None:
    current = {"@a:x": 50, "@bot:x": 100, "@manual:x": 75}
    desired = {"@a:x": 25}
    managed = {"@a:x", "@gone:x"}
    merged = merge_power_levels(current, desired, managed)
    # @a downgraded, @gone has no desired entry so nothing to remove, bot/manual untouched.
    assert merged == {"@a:x": 25, "@bot:x": 100, "@manual:x": 75}


def test_merge_removes_managed_user_who_lost_all_levels() -> None:
    current = {"@a:x": 50, "@bot:x": 100}
    merged = merge_power_levels(current, desired={}, managed_mxids={"@a:x"})
    assert merged == {"@bot:x": 100}
