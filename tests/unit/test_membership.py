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
