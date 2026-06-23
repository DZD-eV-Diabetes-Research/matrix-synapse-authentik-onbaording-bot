"""Guard against drift between onbot/config.py and the committed config docs (Phase 8).

The committed ``docs/CONFIG_REFERENCE.md`` and ``config.example.yml`` are generated from the
pydantic-settings model by ``scripts/gen_config_docs.py``. If the model changes but the docs are not
regenerated, this test fails — telling the developer to run ``pdm run gen-config-docs``.

Skipped when psyplus (a docs-only dependency) is not installed, so the production runtime suite
still passes without it.
"""

from __future__ import annotations

import importlib.util

import pytest

if importlib.util.find_spec("psyplus") is None:  # pragma: no cover - depends on install profile
    pytest.skip("psyplus (docs group) not installed", allow_module_level=True)

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import gen_config_docs as gen  # noqa: E402


def test_markdown_reference_is_up_to_date() -> None:
    expected = gen.render_markdown()
    actual = gen.MARKDOWN_PATH.read_text(encoding="utf-8")
    assert actual == expected, "docs/CONFIG_REFERENCE.md is stale — run `pdm run gen-config-docs`"


def test_yaml_template_is_up_to_date() -> None:
    expected = gen.render_yaml()
    actual = gen.YAML_PATH.read_text(encoding="utf-8")
    assert actual == expected, "config.example.yml is stale — run `pdm run gen-config-docs`"
