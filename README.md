# sf-initial-setup-agent

Bootstraps a complete Salesforce metadata project for an org. Authenticates against your sandbox/production org, scaffolds an SFDX project, enumerates every metadata member (including managed-package contents), retrieves them in parallel chunks, and writes the result into a directory you can put under source control.

Built by [Digadop AI](https://github.com/digadop-ai).

## Architecture (v0.3.x)

Five Python modules plus a local FastAPI web UI:

| Module | Job |
|---|---|
| `prereqs.py` | Detects (and offers to install) Homebrew, Node, sf CLI, Java, jq, Python deps, an Anthropic API key, and `wslu` if running under WSL. Stdlib-only — runs before any deps are installed. |
| `retrieve_metadata.py` | Standalone parallel retriever. Builds chunked manifests (~1500 members per chunk by default), runs 6 workers in a `ThreadPoolExecutor`, deterministically splits and retries failed chunks once. Emits structured JSON progress events on stderr. Runnable directly without the LLM or web UI for CI use. |
| `web_ui.py` | FastAPI server bound to `127.0.0.1` (loopback only — no firewall prompts, no TLS needed). Hosts the wizard, retrieve dashboard with live chunk progress over WebSocket, package summary, and post-retrieve summary. Drives `retrieve_metadata.py` and `troubleshoot.py` as subprocesses. |
| `troubleshoot.py` | Claude (Sonnet 4.6) wrapper invoked **only** when chunks fail. Tools: `read_file`, `write_file`, `run_sf`, `run_shell`, and `rerun_chunk` (re-executes a failed chunk after a fix). Reads `salesforce-mcp-setup.md` as system context if present. |
| `sf_initial_setup_agent.py` | Thin orchestrator entry point. Resolves the Anthropic API key, runs the prereq check, picks a free port (8765..8774), starts uvicorn, and opens your browser. |

The "🔑 happy path" model: a clean retrieve never spends an Anthropic API key. The key is only required if a chunk fails and you want the troubleshooter to investigate.

## What's included

- **All metadata your org's API exposes**, retrieved via explicit member listing (a wildcard `*` only returns your org's namespace; explicit names capture managed-package contents too).
- **Folder-based types** (Report, Dashboard, Document, EmailTemplate) enumerated via SOQL on `Folder`, with `unfiled$public` added for Report and EmailTemplate.
- **`StandardValueSet`** — hardcoded canonical list since `sf org list metadata` doesn't enumerate it.
- **`Profile`** retrieved alongside its shape drivers (`CustomObject`, `ApexClass`, `CustomApplication`, `CustomTab`, `CustomPermission`, `Layout`, `RecordType`) in dedicated chunks so profiles come back fully populated.
- **`InstalledPackage`** included by default (overridable).

## What's NOT included (by design)

- **Data** (records of any object) — separate `sf-data-export-agent` planned.
- **OmniStudio / Vlocity / Industry Cloud** metadata — uses a different metadata system; the agent warns if detected.
- **Tooling-API-only items** like Setup Audit Trail history, Apex test coverage, debug logs — separate monitoring agent planned.

## Prerequisites

- macOS
- [Salesforce CLI v2](https://developer.salesforce.com/tools/salesforcecli) (`sf`) — installed by `setup.sh` if `npm` is available, or via the agent's prereq check
- Node.js 18+
- Python 3.9+
- Java 17+ (Temurin recommended) — some retrieves invoke a JVM-backed metadata packager
- `jq`
- An Anthropic API key — **optional**; only used by the troubleshooter on chunk failure

A one-command installer (`5GL-ai/sf-agent-installer`) that bootstraps all of the above is in development.

## Install

```bash
git clone https://github.com/digadop-ai/sf-initial-setup-agent.git
cd sf-initial-setup-agent
./setup.sh
./run.sh
```

`./run.sh` resolves the API key (prompting once and persisting to `~/.digadop-agents-env` if missing), runs the prereq check (interactively offers to install anything missing), starts the web UI on a free local port, and opens it in your default browser.

## What you'll see in the browser

1. **Wizard** at `/` — fill in: org alias (must already be authed via `sf org login`), project parent directory (the macOS folder picker is one click away), managed-package toggle, optional namespace exclusions, and concurrency / chunk size / wait-window knobs. Settings persist to `~/.sf-initial-setup-agent-config.json`.
2. **Dashboard** at `/dashboard` — click *Start retrieve*. The web UI scaffolds the SFDX project at `<parent>/<alias>-metadata` if it doesn't exist, pins `sourceApiVersion` to your org's max, then spawns `retrieve_metadata.py`. A live chunk table updates over WebSocket as workers progress.
3. **Summary** at `/summary` — final per-chunk outcomes, package summary table, and `<project>/.5gl-sync-state.json` written for the future monitoring agent.
4. If any chunks failed and an Anthropic key is available, the troubleshooter is invoked automatically and its events stream into the same dashboard with `source: "troubleshooter"` tags.

## Standalone retriever (no LLM, no web UI)

`retrieve_metadata.py` is runnable directly — useful for CI or when you want a deterministic invocation:

```bash
python retrieve_metadata.py \
    --alias myorg \
    --directory ~/sf-projects/myorg-metadata \
    --include-managed=true \
    --concurrency 6 \
    --chunk-size 1500 \
    --wait-minutes 60
```

CLI flags:
- `--include-managed=true|false` — default `true`. Override to mimic v0.2.0's strip-managed behavior.
- `--exclude-expired-packages` — drops members from packages with `PackageLicense.Status IN ('Expired','Suspended')`.
- `--exclude-namespaces=ns1,ns2` — drops members starting with `<ns>__`.
- Per-chunk logs land in `<project>/manifest/logs/`. Final per-chunk results land in `<project>/manifest/retrieve-summary.json`.

## Environment variables

- `DIGADOP_ANTHROPIC_API_KEY` — read first; namespaced so it doesn't clash with other tools using `ANTHROPIC_API_KEY`.
- `ANTHROPIC_API_KEY` — fallback. Both can also live in `~/.digadop-agents-env` (sourced by `run.sh`).

## Safety

- Web UI binds **only to `127.0.0.1`** — never `0.0.0.0`. Loopback traffic bypasses the macOS app firewall, so no permission prompt; nothing on your network can reach the UI.
- The `troubleshoot.py` tool surface is path-confined to the project directory you select. Shell commands are denylisted against destructive patterns (`rm -rf`, `sudo`, `curl | sh`, etc.).
- File tools refuse paths outside the project dir.

## Sync state

After a successful retrieve, `<project>/.5gl-sync-state.json` is written with: alias, org id, username, timestamp, agent name + version, manifest hash (sorted union of `(type, member)` tuples), and total file count. A future monitoring agent will read this to query `SetupAuditTrail` for changes since the last sync and offer incremental re-retrieves.

## Disclaimer

Provided as-is. The agent runs commands against your Salesforce org and writes to your local filesystem. Review what it does — destructive operations are flagged, but you should still understand each step. Not affiliated with or endorsed by Salesforce, Inc.
