"""Unit tests for membership diff logic."""

from onbot.models import MappedUser
from onbot.reconciler.membership import (
    desired_room_members,
    diff_room_membership,
    diff_space_membership,
)


def _user(mxid: str, *group_pks: str) -> MappedUser:
    return MappedUser(
        authentik_obj={"pk": mxid, "groups_obj": [{"pk": g} for g in group_pks]},
        mxid=mxid,
    )


def test_desired_members_use_exact_pk_match() -> None:
    # Regression for the legacy substring bug: pk "g1" must not match a user in group "g12".
    users = [_user("@a:x", "g1"), _user("@b:x", "g12"), _user("@c:x", "g1", "g2")]
    assert desired_room_members("g1", users) == {"@a:x", "@c:x"}


def test_diff_adds_and_kicks() -> None:
    diff = diff_room_membership(
        desired_mxids={"@a:x", "@b:x"},
        actual_member_ids=["@b:x", "@stale:x"],
        kick_enabled=True,
    )
    assert diff.to_add == ["@a:x"]
    assert diff.to_kick == ["@stale:x"]


def test_diff_respects_kick_toggle_and_protected() -> None:
    diff = diff_room_membership(
        desired_mxids=set(),
        actual_member_ids=["@stale:x", "@bot:x"],
        kick_enabled=False,
        protected_ids=["@bot:x"],
    )
    assert diff.to_kick == []

    diff2 = diff_room_membership(
        desired_mxids=set(),
        actual_member_ids=["@stale:x", "@bot:x"],
        kick_enabled=True,
        protected_ids=["@bot:x"],
    )
    assert diff2.to_kick == ["@stale:x"]


def test_space_membership_only_adds() -> None:
    users = [_user("@a:x"), _user("@b:x")]
    diff = diff_space_membership(users, ["@a:x", "@extra:x"])
    assert diff.to_add == ["@b:x"]
    assert diff.to_kick == []


def test_global_admins_are_desired_in_every_room_regardless_of_group() -> None:
    users = [_user("@a:x", "g1"), _user("@b:x", "g2")]
    assert desired_room_members("g1", users, always_members=["@admin:x"]) == {"@a:x", "@admin:x"}
    # ...including a room whose group has no members at all.
    assert desired_room_members("g9", users, always_members=["@admin:x"]) == {"@admin:x"}


def test_global_admin_survives_the_kick_pass_when_protected() -> None:
    # The pairing that matters: a global admin is desired *and* protected. Protection alone would
    # still leave them in, but desired-without-protected is the trap — they are in no group, so a
    # kick-enabled pass would evict them from every room the moment the two lists disagree.
    diff = diff_room_membership(
        desired_mxids=desired_room_members("g1", [], always_members=["@admin:x"]),
        actual_member_ids=["@admin:x", "@stale:x"],
        kick_enabled=True,
        protected_ids=["@bot:x", "@admin:x"],
    )
    assert diff.to_add == []
    assert diff.to_kick == ["@stale:x"]


def test_global_admins_are_added_to_the_space() -> None:
    diff = diff_space_membership([_user("@a:x")], ["@a:x"], always_members=["@admin:x"])
    assert diff.to_add == ["@admin:x"]
