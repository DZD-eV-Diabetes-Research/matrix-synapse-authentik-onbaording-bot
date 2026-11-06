"""What one reconcile pass could not converge, collected per room and reported once (REC-03).

A room the bot cannot fully manage (a banned group member, a user at or above the bot's power level
in a pre-v12 room, an extra creator planted through ``chatroom_params``, a 403 on one state write)
must never stop the rest of the pass. The engine therefore isolates every room, writes what went
wrong into a :class:`PassReport`, carries on, and publishes the report at the end of the room stage:
one WARNING line and one :data:`~onbot.events.Signal.room_not_converged` event per affected room, no
matter how many steps in that room failed. The next pass starts with an empty report, so a room that
stays broken is reported once per pass, never in a tight loop.

Pure bookkeeping, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from onbot.clients.base import ApiError


@dataclass(slots=True)
class RoomProblems:
    """Everything that went wrong in one room during one pass."""

    room_kind: str
    room_id: str | None
    group_pk: str | None
    problems: list[str] = field(default_factory=list)


class PassReport:
    """Per-room problems of one reconcile pass, keyed by (kind, room, group)."""

    def __init__(self) -> None:
        self._rooms: dict[tuple[str, str | None, str | None], RoomProblems] = {}

    def add(self, *, room_kind: str, room_id: str | None, group_pk: str | None, problem: str) -> None:
        key = (room_kind, room_id, group_pk)
        entry = self._rooms.get(key)
        if entry is None:
            entry = self._rooms[key] = RoomProblems(room_kind=room_kind, room_id=room_id, group_pk=group_pk)
        if problem not in entry.problems:  # a step re-run after the guard may report it again
            entry.problems.append(problem)

    def extend(
        self, *, room_kind: str, room_id: str | None, group_pk: str | None, problems: list[str]
    ) -> None:
        for problem in problems:
            self.add(room_kind=room_kind, room_id=room_id, group_pk=group_pk, problem=problem)

    @property
    def rooms(self) -> list[RoomProblems]:
        return list(self._rooms.values())

    def __len__(self) -> int:
        return len(self._rooms)


def describe_error(exc: BaseException) -> str:
    """A short, single-line description of a failed step for the report (no traceback)."""
    if isinstance(exc, ApiError):
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        errcode = payload.get("errcode")
        error = payload.get("error")
        detail = ": ".join(str(part) for part in (errcode, error) if part)
        return f"HTTP {exc.status_code} {detail}".rstrip() if detail else f"HTTP {exc.status_code}"
    return f"{type(exc).__name__}: {exc}"
