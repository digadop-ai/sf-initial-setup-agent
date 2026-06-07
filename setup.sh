#!/usr/bin/env bash
# If `./setup.sh` says "permission denied" (e.g. when first running this from
# a Google-Drive-synced copy of the repo on a new machine — Drive doesn't
# always preserve the POSIX executable bit), bootstrap with `bash setup.sh`
# instead. This script will then chmod the rest of the runnables for you.
set -e
cd "$(dirname "$0")"

# Self-heal mode bits in case Drive sync (or a fresh extract) stripped them.
chmod +x setup.sh run.sh \
         prereqs.py retrieve_metadata.py web_ui.py troubleshoot.py sf_initial_setup_agent.py \
         2>/dev/null || true
chmod +x *.command 2>/dev/null || true

VENV="$HOME/.sf-initial-setup-agent-venv"
[ -d "$VENV" ] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -q --upgrade pip
pip install -q -r requirements.txt

command -v sf >/dev/null || {
  echo "Installing Salesforce CLI..."
  npm install -g @salesforce/cli
}

# Compile the AppleScript-based launcher into a double-clickable .app
# if it doesn't already exist (or is older than its source). End users
# get a Dock-able icon that boots the agent silently without showing
# a Terminal window. No code signing required for local-built apps.
APP="Launch sf-initial-setup-agent.app"
SRC="launcher.applescript"
if [ -f "$SRC" ] && command -v osacompile >/dev/null; then
  if [ ! -d "$APP" ] || [ "$SRC" -nt "$APP" ]; then
    osacompile -o "$APP" "$SRC" && echo "Built $APP"
  fi
fi

# The agent prompts for an Anthropic API key on first run if one isn't found
# in the environment or ~/.digadop-agents-env, so no key check is needed here.

echo "✅ Ready. Run ./run.sh  (or double-click 'Run sf-initial-setup-agent.command' in Finder)"
