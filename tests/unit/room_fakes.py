"""Fakes for the room identity scan (D20): what the Synapse admin API says about the bot's rooms.

The reconciler finds its rooms by creator and locked marker (:mod:`onbot.rooms.identity`), through two
admin reads: the room list and the full state of a room. :class:`IdentityScanMixin` gives an engine
test's admin fake both, from the listing methods those fakes already have and a ``room_markers`` map.
"""

from __future__ import annotations

from typing import Any, ClassVar

from onbot.reconciler.state import (
    AdminRoomState,
    GroupRoomState,
    OnbotRoomType,
    SpaceRoomState,
    VisitorLobbyRoomState,
    dump_room_state,
    event_type_name,
)
from onbot.rooms.provision import marker_event_types

BOT = "@bot:company.org"
SERVER = "company.org"


def marker_content(room_type: OnbotRoomType, group_id: str | None = None) -> dict[str, Any]:
    if room_type is OnbotRoomType.group_room:
        assert group_id is not None
        return dump_room_state(GroupRoomState(group_id=group_id))
    if room_type is OnbotRoomType.visitor_lobby:
        assert group_id is not None
        return dump_room_state(VisitorLobbyRoomState(group_id=group_id))
    # The configured aliases default to the config defaults.
    if room_type is OnbotRoomType.space:
        return dump_room_state(SpaceRoomState(configured_alias=group_id or "OnBotSpace"))
    return dump_room_state(AdminRoomState(configured_alias=group_id or "onbot-admin"))


def room_state(
    room_type: OnbotRoomType | None = None,
    group_id: str | None = None,
    *,
    creator: str = BOT,
    room_version: str = "12",
    locked: bool = True,
    content: dict[str, Any] | None = None,
    users: dict[str, int] | None = None,
    server_name: str = SERVER,
    extra: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The state events of a room as the admin Room State API lists them."""
    events: list[dict[str, Any]] = [
        {
            "type": "m.room.create",
            "state_key": "",
            "sender": creator,
            "content": {"room_version": room_version},
        }
    ]
    power: dict[str, Any] = {"users": dict(users or {})}
    if room_version != "12":
        power["users"].setdefault(creator, 100)
    if locked:
        level = 1000 if room_version == "12" else 100
        power["events"] = dict.fromkeys(marker_event_types(server_name), level)
    events.append({"type": "m.room.power_levels", "state_key": "", "sender": creator, "content": power})
    if room_type is not None:
        events.append(
            {
                "type": event_type_name(server_name, room_type),
                "state_key": "",
                "sender": creator,
                "content": content if content is not None else marker_content(room_type, group_id),
            }
        )
    return [*events, *(extra or [])]


class IdentityScanMixin:
    """``list_rooms`` and ``get_room_state_events`` for an admin fake.

    ``list_rooms`` (the client's one room listing) is built from the fake's ``fake_rooms`` and, if it
    has one, ``fake_spaces``; these are test hooks, not client methods. Every listed room counts as
    created by the bot. ``room_markers`` names the marker of each managed
    room (``room id -> (type, group id)``); other rooms carry none. ``room_state_events`` overrides
    the whole state of a room.
    """

    room_markers: ClassVar[dict[str, tuple[OnbotRoomType, str | None]]] = {}
    room_state_events: ClassVar[dict[str, list[dict[str, Any]]]] = {}

    async def list_rooms(self) -> list[dict[str, Any]]:
        rooms = [{"creator": BOT, **r} for r in await self.fake_rooms()]  # type: ignore[attr-defined]
        fake_spaces = getattr(self, "fake_spaces", None)
        if fake_spaces is not None:
            rooms += [{"creator": BOT, "room_type": "m.space", **r} for r in await fake_spaces()]
        return rooms

    async def get_room_state_events(self, room_id: str) -> list[dict[str, Any]]:
        if room_id in self.room_state_events:
            return self.room_state_events[room_id]
        marker = self.room_markers.get(room_id)
        if marker is None:
            return room_state()
        return room_state(*marker)
