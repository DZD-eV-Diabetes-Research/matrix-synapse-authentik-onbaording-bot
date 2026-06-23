#!/usr/bin/env bash
# Run the fast onbot test suite — unit + contract, no live stack. Runs in seconds; the quick
# feedback loop for local dev and the fast CI job.
#
# Coverage is reported but NOT gated here: the composition root (app.py) is only exercised by the
# integration harness, so the authoritative coverage gate lives in ./run_integration_tests.sh
# (the full suite). See BATTLE_PLAN.md Phase 7.
#
# Pass --dev to stop at the first failure with full output (-x -s --tb=long), good for local dev.
# Any other arguments pass through to pytest, e.g.:
#     ./run_tests.sh tests/unit/test_engine.py -k mxid
#     ./run_tests.sh --dev
set -euo pipefail
cd "$(dirname "$0")"

PYTEST_ARGS=("-m" "not integration" "--cov" "--cov-report=term-missing" "--cov-fail-under=0")
for arg in "$@"; do
    if [ "$arg" = "--dev" ]; then
        PYTEST_ARGS+=("-x" "-s" "--tb=long")
    else
        PYTEST_ARGS+=("$arg")
    fi
done

exec pdm run pytest "${PYTEST_ARGS[@]}"
