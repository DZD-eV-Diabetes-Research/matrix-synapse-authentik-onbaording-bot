"""Create every room the bot manages in one place, already marked (ONB-16, ONB-02, D20).

Group rooms, visitor lobbies, the space, the admin control room and the onboarding notice boards are
all created by :func:`create_managed_room` from a :class:`ManagedRoomSpec`. Two things hold for every
one of them:

* **The onbot marker is part of ``createRoom``.** It travels in ``initial_state``, so a room either
  does not exist or carries its marker. Before, the marker was a second request after the room was
  created, and a failure (or a restart) in between left a room nobody could recognise as the bot's.
* **The marker cannot be rewritten by a room moderator.** Right after creation the required power
  level of every onbot marker type is raised above any human level (:func:`lock_markers`). Who
  a room belongs to is decided by its creator plus this marker (see :mod:`onbot.rooms.identity`), so a
  moderator who could edit ``group_id`` could hand the room to another group.

Locking is a second request, because ``power_level_content_override`` replaces the server's whole
default ``events`` map rather than merging into it. If it fails, the room still exists and is marked;
the next pass of the same process retries the lock (the identity resolver remembers the rooms it
created). After a restart in between, the identity scan finds a marked room without the lock and
adopts it through the one-time alias check (:mod:`onbot.rooms.identity`), which locks it then.

The lock level, and what counts as locked:

* **Room version 12 and later.** The bot is the creator and has an infinite power level. Every human
  level the bot hands out is at most 100, so a marker type at :data:`PRIVILEGED_LOCK_LEVEL` is out of
  reach of everybody but a creator. A required level above 100 counts as locked: nobody at 100 or
  below can set an event level above their own.
* **Older room versions.** The bot is an ordinary ``users`` entry, normally at 100, and no longer
  grants its own level (REC-05). The lock level is the bot's level, and a marker type counts as locked
  when its required level is at least the bot's: nobody below the bot can set that. Users at the
  bot's own level (global room admins seated at 100 by older versions, or ``chatroom_params``, D6)
  are the room's admins, equal to the bot, and could rewrite the marker; the lock does not try to
  keep them out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from onbot.logging import get_logger
from onbot.reconciler.power_levels import creators_are_privileged
from onbot.state import AnyRoomState, OnbotRoomType, dump_room_state, event_type_name

if TYPE_CHECKING:
    from onbot.clients.matrix import ApiClientMatrix

log = get_logger(__name__)

# Required level of the onbot marker types in a room where the bot is an infinite creator (v12+).
PRIVILEGED_LOCK_LEVEL = 1000
# The highest level a human gets from the bot (room admin). A v12 marker above it is locked.
HUMAN_CEILING = 100

CREATE_EVENT_TYPE = "m.room.create"


@dataclass(frozen=True, slots=True)
class ManagedRoomSpec:
    """Everything the bot decides about a room before it exists.

    ``room_params`` are passed to ``createRoom`` as they are (preset, visibility,
    ``power_level_content_override``, operator ``chatroom_params``). An ``initial_state`` among them is
    kept; ``extra_initial_state`` and then the marker are appended after it, so the marker always wins.
    """

    marker: AnyRoomState
    alias_localpart: str | None = None
    name: str | None = None
    topic: str | None = None
    encrypted: bool = False
    room_params: Mapping[str, Any] = field(default_factory=dict)
    parent_space_id: str | None = None
    suggested: bool = False
    is_space: bool = False
    is_direct: bool = False
    invite: tuple[str, ...] = ()
    extra_initial_state: tuple[Mapping[str, Any], ...] = ()
    # Notice boards are found through the bot's `m.direct`, not by their marker, and their user can
    # change no state at all; locking would only cost a request per new user.
    lock_marker: bool = True

    @property
    def room_type(self) -> OnbotRoomType:
        return OnbotRoomType(self.marker.room_type)


def marker_event_types(server_name: str) -> list[str]:
    """The state event types of every onbot marker, e.g. ``org.company.onbot.group_room``."""
    return [event_type_name(server_name, room_type) for room_type in OnbotRoomType]


def build_create_room_params(spec: ManagedRoomSpec, server_name: str) -> dict[str, Any]:
    """The ``room_params`` for :meth:`ApiClientMatrix.create_room`, with the marker in ``initial_state``."""
    params: dict[str, Any] = {**spec.room_params}
    if spec.is_space:
        params["creation_content"] = {**(params.get("creation_content") or {}), "type": "m.space"}
    marker_event = {
        "type": event_type_name(server_name, spec.room_type),
        "state_key": "",
        "content": dump_room_state(spec.marker),
    }
    params["initial_state"] = [
        *(params.get("initial_state") or []),
        *(dict(event) for event in spec.extra_initial_state),
        marker_event,
    ]
    return params


async def create_managed_room(client: ApiClientMatrix, spec: ManagedRoomSpec, *, bot_id: str) -> str:
    """Create the room described by ``spec`` and lock its marker; returns the room id.

    The room is created through :meth:`ApiClientMatrix.create_room`, so an ambiguous failure is
    resolved by the creation nonce (RUN-05) and never creates a second room. A failed lock is logged
    and not raised: the room exists and is marked, and the next pass retries the lock.
    """
    room_id = await client.create_room(
        alias_localpart=spec.alias_localpart,
        name=spec.name,
        topic=spec.topic,
        encrypted=spec.encrypted,
        room_params=build_create_room_params(spec, client.server_name),
        parent_space_id=spec.parent_space_id,
        suggested=spec.suggested,
        is_direct=spec.is_direct,
        invite=list(spec.invite) or None,
    )
    if spec.lock_marker:
        try:
            create = await client.get_room_state_event(room_id, CREATE_EVENT_TYPE) or {}
            room_version = str(create.get("room_version") or "1")
            await lock_markers(client, room_id, client.server_name, bot_id=bot_id, room_version=room_version)
        except Exception:
            log.warning(
                "created %s room %s, but could not protect its onbot marker yet; the next pass retries",
                spec.room_type.value,
                room_id,
                exc_info=True,
            )
    return room_id


# --- the lock -----------------------------------------------------------------------------------


def _level(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _bot_level(power_levels: Mapping[str, Any], bot_id: str) -> int:
    users = power_levels.get("users") or {}
    return _level(users.get(bot_id), _level(power_levels.get("users_default"), 0))


def marker_lock_level(power_levels: Mapping[str, Any], bot_id: str, *, room_version: str | None) -> int:
    """The level the marker types get in this room."""
    if creators_are_privileged(room_version):
        return PRIVILEGED_LOCK_LEVEL
    return _bot_level(power_levels, bot_id)


def markers_locked(
    power_levels: Mapping[str, Any], server_name: str, bot_id: str, *, room_version: str | None
) -> bool:
    """Whether nobody below the bot can write any onbot marker type in this room.

    Only explicit ``events`` entries count: a high ``state_default`` alone says nothing about who
    could write the marker in the past, while an entry this high can only have been set by the bot or
    somebody at its level (see the module docstring).
    """
    events = power_levels.get("events") or {}
    types = marker_event_types(server_name)
    if any(t not in events for t in types):
        return False
    lowest = min(_level(events[t], 0) for t in types)
    if creators_are_privileged(room_version):
        return lowest > HUMAN_CEILING
    return lowest >= _bot_level(power_levels, bot_id)


def with_markers_locked(power_levels: Mapping[str, Any], server_name: str, level: int) -> dict[str, Any]:
    """``power_levels`` with every onbot marker type at ``level`` (others untouched)."""
    events = dict(power_levels.get("events") or {})
    for event_type in marker_event_types(server_name):
        events[event_type] = level
    return {**power_levels, "events": events}


class _PowerLevelAccess(Protocol):
    async def get_room_power_levels(self, room_id: str) -> dict[str, Any]: ...

    async def set_room_power_levels(self, room_id: str, power_levels: dict[str, Any]) -> None: ...


async def lock_markers(
    writer: _PowerLevelAccess,
    room_id: str,
    server_name: str,
    *,
    bot_id: str,
    room_version: str | None,
) -> None:
    """Raise the required level of the onbot marker types in ``room_id`` (nothing if already done)."""
    power = await writer.get_room_power_levels(room_id)
    if markers_locked(power, server_name, bot_id, room_version=room_version):
        return
    level = marker_lock_level(power, bot_id, room_version=room_version)
    await writer.set_room_power_levels(room_id, with_markers_locked(power, server_name, level))
