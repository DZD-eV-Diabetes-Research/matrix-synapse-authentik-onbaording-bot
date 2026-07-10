"""The pure command parser behind the control room."""

from __future__ import annotations

import pytest

from onbot.admin.commands import Command, help_text, parse_command


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "hello everyone",
        "we should !announce something later",  # a prefix mid-sentence is conversation
        "!",
        "! announce",  # far likelier a typo than a command
    ],
)
def test_bare_conversation_is_not_a_command(body: str) -> None:
    assert parse_command(body) is None


def test_a_command_with_no_argument() -> None:
    assert parse_command("!help") == Command(name="help", argument="")


def test_the_argument_is_everything_after_the_verb() -> None:
    assert parse_command("!announce Maintenance at 22:00 UTC") == Command(
        name="announce", argument="Maintenance at 22:00 UTC"
    )


def test_a_multi_line_announcement_keeps_its_line_breaks() -> None:
    assert parse_command("!announce first line\nsecond line").argument == "first line\nsecond line"


def test_the_verb_is_case_insensitive_but_the_argument_is_not() -> None:
    assert parse_command("!ANNOUNCE Hello There") == Command(name="announce", argument="Hello There")


def test_surrounding_whitespace_is_ignored() -> None:
    assert parse_command("  !announce   spaced out   ") == Command(name="announce", argument="spaced out")


def test_unknown_verbs_parse_so_the_caller_can_answer_them() -> None:
    command = parse_command("!frobnicate")
    assert command == Command(name="frobnicate", argument="")
    assert not command.is_known


@pytest.mark.parametrize("verb", ["announce", "help", "status"])
def test_the_supported_verbs_are_known_and_documented(verb: str) -> None:
    assert parse_command(f"!{verb}").is_known
    assert f"!{verb}" in help_text()
