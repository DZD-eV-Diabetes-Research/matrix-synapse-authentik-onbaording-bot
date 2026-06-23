#!/usr/bin/env bash
# Run a single idempotent reconcile pass and exit (`onbot reconcile-once`).
#
# Handy for development: converge Authentik → Matrix once, then stop. Destructive lifecycle actions
# are dry-run by default (config `dry_run: true`, AD-5) — they are logged, not performed.
#
# Config is read from $ONBOT_CONFIG_FILE_PATH (default: ./config.yml). Extra args pass through, e.g.:
#     ./run_onbot_reconcile_once.sh --log-level DEBUG
set -euo pipefail
cd "$(dirname "$0")"

export ONBOT_CONFIG_FILE_PATH="${ONBOT_CONFIG_FILE_PATH:-config.yml}"

if [[ ! -f "$ONBOT_CONFIG_FILE_PATH" ]]; then
    echo "⚠️  Config file '$ONBOT_CONFIG_FILE_PATH' not found."
    echo "   Generate a documented starter config:"
    echo "       pdm run onbot generate-config -o config.yml"
    exit 1
fi

echo "🔁 Running one reconcile pass (config: $ONBOT_CONFIG_FILE_PATH)"
exec pdm run onbot reconcile-once "$@"
