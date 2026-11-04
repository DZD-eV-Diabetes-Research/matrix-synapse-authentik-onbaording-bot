"""Which integration stack instance this test run talks to, and how it is brought up.

Several stacks can run side by side on one machine (one per git worktree, for example). Each one is a
separate compose project with its own published host ports. Everything is derived from one variable:

* ``ONBOT_ITEST_INSTANCE`` (default ``0``): instance ``n`` uses the compose project ``onbot-itest``
  (``n == 0``) or ``onbot-itest-<n>``, and the host ports of instance 0 shifted by ``10 * n``
  (Synapse 8008, MAS 8080, Authentik 9000 for instance 0; 8018, 8090, 9010 for instance 1).

Single values can still be overridden one by one: ``ONBOT_ITEST_PROJECT``,
``ONBOT_ITEST_SYNAPSE_PORT``, ``ONBOT_ITEST_MAS_PORT``, ``ONBOT_ITEST_AUTHENTIK_PORT``.

The compose file reads the same variable names (with instance 0 values as defaults), and the harness
hands them over through an env file, so the containers and the host-side helpers always agree.
Only host ports change per instance; container ports and the in-network URLs (``mas:8080``,
``authentik-server:9000``, ``synapse:8008``) stay fixed, so the MAS, Synapse and Authentik configs do
not change either.

This module has no pytest dependency so the unit tier can test it (tests/unit/test_itest_harness.py).
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

INSTANCE_VAR = "ONBOT_ITEST_INSTANCE"
PROJECT_VAR = "ONBOT_ITEST_PROJECT"
SYNAPSE_PORT_VAR = "ONBOT_ITEST_SYNAPSE_PORT"
MAS_PORT_VAR = "ONBOT_ITEST_MAS_PORT"
AUTHENTIK_PORT_VAR = "ONBOT_ITEST_AUTHENTIK_PORT"
# Opt-out for machines without Docker: skip the integration tests instead of failing the run.
SKIP_VAR = "ONBOT_ITEST_SKIP"

DEFAULT_PROJECT = "onbot-itest"
DEFAULT_SYNAPSE_PORT = 8008
DEFAULT_MAS_PORT = 8080
DEFAULT_AUTHENTIK_PORT = 9000
PORT_STEP = 10


class StackConfigError(ValueError):
    """An ONBOT_ITEST_* variable has an unusable value."""


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise StackConfigError(f"{name}={raw!r} is not an integer") from None


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    port = _int(env, name, default)
    if not 1 <= port <= 65535:
        raise StackConfigError(f"{name}: port {port} is out of range")
    return port


@dataclass(frozen=True)
class StackInstance:
    instance: int
    project: str
    synapse_port: int
    mas_port: int
    authentik_port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StackInstance:
        instance = _int(env, INSTANCE_VAR, 0)
        if instance < 0:
            raise StackConfigError(f"{INSTANCE_VAR} must be >= 0, got {instance}")
        offset = instance * PORT_STEP
        default_project = DEFAULT_PROJECT if instance == 0 else f"{DEFAULT_PROJECT}-{instance}"
        stack = cls(
            instance=instance,
            project=env.get(PROJECT_VAR, "").strip() or default_project,
            synapse_port=_port(env, SYNAPSE_PORT_VAR, DEFAULT_SYNAPSE_PORT + offset),
            mas_port=_port(env, MAS_PORT_VAR, DEFAULT_MAS_PORT + offset),
            authentik_port=_port(env, AUTHENTIK_PORT_VAR, DEFAULT_AUTHENTIK_PORT + offset),
        )
        ports = [stack.synapse_port, stack.mas_port, stack.authentik_port]
        if len(set(ports)) != len(ports):
            raise StackConfigError(f"integration stack host ports collide: {ports}")
        return stack

    @property
    def synapse_url(self) -> str:
        return f"http://localhost:{self.synapse_port}"

    @property
    def mas_url(self) -> str:
        return f"http://localhost:{self.mas_port}"

    @property
    def authentik_url(self) -> str:
        return f"http://localhost:{self.authentik_port}"

    def compose_env(self) -> dict[str, str]:
        """The variables docker-compose.yml interpolates, for this instance."""
        return {
            PROJECT_VAR: self.project,
            SYNAPSE_PORT_VAR: str(self.synapse_port),
            MAS_PORT_VAR: str(self.mas_port),
            AUTHENTIK_PORT_VAR: str(self.authentik_port),
        }

    def write_env_file(self, path: Path) -> Path:
        path.write_text("".join(f"{key}={value}\n" for key, value in self.compose_env().items()))
        return path

    def describe(self) -> str:
        return (
            f"instance {self.instance}, compose project {self.project!r}, host ports "
            f"synapse={self.synapse_port} mas={self.mas_port} authentik={self.authentik_port}"
        )


class StackUnavailable(RuntimeError):
    """The integration stack could not be started. The run must fail, not skip (LIV-01)."""


class _Compose(Protocol):
    def start(self) -> None: ...

    def stop(self, down: bool = True) -> None: ...


def skip_requested(env: Mapping[str, str]) -> bool:
    return env.get(SKIP_VAR, "").strip() == "1"


def start_stack(
    compose: _Compose,
    stack: StackInstance,
    *,
    docker_available: Callable[[], bool] = lambda: shutil.which("docker") is not None,
) -> None:
    """Bring the stack up or raise :class:`StackUnavailable`.

    A first failed ``up`` is usually stale state of an earlier run of the same project (exited
    containers that point at a removed network, for example). So tear the project down once and
    retry; only a second failure is final.
    """
    if not docker_available():
        raise StackUnavailable(
            f"docker is not available, the integration suite needs the live stack ({stack.describe()}). "
            f"Run the fast tiers with -m 'not integration', or set {SKIP_VAR}=1 to skip explicitly."
        )
    try:
        compose.start()
        return
    except Exception as first:
        first_error = first
    with contextlib.suppress(Exception):  # the retry below reports what matters
        compose.stop(down=True)
    try:
        compose.start()
    except Exception as second:
        raise StackUnavailable(
            f"could not start the integration stack ({stack.describe()}).\n"
            f"First attempt: {_describe_error(first_error)}\n"
            f"After 'down' and a retry: {_describe_error(second)}"
        ) from second


def _describe_error(exc: BaseException) -> str:
    # testcontainers raises CalledProcessError; compose's own message is in its stderr.
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    detail = f"\n{stderr.strip()[-2000:]}" if stderr else ""
    return f"{exc}{detail}"
