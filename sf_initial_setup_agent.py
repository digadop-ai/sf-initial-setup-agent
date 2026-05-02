import os, subprocess, json, time, re, getpass
from pathlib import Path
from anthropic import Anthropic, APIStatusError, APIConnectionError, RateLimitError

# Shared env file for all 5GL agents. The agent reads it directly so it works
# even if the current shell hasn't sourced it (or if ~/.zshrc never did).
ENV_FILE = Path.home() / ".5gl-agents-env"

# Set in __main__ once the key has been resolved.
client = None

CONFIG_PATH = Path.home() / ".sf-initial-setup-agent-config.json"

# Set by run_agent() before the loop starts; used to confine the file tools.
# (No type annotation — keeps this 3.9-compatible; PEP 604 `X | Y` needs 3.10+.)
PROJECT_ROOT = None

# Substrings we never let the file or shell tools touch, even via the project
# directory (e.g., a symlink into ~/.sfdx).
DENY_SUBSTRINGS = (
    "/.sfdx/", "/.ssh/", "/.aws/", "/.anthropic",
    "/.zshrc", "/.bashrc", "/.bash_profile", "/.profile",
    "/.config/gh/",
)

# Context-window soft limit. Sonnet 4 is 200K; we bail at 180K so the next
# turn's tool results don't push us over.
CONTEXT_TOKEN_SOFT_LIMIT = 180_000

# ---------- api key ----------
# Matches shell-style assignment: optional `export`, NAME=value, with optional
# surrounding single or double quotes. Captures (name, value).
_ENV_LINE = re.compile(r'^\s*(?:export\s+)?(\w+)\s*=\s*(.*?)\s*$')

def _read_env_file_var(var_name):
    """Return the value of var_name in ENV_FILE, or None. Handles quoted values."""
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
                # Inverse of _write_env_file_var's escaping inside double quotes.
                val = val[1:-1]
                val = re.sub(r'\\([\\"`$])', r'\1', val)
            elif len(val) >= 2 and val[0] == val[-1] and val[0] == "'":
                # Single-quoted shell strings have no escapes.
                val = val[1:-1]
            return val
    except Exception:
        return None
    return None

def _write_env_file_var(var_name, value):
    """Add or update var_name=value in ENV_FILE, preserving other lines. Sets 600 perms."""
    lines = []
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text().splitlines():
            stripped = raw.split("#", 1)[0]
            m = _ENV_LINE.match(stripped)
            if m and m.group(1) == var_name:
                continue
            lines.append(raw)
    # Escape characters that have meaning inside double-quoted shell strings so
    # the file remains safe to `source` from ~/.zshrc.
    safe = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    lines.append(f'export {var_name}="{safe}"')
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)

def resolve_api_key():
    """Return an Anthropic API key, prompting the user if necessary.

    Resolution order:
      1. FIVEGL_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY env vars (set by the shell).
      2. Same vars parsed directly out of ~/.5gl-agents-env (defensive — the file
         might exist but the current shell never sourced it).
      3. Interactive prompt; persisted to ~/.5gl-agents-env as the shared default
         for all 5GL agents.
    """
    key = os.environ.get("FIVEGL_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key = _read_env_file_var("FIVEGL_ANTHROPIC_API_KEY") or _read_env_file_var("ANTHROPIC_API_KEY")
    if key:
        return key
    print("\nAnthropic API key not found.")
    print(f"It will be saved to {ENV_FILE} as the shared key for all 5GL agents.")
    print("(Get one at https://console.anthropic.com/settings/keys)\n")
    while True:
        key = getpass.getpass("Anthropic API key (input hidden): ").strip()
        if key:
            break
        print("  (required)")
    _write_env_file_var("FIVEGL_ANTHROPIC_API_KEY", key)
    print(f"→ Saved to {ENV_FILE}\n")
    return key

# ---------- config ----------
def load_config():
    if CONFIG_PATH.exists():
        try: return json.loads(CONFIG_PATH.read_text())
        except Exception: pass
    return {}

def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

# ---------- startup wizard ----------
def ask(label, default=None, required=True):
    suffix = f" [{default}]" if default else ""
    while True:
        v = input(f"{label}{suffix}: ").strip() or (default or "")
        if v or not required: return v
        print("  (required)")

def pick_folder(default=None):
    default_clause = ""
    if default and Path(default).exists():
        default_clause = f' default location POSIX file "{default}"'
    script = f'POSIX path of (choose folder with prompt "Select project parent directory"{default_clause})'
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        print("  (picker cancelled — type path instead)")
        return ask("Project parent directory", default)
    return r.stdout.strip().rstrip("/")

def startup_wizard():
    cfg = load_config()
    print("\n" + "="*60)
    print("  5GL Salesforce Initial Setup Agent")
    print("="*60 + "\n")
    username = ask("Salesforce username (email)", cfg.get("last_username"))
    sandbox = ask("Sandbox? (y/n)", "y" if cfg.get("last_sandbox") else "n").lower().startswith("y")
    alias = ask("Org alias", cfg.get("last_alias"))
    print("\nOpening folder picker...")
    directory = pick_folder(cfg.get("last_directory", str(Path.home())))
    save_config({
        "last_username": username, "last_alias": alias,
        "last_directory": directory, "last_sandbox": sandbox,
    })
    print(f"\n→ Saved for next time: {CONFIG_PATH}\n")
    return {"username": username, "alias": alias,
            "directory": directory, "sandbox": sandbox}

# ---------- tools ----------
TOOLS = [
    {"name": "run_sf",
     "description": "Run a Salesforce CLI command. Include --json when you need to parse output.",
     "input_schema": {"type": "object", "properties": {
         "args": {"type": "array", "items": {"type": "string"}},
         "cwd": {"type": "string"}}, "required": ["args"]}},
    {"name": "run_shell",
     "description": "General shell command. Runs with a 10-minute timeout.",
     "input_schema": {"type": "object", "properties": {
         "cmd": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["cmd"]}},
    {"name": "write_file",
     "description": "Create or overwrite a file. Confined to the project directory.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
    {"name": "read_file",
     "description": "Read a file (truncated to 20KB). Confined to the project directory.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
]

def _is_path_safe(p):
    """True iff p resolves inside PROJECT_ROOT and doesn't hit a deny-listed substring."""
    if PROJECT_ROOT is None:
        return False
    try:
        target = Path(p).expanduser().resolve()
    except Exception:
        return False
    s = str(target)
    if any(bad in s for bad in DENY_SUBSTRINGS):
        return False
    root = PROJECT_ROOT.resolve()
    return target == root or root in target.parents

def _shell_looks_dangerous(cmd):
    """Cheap guard against obvious attempts to read secrets via run_shell."""
    low = cmd.lower()
    return any(bad.strip("/") in low for bad in DENY_SUBSTRINGS)

def run_tool(name, inp):
    try:
        if name == "run_sf":
            r = subprocess.run(["sf"] + inp["args"], capture_output=True,
                               text=True, cwd=inp.get("cwd"), timeout=3600)
            return f"exit={r.returncode}\nSTDOUT:\n{r.stdout[-15000:]}\nSTDERR:\n{r.stderr[-3000:]}"
        if name == "run_shell":
            cmd = inp["cmd"]
            if _shell_looks_dangerous(cmd):
                return f"ERROR: refusing shell command that references a sensitive path ({cmd!r})"
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, cwd=inp.get("cwd"), timeout=600)
            return f"exit={r.returncode}\nSTDOUT:\n{r.stdout[-10000:]}\nSTDERR:\n{r.stderr[-3000:]}"
        if name == "write_file":
            if not _is_path_safe(inp["path"]):
                return f"ERROR: refusing to write outside project directory ({PROJECT_ROOT})"
            p = Path(inp["path"]); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(inp["content"])
            return f"wrote {len(inp['content'])} bytes to {p}"
        if name == "read_file":
            if not _is_path_safe(inp["path"]):
                return f"ERROR: refusing to read outside project directory ({PROJECT_ROOT})"
            return Path(inp["path"]).read_text()[:20000]
        return f"ERROR: unknown tool '{name}'"
    except subprocess.TimeoutExpired as e:
        return f"ERROR: command timed out after {e.timeout}s"
    except Exception as e:
        return f"ERROR: {e}"

SYSTEM = """You are the SF Initial Setup Agent for 5GL.ai consulting.

The user has already provided: username, alias, project directory, and sandbox/prod flag.
Execute this workflow:

1. Check auth: `sf org display --target-org ALIAS --json`. If NOT authenticated, run
   `sf org login web --alias ALIAS` (add `--instance-url https://test.salesforce.com` for sandbox).
   The browser will open for the user to log in.
2. After login, verify the authenticated username matches what the user said
   (case-insensitive, trimmed). If it doesn't match, stop and warn.
3. Before creating the project, check whether `<directory>/<ALIAS>-metadata` already
   exists. If it does, DO NOT overwrite it — ask the user whether to pick a new
   name, reuse the existing project, or abort.
4. `cd` to the project directory and run `sf project generate --name ALIAS-metadata`.
5. From inside the new project folder, run
   `sf project generate manifest --from-org ALIAS --output-dir manifest --name package`.
6. Read manifest/package.xml, sanity-check the types, and explicitly remove
   managed-package metadata (InstalledPackage and types prefixed with a managed
   namespace) unless the user asks to include them.
7. `sf project retrieve start --manifest manifest/package.xml --target-org ALIAS --wait 1800`.
   If the retrieve times out, the CLI prints a job id — use
   `sf project retrieve resume --job-id <id> --wait 1800` to continue rather than restarting.
8. On partial failure, split the manifest by type and retry failed chunks one at a time.
9. Summarize: project path, file count (`find force-app -type f | wc -l`), skipped types.

Rules:
- Use --json on sf commands when parsing output.
- Exclude managed-package metadata unless asked.
- Confirm with the user before any destructive op (deleting files, overwriting an
  existing project, `sf org delete`, etc.).
- write_file and read_file are confined to the project directory. Do not try to
  read or write auth tokens, dotfiles, or anything under ~/.sfdx.
- Pass `cwd` explicitly when running commands from a specific directory. `cd`
  inside run_shell does not persist between calls.
"""

# ---------- agent loop ----------
def _call_api_with_retry(**kwargs):
    """Call client.messages.create with explicit retry on transient failures.

    The SDK already retries internally; this adds a more patient second layer so
    a multi-minute outage doesn't drop a long agent session. Re-raises 4xx
    (e.g., 400 context-length) immediately since those won't fix themselves.
    """
    delays = [2, 5, 15, 30, 60]
    last_err = None
    for attempt in range(len(delays) + 1):
        try:
            return client.messages.create(**kwargs)
        except (APIConnectionError, RateLimitError) as e:
            last_err = e
        except APIStatusError as e:
            status = getattr(e, "status_code", None)
            if status is None or status < 500:
                raise
            last_err = e
        if attempt >= len(delays):
            raise last_err
        wait = delays[attempt]
        print(f"  (API error: {type(last_err).__name__} — retrying in {wait}s)")
        time.sleep(wait)

def run_agent(params):
    global PROJECT_ROOT
    PROJECT_ROOT = Path(params["directory"]).expanduser().resolve()
    if not PROJECT_ROOT.is_dir():
        print(f"ERROR: project directory does not exist: {PROJECT_ROOT}")
        return

    instance = "https://test.salesforce.com" if params["sandbox"] else "https://login.salesforce.com"
    initial = (
        f"Set up a Salesforce project with these inputs:\n"
        f"- Username (expected): {params['username']}\n"
        f"- Alias: {params['alias']}\n"
        f"- Project parent directory: {params['directory']}\n"
        f"- Environment: {'SANDBOX' if params['sandbox'] else 'PRODUCTION'} ({instance})\n\n"
        f"Execute the full workflow now."
    )
    history = [{"role": "user", "content": initial}]
    print(f"→ Starting agent...\n")

    while True:
        while True:
            try:
                resp = _call_api_with_retry(
                    model="claude-sonnet-4-6", max_tokens=4096,
                    system=SYSTEM, tools=TOOLS, messages=history,
                )
            except APIStatusError as e:
                status = getattr(e, "status_code", "?")
                print(f"\nAPI error ({status}): {e}")
                print("Stopping this turn — press Enter to quit or type a follow-up.")
                break
            except Exception as e:
                print(f"\nAPI error after retries: {type(e).__name__}: {e}")
                print("Stopping this turn — press Enter to quit or type a follow-up.")
                break

            history.append({"role": "assistant", "content": resp.content})

            # Print any text the model produced, not just on end_turn, so partial
            # output is visible if the turn gets cut off by max_tokens/refusal.
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    print(f"\nAgent: {b.text}")

            stop = resp.stop_reason

            # Context-window guard. input_tokens is what the server just saw;
            # the next call adds tool results on top.
            usage = getattr(resp, "usage", None)
            if usage and getattr(usage, "input_tokens", 0) >= CONTEXT_TOKEN_SOFT_LIMIT:
                print(f"\n⚠️  Context at {usage.input_tokens} tokens — "
                      f"stopping before next call would overflow the window.")
                print("    Save any output you need and restart the agent.")
                return

            if stop == "end_turn":
                break
            if stop == "max_tokens":
                print("\n⚠️  Response hit max_tokens. Partial output above; stopping this turn.")
                break
            if stop == "refusal":
                print("\n⚠️  Model refused to continue. Stopping.")
                return
            if stop == "pause_turn":
                print("\n⚠️  Server returned pause_turn. Stopping this turn; re-prompt to continue.")
                break

            # Otherwise: tool_use. Collect results.
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    print(f"  → {b.name} {json.dumps(b.input)[:160]}")
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": run_tool(b.name, b.input)})
            if not results:
                print(f"\n⚠️  No tool calls and stop_reason={stop!r}; stopping this turn.")
                break
            history.append({"role": "user", "content": results})

        # Follow-up turn
        msg = input("\nYou (or Enter to quit): ").strip()
        if not msg: break
        history.append({"role": "user", "content": msg})

if __name__ == "__main__":
    api_key = resolve_api_key()
    # max_retries=5 lets the SDK ride out brief 5xx/429 blips on its own. The
    # _call_api_with_retry() wrapper below adds a second, more patient net for
    # longer outages so a multi-minute hiccup doesn't drop the whole session.
    client = Anthropic(api_key=api_key, max_retries=5)
    params = startup_wizard()
    run_agent(params)
