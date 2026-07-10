"""Command-line entry point: ``python -m onbot`` / ``onbot``.

Sub-commands mirror the operational surface from ``BATTLE_PLAN.md`` §4:

* ``run``             — long-running service: scheduled reconcile + (Phase 4) onboarding
* ``reconcile-once``  — run a single idempotent reconcile and exit
* ``generate-config`` — emit a documented example config (G11.2)
* ``healthcheck``     — probe dependencies for container/orchestrator health (Phase 8)
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from onbot import __version__
from onbot.config import generate_example_config, get_config_file_path, load_config
from onbot.logging import configure_logging, get_logger

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onbot", description="Authentik → Matrix sync & onboarding bot.")
    parser.add_argument("--version", action="version", version=f"onbot {__version__}")
    parser.add_argument("--log-level", default=None, help="Override log level (e.g. DEBUG, INFO).")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the bot service (reconcile loop + onboarding).")
    sub.add_parser("reconcile-once", help="Run a single reconcile pass and exit.")
    gen = sub.add_parser("generate-config", help="Write a documented example configuration file.")
    gen.add_argument("-o", "--output", default=None, help="Write to this path instead of stdout.")
    sub.add_parser("healthcheck", help="Check connectivity to required services.")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
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
        asyncio.run(app.run_service(config))
        return 0
    if args.command == "reconcile-once":
        asyncio.run(app.run_reconcile_once(config))
        return 0

    raise SystemExit(f"unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
