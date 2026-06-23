#!/usr/bin/env bash
# Run the FULL test suite (unit + contract + integration) with the coverage gate against a live
# Synapse + MAS + Postgres + Authentik stack (Phase 7b). The stack is brought up automatically by
# the testcontainers fixture in tests/integration/conftest.py (Docker required).
#
# This is the authoritative coverage gate: it runs the whole suite, so the composition root
# (onbot/app.py) — only reachable end-to-end — is exercised and counted (fail_under in pyproject).
#
# Env:
#   ONBOT_ITEST_KEEP=1   leave the stack running after the run (fast local iteration)
# Flags:
#   --dev                stop at first failure with full output (-x -s --tb=long)
# Other args pass through to pytest, e.g.:  ./run_integration_tests.sh -k lifecycle
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
    echo "❌ Docker is required for the integration suite (Synapse+MAS+Postgres+Authentik)." >&2
    exit 1
fi

echo "🧪 Full suite + live stack (Phase 7b). First run pulls images and boots the stack (~2-3 min)."

PYTEST_ARGS=("--cov" "--cov-report=term-missing")
for arg in "$@"; do
    if [ "$arg" = "--dev" ]; then
        PYTEST_ARGS+=("-x" "-s" "--tb=long")
    else
        PYTEST_ARGS+=("$arg")
    fi
done

exec pdm run pytest "${PYTEST_ARGS[@]}"
