#!/usr/bin/env python3
"""sf_initial_setup_agent.py — orchestrator entry point for v0.3.0+.

Thin layer. The real workflow lives elsewhere:

    prereqs.py            — detect + install OS/Python prerequisites
    web_ui.py             — FastAPI server: wizard, retrieve dashboard, WebSocket
    retrieve_metadata.py  — standalone parallel metadata retriever
    troubleshoot.py       — Claude wrapper invoked when chunks fail

This file:
    1. Resolves the Anthropic API key (prompts + persists to ~/.digadop-agents-env if missing).
       Skippable — the happy path runs without an API key; only the troubleshooter needs one.
    2. Runs prereq checks (interactive install on missing).
    3. Picks a free port (8765..8774).
    4. Launches the user's browser at http://127.0.0.1:<port>.
    5. Blocks on uvicorn until Ctrl+C.

History: v0.2.0 was a single-file LLM-in-the-loop supervisor over a 12-script pipeline
under scripts/. Backed up at sf_initial_setup_agent.py.v0.2.0.bak. The v0.3.0 redesign
moves the workflow into Python modules (prereqs / retrieve_metadata / troubleshoot) and
puts a local web UI in front of them.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import prereqs
import web_ui


AGENT_VERSION = "0.5.0"

ENV_FILE = Path.home() / ".digadop-agents-env"

_ENV_LINE = re.compile(r'^\s*(?:export\s+)?(\w+)\s*=\s*(.*?)\s*$')


# ── API key (persisted to ~/.digadop-agents-env, shared across Digadop AI desktop agents) ──────

def _read_env_file_var(var_name: str) -> Optional[str]:
    if not ENV_FILE.exists():
        return None
    try:
        for raw in ENV_FILE.read_text().splitlines():
            line = raw.split("#", 1)[0]
            m = _ENV_LINE.match(line)
            if not m or m.group(1) != var_name:
                continue
            val = m.group(2)
            if len(val) >= 2 and val[0] == val[-1] and val[0] == '"':
                val = val[1:-1]
                val = re.sub(r'\\([\\"`$])', r'\1', val)
            elif len(val) >= 2 and val[0] == val[-1] and val[0] == "'":
                val = val[1:-1]
            return val
    except OSError:
        return None
    return None


def _write_env_file_var(var_name: str, value: str) -> None:
    lines: list[str] = []
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text().splitlines():
            stripped = raw.split("#", 1)[0]
            m = _ENV_LINE.match(stripped)
            if m and m.group(1) == var_name:
                continue
            lines.append(raw)
    safe = (value
            .replace("\\", "\\\\").replace('"', '\\"')
            .replace("$", "\\$").replace("`", "\\`"))
    lines.append(f'export {var_name}="{safe}"')
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)


def resolve_api_key(interactive: bool = True) -> str:
    """Return key from env, key file, or prompt-and-persist. Returns '' if user skips."""
    key = os.environ.get("DIGADOP_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key = _read_env_file_var("DIGADOP_ANTHROPIC_API_KEY") or _read_env_file_var("ANTHROPIC_API_KEY")
    if key:
        return key
    if not interactive:
        return ""
    print()
    print("Anthropic API key not found.")
    print(f"  · Where to get one: https://console.anthropic.com/settings/keys")
    print(f"  · Where it'll be saved: {ENV_FILE} (shared across Digadop AI desktop agents)")
    print(f"  · Only the AI troubleshooter needs it — the happy-path retrieve runs without it.")
    print()
    try:
        key = getpass.getpass("Anthropic API key (input hidden, blank to skip): ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return ""
    if not key:
        print("→ Skipped. The agent will warn (but proceed) if any chunk fails without an API key.")
        return ""
    _write_env_file_var("DIGADOP_ANTHROPIC_API_KEY", key)
    print(f"→ Saved to {ENV_FILE}")
    return key


# ── Prereqs ─────────────────────────────────────────────────────────────────────

def run_prereqs_or_exit() -> None:
    """Check prereqs. If anything required is missing, prompt to install. Exit 1 if user declines."""
    results = prereqs.check_all()
    prereqs.print_status(results)
    missing_required = [(p, v) for p, v in results if not v and not p.optional]
    missing_optional = [(p, v) for p, v in results if not v and p.optional]
    if not missing_required:
        return
    print()
    print(f"{len(missing_required)} required prereq(s) missing.")
    try:
        ans = input("Install them now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    if ans not in ("y", "yes"):
        print("Cannot proceed without required prereqs. Exiting.")
        sys.exit(1)
    prereqs.install_missing_interactively(missing_required + missing_optional)
    print()
    print("Re-checking…")
    results2 = prereqs.check_all()
    prereqs.print_status(results2)
    still_missing = [p for p, v in results2 if not v and not p.optional]
    if still_missing:
        print()
        print(f"Still missing after install attempt: {', '.join(p.name for p in still_missing)}")
        sys.exit(1)


# ── Browser launch (cross-platform with WSL detection) ──────────────────────────

def _launch_browser(url: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False)
            return
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
            return
        proc_version = Path("/proc/version")
        if proc_version.is_file():
            try:
                if "microsoft" in proc_version.read_text().lower():
                    if subprocess.run(["which", "wslview"], capture_output=True).returncode == 0:
                        subprocess.run(["wslview", url], check=False)
                    else:
                        subprocess.run(["cmd.exe", "/c", "start", url], check=False)
                    return
            except OSError:
                pass
        if subprocess.run(["which", "xdg-open"], capture_output=True).returncode == 0:
            subprocess.run(["xdg-open", url], check=False)
        else:
            webbrowser.open(url)
    except Exception:
        pass


def open_browser_when_ready(url: str, delay: float = 1.5) -> None:
    """Open the browser after a short delay so uvicorn has time to bind the port."""
    t = threading.Timer(delay, _launch_browser, args=(url,))
    t.daemon = True
    t.start()


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> int:
    print()
    print(f"sf-initial-setup-agent · v{AGENT_VERSION}")
    print("=" * 50)
    print()

    resolve_api_key(interactive=True)

    print()
    run_prereqs_or_exit()

    try:
        port = web_ui.find_free_port()
    except RuntimeError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 2

    url = f"http://127.0.0.1:{port}"
    print()
    print(f"Web UI: {url}")
    print("Ctrl+C to shut down.")
    print()

    open_browser_when_ready(url)

    try:
        web_ui.start_server("127.0.0.1", port)
    except KeyboardInterrupt:
        print("\nShutting down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
