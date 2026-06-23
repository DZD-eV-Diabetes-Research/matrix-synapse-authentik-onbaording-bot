"""The ``python -m onbot`` shim is importable and re-exports the CLI entry point."""

from __future__ import annotations


def test_main_module_exposes_cli_main() -> None:
    import onbot.__main__ as main_module

    assert main_module.main.__module__ == "onbot.cli"
