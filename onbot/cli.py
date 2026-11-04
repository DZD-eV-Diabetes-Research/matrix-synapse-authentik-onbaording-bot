"""Command-line entry point: ``python -m onbot`` / ``onbot``.

Sub-commands mirror the operational surface from ``BATTLE_PLAN.md`` §4:

* ``run``             — long-running service: scheduled reconcile + (Phase 4) onboarding
* ``reconcile-once``  — run a single idempotent reconcile and exit
* ``broadcast``       — send one announcement to every user's notice board (G4.6)
* ``generate-config`` — emit a documented example config (G11.2)
* ``healthcheck``     : container liveness from the service's heartbeat file; ``--deep`` probes the
  dependencies with the real credentials instead (Phase 8, RUN-04)

``broadcast`` carries no authorisation check of its own, deliberately: anyone who can run this
command can already read the bot's access token out of its config or environment.

Signals: SIGTERM and SIGINT are handled from the first line of :func:`main`, also when the process is
PID 1, where the kernel drops signals nobody handles (RUN-09, SUP-10). Until the event loop takes over
they raise :class:`SystemExit` with the shell's ``128 + signal`` code; inside the loop,
:mod:`onbot.runtime` turns them into a graceful shutdown. The interpreter's own startup comes before
:func:`main`, which is why the image still runs ``tini`` as PID 1.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from collections.abc import Sequence
from types import FrameType
from typing import Any

from onbot import __version__
from onbot.config import generate_example_config, get_config_file_path, load_config
from onbot.logging import configure_logging, get_logger

log = get_logger(__name__)


_LOG_LEVEL_HELP = "Override log level (e.g. DEBUG, INFO)."
_HEARTBEAT_HELP = "Heartbeat file path (default: $ONBOT_HEARTBEAT_FILE, else /tmp/onbot-heartbeat.json)."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onbot", description="Authentik → Matrix sync & onboarding bot.")
    parser.add_argument("--version", action="version", version=f"onbot {__version__}")
    parser.add_argument("--log-level", default=None, help=_LOG_LEVEL_HELP)

    # `--log-level` is accepted after the subcommand too (`onbot reconcile-once --log-level DEBUG`).
    # SUPPRESS keeps a subparser that did not get the flag from resetting a value given before the
    # subcommand back to None.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default=argparse.SUPPRESS, help=_LOG_LEVEL_HELP)

    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", parents=[common], help="Run the bot service (reconcile loop + onboarding).")
    run.add_argument("--heartbeat-file", default=None, help=_HEARTBEAT_HELP)
    sub.add_parser("reconcile-once", parents=[common], help="Run a single reconcile pass and exit.")
    bcast = sub.add_parser(
        "broadcast",
        parents=[common],
        help="Send a message to every user's onboarding room.",
        description=(
            "Send one message, as a notice, into the read-only onboarding room of every user the "
            "bot has onboarded. Exits non-zero if any room could not be reached."
        ),
    )
    bcast.add_argument("message", help="The message to send, e.g. 'Maintenance at 22:00 UTC'.")
    gen = sub.add_parser(
        "generate-config", parents=[common], help="Write a documented example configuration file."
    )
    gen.add_argument("-o", "--output", default=None, help="Write to this path instead of stdout.")
    health = sub.add_parser(
        "healthcheck",
        parents=[common],
        help="Check that the running service is alive (heartbeat), or with --deep its dependencies.",
        description=(
            "Without options: read the heartbeat file of the running `onbot run` and exit non-zero when "
            "its reconcile loop or sync pump has not succeeded for too long. Makes no network call and "
            "needs no config. With --deep: probe Synapse, Authentik and MAS with the configured "
            "credentials instead."
        ),
    )
    health.add_argument("--heartbeat-file", default=None, help=_HEARTBEAT_HELP)
    health.add_argument(
        "--deep",
        action="store_true",
        help="Probe the Matrix, Synapse admin, Authentik and MAS APIs with the real credentials.",
    )
    return parser


def _cmd_generate_config(output: str | None) -> int:
    text = generate_example_config()
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text)
        log.info("wrote example config to %s", output)
    else:
        print(text, end="")
    return 0


def _exit_on_signal(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def _install_early_signal_handlers() -> dict[int, Any]:
    """SIGTERM/SIGINT end the process until the event loop installs its own handlers."""
    previous: dict[int, Any] = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):  # not the main thread (embedded use)
            previous[sig] = signal.signal(sig, _exit_on_signal)
    return previous


def _cmd_heartbeat(heartbeat_file: str | None) -> int:
    from onbot.heartbeat import check_heartbeat_file, resolve_heartbeat_path

    path = resolve_heartbeat_path(heartbeat_file)
    verdict = check_heartbeat_file(path)
    for line in verdict.lines:
        print(line)
    print(f"{'healthy' if verdict.healthy else 'UNHEALTHY'} ({path})")
    return 0 if verdict.healthy else 1


def main(argv: Sequence[str] | None = None) -> int:
    previous = _install_early_signal_handlers()
    try:
        return _main(argv)
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)


def _main(argv: Sequence[str] | None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    if args.command == "healthcheck" and not args.deep:
        # The container HEALTHCHECK: no config, no network, no token (RUN-04, RUN-11). Quiet on
        # purpose, because Docker runs it every interval.
        return _cmd_heartbeat(args.heartbeat_file)

    log.info("onbot %s — command %r", __version__, args.command)

    if args.command == "generate-config":
        return _cmd_generate_config(args.output)

    # Commands that need live configuration + the async runtime.
    if get_config_file_path() is None:
        log.warning("no config file found; relying on ONBOT_* environment variables")
    config = load_config()
    # The config file's log_level is only knowable now; the CLI flag keeps precedence over it.
    if args.log_level is None:
        configure_logging(config.log_level)

    if args.command == "healthcheck":
        from onbot.healthcheck import run_healthcheck

        return asyncio.run(run_healthcheck(config))

    from onbot import app  # local import keeps `generate-config` usable without a config file

    if args.command == "run":
        from onbot.heartbeat import resolve_heartbeat_path

        return asyncio.run(
            app.run_service(config, heartbeat_path=resolve_heartbeat_path(args.heartbeat_file))
        )
    if args.command == "reconcile-once":
        return asyncio.run(app.run_interruptible(app.run_reconcile_once(config)))
    if args.command == "broadcast":
        return asyncio.run(app.run_interruptible(app.run_broadcast(config, args.message)))

    raise SystemExit(f"unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
