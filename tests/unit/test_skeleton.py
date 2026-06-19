"""Smoke tests for the CLI wiring."""

import pytest

import onbot
from onbot.cli import build_parser, main


def test_package_has_version() -> None:
    assert isinstance(onbot.__version__, str)
    assert onbot.__version__


def test_parser_exposes_all_subcommands() -> None:
    parser = build_parser()
    subactions = [a for a in parser._actions if a.dest == "command"]
    assert subactions, "expected a 'command' subparser"
    choices = set(subactions[0].choices or [])
    assert {"run", "reconcile-once", "generate-config", "healthcheck"} <= choices


def test_generate_config_prints_valid_yaml(capsys: pytest.CaptureFixture[str]) -> None:
    import yaml

    assert main(["generate-config"]) == 0
    out = capsys.readouterr().out
    parsed = yaml.safe_load(out)
    assert parsed["synapse_server"]["server_name"] is None
    assert parsed["log_level"] == "INFO"


def test_healthcheck_not_implemented_yet() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["healthcheck"])
    assert "not implemented" in str(exc.value)
