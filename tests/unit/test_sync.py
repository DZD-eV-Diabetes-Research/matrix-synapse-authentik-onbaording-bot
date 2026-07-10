"""The shared sync pump: stream position, backoff, stop, and fan-out to handlers."""

from __future__ import annotations

from onbot.clients.matrix import RoomSync, SyncNotSupportedError, SyncResult
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
