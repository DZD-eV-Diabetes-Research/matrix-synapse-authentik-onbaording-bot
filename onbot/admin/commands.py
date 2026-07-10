"""Parsing and help text for the control room's commands (pure — no I/O, no Matrix).

Commands carry a ``!`` prefix. A bare message without one is **not** a command and is ignored
entirely, so admins can discuss an outage in the control room without a stray sentence paging every
user on the server. That is the whole reason the prefix exists.

Parsing lives here, away from the handler, so the awkward cases can be pinned down by tests instead
of guessed at: ``!announce`` with no text, an argument spanning several lines, a lone ``!``, mixed
case, an unknown verb.
"""

from __future__ import annotations

from dataclasses import dataclass

COMMAND_PREFIX = "!"

ANNOUNCE = "announce"
HELP = "help"
STATUS = "status"

KNOWN_COMMANDS = frozenset({ANNOUNCE, HELP, STATUS})


@dataclass(frozen=True, slots=True)
class Command:
    """A parsed command: the verb, lower-cased, and everything after it verbatim."""

    name: str
    argument: str = ""

    @property
    def is_known(self) -> bool:
        return self.name in KNOWN_COMMANDS


def parse_command(body: str) -> Command | None:
    """Parse a room message into a :class:`Command`, or ``None`` if it is not one.

    ``None`` covers ordinary conversation: no prefix, a lone ``!``, or ``! spaced`` (which is far
    more likely to be someone typing than to be a command). Unknown verbs *do* parse — the caller
    answers them with the help text rather than staying silent, since silence looks like a bug.
    """
    text = body.strip()
    if not text.startswith(COMMAND_PREFIX):
        return None
    rest = text[len(COMMAND_PREFIX) :]
    if not rest or rest[0].isspace():
        return None

    split_at = next((i for i, char in enumerate(rest) if char.isspace()), len(rest))
    name = rest[:split_at].lower()
    argument = rest[split_at:].strip()
    return Command(name=name, argument=argument)


def help_text() -> str:
    """The command reference, posted and pinned in the control room.

    Its hash decides whether the bot re-posts it, so keep it stable: an incidental edit re-pins a
    fresh copy in every deployment's control room.
    """
    return "\n".join(
        (
            "Onbot commands — messages without a ! prefix are ignored.",
            "",
            f"{COMMAND_PREFIX}{ANNOUNCE} <message>  send <message> to every user's onboarding room",
            f"{COMMAND_PREFIX}{STATUS}             bot version, last reconcile, managed rooms",
            f"{COMMAND_PREFIX}{HELP}               this message",
            "",
            "Only users on the bot's admin allowlist may run commands.",
        )
    )
