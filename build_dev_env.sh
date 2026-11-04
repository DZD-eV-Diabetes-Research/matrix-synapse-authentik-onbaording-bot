#!/usr/bin/env bash
# Set up (and activate) the onbot development environment via PDM (Python 3.14).
#
# Must be *sourced*, not executed, so the activated virtualenv stays in your shell:
#     source ./build_dev_env.sh
#
# The setup steps run in a subshell with `set -eo pipefail`, so a failing step stops the setup but
# none of those options leak into your interactive shell (review finding SUP-16). Only the final
# `source .venv/bin/activate` runs in your shell.

# Detect if being sourced (so we can activate the venv in the parent shell).
if ! (return 0 2>/dev/null); then
    echo "Error: this script must be sourced, not executed, otherwise the venv won't stay active."
    echo "Usage: source $0"
    exit 1
fi

# The PDM version CI and the release workflow use (.github/workflows/*.yml, `version:`).
ONBOT_DEV_PDM_VERSION="2.27.0"

_onbot_dev_setup() (
    # A subshell (note the parentheses): strict mode and cd stay in here.
    set -eo pipefail
    cd "$(dirname "${BASH_SOURCE[0]}")"

    if command -v pdm &>/dev/null; then
        echo "pdm already installed ($(pdm --version))."
    elif command -v pipx &>/dev/null; then
        echo "pdm not found. Installing pdm==${ONBOT_DEV_PDM_VERSION} with pipx..."
        pipx install "pdm==${ONBOT_DEV_PDM_VERSION}"
    else
        echo "pdm not found, and pipx is not available to install it."
        echo "Install pdm ${ONBOT_DEV_PDM_VERSION} (for example 'pipx install pdm==${ONBOT_DEV_PDM_VERSION}',"
        echo "see https://pdm-project.org/latest/#installation), then source this script again."
        return 1
    fi

    # PDM resolves the interpreter from .python-version / requires-python. Only pick one
    # explicitly if no project interpreter is selected yet.
    if [[ ! -x .venv/bin/python ]]; then
        echo "Selecting a Python 3.14 interpreter for PDM..."
        pdm use -f "3.14" || echo "  (could not pre-select; pdm install will resolve one)"
    fi

    # Installs the project plus the dev dependency groups (ruff/mypy/pytest/respx/...), exactly as
    # locked.
    echo "Installing dependencies (pdm install --frozen-lockfile)..."
    pdm install --dev --frozen-lockfile
)

if _onbot_dev_setup; then
    _onbot_dev_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "$_onbot_dev_dir/.venv/bin/activate" ]]; then
        echo "Activating environment at $_onbot_dev_dir/.venv"
        # shellcheck disable=SC1091
        source "$_onbot_dev_dir/.venv/bin/activate"
    else
        echo "No .venv found after install. The run scripts use 'pdm run', so this is non-fatal."
    fi
    echo "onbot dev environment ready (Python 3.14, via PDM)."
    echo ""
    echo "Next steps:"
    echo "  ./run_tests.sh                 # fast unit + contract suite (coverage reported, not gated)"
    echo "  ./run_integration_tests.sh     # full suite against the live stack, with the coverage gate"
    echo "  ./run_onbot.sh                 # start the bot service (reconcile loop + onboarding)"
    echo "  ./run_onbot_reconcile_once.sh  # a single reconcile pass"
    unset _onbot_dev_dir
else
    echo "Setup failed (see above). Your shell options are unchanged."
fi
unset -f _onbot_dev_setup
unset ONBOT_DEV_PDM_VERSION
