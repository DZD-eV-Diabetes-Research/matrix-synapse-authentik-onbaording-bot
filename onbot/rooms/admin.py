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

Found again by its canonical alias, and marked with an ``onbot.admin_room`` state event so it is
recognisably bot-managed rather than some room that happens to share the alias.
"""

from __future__ import annotations

import hashlib
from typing import Any

from onbot.admin.admins import AdminResolver
from onbot.admin.commands import help_text
from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix
from onbot.config import OnbotConfig
from onbot.events import Event
from onbot.logging import get_logger
from onbot.reconciler.state import (
    AdminRoomState,
    OnbotRoomType,
    dump_room_state,
    event_type_name,
    parse_room_state,
)

log = get_logger(__name__)

PINNED_EVENTS_TYPE = "m.room.pinned_events"
NOTICE_MSGTYPE = "m.notice"

BOT_LEVEL = 100
# Members may speak (0) but may not touch the room's state, membership or bans (100).
_GOVERNED_KEYS = ("state_default", "invite", "kick", "ban", "redact")


def admin_room_power_levels(bot_user_id: str) -> dict[str, Any]:
    """``power_level_content_override`` for the control room: talk freely, govern not at all."""
    return {
        "users": {bot_user_id: BOT_LEVEL},
        "users_default": 0,
        "events_default": 0,
        **dict.fromkeys(_GOVERNED_KEYS, BOT_LEVEL),
    }


def help_text_hash(text: str) -> str:
    """Idempotency key for the pinned help — mirrors the welcome flow's per-message hashing."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AdminRoomProvisioner:
    """Ensure the control room exists, is shaped correctly, and carries the current pinned help."""

    def __init__(self, client: ApiClientMatrix, config: OnbotConfig, admins: AdminResolver) -> None:
        self.client = client
        self.config = config
        self.admins = admins
        self.cfg = config.admin_room
        self.bot_id = config.synapse_server.bot_user_id
        self.server_name = config.synapse_server.server_name
        self.alias = f"#{self.cfg.alias}:{self.server_name}"
        # Bound by ensure(); until then the invite pass has no room to invite anybody into.
        self.room_id: str | None = None
        self._state_event_type = event_type_name(self.server_name, OnbotRoomType.admin_room)

    async def ensure(self) -> str | None:
        """Return the control room's id, creating it if needed. ``None`` when the feature is off."""
        if not self.cfg.enabled:
            return None
        room_id = await self.client.resolve_room_alias(self.alias)
        if room_id is None:
            room_id = await self._create()
        self.room_id = room_id
        await self.ensure_admins_invited()
        await self._ensure_topic(room_id)
        await self._ensure_pinned_help(room_id)
        return room_id

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
        """
        if self.room_id is None:
            return
        for mxid in sorted(await self.admins.admins()):
            try:
                membership = await self.client.get_membership(self.room_id, mxid)
                if membership in ("join", "invite"):
                    continue
                await self.client.invite_user(self.room_id, mxid)
                log.info("invited %s to the admin control room", mxid)
            except ApiError:
                # An admin sourced from an Authentik group may not have logged in yet, so may have no
                # Matrix account to invite. Warn and carry on: the next tick retries.
                log.warning("could not invite %s to the admin control room", mxid, exc_info=True)

    async def on_reconcile(self, _event: Event) -> None:
        """Bus handler: re-run the invite pass once the reconciler has finished a tick."""
        await self.ensure_admins_invited()

    async def _create(self) -> str:
        # Created empty and populated below, one invite at a time. An admin sourced from an Authentik
        # group may not have logged in yet, and so may have no Matrix account; an unknown MXID in
        # `createRoom`'s invite list fails the whole call, leaving the bot with no control room at
        # all. A single failed invite is a warning.
        room_id = await self.client.create_room(
            alias_localpart=self.cfg.alias,
            name=self.cfg.name,
            topic=self.cfg.topic,
            encrypted=False,  # ADR-0009: the bot must be able to read what is said here.
            room_params={
                "preset": "private_chat",
                "visibility": "private",
                "creation_content": {"m.federate": False},
                "power_level_content_override": admin_room_power_levels(self.bot_id),
            },
        )
        await self.client.put_room_state_event(
            room_id,
            self._state_event_type,
            dump_room_state(AdminRoomState(authentik_server=self.config.authentik_server.url)),
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
            return AdminRoomState(authentik_server=self.config.authentik_server.url)
        parsed = parse_room_state(OnbotRoomType.admin_room, content)
        assert isinstance(parsed, AdminRoomState)
        return parsed
