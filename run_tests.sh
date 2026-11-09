#!/usr/bin/env bash
# Run the fast onbot test suite: unit + contract, no live stack. Runs in seconds; the quick
# feedback loop for local dev and the fast CI job.
#
# Coverage is gated at 90 % here, the fast suite's own floor (review finding MNT-15). The higher
# gate over the full suite (fail_under in pyproject.toml) lives in ./run_integration_tests.sh, the
# only run that reaches the composition root (app.py) end-to-end.
#
# Pass --dev to stop at the first failure with full output (-x -s --tb=long), good for local dev.
# Any other arguments pass through to pytest, e.g.:
#     ./run_tests.sh tests/unit/test_engine.py -k mxid
#     ./run_tests.sh --dev
set -euo pipefail
cd "$(dirname "$0")"

PYTEST_ARGS=("-m" "not integration" "--cov" "--cov-report=term-missing" "--cov-fail-under=90")
for arg in "$@"; do
    if [ "$arg" = "--dev" ]; then
        PYTEST_ARGS+=("-x" "-s" "--tb=long")
    else
        PYTEST_ARGS+=("$arg")
    fi
done

exec pdm run pytest "${PYTEST_ARGS[@]}"
