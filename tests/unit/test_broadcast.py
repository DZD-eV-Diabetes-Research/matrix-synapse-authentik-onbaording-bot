"""Broadcast fan-out over a fake Matrix client: targeting, msgtype, and fail-soft delivery."""

from __future__ import annotations

import asyncio

import pytest

from onbot.admin.broadcast import BroadcastFailure, BroadcastResult, BroadcastService
from onbot.config import AuthentikServer, OnbotConfig, SynapseServer

BOT = "@bot:matrix.test"


def _config(**overrides: object) -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id=BOT,
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        **overrides,  # type: ignore[arg-type]
    )


class _FakeClient:
    """Serves ``m.direct`` account data and records sends; ``fail_rooms`` raise instead."""

    def __init__(self, direct: dict[str, list[str]], *, fail_rooms: set[str] | None = None) -> None:
        self._direct = direct
        self._fail_rooms = fail_rooms or set()
        self.sends: list[tuple[str, str, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, list[str]]:
        assert user_id == BOT
        assert data_type == "m.direct"
        return dict(self._direct)

    async def send_text_message(self, room_id: str, body: str, *, msgtype: str = "m.text") -> str:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # let the other senders pile up, so the bound is observable
            if room_id in self._fail_rooms:
                raise RuntimeError("M_LIMIT_EXCEEDED")
            self.sends.append((room_id, body, msgtype))
            return f"$evt-{room_id}"
        finally:
            self.in_flight -= 1


def _service(client: object, config: OnbotConfig | None = None, **kwargs: object) -> BroadcastService:
    return BroadcastService(client, config or _config(), **kwargs)  # type: ignore[arg-type]


async def test_targets_every_direct_room_and_maps_it_to_its_user() -> None:
    client = _FakeClient({"@a:matrix.test": ["!ra:x"], "@b:matrix.test": ["!rb:x", "!rb2:x"]})

    rooms = await _service(client).target_rooms()

    assert rooms == {"!ra:x": "@a:matrix.test", "!rb:x": "@b:matrix.test", "!rb2:x": "@b:matrix.test"}


async def test_bot_and_ignored_users_are_never_broadcast_to() -> None:
    client = _FakeClient(
        {BOT: ["!self:x"], "@ignored:matrix.test": ["!ign:x"], "@real:matrix.test": ["!real:x"]}
    )
    config = _config(matrix_user_ignore_list=["@ignored:matrix.test"])

    result = await _service(client, config).broadcast("hello")

    assert result.sent == ["!real:x"]
    assert [room for room, _, _ in client.sends] == ["!real:x"]


async def test_announcements_go_out_as_notices() -> None:
    client = _FakeClient({"@a:matrix.test": ["!ra:x"]})

    await _service(client).broadcast("Maintenance at 22:00 UTC")

    assert client.sends == [("!ra:x", "Maintenance at 22:00 UTC", "m.notice")]


async def test_one_failing_room_does_not_silence_the_others() -> None:
    client = _FakeClient({"@a:matrix.test": ["!ok:x"], "@b:matrix.test": ["!bad:x"]}, fail_rooms={"!bad:x"})

    result = await _service(client).broadcast("hello")

    assert result.sent == ["!ok:x"]
    assert result.failed_count == 1
    failure = result.failures[0]
    assert failure.room_id == "!bad:x"
    assert failure.user_id == "@b:matrix.test"
    assert "M_LIMIT_EXCEEDED" in failure.error


async def test_send_concurrency_is_bounded() -> None:
    # An unbounded gather over these would open all six sends at once; the semaphore must not.
    client = _FakeClient({f"@u{i}:matrix.test": [f"!r{i}:x"] for i in range(6)})

    result = await _service(client, concurrency=2).broadcast("hello")

    assert result.sent_count == 6
    assert client.max_in_flight <= 2


async def test_no_direct_rooms_is_a_no_op_not_an_error() -> None:
    result = await _service(_FakeClient({})).broadcast("hello")

    assert result.sent_count == 0
    assert result.failed_count == 0


@pytest.mark.parametrize("concurrency", [0, -5])
async def test_concurrency_is_clamped_to_at_least_one(concurrency: int) -> None:
    # asyncio.Semaphore(0) would deadlock the fan-out forever; a bad config must not hang the bot.
    client = _FakeClient({"@a:matrix.test": ["!ra:x"]})

    result = await asyncio.wait_for(_service(client, concurrency=concurrency).broadcast("hello"), timeout=5)

    assert result.sent_count == 1


def test_summary_names_the_failures() -> None:
    result = BroadcastResult(sent=["!ok:x"], failures=[BroadcastFailure("!bad:x", "@b:matrix.test", "boom")])

    summary = result.summary()

    assert "sent to 1 rooms, 1 failed" in summary
    assert "@b:matrix.test (!bad:x): boom" in summary
