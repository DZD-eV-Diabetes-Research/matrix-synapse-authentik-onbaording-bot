"""Unit tests for the dry-run Matrix effectors (the safe, mutation-free default seam)."""

from __future__ import annotations

from onbot.models import RoomCreateAttributes
from onbot.reconciler.effectors import DryRunEffectors, MatrixEffectors


def test_dry_run_effectors_satisfy_protocol() -> None:
    assert isinstance(DryRunEffectors(), MatrixEffectors)


async def test_dry_run_effectors_mutate_nothing_and_return_synthetic_ids() -> None:
    eff = DryRunEffectors()
    attrs = RoomCreateAttributes(alias="team", canonical_alias="#team:matrix.test", name="Team", topic="t")

    room_id = await eff.create_group_room(attrs, "!space:matrix.test")
    space_id = await eff.create_space(alias="onbot", name="OnBot", topic="s", params={})

    # Synthetic ids are well-formed Matrix room ids and unique per call.
    assert room_id.startswith("!dryrun-") and room_id.endswith(":dry-run")
    assert space_id.startswith("!dryrun-")
    assert room_id != space_id

    # Read returns an empty power-level map; every write is a no-op that must not raise.
    assert await eff.get_room_power_levels("!r:x") == {}
    await eff.kick_user("!r:x", "@u:x", "gone")
    await eff.set_room_power_levels("!r:x", {"users": {"@a:x": 50}})
    await eff.set_room_name("!r:x", "Team")
    await eff.set_room_topic("!r:x", "topic")
    await eff.put_room_state("!r:x", "org.onbot.state", {"v": 1})
