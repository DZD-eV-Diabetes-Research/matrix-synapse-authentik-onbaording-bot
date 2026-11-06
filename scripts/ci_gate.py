"""CI shortcuts that never weaken the gate (review decision D17).

Two decisions, both fail-safe (when in doubt, run the full suite):

``needs-integration``
    Reads the changed paths of a push or pull request (one per line on stdin) and prints ``true``
    when the slow integration job has to run. It is skipped only when *every* changed path is
    documentation or repository metadata (:data:`DOCS_ONLY`); any other path, an unknown one, an
    empty list or ``CHANGELOG.md`` (:data:`ALWAYS_RUN`) runs it. The CI workflow passes ``--all``
    when it cannot compute the diff.

``reuse``
    Decides whether a release may reuse the CI result of the tagged commit instead of running the
    whole CI again. Input: the JSON of ``GET /repos/{repo}/actions/workflows/ci.yml/runs`` (filtered
    by ``head_sha``) and of ``GET /repos/{repo}/actions/runs/{id}/jobs`` for the run it picked.
    Reuse only a run that is a ``push`` to ``main`` of exactly that commit, completed with
    ``success``, in which every job of :data:`REQUIRED_JOBS` concluded ``success`` (a skipped
    integration job does not count). Prints ``true`` or ``false``.

Usage in the workflows:

    git diff --name-only "$BASE" "$HEAD" | python3 scripts/ci_gate.py needs-integration
    python3 scripts/ci_gate.py pick-run runs.json "$SHA"          # prints a run id or nothing
    python3 scripts/ci_gate.py reuse runs.json jobs.json "$SHA"   # prints true or false

Standard library only: it runs on the runner's system Python, before any dependency is installed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

# Paths whose change cannot affect the integration suite. Everything else runs it.
DOCS_ONLY: tuple[str, ...] = (
    "docs/*",
    "*.md",
    "LICENSE",
    "config.example.yml",  # generated from the model; the quality job checks it is in sync
    ".gitleaks.toml",  # the secret scan job runs on every change anyway
    ".github/dependabot.yml",
    ".vscode/*",
)

# The jobs of ci.yml a reused run must have passed, by their display names.
REQUIRED_JOBS: frozenset[str] = frozenset(
    {
        "lint · type · test",
        "secret scan (gitleaks)",
        "image build · smoke test",
        "integration (live Synapse · MAS · Authentik)",
    }
)

# Documentation that still runs the suite: the changelog commit is the one a release tags, so its
# push run has to include the integration job for the release to reuse it.
ALWAYS_RUN: tuple[str, ...] = ("CHANGELOG.md",)

CI_WORKFLOW_PATH = ".github/workflows/ci.yml"


def is_docs_only(path: str) -> bool:
    # fnmatch's "*" also matches "/", so "docs/*" covers the whole tree and "*.md" any depth.
    if path in ALWAYS_RUN:
        return False
    return any(fnmatchcase(path, pattern) for pattern in DOCS_ONLY)


def needs_integration(paths: Iterable[str]) -> bool:
    changed = [p.strip() for p in paths if p.strip()]
    if not changed:
        return True  # no diff known: never assume it is safe to skip
    return not all(is_docs_only(p) for p in changed)


def pick_run(runs: Mapping[str, Any], sha: str) -> int | None:
    """The newest successful push run of ci.yml on main for exactly ``sha``."""
    candidates = [
        run
        for run in runs.get("workflow_runs", [])
        if run.get("head_sha") == sha
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("path", "").split("@")[0] == CI_WORKFLOW_PATH
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda run: (run.get("run_started_at") or "", run.get("id", 0)))
    run_id = newest.get("id")
    return run_id if isinstance(run_id, int) else None


def jobs_passed(jobs: Mapping[str, Any]) -> bool:
    """Every required job ran (latest attempt) and succeeded."""
    conclusions: dict[str, str | None] = {}
    for job in jobs.get("jobs", []):
        conclusions[job.get("name", "")] = job.get("conclusion")
    return all(conclusions.get(name) == "success" for name in REQUIRED_JOBS)


def reuse(runs: Mapping[str, Any], jobs: Mapping[str, Any], sha: str) -> bool:
    run_id = pick_run(runs, sha)
    if run_id is None:
        return False
    if any(job.get("run_id") not in (None, run_id) for job in jobs.get("jobs", [])):
        return False  # jobs of a different run were passed in
    return jobs_passed(jobs)


def _load_json(path: str) -> Mapping[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ni = sub.add_parser("needs-integration", help="changed paths on stdin; prints true or false")
    ni.add_argument("--all", action="store_true", help="the diff is unknown: always true")
    pr = sub.add_parser("pick-run", help="prints the id of a reusable CI run, or nothing")
    pr.add_argument("runs")
    pr.add_argument("sha")
    ru = sub.add_parser("reuse", help="prints true when the release may reuse the CI result")
    ru.add_argument("runs")
    ru.add_argument("jobs")
    ru.add_argument("sha")
    args = parser.parse_args(argv)

    if args.command == "needs-integration":
        print("true" if args.all or needs_integration(sys.stdin) else "false")
    elif args.command == "pick-run":
        run_id = pick_run(_load_json(args.runs), args.sha)
        if run_id is not None:
            print(run_id)
    else:
        print("true" if reuse(_load_json(args.runs), _load_json(args.jobs), args.sha) else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
