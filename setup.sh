#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

VENV="$HOME/.sf-initial-setup-agent-venv"
[ -d "$VENV" ] || python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install -q --upgrade pip
pip install -q -r requirements.txt

command -v sf >/dev/null || {
  echo "Installing Salesforce CLI..."
  npm install -g @salesforce/cli
}

# The agent prompts for an Anthropic API key on first run if one isn't found
# in the environment or ~/.5gl-agents-env, so no key check is needed here.

echo "✅ Ready. Run ./run.sh"
