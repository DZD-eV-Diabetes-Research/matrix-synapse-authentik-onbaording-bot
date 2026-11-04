"""Liveness heartbeat of the running service, and the check that reads it (RUN-04, SUP-09).

The service runs three loops that each catch their own errors and carry on, so the process never
dies of a wedged loop and a dependency probe from outside cannot see one. The loops therefore report
here instead: the reconcile loop after every pass, the sync pump after every sync response. Each
report rewrites one small JSON file, and ``onbot healthcheck`` (the container's ``HEALTHCHECK``)
judges the bot by the age of those timestamps. That check makes no network call and mints no token.

The file also carries the thresholds the check applies. The service derives them from its own config
(the reconcile interval), so the check needs no config at all and cannot disagree with the service
about what "too old" means.

Only ``onbot run`` writes the file. A one-shot command run next to the service (``docker exec ...
onbot reconcile-once``) must not overwrite the service's heartbeat with its own.

File format (``schema_version`` 1)::

    {
      "schema_version": 1,
      "pid": 1,
      "started_at": 1767225600.0,
      "updated_at": 1767225660.0,
      "max_age_sec": {"reconcile": 1200, "sync": 300},
      "components": {
        "reconcile": {"last_ok_at": 1767225650.0, "last_error_at": null, "consecutive_failures": 0},
        "sync": {"last_ok_at": 1767225659.0, "last_error_at": null, "consecutive_failures": 0},
        "sync_disabled": "homeserver does not support sliding sync"
      },
      "startup_pending": ["admin control room"]
    }
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onbot.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1

HEARTBEAT_FILE_ENV_VAR = "ONBOT_HEARTBEAT_FILE"
# /tmp is writable for the image's non-root user. A read-only root filesystem needs a tmpfs there, or
# the variable above pointing at another writable path (see docs/deployment.md).
DEFAULT_HEARTBEAT_FILE = "/tmp/onbot-heartbeat.json"

RECONCILE = "reconcile"
SYNC = "sync"

# A sync response arrives at least every long-poll timeout (30 s) while the pump is healthy, and the
# pump's error backoff is capped at 60 s, so five minutes without one is a stuck or failing pump.
SYNC_MAX_AGE_SEC = 300
# Headroom on top of the reconcile interval for one slow pass (a large directory, a slow homeserver).
RECONCILE_SLACK_SEC = 300


def reconcile_max_age_sec(tick_sec: float) -> int:
    """How long the last successful reconcile may lie back: three missed ticks plus one slow pass."""
    return int(3 * tick_sec + RECONCILE_SLACK_SEC)


def resolve_heartbeat_path(explicit: str | None = None) -> Path:
    """The heartbeat file: the explicit path, else ``ONBOT_HEARTBEAT_FILE``, else the default."""
    return Path(explicit or os.environ.get(HEARTBEAT_FILE_ENV_VAR) or DEFAULT_HEARTBEAT_FILE)


class Heartbeat:
    """Collects the loops' reports and mirrors them into the heartbeat file.

    Writing is best-effort: a file that cannot be written is logged (once) and never takes a loop
    down. The healthcheck then reports the missing or stale file, which is the right symptom.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        max_age_sec: dict[str, int],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._clock = clock
        self._write_failed = False
        now = clock()
        self._state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "pid": os.getpid(),
            "started_at": now,
            "updated_at": now,
            "max_age_sec": dict(max_age_sec),
            "components": {},
            "startup_pending": [],
        }

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    def start(self) -> None:
        """Write the initial file, replacing whatever an earlier process left behind."""
        self._write()

    def ok(self, component: str) -> None:
        entry = self._component(component)
        entry["last_ok_at"] = self._clock()
        entry["consecutive_failures"] = 0
        self._write()

    def failed(self, component: str) -> None:
        entry = self._component(component)
        entry["last_error_at"] = self._clock()
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
        self._write()

    def disable(self, component: str, reason: str) -> None:
        """Take a component out of the check, e.g. the sync pump on a server without sliding sync."""
        self._state["components"][f"{component}_disabled"] = reason
        self._write()

    def set_startup_pending(self, steps: list[str]) -> None:
        self._state["startup_pending"] = sorted(steps)
        self._write()

    def _component(self, component: str) -> dict[str, Any]:
        entries: dict[str, dict[str, Any]] = self._state["components"]
        return entries.setdefault(
            component, {"last_ok_at": None, "last_error_at": None, "consecutive_failures": 0}
        )

    def _write(self) -> None:
        self._state["updated_at"] = self._clock()
        if self.path is None:
            return
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            # Atomic on POSIX: the healthcheck never reads a half-written file.
            tmp.replace(self.path)
        except OSError:
            if not self._write_failed:
                log.warning(
                    "cannot write the heartbeat file %s; `onbot healthcheck` will report the bot unhealthy "
                    "(set %s to a writable path)",
                    self.path,
                    HEARTBEAT_FILE_ENV_VAR,
                    exc_info=True,
                )
            self._write_failed = True
            return
        if self._write_failed:
            log.info("heartbeat file %s is writable again", self.path)
            self._write_failed = False


# --- the reading side: `onbot healthcheck` ----------------------------------------------------


@dataclass(slots=True)
class HeartbeatVerdict:
    """Outcome of judging a heartbeat file: healthy or not, and one line per finding."""

    healthy: bool
    lines: list[str] = field(default_factory=list)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, but belongs to somebody else
        return True
    return True


def evaluate_heartbeat(
    data: dict[str, Any],
    *,
    now: float,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> HeartbeatVerdict:
    """Judge a parsed heartbeat file. Pure apart from the injected ``pid_alive`` probe."""
    verdict = HeartbeatVerdict(healthy=True)

    def fail(line: str) -> None:
        verdict.healthy = False
        verdict.lines.append(f"FAIL {line}")

    if data.get("schema_version") != SCHEMA_VERSION:
        fail(f"unknown heartbeat schema_version {data.get('schema_version')!r}")
        return verdict

    pid = data.get("pid")
    if isinstance(pid, int) and not pid_alive(pid):
        fail(f"the process that wrote the heartbeat (pid {pid}) is gone")

    started_at = float(data.get("started_at") or 0)
    components: dict[str, Any] = data.get("components") or {}
    limits: dict[str, Any] = data.get("max_age_sec") or {}
    for name in (RECONCILE, SYNC):
        disabled = components.get(f"{name}_disabled")
        if disabled:
            verdict.lines.append(f"OK   {name}: not checked ({disabled})")
            continue
        limit = limits.get(name)
        if limit is None:
            continue
        entry = components.get(name) or {}
        last_ok = entry.get("last_ok_at")
        # Before the first success the clock runs from process start, so a slow first pass is not a
        # failure but a pass that never comes is.
        reference = float(last_ok) if last_ok is not None else started_at
        age = now - reference
        failures = int(entry.get("consecutive_failures") or 0)
        since = "last success" if last_ok is not None else "start, no success yet"
        detail = f"{age:.0f}s since {since} (limit {limit}s, {failures} consecutive failures)"
        if age > float(limit):
            fail(f"{name}: {detail}")
        else:
            verdict.lines.append(f"OK   {name}: {detail}")

    pending = data.get("startup_pending") or []
    if pending:
        # Informational: these steps retry in the background and heal by themselves.
        verdict.lines.append(f"INFO startup steps still retrying: {', '.join(pending)}")
    return verdict


def check_heartbeat_file(path: Path, *, now: float | None = None) -> HeartbeatVerdict:
    """Read and judge the heartbeat file at ``path``; a missing or unreadable file is unhealthy."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HeartbeatVerdict(False, [f"FAIL no heartbeat file at {path}; is `onbot run` running?"])
    except (OSError, ValueError) as exc:
        return HeartbeatVerdict(False, [f"FAIL cannot read heartbeat file {path}: {exc}"])
    if not isinstance(data, dict):
        return HeartbeatVerdict(False, [f"FAIL heartbeat file {path} is not a JSON object"])
    return evaluate_heartbeat(data, now=time.time() if now is None else now)
