#!/usr/bin/env bash
# Run the FULL test suite (unit + contract + integration) with the coverage gate against a live
# Synapse + MAS + Postgres + Authentik stack (Phase 7b). The stack is brought up automatically by
# the testcontainers fixture in tests/integration/conftest.py (Docker required).
#
# This is the authoritative coverage gate: it runs the whole suite, so the composition root
# (onbot/app.py), only reachable end-to-end, is exercised and counted (fail_under in pyproject).
# A stack that cannot start fails the run (exit code != 0); it is never reported as skipped.
#
# Env:
#   ONBOT_ITEST_KEEP=1       leave the stack(s) running after the run (fast local iteration: the
#                            next run reuses them and skips the boot)
#   ONBOT_ITEST_INSTANCE=<n> use an isolated stack instance: compose project onbot-itest-<n>, host
#                            ports shifted by 10*n. Lets several worktrees run the suite at the same
#                            time. See tests/README.md.
#   ONBOT_ITEST_WORKERS=<k>  split the run over k pytest-xdist workers (default 1). Every worker
#                            boots a stack of its own (about 2 to 3 GB of RAM each): worker 0 uses
#                            the instance's stack, worker j the project onbot-itest-<n>-w<j>.
# Flags:
#   --dev                stop at first failure with full output (-x -s --tb=long)
#   --down               tear down every stack of this instance (workers included) and exit
# Other args pass through to pytest, e.g.:  ./run_integration_tests.sh -k lifecycle
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
    echo "❌ Docker is required for the integration suite (Synapse+MAS+Postgres+Authentik)." >&2
    exit 1
fi

if [ "${ONBOT_ITEST_SKIP:-}" = "1" ]; then
    echo "❌ ONBOT_ITEST_SKIP=1 is set, but this script is the integration run. Unset it." >&2
    exit 1
fi

WORKERS="${ONBOT_ITEST_WORKERS:-1}"
if ! [[ "$WORKERS" =~ ^[0-9]+$ ]] || [ "$WORKERS" -lt 1 ] || [ "$WORKERS" -gt 10 ]; then
    echo "❌ ONBOT_ITEST_WORKERS must be a number from 1 to 10, got '${WORKERS}'." >&2
    exit 1
fi

# The compose projects of this instance: the instance's own and one per further worker.
projects() {
    pdm run python -c '
import os
from tests.integration.stack_instance import MAX_WORKER, StackInstance
base = StackInstance.from_env({k: v for k, v in os.environ.items() if k != "PYTEST_XDIST_WORKER"})
for worker in range(MAX_WORKER + 1):
    print(base.for_worker(worker).project)
'
}

for arg in "$@"; do
    if [ "$arg" = "--down" ]; then
        running="$(docker compose ls -a -q)"
        for project in $(projects); do
            if grep -qx "$project" <<<"$running"; then
                echo "Tearing down ${project}"
                docker compose -f tests/integration/stack/docker-compose.yml -p "$project" down -v
            fi
        done
        exit 0
    fi
done

echo "🧪 Full suite + live stack (Phase 7b). First run pulls images and boots the stack (~2-3 min)."
pdm run python -c '
import os, sys
from tests.integration.stack_instance import StackInstance as S
base = S.from_env(os.environ)
for worker in range(int(sys.argv[1])):
    print("   Stack:", base.for_worker(worker).describe())
' "$WORKERS"

PYTEST_ARGS=("--cov" "--cov-report=term-missing")
if [ "$WORKERS" -gt 1 ]; then
    # One test file stays on one worker, so a file's tests keep their order and their stack.
    PYTEST_ARGS+=("-n" "$WORKERS" "--dist" "loadfile")
fi
for arg in "$@"; do
    if [ "$arg" = "--dev" ]; then
        PYTEST_ARGS+=("-x" "-s" "--tb=long")
    else
        PYTEST_ARGS+=("$arg")
    fi
done

exec pdm run pytest "${PYTEST_ARGS[@]}"
