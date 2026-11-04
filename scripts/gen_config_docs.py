#!/usr/bin/env python
"""Generate the committed config reference + YAML template from :class:`onbot.config.OnbotConfig`.

The single source of truth for configuration is the pydantic-settings model in ``onbot/config.py``.
This script renders two artifacts from it with `psyplus <https://pypi.org/project/psyplus/>`_:

* ``docs/CONFIG_REFERENCE.md`` — a human-readable reference (one section per field: type, default,
  ``ONBOT_*`` env-var, description, allowed values).
* ``config.example.yml`` — a fully commented, fillable YAML template.

Both are committed so they show up in code review and on GitHub. To avoid drift they are *generated*,
never hand-edited — change the ``Field(...)`` metadata in ``onbot/config.py`` and re-render:

    ./gen_config_docs.sh          # regenerate both files (root wrapper around this script)
    ./gen_config_docs.sh --check  # verify they are current; exit 1 on drift

    pdm run gen-config-docs       # the same, straight through pdm
    pdm run check-config-docs     # CI/pre-commit: fail if the committed files are stale

psyplus renders each field's ``title``, ``description`` and ``examples``; type, default,
required-ness and the ``ONBOT_*`` env-var name are derived from the model automatically.

``psyplus`` is a docs-only dependency (the ``docs`` group) and is deliberately **not** a runtime
dependency — the production image does not carry it. The runtime ``onbot generate-config`` command
emits its own minimal template via ``onbot.config.generate_example_config`` instead.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from psyplus import YamlSettingsPlus, yaml_generator
from psyplus.field_info import FieldInfoContainer
from psyplus.utils import clean_annotation
from pydantic import BaseModel, SecretStr

from onbot.config import OnbotConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKDOWN_PATH = REPO_ROOT / "docs" / "CONFIG_REFERENCE.md"
YAML_PATH = REPO_ROOT / "config.example.yml"

_GENERATED_BANNER = (
    "<!-- GENERATED FILE — do not edit by hand.\n"
    "     Regenerate with `./gen_config_docs.sh` after changing onbot/config.py. -->\n\n"
)
_YAML_BANNER = (
    "# Onbot configuration template — GENERATED from onbot/config.py.\n"
    "# Regenerate with `./gen_config_docs.sh`; do not edit by hand.\n"
    "# Copy this to config.yml and fill in the required (Required: True) values. Every setting can\n"
    "# also be supplied via its ONBOT_* environment variable (shown per field below).\n"
)


def _strip_trailing_ws(text: str) -> str:
    """Drop per-line trailing whitespace.

    psyplus's YAML line-folding can leave trailing spaces on wrapped scalars (e.g. the long welcome
    messages). Those are semantically irrelevant in YAML but would be stripped by the
    ``trailing-whitespace`` pre-commit hook, which would then make the committed file disagree with a
    fresh ``render`` (drift). Normalising here keeps the generator, the committed file and the hooks
    in agreement.
    """
    return "\n".join(line.rstrip() for line in text.splitlines())


def _construct(cls: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    """Build ``cls`` from psyplus' placeholder data without validating it.

    psyplus fills required fields with empty placeholders ("" for a URL, None for a secret) and then
    runs ``model_validate``, which the model's validators rightly reject (an empty URL is not a URL).
    The template only needs the structure, so construct the instance, nested blocks included.
    """
    values: dict[str, Any] = {}
    for key, value in data.items():
        annotation = clean_annotation(cls.model_fields[key].annotation)
        if isinstance(value, dict) and inspect.isclass(annotation) and issubclass(annotation, BaseModel):
            value = _construct(annotation, value)
        values[key] = value
    return cls.model_construct(**values)


def _build_placeholder_instance(cls: type[BaseModel]) -> BaseModel:
    return _construct(cls, yaml_generator._build_dict_for_class(cls))


_original_type_string = FieldInfoContainer.get_type_string


def _type_string(self: FieldInfoContainer) -> str | None:
    """Show a credential field as a secret string; psyplus would call a ``SecretStr`` "Object"."""
    if clean_annotation(self.get_annotation()) is SecretStr:
        return "str (secret)"
    return _original_type_string(self)


@contextlib.contextmanager
def _psyplus_adapted() -> Iterator[None]:
    with (
        mock.patch.object(yaml_generator, "build_settings_instance", _build_placeholder_instance),
        mock.patch.object(FieldInfoContainer, "get_type_string", _type_string),
    ):
        yield


def render_markdown() -> str:
    with _psyplus_adapted():
        rendered = YamlSettingsPlus(OnbotConfig).render_markdown()
    body = _strip_trailing_ws(rendered)
    return _GENERATED_BANNER + body.rstrip() + "\n"


def render_yaml() -> str:
    with _psyplus_adapted():
        rendered = YamlSettingsPlus(OnbotConfig).render_yaml()
    body = _strip_trailing_ws(rendered)
    return _YAML_BANNER + "\n" + body.rstrip() + "\n"


def _check(path: Path, expected: str) -> bool:
    """Return True if ``path`` already holds ``expected`` (no drift)."""
    actual = path.read_text(encoding="utf-8") if path.exists() else ""
    if actual == expected:
        print(f"OK    {path.relative_to(REPO_ROOT)}")
        return True
    print(f"DRIFT {path.relative_to(REPO_ROOT)} — run `pdm run gen-config-docs`")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed files match the model instead of writing them (exit 1 on drift).",
    )
    args = parser.parse_args(argv)

    targets = [(MARKDOWN_PATH, render_markdown()), (YAML_PATH, render_yaml())]

    if args.check:
        ok = all(_check(path, content) for path, content in targets)
        return 0 if ok else 1

    for path, content in targets:
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
