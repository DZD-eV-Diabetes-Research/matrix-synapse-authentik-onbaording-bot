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
command that pages the entire company, at-most-once is the failure mode you want. Only commands from
allowlisted admins are recorded (SEC-12): a member who is not an admin cannot flood the bounded
buffer and push the real commands out of it.

**Edits are never commands** (ONB-11, SEC-12). An edit (``m.relates_to.rel_type: m.replace``) is a new
event with a new id, so the replay guard does not stop it, and its ``body`` is only a fallback whose
``* `` prefix is a client convention. A client that leaves the prefix out would turn an admin's typo
fix into a second company-wide ``!announce``. Edits are ignored; an admin whose edit holds a command
is told once to send a new message instead.

**A broadcast runs beside the sync stream, not inside it** (ONB-03). Under the homeserver's rate
limit a large ``!announce`` takes as long as the server's pace allows, which can be many minutes.
The command is acknowledged with the number of rooms, the fan-out runs as a task of its own, and its
result is posted when it ends; meanwhile the other commands keep working. One broadcast runs at a
time: a second ``!announce`` while one is going is refused, not queued. The result reply is bounded
in size (ONB-04); if posting it fails anyway, a short note takes its place.

**The same announcement twice in a row asks first** (ONB-11). An ``!announce`` with the same text
(whitespace aside) from the same admin within :data:`DUPLICATE_WINDOW_SEC` of their previous one is
held: the bot answers with an id, and only ``!confirm <id>`` from that admin within
:data:`DUPLICATE_CONFIRM_SEC` sends it (``!reject <id>`` drops it). A message pasted twice, or sent
again because the first answer was slow, does not page everybody twice. Held announcements live in
this process only; a restart drops them, which is the at-most-once direction. Their ids share the
``!confirm`` namespace with the guard's batches: they are matched in any case like those (W6R-04),
a new id never repeats an open one of either kind, and ``!pending`` lists both.

**Authorisation is the allowlist, never the power level.** The room's power levels let any member
speak — that is the point of a discussion room. If someone gets themselves into it, their power
level must not be the only thing standing between them and a message to every employee.

The allowlist is resolved per command through :class:`~onbot.admin.admins.AdminResolver` and is
never cached on this handler. Half of it comes from an Authentik group, and somebody removed from
that group must lose their commands within a TTL, not on the next restart of the bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from onbot import __version__
from onbot.admin.admins import AdminResolver
from onbot.admin.broadcast import BroadcastService, RecipientsUnknown, describe_error
from onbot.admin.commands import (
    ANNOUNCE,
    CONFIRM,
    GUARD,
    GUARD_RESET,
    LOBBY,
    LOBBY_REOPEN,
    PENDING,
    REJECT,
    ROOM,
    ROOM_ADOPT,
    ROOM_ADOPT_LOBBY,
    STATUS,
    Command,
    help_text,
    parse_command,
)
from onbot.clients.matrix import ApiClientMatrix, SyncResult
from onbot.config import OnbotConfig
from onbot.guard import (
    RESET_HINT,
    DestructiveActionGuard,
    GuardStateUnreadable,
    describe_batch,
    new_batch_id,
    normalize_batch_id,
)
from onbot.logging import get_logger
from onbot.reconciler.engine import ReconcilerEngine
from onbot.reconciler.state import OnbotRoomType, event_type_name

log = get_logger(__name__)

MESSAGE_TYPE = "m.room.message"
NOTICE_MSGTYPE = "m.notice"
CURSOR_STATE_NAME = "admin_cursor"

# How many handled event ids to remember. Sliding sync replays at most 50 events per room, so this
# is several restarts' worth of headroom while staying small enough for one account-data blob.
MAX_REMEMBERED_EVENTS = 200

# https://spec.matrix.org/latest/client-server-api/#event-replacements
REPLACE_REL_TYPE = "m.replace"
EDIT_FALLBACK_PREFIX = "* "
EDIT_IS_NO_COMMAND = (
    "Edits are not commands, so the bot did not act on yours. Send the corrected command as a new "
    "message (an announcement that already went out stays as it is)."
)
BROADCAST_RUNNING = (
    "An announcement is still being sent to {rooms} rooms. Wait for its result here before sending "
    "another; this one was not sent."
)
REPLY_FAILED = "The result of the last command could not be posted here ({error}); it is in the bot's log."
COMMAND_FAILED = "The last command failed ({error}); details are in the bot's log."

# W5R-04: what an announcement cut off by a shutdown, or one sent during it, says.
INTERRUPTED = (
    "The announcement was interrupted by a shutdown of the bot after {done} of {total} rooms; the "
    "others did not get it. Send it again after the restart if it still matters."
)
INTERRUPTED_NOTE_TIMEOUT_SEC = 5.0
SHUTTING_DOWN = (
    "The bot is shutting down, so this announcement was not sent. Send it again after the restart."
)

# ONB-11: the same text from the same admin within this many seconds of their previous `!announce` is
# held until they confirm it, and a held one can be confirmed for this long.
DUPLICATE_WINDOW_SEC = 60.0
DUPLICATE_CONFIRM_SEC = 600.0
# How much of a held announcement `!pending` shows.
HELD_PREVIEW_CHARS = 80
DUPLICATE_HELD = (
    "You sent the same announcement {ago} seconds ago, so this one was not sent. To send it again "
    "anyway, answer !confirm {id} within {minutes} minutes; !reject {id} drops it."
)


def normalize_announcement(text: str) -> str:
    """``text`` with every run of whitespace as one space, for the duplicate check (ONB-11)."""
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class HeldAnnouncement:
    """An ``!announce`` held as a duplicate until its sender confirms it (ONB-11)."""

    sender: str
    message: str
    held_at: float


def is_edit(content: dict[str, Any]) -> bool:
    """Whether a message's content replaces an earlier message (an edit)."""
    relates_to = content.get("m.relates_to")
    return isinstance(relates_to, dict) and relates_to.get("rel_type") == REPLACE_REL_TYPE


def edited_body(content: dict[str, Any]) -> str:
    """The text an edit sets: ``m.new_content.body``, else the fallback ``body`` without ``* ``."""
    new_content = content.get("m.new_content")
    if isinstance(new_content, dict) and isinstance(new_content.get("body"), str):
        return str(new_content["body"])
    body = str(content.get("body") or "")
    return body.removeprefix(EDIT_FALLBACK_PREFIX)


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
        guard: DestructiveActionGuard | None = None,
        started_at_ms: int | None = None,
        remembered_events: int = MAX_REMEMBERED_EVENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.config = config
        self.broadcast = broadcast
        self.admins = admins
        self.engine = engine
        self.guard = guard
        self.bot_id = config.synapse_server.bot_user_id
        self.room_id: str | None = None
        self._started_at_ms = started_at_ms if started_at_ms is not None else int(time.time() * 1000)
        self._seen: deque[str] = deque(maxlen=remembered_events)
        # Events of non-admins already answered, in this process only (a replayed slice must not
        # answer them twice). Kept apart from ``_seen`` so they cannot evict a real command (SEC-12).
        self._answered: deque[str] = deque(maxlen=remembered_events)
        self._cursor_type = event_type_name(config.synapse_server.server_name, CURSOR_STATE_NAME)
        # The broadcast in flight, if any (one at a time), and how many rooms it goes to.
        self._broadcast_task: asyncio.Task[None] | None = None
        self._broadcast_rooms = 0
        # Set by stop(): no new broadcast starts while the service shuts down (W5R-04).
        self._stopping = False
        # ONB-11: each admin's last announcement (normalised text, when), and the held duplicates.
        self._clock = clock
        self._last_announcement: dict[str, tuple[str, float]] = {}
        self._held_announcements: dict[str, HeldAnnouncement] = {}
        if guard is not None:
            # `!confirm <id>` reaches both, so the guard's new batch ids avoid the held ones (W6R-04).
            guard.reserve_ids(self._held_announcements.keys)

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
                if event.get("type") != MESSAGE_TYPE:
                    continue
                try:
                    await self._handle_message(event)
                except Exception as exc:
                    # One command that fails must not drop the rest of the slice (W7R-04); the
                    # replay guard already recorded it, so it is not run again.
                    log.exception("control room: handling %s failed", event.get("event_id"))
                    with contextlib.suppress(Exception):
                        await self._reply(COMMAND_FAILED.format(error=describe_error(exc)))

    async def _handle_message(self, event: dict[str, Any]) -> None:
        sender = event.get("sender")
        content = event.get("content") or {}
        # The bot's own replies are messages in this room too; re-parsing them would let a reply to
        # !announce become the next command. Ignoring m.notice also keeps two bots from looping.
        if sender == self.bot_id or content.get("msgtype") == NOTICE_MSGTYPE:
            return
        if self._is_replay(event):
            return

        edit = is_edit(content)
        command = parse_command(edited_body(content) if edit else str(content.get("body") or ""))
        if command is None:
            return  # ordinary conversation; the room is for people too

        event_id = str(event.get("event_id") or "")
        # Resolved now, not at construction: an admin removed from the Authentik group loses their
        # commands here, one TTL later at worst. They keep their seat in the room and can still read
        # it; kicking them is a separate decision, deliberately not taken here.
        if sender not in await self.admins.admins():
            if edit:
                return  # an edit is no command for anybody; nothing to refuse
            if event_id:
                self._answered.append(event_id)
            log.warning("refused %r from non-admin %s in the control room", command.name, sender)
            await self._reply(f"{sender} is not on the bot's admin allowlist.")
            return

        # Recorded only now, after the allowlist (SEC-12), and before acting (at most once).
        if event_id:
            await self._remember(event_id)
        if edit:
            log.info("ignored an edit by %s that holds the command %r", sender, command.name)
            await self._reply(EDIT_IS_NO_COMMAND)
            return
        await self._dispatch(command, sender)

    def _is_replay(self, event: dict[str, Any]) -> bool:
        """True for an event this process must not act on: too old, or already handled."""
        timestamp = event.get("origin_server_ts")
        if isinstance(timestamp, int | float) and timestamp < self._started_at_ms:
            return True
        event_id = event.get("event_id")
        return bool(event_id) and (event_id in self._seen or event_id in self._answered)

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
            await self._announce(command.argument, sender)
        elif command.name == STATUS:
            await self._reply(await self._status())
        elif command.name == PENDING:
            await self._reply(await self._pending())
        elif command.name in (CONFIRM, REJECT):
            await self._decide_batch(command, sender)
        elif command.name == GUARD:
            await self._guard_command(command, sender)
        elif command.name == LOBBY:
            await self._lobby_command(command, sender)
        elif command.name == ROOM:
            await self._room_command(command, sender)
        else:
            # !help, and anything unrecognised: answering is friendlier than silence, which reads
            # as the bot being down.
            await self._reply(help_text())

    async def _announce(self, message: str, sender: str) -> None:
        if not message:
            await self._reply("Nothing to announce. Usage: !announce <message>")
            return
        if self.broadcast_running:
            await self._reply(BROADCAST_RUNNING.format(rooms=self._broadcast_rooms))
            return
        now = self._clock()
        text = normalize_announcement(message)
        previous = self._last_announcement.get(sender)
        # Every announcement counts, a held one too: pasting it a third time is held again.
        self._last_announcement[sender] = (text, now)
        if previous is not None and previous[0] == text and now - previous[1] < DUPLICATE_WINDOW_SEC:
            await self._hold_duplicate(message, sender, ago=now - previous[1])
            return
        await self._start_broadcast(message)

    async def _start_broadcast(self, message: str) -> None:
        if self._stopping:
            # The pump finishes the slice in hand after a stop; a broadcast started now would run
            # into closing HTTP clients and nobody would cancel it (W5R-04). The refusal is posted
            # here, inside the slice the shutdown waits for, not as a task: the second stop() after
            # the drain would cancel such a task before it posted (W6R-03). Bounded, so a server that
            # does not answer cannot hold the shutdown.
            log.warning("the bot is shutting down; an announcement was not sent")
            with contextlib.suppress(Exception):
                async with asyncio.timeout(INTERRUPTED_NOTE_TIMEOUT_SEC):
                    await self._reply_safely(SHUTTING_DOWN)
            return
        self._broadcast_rooms = 0
        self._broadcast_task = asyncio.create_task(self._run_broadcast(message), name="onbot-broadcast")

    async def _hold_duplicate(self, message: str, sender: str, *, ago: float) -> None:
        """Keep a repeated announcement back until its sender confirms it (ONB-11)."""
        self._forget_expired_announcements()
        taken = set(self._held_announcements) | await self._open_batch_ids()
        held_id = new_batch_id()
        while held_id in taken:
            held_id = new_batch_id()
        self._held_announcements[held_id] = HeldAnnouncement(sender, message, self._clock())
        log.warning("held a repeated announcement by %s as %s until they confirm it", sender, held_id)
        await self._reply(
            DUPLICATE_HELD.format(
                ago=max(1, round(ago)), id=held_id, minutes=round(DUPLICATE_CONFIRM_SEC / 60)
            )
        )

    async def _open_batch_ids(self) -> set[str]:
        """The ids of the guard's open batches, which a held announcement's id must not repeat."""
        if self.guard is None:
            return set()
        try:
            return {b.id for b in await self.guard.open_batches()}
        except GuardStateUnreadable:
            return set()  # nothing can be confirmed until it is reset; the new id is still random
        except Exception:
            # A transport or API error must not cost the admin their answer, nor the rest of the
            # slice its commands (W7R-04). The new id is random; a clash is very unlikely.
            log.warning("could not read the guard's open batches to pick a new id", exc_info=True)
            return set()

    def _forget_expired_announcements(self) -> None:
        now = self._clock()
        for held_id, held in list(self._held_announcements.items()):
            if now - held.held_at >= DUPLICATE_CONFIRM_SEC:
                del self._held_announcements[held_id]

    async def _decide_announcement(self, command: Command, held_id: str, sender: str) -> None:
        """``!confirm <id>`` / ``!reject <id>`` of a held announcement: only by the admin who sent it."""
        held = self._held_announcements[held_id]
        if held.sender != sender:
            await self._reply(
                f"{held_id} is an announcement {held.sender} sent twice; only they can confirm or drop it."
            )
            return
        if command.name == REJECT:
            del self._held_announcements[held_id]
            log.info("admin %s dropped the held announcement %s", sender, held_id)
            await self._reply(f"Dropped the repeated announcement {held_id}; it was not sent.")
            return
        if self.broadcast_running:
            await self._reply(BROADCAST_RUNNING.format(rooms=self._broadcast_rooms))
            return
        del self._held_announcements[held_id]
        self._last_announcement[sender] = (normalize_announcement(held.message), self._clock())
        log.info("admin %s confirmed the repeated announcement %s", sender, held_id)
        await self._start_broadcast(held.message)

    @property
    def broadcast_running(self) -> bool:
        return self._broadcast_task is not None and not self._broadcast_task.done()

    async def _run_broadcast(self, message: str) -> None:
        """The fan-out of one ``!announce``, beside the sync stream; posts its result at the end."""

        async def started(rooms: int) -> None:
            self._broadcast_rooms = rooms
            await self._reply(f"Sending the announcement to {rooms} rooms; the result follows here.")

        try:
            result = await self.broadcast.broadcast(message, on_start=started)
        except asyncio.CancelledError:
            done, total = getattr(self.broadcast, "interrupted", None) or (0, self._broadcast_rooms)
            with contextlib.suppress(Exception):
                async with asyncio.timeout(INTERRUPTED_NOTE_TIMEOUT_SEC):
                    await self._reply(INTERRUPTED.format(done=done, total=total))
            raise
        except Exception as exc:
            log.exception("the broadcast failed")
            await self._reply_safely(f"The announcement failed: {exc}")
            return
        await self._reply_safely(result.summary())

    async def wait_for_broadcast(self) -> None:
        """Wait until the broadcast in flight (if any) has ended and posted its result."""
        task = self._broadcast_task
        if task is not None:
            await asyncio.shield(task)

    async def stop(self) -> None:
        """Cancel a broadcast in flight (shutdown). The rooms not reached yet do not get it.

        From now on no new broadcast starts (W5R-04); safe to call again after the sync pump drained.
        """
        self._stopping = True
        task = self._broadcast_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _reply_safely(self, text: str) -> None:
        """Post ``text``; if that fails, post a short note instead, so the admin is not left guessing."""
        try:
            await self._reply(text)
            return
        except Exception as exc:
            log.warning("could not post a reply in the control room; the reply was:\n%s", text, exc_info=True)
            error = describe_error(exc)
        try:
            await self._reply(REPLY_FAILED.format(error=error))
        except Exception:
            log.warning("could not post the short note either", exc_info=True)

    async def _pending(self) -> str:
        """The guard's open batches and the held announcements: everything `!confirm` can release."""
        parts = [await self._pending_batches()]
        self._forget_expired_announcements()
        parts += [self._describe_held(i, h) for i, h in self._held_announcements.items()]
        return "\n\n".join(parts)

    async def _pending_batches(self) -> str:
        if self.guard is None:
            return "The destructive-action guard is not available in this process."
        try:
            batches = await self.guard.open_batches()
        except GuardStateUnreadable as exc:
            return f"The guard cannot read its state: {exc}. {RESET_HINT}."
        except Exception as exc:
            log.warning("could not read the guard's open batches for !pending", exc_info=True)
            return f"The guard state cannot be fetched right now ({describe_error(exc)}); try again later."
        if not batches:
            return "No destructive actions are waiting for a confirmation."
        return "\n\n".join(describe_batch(b) for b in batches)

    def _describe_held(self, held_id: str, held: HeldAnnouncement) -> str:
        """One held announcement for `!pending` (W6R-04)."""
        left = max(1, round((held.held_at + DUPLICATE_CONFIRM_SEC - self._clock()) / 60))
        text = normalize_announcement(held.message)
        preview = text if len(text) <= HELD_PREVIEW_CHARS else text[: HELD_PREVIEW_CHARS - 3] + "..."
        return (
            f"Announcement {held_id} (held, repeats the previous one of {held.sender}): {preview!r}. "
            f"Only {held.sender} sends it with !confirm {held_id} or drops it with !reject {held_id}; it "
            f"lapses in {left} minute(s)."
        )

    async def _guard_command(self, command: Command, sender: str) -> None:
        """``!guard reset``: replace an unreadable guard state; the old blob is kept verbatim."""
        if command.argument.strip().lower() != GUARD_RESET:
            await self._reply(
                f"Usage: !{GUARD} {GUARD_RESET}. It replaces a guard state the bot cannot read and keeps "
                "a copy of the old one; it does nothing while the state is readable."
            )
            return
        if self.guard is None:
            await self._reply("The destructive-action guard is not available in this process.")
            return
        done, reply = await self.guard.reset(sender)
        await self._reply(reply)
        if done and self.engine is not None:
            self.engine.trigger()  # re-judge what was paused now, not a whole tick later

    async def _lobby_command(self, command: Command, sender: str) -> None:
        """``!lobby reopen <group, room id or alias>``: hand a lobby closed by hand back to the bot (D16)."""
        verb, _, target = command.argument.strip().partition(" ")
        if verb.lower() != LOBBY_REOPEN or not target.strip():
            await self._reply(
                f"Usage: !{LOBBY} {LOBBY_REOPEN} <group pk, lobby room id or lobby alias>. It reopens a "
                "visitor lobby somebody closed by hand; afterwards the bot manages it from the "
                "configuration again."
            )
            return
        if self.engine is None:
            await self._reply("The reconciler is not available in this process.")
            return
        log.info("admin %s asked to reopen the visitor lobby %s", sender, target.strip())
        await self._reply(await self.engine.reopen_lobby(target))

    async def _room_command(self, command: Command, sender: str) -> None:
        """``!room adopt [lobby] <group> <room>``: re-adopt a group room or visitor lobby that lost
        its marker (W4R-06, D20, D25)."""
        parts = command.argument.split()
        lobby = len(parts) == 4 and parts[1].lower() == ROOM_ADOPT_LOBBY
        if lobby:
            parts = [parts[0], *parts[2:]]
        if len(parts) != 3 or parts[0].lower() != ROOM_ADOPT:
            await self._reply(
                f"Usage: !{ROOM} {ROOM_ADOPT} [{ROOM_ADOPT_LOBBY}] <group pk> <room id or alias>. It hands "
                "a group room the bot created (with `lobby`: a group's visitor lobby), whose onbot marker "
                "was removed or rewritten by somebody else, back to the bot (see 'A room lost its onbot "
                "marker' in docs/troubleshooting.md)."
            )
            return
        if self.engine is None:
            await self._reply("The reconciler is not available in this process.")
            return
        kind = OnbotRoomType.visitor_lobby if lobby else OnbotRoomType.group_room
        log.info("admin %s asked to adopt %s as the %s of group %s", sender, parts[2], kind.value, parts[1])
        await self._reply(await self.engine.adopt_room(parts[1], parts[2], by=sender, kind=kind))

    async def _decide_batch(self, command: Command, sender: str) -> None:
        """``!confirm <id>`` / ``!reject <id>``: exactly one batch, never all of them (D13)."""
        batch_id = normalize_batch_id(command.argument.split()[0]) if command.argument.split() else ""
        if not batch_id:
            await self._reply(
                f"Usage: !{command.name} <batch id>. There is no {command.name} without an id; "
                "!pending lists the open batches."
            )
            return
        self._forget_expired_announcements()
        if batch_id in self._held_announcements:
            await self._decide_announcement(command, batch_id, sender)
            return
        if self.guard is None:
            await self._reply("The destructive-action guard is not available in this process.")
            return
        if command.name == CONFIRM:
            done, reply = await self.guard.confirm(batch_id, sender)
        else:
            done, reply = await self.guard.reject(batch_id, sender)
        await self._reply(reply)
        if done and command.name == CONFIRM and self.engine is not None:
            self.engine.trigger()  # run it now, not a whole tick later

    async def _status(self) -> str:
        status = await self._status_line() + "; " + self._rate_limit_status()
        if self.broadcast_running:
            status += f"; an announcement is being sent to {self._broadcast_rooms} rooms"
        if self.guard is not None:
            try:
                waiting = len(await self.guard.open_batches())
            except GuardStateUnreadable:
                return (
                    status
                    + "; the guard cannot read its state and pauses every destructive action, see !pending"
                )
            except Exception as exc:
                log.warning("could not read the guard's open batches for !status", exc_info=True)
                return status + f"; the guard state cannot be fetched right now ({describe_error(exc)})"
            status += f"; destructive actions paused in {waiting} batch(es)" + (
                ", see !pending" if waiting else ""
            )
        return status

    async def _status_line(self) -> str:
        try:
            managed = str(len(await self.broadcast.target_rooms()))
        except RecipientsUnknown:
            managed = "unknown until a pass has mapped the users"
        if self.engine is None or self.engine.last_reconcile_at is None:
            last = "not yet"
        else:
            last = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.engine.last_reconcile_at))
        return f"onbot {__version__} — last reconcile: {last} — managed rooms: {managed}"

    def _rate_limit_status(self) -> str:
        """Whether the bot's send rate limit is lifted, which decides how fast a broadcast is (ONB-03)."""
        lifted = getattr(self.broadcast, "rate_limit_lifted", None)
        if lifted is True:
            return "send rate limit: lifted for the bot"
        if lifted is False:
            return (
                "send rate limit: NOT lifted (the Synapse override failed, see the log), so broadcasts go "
                "out at the homeserver's pace"
            )
        return "send rate limit: override not applied yet"

    async def _reply(self, text: str) -> None:
        assert self.room_id is not None
        await self.client.send_text_message(self.room_id, text, msgtype=NOTICE_MSGTYPE)
