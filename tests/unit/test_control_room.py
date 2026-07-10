"""The control-room command router: authorisation, replay protection, and dispatch."""

from __future__ import annotations

from typing import Any

from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastResult
from onbot.admin.control_room import ControlRoomHandler
from onbot.clients.matrix import RoomSync, SyncResult
from onbot.config import AdminRoom, AuthentikServer, OnbotConfig, SynapseServer

BOT = "@bot:matrix.test"
ADMIN = "@admin:matrix.test"
STRANGER = "@stranger:matrix.test"
ROOM = "!control:matrix.test"
GROUP = "group-pk-1"

NOW_MS = 1_000_000


def _config(admins: list[str] | None = None, *, group_pks: list[str] | None = None) -> OnbotConfig:
    return OnbotConfig(
        synapse_server=SynapseServer(
            server_name="matrix.test",
            server_url="https://matrix.test",
            bot_user_id=BOT,
            bot_access_token="tok",
        ),
        authentik_server=AuthentikServer(url="https://authentik.test", api_key="k"),
        admin_room=AdminRoom(
            enabled=True,
            admin_user_ids=admins if admins is not None else [ADMIN],
            authentik_group_pks_granting_bot_admin=group_pks or [],
        ),
    )


class _FakeAuthentik:
    """Authentik's answer for the admin group, mutable between refreshes."""

    def __init__(self, *usernames: str) -> None:
        self.usernames = list(usernames)
        self.fail = False

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("authentik is down")
        return [{"pk": name, "username": name} for name in self.usernames]


class _FakeClient:
    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []
        self.account_data: dict[str, dict[str, Any]] = {}

    async def send_text_message(self, room_id: str, body: str, *, msgtype: str = "m.text") -> str:
        self.sends.append((room_id, body))
        return f"$evt{len(self.sends)}"

    async def get_account_data(self, user_id: str, data_type: str) -> dict[str, Any]:
        return dict(self.account_data.get(data_type, {}))

    async def set_account_data(self, user_id: str, data_type: str, content: dict[str, Any]) -> None:
        self.account_data[data_type] = dict(content)


class _FakeBroadcast:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rooms = {"!a:x": "@a:x", "!b:x": "@b:x"}

    async def broadcast(self, message: str) -> BroadcastResult:
        self.calls.append(message)
        return BroadcastResult(sent=list(self.rooms))

    async def target_rooms(self) -> dict[str, str]:
        return dict(self.rooms)


def _handler(
    client: _FakeClient,
    broadcast: _FakeBroadcast,
    *,
    admins: list[str] | None = None,
    engine: object | None = None,
    remembered_events: int = 200,
    resolver: AdminResolver | None = None,
) -> ControlRoomHandler:
    config = _config(admins)
    handler = ControlRoomHandler(
        client,  # type: ignore[arg-type]
        config,
        broadcast,  # type: ignore[arg-type]
        resolver or AdminResolver(_FakeAuthentik(), config),  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
        started_at_ms=NOW_MS,
        remembered_events=remembered_events,
    )
    handler.room_id = ROOM
    return handler


def _message(
    body: str,
    *,
    sender: str = ADMIN,
    event_id: str = "$cmd1",
    ts: int = NOW_MS + 1,
    msgtype: str = "m.text",
    room_id: str = ROOM,
) -> SyncResult:
    return SyncResult(
        pos="s1",
        rooms=[
            RoomSync(
                room_id=room_id,
                timeline=[
                    {
                        "type": "m.room.message",
                        "event_id": event_id,
                        "sender": sender,
                        "origin_server_ts": ts,
                        "content": {"msgtype": msgtype, "body": body},
                    }
                ],
            )
        ],
    )


async def _run(handler: ControlRoomHandler, *results: SyncResult) -> None:
    for result in results:
        await handler.handle_sync(result)


# --- authorisation ---------------------------------------------------------


async def test_an_allowlisted_admin_can_announce() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce Maintenance at 22:00"))

    assert broadcast.calls == ["Maintenance at 22:00"]
    assert "sent to 2 rooms, 0 failed" in client.sends[0][1]


async def test_a_member_who_is_not_on_the_allowlist_is_refused() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce fire everyone", sender=STRANGER))

    assert broadcast.calls == []
    assert "not on the bot's admin allowlist" in client.sends[0][1]


async def test_an_empty_allowlist_refuses_everyone() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast, admins=[]), _message("!announce hello"))

    assert broadcast.calls == []


def _dynamic_handler(
    client: _FakeClient, broadcast: _FakeBroadcast, authentik: _FakeAuthentik, clock: list[float]
) -> ControlRoomHandler:
    """A handler whose admins come from an Authentik group, over a clock the test winds forward."""
    config = _config(admins=[], group_pks=[GROUP])
    resolver = AdminResolver(
        authentik,  # type: ignore[arg-type]
        config,
        ttl_sec=60,
        clock=lambda: clock[0],
    )
    handler = ControlRoomHandler(
        client,  # type: ignore[arg-type]
        config,
        broadcast,  # type: ignore[arg-type]
        resolver,
        started_at_ms=NOW_MS,
    )
    handler.room_id = ROOM
    return handler


async def test_a_group_member_gains_and_then_loses_command_access_without_a_restart() -> None:
    # The whole point of sourcing admins from Authentik: revocation that does not need a deploy. The
    # handler is never reconstructed here — the same object must refuse what it just allowed.
    client, broadcast, clock = _FakeClient(), _FakeBroadcast(), [1000.0]
    authentik = _FakeAuthentik("alice")
    handler = _dynamic_handler(client, broadcast, authentik, clock)
    alice = "@alice:matrix.test"

    await _run(handler, _message("!announce while a member", sender=alice, event_id="$a"))
    assert broadcast.calls == ["while a member"]

    authentik.usernames.clear()  # alice is removed from the Authentik admin group
    clock[0] += 60  # ...and one TTL passes

    await _run(handler, _message("!announce after removal", sender=alice, event_id="$b"))

    assert broadcast.calls == ["while a member"]
    assert "not on the bot's admin allowlist" in client.sends[-1][1]


async def test_authentik_going_down_neither_opens_nor_closes_the_gate() -> None:
    client, broadcast, clock = _FakeClient(), _FakeBroadcast(), [1000.0]
    authentik = _FakeAuthentik("alice")
    handler = _dynamic_handler(client, broadcast, authentik, clock)
    alice = "@alice:matrix.test"

    await _run(handler, _message("!announce first", sender=alice, event_id="$a"))
    authentik.fail = True
    clock[0] += 600

    await _run(handler, _message("!announce second", sender=alice, event_id="$b"))
    await _run(handler, _message("!announce nope", sender=STRANGER, event_id="$c"))

    assert broadcast.calls == ["first", "second"]  # alice keeps hers; the stranger gains nothing


# --- replay protection -----------------------------------------------------


async def test_the_same_slice_delivered_twice_announces_once() -> None:
    # The pump restarts at pos=None and the server replays the timeline; this must not re-page.
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast)
    slice_ = _message("!announce Maintenance at 22:00")

    await _run(handler, slice_, slice_)

    assert broadcast.calls == ["Maintenance at 22:00"]


async def test_events_predating_this_process_are_ignored() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce old news", ts=NOW_MS - 1))

    assert broadcast.calls == []


async def test_the_cursor_survives_a_restart() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    await _run(_handler(client, broadcast), _message("!announce once"))
    assert broadcast.calls == ["once"]

    # A new handler, same account data: the replayed event must not run a second time. Its
    # timestamp is in this second process's future, so only the event-id guard can save us here.
    restarted = _handler(client, broadcast)
    await restarted.start(ROOM)
    await _run(restarted, _message("!announce once"))

    assert broadcast.calls == ["once"]


async def test_the_remembered_cursor_is_bounded() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast, remembered_events=2)

    for i in range(4):
        await _run(handler, _message("!status", event_id=f"$cmd{i}"))

    assert client.account_data[handler._cursor_type]["event_ids"] == ["$cmd2", "$cmd3"]


# --- what is and is not a command ------------------------------------------


async def test_bare_chat_in_the_room_is_inert() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("should we announce the outage?"))

    assert broadcast.calls == []
    assert client.sends == []


async def test_the_bots_own_messages_are_ignored() -> None:
    # Otherwise the reply to !announce is re-parsed as the next command.
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce loop", sender=BOT))

    assert broadcast.calls == []


async def test_notices_are_ignored_so_two_bots_cannot_loop() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce loop", msgtype="m.notice"))

    assert broadcast.calls == []


async def test_messages_in_other_rooms_are_not_commands() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce hi", room_id="!elsewhere:x"))

    assert broadcast.calls == []


async def test_announce_without_a_message_explains_itself_instead_of_sending_nothing() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce"))

    assert broadcast.calls == []
    assert "Usage: !announce" in client.sends[0][1]


async def test_help_and_unknown_commands_both_answer_with_the_help_text() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast)

    await _run(
        handler,
        _message("!help", event_id="$a"),
        _message("!frobnicate", event_id="$b"),
    )

    assert len(client.sends) == 2
    assert all("!announce <message>" in body for _, body in client.sends)


async def test_status_reports_version_reconcile_time_and_room_count() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    class _Engine:
        last_reconcile_at = 1_700_000_000.0

    await _run(_handler(client, broadcast, engine=_Engine()), _message("!status"))

    body = client.sends[0][1]
    assert "onbot " in body
    assert "2023-11-14" in body  # the reconcile timestamp, rendered in UTC
    assert "managed rooms: 2" in body


async def test_status_before_the_first_reconcile_says_so() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!status"))

    assert "last reconcile: not yet" in client.sends[0][1]
