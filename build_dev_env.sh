#!/usr/bin/env bash
# Set up (and activate) the onbot development environment via PDM (Python 3.14).
#
# Must be *sourced*, not executed, so the activated virtualenv stays in your shell:
#     source ./build_dev_env.sh

# Detect if being sourced (so we can activate the venv in the parent shell).
(return 0 2>/dev/null) && SOURCED=1 || SOURCED=0
if [[ $SOURCED -eq 0 ]]; then
    echo "❌ Error: this script must be sourced, not executed — otherwise the venv won't stay active."
    echo "Usage: source $0"
    exit 1
fi

# Strict mode, but no -u: sourcing into the parent shell shouldn't trip on its unset vars.
set -eo pipefail

# === CONFIGURATION ===
PYTHON_VERSION="3.14"   # must satisfy requires-python in pyproject.toml
ENV_DIR=".venv"         # PDM is configured for an in-project venv (venv.in_project = True)

# === FUNCTIONS ===

install_pdm() {
    if ! command -v pdm &>/dev/null; then
        echo "pdm not found. Installing via the official installer..."
        curl -sSL https://pdm-project.org/install-pdm.py | python3 -
        export PATH="$HOME/.local/bin:$PATH"
        echo "pdm installed. (Add ~/.local/bin to your PATH to keep it.)"
    else
        echo "pdm already installed ($(pdm --version))."
    fi
}

ensure_python() {
    # PDM resolves the interpreter from .python-version / requires-python. Only pick one
    # explicitly if no project interpreter is selected yet.
    if [[ ! -x "$ENV_DIR/bin/python" ]]; then
        echo "Selecting a Python $PYTHON_VERSION interpreter for PDM..."
        pdm use -f "$PYTHON_VERSION" || echo "  (could not pre-select; pdm install will resolve one)"
    fi
}

install_deps() {
    # Installs the project plus the `dev` dependency group (ruff/mypy/pytest/respx/...).
    echo "Installing dependencies (pdm install)..."
    pdm install
}

activate_env() {
    if [[ -f "$ENV_DIR/bin/activate" ]]; then
        echo "🏎️  Activating environment at $ENV_DIR"
        # shellcheck disable=SC1091
        source "$ENV_DIR/bin/activate"
    else
        echo "⚠️  No $ENV_DIR found after install — run scripts use 'pdm run', so this is non-fatal."
    fi
}

# === MAIN SCRIPT ===

install_pdm
ensure_python
install_deps
activate_env

echo "✅ onbot dev environment ready (Python $PYTHON_VERSION, via PDM)."
echo ""
echo "Next steps:"
echo "  ./run_tests.sh                 # fast unit + contract suite (with the coverage gate)"
echo "  ./run_onbot.sh                 # start the bot service (reconcile loop + onboarding)"
echo "  ./run_onbot_reconcile_once.sh  # a single reconcile pass (dry-run by default)"
