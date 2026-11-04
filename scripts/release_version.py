"""The one place that turns a release tag into the version of every published artifact.

The release workflow derives the version here once and hands the result to both the wheel/sdist
build and the Docker build (``PDM_BUILD_SCM_VERSION``), so PyPI and Docker Hub always carry the same
version (review finding SUP-06). The Docker build calls ``verify`` after installing the package, and
the release workflow calls ``check-dist`` after ``pdm build``, so a version that pdm-backend silently
replaced with its fallback fails the release instead of shipping as ``0.0.0``.

Accepted tags are PEP 440 versions with a three-part release, optionally a pre-release (a, b, rc)
and/or a post-release: ``0.1.0``, ``0.1.0b3``, ``0.1.0rc1``, ``0.1.0.post1``. Non-canonical
spellings that PEP 440 normalises are accepted with a notice (``0.0.1-beta.1`` becomes
``0.0.1b1``, ``v0.1.0`` becomes ``0.1.0``); the published version is always the normalised one.
Rejected: dev releases (``0.1.0.dev0`` once leaked to PyPI and sorts above every stable release),
local versions (``+abc``, PyPI refuses them), epochs, and anything that is not PEP 440.

Usage (dry run, prints what a release of that tag would publish):

    python scripts/release_version.py derive 0.1.0b3
    python scripts/release_version.py derive 0.1.0 --prerelease-flag false

Needs ``packaging`` (dependency group ``build`` in pyproject.toml).
"""

from __future__ import annotations

import argparse
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

PACKAGE = "onbot"
DEFAULT_IMAGE = "dzdde/onbot"
# The rolling Docker tag of the pre-release channel; stable releases get "latest" instead.
PRERELEASE_DOCKER_TAG = "beta"


class ReleaseVersionError(ValueError):
    """The tag cannot be released."""


@dataclass(frozen=True)
class Release:
    tag: str
    version: Version

    @property
    def canonical(self) -> str:
        return str(self.version)

    @property
    def prerelease(self) -> bool:
        return self.version.is_prerelease

    @property
    def tag_is_canonical(self) -> bool:
        return self.tag == self.canonical

    def docker_tags(self, image: str = DEFAULT_IMAGE) -> list[str]:
        """Image tags to push: the version itself plus the rolling channel tags."""
        if self.prerelease:
            names = [self.canonical, PRERELEASE_DOCKER_TAG]
        else:
            major, minor, _ = self.version.release
            names = [self.canonical, f"{major}.{minor}", f"{major}", "latest"]
        return [f"{image}:{name}" for name in names]


def parse_release(tag: str) -> Release:
    tag = tag.strip()
    if not tag:
        raise ReleaseVersionError("the release tag is empty")
    try:
        version = Version(tag)
    except InvalidVersion:
        raise ReleaseVersionError(
            f"tag {tag!r} is not a PEP 440 version; use e.g. 0.1.0, 0.1.0b1 or 0.1.0rc1"
        ) from None
    if version.local is not None:
        raise ReleaseVersionError(f"tag {tag!r} has a local version part ('+...'); PyPI rejects those")
    if version.epoch != 0:
        raise ReleaseVersionError(f"tag {tag!r} has an epoch ('N!'); not supported for releases")
    if version.is_devrelease:
        raise ReleaseVersionError(
            f"tag {tag!r} is a dev release; dev releases sort above every stable release on PyPI "
            "and must not be published. Use a pre-release (a, b or rc) instead"
        )
    if len(version.release) != 3:
        raise ReleaseVersionError(f"tag {tag!r} needs a three-part release number (X.Y.Z)")
    return Release(tag=tag, version=version)


def check_prerelease_flag(release: Release, flag: bool | None) -> None:
    """The GitHub release's "pre-release" checkbox must agree with the version."""
    if flag is None or flag == release.prerelease:
        return
    if release.prerelease:
        raise ReleaseVersionError(
            f"{release.canonical} is a pre-release version, but the GitHub release is not marked as "
            "pre-release. Tick 'Set as a pre-release' or use a stable version tag"
        )
    raise ReleaseVersionError(
        f"{release.canonical} is a stable version, but the GitHub release is marked as pre-release. "
        "Untick 'Set as a pre-release' or use a pre-release tag such as "
        f"{release.canonical}b1"
    )


def verify_installed(expected: str, actual: str) -> None:
    """The version pdm-backend built must be the one we asked for (not its 0.0.0 fallback)."""
    want = parse_release(expected).version
    try:
        got = Version(actual)
    except InvalidVersion:
        raise ReleaseVersionError(f"the built package reports an invalid version {actual!r}") from None
    if got != want:
        raise ReleaseVersionError(
            f"the built package has version {actual}, expected {want}; "
            "pdm-backend did not pick up PDM_BUILD_SCM_VERSION"
        )


def check_dist(release: Release, dist_dir: Path) -> list[Path]:
    """``dist_dir`` holds exactly this version's sdist and wheel, and the sdist ships no tests."""
    expected = {
        f"{PACKAGE}-{release.canonical}.tar.gz",
        f"{PACKAGE}-{release.canonical}-py3-none-any.whl",
    }
    found = {path.name for path in dist_dir.iterdir() if path.is_file()}
    if found != expected:
        raise ReleaseVersionError(f"{dist_dir} holds {sorted(found)}, expected {sorted(expected)}")
    sdist = dist_dir / f"{PACKAGE}-{release.canonical}.tar.gz"
    with tarfile.open(sdist) as archive:
        root = f"{PACKAGE}-{release.canonical}/"
        leaked = [name for name in archive.getnames() if name.startswith(f"{root}tests")]
    if leaked:
        raise ReleaseVersionError(f"the sdist contains test files (fixtures with secrets): {leaked[:5]}")
    return sorted(dist_dir / name for name in expected)


def _bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {value!r}")


def _github_output(path: Path, release: Release, tags: list[str]) -> None:
    delimiter = "EOF_DOCKER_TAGS"
    with path.open("a", encoding="utf-8") as out:
        out.write(f"version={release.canonical}\n")
        out.write(f"prerelease={str(release.prerelease).lower()}\n")
        out.write(f"docker-tags<<{delimiter}\n" + "\n".join(tags) + f"\n{delimiter}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Derive and check the release version.")
    sub = parser.add_subparsers(dest="command", required=True)

    derive = sub.add_parser("derive", help="validate a tag and print the version and Docker tags")
    derive.add_argument("tag")
    derive.add_argument(
        "--prerelease-flag",
        type=_bool,
        default=None,
        help="the GitHub release's pre-release checkbox; must agree with the version",
    )
    derive.add_argument("--image", default=DEFAULT_IMAGE)
    derive.add_argument("--github-output", type=Path, help="append step outputs to this file")

    verify = sub.add_parser("verify", help="compare an installed version with the requested one")
    verify.add_argument("--expected", required=True)
    verify.add_argument("--actual", required=True)

    dist = sub.add_parser("check-dist", help="check the built sdist and wheel")
    dist.add_argument("version")
    dist.add_argument("dist_dir", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "derive":
            release = parse_release(args.tag)
            check_prerelease_flag(release, args.prerelease_flag)
            tags = release.docker_tags(args.image)
            if not release.tag_is_canonical:
                print(
                    f"::notice::tag {release.tag!r} is published as the PEP 440 version "
                    f"{release.canonical!r} (PyPI and Docker)"
                )
            print(f"version:    {release.canonical}")
            print(f"channel:    {'pre-release' if release.prerelease else 'stable'}")
            print("pypi:       " + f"{PACKAGE}=={release.canonical}")
            print("docker:     " + " ".join(tags))
            if args.github_output:
                _github_output(args.github_output, release, tags)
        elif args.command == "verify":
            verify_installed(args.expected, args.actual)
            print(f"version ok: {args.actual}")
        else:
            for path in check_dist(parse_release(args.version), args.dist_dir):
                print(f"dist ok:    {path.name}")
    except ReleaseVersionError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
