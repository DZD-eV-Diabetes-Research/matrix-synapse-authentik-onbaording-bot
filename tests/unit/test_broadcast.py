"""Broadcast fan-out over a fake Matrix client: targeting, msgtype, and fail-soft delivery."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from onbot.admin.broadcast import (
    BroadcastFailure,
    BroadcastResult,
    BroadcastService,
    RecipientsUnknown,
    describe_error,
)
from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix
from onbot.config import AdminRoom, AuthentikServer, OnbotConfig, SynapseServer

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
        admin_room=AdminRoom(enabled=False),
        **overrides,  # type: ignore[arg-type]
    )


class _FakeClient:
    """Serves ``m.direct`` account data and records sends; ``fail_rooms`` raise instead."""

    def __init__(
        self,
        direct: dict[str, list[str]],
        *,
        fail_rooms: set[str] | None = None,
        memberships: dict[str, str | None] | None = None,
    ) -> None:
        self._direct = direct
        self._fail_rooms = fail_rooms or set()
        # room -> the target user's membership there; "join" when not listed.
        self._memberships = memberships or {}
        self.sends: list[tuple[str, str, str]] = []
        self.kwargs: list[dict[str, object]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, list[str]]:
        assert user_id == BOT
        assert data_type == "m.direct"
        return dict(self._direct)

    async def get_membership(self, room_id: str, user_id: str) -> str | None:
        return self._memberships.get(room_id, "join")

    async def send_text_message(
        self, room_id: str, body: str, *, msgtype: str = "m.text", **kwargs: object
    ) -> str:
        self.kwargs.append(kwargs)
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


def _service(
    client: _FakeClient,
    config: OnbotConfig | None = None,
    *,
    mapped: set[str] | None = None,
    **kwargs: object,
) -> BroadcastService:
    """``mapped`` are the users the last pass mapped; by default everybody in ``m.direct``."""
    recipients = set(client._direct) if mapped is None else mapped

    async def source() -> set[str]:
        return recipients

    return BroadcastService(client, config or _config(), recipients=source, **kwargs)  # type: ignore[arg-type]


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
    # Users are mapped, but none of them has a notice board yet.
    result = await _service(_FakeClient({}), mapped={"@a:matrix.test"}).broadcast("hello")

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


# --- SEC-06: only mapped users; ONB-07: only users still in their notice board ---


async def test_users_the_last_pass_did_not_map_are_not_broadcast_to() -> None:
    """Live check 6: an outsider an older version welcomed stays in m.direct but gets nothing."""
    client = _FakeClient({"@a:matrix.test": ["!ra:x"], "@outsider:matrix.test": ["!ro:x"]})

    result = await _service(client, mapped={"@a:matrix.test"}).broadcast("hello")

    assert result.sent == ["!ra:x"]
    assert [room for room, _, _ in client.sends] == ["!ra:x"]


async def test_without_a_mapped_set_nothing_is_sent() -> None:
    client = _FakeClient({"@a:matrix.test": ["!ra:x"]})

    async def unknown() -> None:
        return None

    service = BroadcastService(client, _config(), recipients=unknown)  # type: ignore[arg-type]
    result = await service.broadcast("hello")

    assert client.sends == []
    assert result.refused is not None
    assert result.summary().startswith("not sent: no reconcile pass has mapped the users yet")
    with pytest.raises(RecipientsUnknown):
        await service.target_rooms()


async def test_a_room_the_user_left_is_skipped_and_counted_apart() -> None:
    client = _FakeClient(
        {"@a:matrix.test": ["!ra:x"], "@b:matrix.test": ["!rb:x"], "@c:matrix.test": ["!rc:x"]},
        memberships={"!rb:x": "leave", "!rc:x": "invite"},
    )

    result = await _service(client).broadcast("hello")

    # The invited user reads it once they accept; the one who left would never see it.
    assert sorted(result.sent) == ["!ra:x", "!rc:x"]
    assert result.not_member == [("!rb:x", "@b:matrix.test")]
    assert result.summary() == "sent to 2 rooms, 0 failed, 1 skipped (the user left their notice board)"


async def test_an_empty_mapped_set_refuses_the_broadcast() -> None:
    """W4R-05: an Authentik answer without users (outage, token scope, filter typo) maps nobody.

    That is not "everybody received it": it is refused like an unknown set, so the CLI exits 1 and
    !announce says why instead of reporting "sent to 0 rooms, 0 failed".
    """
    client = _FakeClient({"@a:matrix.test": ["!ra:x"]})

    result = await _service(client, mapped=set()).broadcast("hello")

    assert client.sends == []
    assert result.refused is not None
    assert "no users are mapped" in result.summary()


# --- ONB-10: announcements are rendered like the welcome messages ------------------------------


async def test_a_markdown_announcement_carries_the_rendered_html() -> None:
    client = _FakeClient({"@a:matrix.test": ["!ra:x"]})
    text = "**Maintenance** tonight, see [status](https://status.example.org)"

    await _service(client).broadcast(text)

    assert client.sends[0][1] == text  # the source stays in body, as Element sends it
    formatted = client.kwargs[0]["formatted_body"]
    assert isinstance(formatted, str)
    assert "<strong>Maintenance</strong>" in formatted
    assert '<a href="https://status.example.org">status</a>' in formatted


async def test_a_plain_announcement_stays_a_plain_message() -> None:
    client = _FakeClient({"@a:matrix.test": ["!ra:x"]})

    await _service(client).broadcast("Maintenance at 22:00 UTC")

    assert client.kwargs[0]["formatted_body"] is None


# --- ONB-04: the result stays postable however many rooms fail -----------------------------------


def _api_error(room: str, status: int = 429, errcode: str = "M_LIMIT_EXCEEDED") -> ApiError:
    url = f"https://matrix.test/_matrix/client/v3/rooms/{room}/send/m.room.message/{'0' * 32}"
    payload = {"errcode": errcode, "error": "Too Many Requests", "retry_after_ms": 5000}
    return ApiError("PUT", url, status, payload)


def test_the_summary_of_a_large_partial_failure_stays_far_below_the_event_size_limit() -> None:
    failures = [
        BroadcastFailure(
            f"!room{i}:matrix.test", f"@user{i}:matrix.test", describe_error(_api_error(f"!r{i}"))
        )
        for i in range(300)
    ]
    forbidden = describe_error(_api_error("!gone", 403, "M_FORBIDDEN"))
    failures.append(BroadcastFailure("!gone:matrix.test", "@gone:matrix.test", forbidden))
    result = BroadcastResult(sent=["!ok:x"] * 200, failures=failures)

    summary = result.summary()

    # Synapse refuses events above 65536 bytes; the reply must not come anywhere near that.
    assert len(summary.encode()) < 4000
    assert summary.startswith("sent to 200 rooms, 301 failed")
    assert "Failed: 300 rate limited (HTTP 429), 1 HTTP 403 M_FORBIDDEN: Too Many Requests" in summary
    assert "... and 281 more (every room is in the bot's log)" in summary
    # Neither the request URL nor the transaction id ends up in the reply.
    assert "/_matrix/client" not in summary


def test_errors_are_described_without_the_request_url() -> None:
    assert describe_error(_api_error("!r", 404, "M_NOT_FOUND")) == "HTTP 404 M_NOT_FOUND: Too Many Requests"
    assert describe_error(RuntimeError("boom")) == "RuntimeError: boom"
    assert describe_error(TimeoutError()) == "TimeoutError"


# --- ONB-03: paced by the server's rate-limit answers --------------------------------------------


class _TokenBucketHomeserver:
    """Synapse's per-user message limiter (``rc_message``) over a fake clock, behind a real client.

    A send costs one token; tokens refill at ``rate`` per second up to ``burst``. A send without a
    token gets 429 with ``retry_after_ms`` until the next one, as Synapse answers.
    """

    def __init__(
        self, direct: dict[str, list[str]], *, rate: float, burst: int, refuse_all: bool = False
    ) -> None:
        self.direct = direct
        self.rate = rate
        self.burst = burst
        self.refuse_all = refuse_all
        self.now = 0.0
        self.tokens = float(burst)
        self.updated = 0.0
        self.delivered: dict[str, list[str]] = {}  # room -> txn ids that created an event
        self.txns_seen: dict[str, set[str]] = {}  # room -> every txn id a send used

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def _refill(self) -> None:
        self.tokens = min(self.burst, self.tokens + (self.now - self.updated) * self.rate)
        self.updated = self.now

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/account_data/m.direct" in path:
            return httpx.Response(200, json=self.direct)
        if "/state/m.room.member/" in path:
            return httpx.Response(200, json={"membership": "join"})
        if "/send/m.room.message/" in path:
            room = path.split("/rooms/")[1].split("/")[0]
            txn = path.rsplit("/", 1)[1]
            self.txns_seen.setdefault(room, set()).add(txn)
            self._refill()
            if self.refuse_all or self.tokens < 1:
                wait_ms = 600_000 if self.refuse_all else int((1 - self.tokens) / self.rate * 1000) + 1
                body = {
                    "errcode": "M_LIMIT_EXCEEDED",
                    "error": "Too Many Requests",
                    "retry_after_ms": wait_ms,
                }
                return httpx.Response(429, json=body)
            self.tokens -= 1
            txns = self.delivered.setdefault(room, [])
            if txn not in txns:
                txns.append(txn)
            return httpx.Response(200, json={"event_id": f"$e-{room}-{txn[-6:]}"})
        return httpx.Response(404, json={"errcode": "M_UNRECOGNIZED"})


def _paced_service(
    server: _TokenBucketHomeserver, **kwargs: object
) -> tuple[BroadcastService, ApiClientMatrix]:
    client = ApiClientMatrix(
        server_url="https://matrix.test",
        access_token="tok",
        server_name="matrix.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(server.handle)),
        sleep=server.sleep,
    )

    async def mapped() -> set[str]:
        return set(server.direct)

    service = BroadcastService(
        client,
        _config(),
        recipients=mapped,
        clock=server.clock,
        sleep=server.sleep,
        **kwargs,  # type: ignore[arg-type]
    )
    return service, client


async def test_a_throttled_broadcast_reaches_every_room_at_the_servers_pace() -> None:
    """ONB-03: Synapse's default rc_message (0.2/s, burst 10) and 30 rooms.

    Before the fix each send waited out its own 429s in the shared HTTP retry (4 attempts) while the
    other workers drained the same bucket, and most rooms after the burst failed.
    """
    direct = {f"@u{i:02d}:matrix.test": [f"!r{i:02d}:matrix.test"] for i in range(30)}
    server = _TokenBucketHomeserver(direct, rate=0.2, burst=10)
    service, client = _paced_service(server)
    service.rate_limit_lifted = False
    try:
        result = await service.broadcast("Maintenance tonight")
    finally:
        await client.aclose()

    assert result.failures == []
    assert result.sent_count == 30
    assert sorted(server.delivered) == sorted(room for rooms in direct.values() for room in rooms)
    # Every attempt at a room used the same transaction id, so a repeat could not post it twice.
    assert all(len(txns) == 1 for txns in server.txns_seen.values())
    # 20 messages after the burst, one token every 5 s: about 100 s of the server's time.
    assert 95 <= server.now <= 130
    assert result.rate_limited >= 1
    summary = result.summary()
    assert "rate-limited the bot" in summary
    assert "send rate limit is not lifted" in summary


async def test_a_broadcast_that_never_gets_through_gives_up_after_the_stall_limit() -> None:
    direct = {f"@u{i}:matrix.test": [f"!r{i}:matrix.test"] for i in range(3)}
    server = _TokenBucketHomeserver(direct, rate=0.2, burst=1, refuse_all=True)
    service, client = _paced_service(server, stall_limit_sec=300)
    try:
        result = await service.broadcast("hello")
    finally:
        await client.aclose()

    assert result.sent_count == 0
    assert result.failed_count == 3
    assert all("gave up" in failure.error for failure in result.failures)
    # The limit plus at most one capped wait: the server's ten minutes are not waited out per room.
    assert 300 <= server.now <= 300 + 60 + 1
