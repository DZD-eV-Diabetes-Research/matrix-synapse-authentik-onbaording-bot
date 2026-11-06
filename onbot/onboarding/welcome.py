"""Welcome / onboarding flow (AD-3, G4.*).

When a user is provisioned/seen, the bot opens a 1:1 room with them and sends the configured welcome
messages. The room is a **read-only notice board**, not a conversation: the bot force-joins the user
into it and holds the only power level that may post (:mod:`onbot.onboarding.notice_board`).

Three layers of idempotency keep the flow safe to call repeatedly (the listener may fire on both the
reconciler signal *and* a join event):

* **One DM per user** — the bot's ``m.direct`` account data maps each user to their DM room; an
  existing room is reused rather than re-created.
* **Each message once:** the DM's onbot ``direct_room`` state event records a key per sent message,
  the hash of its normalised text or its configured ``id`` (G4.3, ONB-18). Already-sent messages are
  skipped; only new or changed ones go out. Each send carries a transaction id derived from the room
  and that key, so a send repeated after a lost answer or a crash is deduplicated by Synapse (ONB-09).
* **One force-join ever** — recorded as ``force_joined_at`` in that same state event, so a user who
  leaves the notice board is not re-joined on the next reconcile tick.
* **Setup finishes eventually:** the notice board is created with its marker in ``initial_state``
  (:mod:`onbot.rooms.provision`), saying ``setup_complete: false``. Space link, avatar and force-join
  happen afterwards, and only then is the flag set. A failure or a restart in between leaves the flag
  unset, so the next welcome of that user finishes the setup instead of skipping it forever (ONB-02).

Those layers only hold if the bookkeeping is visible *before* the trigger it guards against fires.
Force-joining the user lands a ``join`` membership event on the bot's own sync stream, which re-enters
this flow for the same user while the first call is still sending messages — so the state event is
written as soon as the room exists and again after each message, never once at the end. The two calls
are also serialised by :attr:`WelcomeService._locks`, since neither Matrix account data nor room state
offers a compare-and-set. Welcomes of *different* users run concurrently; the one document they share,
the bot's ``m.direct``, is re-read and written under a lock of its own (RUN-01).

The messages themselves are templates: placeholders (``{space_link}`` and friends) are filled in per
recipient and the result is rendered from markdown to HTML, both in :mod:`onbot.formatting`. Neither
step touches the idempotency key, which comes from the configured text (or id): a message is
identified by what the operator wrote, not by what one user saw.

No database: all bookkeeping lives in Matrix account data + room state (AD-1).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Collection
from datetime import UTC, datetime
from urllib.parse import quote

from pydantic import ValidationError

from onbot.clients.base import ApiError
from onbot.clients.matrix import ApiClientMatrix
from onbot.clients.synapse_admin import ApiClientSynapseAdmin
from onbot.config import OnbotConfig, WelcomeMessage
from onbot.formatting import placeholders_in, render_markdown, substitute
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
from onbot.rooms.identity import RoomIdentityResolver, RoomKey
from onbot.rooms.provision import ManagedRoomSpec, create_managed_room

log = get_logger(__name__)

M_DIRECT = "m.direct"

# Placeholders a welcome message can only use when a parent space is configured. A message using one
# of these without a space is held back rather than sent with a hole in it.
_SPACE_PLACEHOLDERS = frozenset({"space_link", "space_alias", "space_name"})

# How the homeserver answers a read of a room the bot is not in (any more).
_NOT_IN_ROOM = (403, 404)

# A force-join the homeserver refuses for a reason of its own (the user is gone, the room is not
# joinable): degrade to the standing invite rather than failing the whole welcome.
_FORCE_JOIN_SOFT_FAILURES = (403, 404)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_message(text: str) -> str:
    """``text`` without whitespace at line ends and without blank lines around it (ONB-18).

    Switching a YAML block from ``|`` to ``|-``, or a stray trailing space, changes the configured
    string but not the message anybody reads, so it must not count as a new message.
    """
    return "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")


def message_text(message: str | WelcomeMessage) -> str:
    return message.text if isinstance(message, WelcomeMessage) else message


def message_keys(message: str | WelcomeMessage) -> tuple[str, ...]:
    """The keys under which ``message`` may be recorded as sent; the first is the one recorded now.

    A plain message is keyed by a hash of its normalised text. Hashes are taken over the
    *configured* text, before placeholders are filled in: what identifies a message is what the
    operator wrote, not what one particular user ended up seeing. A message with an ``id`` is keyed
    by the id, so its text can be corrected without re-sending it (ONB-18).

    The other keys are what older records may hold: the hash of the raw text (before normalisation
    existed) and, for a message that got an id later, the hashes of its text. A match on any of them
    counts as sent, so neither change re-sends anything.
    """
    text = message_text(message)
    keys = [_digest(normalize_message(text)), _digest(text)]
    if isinstance(message, WelcomeMessage):
        keys.insert(0, f"id:{message.id}")
    return tuple(dict.fromkeys(keys))


def welcome_txn_id(room_id: str, key: str) -> str:
    """The transaction id of one welcome message in one room, the same on every attempt (ONB-09).

    Synapse remembers a transaction id per sender and device (for a day in its database, besides a
    30 minute cache) and answers a repeated send with the event it already created. So a send whose
    answer was lost, or one repeated after a crash before the message was recorded, does not post it
    twice. The bot's device is fixed by its access token, which keeps the ids in one scope.
    """
    return "onbot-welcome-" + hashlib.sha256(f"{room_id}\n{key}".encode()).hexdigest()[:32]


def matrix_to_link(alias: str) -> str:
    """A ``matrix.to`` permalink for a room alias, the form clients turn into a join/open link.

    Built from the alias rather than the resolved room id on purpose: an alias link carries its own
    routing, needs no ``?via=`` server hints, and reads as the room's name if the link ever shows up
    as raw text. https://spec.matrix.org/latest/appendices/#matrixto-navigation
    """
    return f"https://matrix.to/#/{quote(alias, safe=':')}"


class NoticeBoardUnreadable(RuntimeError):
    """A user's notice board carries bookkeeping the bot cannot parse; retrying cannot help (ONB-08)."""


class WelcomeService:
    def __init__(
        self,
        client: ApiClientMatrix,
        config: OnbotConfig,
        *,
        admin: ApiClientSynapseAdmin | None = None,
        media: MediaUploader | None = None,
        rooms: RoomIdentityResolver | None = None,
    ) -> None:
        self.client = client
        # Finds the managed space by its marker (D20); without it, the space alias is trusted.
        self.rooms = rooms
        self.config = config
        # Force-joining goes through the Synapse admin API; without it the bot can only invite.
        self.admin = admin
        self.media = media
        self.bot_id = config.synapse_server.bot_user_id
        self.server_name = config.synapse_server.server_name
        self._direct_event_type = event_type_name(self.server_name, OnbotRoomType.direct_room)
        # G4.5: optionally gather onboarding DMs under the managed space (opt-in — 1:1 rooms in a
        # space is a matter of taste). Resolved lazily and cached: the space the reconciler found, or
        # the room at the configured space alias once it is shown to be the bot's space.
        space_cfg = config.create_matrix_rooms_in_a_matrix_space
        self._place_in_space = config.place_onboarding_rooms_in_space and space_cfg.enabled
        self._space_alias = f"#{space_cfg.alias}:{self.server_name}"
        self._space_id: str | None = None
        # G4.6: what a welcome message may refer to by placeholder. The space entries are absent when
        # no parent space is configured, which is what makes a message using them unsendable.
        self._space_values: dict[str, str] = (
            {
                "space_alias": self._space_alias,
                "space_link": matrix_to_link(self._space_alias),
                "space_name": space_cfg.create_matrix_space_if_not_exists.name,
            }
            if space_cfg.enabled
            else {}
        )
        self._warned_about_placeholders = False
        # One lock per user, so the reconciler signal and the sync stream cannot welcome the same
        # person at once. Bounded by the number of users the bot has ever seen.
        self._locks: dict[str, asyncio.Lock] = {}
        # One lock for the bot's `m.direct`, a single account-data document every welcome reads,
        # extends and writes back whole. Without it two users welcomed at once both read the old
        # document and the second write drops the first user's room (RUN-01, ONB-01).
        self._direct_lock = asyncio.Lock()
        # Whether a new notice board of this process was checked for forced encryption (ONB-06).
        self._encryption_checked = False

    async def welcome_user(self, mxid: str) -> None:
        """Ensure ``mxid`` has a DM with all configured welcome messages delivered (idempotent)."""
        messages = self.config.welcome_new_users_messages or []
        if not messages:
            return
        async with self._locks.setdefault(mxid, asyncio.Lock()):
            await self._welcome_user(mxid, messages)

    async def _welcome_user(self, mxid: str, messages: list[str | WelcomeMessage]) -> None:
        room_id, created = await self._ensure_direct_room(mxid)
        if created:
            state = DirectRoomState(user_id=mxid, setup_complete=False)
        else:
            try:
                state = await self._load_direct_state(room_id, mxid)
                await self._heal_power_levels(room_id)
            except ApiError as exc:
                if exc.status_code not in _NOT_IN_ROOM or room_id in await self.client.joined_rooms():
                    raise
                # The bot is no longer in it (purged by hand, or kicked by a user of a room from
                # before the notice boards): a room nobody can post into is no notice board (ONB-08).
                log.warning(
                    "the bot is no longer in %s, the notice board of %s (HTTP %s); opening a new one",
                    room_id,
                    mxid,
                    exc.status_code,
                )
                await self._forget_direct_room(mxid, room_id)
                room_id, created = await self._ensure_direct_room(mxid)
                state = DirectRoomState(user_id=mxid, setup_complete=False)
            else:
                await self._maybe_reinvite(room_id, mxid, state)
        if not state.setup_complete:
            await self._finish_setup(room_id, mxid, state)

        sent = 0
        recorded = state.welcome_messages_sent
        rekeyed = False
        for message in messages:
            key, *older_keys = message_keys(message)
            if key in recorded:
                continue
            earlier = next((recorded[k] for k in older_keys if k in recorded), None)
            if earlier is not None:
                # Sent under a key of an older form (raw text hash, or before it got an id): record it
                # under its current key too, so the next edit of an id message still sends nothing.
                recorded[key] = earlier
                rekeyed = True
                continue
            body = self._fill_placeholders(message_text(message), mxid)
            if body is None:
                continue
            await self.client.send_text_message(
                room_id, body, formatted_body=render_markdown(body), txn_id=welcome_txn_id(room_id, key)
            )
            recorded[key] = datetime.now(UTC).isoformat()
            # Record each message the moment it lands: a crash (or a restart) between two sends must
            # not replay the ones already delivered.
            await self._persist(room_id, state)
            rekeyed = False
            sent += 1
        if rekeyed:
            await self._persist(room_id, state)

        if sent:
            log.info(
                "welcomed %s in %s (%d new, %d tracked)",
                mxid,
                room_id,
                sent,
                len(state.welcome_messages_sent),
            )

    def _fill_placeholders(self, message: str, mxid: str) -> str | None:
        """Fill ``message``'s placeholders for ``mxid``; ``None`` if it needs a space there is none of.

        Held back rather than sent with a hole where the link should be. The message is not recorded
        as sent either, so configuring the space makes it go out on the next tick instead of never.
        """
        missing = (placeholders_in(message) & _SPACE_PLACEHOLDERS) - self._space_values.keys()
        if missing:
            if not self._warned_about_placeholders:
                self._warned_about_placeholders = True
                log.warning(
                    "welcome message uses %s, but create_matrix_rooms_in_a_matrix_space is disabled; "
                    "holding that message back",
                    ", ".join(f"{{{name}}}" for name in sorted(missing)),
                )
            return None
        return substitute(message, {"user_id": mxid, **self._space_values})

    async def _finish_setup(self, room_id: str, mxid: str, state: DirectRoomState) -> None:
        """Space link, avatar and force-join of a new notice board; retried until all are done (ONB-02).

        Each step is safe to repeat. A step that raises leaves ``setup_complete`` unset, so the next
        welcome of this user runs the setup again, and no message is sent into a room the user may
        only have been invited to.
        """
        await self._maybe_place_in_space(room_id)
        await self._maybe_set_avatar(room_id)
        if state.force_joined_at is None and await self._force_join(room_id, mxid):
            state.force_joined_at = int(datetime.now(UTC).timestamp())
        state.setup_complete = True
        await self._persist(room_id, state)

    async def _persist(self, room_id: str, state: DirectRoomState) -> None:
        await self.client.put_room_state_event(room_id, self._direct_event_type, dump_room_state(state))

    async def _ensure_direct_room(self, mxid: str) -> tuple[str, bool]:
        """Return ``(room_id, created)`` — reusing the bot's existing DM with ``mxid`` if any."""
        direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
        existing = direct.get(mxid) or []
        if existing:
            return str(existing[0]), False

        # `private_chat`, not `trusted_private_chat`, which hands the invitee power level 100 and makes
        # the room un-demotable forever; the power levels make it a notice board from the start. The
        # invite stays even when the user is force-joined afterwards: it is the fallback when the
        # force-join is disabled or fails.
        room_id = await create_managed_room(
            self.client,
            ManagedRoomSpec(
                marker=DirectRoomState(user_id=mxid, setup_complete=False),
                name=self.config.onboarding_room_name,
                topic=self.config.onboarding_room_topic,
                # Unencrypted on purpose (ADR-0009): the bot has no crypto. Synapse may still add
                # encryption on its own; see _check_unencrypted.
                encrypted=False,
                is_direct=True,
                invite=(mxid,),
                room_params={
                    "preset": "private_chat",
                    "power_level_content_override": notice_board_power_levels(),
                },
                lock_marker=False,
            ),
            bot_id=self.bot_id,
        )
        await self._record_direct_room(mxid, room_id)
        await self._check_unencrypted(room_id)
        return room_id, True

    async def _check_unencrypted(self, room_id: str) -> None:
        """Log once per process when a new notice board came out encrypted after all (ONB-06).

        Synapse's ``encryption_enabled_by_default_for_room_type`` encrypts every new private room,
        and ``createRoom`` offers no way out. The messages still go out (in plain text, which clients
        flag as "not encrypted"), but the notice boards are not what ADR-0009 promises, and only the
        homeserver setting can fix that. The check is skipped once the first new board was checked.
        """
        if self._encryption_checked:
            return
        self._encryption_checked = True
        try:
            encrypted = await self.client.room_is_encrypted(room_id)
        except ApiError:
            log.debug("could not check whether %s is encrypted", room_id, exc_info=True)
            return
        if encrypted:
            log.error(
                "new notice boards are end-to-end encrypted (%s): the homeserver encrypts new private "
                "rooms (Synapse encryption_enabled_by_default_for_room_type). The bot posts plain text "
                "into them, which clients flag as unencrypted; see docs/troubleshooting.md",
                room_id,
            )

    async def _record_direct_room(self, mxid: str, room_id: str) -> None:
        """Add ``room_id`` to ``mxid``'s entry in ``m.direct``, re-read under the lock (RUN-01).

        Account data has no compare-and-set, so the document is read again right before it is written,
        and every writer in this process goes through :attr:`_direct_lock`. Creating the room stays
        outside the lock: it is the slow part, and the per-user lock already keeps one user from
        getting two rooms.
        """
        async with self._direct_lock:
            direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
            rooms = direct.setdefault(mxid, [])
            if room_id not in rooms:
                rooms.append(room_id)
            await self.client.set_account_data(self.bot_id, M_DIRECT, direct)

    async def prune_direct(self, accounts: Collection[str]) -> list[str]:
        """Drop the ``m.direct`` entries of accounts that are gone; returns the MXIDs dropped.

        ``accounts`` are the active local accounts Synapse listed in the last pass. An entry outside
        them is dropped only on positive evidence: the account is on another server (the bot no
        longer welcomes anybody there, SEC-06), or Synapse reports it deactivated (offboarding erased
        it, ONB-07). An account merely missing from the list (MAS-locked users are not listed, for
        example) keeps its notice board, so a user who returns finds the same room. The room itself is
        left alone: the entry only decides who broadcasts reach and which room the next welcome uses.
        """
        direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
        known = set(accounts)
        gone: list[str] = []
        for mxid in sorted(direct):
            if mxid in known:
                continue
            if not mxid.endswith(f":{self.server_name}"):
                gone.append(mxid)
            elif self.admin is not None:
                account = await self.admin.get_user(mxid)
                if account is not None and account.get("deactivated"):
                    gone.append(mxid)
        if not gone:
            return []
        async with self._direct_lock:
            direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
            for mxid in gone:
                direct.pop(mxid, None)
            await self.client.set_account_data(self.bot_id, M_DIRECT, direct)
        log.info(
            "dropped the notice boards of %d departed or foreign account(s) from m.direct: %s",
            len(gone),
            ", ".join(gone),
        )
        return gone

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
        drifted = power_level_drift(current)
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
            self._space_id = await self._find_space()
        if self._space_id is None:
            log.warning("cannot place DM in space: the space %s is not known yet", self._space_alias)
            return
        await self.client.link_room_to_space(self._space_id, room_id)

    async def _find_space(self) -> str | None:
        """The bot's space: as the reconciler found it, or the room at its alias if that is the space."""
        if self.rooms is None:
            return await self.client.resolve_room_alias(self._space_alias)
        if self.rooms.space_id is not None:
            return self.rooms.space_id
        candidate = await self.client.resolve_room_alias(self._space_alias)
        if candidate is None:
            return None
        identity = await self.rooms.identify(candidate)
        wanted = RoomKey(OnbotRoomType.space, name=self.config.create_matrix_rooms_in_a_matrix_space.alias)
        if identity is None or not identity.trusted or identity.key != wanted:
            log.warning(
                "the space alias %s points at %s, which is not the bot's space; not placing DMs there",
                self._space_alias,
                candidate,
            )
            return None
        return candidate

    async def _load_direct_state(self, room_id: str, mxid: str) -> DirectRoomState:
        content = await self.client.get_room_state_event(room_id, self._direct_event_type)
        if content is None:
            return DirectRoomState(user_id=mxid)
        try:
            parsed = parse_room_state(OnbotRoomType.direct_room, content)
        except ValidationError as exc:
            # Guessing would either re-send every message or none; neither is the bot's call (ONB-08).
            raise NoticeBoardUnreadable(
                f"the onbot state of their notice board {room_id} cannot be read ({exc.error_count()} "
                f"problem(s), first: {exc.errors()[0]['msg']}); correct or remove the "
                f"{self._direct_event_type} state event by hand"
            ) from exc
        assert isinstance(parsed, DirectRoomState)
        return parsed

    async def _forget_direct_room(self, mxid: str, room_id: str) -> None:
        """Drop ``room_id`` from ``mxid``'s ``m.direct`` entry (and the entry, once it is empty)."""
        async with self._direct_lock:
            direct = await self.client.get_account_data(self.bot_id, M_DIRECT)
            rooms = [r for r in direct.get(mxid) or [] if r != room_id]
            if rooms:
                direct[mxid] = rooms
            else:
                direct.pop(mxid, None)
            await self.client.set_account_data(self.bot_id, M_DIRECT, direct)

    async def _maybe_reinvite(self, room_id: str, mxid: str, state: DirectRoomState) -> None:
        """Invite a user who left their notice board back, once (ONB-07).

        The room is invite-only and the original invitation is used up, so a user who clicked
        "Leave" would otherwise never get announcements again. One fresh invitation lets them return
        when they want to; they are not dragged back in. A user who leaves (or declines) again stays
        out. A banned user is never invited.
        """
        if state.reinvited_at is not None:
            return
        if await self.client.get_membership(room_id, mxid) != "leave":
            return
        try:
            await self.client.invite_user(room_id, mxid)
        except ApiError as exc:
            if exc.status_code >= 500:
                raise
            log.warning(
                "could not invite %s back into their notice board %s (HTTP %s)",
                mxid,
                room_id,
                exc.status_code,
            )
            return
        state.reinvited_at = int(datetime.now(UTC).timestamp())
        await self._persist(room_id, state)
        log.info("%s had left their notice board %s; invited them back once", mxid, room_id)
