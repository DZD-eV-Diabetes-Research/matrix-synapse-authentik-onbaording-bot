"""Route commands sent in the control room (ADR-0010) — a :class:`~onbot.sync.SyncPump` handler.

This is the one place the bot *reacts to what someone said*. Everything guarding that is here.

**Replay protection is not optional.** The sync pump starts at ``pos=None``, and sliding sync then
replays up to 50 timeline events per room. Onboarding shrugs this off because welcoming is
idempotent. A command router that shrugged it off would re-send the last ``!announce`` to every user
on the server on every single restart of the bot. Two independent guards, deliberately both:

* **Age.** Events older than this process are ignored outright. Cheap, and it alone would nearly do.
* **Identity.** Event ids the bot has already acted on are remembered in a bounded ring buffer,
  persisted in the bot's account data, so a restart does not forget them. This alone would nearly do
  too. Together they survive a clock skew that defeats the first or a truncated buffer that defeats
  the second.

An event is marked seen and the cursor persisted **before** the command runs, not after. If the bot
dies mid-``!announce`` the announcement is lost rather than half-sent-and-then-repeated: for a
command that pages the entire company, at-most-once is the failure mode you want.

**Authorisation is the allowlist, never the power level.** The room's power levels let any member
speak — that is the point of a discussion room. If someone gets themselves into it, their power
level must not be the only thing standing between them and a message to every employee.

The allowlist is resolved per command through :class:`~onbot.admin.admins.AdminResolver` and is
never cached on this handler. Half of it comes from an Authentik group, and somebody removed from
that group must lose their commands within a TTL, not on the next restart of the bot.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from onbot import __version__
from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastService
from onbot.admin.commands import ANNOUNCE, STATUS, Command, help_text, parse_command
from onbot.clients.matrix import ApiClientMatrix, SyncResult
from onbot.config import OnbotConfig
from onbot.logging import get_logger
from onbot.reconciler.engine import ReconcilerEngine
from onbot.reconciler.state import event_type_name

log = get_logger(__name__)

MESSAGE_TYPE = "m.room.message"
NOTICE_MSGTYPE = "m.notice"
CURSOR_STATE_NAME = "admin_cursor"

# How many handled event ids to remember. Sliding sync replays at most 50 events per room, so this
# is several restarts' worth of headroom while staying small enough for one account-data blob.
MAX_REMEMBERED_EVENTS = 200


class ControlRoomHandler:
    """Consume ``m.room.message`` events in the control room and act on the commands among them."""

    def __init__(
        self,
        client: ApiClientMatrix,
        config: OnbotConfig,
        broadcast: BroadcastService,
        admins: AdminResolver,
        *,
        engine: ReconcilerEngine | None = None,
        started_at_ms: int | None = None,
        remembered_events: int = MAX_REMEMBERED_EVENTS,
    ) -> None:
        self.client = client
        self.config = config
        self.broadcast = broadcast
        self.admins = admins
        self.engine = engine
        self.bot_id = config.synapse_server.bot_user_id
        self.room_id: str | None = None
        self._started_at_ms = started_at_ms if started_at_ms is not None else int(time.time() * 1000)
        self._seen: deque[str] = deque(maxlen=remembered_events)
        self._cursor_type = event_type_name(config.synapse_server.server_name, CURSOR_STATE_NAME)

    async def start(self, room_id: str) -> None:
        """Bind to the provisioned control room and restore the handled-event cursor."""
        self.room_id = room_id
        data = await self.client.get_account_data(self.bot_id, self._cursor_type)
        self._seen.extend(str(e) for e in data.get("event_ids", []))
        log.info("admin control room active in %s (%d admins)", room_id, len(await self.admins.admins()))

    async def handle_sync(self, result: SyncResult) -> None:
        if self.room_id is None:
            return
        for room in result.rooms:
            if room.room_id != self.room_id:
                continue
            for event in room.timeline:
                if event.get("type") == MESSAGE_TYPE:
                    await self._handle_message(event)

    async def _handle_message(self, event: dict[str, Any]) -> None:
        sender = event.get("sender")
        content = event.get("content") or {}
        # The bot's own replies are messages in this room too; re-parsing them would let a reply to
        # !announce become the next command. Ignoring m.notice also keeps two bots from looping.
        if sender == self.bot_id or content.get("msgtype") == NOTICE_MSGTYPE:
            return
        if self._is_replay(event):
            return

        command = parse_command(str(content.get("body") or ""))
        if command is None:
            return  # ordinary conversation; the room is for people too

        event_id = str(event.get("event_id") or "")
        if event_id:
            await self._remember(event_id)

        # Resolved now, not at construction: an admin removed from the Authentik group loses their
        # commands here, one TTL later at worst. They keep their seat in the room and can still read
        # it — kicking them is a separate decision, deliberately not taken here.
        if sender not in await self.admins.admins():
            log.warning("refused %r from non-admin %s in the control room", command.name, sender)
            await self._reply(f"{sender} is not on the bot's admin allowlist.")
            return

        await self._dispatch(command, sender)

    def _is_replay(self, event: dict[str, Any]) -> bool:
        """True for an event this process must not act on: too old, or already handled."""
        timestamp = event.get("origin_server_ts")
        if isinstance(timestamp, int | float) and timestamp < self._started_at_ms:
            return True
        event_id = event.get("event_id")
        return bool(event_id) and event_id in self._seen

    async def _remember(self, event_id: str) -> None:
        """Record and persist before acting, so a crash loses the command rather than repeats it."""
        self._seen.append(event_id)
        try:
            await self.client.set_account_data(
                self.bot_id, self._cursor_type, {"event_ids": list(self._seen)}
            )
        except Exception:
            log.exception("could not persist the admin cursor; a restart may replay %s", event_id)

    async def _dispatch(self, command: Command, sender: str) -> None:
        log.info("admin %s ran %r", sender, command.name)
        if command.name == ANNOUNCE:
            await self._announce(command.argument)
        elif command.name == STATUS:
            await self._reply(await self._status())
        else:
            # !help, and anything unrecognised: answering is friendlier than silence, which reads
            # as the bot being down.
            await self._reply(help_text())

    async def _announce(self, message: str) -> None:
        if not message:
            await self._reply("Nothing to announce. Usage: !announce <message>")
            return
        result = await self.broadcast.broadcast(message)
        await self._reply(result.summary())

    async def _status(self) -> str:
        rooms = await self.broadcast.target_rooms()
        if self.engine is None or self.engine.last_reconcile_at is None:
            last = "not yet"
        else:
            last = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.engine.last_reconcile_at))
        return f"onbot {__version__} — last reconcile: {last} — managed rooms: {len(rooms)}"

    async def _reply(self, text: str) -> None:
        assert self.room_id is not None
        await self.client.send_text_message(self.room_id, text, msgtype=NOTICE_MSGTYPE)
