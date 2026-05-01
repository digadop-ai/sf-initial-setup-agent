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

if [ -z "$FIVEGL_ANTHROPIC_API_KEY" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "⚠️  No Anthropic API key set."
  echo "   Add to ~/.5gl-agents-env:"
  echo '     export FIVEGL_ANTHROPIC_API_KEY=sk-ant-...'
  echo "   Then ensure ~/.zshrc sources it:"
  echo '     echo "source ~/.5gl-agents-env" >> ~/.zshrc'
  echo "   Then: source ~/.zshrc"
  exit 1
fi

echo "✅ Ready. Run ./run.sh"
