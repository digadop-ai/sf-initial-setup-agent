#!/usr/bin/env python3
"""
troubleshoot.py — Claude-powered troubleshooter for failed retrieve chunks.

Invoked by the orchestrator (or standalone) AFTER retrieve_metadata.py exits with
failures, only if an Anthropic API key is available. The "🔑 happy path never
spends it" model: a clean retrieve never calls this script.

CLI:
    python troubleshoot.py --alias myorg --directory ~/proj/myorg-metadata \
        [--mcp-doc salesforce-mcp-setup.md] [--max-rounds 30]

Reads:
    <directory>/manifest/retrieve-summary.json
    <directory>/manifest/logs/<chunk_id>.log
    salesforce-mcp-setup.md (optional system context — agent's own copy)

Writes (via Claude's tool calls):
    <directory>/manifest/<chunk_id>.xml             (manifest edits)
    <directory>/manifest/logs/<chunk_id>.retry-N.log (rerun_chunk)
    <directory>/manifest/retrieve-summary.json       (updated outcomes)

Emits structured progress events on stderr (one JSON per line) so the orchestrator
/ web UI can stream them.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import anthropic


MODEL = "claude-sonnet-4-6"
MAX_TOOLS_ROUNDS = 30
MAX_TOKENS_PER_TURN = 4096
DEFAULT_MCP_DOC_NAME = "salesforce-mcp-setup.md"
TOOL_FILE_READ_CAP = 50_000     # chars
TOOL_OUTPUT_TAIL = 20_000       # chars
SF_TIMEOUT_SECONDS = 600
SHELL_TIMEOUT_SECONDS = 300
RERUN_WAIT_MINUTES = 60

# Path safety — duplicated here for now; will be extracted to a shared module
# in step 5 (orchestrator refactor) so it's defined in exactly one place.
DENY_SUBSTRINGS = (
    "rm -rf", "rm -fr", "sudo ", "chmod 777", "curl ", "wget ",
    "| sh", "| bash", "> /etc/", "/dev/", "format ", "shutdown",
    "reboot", "dd if=", "mkfs", ":(){:|:&};:",
)


def _resolve_under(target: str, base: Path) -> Path:
    p = Path(target).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def _is_path_safe(target: Path, base: Path) -> bool:
    try:
        target = target.resolve()
        base = base.resolve()
    except (OSError, RuntimeError):
        return False
    return str(target) == str(base) or str(target).startswith(str(base) + os.sep)


def _is_command_safe(cmd: str) -> tuple[bool, Optional[str]]:
    lowered = cmd.lower()
    for substr in DENY_SUBSTRINGS:
        if substr in lowered:
            return False, f"command contains denylisted pattern: {substr.strip()!r}"
    return True, None


# ── Progress events ─────────────────────────────────────────────────────────────

def emit(event_type: str, **fields) -> None:
    payload = {
        "event": event_type,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    }
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stderr.flush()


# ── Tool definitions ────────────────────────────────────────────────────────────

def tool_schemas() -> list[dict]:
    return [
        {
            "name": "read_file",
            "description": "Read a file inside the project directory. Returns up to 50,000 chars.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the project dir, or absolute path within it.",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write or overwrite a file inside the project directory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "run_sf",
            "description": "Run a `sf` CLI subcommand. Pass args without the leading 'sf'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "e.g., ['org', 'display', '--target-org', 'myorg', '--json']",
                    },
                },
                "required": ["args"],
            },
        },
        {
            "name": "run_shell",
            "description": "Run a shell command in the project dir. Denylist applied (no rm -rf, sudo, curl, etc.).",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
        {
            "name": "rerun_chunk",
            "description": (
                "Re-execute a single failed chunk after the troubleshooter has investigated. "
                "Returns success status, file count, and the new log path. The chunk's manifest "
                "is read from <project>/manifest/<chunk_id>.xml unless overridden."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "chunk_id": {
                        "type": "string",
                        "description": "e.g., 'chunk-007' or 'profile-002'",
                    },
                    "manifest_path": {
                        "type": "string",
                        "description": "Optional override path; defaults to manifest/<chunk_id>.xml",
                    },
                },
                "required": ["chunk_id"],
            },
        },
    ]


# ── Tool implementations ────────────────────────────────────────────────────────

def tool_read_file(args: dict, project_dir: Path) -> dict:
    path = _resolve_under(args["path"], project_dir)
    if not _is_path_safe(path, project_dir):
        return {"error": f"path outside project dir: {path}"}
    if not path.is_file():
        return {"error": f"file not found: {path}"}
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return {"error": str(e)}
    if len(text) > TOOL_FILE_READ_CAP:
        return {
            "content": text[:TOOL_FILE_READ_CAP],
            "truncated": True,
            "total_bytes": len(text),
        }
    return {"content": text}


def tool_write_file(args: dict, project_dir: Path) -> dict:
    path = _resolve_under(args["path"], project_dir)
    if not _is_path_safe(path, project_dir):
        return {"error": f"path outside project dir: {path}"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"])
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True, "bytes_written": len(args["content"]), "path": str(path)}


def tool_run_sf(args: dict, project_dir: Path) -> dict:
    sf_args = args["args"]
    if not isinstance(sf_args, list) or not all(isinstance(a, str) for a in sf_args):
        return {"error": "args must be a list of strings"}
    try:
        result = subprocess.run(
            ["sf"] + sf_args,
            capture_output=True, text=True,
            cwd=project_dir, timeout=SF_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"sf command exceeded {SF_TIMEOUT_SECONDS}s"}
    except FileNotFoundError:
        return {"error": "sf CLI not found on PATH"}
    return {
        "stdout": result.stdout[-TOOL_OUTPUT_TAIL:],
        "stderr": result.stderr[-5000:],
        "returncode": result.returncode,
    }


def tool_run_shell(args: dict, project_dir: Path) -> dict:
    cmd = args["command"]
    safe, msg = _is_command_safe(cmd)
    if not safe:
        return {"error": f"refused: {msg}"}
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=project_dir, timeout=SHELL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"shell command exceeded {SHELL_TIMEOUT_SECONDS}s"}
    return {
        "stdout": result.stdout[-10000:],
        "stderr": result.stderr[-3000:],
        "returncode": result.returncode,
    }


def tool_rerun_chunk(
    args: dict,
    alias: str,
    project_dir: Path,
    manifest_dir: Path,
    log_dir: Path,
    summary_path: Path,
) -> dict:
    chunk_id = args["chunk_id"]
    manifest_path = Path(args.get("manifest_path") or (manifest_dir / f"{chunk_id}.xml"))
    if not manifest_path.is_absolute():
        manifest_path = project_dir / manifest_path
    manifest_path = manifest_path.resolve()

    if not _is_path_safe(manifest_path, project_dir):
        return {"error": f"manifest path outside project dir: {manifest_path}"}
    if not manifest_path.is_file():
        return {"error": f"manifest not found: {manifest_path}"}

    log_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (log_dir / f"{chunk_id}.retry-{n}.log").exists():
        n += 1
    log_path = log_dir / f"{chunk_id}.retry-{n}.log"

    cmd = [
        "sf", "project", "retrieve", "start",
        "--manifest", str(manifest_path),
        "--target-org", alias,
        "--wait", str(RERUN_WAIT_MINUTES),
        "--ignore-conflicts",
        "--json",
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=project_dir, timeout=RERUN_WAIT_MINUTES * 60 + 60,
        )
    except subprocess.TimeoutExpired:
        return {"error": "rerun_chunk subprocess timeout"}
    elapsed = time.time() - started

    log_path.write_text(
        f"# {chunk_id} retry-{n}  elapsed={elapsed:.1f}s  exit={proc.returncode}\n"
        f"# cmd: {' '.join(cmd)}\n\n"
        f"=== STDOUT ===\n{proc.stdout}\n=== STDERR ===\n{proc.stderr}\n"
    )

    files_retrieved = 0
    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        files_retrieved = len(data.get("result", {}).get("files", []))
    except json.JSONDecodeError:
        pass

    success = proc.returncode == 0

    if summary_path.is_file():
        try:
            with summary_path.open() as f:
                summary = json.load(f)
            for c in summary.get("chunks", []):
                if c.get("chunk_id") == chunk_id:
                    c["success"] = success
                    c["files_retrieved"] = files_retrieved
                    c["elapsed_s"] = round(elapsed, 1)
                    c["log_path"] = str(log_path)
                    c["retried"] = True
                    c["error"] = None if success else (proc.stderr or proc.stdout or "")[-500:]
                    break
            chunks = summary.get("chunks", [])
            summary["totals"] = {
                "succeeded": sum(1 for c in chunks if c.get("success")),
                "failed": sum(1 for c in chunks if not c.get("success")),
                "files_retrieved": sum(c.get("files_retrieved", 0) for c in chunks if c.get("success")),
            }
            with summary_path.open("w") as f:
                json.dump(summary, f, indent=2)
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    emit("chunk_retried",
         chunk_id=chunk_id, success=success,
         files=files_retrieved, elapsed_s=round(elapsed, 1),
         log_path=str(log_path))

    return {
        "success": success,
        "files_retrieved": files_retrieved,
        "elapsed_s": round(elapsed, 1),
        "log_path": str(log_path),
        "stderr_tail": proc.stderr[-1000:] if not success else "",
    }


# ── API client w/ retry ─────────────────────────────────────────────────────────

def call_api_with_retry(client: anthropic.Anthropic, **kwargs) -> Any:
    delay = 1.0
    for attempt in range(5):
        try:
            return client.messages.create(**kwargs)
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as e:
            if attempt == 4:
                raise
            emit("api_retry", attempt=attempt + 1, error=type(e).__name__)
            time.sleep(delay)
            delay *= 2
        except anthropic.APIStatusError as e:
            if e.status_code in (500, 502, 503, 504, 529) and attempt < 4:
                emit("api_retry", attempt=attempt + 1, status=e.status_code)
                time.sleep(delay)
                delay *= 2
                continue
            raise


# ── Key resolution ──────────────────────────────────────────────────────────────

def _resolve_api_key() -> Optional[str]:
    for name in ("DIGADOP_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        v = os.environ.get(name)
        if v:
            return v
    env_file = Path("~/.digadop-agents-env").expanduser()
    if env_file.is_file():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in ("DIGADOP_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
                return v.strip().strip('"').strip("'")
    return None


# ── Initial context ─────────────────────────────────────────────────────────────

def build_user_brief(summary: dict, project_dir: Path) -> str:
    failed = [c for c in summary.get("chunks", []) if not c.get("success")]
    totals = summary.get("totals", {})
    lines = [
        f"A `sf project retrieve` run completed with {totals.get('failed', 0)} failed chunk(s) "
        f"out of {totals.get('succeeded', 0) + totals.get('failed', 0)} total.",
        "",
        f"Project directory: `{project_dir}`",
        "",
        "## Failed chunks",
        "",
    ]
    for c in failed:
        label = c.get("type_label") or c.get("primary_type") or ""
        header = f"- **{c['chunk_id']}**"
        if label:
            header += f" — {label}"
        header += f" — {c.get('error') or 'no error message captured'}"
        lines.append(header)
        lines.append(f"  - log: `{c.get('log_path') or 'n/a'}`")
        lines.append(f"  - members attempted: {c.get('members_attempted', 0)}")
        lines.append(f"  - elapsed: {c.get('elapsed_s', 0)}s")
        lines.append(f"  - already retried: {c.get('retried', False)}")
        warnings = c.get("warnings") or []
        if warnings:
            cats: dict[str, int] = {}
            for w in warnings:
                cats[w.get("category", "other")] = cats.get(w.get("category", "other"), 0) + 1
            cat_str = ", ".join(f"{k}={v}" for k, v in sorted(cats.items()))
            lines.append(f"  - warnings: {cat_str}")
    lines += [
        "",
        "## Your job",
        "Investigate. Use `read_file` to inspect logs and manifests. Diagnose the failure. "
        "If you can fix it (e.g., remove a problematic member from the chunk's manifest with `write_file`), "
        "do so, then call `rerun_chunk` to retry. Use `run_sf` and `run_shell` for ad-hoc diagnosis.",
        "",
        "Be terse. Don't narrate every step. When you've fixed what you can or determined a failure "
        "is unfixable, send a final summary message and stop calling tools.",
    ]
    return "\n".join(lines)


def build_system_prompt(alias: str, project_dir: Path, mcp_doc_path: Optional[Path]) -> str:
    parts = [
        "You are a Salesforce metadata retrieve troubleshooter.",
        f"Org alias: {alias}",
        f"Project directory: {project_dir}",
        "",
        "Tools:",
        "- read_file(path): inspect logs, manifests, sfdx-project.json, anything in the project dir.",
        "- write_file(path, content): edit chunk manifests if needed.",
        "- run_sf(args): run any `sf` subcommand for diagnosis.",
        "- run_shell(command): ad-hoc shell. Denylist applied.",
        "- rerun_chunk(chunk_id, manifest_path?): re-execute a chunk after a fix.",
        "",
        "Be terse. Investigate, fix, re-run, report. Stop calling tools when you're done.",
    ]
    if mcp_doc_path and mcp_doc_path.is_file():
        try:
            doc_content = mcp_doc_path.read_text()
            parts.append("")
            parts.append("# Reference: salesforce-mcp-setup.md (Salesforce metadata gotchas)")
            parts.append("")
            parts.append(doc_content)
        except OSError:
            pass
    return "\n".join(parts)


# ── Main loop ───────────────────────────────────────────────────────────────────

def run_troubleshooter(
    alias: str,
    project_dir: Path,
    mcp_doc_path: Optional[Path] = None,
    max_rounds: int = MAX_TOOLS_ROUNDS,
) -> int:
    manifest_dir = project_dir / "manifest"
    log_dir = manifest_dir / "logs"
    summary_path = manifest_dir / "retrieve-summary.json"

    if not summary_path.is_file():
        emit("error", message=f"no retrieve-summary.json at {summary_path}")
        return 2

    try:
        with summary_path.open() as f:
            summary = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        emit("error", message=f"could not read summary: {e}")
        return 2

    failed = [c for c in summary.get("chunks", []) if not c.get("success")]
    if not failed:
        emit("nothing_to_do", message="all chunks succeeded")
        return 0

    api_key = _resolve_api_key()
    if not api_key:
        emit("no_api_key",
             message="No Anthropic API key (env or ~/.digadop-agents-env). Cannot troubleshoot.")
        return 3

    client = anthropic.Anthropic(api_key=api_key)
    emit("troubleshoot_started", failed_count=len(failed), model=MODEL)

    system_prompt = build_system_prompt(alias, project_dir, mcp_doc_path)
    messages: list[dict] = [{"role": "user", "content": build_user_brief(summary, project_dir)}]
    tools = tool_schemas()

    handlers = {
        "read_file":    lambda a: tool_read_file(a, project_dir),
        "write_file":   lambda a: tool_write_file(a, project_dir),
        "run_sf":       lambda a: tool_run_sf(a, project_dir),
        "run_shell":    lambda a: tool_run_shell(a, project_dir),
        "rerun_chunk":  lambda a: tool_rerun_chunk(a, alias, project_dir, manifest_dir, log_dir, summary_path),
    }

    for round_num in range(max_rounds):
        try:
            response = call_api_with_retry(
                client,
                model=MODEL,
                max_tokens=MAX_TOKENS_PER_TURN,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )
        except Exception as e:
            emit("api_error", error=str(e), error_type=type(e).__name__)
            return 4

        emit("claude_turn",
             round=round_num,
             stop_reason=response.stop_reason,
             input_tokens=getattr(response.usage, "input_tokens", None),
             output_tokens=getattr(response.usage, "output_tokens", None))

        tool_uses = []
        for block in response.content:
            if block.type == "text":
                emit("claude_message", round=round_num, text=block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn" or not tool_uses:
            emit("troubleshoot_done", rounds=round_num + 1)
            break

        tool_results = []
        for tu in tool_uses:
            handler = handlers.get(tu.name)
            emit("tool_call", name=tu.name, input=tu.input)
            if not handler:
                result = {"error": f"unknown tool: {tu.name}"}
            else:
                try:
                    result = handler(tu.input)
                except Exception as e:
                    result = {"error": f"tool raised: {type(e).__name__}: {e}"}
            emit("tool_result", name=tu.name, keys=list(result.keys()))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result),
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        emit("troubleshoot_max_rounds", limit=max_rounds)
        return 5

    return 0


# ── Entry point ─────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Claude-powered troubleshooter for failed retrieve chunks.",
    )
    parser.add_argument("--alias", required=True)
    parser.add_argument("--directory", required=True, help="SFDX project directory")
    parser.add_argument("--mcp-doc", default=DEFAULT_MCP_DOC_NAME,
                        help=f"Path to a Salesforce gotchas doc (default: {DEFAULT_MCP_DOC_NAME} alongside this script)")
    parser.add_argument("--max-rounds", type=int, default=MAX_TOOLS_ROUNDS,
                        help=f"Max tool-use rounds (default {MAX_TOOLS_ROUNDS})")
    args = parser.parse_args(argv)

    project_dir = Path(args.directory).expanduser().resolve()
    if not project_dir.is_dir():
        print(f"error: project dir does not exist: {project_dir}", file=sys.stderr)
        return 2

    mcp_doc_path = Path(args.mcp_doc).expanduser()
    if not mcp_doc_path.is_absolute():
        mcp_doc_path = Path(__file__).resolve().parent / mcp_doc_path
    if not mcp_doc_path.is_file():
        mcp_doc_path = None  # optional

    return run_troubleshooter(args.alias, project_dir, mcp_doc_path, args.max_rounds)


if __name__ == "__main__":
    sys.exit(main())
