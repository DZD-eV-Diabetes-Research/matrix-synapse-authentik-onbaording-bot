"""Process lifecycle primitives: signal handling, bounded shutdown, retried startup steps.

Used by the composition root (:mod:`onbot.app`) and kept apart from its wiring so each can be read on
its own. Review findings RUN-02 and RUN-09.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from onbot.clients.base import ApiError
from onbot.heartbeat import Heartbeat
from onbot.logging import get_logger
from onbot.safe_fetch import MediaFetchRejected

log = get_logger(__name__)

# How long running work may take to finish after SIGTERM before it is cancelled. Docker's default
# stop timeout is 10 s; this leaves room for cancellation and closing the HTTP clients.
SHUTDOWN_GRACE_SEC = 6.0
# How long to wait for cancelled tasks to unwind before giving up on them.
CANCEL_TIMEOUT_SEC = 2.0

STARTUP_INITIAL_BACKOFF_SEC = 1.0
STARTUP_MAX_BACKOFF_SEC = 60.0

# 4xx answers that do not mean "never": not authenticated yet (MAS still booting), timeouts, rate
# limits. Any other 4xx from the homeserver is a refusal for good, but only when it carries a Matrix
# ``errcode``: a reverse proxy answers 403, 404 or 421 with an HTML page while the homeserver behind it
# is still booting, and giving up then would open onboarding without a device (RUN-02).
_TRANSIENT_CLIENT_ERRORS = frozenset({401, 408, 425, 429})


# --- signals ----------------------------------------------------------------------------------


class Shutdown:
    """SIGTERM/SIGINT as two events: stop requested, and (on a second signal) stop *now*."""

    SIGNALS = (signal.SIGTERM, signal.SIGINT)

    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.forced = asyncio.Event()
        self.signum: int | None = None

    def request(self, signum: int = signal.SIGTERM) -> None:
        if self.requested.is_set():
            if not self.forced.is_set():
                log.warning("received %s again; cancelling everything now", signal.Signals(signum).name)
            self.forced.set()
            return
        self.signum = signum
        log.info("received %s; shutting down", signal.Signals(signum).name)
        self.requested.set()

    @property
    def exit_code(self) -> int:
        """The shell convention for a process ended by the signal (143 for SIGTERM)."""
        return 128 + self.signum if self.signum is not None else 0

    @contextlib.contextmanager
    def installed(self) -> Iterator[Shutdown]:
        """Route the signals to :meth:`request` for the lifetime of the block (main thread only)."""
        loop = asyncio.get_running_loop()
        installed: list[tuple[int, Any]] = []
        for sig in self.SIGNALS:
            previous = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, self.request, sig)
            except NotImplementedError, RuntimeError, ValueError:  # pragma: no cover - not main thread
                log.debug("cannot install a handler for %s here", sig)
                continue
            installed.append((sig, previous))
        try:
            yield self
        finally:
            for signum, previous in installed:
                loop.remove_signal_handler(signum)
                if previous is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        signal.signal(signum, previous)


async def drain(tasks: set[Any], *, grace: float, forced: asyncio.Event) -> set[Any]:
    """Wait up to ``grace`` seconds (less if ``forced`` fires) for ``tasks``; return the unfinished."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace
    pending = {t for t in tasks if not t.done()}
    force_wait = asyncio.ensure_future(forced.wait())
    try:
        while pending and not forced.is_set():
            timeout = deadline - loop.time()
            if timeout <= 0:
                break
            await asyncio.wait({*pending, force_wait}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            pending = {t for t in pending if not t.done()}
    finally:
        force_wait.cancel()
    return pending


async def cancel(tasks: set[Any]) -> None:
    """Cancel ``tasks`` and wait, bounded, for them to unwind."""
    for task in tasks:
        task.cancel()
    if tasks:
        _done, stuck = await asyncio.wait(tasks, timeout=CANCEL_TIMEOUT_SEC)
        for task in stuck:  # pragma: no cover - only a task that swallows cancellation
            log.error("task %s did not stop after cancellation", task)


async def run_interruptible(
    work: Awaitable[int | None],
    *,
    shutdown: Shutdown | None = None,
    grace: float = SHUTDOWN_GRACE_SEC,
) -> int:
    """Run a one-shot command with signal handling; return its exit code.

    A signal lets the command finish within ``grace`` (a half-sent broadcast is worse than a
    slightly later exit); after that, or on a second signal, it is cancelled and the exit code is
    ``128 + signal``.
    """
    shutdown = shutdown or Shutdown()
    with shutdown.installed():
        task = asyncio.ensure_future(work)
        stop = asyncio.ensure_future(shutdown.requested.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                log.warning("waiting up to %.0fs for the command to finish", grace)
                if await drain({task}, grace=grace, forced=shutdown.forced):
                    await cancel({task})
                    log.warning("command interrupted")
                    return shutdown.exit_code
        finally:
            stop.cancel()
        result = task.result()
        return 0 if result is None else int(result)


# --- startup steps ----------------------------------------------------------------------------


def is_permanent_failure(exc: BaseException) -> bool:
    """Whether a startup step failed because the server refused it for good (so: stop retrying).

    A client error from one of onbot's API clients is permanent only when the body is a Matrix error
    (it has an ``errcode``), so the homeserver itself answered. A bare 4xx is most likely a proxy in
    front of a server that is not up yet and is retried.

    An avatar the fetch policy refuses (internal address, not an image, too large) is permanent too:
    the same URL is refused the same way on every retry, and so is a 4xx from the avatar's own host
    (``httpx.HTTPStatusError``, raised only by the avatar fetcher).
    """
    if isinstance(exc, MediaFetchRejected):
        return True
    if isinstance(exc, ApiError):
        return _is_final_client_error(exc.status_code) and _matrix_errcode(exc.payload) is not None
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_final_client_error(exc.response.status_code)
    return False


def _is_final_client_error(status: int) -> bool:
    return 400 <= status < 500 and status not in _TRANSIENT_CLIENT_ERRORS


def _matrix_errcode(payload: Any) -> str | None:
    """The Matrix ``errcode`` of an error body, or ``None`` when it is not a Matrix error."""
    if isinstance(payload, dict):
        errcode = payload.get("errcode")
        if isinstance(errcode, str) and errcode:
            return errcode
    return None


@dataclass(slots=True, eq=False)
class StartupStep:
    """One piece of startup work against the homeserver, retried until it is done."""

    name: str
    action: Callable[[], Awaitable[None]]
    # Wait for this before the first attempt (e.g. the admin room needs a registered device).
    after: StartupStep | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    succeeded: bool = False


class StartupSupervisor:
    """Run startup steps without blocking the service, retrying each until it succeeds (RUN-02).

    A step that raises is retried with an exponential backoff (capped), except when the server
    refused it for good (:func:`is_permanent_failure`): that is logged as an error and the step is
    considered done, which is how every step behaved before it was retried at all.
    """

    def __init__(
        self,
        steps: list[StartupStep],
        *,
        heartbeat: Heartbeat | None = None,
        initial_backoff_sec: float = STARTUP_INITIAL_BACKOFF_SEC,
        max_backoff_sec: float = STARTUP_MAX_BACKOFF_SEC,
    ) -> None:
        self.steps = steps
        self._heartbeat = heartbeat
        self._initial = initial_backoff_sec
        self._max = max_backoff_sec
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def pending(self) -> list[str]:
        return [s.name for s in self.steps if not s.done.is_set()]

    async def run_once(self) -> list[str]:
        """One attempt per step, in order, for one-shot commands. Returns the names that failed."""
        failed: list[str] = []
        for step in self.steps:
            try:
                await step.action()
                step.succeeded = True
            except Exception:
                log.warning("startup step %r failed; continuing without it", step.name, exc_info=True)
                failed.append(step.name)
            step.done.set()
        return failed

    async def run(self) -> None:
        """Drive every step to completion concurrently, until done or stopped."""
        self._report()
        await asyncio.gather(*(self._drive(step) for step in self.steps))
        if not self._stop.is_set():
            log.info("startup complete")

    async def _drive(self, step: StartupStep) -> None:
        if step.after is not None and not await self._wait(step.after.done):
            return
        attempt = 0
        while not self._stop.is_set():
            try:
                await step.action()
            except Exception as exc:
                if self._stop.is_set():
                    return  # failed because we are shutting down; nothing to retry or report
                if is_permanent_failure(exc):
                    log.error(
                        "startup step %r refused by the server; continuing without it",
                        step.name,
                        exc_info=exc,
                    )
                    break
                attempt += 1
                delay = min(self._initial * 2 ** (attempt - 1), self._max)
                if attempt == 1:
                    log.warning("startup step %r failed; retrying in the background", step.name, exc_info=exc)
                else:
                    log.warning(
                        "startup step %r still failing (attempt %d: %s); next try in %.0fs",
                        step.name,
                        attempt,
                        exc,
                        delay,
                    )
                if not await self._sleep(delay):
                    return
                continue
            step.succeeded = True
            if attempt:
                log.info("startup step %r succeeded after %d failed attempt(s)", step.name, attempt)
            break
        step.done.set()
        self._report()

    def _report(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.set_startup_pending(self.pending)

    async def _wait(self, event: asyncio.Event) -> bool:
        """Wait for ``event``; ``False`` if a stop came first."""
        waiter = asyncio.ensure_future(event.wait())
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({waiter, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            stop.cancel()
        return event.is_set() and not self._stop.is_set()

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, waking on stop; ``False`` if stopped."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        return not self._stop.is_set()
