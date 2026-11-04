"""Turn one dependency group of ``pdm.lock`` into a hash-pinned pip requirements file.

The Docker build uses this instead of ``pdm export`` so that it never installs PDM (and the
roughly 40 unpinned packages PDM pulls in) just to read the lockfile. It needs only the standard
library, so it runs on the bare base image:

    python scripts/lock_requirements.py default > runtime.txt
    pip install --require-hashes --no-deps --only-binary=:all: -r runtime.txt

Every requirement is ``name==version`` with all sha256 hashes the lock records, so pip verifies
each downloaded file. The lock already lists the full transitive set of the group (including the
packages that extras pull in), which is why the install runs with ``--no-deps`` and the extras
themselves are not repeated here. A unit test checks that the result names the same packages,
versions and hashes as ``pdm export`` (tests/unit/test_release_tooling.py).
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

DEFAULT_LOCK = Path(__file__).resolve().parents[1] / "pdm.lock"


class LockError(ValueError):
    """The lockfile cannot be turned into a requirements file."""


def requirements(lock: dict[str, object], group: str) -> list[str]:
    """The requirement lines (with ``--hash`` options) for ``group``, sorted by package name."""
    metadata = lock.get("metadata")
    groups = metadata.get("groups", []) if isinstance(metadata, dict) else []
    if group not in groups:
        raise LockError(f"group {group!r} is not in the lockfile (it has {groups}); run `pdm lock`")

    packages = lock.get("package", [])
    if not isinstance(packages, list):
        raise LockError("the lockfile has no [[package]] table")

    # The lock repeats a package once per extra (markdown-it-py and markdown-it-py[linkify]); the
    # files and version are the same, so one line per name is enough.
    lines: dict[str, str] = {}
    for package in packages:
        if group not in package.get("groups", []):
            continue
        name, version = package["name"], package["version"]
        hashes = sorted({entry["hash"] for entry in package.get("files", [])})
        if not hashes:
            raise LockError(f"{name} {version} has no hashes in the lockfile")
        if any(not h.startswith("sha256:") for h in hashes):
            raise LockError(f"{name} {version} has a non-sha256 hash in the lockfile")
        marker = package.get("marker")
        requirement = f"{name}=={version}" + (f" ; {marker}" if marker else "")
        line = " \\\n".join([requirement, *(f"    --hash={h}" for h in hashes)])
        previous = lines.setdefault(name, line)
        if previous != line:
            raise LockError(f"{name} is locked twice with different versions, markers or files")
    if not lines:
        raise LockError(f"group {group!r} has no packages")
    return [lines[name] for name in sorted(lines)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a hash-pinned requirements file from pdm.lock.")
    parser.add_argument("group", help="dependency group, e.g. 'default' (runtime) or 'build'")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK, help="path to pdm.lock")
    args = parser.parse_args(argv)
    try:
        lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))
        lines = requirements(lock, args.group)
    except (OSError, tomllib.TOMLDecodeError, LockError) as exc:
        print(f"lock_requirements: {exc}", file=sys.stderr)
        return 1
    print(f"# Generated from {args.lock.name}, group {args.group!r}, by scripts/lock_requirements.py.")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
