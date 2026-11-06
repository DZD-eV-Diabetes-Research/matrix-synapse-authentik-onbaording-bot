"""Release and supply-chain tooling (review session S15, findings SUP-06, SUP-08, SUP-13 to SUP-17).

Covers the scripts the release workflow and the Dockerfile run (scripts/release_version.py,
scripts/lock_requirements.py), the packaging metadata, the integration stack's port bindings, and
the two shell scripts build_dev_env.sh and scripts/provision-bot-account.sh (against a fake
``docker`` and a fake ``pdm`` on PATH, so no Docker and no network are needed).
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


rv = _load("release_version")
lr = _load("lock_requirements")


# --------------------------------------------------------------------------- release_version (SUP-06)


@pytest.mark.parametrize(
    ("tag", "version", "prerelease"),
    [
        ("0.1.0", "0.1.0", False),
        ("0.1.0b3", "0.1.0b3", True),
        ("0.1.0rc1", "0.1.0rc1", True),
        ("0.1.0a2", "0.1.0a2", True),
        ("0.1.0.post1", "0.1.0.post1", False),
        # The shape the 0.0.1 betas were tagged with: published as its PEP 440 normal form, the same
        # string PyPI shows, instead of falling back to 0.0.0 in the image.
        ("0.0.1-beta.1", "0.0.1b1", True),
        ("v0.2.0", "0.2.0", False),
    ],
)
def test_tags_map_to_one_pep440_version(tag: str, version: str, prerelease: bool) -> None:
    release = rv.parse_release(tag)
    assert release.canonical == version
    assert release.prerelease is prerelease
    assert release.tag_is_canonical is (tag == version)


@pytest.mark.parametrize(
    ("tag", "reason"),
    [
        ("beta", "not a PEP 440 version"),
        ("", "empty"),
        ("0.1.0.dev0", "dev release"),
        ("0.1.0+build.5", "local version"),
        ("1!0.1.0", "epoch"),
        ("0.1", "three-part"),
        ("0.1.0.0", "three-part"),
    ],
)
def test_unpublishable_tags_are_rejected(tag: str, reason: str) -> None:
    with pytest.raises(rv.ReleaseVersionError, match=reason):
        rv.parse_release(tag)


def test_stable_release_gets_rolling_tags_and_latest() -> None:
    assert rv.parse_release("1.4.2").docker_tags("dzdde/onbot") == [
        "dzdde/onbot:1.4.2",
        "dzdde/onbot:1.4",
        "dzdde/onbot:1",
        "dzdde/onbot:latest",
    ]


def test_prerelease_gets_its_version_and_beta_only() -> None:
    assert rv.parse_release("1.5.0rc1").docker_tags("dzdde/onbot") == [
        "dzdde/onbot:1.5.0rc1",
        "dzdde/onbot:beta",
    ]


def test_prerelease_checkbox_must_agree_with_the_version() -> None:
    rv.check_prerelease_flag(rv.parse_release("0.2.0b1"), True)
    rv.check_prerelease_flag(rv.parse_release("0.2.0"), False)
    rv.check_prerelease_flag(rv.parse_release("0.2.0"), None)
    with pytest.raises(rv.ReleaseVersionError, match="not marked as pre-release"):
        rv.check_prerelease_flag(rv.parse_release("0.2.0b1"), False)
    with pytest.raises(rv.ReleaseVersionError, match="marked as pre-release"):
        rv.check_prerelease_flag(rv.parse_release("0.2.0"), True)


def test_verify_rejects_the_pdm_backend_fallback_version() -> None:
    rv.verify_installed("0.2.0b1", "0.2.0b1")
    rv.verify_installed("0.0.1-beta.1", "0.0.1b1")  # same version, different spelling
    with pytest.raises(rv.ReleaseVersionError, match="did not pick up"):
        rv.verify_installed("0.2.0b1", "0.0.0")


def test_derive_writes_github_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "github_output"
    assert rv.main(["derive", "0.0.1-beta.2", "--prerelease-flag", "true", "--github-output", str(out)]) == 0
    assert "published as the PEP 440 version '0.0.1b2'" in capsys.readouterr().out
    assert out.read_text() == (
        "version=0.0.1b2\n"
        "prerelease=true\n"
        "docker-tags<<EOF_DOCKER_TAGS\n"
        "dzdde/onbot:0.0.1b2\n"
        "dzdde/onbot:beta\n"
        "EOF_DOCKER_TAGS\n"
    )


def test_derive_fails_with_an_error_annotation(capsys: pytest.CaptureFixture[str]) -> None:
    assert rv.main(["derive", "0.1.0", "--prerelease-flag", "true"]) == 1
    assert capsys.readouterr().err.startswith("::error::")


def _write_dist(dist: Path, version: str, extra_member: str | None = None) -> None:
    dist.mkdir()
    (dist / f"onbot-{version}-py3-none-any.whl").write_bytes(b"wheel")
    with tarfile.open(dist / f"onbot-{version}.tar.gz", "w:gz") as archive:
        for member in [f"onbot-{version}/pyproject.toml", extra_member]:
            if member:
                info = tarfile.TarInfo(member)
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))


def test_check_dist_accepts_exactly_the_versioned_artifacts(tmp_path: Path) -> None:
    _write_dist(tmp_path / "dist", "0.3.0rc1")
    names = [p.name for p in rv.check_dist(rv.parse_release("0.3.0rc1"), tmp_path / "dist")]
    assert names == ["onbot-0.3.0rc1-py3-none-any.whl", "onbot-0.3.0rc1.tar.gz"]


def test_check_dist_rejects_the_fallback_version(tmp_path: Path) -> None:
    _write_dist(tmp_path / "dist", "0.0.0")
    with pytest.raises(rv.ReleaseVersionError, match="expected"):
        rv.check_dist(rv.parse_release("0.3.0"), tmp_path / "dist")


def test_check_dist_rejects_an_sdist_with_test_fixtures(tmp_path: Path) -> None:
    _write_dist(tmp_path / "dist", "0.3.0", "onbot-0.3.0/tests/integration/stack/mas/config.yaml")
    with pytest.raises(rv.ReleaseVersionError, match="test files"):
        rv.check_dist(rv.parse_release("0.3.0"), tmp_path / "dist")


# --------------------------------------------------------------------------- lock_requirements (SUP-08)


def _requirement_sets(text: str) -> dict[str, tuple[str, frozenset[str]]]:
    """{name: (version, hashes)} from a pip requirements file with --hash options."""
    result: dict[str, tuple[str, frozenset[str]]] = {}
    for block in text.replace("\\\n", " ").splitlines():
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        parts = block.split()
        name, version = parts[0].split(";")[0].split("==")
        name = name.split("[")[0].lower()
        result[name] = (
            version,
            frozenset(p.removeprefix("--hash=") for p in parts if p.startswith("--hash=")),
        )
    return result


def test_runtime_requirements_match_pdm_export() -> None:
    pdm = shutil.which("pdm")
    if pdm is None:  # pragma: no cover - pdm is how the suite is normally run
        pytest.skip("pdm is not on PATH")
    exported = subprocess.run(
        [pdm, "export", "--prod", "--no-extras", "-o", "/dev/stdout"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PDM_CHECK_UPDATE": "false"},
    ).stdout
    ours = "\n".join(lr.requirements(tomllib.loads((REPO / "pdm.lock").read_text()), "default"))
    assert _requirement_sets(ours) == _requirement_sets(exported)


def test_build_group_holds_the_locked_build_tools() -> None:
    lines = lr.requirements(tomllib.loads((REPO / "pdm.lock").read_text()), "build")
    assert [line.split("==")[0] for line in lines] == ["packaging", "pdm-backend"]
    assert all("--hash=sha256:" in line for line in lines)


def test_every_requirement_is_pinned_and_hashed() -> None:
    for line in lr.requirements(tomllib.loads((REPO / "pdm.lock").read_text()), "default"):
        head, *hashes = line.split(" \\\n")
        assert "==" in head
        assert hashes and all(h.strip().startswith("--hash=sha256:") for h in hashes)


def _lock(*packages: dict[str, object], groups: tuple[str, ...] = ("default",)) -> dict[str, object]:
    return {"metadata": {"groups": list(groups)}, "package": list(packages)}


def test_extras_entries_collapse_and_markers_are_kept() -> None:
    files = [{"file": "a.whl", "hash": "sha256:aa"}]
    lock = _lock(
        {"name": "md", "version": "1.0", "groups": ["default"], "files": files},
        {"name": "md", "version": "1.0", "extras": ["x"], "groups": ["default"], "files": files},
        {
            "name": "win",
            "version": "2.0",
            "groups": ["default"],
            "marker": 'sys_platform == "win32"',
            "files": files,
        },
        {"name": "devonly", "version": "3.0", "groups": ["dev"], "files": files},
    )
    assert lr.requirements(lock, "default") == [
        "md==1.0 \\\n    --hash=sha256:aa",
        'win==2.0 ; sys_platform == "win32" \\\n    --hash=sha256:aa',
    ]


@pytest.mark.parametrize(
    ("lock", "group", "message"),
    [
        (_lock(), "build", "not in the lockfile"),
        (_lock({"name": "a", "version": "1", "groups": ["default"], "files": []}), "default", "no hashes"),
        (
            _lock({"name": "a", "version": "1", "groups": ["default"], "files": [{"hash": "md5:00"}]}),
            "default",
            "non-sha256",
        ),
        (_lock(), "default", "no packages"),
    ],
)
def test_unusable_locks_fail(lock: dict[str, object], group: str, message: str) -> None:
    with pytest.raises(lr.LockError, match=message):
        lr.requirements(lock, group)


# --------------------------------------------------------------------------- packaging metadata


def _pyproject() -> dict[str, object]:
    return tomllib.loads((REPO / "pyproject.toml").read_text())


def test_every_runtime_dependency_has_an_upper_bound() -> None:
    """SUP-13: a breaking upstream major must not reach `pip install onbot` untested."""
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    uncapped = [dep for dep in project["dependencies"] if "<" not in dep]
    assert uncapped == []


def test_sdist_excludes_the_tests_and_their_fixture_secrets() -> None:
    """SUP-15: tests/integration/stack holds public test keys; they must not ship on PyPI."""
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    assert "tests" in tool["pdm"]["build"]["excludes"]


def test_dockerignore_is_an_allowlist() -> None:
    """SUP-17: only the files the image build reads are in the build context."""
    lines = [
        line.strip()
        for line in (REPO / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    admitted = {line[1:] for line in lines if line.startswith("!")}
    assert admitted == {
        "pyproject.toml",
        "pdm.lock",
        "README.md",
        "LICENSE",
        "scripts/lock_requirements.py",
        "scripts/release_version.py",
        "onbot/**/*.py",
    }


# --------------------------------------------------------------------------- integration stack (SUP-15)


def test_integration_stack_publishes_ports_on_loopback_only() -> None:
    compose = yaml.safe_load((REPO / "tests/integration/stack/docker-compose.yml").read_text())
    published = {
        name: service["ports"] for name, service in compose["services"].items() if "ports" in service
    }
    assert set(published) == {"authentik-server", "mas", "synapse"}
    for ports in published.values():
        for port in ports:
            assert port.startswith("127.0.0.1:"), port


# --------------------------------------------------------------------------- shell scripts


def _executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_build_dev_env_does_not_leak_strict_mode_into_the_caller(tmp_path: Path) -> None:
    """SUP-16: after sourcing, a failing command must not end the developer's shell."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "pdm", '#!/bin/sh\n[ "$1" = install ] && [ -n "$FAKE_PDM_FAIL" ] && exit 3\nexit 0\n'
    )
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(REPO / "build_dev_env.sh", project)
    script = (
        'source ./build_dev_env.sh >/dev/null; echo "rc=$? opts=$- $(set -o | grep -E "^pipefail")";'
        ' false; echo "alive"'
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    for fail in ("", "1"):
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=project,
            env={**env, "FAKE_PDM_FAIL": fail},
            capture_output=True,
            text=True,
            check=True,
        )
        opts_line, alive = result.stdout.strip().splitlines()[-2:]
        assert "e" not in opts_line.split("opts=")[1].split()[0]
        assert opts_line.endswith("off")
        assert alive == "alive"


def test_build_dev_env_refuses_to_run_unsourced() -> None:
    result = subprocess.run(["bash", str(REPO / "build_dev_env.sh")], capture_output=True, text=True)
    assert result.returncode == 1
    assert "must be sourced" in result.stdout


# Real MAS tokens carry an underscore before a checksum suffix; the parser must keep it.
FAKE_TOKEN = "mct_FAKEfake0123456789abcdefTOKEN_a1B2c3"

FAKE_DOCKER = f"""#!/bin/bash
# Fake `docker compose ...` for provision-bot-account.sh: logs argv and stdin, answers like the stack.
printf 'ARGV %s\\n' "$*" >> "$FAKE_LOG"
args="$*"
case "$args" in
  "compose config --services") printf 'mas\\nsynapse\\n' ;;
  *"mas-cli manage register-user"*) echo "User registered" ;;
  *"mas-cli manage kill-sessions"*) echo "killed 2 sessions" ;;
  *"mas-cli manage issue-compatibility-token"*)
    # FAKE_ISSUE_FAIL lists the issue calls (1-based) that fail, e.g. "1" or "2".
    n=$(( $(cat "$FAKE_LOG.issues" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$FAKE_LOG.issues"
    case ",${{FAKE_ISSUE_FAIL:-}}," in
      *",$n,"*) echo "Error: database unavailable" ;;
      *) echo "Compatibility token issued: {FAKE_TOKEN}" ;;
    esac ;;
  *"synapse sh -c"*) exit 0 ;;
  *"synapse curl"*)
    stdin="$(cat)"
    printf 'STDIN %s\\n' "$stdin" >> "$FAKE_LOG"
    case "$stdin" in
      *"Bearer {FAKE_TOKEN}"*) echo '{{"user_id":"@dzd-bot:example.org"}}' ;;
      *) exit 22 ;;
    esac ;;
  *) echo "unexpected: $args" >&2; exit 99 ;;
esac
"""


def _provision(
    tmp_path: Path, *args: str, issue_fail: str = ""
) -> tuple[subprocess.CompletedProcess[str], str]:
    stack = tmp_path / "stack"
    stack.mkdir(exist_ok=True)
    (stack / "docker-compose.yml").write_text("services: {}\n")
    # A .env with a command substitution: it must be read as data, never executed.
    (stack / ".env").write_text(
        'MATRIX_SERVER_NAME="example.org"\nSYNAPSE_DOMAIN=matrix.example.org\n'
        f"EVIL=$(touch {tmp_path}/pwned)\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _executable(bin_dir / "docker", FAKE_DOCKER)
    log = tmp_path / "docker.log"
    result = subprocess.run(
        ["bash", str(REPO / "scripts/provision-bot-account.sh"), "-m", str(stack), "-y", *args],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "FAKE_LOG": str(log),
            "HOME": str(tmp_path),
            "FAKE_ISSUE_FAIL": issue_fail,
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return result, log.read_text() if log.exists() else ""


def test_provision_never_puts_the_token_on_a_command_line(tmp_path: Path) -> None:
    """SUP-14: the token reaches curl on stdin; no argv of any process carries it."""
    result, log = _provision(tmp_path, "--write-token-to", str(tmp_path / "token"))
    assert result.returncode == 0, result.stderr
    argv_lines = [line for line in log.splitlines() if line.startswith("ARGV ")]
    assert any("synapse curl" in line and "-H @-" in line for line in argv_lines)
    assert not any(FAKE_TOKEN in line for line in argv_lines)
    assert f"STDIN Authorization: Bearer {FAKE_TOKEN}" in log
    assert "verified: token authenticates as @dzd-bot:example.org" in result.stderr
    assert not (tmp_path / "pwned").exists(), ".env was executed"


def test_provision_masks_the_token_and_writes_a_private_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("old\n")
    token_file.chmod(0o644)  # an existing, world-readable file is replaced, not reused
    result, _ = _provision(tmp_path, "--write-token-to", str(token_file))
    assert result.returncode == 0, result.stderr
    assert FAKE_TOKEN not in result.stderr + result.stdout
    assert f"<{FAKE_TOKEN[:8]}...({len(FAKE_TOKEN)} chars)>" in result.stderr
    assert token_file.read_text() == f"{FAKE_TOKEN}\n"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_provision_quiet_prints_only_the_token(tmp_path: Path) -> None:
    result, _ = _provision(tmp_path, "-q")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{FAKE_TOKEN}\n"
    assert FAKE_TOKEN not in result.stderr


def test_provision_needs_somewhere_to_put_the_token(tmp_path: Path) -> None:
    result, log = _provision(tmp_path)
    assert result.returncode == 1
    assert "nowhere to put the token" in result.stderr
    assert "issue-compatibility-token" not in log  # nothing minted


def _mas_commands(log: str) -> list[str]:
    return [line for line in log.splitlines() if line.startswith("ARGV ") and "mas-cli" in line]


def test_provision_revokes_previous_sessions_after_a_probe_and_before_the_final_token(
    tmp_path: Path,
) -> None:
    """kill-sessions ends every session, so: probe token, end all sessions, then the final token."""
    result, log = _provision(tmp_path, "--reissue-only", "--revoke-previous", "-q")
    assert result.returncode == 0, result.stderr
    commands = _mas_commands(log)
    assert len(commands) == 3
    assert "issue-compatibility-token" in commands[0]  # the probe
    assert "kill-sessions dzd-bot" in commands[1]
    assert "issue-compatibility-token" in commands[2]  # the token that is handed out
    assert result.stdout == f"{FAKE_TOKEN}\n"
    assert FAKE_TOKEN not in result.stderr


def test_provision_ends_no_session_when_the_probe_token_fails(tmp_path: Path) -> None:
    result, log = _provision(tmp_path, "--reissue-only", "--revoke-previous", "-q", issue_fail="1")
    assert result.returncode != 0
    assert "kill-sessions" not in log
    assert len(_mas_commands(log)) == 1
    assert "could not issue a probe token" in result.stderr
    assert "no session was ended" in result.stderr
    assert result.stdout == ""


def test_provision_says_clearly_when_the_final_token_fails_after_the_revoke(tmp_path: Path) -> None:
    result, log = _provision(tmp_path, "--reissue-only", "--revoke-previous", "-q", issue_fail="2")
    assert result.returncode != 0
    commands = _mas_commands(log)
    assert "kill-sessions dzd-bot" in commands[1]
    assert "ALL sessions of 'dzd-bot' were already ended" in result.stderr
    assert "without --revoke-previous" in result.stderr
    assert result.stdout == ""
    assert FAKE_TOKEN not in result.stderr  # the probe token is never printed


def test_provision_without_revoke_issues_a_single_token(tmp_path: Path) -> None:
    result, log = _provision(tmp_path, "--reissue-only", "-q")
    assert result.returncode == 0, result.stderr
    commands = _mas_commands(log)
    assert len(commands) == 1 and "issue-compatibility-token" in commands[0]


def test_provision_keeps_previous_sessions_by_default_without_a_terminal(tmp_path: Path) -> None:
    result, log = _provision(tmp_path, "-q")
    assert result.returncode == 0, result.stderr
    assert "kill-sessions" not in log
