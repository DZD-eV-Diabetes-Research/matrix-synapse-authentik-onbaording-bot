"""CI shortcuts of review decision D17 (scripts/ci_gate.py and its use in the workflows).

The integration job may be skipped for documentation-only changes, and a release may reuse a green
CI run of the tagged commit. Both decisions must fail safe: when in doubt, the full suite runs.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SHA = "89afecf0000000000000000000000000000000aa"
OTHER_SHA = "771cdaa0000000000000000000000000000000bb"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_gate", REPO / "scripts" / "ci_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_gate"] = module
    spec.loader.exec_module(module)
    return module


cg = _load()


def _workflow(name: str) -> dict[str, Any]:
    data = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _run(**overrides: Any) -> dict[str, Any]:
    run = {
        "id": 42,
        "head_sha": SHA,
        "event": "push",
        "head_branch": "main",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "run_started_at": "2026-11-06T10:10:00Z",
    }
    run.update(overrides)
    return run


def _jobs(run_id: int = 42, **conclusions: str) -> dict[str, Any]:
    jobs = []
    for name in sorted(cg.REQUIRED_JOBS):
        jobs.append({"name": name, "conclusion": "success", "run_id": run_id})
    jobs.append({"name": "changed paths", "conclusion": "success", "run_id": run_id})
    for job in jobs:
        for prefix, conclusion in conclusions.items():
            if job["name"].startswith(prefix):
                job["conclusion"] = conclusion
    return {"jobs": jobs}


# --------------------------------------------------------------------------- needs-integration


@pytest.mark.parametrize(
    "paths",
    [
        ["docs/features.md"],
        ["docs/review_26-11-04/HANDOVER.md", "README.md", "todo.md"],
        ["docs/adr/0010-admin-control-room.md", "docs/img/diagram.svg"],
        ["config.example.yml", "docs/CONFIG_REFERENCE.md"],
        ["LICENSE", ".github/dependabot.yml", ".gitleaks.toml", ".vscode/settings.json"],
        ["tests/README.md"],
    ],
)
def test_documentation_only_changes_skip_the_integration_suite(paths: list[str]) -> None:
    assert cg.needs_integration(paths) is False


@pytest.mark.parametrize(
    "paths",
    [
        ["onbot/app.py"],
        ["docs/features.md", "onbot/reconciler/engine.py"],
        ["tests/integration/test_lifecycle.py"],
        ["tests/integration/stack/docker-compose.yml"],
        ["pyproject.toml"],
        ["pdm.lock"],
        ["Dockerfile"],
        [".dockerignore"],
        ["run_integration_tests.sh"],
        [".github/workflows/ci.yml"],
        ["scripts/ci_gate.py"],
        ["some/new/path.txt"],  # unknown paths always run it
        [".python-version"],
        # The release commit: its push run must include the integration job so the tag can reuse it.
        ["CHANGELOG.md"],
        ["CHANGELOG.md", "docs/features.md"],
    ],
)
def test_any_other_change_runs_the_integration_suite(paths: list[str]) -> None:
    assert cg.needs_integration(paths) is True


def test_an_empty_or_unknown_diff_runs_the_integration_suite() -> None:
    assert cg.needs_integration([]) is True
    assert cg.needs_integration(["", "  "]) is True


def test_cli_reads_paths_from_stdin_and_all_forces_a_run() -> None:
    def cli(stdin: str, *args: str) -> str:
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "ci_gate.py"), "needs-integration", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert cli("docs/a.md\nREADME.md\n") == "false"
    assert cli("docs/a.md\nCHANGELOG.md\n") == "true"
    assert cli("docs/a.md\nonbot/app.py\n") == "true"
    assert cli("") == "true"
    assert cli("docs/a.md\n", "--all") == "true"


# --------------------------------------------------------------------------- release reuse


def test_a_green_push_run_of_the_tagged_commit_is_reused() -> None:
    assert cg.reuse({"workflow_runs": [_run()]}, _jobs(), SHA) is True


@pytest.mark.parametrize(
    ("run", "reason"),
    [
        (_run(head_sha=OTHER_SHA), "another commit"),
        (_run(event="pull_request"), "a PR run"),
        (_run(event="workflow_dispatch"), "a manual run"),
        (_run(head_branch="feature"), "another branch"),
        (_run(path=".github/workflows/release.yml"), "another workflow"),
        (_run(status="in_progress", conclusion=None), "still running"),
        (_run(conclusion="failure"), "failed"),
        (_run(conclusion="cancelled"), "cancelled by a newer push"),
    ],
)
def test_a_run_that_does_not_vouch_for_the_commit_is_not_reused(run: dict[str, Any], reason: str) -> None:
    assert cg.reuse({"workflow_runs": [run]}, _jobs(), SHA) is False, reason


def test_a_run_with_a_skipped_integration_job_is_not_reused() -> None:
    # A docs-only push skips the integration job; its run is green but proves nothing about it.
    jobs = _jobs(**{"integration": "skipped"})
    assert cg.reuse({"workflow_runs": [_run()]}, jobs, SHA) is False


@pytest.mark.parametrize("job", sorted(cg.REQUIRED_JOBS))
def test_every_required_job_must_have_passed(job: str) -> None:
    jobs = {"jobs": [j for j in _jobs()["jobs"] if j["name"] != job]}
    assert cg.reuse({"workflow_runs": [_run()]}, jobs, SHA) is False
    assert cg.reuse({"workflow_runs": [_run()]}, _jobs(**{job: "failure"}), SHA) is False


def test_jobs_of_another_run_are_not_accepted() -> None:
    assert cg.reuse({"workflow_runs": [_run(id=42)]}, _jobs(run_id=7), SHA) is False


def test_the_newest_matching_run_is_picked() -> None:
    runs = {
        "workflow_runs": [
            _run(id=1, run_started_at="2026-11-06T09:00:00Z"),
            _run(id=3, run_started_at="2026-11-06T11:00:00Z", head_sha=OTHER_SHA),
            _run(id=2, run_started_at="2026-11-06T10:00:00Z"),
        ]
    }
    assert cg.pick_run(runs, SHA) == 2


def test_broken_or_missing_api_answers_mean_no_reuse(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("<html>rate limited</html>", encoding="utf-8")
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({"workflow_runs": [_run()]}), encoding="utf-8")
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps(_jobs()), encoding="utf-8")
    script = str(REPO / "scripts" / "ci_gate.py")

    def cli(*args: str) -> str:
        return subprocess.run(
            [sys.executable, script, *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    assert cli("reuse", str(runs), str(jobs), SHA) == "true"
    assert cli("reuse", str(broken), str(jobs), SHA) == "false"
    assert cli("reuse", str(runs), str(broken), SHA) == "false"
    assert cli("reuse", str(runs), str(tmp_path / "missing.json"), SHA) == "false"
    assert cli("pick-run", str(runs), SHA) == "42"
    assert cli("pick-run", str(broken), SHA) == ""


# --------------------------------------------------------------------------- workflow wiring


def test_required_jobs_match_the_job_names_in_ci_yml() -> None:
    names = {job["name"] for key, job in _workflow("ci.yml")["jobs"].items() if key != "changes"}
    assert names == set(cg.REQUIRED_JOBS)


def test_only_the_integration_job_is_skippable_in_ci_yml() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    assert jobs["integration"]["needs"] == "changes"
    assert jobs["integration"]["if"] == "needs.changes.outputs.integration == 'true'"
    for key in ("quality", "secrets", "image"):
        assert "if" not in jobs[key], key


def test_release_publishes_only_behind_the_ci_gate() -> None:
    jobs = _workflow("release.yml")["jobs"]
    gate = (
        "!cancelled() && needs.version.result == 'success' && (needs.ci.result == 'success' || "
        "(needs.ci.result == 'skipped' && needs.ci-reuse.outputs.reuse == 'true'))"
    )
    for key in ("build-dist", "publish-image"):
        assert gate in jobs[key]["if"], key
        assert {"version", "ci-reuse", "ci"} <= set(jobs[key]["needs"]), key
    assert "needs.build-dist.result == 'success'" in jobs["publish-pypi"]["if"]
    assert "build-dist" in jobs["publish-pypi"]["needs"]
    # The full CI runs whenever there is nothing to reuse, also when the lookup itself failed.
    assert "needs.ci-reuse.outputs.reuse != 'true'" in jobs["ci"]["if"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    # The lookup needs to read runs, nothing more.
    assert jobs["ci-reuse"]["permissions"] == {"contents": "read", "actions": "read"}
