"""Smoke tests for the Phase 2 skeleton: package imports and the CLI wires up."""

import pytest

import onbot
from onbot.cli import build_parser, main


def test_package_has_version() -> None:
    assert isinstance(onbot.__version__, str)
    assert onbot.__version__


def test_parser_exposes_all_subcommands() -> None:
    parser = build_parser()
    # argparse stores subcommand names on the 'command' subparsers action.
    subactions = [a for a in parser._actions if a.dest == "command"]
    assert subactions, "expected a 'command' subparser"
    choices = set(subactions[0].choices)
    assert {"run", "reconcile-once", "generate-config", "healthcheck"} <= choices


def test_commands_not_implemented_yet() -> None:
    # Phase 2 is skeleton-only; every command should exit cleanly-but-unimplemented.
    with pytest.raises(SystemExit) as exc:
        main(["reconcile-once"])
    assert "not implemented" in str(exc.value)
