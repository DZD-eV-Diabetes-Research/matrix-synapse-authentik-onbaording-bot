"""The control-room command router: authorisation, replay protection, and dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from onbot.admin import control_room
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
        return [{"pk": name, "username": name, "groups": [GROUP]} for name in self.usernames]


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
        self.rate_limit_lifted: bool | None = True
        # Set to hold a broadcast in flight until the test releases it.
        self.release: asyncio.Event | None = None
        self.result: BroadcastResult | None = None

    async def broadcast(
        self, message: str, *, on_start: Callable[[int], Awaitable[None]] | None = None
    ) -> BroadcastResult:
        self.calls.append(message)
        if on_start is not None:
            await on_start(len(self.rooms))
        if self.release is not None:
            await self.release.wait()
        return self.result or BroadcastResult(sent=list(self.rooms))

    async def target_rooms(self) -> dict[str, str]:
        return dict(self.rooms)


def _handler(
    client: _FakeClient,
    broadcast: _FakeBroadcast,
    *,
    admins: list[str] | None = None,
    group_pks: list[str] | None = None,
    engine: object | None = None,
    remembered_events: int = 200,
    resolver: AdminResolver | None = None,
    clock: Callable[[], float] | None = None,
) -> ControlRoomHandler:
    config = _config(admins, group_pks=group_pks)
    handler = ControlRoomHandler(
        client,  # type: ignore[arg-type]
        config,
        broadcast,  # type: ignore[arg-type]
        resolver or AdminResolver(_FakeAuthentik(), config),  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
        started_at_ms=NOW_MS,
        remembered_events=remembered_events,
        clock=clock or (lambda: 0.0),
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
    extra_content: dict[str, Any] | None = None,
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
                        "content": {"msgtype": msgtype, "body": body, **(extra_content or {})},
                    }
                ],
            )
        ],
    )


async def _run(handler: ControlRoomHandler, *results: SyncResult) -> None:
    """Feed the slices and wait for a broadcast they started, which runs beside the stream."""
    for result in results:
        await handler.handle_sync(result)
        await handler.wait_for_broadcast()


# --- authorisation ---------------------------------------------------------


async def test_an_allowlisted_admin_can_announce() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce Maintenance at 22:00"))

    assert broadcast.calls == ["Maintenance at 22:00"]
    assert client.sends[0][1] == "Sending the announcement to 2 rooms; the result follows here."
    assert "sent to 2 rooms, 0 failed" in client.sends[1][1]


async def test_a_member_who_is_not_on_the_allowlist_is_refused() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!announce fire everyone", sender=STRANGER))

    assert broadcast.calls == []
    assert "not on the bot's admin allowlist" in client.sends[0][1]


async def test_an_empty_allowlist_refuses_everyone() -> None:
    # The config refuses an enabled room without any admin source (D19); an admin group without
    # members is how the union still ends up empty at runtime.
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast, admins=[], group_pks=[GROUP]), _message("!announce hello"))

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


async def test_status_without_mapped_users_says_the_room_count_is_unknown() -> None:
    from onbot.admin.broadcast import RecipientsUnknown

    class _Unmapped(_FakeBroadcast):
        async def target_rooms(self) -> dict[str, str]:
            raise RecipientsUnknown("not yet")

    client = _FakeClient()
    await _run(_handler(client, _Unmapped()), _message("!status"))

    assert "managed rooms: unknown until a pass has mapped the users" in client.sends[0][1]


async def test_status_before_the_first_reconcile_says_so() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _message("!status"))

    assert "last reconcile: not yet" in client.sends[0][1]


# --- !lobby reopen (D16) --------------------------------------------------------------------------


class _LobbyEngine:
    def __init__(self) -> None:
        self.reopened: list[str] = []

    async def reopen_lobby(self, target: str) -> str:
        self.reopened.append(target)
        return f"Reopened {target}"


async def test_lobby_reopen_asks_the_engine_and_answers_with_its_reply() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    engine = _LobbyEngine()

    await _run(_handler(client, broadcast, engine=engine), _message("!lobby reopen group-7"))

    assert engine.reopened == ["group-7"]
    assert client.sends == [(ROOM, "Reopened group-7")]


async def test_lobby_without_reopen_and_a_target_explains_the_usage() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    engine = _LobbyEngine()

    await _run(
        _handler(client, broadcast, engine=engine),
        _message("!lobby", event_id="$a"),
        _message("!lobby reopen", event_id="$b"),
        _message("!lobby close g1", event_id="$c"),
    )

    assert engine.reopened == []
    assert len(client.sends) == 3 and all("Usage: !lobby reopen" in body for _, body in client.sends)


async def test_lobby_reopen_is_refused_for_somebody_off_the_allowlist() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    engine = _LobbyEngine()

    await _run(_handler(client, broadcast, engine=engine), _message("!lobby reopen g1", sender=STRANGER))

    assert engine.reopened == []


# --- ONB-11, SEC-12: edits are never commands -----------------------------------------------------


def _edit(
    new_body: str, *, fallback: str | None = None, sender: str = ADMIN, event_id: str = "$edit"
) -> SyncResult:
    """An edit of ``$cmd1``: ``fallback`` is the plain ``body`` (Element prefixes it with ``* ``)."""
    return _message(
        fallback if fallback is not None else new_body,
        sender=sender,
        event_id=event_id,
        extra_content={
            "m.new_content": {"msgtype": "m.text", "body": new_body},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$cmd1"},
        },
    )


async def test_an_edit_without_the_fallback_prefix_does_not_announce_again() -> None:
    """ONB-11: a client that sends the edit's body without ``* `` must not page everybody twice."""
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast)

    await _run(handler, _message("!announce Maintenance at 22:00"), _edit("!announce Maintenance at 23:00"))

    assert broadcast.calls == ["Maintenance at 22:00"]
    assert "Edits are not commands" in client.sends[-1][1]


async def test_an_element_edit_of_a_command_is_answered_not_ignored_silently() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(
        _handler(client, broadcast),
        _edit("!announce Maintenance at 23:00", fallback="* !announce Maintenance at 23:00"),
    )

    assert broadcast.calls == []
    assert [body for _, body in client.sends] == [control_room.EDIT_IS_NO_COMMAND]


async def test_an_edit_is_answered_once_even_when_the_slice_is_replayed() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast)
    edit = _edit("!status")

    await _run(handler, edit, edit)

    assert len(client.sends) == 1


async def test_an_edit_by_somebody_off_the_allowlist_gets_no_answer() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _edit("!announce hi", sender=STRANGER))

    assert broadcast.calls == []
    assert client.sends == []
    assert client.account_data == {}


async def test_an_edit_that_is_no_command_is_plain_conversation() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()

    await _run(_handler(client, broadcast), _edit("we should announce it tomorrow"))

    assert client.sends == []


# --- SEC-12: only admins' commands enter the persisted replay buffer ------------------------------


async def test_commands_of_non_admins_are_not_recorded() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast)

    await _run(handler, _message("!announce hi", sender=STRANGER, event_id="$s"))

    assert handler._cursor_type not in client.account_data  # no write, no slot taken


async def test_a_member_who_is_not_an_admin_cannot_flush_the_replay_buffer() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast, remembered_events=2)

    await _run(handler, _message("!announce once", event_id="$admin"))
    await _run(handler, *(_message("!status", sender=STRANGER, event_id=f"$s{i}") for i in range(5)))

    assert client.account_data[handler._cursor_type]["event_ids"] == ["$admin"]
    # A restart replaying the admin's command still finds it in the buffer.
    restarted = _handler(client, broadcast, remembered_events=2)
    await restarted.start(ROOM)
    await _run(restarted, _message("!announce once", event_id="$admin"))
    assert broadcast.calls == ["once"]


async def test_a_replayed_slice_does_not_refuse_a_non_admin_twice() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast)
    slice_ = _message("!announce hi", sender=STRANGER)

    await _run(handler, slice_, slice_)

    assert len(client.sends) == 1


# --- ONB-03: the broadcast runs beside the sync stream -------------------------------------------


async def test_a_running_broadcast_does_not_block_the_room_and_a_second_one_is_refused() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    broadcast.release = asyncio.Event()
    handler = _handler(client, broadcast)

    await handler.handle_sync(_message("!announce first", event_id="$a"))
    await asyncio.sleep(0)  # the fan-out starts and acknowledges
    assert handler.broadcast_running
    await handler.handle_sync(_message("!announce second", event_id="$b"))
    await handler.handle_sync(_message("!status", event_id="$c"))

    assert broadcast.calls == ["first"]
    replies = [body for _, body in client.sends]
    assert replies[0] == "Sending the announcement to 2 rooms; the result follows here."
    assert "still being sent to 2 rooms" in replies[1] and "this one was not sent" in replies[1]
    assert "an announcement is being sent to 2 rooms" in replies[2]

    broadcast.release.set()
    await handler.wait_for_broadcast()
    assert "sent to 2 rooms, 0 failed" in client.sends[-1][1]
    assert not handler.broadcast_running


async def test_stop_cancels_a_broadcast_in_flight() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    broadcast.release = asyncio.Event()
    handler = _handler(client, broadcast)
    await handler.handle_sync(_message("!announce first"))
    await asyncio.sleep(0)

    await asyncio.wait_for(handler.stop(), timeout=2)

    assert not handler.broadcast_running
    assert not any("sent to" in body for _, body in client.sends)


class _SizeLimitedClient(_FakeClient):
    """Refuses a reply above a size limit, as Synapse refuses an event above 65536 bytes."""

    async def send_text_message(self, room_id: str, body: str, *, msgtype: str = "m.text") -> str:
        if len(body) > 200:
            raise RuntimeError("M_TOO_LARGE")
        return await super().send_text_message(room_id, body, msgtype=msgtype)


async def test_a_result_that_cannot_be_posted_is_replaced_by_a_short_note() -> None:
    """ONB-04: silence after !announce reads as "did not work" and gets the command typed again."""
    from onbot.admin.broadcast import BroadcastFailure

    client, broadcast = _SizeLimitedClient(), _FakeBroadcast()
    broadcast.result = BroadcastResult(
        failures=[BroadcastFailure(f"!r{i}:x", f"@u{i}:x", "HTTP 500") for i in range(10)]
    )

    await _run(_handler(client, broadcast), _message("!announce hello"))

    assert "could not be posted here (RuntimeError: M_TOO_LARGE)" in client.sends[-1][1]


async def test_status_says_whether_the_send_rate_limit_is_lifted() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast)

    broadcast.rate_limit_lifted = False
    await _run(handler, _message("!status", event_id="$a"))
    broadcast.rate_limit_lifted = True
    await _run(handler, _message("!status", event_id="$b"))

    assert "send rate limit: NOT lifted" in client.sends[0][1]
    assert "send rate limit: lifted for the bot" in client.sends[1][1]


# --- ONB-11 (S18): the same announcement twice within a minute asks first --------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


OTHER_ADMIN = "@other:matrix.test"


def _held_id(reply: str) -> str:
    assert "was not sent" in reply, reply
    return reply.split("!confirm ", 1)[1].split()[0]


async def test_the_same_announcement_within_a_minute_is_held_until_confirmed() -> None:
    client, broadcast, clock = _FakeClient(), _FakeBroadcast(), _Clock()
    handler = _handler(client, broadcast, clock=clock)

    await _run(handler, _message("!announce Maintenance  at 22:00", event_id="$a"))
    clock.now += 30
    await _run(handler, _message("!announce Maintenance at\n22:00 ", event_id="$b"))

    assert broadcast.calls == ["Maintenance  at 22:00"]
    held = _held_id(client.sends[-1][1])
    assert "30 seconds ago" in client.sends[-1][1]

    await _run(handler, _message(f"!confirm {held}", event_id="$c"))
    assert broadcast.calls == ["Maintenance  at 22:00", "Maintenance at\n22:00"]
    # Confirmed once: the id is used up.
    await _run(handler, _message(f"!confirm {held}", event_id="$d"))
    assert len(broadcast.calls) == 2


async def test_a_held_announcement_is_only_confirmed_by_its_sender_and_can_be_dropped() -> None:
    client, broadcast, clock = _FakeClient(), _FakeBroadcast(), _Clock()
    handler = _handler(client, broadcast, admins=[ADMIN, OTHER_ADMIN], clock=clock)
    await _run(
        handler, _message("!announce hello", event_id="$a"), _message("!announce hello", event_id="$b")
    )
    held = _held_id(client.sends[-1][1])

    await _run(handler, _message(f"!confirm {held}", sender=OTHER_ADMIN, event_id="$c"))
    assert "only they can confirm" in client.sends[-1][1]
    await _run(handler, _message(f"!reject {held}", event_id="$d"))
    assert "Dropped the repeated announcement" in client.sends[-1][1]
    await _run(handler, _message(f"!confirm {held}", event_id="$e"))
    assert broadcast.calls == ["hello"]


async def test_a_repeat_after_a_minute_another_text_or_another_admin_is_sent_at_once() -> None:
    client, broadcast, clock = _FakeClient(), _FakeBroadcast(), _Clock()
    handler = _handler(client, broadcast, admins=[ADMIN, OTHER_ADMIN], clock=clock)
    await _run(handler, _message("!announce hello", event_id="$a"))
    await _run(handler, _message("!announce hello", sender=OTHER_ADMIN, event_id="$b"))
    await _run(handler, _message("!announce hello again", event_id="$c"))
    clock.now += control_room.DUPLICATE_WINDOW_SEC
    await _run(handler, _message("!announce hello again", event_id="$d"))
    assert broadcast.calls == ["hello", "hello", "hello again", "hello again"]
    assert not any("was not sent" in body for _, body in client.sends)


async def test_a_held_announcement_expires() -> None:
    client, broadcast, clock = _FakeClient(), _FakeBroadcast(), _Clock()
    handler = _handler(client, broadcast, clock=clock)
    await _run(
        handler, _message("!announce hello", event_id="$a"), _message("!announce hello", event_id="$b")
    )
    held = _held_id(client.sends[-1][1])
    clock.now += control_room.DUPLICATE_CONFIRM_SEC
    await _run(handler, _message(f"!confirm {held}", event_id="$c"))
    assert broadcast.calls == ["hello"]


# --- W5R-04: a shutdown starts no broadcast and says what it cut off --------------------------------


async def test_an_announce_in_the_slice_finished_after_stop_is_not_sent() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    handler = _handler(client, broadcast)
    await handler.stop()  # the service is shutting down; the pump still finishes its slice

    await _run(handler, _message("!announce too late"))

    assert broadcast.calls == []
    assert client.sends[-1][1] == control_room.SHUTTING_DOWN
    await handler.stop()  # the second stop after the drain finds nothing left running
    assert not handler.broadcast_running


async def test_a_broadcast_cut_off_by_a_shutdown_says_how_far_it_got() -> None:
    client, broadcast = _FakeClient(), _FakeBroadcast()
    broadcast.release = asyncio.Event()
    handler = _handler(client, broadcast)
    await handler.handle_sync(_message("!announce first"))
    await asyncio.sleep(0)

    await asyncio.wait_for(handler.stop(), timeout=2)

    assert "interrupted by a shutdown of the bot after 0 of 2 rooms" in client.sends[-1][1]
