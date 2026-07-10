"""CLI surface tests: argument parsing and command dispatch (Phase 8)."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from onbot import cli


def test_generate_config_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["generate-config"]) == 0
    out = capsys.readouterr().out
    assert "synapse_server" in out
    assert "authentik_server" in out


def test_generate_config_to_file(tmp_path: Path) -> None:
    target = tmp_path / "config.yml"
    assert cli.main(["generate-config", "-o", str(target)]) == 0
    assert "synapse_server" in target.read_text(encoding="utf-8")


def test_healthcheck_dispatches_and_propagates_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_healthcheck(config: object) -> int:
        captured["config"] = config
        return 3

    sentinel = SimpleNamespace(log_level="INFO")  # main() reads log_level off the loaded config
    monkeypatch.setattr(cli, "load_config", lambda: sentinel)
    monkeypatch.setattr(cli, "get_config_file_path", lambda: "config.yml")
    monkeypatch.setattr("onbot.healthcheck.run_healthcheck", fake_run_healthcheck)

    assert cli.main(["healthcheck"]) == 3
    assert captured["config"] is sentinel


def _run_healthcheck_with(monkeypatch: pytest.MonkeyPatch, config_level: str, argv: list[str]) -> int:
    async def fake_run_healthcheck(config: object) -> int:
        return 0

    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(log_level=config_level))
    monkeypatch.setattr(cli, "get_config_file_path", lambda: "config.yml")
    monkeypatch.setattr("onbot.healthcheck.run_healthcheck", fake_run_healthcheck)
    cli.main(argv)
    return logging.getLogger().level


def test_config_log_level_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: the config file's log_level used to be parsed and then never read.
    assert _run_healthcheck_with(monkeypatch, "DEBUG", ["healthcheck"]) == logging.DEBUG


def test_cli_log_level_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_healthcheck_with(monkeypatch, "DEBUG", ["--log-level", "INFO", "healthcheck"]) == logging.INFO


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.main(["does-not-exist"])
