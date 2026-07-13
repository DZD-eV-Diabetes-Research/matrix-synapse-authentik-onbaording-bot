"""Per-room join-rule computation (pure logic), shaped like :mod:`power_levels`.

Only the visitor lobby has a join rule this bot governs. A lobby is ``restricted`` to the parent
space: any member of the space may join it unaided, and nobody else can. Every other room the bot
touches keeps whatever join rule it was created with — the private group room stays ``invite`` — so
:func:`desired_join_rules` returns ``None`` for them, meaning "the bot has no opinion; do not write".

The ``restricted`` rule cannot be expressed as static config because it names the space's *room id*,
which is resolved at runtime from the space alias. See ADR-0012.

https://spec.matrix.org/latest/client-server-api/#mroomjoin_rules
"""

from __future__ import annotations

from typing import Any

from onbot.reconciler.state import OnbotRoomType

# https://spec.matrix.org/latest/client-server-api/#restricted-rooms — the allow-list entry names
# a room whose members may join; a space is such a room.
_ALLOW_TYPE_ROOM_MEMBERSHIP = "m.room_membership"


def desired_join_rules(room_type: OnbotRoomType, space_id: str) -> dict[str, Any] | None:
    """The ``m.room.join_rules`` content the bot wants for ``room_type``, or ``None`` for no opinion.

    A ``visitor_lobby`` is ``restricted`` to ``space_id``: members of the parent space join unaided.
    Requires room version 8+ (``restricted``); ``knock_restricted`` would need 10+. Any other room
    type returns ``None`` — the bot does not manage its join rule.
    """
    if room_type is OnbotRoomType.visitor_lobby:
        return {
            "join_rule": "restricted",
            "allow": [{"type": _ALLOW_TYPE_ROOM_MEMBERSHIP, "room_id": space_id}],
        }
    return None


def join_rules_change(current: dict[str, Any], desired: dict[str, Any] | None) -> dict[str, Any] | None:
    """The content to write to converge ``current`` onto ``desired``, or ``None`` when already equal.

    Returning ``None`` when there is nothing to do is what keeps a no-op tick from sending a state
    event (ADR-0002: a reconcile that changes nothing writes nothing). ``desired`` of ``None`` — a
    room the bot does not govern — is always a no-op.
    """
    if desired is None:
        return None
    if current == desired:
        return None
    return desired
