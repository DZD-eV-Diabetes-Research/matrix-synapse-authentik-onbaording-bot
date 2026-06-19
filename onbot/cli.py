"""Command-line entry point: ``python -m onbot`` / ``onbot``.

Sub-commands mirror the operational surface from ``BATTLE_PLAN.md`` §4. They are stubs for
now (Phase 2 is skeleton + tooling, no behaviour); each is implemented in later phases:

* ``run``             — long-running service: scheduled reconcile + sliding-sync onboarding
* ``reconcile-once``  — run a single idempotent reconcile and exit (Phase 3)
* ``generate-config`` — emit a documented example config (Phase 2/3)
* ``healthcheck``     — probe dependencies for container/orchestrator health (Phase 8)
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from onbot import __version__
from onbot.logging import configure_logging, get_logger

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onbot", description="Authentik → Matrix sync & onboarding bot.")
    parser.add_argument("--version", action="version", version=f"onbot {__version__}")
    parser.add_argument("--log-level", default=None, help="Override log level (e.g. DEBUG, INFO).")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the bot service (reconcile loop + onboarding).")
    sub.add_parser("reconcile-once", help="Run a single reconcile pass and exit.")
    sub.add_parser("generate-config", help="Write a documented example configuration file.")
    sub.add_parser("healthcheck", help="Check connectivity to required services.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    log.info("onbot %s — command %r", __version__, args.command)
    # Phase 2 skeleton: behaviour lands in Phases 3-8.
    raise SystemExit(f"'{args.command}' is not implemented yet (see BATTLE_PLAN.md).")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
