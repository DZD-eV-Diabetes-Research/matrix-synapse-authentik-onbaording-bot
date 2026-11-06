"""``reconcile-once --dry-run`` (MNT-09) and the admin-room wiring of the guard (review session S3).

A dry run reads the live state and logs every write instead of sending it, including the Synapse admin
writes that used to bypass the effector seam, and saves no bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from onbot.admin.control_room import ControlRoomHandler
from onbot.app import build_app
from onbot.cli import build_parser
from onbot.config import AdminRoom, AuthentikServer, OnbotConfig, SynapseServer
from onbot.events import Signal
from onbot.guard import ReadOnlyGuardStore
from onbot.reconciler.effectors import DryRunEffectors, DryRunSynapseAdmin, is_synthetic_room
from onbot.reconciler.state import GroupRoomState
from onbot.rooms.admin import AdminRoomProvisioner
from onbot.rooms.provision import ManagedRoomSpec
from tests.unit.test_engine import _BASE, FakeAdmin, FakeAuthentik, RecordingEffectors


def _config() -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="example.test",
            server_url="https://matrix.test",
            bot_user_id="@bot:example.test",
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="ak"),
        admin_room=AdminRoom(enabled=False),
    )


# --- the recording wrappers -----------------------------------------------------------------------


class _Live(RecordingEffectors):
    """The live effectors: they know the room's state and power levels."""

    def __init__(self) -> None:
        super().__init__()
        self.state_store[("!real:x", "m.room.topic")] = {"topic": "live"}

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]:
        return {"users": {"@a:x": 50}}


async def test_dry_run_effectors_read_live_and_write_nothing(caplog: pytest.LogCaptureFixture) -> None:
    live = _Live()
    dry = DryRunEffectors(reads=live)
    assert await dry.get_room_state("!real:x", "m.room.topic") == {"topic": "live"}
    assert await dry.get_room_power_levels("!real:x") == {"users": {"@a:x": 50}}

    with caplog.at_level(logging.INFO):
        await dry.kick_user("!real:x", "@a:x", "gone")
        await dry.set_room_power_levels("!real:x", {"users": {}})
        room_id = await dry.create_managed_room(
            ManagedRoomSpec(marker=GroupRoomState(group_id="g"), alias_localpart="g", name="G")
        )
    assert live.kicks == [] and live.power_levels == []
    assert "[dry-run] would kick @a:x from !real:x" in caplog.text
    # A room only the dry run created reads as empty instead of failing on the server.
    assert is_synthetic_room(room_id)
    assert await dry.get_room_state(room_id, "m.room.topic") is None
    assert await dry.get_room_power_levels(room_id) == {}


async def test_dry_run_admin_reads_through_and_logs_its_writes(caplog: pytest.LogCaptureFixture) -> None:
    inner = FakeAdmin()
    dry = DryRunSynapseAdmin(inner)
    assert await dry.list_users() == await inner.list_users()
    assert await dry.list_room_members("!dryrun-abc:dry-run") == []
    with caplog.at_level(logging.INFO):
        await dry.add_user_to_room("!r:x", "@a:x")
        await dry.room_set_blocked("!r:x", blocked=True)
        await dry.delete_room("!r:x", purge=True)
    assert inner.added == [] and inner.blocked_changes == [] and inner.deleted == []
    assert "[dry-run] would join @a:x to !r:x" in caplog.text
    assert "[dry-run] would block room !r:x" in caplog.text


async def test_a_dry_run_pass_reports_what_the_guard_would_pause(caplog: pytest.LogCaptureFixture) -> None:
    from onbot.reconciler.engine import ReconcilerEngine

    class Nobody(FakeAuthentik):
        async def list_users(self, **_: Any) -> list[dict[str, Any]]:
            return []

    admin = FakeAdmin()
    live = RecordingEffectors()
    engine = ReconcilerEngine(
        OnbotConfig.model_validate(_BASE),
        Nobody(),  # type: ignore[arg-type]
        DryRunSynapseAdmin(admin),  # type: ignore[arg-type]
        DryRunEffectors(reads=live),
        dry_run=True,
    )
    with caplog.at_level(logging.INFO):
        await engine.reconcile_once()
    assert live.kicks == []
    assert "[dry-run] guard: paused" in caplog.text
    assert "[dry-run] guard: paused: kick @alice:company.org" in caplog.text
    assert await engine.guard.open_batches() == []  # nothing recorded


# --- wiring ------------------------------------------------------------------------------------------


async def test_build_app_dry_run_wires_recorders_and_read_only_stores() -> None:
    config = _config()
    config.admin_room.enabled = True
    life = config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
    life.dry_run = False
    async with build_app(config, dry_run=True) as app:
        assert app.engine.dry_run
        assert isinstance(app.engine.effectors, DryRunEffectors)
        assert isinstance(app.engine.admin, DryRunSynapseAdmin)
        assert isinstance(app.guard.store, ReadOnlyGuardStore)
        assert app.engine.lifecycle is not None and app.engine.lifecycle.dry_run is True
        assert app.engine.lifecycle.cfg.dry_run is False  # plans as configured, acts as a dry run
        assert app.control_room is None and app.notifier is None  # nothing is posted or read
        assert [s.name for s in app.reconcile_once_steps()] == ["version negotiation"]
    assert life.dry_run is False


async def test_the_notifier_is_bound_with_the_control_room(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    config.admin_room.enabled = True

    async def ensure(_self: AdminRoomProvisioner) -> str:
        return "!control:example.test"

    async def start(self: ControlRoomHandler, room_id: str) -> None:
        self.room_id = room_id

    monkeypatch.setattr(AdminRoomProvisioner, "ensure", ensure)
    monkeypatch.setattr(ControlRoomHandler, "start", start)
    async with build_app(config) as app:
        assert app.notifier is not None
        assert app.control_room is not None and app.control_room.guard is app.engine.guard
        sent: list[tuple[str, str]] = []

        async def send(room_id: str, body: str, **_: Any) -> str:
            sent.append((room_id, body))
            return "$e"

        monkeypatch.setattr(app.matrix, "send_text_message", send)
        await app.events.emit(
            Signal.destructive_actions_suppressed, action="kick", reason="r", summary="paused"
        )
        assert sent == []  # queued until the room is bound
        await app.start_control_room()
        assert sent == [("!control:example.test", "paused")]


def test_reconcile_once_takes_a_dry_run_flag() -> None:
    parser = build_parser()
    assert parser.parse_args(["reconcile-once", "--dry-run"]).dry_run is True
    assert parser.parse_args(["reconcile-once"]).dry_run is False
