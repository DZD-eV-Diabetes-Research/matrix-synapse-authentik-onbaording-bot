#!/usr/bin/env bash
# Start the onbot service: scheduled reconcile loop + onboarding listener (`onbot run`).
#
# Config is read from $ONBOT_CONFIG_FILE_PATH (default: ./config.yml); ONBOT_* env vars override it.
# Any extra arguments pass straight through to the `onbot` CLI, e.g.:
#     ./run_onbot.sh --log-level DEBUG
set -euo pipefail
cd "$(dirname "$0")"

export ONBOT_CONFIG_FILE_PATH="${ONBOT_CONFIG_FILE_PATH:-config.yml}"

if [[ ! -f "$ONBOT_CONFIG_FILE_PATH" ]]; then
    echo "⚠️  Config file '$ONBOT_CONFIG_FILE_PATH' not found."
    echo "   Generate a documented starter config:"
    echo "       pdm run onbot generate-config -o config.yml"
    echo "   Or point ONBOT_CONFIG_FILE_PATH at your config and re-run."
    exit 1
fi

echo "🤖 Starting onbot service (config: $ONBOT_CONFIG_FILE_PATH)"
exec pdm run onbot run "$@"
