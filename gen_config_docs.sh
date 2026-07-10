#!/usr/bin/env bash
# Regenerate the committed configuration documentation from onbot/config.py.
#
# `onbot/config.py` is the single source of truth for configuration. Two artifacts are rendered from
# it with psyplus (https://github.com/DZD-eV-Diabetes-Research/pydantic-settings-yaml-plus) and
# committed, so they show up in code review and on GitHub:
#
#     docs/CONFIG_REFERENCE.md   human-readable reference (type, default, env-var, description)
#     config.example.yml         fully commented, fillable YAML template
#
# Never hand-edit those two files — edit the `Field(...)` metadata in onbot/config.py and re-run
# this script. psyplus reads each field's `title`, `description` and `examples`; the type, default,
# required-ness and ONBOT_* env-var name are derived automatically.
#
# Usage:
#     ./gen_config_docs.sh            # rewrite both files
#     ./gen_config_docs.sh --check    # verify they match the model; exit 1 on drift (CI/pre-commit)
#
# The drift check also runs as a unit test (tests/unit/test_config_docs.py), so a stale commit fails
# the fast test job even if this script is never invoked.
set -euo pipefail
cd "$(dirname "$0")"

# psyplus lives in the `docs` dependency group, which is not installed in the production image.
if ! pdm run python -c "import psyplus" 2>/dev/null; then
    echo "⚠️  psyplus is not installed (it lives in the 'docs' dependency group)."
    echo "   Install it with:"
    echo "       pdm install -G docs"
    exit 1
fi

exec pdm run python scripts/gen_config_docs.py "$@"
