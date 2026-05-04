#!/usr/bin/env bash
# run.sh — launch the v0.3.0+ orchestrator. The orchestrator (sf_initial_setup_agent.py)
# starts uvicorn on a free 127.0.0.1 port and opens the user's browser. WSL/macOS
# browser-launch detection lives in the Python orchestrator (_launch_browser).
set -e

cd "$(dirname "$0")"

VENV="$HOME/.sf-initial-setup-agent-venv"
if [ ! -d "$VENV" ]; then
    echo "error: venv not found at $VENV"
    echo "Run ./setup.sh first."
    exit 1
fi

# Source the shared 5GL key file so the API key is available to the orchestrator
# AND inherited by any subprocesses it spawns (retrieve_metadata.py, troubleshoot.py).
[ -f "$HOME/.5gl-agents-env" ] && source "$HOME/.5gl-agents-env"

# shellcheck disable=SC1091
source "$VENV/bin/activate"
exec python sf_initial_setup_agent.py "$@"
