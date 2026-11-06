"""Which room is which: room identity by creator and marker, not by alias (D20, REC-04, SEC-02).

A room is a group's room (or its visitor lobby, the space, or the admin control room) when **the bot
created it** and it carries the **onbot marker** of that type, naming that group. The marker is
trusted only once it is **locked**: its event types need a power level no human has
(:func:`onbot.rooms.provision.markers_locked`). The bot locks the marker when it creates a room, so a
room moderator can never rewrite ``group_id`` and move the room to another group. There is no stored
mapping (the bot keeps no database, AD-1); the rooms themselves are the record.

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

Reads use the Synapse admin API (the room list and the full state of one room), which work whether or
not the bot is a member. One state read per bot room and process: what a room is never changes once
it is locked, so the result is cached; only unlocked or unreadable rooms are read again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from onbot.clients.base import ApiError
from onbot.logging import get_logger
from onbot.models import MatrixRoom
from onbot.reconciler.state import (
    MANAGED_ROOM_TYPES,
    GroupRoomState,
    OnbotRoomType,
    VisitorLobbyRoomState,
    event_type_name,
    parse_room_state,
)
from onbot.rooms.provision import lock_markers, markers_locked

log = get_logger(__name__)

CREATE_EVENT_TYPE = "m.room.create"
POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
TOMBSTONE_EVENT_TYPE = "m.room.tombstone"

# Parallel state reads during a scan. The first scan of a process reads every room the bot created.
SCAN_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class RoomKey:
    """A role a room plays for the bot: a group's room or lobby, the space, the control room."""

    kind: OnbotRoomType
    group_id: str | None = None

    def describe(self) -> str:
        if self.kind is OnbotRoomType.group_room:
            return f"room of group {self.group_id}"
        if self.kind is OnbotRoomType.visitor_lobby:
            return f"visitor lobby of group {self.group_id}"
        if self.kind is OnbotRoomType.space:
            return "space"
        if self.kind is OnbotRoomType.admin_room:
            return "admin control room"
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

    @property
    def trusted(self) -> bool:
        return self.key is not None and self.locked and self.problem is None


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
    identity = RoomIdentity(
        room_id=room_id,
        key=None,
        room_version=room_version,
        tombstone=tombstone if isinstance(tombstone, str) else None,
        predecessor=predecessor if isinstance(predecessor, str) else None,
    )

    markers: list[tuple[OnbotRoomType, str, dict[str, Any]]] = []
    for room_type in MANAGED_ROOM_TYPES:
        event_type = event_type_name(server_name, room_type)
        content = (by_type.get(event_type) or {}).get("content")
        if content:  # an empty content is a removed state event
            markers.append((room_type, event_type, dict(content)))
    if not markers:
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
    identity.key = RoomKey(room_type, group_id)
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
        return [i for i in self.identities.values() if i.key == key and i.problem is None]

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
        self._reported: set[str] = set()
        # The space as last resolved by the reconciler, for the welcome flow's space placement.
        self.space_id: str | None = None

    # --- reading --------------------------------------------------------------------------------

    async def scan(self) -> RoomIndex:
        """List every room and identify the bot's own (cached per room id once locked)."""
        index = RoomIndex()
        listing = await self.admin.list_rooms()
        skip: Collection[str] = await self._direct_rooms() if self._direct_rooms is not None else ()
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

    async def identify(self, room_id: str) -> RoomIdentity | None:
        """The identity of one room (``None``: not the bot's), from the cache or one state read."""
        if room_id in self._cache:
            return self._cache[room_id]
        return await self._read(room_id)

    async def _read(self, room_id: str) -> RoomIdentity | None:
        state = await self.admin.get_room_state_events(room_id)
        identity = classify_room(room_id, state, bot_id=self.bot_id, server_name=self.server_name)
        if identity is None or identity.key is None or identity.trusted:
            # Not the bot's, not a managed role, or a locked claim: none of these changes again.
            self._cache[room_id] = identity
        return identity

    def register(self, room: MatrixRoom, key: RoomKey) -> None:
        """Record a room the bot just created for ``key`` (its marker came with ``createRoom``)."""
        self._cache[room.room_id] = RoomIdentity(
            room_id=room.room_id, key=key, locked=True, room_version=room.room_version
        )

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
        if identity.key != key or identity.problem is not None:
            what = identity.problem or (
                f"is the {identity.key.describe()}" if identity.key else "carries no onbot marker"
            )
            return Resolution(
                problem=f"its alias {alias} points at room {target}, which {what}; not adopted, and no "
                "second room is created"
            )
        # A marked room of the bot's at the configured alias, from before markers were locked.
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
        if not new.trusted:
            if new.key is None:
                await self.writer.put_room_state(new.room_id, old.marker_type, dict(old.marker_content or {}))
                new.key, new.marker_type, new.marker_content = old.key, old.marker_type, old.marker_content
            if new.key != old.key:
                return Resolution(
                    problem=f"room {old.room_id} was upgraded to {new.room_id}, which cannot take over "
                    f"as the {old.key.describe()}"
                )
            await self._lock(new)
            log.warning(
                "room identity: the %s moved to its upgraded room %s", old.key.describe(), new.room_id
            )
        return await self._resolved(index, new)

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


def stray_claims(index: RoomIndex) -> list[RoomIdentity]:
    """Rooms of the bot's that are marked but not trusted: not adopted, or with an unreadable marker.

    Called after the roles were resolved, so a legacy room adopted in this pass is no longer among them.
    """
    return [i for i in index.identities.values() if not i.trusted and (i.key is not None or i.problem)]


def describe_stray(identity: RoomIdentity) -> str:
    if identity.problem is not None:
        return f"{identity.problem}; it is not treated as a managed room"
    assert identity.key is not None
    return (
        f"carries an onbot marker from an older version for the {identity.key.describe()}, but that "
        "role's alias does not point at it; it is not adopted and the bot leaves it alone"
    )
