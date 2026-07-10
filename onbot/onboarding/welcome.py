"""Welcome / onboarding flow (AD-3, G4.*).

When a user is provisioned/seen, the bot opens a 1:1 room with them and sends the configured welcome
messages. The room is a **read-only notice board**, not a conversation: the bot force-joins the user
into it and holds the only power level that may post (:mod:`onbot.onboarding.notice_board`).

Three layers of idempotency keep the flow safe to call repeatedly (the listener may fire on both the
reconciler signal *and* a join event):

* **One DM per user** — the bot's ``m.direct`` account data maps each user to their DM room; an
  existing room is reused rather than re-created.
* **Each message once** — the DM's onbot ``direct_room`` state event records a content hash per sent
  message (G4.3). Already-sent messages are skipped; only new/changed ones go out.
* **One force-join ever** — recorded as ``force_joined_at`` in that same state event, so a user who
  leaves the notice board is not re-joined on the next reconcile tick.

No database: all bookkeeping lives in Matrix account data + room state (AD-1).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig
from onbot.logging import get_logger
from onbot.media import MediaUploader
from onbot.onboarding.notice_board import notice_board_power_levels, power_level_drift
from onbot.reconciler.state import (
    DirectRoomState,
    OnbotRoomType,
    dump_room_state,
    event_type_name,
    parse_room_state,
)

log = get_logger(__name__)

M_DIRECT = "m.direct"

# A force-join the homeserver refuses for a reason of its own (the user is gone, the room is not
# joinable): degrade to the standing invite rather than failing the whole welcome.
_FORCE_JOIN_SOFT_FAILURES = (403, 404)


def _message_key(message: str) -> str:
    """Stable per-message idempotency key (content hash). Changing the text re-sends only that one."""
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


class WelcomeService:
    def __init__(
        self,
        client: ApiClientMatrix,
        config: OnbotConfig,
        *,
        admin: ApiClientSynapseAdmin | None = None,
        media: MediaUploader | None = None,
    ) -> None:
        self.client = client
        self.config = config
        # Force-joining goes through the Synapse admin API; without it the bot can only invite.
        self.admin = admin
        self.media = media
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
        if created:
            state = DirectRoomState(user_id=mxid, authentik_server=self.config.authentik_server.url)
            if await self._force_join(room_id, mxid):
                state.force_joined_at = int(datetime.now(UTC).timestamp())
        else:
            state = await self._load_direct_state(room_id, mxid)
            await self._heal_power_levels(room_id)

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

        room_id = await self.client.create_direct_message_room(
            mxid,
            name=self.config.onboarding_room_name,
            topic=self.config.onboarding_room_topic,
            power_level_content_override=notice_board_power_levels(self.bot_id),
        )
        direct.setdefault(mxid, []).append(room_id)
        await self.client.set_account_data(self.bot_id, M_DIRECT, direct)
        await self._maybe_place_in_space(room_id)
        await self._maybe_set_avatar(room_id)
        return room_id, True

    async def _force_join(self, room_id: str, mxid: str) -> bool:
        """Join ``mxid`` into their notice board via the admin API. ``False`` leaves the invite standing.

        Force-joining works on an invite-only room because the calling admin — the bot — is in the
        room and may invite. Without it the welcome messages sit in a room nobody ever opened, since
        the user has to accept an invitation first.
        """
        if not self.config.force_join_onboarding_room or self.admin is None:
            return False
        try:
            await self.admin.add_user_to_room(room_id, mxid)
        except ApiError as exc:
            if exc.status_code not in _FORCE_JOIN_SOFT_FAILURES:
                raise
            log.warning(
                "could not force-join %s into %s (HTTP %s); leaving the invite for them to accept",
                mxid,
                room_id,
                exc.status_code,
            )
            return False
        log.info("force-joined %s into their onboarding room %s", mxid, room_id)
        return True

    async def _maybe_set_avatar(self, room_id: str) -> None:
        """Give the notice board the bot's own avatar, so it is recognisable in the room list."""
        url = self.config.synapse_server.bot_avatar_url
        if not url or self.media is None:
            return
        try:
            await self.client.set_room_avatar(room_id, await self.media.upload_from_url(url))
        except Exception:
            log.exception("failed to set avatar of onboarding room %s from %s", room_id, url)

    async def _heal_power_levels(self, room_id: str) -> None:
        """Re-apply the notice-board power levels to an existing room if somebody changed them.

        Only repairs rooms the bot still outranks; a room whose user sits at the bot's own level is
        beyond repair (see :mod:`onbot.onboarding.notice_board`), and the write there fails
        harmlessly.
        """
        current = await self.client.get_room_power_levels(room_id)
        drifted = power_level_drift(current, self.bot_id)
        if drifted is None:
            return
        try:
            await self.client.set_room_power_levels(room_id, drifted)
        except ApiError:
            log.warning("cannot restore power levels of %s; the bot does not outrank its user", room_id)
            return
        log.info("restored notice-board power levels in %s", room_id)

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
