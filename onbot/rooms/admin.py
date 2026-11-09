"""Provision the operator control room: create it if missing, keep its help pinned (ADR-0010).

This is the room's *shape*; the commands sent inside it are :mod:`onbot.admin.control_room`.

Four decisions are baked into the room at creation, and each is load-bearing:

* **Unencrypted.** The bot has no crypto stack and reads messages here (ADR-0009). An encrypted
  control room would be a control room the bot cannot hear.
* **``m.federate: false``.** The room commands a bot that can message every user on the server. It
  has no business being reachable from other homeservers.
* **Power levels that let members talk but not govern.** ``events_default: 0`` so admins can
  discuss; ``state_default``/``invite``/``kick``/``ban`` at 100 so only the bot changes the room
  itself. These are the fence — the gate is the allowlist (:mod:`onbot.admin.admins`) checked on
  every command, because a power level says what someone may do *in a room*, not whether they may
  address the whole server.
* **Invited, never force-joined.** Admins are people who chose this job; users are employees who
  did not choose their notice board (:mod:`onbot.onboarding.welcome`).

Found again like every room the bot manages: it created the room, and the room carries the locked
``onbot.admin_room`` marker (:mod:`onbot.rooms.identity`, ONB-17). The alias is presentation only. It
is consulted once, to adopt a control room created by an older version, and before creating a new
one: when it belongs to a room that is not the bot's control room, that room is never taken over
(the bot would otherwise post its help there, invite the admins into it and take commands from it).
The control room then stays off, with an error naming the alias.

**The marker is the bot's, never read back from the room** (W6R-08, D20). Besides its role it holds
the pinned help's hash and the admins the bot invited. The bot changes those fields on the marker it
knows (the one the identity scan trusted, written by the bot, or the one it wrote since), never on
what a ``GET`` returns now: somebody at the bot's level may have rewritten it, and writing that back
would sign their content as the bot's. Like the other managed rooms, the control room gets its
marker, the marker's lock and its alias repaired at startup and after every reconcile pass; a
change somebody else made is reported once in the log and in the room.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from pydantic import ValidationError

from onbot.admin.admins import AdminResolver
from onbot.admin.commands import help_text
from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix
from onbot.config import OnbotConfig
from onbot.events import Event
from onbot.logging import get_logger
from onbot.models import MatrixRoom
from onbot.reconciler.repair import CANONICAL_ALIAS_EVENT_TYPE, marker_repair
from onbot.reconciler.state import (
    AdminRoomState,
    OnbotRoomType,
    dump_room_state,
    event_type_name,
    parse_room_state,
)
from onbot.rooms.identity import RoomIdentityResolver, RoomKey
from onbot.rooms.provision import ManagedRoomSpec, create_managed_room, lock_markers, markers_locked

log = get_logger(__name__)

PINNED_EVENTS_TYPE = "m.room.pinned_events"
CREATE_EVENT_TYPE = "m.room.create"
NOTICE_MSGTYPE = "m.notice"

BOT_LEVEL = 100

ENCRYPTED_ROOM_NOTICE = (
    "This room is end-to-end encrypted, and the bot cannot read encrypted messages, so it will not "
    "see commands sent from your client here. The homeserver encrypts new private rooms by default "
    "(Synapse: encryption_enabled_by_default_for_room_type). An operator has to turn that setting "
    "off and then replace this room; see the troubleshooting guide."
)
# Members may speak (0) but may not touch the room's state, membership or bans (100).
_GOVERNED_KEYS = ("state_default", "invite", "kick", "ban", "redact")


def admin_room_power_levels() -> dict[str, Any]:
    """``power_level_content_override`` for the control room: talk freely, govern not at all.

    The ``users`` map is deliberately omitted. The bot creates this room, so it is the *creator*:
    under room version 12 it holds an infinite power level and must not be named in
    ``m.room.power_levels`` (the auth rules reject it), and on older versions the server's default
    power levels seat the creator at 100 anyway. Either way the bot governs the room without being
    listed here. See ``docs/adr/0011-room-version-12.md``.
    """
    return {
        "users_default": 0,
        "events_default": 0,
        **dict.fromkeys(_GOVERNED_KEYS, BOT_LEVEL),
    }


def help_text_hash(text: str) -> str:
    """Idempotency key for the pinned help — mirrors the welcome flow's per-message hashing."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AdminRoomProvisioner:
    """Ensure the control room exists, is shaped correctly, and carries the current pinned help."""

    def __init__(
        self,
        client: ApiClientMatrix,
        config: OnbotConfig,
        admins: AdminResolver,
        rooms: RoomIdentityResolver,
    ) -> None:
        self.client = client
        self.config = config
        self.admins = admins
        self.rooms = rooms
        self.cfg = config.admin_room
        self.server_name = config.synapse_server.server_name
        self.alias = f"#{self.cfg.alias}:{self.server_name}"
        # Bound by ensure(); until then the invite pass has no room to invite anybody into.
        self.room_id: str | None = None
        self._state_event_type = event_type_name(self.server_name, OnbotRoomType.admin_room)
        # The marker is changed and written whole by the invite pass, the pinned help and the repair,
        # which may run at once (a reconcile finishing during startup).
        self._state_lock = asyncio.Lock()
        # The marker as the bot last wrote it or the identity scan trusted it (W6R-08); set by ensure().
        self._marker: dict[str, Any] | None = None
        self._room_version: str | None = None
        # Repairs already reported, so a change is reported once, not after every pass.
        self._reported: set[str] = set()

    async def ensure(self) -> str | None:
        """Return the control room's id, creating it if needed. ``None`` when the feature is off."""
        if not self.cfg.enabled:
            return None
        found = await self._find_or_create()
        if found is None:
            return None
        room_id, created = found
        self.room_id = room_id
        # What the bot knows its marker to be: the trusted one the scan found, or the one createRoom
        # wrote. Never what the room says now (W6R-08).
        known = None if created else self.rooms.known_marker(room_id)
        self._marker = known or dump_room_state(AdminRoomState(configured_alias=self.cfg.alias))
        if not created:
            await self.repair()
        await self._check_unencrypted(room_id)
        await self.ensure_admins_invited()
        await self._ensure_topic(room_id)
        await self._ensure_pinned_help(room_id)
        return room_id

    async def _check_unencrypted(self, room_id: str) -> None:
        """Say so loudly when the control room is encrypted, so it does not just look alive (ONB-06).

        The bot creates it without encryption, but Synapse's
        ``encryption_enabled_by_default_for_room_type`` adds ``m.room.encryption`` to every new
        private room and a client cannot opt out at ``createRoom``. Admins' clients then encrypt
        their commands and the bot, which has no crypto (ADR-0009), never sees one. The room stays
        bound: commands sent in plain text still work and the reports still arrive, readable by the
        admins.
        """
        if not await self.client.room_is_encrypted(room_id):
            return
        log.error(
            "the admin control room %s (%s) is end-to-end encrypted, so the bot cannot read commands "
            "from clients that encrypt them. Synapse encrypts new private rooms when "
            "encryption_enabled_by_default_for_room_type is set; see docs/troubleshooting.md",
            self.alias,
            room_id,
        )
        await self.client.send_text_message(room_id, ENCRYPTED_ROOM_NOTICE, msgtype=NOTICE_MSGTYPE)

    async def ensure_admins_invited(self) -> None:
        """Resolve the current admin set and invite whoever is not already in the room.

        Runs at startup and again on every reconcile (:data:`~onbot.events.Signal.reconcile_completed`),
        because the two halves of being an admin must not drift apart: a user added to the Authentik
        group may *command* the bot as soon as the resolver's TTL lapses, and would otherwise have no
        way into the room to do it. The room is invite-only and ``invite`` sits at power level 100,
        which only the bot holds — so nobody can let them in by hand, and a restart would be the only
        remedy.

        Cheap enough for the tick: one membership lookup per admin, and anyone already joined or
        invited is skipped, so nobody is re-invited or re-notified.

        An admin who left the room or declined the invitation is remembered and not invited again
        (ONB-12): the record (``invited_admins`` in the room's marker) names everybody the bot invited
        or found in the room while they were admins. Dropping out of the admin set clears the entry,
        so somebody made an admin again is invited again. A banned admin is never invited. The
        record is the bot's own (see the module docstring): a hand-edited marker changes nothing here.
        """
        room_id = self.room_id
        if room_id is None:
            return
        admins = sorted(await self.admins.admins())
        async with self._state_lock:
            state = self._load_state()
            known = set(state.invited_admins)
            record = known & set(admins)
            for mxid in admins:
                if await self._invite_if_due(room_id, mxid, known):
                    record.add(mxid)
            if record != known:
                state.invited_admins = sorted(record)
                await self._write_state(room_id, state)

    async def _invite_if_due(self, room_id: str, mxid: str, known: set[str] | None) -> bool:
        """Invite ``mxid`` unless they are in the room, banned, or left after an invitation.

        Returns whether ``mxid`` is (now) in the room or invited, so it goes into the record.
        """
        try:
            membership = await self.client.get_membership(room_id, mxid)
        except ApiError:
            log.warning("could not read the membership of %s in the admin control room", mxid, exc_info=True)
            return False
        if membership in ("join", "invite"):
            return True
        if membership == "ban":
            log.debug("%s is banned from the admin control room; not inviting them", mxid)
            return False
        if membership == "leave" and (known is None or mxid in known):
            log.debug("%s left the admin control room or declined; not inviting them again", mxid)
            return False
        try:
            await self.client.invite_user(room_id, mxid)
        except ApiError as exc:
            # An admin sourced from an Authentik group may not have logged in yet, so may have no
            # Matrix account to invite. Warn and carry on: the next tick retries.
            log.warning("could not invite %s to the admin control room: %s", mxid, exc)
            return False
        log.info("invited %s to the admin control room", mxid)
        return True

    async def on_reconcile(self, _event: Event) -> None:
        """Bus handler: repair the room and re-run the invite pass once the reconciler finished a tick."""
        await self.repair()
        await self.ensure_admins_invited()

    # --- repair (S7 for the control room, W6R-08) -------------------------------------------------

    async def repair(self) -> None:
        """Restore the marker, its lock and the alias where somebody changed them.

        Reads first and writes only on a difference, like the reconciler's room upkeep. Each repair
        stands on its own: one the server refuses is logged, and the others still run.
        """
        room_id = self.room_id
        if room_id is None or self._marker is None:
            return  # not bound by ensure() yet: nothing known to restore from
        for what, work in (
            ("marker", self._repair_marker),
            ("marker lock", self._repair_lock),
            ("alias", self._repair_alias),
        ):
            try:
                await work(room_id)
            except Exception:
                log.warning(
                    "the admin control room's %s repair failed; retried after the next pass",
                    what,
                    exc_info=True,
                )

    async def _repair_marker(self, room_id: str) -> None:
        async with self._state_lock:
            event = await self.client.get_room_state_event_with_sender(room_id, self._state_event_type)
            repair = marker_repair(
                event,
                bot_id=self.config.synapse_server.bot_user_id,
                expected=AdminRoomState(configured_alias=self.cfg.alias),
                known=self._marker,
            )
            if repair is None:
                return
            await self._write_state(room_id, self._parse(repair.content))
        message = f"the admin control room {room_id}: {repair.reason}; the bot restored it"
        if repair.tampered:
            await self._report_once(f"marker|{(event or {}).get('event_id') or 'missing'}", message + ".")
        else:
            log.info("%s", message)

    async def _repair_lock(self, room_id: str) -> None:
        bot_id = self.config.synapse_server.bot_user_id
        room_version = await self._version(room_id)
        power = await self.client.get_room_power_levels(room_id)
        if markers_locked(power, self.server_name, bot_id, room_version=room_version):
            return
        await lock_markers(self.client, room_id, self.server_name, bot_id=bot_id, room_version=room_version)
        await self._report_once(
            f"lock|{sorted((power.get('events') or {}).items())}",
            f"the admin control room {room_id}: the power level required to change its onbot marker had "
            "been lowered; the bot raised it again.",
        )

    async def _repair_alias(self, room_id: str) -> None:
        """Recreate a removed alias when it is free, and the main address when the room has none."""
        target = await self.client.resolve_room_alias(self.alias)
        if target is None:
            await self.client.set_room_alias(self.alias, room_id)
            log.info("restored the alias %s of the admin control room %s", self.alias, room_id)
        elif target != room_id:
            await self._report_once(
                f"alias|{target}",
                f"the alias {self.alias} of the admin control room {room_id} belongs to room {target}. The "
                "bot leaves it there; the control room keeps working without it.",
            )
            return
        current = await self.client.get_room_state_event(room_id, CANONICAL_ALIAS_EVENT_TYPE) or {}
        if not current.get("alias"):
            await self.client.put_room_state_event(
                room_id, CANONICAL_ALIAS_EVENT_TYPE, {**current, "alias": self.alias}
            )
            log.info("restored the main address %s of the admin control room %s", self.alias, room_id)

    async def _version(self, room_id: str) -> str | None:
        if self._room_version is None:
            create = await self.client.get_room_state_event(room_id, CREATE_EVENT_TYPE) or {}
            self._room_version = str(create.get("room_version") or "1")
        return self._room_version

    async def _report_once(self, key: str, text: str) -> None:
        """Log ``text`` at WARNING and post it in the control room, once per process for ``key``."""
        if key in self._reported:
            return
        self._reported.add(key)
        log.warning("rooms: %s", text)
        assert self.room_id is not None
        try:
            await self.client.send_text_message(self.room_id, f"Rooms: {text}", msgtype=NOTICE_MSGTYPE)
        except Exception:
            log.warning("could not post the repair report in the admin control room", exc_info=True)

    async def _find_or_create(self) -> tuple[str, bool] | None:
        """The control room's id and whether it was created now: found by its marker, adopted once by
        alias, or created.

        ``None`` (and an error in the log) when the alias belongs to a room that is not the bot's control
        room, or two rooms claim to be it: that needs an admin. A problem that may clear by itself (bot
        rooms that could not be read) raises, so the startup step tries again.
        """
        key = RoomKey(OnbotRoomType.admin_room, name=self.cfg.alias)
        resolution = await self.rooms.resolve(await self.rooms.scan(), key, self.alias)
        if resolution.problem is not None:
            if resolution.transient:
                raise RuntimeError(f"admin control room: {resolution.problem}")
            log.error("the admin control room %s stays off: %s", self.alias, resolution.problem)
            return None
        if resolution.room is not None:
            return resolution.room.room_id, False
        room_id = await self._create()
        self.rooms.register(MatrixRoom(room_id=room_id, canonical_alias=self.alias), key)
        return room_id, True

    async def _create(self) -> str:
        # Created empty and populated below, one invite at a time. An admin sourced from an Authentik
        # group may not have logged in yet, and so may have no Matrix account; an unknown MXID in
        # `createRoom`'s invite list fails the whole call, leaving the bot with no control room at
        # all. A single failed invite is a warning.
        room_id = await create_managed_room(
            self.client,
            ManagedRoomSpec(
                marker=AdminRoomState(configured_alias=self.cfg.alias),
                alias_localpart=self.cfg.alias,
                name=self.cfg.name,
                topic=self.cfg.topic,
                encrypted=False,  # ADR-0009: the bot must be able to read what is said here.
                room_params={
                    "preset": "private_chat",
                    "visibility": "private",
                    "creation_content": {"m.federate": False},
                    "power_level_content_override": admin_room_power_levels(),
                },
            ),
            bot_id=self.config.synapse_server.bot_user_id,
        )
        log.info("created admin control room %s (%s)", self.alias, room_id)
        return room_id

    async def _ensure_topic(self, room_id: str) -> None:
        current = await self.client.get_room_state_event(room_id, "m.room.topic") or {}
        if current.get("topic") == self.cfg.topic:
            return
        await self.client.set_room_topic(room_id, self.cfg.topic)

    async def _ensure_pinned_help(self, room_id: str) -> None:
        """Post the command reference and pin it — but only when its text actually changed.

        Without the hash the bot would leave another copy of the same help message in the room on
        every single restart.
        """
        text = help_text()
        digest = help_text_hash(text)
        async with self._state_lock:
            state = self._load_state()
            if state.help_text_hash == digest:
                return

            event_id = await self.client.send_text_message(room_id, text, msgtype=NOTICE_MSGTYPE)
            await self._pin(room_id, event_id, replacing=state.help_event_id)
            state.help_text_hash = digest
            state.help_event_id = event_id
            await self._write_state(room_id, state)
        log.info("posted and pinned the admin help message in %s", room_id)

    async def _pin(self, room_id: str, event_id: str, *, replacing: str | None) -> None:
        """Pin ``event_id``, dropping the help message it supersedes but keeping unrelated pins."""
        # https://spec.matrix.org/latest/client-server-api/#mroompinned_events
        current = await self.client.get_room_state_event(room_id, PINNED_EVENTS_TYPE) or {}
        pinned = [e for e in current.get("pinned", []) if e != replacing]
        pinned.append(event_id)
        await self.client.put_room_state_event(room_id, PINNED_EVENTS_TYPE, {"pinned": pinned})

    def _load_state(self) -> AdminRoomState:
        """The marker the bot knows (W6R-08), never one read in the room; fresh if there is none."""
        return self._parse(self._marker)

    def _parse(self, content: dict[str, Any] | None) -> AdminRoomState:
        if content:
            try:
                parsed = parse_room_state(OnbotRoomType.admin_room, content)
            except ValidationError:
                log.warning(
                    "the admin control room's marker as the bot knows it cannot be read; using a fresh one"
                )
            else:
                if isinstance(parsed, AdminRoomState):
                    return parsed
        return AdminRoomState(configured_alias=self.cfg.alias)

    async def _write_state(self, room_id: str, state: AdminRoomState) -> None:
        """Write the marker and remember it as the bot's own."""
        content = dump_room_state(state)
        await self.client.put_room_state_event(room_id, self._state_event_type, content)
        self._marker = content
        self.rooms.remember_marker(room_id, content)
