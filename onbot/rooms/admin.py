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
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from onbot.admin.admins import AdminResolver
from onbot.admin.commands import help_text
from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix
from onbot.config import OnbotConfig
from onbot.events import Event
from onbot.logging import get_logger
from onbot.models import MatrixRoom
from onbot.reconciler.state import (
    AdminRoomState,
    OnbotRoomType,
    dump_room_state,
    event_type_name,
    parse_room_state,
)
from onbot.rooms.identity import RoomIdentityResolver, RoomKey
from onbot.rooms.provision import ManagedRoomSpec, create_managed_room

log = get_logger(__name__)

PINNED_EVENTS_TYPE = "m.room.pinned_events"
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
        # The marker is read, changed and written whole by the invite pass and the pinned help, which
        # may run at once (a reconcile finishing during startup).
        self._state_lock = asyncio.Lock()

    async def ensure(self) -> str | None:
        """Return the control room's id, creating it if needed. ``None`` when the feature is off."""
        if not self.cfg.enabled:
            return None
        room_id = await self._find_or_create()
        if room_id is None:
            return None
        self.room_id = room_id
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
        so somebody made an admin again is invited again. A banned admin is never invited. When the
        record cannot be read, only admins who were never in the room are invited.
        """
        room_id = self.room_id
        if room_id is None:
            return
        admins = sorted(await self.admins.admins())
        async with self._state_lock:
            try:
                state: AdminRoomState | None = await self._load_state(room_id)
            except Exception:
                log.warning(
                    "cannot read the admin control room's marker; inviting only admins who were never "
                    "in the room",
                    exc_info=True,
                )
                state = None
            known = set(state.invited_admins) if state is not None else None
            record = (known or set()) & set(admins)
            for mxid in admins:
                if await self._invite_if_due(room_id, mxid, known):
                    record.add(mxid)
            if state is not None and record != known:
                state.invited_admins = sorted(record)
                await self.client.put_room_state_event(
                    room_id, self._state_event_type, dump_room_state(state)
                )

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
        """Bus handler: re-run the invite pass once the reconciler has finished a tick."""
        await self.ensure_admins_invited()

    async def _find_or_create(self) -> str | None:
        """The control room's id: found by its marker, adopted once by alias, or created.

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
            return resolution.room.room_id
        room_id = await self._create()
        self.rooms.register(MatrixRoom(room_id=room_id, canonical_alias=self.alias), key)
        return room_id

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
            state = await self._load_state(room_id)
            if state.help_text_hash == digest:
                return

            event_id = await self.client.send_text_message(room_id, text, msgtype=NOTICE_MSGTYPE)
            await self._pin(room_id, event_id, replacing=state.help_event_id)
            state.help_text_hash = digest
            state.help_event_id = event_id
            await self.client.put_room_state_event(room_id, self._state_event_type, dump_room_state(state))
        log.info("posted and pinned the admin help message in %s", room_id)

    async def _pin(self, room_id: str, event_id: str, *, replacing: str | None) -> None:
        """Pin ``event_id``, dropping the help message it supersedes but keeping unrelated pins."""
        # https://spec.matrix.org/latest/client-server-api/#mroompinned_events
        current = await self.client.get_room_state_event(room_id, PINNED_EVENTS_TYPE) or {}
        pinned = [e for e in current.get("pinned", []) if e != replacing]
        pinned.append(event_id)
        await self.client.put_room_state_event(room_id, PINNED_EVENTS_TYPE, {"pinned": pinned})

    async def _load_state(self, room_id: str) -> AdminRoomState:
        content = await self.client.get_room_state_event(room_id, self._state_event_type)
        if content is None:
            return AdminRoomState(configured_alias=self.cfg.alias)
        parsed = parse_room_state(OnbotRoomType.admin_room, content)
        assert isinstance(parsed, AdminRoomState)
        return parsed
