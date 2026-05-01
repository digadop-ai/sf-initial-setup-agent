# sf-initial-setup-agent

A Claude-powered agent that bootstraps a new Salesforce metadata project for an org. Given an org username and alias, it creates a VS Code SFDX project, generates a manifest of all metadata, and retrieves it for source-controlled work.

Built and maintained by [5GL.ai](https://5gl.ai), a Salesforce consulting firm.

## What it does

1. Authenticates the org via `sf org login web` (browser flow) if not already authed
2. Generates a new SFDX project at `<your-directory>/<alias>-metadata`
3. Builds a `package.xml` manifest from the org's metadata
4. Removes managed-package types (unless asked to keep them)
5. Runs `sf project retrieve start` to pull all metadata into source

The agent pauses for confirmation before any destructive operation.

## Prerequisites

- macOS (other platforms not currently supported)
- [Salesforce CLI v2](https://developer.salesforce.com/tools/salesforcecli) (`sf`) — `npm install -g @salesforce/cli`
- Python 3.9+
- An Anthropic API key — get one at [console.anthropic.com](https://console.anthropic.com)
- A Salesforce org you can authenticate to

## Install (manual, for now)

```bash
git clone https://github.com/5GL-ai/sf-initial-setup-agent.git
cd sf-initial-setup-agent
./setup.sh

# Add your Anthropic key (one-time):
echo 'export FIVEGL_ANTHROPIC_API_KEY=sk-ant-...' >> ~/.5gl-agents-env
echo 'source ~/.5gl-agents-env' >> ~/.zshrc
source ~/.zshrc

./run.sh
```

A one-command installer (`install.sh`) is in development.

## Running

```bash
./run.sh
```

The agent prompts for: Salesforce username, sandbox flag, org alias, and a project parent directory (via the macOS folder picker). Last-used values are remembered in `~/.sf-initial-setup-agent-config.json`.

## Environment variable

The agent reads `FIVEGL_ANTHROPIC_API_KEY` first, then falls back to `ANTHROPIC_API_KEY`. The namespaced name lets the agent coexist with other tools (Cursor, Claude Code, etc.) that use the standard `ANTHROPIC_API_KEY`.

## Safety

The agent confines `read_file` and `write_file` to the project directory you select, and refuses shell commands that touch `~/.sfdx`, `~/.ssh`, `~/.aws`, dotfiles, or other sensitive paths. It cannot modify your shell rc files or read your auth tokens.

## Disclaimer

Provided as-is. The agent runs commands against your Salesforce org and your local filesystem. Review what it's doing — it pauses before destructive operations, but you should still understand each step. Not affiliated with or endorsed by Salesforce, Inc.
