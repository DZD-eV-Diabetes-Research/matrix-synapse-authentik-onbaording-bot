"""The liveness heartbeat (RUN-04, SUP-09) and the default ``onbot healthcheck`` that reads it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from onbot import cli
from onbot.heartbeat import (
    DEFAULT_HEARTBEAT_FILE,
    HEARTBEAT_FILE_ENV_VAR,
    Heartbeat,
    check_heartbeat_file,
    evaluate_heartbeat,
    reconcile_max_age_sec,
    resolve_heartbeat_path,
)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _heartbeat(path: Path | None, clock: _Clock) -> Heartbeat:
    return Heartbeat(path, max_age_sec={"reconcile": 600, "sync": 300}, clock=clock)


def _alive(_pid: int) -> bool:
    return True


def test_reconcile_threshold_is_three_ticks_plus_slack() -> None:
    assert reconcile_max_age_sec(300) == 1200
    assert reconcile_max_age_sec(1) == 303


def test_heartbeat_path_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_heartbeat_path() == Path(DEFAULT_HEARTBEAT_FILE)
    monkeypatch.setenv(HEARTBEAT_FILE_ENV_VAR, "/run/onbot/hb.json")
    assert resolve_heartbeat_path() == Path("/run/onbot/hb.json")
    assert resolve_heartbeat_path("/elsewhere.json") == Path("/elsewhere.json")


def test_the_file_is_written_on_start_and_on_every_report(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "sub" / "hb.json"  # the directory is created
    hb = _heartbeat(path, clock)
    hb.start()
    assert json.loads(path.read_text())["components"] == {}

    clock.now = 1010
    hb.ok("reconcile")
    clock.now = 1020
    hb.failed("sync")
    hb.failed("sync")

    data = json.loads(path.read_text())
    assert data["components"]["reconcile"]["last_ok_at"] == 1010
    assert data["components"]["sync"] == {
        "last_ok_at": None,
        "last_error_at": 1020,
        "consecutive_failures": 2,
    }
    assert data["max_age_sec"] == {"reconcile": 600, "sync": 300}
    assert not list(path.parent.glob(".*.tmp"))  # written atomically, no leftovers


def test_an_unwritable_file_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    hb = _heartbeat(blocker / "hb.json", _Clock())

    hb.start()
    hb.ok("reconcile")  # logged once, swallowed


def _state(hb: Heartbeat) -> dict[str, Any]:
    return json.loads(json.dumps(hb.state))


def test_fresh_components_are_healthy() -> None:
    clock = _Clock()
    hb = _heartbeat(None, clock)
    hb.ok("reconcile")
    hb.ok("sync")
    clock.now += 200

    verdict = evaluate_heartbeat(_state(hb), now=clock.now, pid_alive=_alive)

    assert verdict.healthy, verdict.lines


def test_a_stale_sync_pump_is_unhealthy() -> None:
    clock = _Clock()
    hb = _heartbeat(None, clock)
    hb.ok("reconcile")
    hb.ok("sync")
    clock.now += 301
    hb.ok("reconcile")  # the reconcile loop is fine; the pump is wedged

    verdict = evaluate_heartbeat(_state(hb), now=clock.now, pid_alive=_alive)

    assert not verdict.healthy
    assert any(line.startswith("FAIL sync") for line in verdict.lines)


def test_a_reconcile_loop_that_keeps_failing_turns_unhealthy() -> None:
    # Every pass failing (for example a missing space) used to look healthy as long as the upstreams
    # answered.
    clock = _Clock()
    hb = _heartbeat(None, clock)
    hb.ok("reconcile")
    for _ in range(7):
        clock.now += 100
        hb.failed("reconcile")
        hb.ok("sync")

    verdict = evaluate_heartbeat(_state(hb), now=clock.now, pid_alive=_alive)

    assert not verdict.healthy
    assert any("FAIL reconcile" in line and "7 consecutive failures" in line for line in verdict.lines)


def test_before_the_first_success_the_clock_runs_from_process_start() -> None:
    clock = _Clock()
    hb = _heartbeat(None, clock)
    clock.now += 250
    assert evaluate_heartbeat(_state(hb), now=clock.now, pid_alive=_alive).healthy
    clock.now += 100  # 350 s without a single sync response
    assert not evaluate_heartbeat(_state(hb), now=clock.now, pid_alive=_alive).healthy


def test_a_disabled_sync_pump_is_not_checked() -> None:
    clock = _Clock()
    hb = _heartbeat(None, clock)
    hb.ok("reconcile")
    hb.disable("sync", "homeserver does not support sliding sync")
    clock.now += 500

    assert evaluate_heartbeat(_state(hb), now=clock.now, pid_alive=_alive).healthy


def test_a_dead_writer_is_unhealthy() -> None:
    clock = _Clock()
    hb = _heartbeat(None, clock)
    hb.ok("reconcile")
    hb.ok("sync")

    verdict = evaluate_heartbeat(_state(hb), now=clock.now, pid_alive=lambda _pid: False)

    assert not verdict.healthy


def test_pending_startup_steps_are_reported_but_not_fatal() -> None:
    clock = _Clock()
    hb = _heartbeat(None, clock)
    hb.ok("reconcile")
    hb.ok("sync")
    hb.set_startup_pending(["admin control room"])

    verdict = evaluate_heartbeat(_state(hb), now=clock.now, pid_alive=_alive)

    assert verdict.healthy
    assert any("admin control room" in line for line in verdict.lines)


def test_a_missing_or_corrupt_file_is_unhealthy(tmp_path: Path) -> None:
    assert not check_heartbeat_file(tmp_path / "absent.json").healthy
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert not check_heartbeat_file(broken).healthy
    broken.write_text("[]")
    assert not check_heartbeat_file(broken).healthy


# --- the CLI: the container HEALTHCHECK must not need config or network ------------------------


def _forbid(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("the default healthcheck must not load config or probe dependencies")


def test_default_healthcheck_reads_only_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Before the fix `onbot healthcheck` minted MAS tokens and probed every upstream (RUN-11).
    monkeypatch.setattr(cli, "load_config", _forbid)
    monkeypatch.setattr("onbot.healthcheck.run_healthcheck", _forbid)
    path = tmp_path / "hb.json"
    hb = Heartbeat(path, max_age_sec={"reconcile": 600, "sync": 300})
    hb.ok("reconcile")
    hb.ok("sync")

    assert cli.main(["healthcheck", "--heartbeat-file", str(path)]) == 0
    assert "healthy" in capsys.readouterr().out

    monkeypatch.setenv(HEARTBEAT_FILE_ENV_VAR, str(tmp_path / "other.json"))
    assert cli.main(["healthcheck"]) == 1
    assert "no heartbeat file" in capsys.readouterr().out
