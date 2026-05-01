#!/usr/bin/env bash
cd "$(dirname "$0")"
source "$HOME/.sf-initial-setup-agent-venv/bin/activate"
python sf_initial_setup_agent.py
