"""Welcome / onboarding flow (AD-3, G4.*).

When a user is provisioned/seen, the bot opens a 1:1 direct room with them and sends the configured
welcome messages. Two layers of idempotency keep it safe to call repeatedly (the listener may fire
on both the reconciler signal *and* a join event):

* **One DM per user** — the bot's ``m.direct`` account data maps each user to their DM room; an
  existing room is reused rather than re-created.
* **Each message once** — the DM's onbot ``direct_room`` state event records a content hash per sent
  message (G4.3). Already-sent messages are skipped; only new/changed ones go out.

No database: all bookkeeping lives in Matrix account data + room state (AD-1).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from onbot.clients.matrix import ApiClientMatrix
from onbot.config import OnbotConfig
from onbot.logging import get_logger
from onbot.reconciler.state import (
    DirectRoomState,
    OnbotRoomType,
    dump_room_state,
    event_type_name,
    parse_room_state,
)

log = get_logger(__name__)

M_DIRECT = "m.direct"


def _message_key(message: str) -> str:
    """Stable per-message idempotency key (content hash). Changing the text re-sends only that one."""
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


class WelcomeService:
    def __init__(self, client: ApiClientMatrix, config: OnbotConfig) -> None:
        self.client = client
        self.config = config
        self.bot_id = config.synapse_server.bot_user_id
        self.server_name = config.synapse_server.server_name
        self._direct_event_type = event_type_name(self.server_name, OnbotRoomType.direct_room)
        # G4.5: optionally gather onboarding DMs under the managed space (opt-in — 1:1 rooms in a
        # space is a matter of taste). Resolved lazily from the configured space alias and cached.
        space_cfg = config.create_matrix_rooms_in_a_matrix_space
        self._place_in_space = config.place_onboarding_rooms_in_space and space_cfg.enabled
        self._space_alias = f"#{space_cfg.alias}:{self.server_name}"
        self._space_id: str | None = None

    async def welcome_user(self, mxid: str) -> None:
        """Ensure ``mxid`` has a DM with all configured welcome messages delivered (idempotent)."""
        messages = self.config.welcome_new_users_messages or []
        if not messages:
            return

        room_id, created = await self._ensure_direct_room(mxid)
        state = (
            await self._load_direct_state(room_id, mxid)
            if not created
            else DirectRoomState(user_id=mxid, authentik_server=self.config.authentik_server.url)
        )

        changed = created
        for message in messages:
            key = _message_key(message)
            if key in state.welcome_messages_sent:
                continue
            await self.client.send_text_message(room_id, message)
            state.welcome_messages_sent[key] = datetime.now(UTC).isoformat()
            changed = True

        if changed:
            await self.client.put_room_state_event(room_id, self._direct_event_type, dump_room_state(state))
            log.info(
                "welcomed %s in %s (%d messages tracked)",
                mxid,
                room_id,
                len(state.welcome_messages_sent),
            )

    async def _ensure_direct_room(self, mxid: str) -> tuple[str, bool]:
        """Return ``(room_id, created)`` — reusing the bot's existing DM with ``mxid`` if any."""
        direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
        existing = direct.get(mxid) or []
        if existing:
            return str(existing[0]), False

        room_id = await self.client.create_direct_message_room(mxid)
        direct.setdefault(mxid, []).append(room_id)
        await self.client.set_account_data(self.bot_id, M_DIRECT, direct)
        await self._maybe_place_in_space(room_id)
        return room_id, True

    async def _maybe_place_in_space(self, room_id: str) -> None:
        """Add a freshly created DM to the managed space, if configured (G4.5)."""
        if not self._place_in_space:
            return
        if self._space_id is None:
            self._space_id = await self.client.resolve_room_alias(self._space_alias)
        if self._space_id is None:
            log.warning("cannot place DM in space: alias %s does not resolve yet", self._space_alias)
            return
        await self.client.link_room_to_space(self._space_id, room_id)

    async def _load_direct_state(self, room_id: str, mxid: str) -> DirectRoomState:
        content = await self.client.get_room_state_event(room_id, self._direct_event_type)
        if content is None:
            return DirectRoomState(user_id=mxid, authentik_server=self.config.authentik_server.url)
        parsed = parse_room_state(OnbotRoomType.direct_room, content)
        assert isinstance(parsed, DirectRoomState)
        return parsed
