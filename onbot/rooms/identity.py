"""Which room is which: room identity by creator and marker, not by alias (D20, REC-04, SEC-02).

A room is a group's room (or its visitor lobby, the space, or the admin control room) when **the bot
created it** and it carries the **onbot marker** of that type, naming that group, **written by the
bot**. The marker is trusted only once it is **locked**: its event types need a power level no human
has (:func:`onbot.rooms.provision.markers_locked`). The bot locks the marker when it creates a room, so a
room moderator can never rewrite ``group_id`` and move the room to another group. There is no stored
mapping (the bot keeps no database, ADR-0001); the rooms themselves are the record.

The sender check covers what the lock cannot: a legacy room from before the lock, where a moderator
could rewrite the marker, and a room older than version 12, where a human at the bot's own level can
rewrite even a locked one. A marker written by anybody but the bot is reported and never a claim: the
room is not trusted, not adopted and not torn down.

The alias is presentation only. It is read in exactly two places, both before the bot would create a
room for a role nobody holds yet:

* **One-time adoption (migration).** Rooms created by older versions carry the marker but not the
  lock, and the marker was writable by moderators then. Such a room is adopted, which means locked,
  only when the alias that the current configuration gives that role points at it. From then on it
  is found by its locked marker, and the alias is never consulted for it again. A marked room that is
  not at the alias (an old room stranded by an alias change, say) is reported once and left alone.
* **Squatting check.** When the alias already belongs to a room the bot did not create, or to one of
  its rooms with another role, the role is reported and skipped. The bot never adopts such a room and
  never creates a second one beside it.

Fails closed. Two locked rooms claiming the same role are a conflict: reported, and that role is
skipped until an admin resolves it. A bot room whose state cannot be read this pass makes the scan
incomplete; no room is then created or adopted on a guess, and obsolete-room teardown waits.

A room upgraded by somebody other than the bot (``m.room.tombstone``) is reported and its role
skipped. A replacement that the bot itself created as the room's successor (``predecessor`` in its
create event) takes the role over and gets the marker (D20; the bot does not upgrade rooms itself yet).
From then on the old room, which keeps its locked marker, no longer claims the role.

Reads use the Synapse admin API (the room list and the full state of one room), which work whether or
not the bot is a member. One state read per bot room and process: what a room is never changes once
it is locked, so the result is cached; only unlocked or unreadable rooms, and rooms whose marker was
removed or written by somebody else, are read again.

The cache is also the bot's memory of what it wrote: a trusted room's marker content as the bot last
wrote or read it, from which the reconciler restores a marker somebody removed or rewrote (W4R-08).
That memory lives only as long as the process. After a restart, a room whose marker was removed or
rewritten meanwhile cannot be told apart from a room that never played a role, so it is reported with
what a human has to do and never taken back on a guess (W4R-06, D20). A room the
bot just created is read once on the next scan, so its real lock state counts; a lock that failed at
creation is retried then.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from onbot.admin.broadcast import describe_error
from onbot.clients.base import ApiError
from onbot.logging import get_logger
from onbot.models import MatrixRoom
from onbot.rooms.provision import lock_markers, markers_locked
from onbot.state import (
    MANAGED_ROOM_TYPES,
    AdminRoomState,
    GroupRoomState,
    OnbotRoomType,
    SpaceRoomState,
    VisitorLobbyRoomState,
    event_type_name,
    parse_room_state,
)

log = get_logger(__name__)

CREATE_EVENT_TYPE = "m.room.create"
POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
TOMBSTONE_EVENT_TYPE = "m.room.tombstone"
JOIN_RULES_EVENT_TYPE = "m.room.join_rules"
SPACE_ROOM_TYPE = "m.space"

# Parallel state reads during a scan. The first scan of a process reads every room the bot created.
SCAN_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class RoomKey:
    """A role a room plays for the bot: a group's room or lobby, the space, the control room."""

    kind: OnbotRoomType
    group_id: str | None = None
    # The configured alias localpart of a space or control room (see SpaceRoomState); `None` in a
    # marker of an older version.
    name: str | None = None

    def describe(self) -> str:
        if self.kind is OnbotRoomType.group_room:
            return f"room of group {self.group_id}"
        if self.kind is OnbotRoomType.visitor_lobby:
            return f"visitor lobby of group {self.group_id}"
        if self.kind is OnbotRoomType.space:
            return f"space {self.name}" if self.name else "space"
        if self.kind is OnbotRoomType.admin_room:
            return f"admin control room {self.name}" if self.name else "admin control room"
        return f"{self.kind.value} room"


@dataclass(slots=True)
class RoomIdentity:
    """What one room the bot created is, as far as its state says."""

    room_id: str
    key: RoomKey | None
    """The role its marker claims; ``None`` for a room without a managed marker (a notice board)."""
    locked: bool = False
    """The marker cannot be written by any human below the bot, so the claim is trusted."""
    room_version: str | None = None
    problem: str | None = None
    """Why the marker cannot be read as a claim (two roles, invalid content). Never trusted."""
    tombstone: str | None = None
    """The replacement room named by an ``m.room.tombstone``, if the room was upgraded."""
    predecessor: str | None = None
    """The room this one replaces, from its create event."""
    marker_type: str | None = None
    marker_content: dict[str, Any] | None = None
    """The marker as the bot last wrote or read it (see :meth:`RoomIdentityResolver.remember_marker`)."""
    removed_markers: tuple[OnbotRoomType, ...] = ()
    """Managed marker types whose state event is there but empty: somebody removed the marker."""
    foreign_markers: tuple[OnbotRoomType, ...] = ()
    """Managed marker types whose state event somebody other than the bot wrote."""
    marker_types: tuple[OnbotRoomType, ...] = ()
    """Managed marker types the room carries with content, whoever wrote them."""
    is_space: bool = False
    """The room was created as a space (``type: m.space`` in its create event)."""
    join_rule: str | None = None
    """The room's join rule (``restricted`` for a visitor lobby), as last read."""
    notice_board_marker: bool = False
    """The room carries an onbot ``direct_room`` state event (with content or removed): it is, or was,
    a user's notice board, never a managed role (W7R-02)."""

    @property
    def trusted(self) -> bool:
        return self.key is not None and self.locked and self.problem is None

    def lost_marker(self, kind: OnbotRoomType) -> str | None:
        """What happened to the marker of a ``kind`` room the bot created, if it lost it; else ``None``.

        A marker removed or written by somebody else cannot be told apart from a room that never was
        a role after the process that knew the room restarted (W4R-06), so such a room is never taken
        back on its own (D20); this says what a human finds and has to fix.
        """
        if kind in self.removed_markers:
            return f"its onbot {kind.value} marker was removed"
        if self.problem is not None and self.problem.endswith("not by the bot"):
            return f"it {self.problem}"  # "it carries an onbot ... marker written by @x, not by the bot"
        return None


def classify_room(
    room_id: str, state: Sequence[Mapping[str, Any]], *, bot_id: str, server_name: str
) -> RoomIdentity | None:
    """Read a room's identity from its full state; ``None`` when the bot did not create it."""
    by_type: dict[str, Mapping[str, Any]] = {}
    for event in state:
        if event.get("state_key", "") == "" and isinstance(event.get("type"), str):
            by_type[str(event["type"])] = event
    create = by_type.get(CREATE_EVENT_TYPE)
    if create is None:
        return None
    create_content = create.get("content") or {}
    creator = create.get("sender") or create_content.get("creator")
    if creator != bot_id:
        return None
    room_version = str(create_content.get("room_version") or "1")
    predecessor = (create_content.get("predecessor") or {}).get("room_id")
    tombstone = (by_type.get(TOMBSTONE_EVENT_TYPE, {}).get("content") or {}).get("replacement_room")
    join_rule = (by_type.get(JOIN_RULES_EVENT_TYPE, {}).get("content") or {}).get("join_rule")
    identity = RoomIdentity(
        room_id=room_id,
        key=None,
        room_version=room_version,
        tombstone=tombstone if isinstance(tombstone, str) else None,
        predecessor=predecessor if isinstance(predecessor, str) else None,
        is_space=create_content.get("type") == SPACE_ROOM_TYPE,
        join_rule=join_rule if isinstance(join_rule, str) else None,
        notice_board_marker=event_type_name(server_name, OnbotRoomType.direct_room) in by_type,
    )

    markers: list[tuple[OnbotRoomType, str, dict[str, Any]]] = []
    foreign: list[str] = []
    foreign_types: list[OnbotRoomType] = []
    removed: list[OnbotRoomType] = []
    for room_type in MANAGED_ROOM_TYPES:
        event_type = event_type_name(server_name, room_type)
        event = by_type.get(event_type) or {}
        content = event.get("content")
        if not content:  # an empty content is a removed state event
            if event_type in by_type:
                removed.append(room_type)
            continue
        markers.append((room_type, event_type, dict(content)))
        if event.get("sender") != bot_id:
            foreign.append(f"{room_type.value} marker written by {event.get('sender')}")
            foreign_types.append(room_type)
    identity.foreign_markers = tuple(foreign_types)
    identity.marker_types = tuple(m[0] for m in markers)
    # Recorded even beside a marker with content: a former control room or lobby into which somebody
    # wrote a group room marker must not pass for a group room that lost its marker (W7R-03).
    identity.removed_markers = tuple(removed)
    if not markers:
        return identity
    if foreign:
        # The bot writes every marker itself (in createRoom's initial_state, whose events Synapse
        # sends as the creator, on adoption and when following an upgrade). One written by anybody
        # else (a moderator in an unlocked legacy room, a human at the bot's level in a room older
        # than version 12) is never a claim: not trusted, not adopted, never torn down.
        identity.problem = "carries an onbot " + ", ".join(foreign) + ", not by the bot"
        return identity
    if len(markers) > 1:
        identity.problem = (
            "carries more than one onbot marker (" + ", ".join(m[0].value for m in markers) + ")"
        )
        return identity
    room_type, event_type, content = markers[0]
    identity.marker_type, identity.marker_content = event_type, content
    try:
        parsed = parse_room_state(room_type, content)
    except ValidationError:
        identity.problem = f"has an unreadable onbot {room_type.value} marker"
        return identity
    group_id = parsed.group_id if isinstance(parsed, GroupRoomState | VisitorLobbyRoomState) else None
    name = parsed.configured_alias if isinstance(parsed, SpaceRoomState | AdminRoomState) else None
    identity.key = RoomKey(room_type, group_id, name)
    power = (by_type.get(POWER_LEVELS_EVENT_TYPE) or {}).get("content") or {}
    identity.locked = markers_locked(power, server_name, bot_id, room_version=room_version)
    return identity


@dataclass(slots=True)
class RoomIndex:
    """The result of one scan: every listed room, and what the bot's own rooms are."""

    rooms: dict[str, MatrixRoom] = field(default_factory=dict)
    identities: dict[str, RoomIdentity] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)
    """Bot rooms whose state could not be read this pass, with the error."""

    @property
    def complete(self) -> bool:
        return not self.unreadable

    def claims(self, key: RoomKey) -> list[RoomIdentity]:
        """The rooms whose marker claims ``key``, without a predecessor whose upgrade was followed."""
        return [
            i
            for i in self.identities.values()
            if i.key == key and i.problem is None and not self.superseded(i)
        ]

    def superseded(self, identity: RoomIdentity) -> bool:
        """Whether ``identity`` was upgraded to a trusted bot room that took over its role.

        The old room keeps its locked marker; without this, the followed upgrade would read as two
        rooms claiming one role on the next pass.
        """
        if identity.tombstone is None:
            return False
        successor = self.identities.get(identity.tombstone)
        return (
            successor is not None
            and successor.trusted
            and successor.predecessor == identity.room_id
            and successor.key == identity.key
        )

    def trusted(self, kinds: Collection[OnbotRoomType] = MANAGED_ROOM_TYPES) -> list[RoomIdentity]:
        return [
            i for i in self.identities.values() if i.trusted and i.key is not None and i.key.kind in kinds
        ]

    def describe_incomplete(self) -> str:
        sample = ", ".join(sorted(self.unreadable)[:3])
        more = "" if len(self.unreadable) <= 3 else f" and {len(self.unreadable) - 3} more"
        return (
            f"the state of {len(self.unreadable)} room(s) the bot created could not be read ({sample}{more})"
        )


class NoticeBoardsUnreadable(Exception):
    """The bot's ``m.direct`` (its list of notice boards) could not be read (W7R-02)."""


class MarkerWriteFailed(Exception):
    """:meth:`RoomIdentityResolver.adopt` protected the marker types, but writing the marker failed."""

    def __init__(self, cause: ApiError) -> None:
        super().__init__(str(cause))
        self.cause = cause


@dataclass(slots=True)
class Resolution:
    """Which room plays a role this pass, or why there is none."""

    room: MatrixRoom | None = None
    may_create: bool = False
    """No room plays the role and the alias is free: the caller may create one."""
    problem: str | None = None
    """Reported, and the role skipped this pass."""
    transient: bool = False
    """The problem may clear by itself (an unreadable room), as opposed to needing an admin."""
    adopted: bool = False


class _AdminReads(Protocol):
    async def list_rooms(self) -> list[dict[str, Any]]: ...

    async def get_room_state_events(self, room_id: str) -> list[dict[str, Any]]: ...


class _RoomWrites(Protocol):
    async def resolve_room_alias(self, alias: str) -> str | None: ...

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any] | None: ...

    async def put_room_state(self, room_id: str, event_type: str, content: dict[str, Any]) -> None: ...

    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]: ...

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None: ...


class RoomIdentityResolver:
    """Find the rooms the bot manages, adopt legacy ones once, and say which role is in trouble.

    One instance per process, shared by the reconciler, the admin control room and the welcome flow,
    so the cache of room identities is built once.
    """

    def __init__(
        self,
        admin: _AdminReads,
        writer: _RoomWrites,
        *,
        bot_id: str,
        server_name: str,
        direct_rooms: Callable[[], Awaitable[Collection[str]]] | None = None,
    ) -> None:
        self.admin = admin
        self.writer = writer
        self.bot_id = bot_id
        self.server_name = server_name
        # The notice boards, from the bot's `m.direct`: rooms that are never a managed role, so a
        # scan does not read them.
        self._direct_rooms = direct_rooms
        self._cache: dict[str, RoomIdentity | None] = {}
        # Rooms this process created, by role: their lock is retried when it failed at creation.
        self._created: dict[str, RoomKey] = {}
        self._reported: set[str] = set()
        # The space as last resolved by the reconciler, for the welcome flow's space placement.
        self.space_id: str | None = None

    # --- reading --------------------------------------------------------------------------------

    async def scan(self) -> RoomIndex:
        """List every room and identify the bot's own (cached per room id once locked)."""
        index = RoomIndex()
        listing = await self.admin.list_rooms()
        skip: Collection[str] = ()
        if self._direct_rooms is not None:
            try:
                skip = await self._direct_rooms()
            except Exception:
                # Only an optimisation here: without the list, the notice boards are read too.
                log.warning(
                    "could not read the bot's m.direct; the room scan reads every bot room", exc_info=True
                )
        to_read: list[str] = []
        for raw in listing:
            room = MatrixRoom.from_admin_api(raw, is_space=raw.get("room_type") == "m.space")
            index.rooms[room.room_id] = room
            if room.room_id in skip:
                continue
            if room.room_id in self._cache:
                cached = self._cache[room.room_id]
                if cached is not None:
                    index.identities[room.room_id] = cached
                continue
            if room.creator is not None and room.creator != self.bot_id:
                self._cache[room.room_id] = None  # the creator never changes
                continue
            to_read.append(room.room_id)

        semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

        async def read(room_id: str) -> None:
            async with semaphore:
                try:
                    identity = await self._read(room_id)
                except ApiError as exc:
                    if exc.status_code == 404:
                        return  # deleted since the listing
                    index.unreadable[room_id] = str(exc)
                    return
            if identity is not None:
                index.identities[room_id] = identity

        await asyncio.gather(*(read(room_id) for room_id in to_read))
        if index.unreadable:
            log.warning(
                "room identity: %s; no room is created or adopted on a guess", index.describe_incomplete()
            )
        return index

    async def is_notice_board(self, room_id: str) -> bool:
        """Whether ``room_id`` is one of the notice boards in the bot's ``m.direct``.

        Raises :class:`NoticeBoardsUnreadable` when ``m.direct`` cannot be read, so a caller about to
        hand the room a role fails closed (W7R-02).
        """
        if self._direct_rooms is None:
            return False
        try:
            boards = await self._direct_rooms()
        except Exception as exc:
            # The short, bounded cause (status and errcode): the message ends up in the `!room adopt`
            # reply, and an ApiError's own text carries the URL and the whole payload (W8R-07).
            raise NoticeBoardsUnreadable(describe_error(exc)) from exc
        return room_id in boards

    async def identify(self, room_id: str) -> RoomIdentity | None:
        """The identity of one room (``None``: not the bot's), from the cache or one state read."""
        if room_id in self._cache:
            return self._cache[room_id]
        return await self._read(room_id)

    async def _read(self, room_id: str) -> RoomIdentity | None:
        state = await self.admin.get_room_state_events(room_id)
        identity = classify_room(room_id, state, bot_id=self.bot_id, server_name=self.server_name)
        if (
            identity is None
            or identity.trusted
            or (identity.key is None and identity.problem is None and not identity.removed_markers)
        ):
            # Not the bot's, a room that never was a managed role, or a locked claim: none of these
            # changes again. A removed or foreign marker is read again on every scan, so a marker a
            # human restores as the bot account counts from the next pass on (W4R-06).
            self._cache[room_id] = identity
        return identity

    async def reread(self, room_id: str) -> RoomIdentity | None:
        """The identity of one room read now, not from the cache (``None``: not the bot's)."""
        self._cache.pop(room_id, None)
        return await self._read(room_id)

    async def adopt(self, identity: RoomIdentity, key: RoomKey, content: dict[str, Any]) -> None:
        """Hand ``key`` to a room the bot created that lost its marker (``!room adopt``, W4R-06).

        Locks the marker types first, so nobody below the bot can change the marker in between, then
        writes the marker as the bot. From then on the room is trusted like any room the bot created
        for ``key``. The caller has checked that the room may take the role. A failed lock raises the
        :class:`ApiError` (nothing changed); a failed marker write after it raises
        :class:`MarkerWriteFailed` (the lock stays, W6R-05).
        """
        marker_type = event_type_name(self.server_name, key.kind)
        await lock_markers(
            self.writer,
            identity.room_id,
            self.server_name,
            bot_id=self.bot_id,
            room_version=identity.room_version,
        )
        try:
            await self.writer.put_room_state(identity.room_id, marker_type, dict(content))
        except ApiError as exc:
            raise MarkerWriteFailed(exc) from exc
        identity.key, identity.marker_type, identity.marker_content = key, marker_type, dict(content)
        identity.problem = None
        identity.removed_markers = identity.foreign_markers = ()
        identity.marker_types = (key.kind,)
        identity.locked = True
        self._cache[identity.room_id] = identity

    def known_marker(self, room_id: str) -> dict[str, Any] | None:
        """The marker content of a trusted room as the bot last wrote or read it (W4R-08)."""
        identity = self._cache.get(room_id)
        if identity is None or not identity.trusted or identity.marker_content is None:
            return None
        return dict(identity.marker_content)

    def remember_marker(self, room_id: str, content: dict[str, Any]) -> None:
        """Record ``content`` as what the bot wrote (or found written by itself) into a trusted room.

        Content naming another role than the room plays is not remembered (W5R-01).
        """
        identity = self._cache.get(room_id)
        if identity is None or not identity.trusted or not content:
            return
        conflict = self.marker_conflict(room_id, content)
        if conflict is not None:
            log.error("room identity: not remembering the marker of %s: %s", room_id, conflict)
            return
        identity.marker_content = dict(content)

    def marker_conflict(self, room_id: str, content: dict[str, Any]) -> str | None:
        """Why ``content`` cannot be the marker of ``room_id``, a trusted room; ``None`` if it can.

        A marker names the room's role (its type, its group, the space's or control room's alias). For
        a room the bot knows, any content naming another role is somebody else's, however it got here.
        """
        identity = self._cache.get(room_id)
        if identity is None or not identity.trusted or identity.key is None:
            return None
        key = identity.key
        try:
            parsed = parse_room_state(key.kind, content)
        except ValidationError:
            return f"it is not a valid onbot {key.kind.value} marker"
        group_id = parsed.group_id if isinstance(parsed, GroupRoomState | VisitorLobbyRoomState) else None
        name = parsed.configured_alias if isinstance(parsed, SpaceRoomState | AdminRoomState) else None
        if group_id != key.group_id or (name is not None and key.name is not None and name != key.name):
            claimed = RoomKey(key.kind, group_id, name).describe()
            return f"it names the {claimed}, but the room is the {key.describe()}"
        return None

    def register(self, room: MatrixRoom, key: RoomKey) -> None:
        """Record a room the bot just created for ``key`` (its marker came with ``createRoom``).

        Whether its marker got locked is not assumed (:func:`create_managed_room` only logs a failed
        lock): the next scan reads the room's real state, and :meth:`resolve` retries the lock of a
        room this process created for the role.
        """
        self._cache.pop(room.room_id, None)
        self._created[room.room_id] = key

    # --- resolving a role -----------------------------------------------------------------------

    async def resolve(
        self, index: RoomIndex, key: RoomKey, alias: str | None, *, alias_usable: bool = True
    ) -> Resolution:
        """The room that plays ``key`` this pass, adopting a legacy room at ``alias`` once.

        ``alias`` is the canonical alias the configuration gives the role. ``alias_usable=False``
        (the alias collides with another role's) still finds a locked room but neither adopts nor
        allows creating one.
        """
        trusted = [i for i in index.claims(key) if i.locked]
        if len(trusted) > 1:
            ids = ", ".join(sorted(i.room_id for i in trusted))
            return Resolution(
                problem=f"rooms {ids} all claim to be the {key.describe()}; skipped until an admin "
                "removes all but one (D20)"
            )
        if trusted:
            return await self._resolved(index, trusted[0])

        own = [i for i in index.claims(key) if self._created.get(i.room_id) == key]
        if len(own) == 1:
            # Created by this process for the role, but locking its marker failed then: retry. A
            # failure raises, so the role is skipped this pass and tried again on the next.
            await self._lock(own[0])
            log.warning(
                "room identity: protected the marker of %s, the %s, which failed when it was created",
                own[0].room_id,
                key.describe(),
            )
            return await self._resolved(index, own[0])

        if not index.complete:
            return Resolution(
                problem=f"no room is known as the {key.describe()} yet, and {index.describe_incomplete()}; "
                "not creating or adopting one on a guess",
                transient=True,
            )
        if alias is None:
            return Resolution(may_create=True)
        if not alias_usable:
            return Resolution(problem=f"its alias {alias} is wanted by another room too (see the log)")
        target = await self.writer.resolve_room_alias(alias)
        if target is None:
            return Resolution(may_create=True)

        identity = index.identities.get(target)
        if identity is None:
            identity = await self.identify(target)
        if identity is None:
            creator = index.rooms[target].creator if target in index.rooms else None
            return Resolution(
                problem=f"its alias {alias} belongs to room {target}, which the bot did not create"
                + (f" (creator {creator})" if creator else "")
                + "; not adopted, and no second room is created (SEC-02)"
            )
        lost = identity.lost_marker(key.kind)
        if lost is not None:
            return Resolution(
                problem=f"its alias {alias} points at room {target}, which the bot created, but {lost}. "
                "The bot does not take such a room back on its own (D20); until a human restores the "
                "marker as the bot account or moves the alias away, no room is adopted or created "
                "(see 'A room lost its onbot marker' in docs/troubleshooting.md)"
            )
        if not _claims_role(identity, key) or identity.problem is not None:
            what = identity.problem or (
                f"is the {identity.key.describe()}" if identity.key else "carries no onbot marker"
            )
            return Resolution(
                problem=f"its alias {alias} points at room {target}, which {what}; not adopted, and no "
                "second room is created"
            )
        # A marked room of the bot's at the configured alias, from before markers were locked.
        if identity.key != key:
            await self._name(identity, key)
        await self._lock(identity)
        log.warning(
            "room identity: adopted %s as the %s (found at its alias %s); from now on it is found by "
            "its marker",
            target,
            key.describe(),
            alias,
        )
        resolution = await self._resolved(index, identity)
        resolution.adopted = True
        return resolution

    async def _resolved(self, index: RoomIndex, identity: RoomIdentity) -> Resolution:
        """``identity`` plays its role, unless it was upgraded to a room the bot did not create."""
        tombstone = identity.tombstone or await self._live_tombstone(identity.room_id)
        if tombstone:
            # Remembered in the (cached) identity, so the next scan sees the upgrade as followed.
            identity.tombstone = tombstone
            successor = index.identities.get(tombstone)
            if successor is None or successor.predecessor != identity.room_id:
                assert identity.key is not None
                return Resolution(
                    problem=f"room {identity.room_id} was upgraded to {tombstone} by somebody other "
                    f"than the bot; the {identity.key.describe()} is skipped until an admin decides (D20)"
                )
            return await self._follow_upgrade(index, identity, successor)
        room = index.rooms.get(identity.room_id) or MatrixRoom(
            room_id=identity.room_id, room_version=identity.room_version, creator=self.bot_id
        )
        return Resolution(room=room)

    async def _live_tombstone(self, room_id: str) -> str | None:
        content = await self.writer.get_room_state(room_id, TOMBSTONE_EVENT_TYPE) or {}
        replacement = content.get("replacement_room")
        return replacement if isinstance(replacement, str) else None

    async def _follow_upgrade(self, index: RoomIndex, old: RoomIdentity, new: RoomIdentity) -> Resolution:
        """Hand the role to the bot's own successor room and mark it there."""
        assert old.key is not None and old.marker_type is not None
        if new.key is None and new.problem is None:
            await self.writer.put_room_state(new.room_id, old.marker_type, dict(old.marker_content or {}))
            new.key, new.marker_type, new.marker_content = old.key, old.marker_type, old.marker_content
        if new.key != old.key or new.problem is not None:
            return Resolution(
                problem=f"room {old.room_id} was upgraded to {new.room_id}, which cannot take over "
                f"as the {old.key.describe()}"
            )
        if not new.locked:
            await self._lock(new)
            log.warning(
                "room identity: the %s moved to its upgraded room %s", old.key.describe(), new.room_id
            )
        return await self._resolved(index, new)

    async def _name(self, identity: RoomIdentity, key: RoomKey) -> None:
        """Record the configured alias in a legacy space or control room marker that lacks it."""
        assert identity.marker_type is not None
        content = {**(identity.marker_content or {}), "configured_alias": key.name}
        await self.writer.put_room_state(identity.room_id, identity.marker_type, content)
        identity.marker_content, identity.key = content, key

    async def _lock(self, identity: RoomIdentity) -> None:
        await lock_markers(
            self.writer,
            identity.room_id,
            self.server_name,
            bot_id=self.bot_id,
            room_version=identity.room_version,
        )
        identity.locked = True
        self._cache[identity.room_id] = identity

    def log_once(self, room_id: str, message: str) -> None:
        """Log ``message`` about ``room_id`` once per process (for rooms that stay as they are)."""
        if room_id in self._reported:
            return
        self._reported.add(room_id)
        log.warning("room identity: %s", message)


def _claims_role(identity: RoomIdentity, key: RoomKey) -> bool:
    """Whether ``identity``'s marker claims ``key`` (a legacy marker without a configured alias does)."""
    if identity.key == key:
        return True
    legacy = identity.key is not None and identity.key.name is None and key.name is not None
    return legacy and identity.key == RoomKey(key.kind, key.group_id)


def stray_claims(index: RoomIndex) -> list[RoomIdentity]:
    """Rooms of the bot's that are marked but not trusted: not adopted, or with an unreadable marker.

    Called after the roles were resolved, so a legacy room adopted in this pass is no longer among them.
    """
    return [
        i
        for i in index.identities.values()
        if not i.trusted and (i.key is not None or i.problem or i.removed_markers)
    ]


def describe_stray(identity: RoomIdentity) -> str:
    if identity.problem is not None:
        return f"{identity.problem}; it is not treated as a managed room"
    if identity.key is None and identity.removed_markers:
        kinds = ", ".join(k.value for k in identity.removed_markers)
        return (
            f"was created by the bot, but its onbot {kinds} marker was removed. The bot no longer knows "
            "which role it played and leaves it alone (see 'A room lost its onbot marker' in "
            "docs/troubleshooting.md)"
        )
    assert identity.key is not None
    return (
        f"carries an onbot marker from an older version for the {identity.key.describe()}, but that "
        "role's alias does not point at it; it is not adopted and the bot leaves it alone"
    )
