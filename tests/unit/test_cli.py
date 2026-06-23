"""CLI surface tests: argument parsing and command dispatch (Phase 8)."""

from __future__ import annotations

from pathlib import Path

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

    sentinel = object()
    monkeypatch.setattr(cli, "load_config", lambda: sentinel)
    monkeypatch.setattr(cli, "get_config_file_path", lambda: "config.yml")
    monkeypatch.setattr("onbot.healthcheck.run_healthcheck", fake_run_healthcheck)

    assert cli.main(["healthcheck"]) == 3
    assert captured["config"] is sentinel


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.main(["does-not-exist"])
