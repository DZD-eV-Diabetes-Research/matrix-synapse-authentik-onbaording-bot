"""The shared sync pump: stream position, backoff, stop, and fan-out to handlers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from onbot.clients.base import ApiError
from onbot.clients.matrix import RoomSync, SyncNotSupportedError, SyncResult
from onbot.heartbeat import Heartbeat
from onbot.sync import SyncPump


class _ScriptedClient:
    """A fake Matrix client whose ``sliding_sync`` yields scripted results, then stops the pump.

    Once the script is exhausted it requests stop and returns an empty result, so the final scripted
    batch is fully dispatched before ``run()`` exits (the loop only checks stop between iterations).
    """

    def __init__(self, pump: SyncPump, results: list[object]) -> None:
        self._pump = pump
        self._results = list(results)
        self.calls = 0
        self.positions: list[str | None] = []

    async def sliding_sync(self, pos: str | None) -> SyncResult:
        self.calls += 1
        self.positions.append(pos)
        if not self._results:
            self._pump.request_stop()
            return SyncResult(pos=None, rooms=[])
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, SyncResult)
        return item


class _RecordingHandler:
    def __init__(self) -> None:
        self.seen: list[SyncResult] = []

    async def handle_sync(self, result: SyncResult) -> None:
        self.seen.append(result)


class _FailingHandler:
    async def handle_sync(self, result: SyncResult) -> None:
        raise RuntimeError("boom")


def _slice(pos: str = "s1") -> SyncResult:
    return SyncResult(pos=pos, rooms=[RoomSync(room_id="!r:x", timeline=[])])


def _pump(results: list[object]) -> tuple[SyncPump, _ScriptedClient]:
    pump = SyncPump(client=None, error_backoff_sec=0)  # type: ignore[arg-type]
    client = _ScriptedClient(pump, results)
    pump.client = client  # type: ignore[assignment]
    return pump, client


async def test_every_handler_sees_every_slice() -> None:
    pump, _ = _pump([_slice()])
    first, second = _RecordingHandler(), _RecordingHandler()
    pump.register(first)
    pump.register(second)

    await pump.run()

    assert len(first.seen) == 1
    assert len(second.seen) == 1


async def test_the_stream_position_is_carried_into_the_next_request() -> None:
    pump, client = _pump([_slice("s1"), _slice("s2")])

    await pump.run()

    # First call starts the stream; each subsequent one resumes from the previous slice's pos.
    assert client.positions == [None, "s1", "s2"]


async def test_a_failing_handler_does_not_stop_the_others_or_the_pump() -> None:
    # A broken command router must not stop new users being welcomed.
    pump, client = _pump([_slice(), _slice()])
    survivor = _RecordingHandler()
    pump.register(_FailingHandler())
    pump.register(survivor)

    await pump.run()

    assert len(survivor.seen) == 2
    assert client.calls == 3  # two slices, then the empty one that stops the loop


async def test_unsupported_sliding_sync_ends_the_loop_quietly() -> None:
    pump, client = _pump([SyncNotSupportedError("nope")])
    handler = _RecordingHandler()
    pump.register(handler)

    await pump.run()

    assert client.calls == 1
    assert handler.seen == []


async def test_transient_errors_back_off_and_the_pump_continues() -> None:
    pump, client = _pump([RuntimeError("transient"), _slice()])
    handler = _RecordingHandler()
    pump.register(handler)

    await pump.run()

    # error → backoff → slice (dispatched) → empty (stop).
    assert client.calls == 3
    assert len(handler.seen) == 1


async def test_stop_requested_before_running_dispatches_nothing() -> None:
    pump, client = _pump([_slice()])
    handler = _RecordingHandler()
    pump.register(handler)
    pump.request_stop()

    await pump.run()

    assert client.calls == 0
    assert handler.seen == []


# --- recovery from a forgotten connection (RUN-03, ONB-14) -----------------------------------


def _rejected(errcode: str, error: str = "Unknown position") -> ApiError:
    return ApiError("POST", "https://hs/sync", 400, {"errcode": errcode, "error": error})


@pytest.mark.parametrize(
    ("errcode", "error"),
    [
        ("M_UNKNOWN_POS", "Unknown position"),  # expired or purged connection (MSC4186)
        ("M_UNKNOWN", "Invalid stream token"),  # what Synapse 1.156 says for a malformed pos
    ],
)
async def test_a_rejected_position_starts_a_fresh_connection_at_once(errcode: str, error: str) -> None:
    # Before the fix the pump resent the dead pos forever, five seconds apart. The huge backoff
    # proves the reset is immediate: a backoff would blow the timeout below.
    pump, client = _pump([_slice("p1"), _rejected(errcode, error), _slice("p2")])
    pump._error_backoff_sec = 3600
    handler = _RecordingHandler()
    pump.register(handler)

    await asyncio.wait_for(pump.run(), timeout=2)

    assert client.positions == [None, "p1", None, "p2"]
    assert [r.pos for r in handler.seen] == ["p1", "p2"]


async def test_a_400_without_a_position_backs_off_instead_of_looping() -> None:
    pump, client = _pump([_rejected("M_UNKNOWN_POS"), _slice("p1")])
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    pump._sleep = record  # type: ignore[method-assign]
    await pump.run()

    assert client.positions == [None, None, "p1"]
    assert sleeps == [0]  # one regular backoff (error_backoff_sec=0 in _pump)


async def test_backoff_grows_exponentially_and_is_capped() -> None:
    pump, _ = _pump([RuntimeError("down")] * 6 + [_slice()])
    pump._error_backoff_sec = 5
    pump._max_error_backoff_sec = 60
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    pump._sleep = record  # type: ignore[method-assign]
    await pump.run()

    assert sleeps == [5, 10, 20, 40, 60, 60]


async def test_a_repeated_failure_logs_one_traceback(caplog: pytest.LogCaptureFixture) -> None:
    pump, _ = _pump([RuntimeError("down"), RuntimeError("down"), RuntimeError("down"), _slice()])
    with caplog.at_level(logging.WARNING, logger="onbot.sync"):
        await pump.run()

    assert len([r for r in caplog.records if r.exc_info]) == 1


async def test_restart_stream_discards_the_position_of_the_poll_in_flight() -> None:
    pump, client = _pump([_slice("p1"), _slice("p2"), _slice("p3")])

    class _Restarter:
        async def handle_sync(self, result: SyncResult) -> None:
            if result.pos == "p1":
                pump.restart_stream()

    pump.register(_Restarter())
    await pump.run()

    assert client.positions == [None, None, "p2", "p3"]


# --- prompt stop (RUN-09) ----------------------------------------------------------------------


class _HangingClient:
    """A long-poll that never answers, like Synapse holding a sync open for 30 s."""

    def __init__(self) -> None:
        self.cancelled = False

    async def sliding_sync(self, pos: str | None) -> SyncResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


async def test_stop_abandons_a_long_poll_in_flight() -> None:
    # Before the fix the pump finished its 30 s long-poll before looking at the stop flag.
    client = _HangingClient()
    pump = SyncPump(client=client)  # type: ignore[arg-type]
    task = asyncio.create_task(pump.run())
    await asyncio.sleep(0.05)

    pump.request_stop()
    await asyncio.wait_for(task, timeout=1)

    assert client.cancelled


# --- heartbeat (RUN-04) ------------------------------------------------------------------------


def _heartbeat(tmp_path: Path) -> Heartbeat:
    return Heartbeat(tmp_path / "hb.json", max_age_sec={"reconcile": 100, "sync": 100})


async def test_each_sync_response_and_failure_is_reported_to_the_heartbeat(tmp_path: Path) -> None:
    heartbeat = _heartbeat(tmp_path)
    pump, _ = _pump([RuntimeError("down"), _slice()])
    pump._heartbeat = heartbeat

    await pump.run()

    sync = heartbeat.state["components"]["sync"]
    assert sync["last_error_at"] is not None
    assert sync["last_ok_at"] is not None
    assert sync["consecutive_failures"] == 0  # reset by the success that followed


async def test_unsupported_sliding_sync_takes_the_pump_out_of_the_healthcheck(tmp_path: Path) -> None:
    heartbeat = _heartbeat(tmp_path)
    pump, _ = _pump([SyncNotSupportedError("nope")])
    pump._heartbeat = heartbeat

    await pump.run()

    assert "sync_disabled" in heartbeat.state["components"]
