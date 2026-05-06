#!/usr/bin/env python3
"""
web_ui.py — Local FastAPI web UI for sf-initial-setup-agent v0.3.0+.

Routes:
    GET  /                  → wizard form (or redirect to /dashboard if a session is running)
    POST /wizard            → persist config, redirect to /dashboard
    GET  /dashboard         → retrieve dashboard (chunk progress table)
    POST /pick-project-dir  → native folder picker (osascript on macOS)
    POST /start-retrieve    → spawn retrieve_metadata.py as subprocess
    POST /cancel-retrieve   → terminate the running subprocess
    WS   /ws/progress       → live chunk events (replays history on reconnect)
    GET  /summary           → final package + chunk summary
    GET  /static/*          → vendored CSS/JS

Bind discipline: ALWAYS 127.0.0.1 (loopback bypasses the macOS app firewall prompt).
Standalone test: `python web_ui.py` — picks a free port, opens nothing; user types URL.
The orchestrator (step 5) drives this module — opens browser, monitors session state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn


_SCRIPT_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _SCRIPT_DIR / "templates"
_STATIC_DIR = _SCRIPT_DIR / "static"
_RETRIEVE_SCRIPT = _SCRIPT_DIR / "retrieve_metadata.py"
_TROUBLESHOOT_SCRIPT = _SCRIPT_DIR / "troubleshoot.py"
_CONFIG_PATH = Path("~/.sf-initial-setup-agent-config.json").expanduser()
AGENT_NAME = "sf-initial-setup-agent"

DEFAULT_PORT = 8765
PORT_ATTEMPTS = 10


# ── State ───────────────────────────────────────────────────────────────────────

@dataclass
class RetrieveSession:
    config: dict[str, Any]
    started_at: datetime
    events: list[dict] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    proc: Optional[asyncio.subprocess.Process] = None
    state: str = "running"          # running | done | failed | cancelled
    returncode: Optional[int] = None
    summary: Optional[dict] = None  # populated from manifest/retrieve-summary.json after exit

    async def push(self, event: dict) -> None:
        self.events.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


@dataclass
class AppState:
    config: dict[str, Any]
    session: Optional[RetrieveSession] = None


def _load_config() -> dict[str, Any]:
    if _CONFIG_PATH.is_file():
        try:
            with _CONFIG_PATH.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "alias": "",
        "project_parent_dir": "",
        "include_managed": True,
        "exclude_expired_packages": False,
        "exclude_namespaces": "",
        "concurrency": 6,
        "chunk_size": 1500,
        "wait_minutes": 60,
    }


def _save_config(config: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _CONFIG_PATH.open("w") as f:
        json.dump(config, f, indent=2)


def _project_dir_from_config(cfg: dict[str, Any]) -> Path:
    parent = Path(cfg["project_parent_dir"]).expanduser()
    return parent / f"{cfg['alias']}-metadata"


state = AppState(config=_load_config())


# ── Project-setup helpers ───────────────────────────────────────────────────────

def _has_api_key() -> bool:
    if os.environ.get("FIVEGL_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return True
    env_file = Path("~/.5gl-agents-env").expanduser()
    if env_file.is_file():
        try:
            return "ANTHROPIC_API_KEY" in env_file.read_text()
        except OSError:
            return False
    return False


def detect_api_version(alias: str) -> str:
    """Pick the max API version supported by the org. Returns e.g. '62.0'."""
    sf_proc = subprocess.run(
        ["sf", "org", "display", "--target-org", alias, "--json"],
        capture_output=True, text=True, timeout=30,
    )
    if sf_proc.returncode != 0:
        raise RuntimeError(f"sf org display failed: {sf_proc.stderr or sf_proc.stdout}")
    data = json.loads(sf_proc.stdout)
    instance_url = data.get("result", {}).get("instanceUrl")
    if not instance_url:
        raise RuntimeError("sf org display did not return instanceUrl")
    with urllib.request.urlopen(f"{instance_url}/services/data", timeout=15) as resp:
        versions = json.loads(resp.read())
    return max(versions, key=lambda v: float(v["version"]))["version"]


def ensure_scaffolded(project_dir: Path, alias: str) -> dict:
    """Create SFDX project if missing; pin sourceApiVersion to org max. Idempotent."""
    sfdx_proj = project_dir / "sfdx-project.json"
    scaffolded_now = False
    if not sfdx_proj.is_file():
        parent = project_dir.parent
        name = project_dir.name
        parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["sf", "project", "generate", "--name", name, "--output-dir", str(parent),
             "--default-package-dir", "force-app"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"`sf project generate` failed: {result.stderr or result.stdout}")
        scaffolded_now = True

    api_version = detect_api_version(alias)
    with sfdx_proj.open() as f:
        proj = json.load(f)
    proj["sourceApiVersion"] = api_version
    with sfdx_proj.open("w") as f:
        json.dump(proj, f, indent=2)
    return {"scaffolded_now": scaffolded_now, "api_version": api_version}


def _manifest_hash(manifest_dir: Path) -> str:
    """Hex digest of the sorted union of (type, member) tuples across all chunk manifests.

    Per master decisions: this is the `manifest_hash` field of `.5gl-sync-state.json`.
    """
    from xml.etree import ElementTree as ET
    XMLNS = "http://soap.sforce.com/2006/04/metadata"
    tuples: set[tuple[str, str]] = set()
    if not manifest_dir.is_dir():
        return ""
    for path in sorted(manifest_dir.glob("*.xml")):
        try:
            tree = ET.parse(path)
        except ET.ParseError:
            continue
        for types in tree.findall(f"{{{XMLNS}}}types"):
            name_el = types.find(f"{{{XMLNS}}}name")
            type_name = (name_el.text or "").strip() if name_el is not None else ""
            for mem in types.findall(f"{{{XMLNS}}}members"):
                if mem.text:
                    tuples.add((type_name, mem.text.strip()))
    h = hashlib.sha256()
    for t, m in sorted(tuples):
        h.update(f"{t}:{m}\n".encode())
    return h.hexdigest()


def _agent_version() -> str:
    """Read AGENT_VERSION from sf_initial_setup_agent.py (avoids circular import)."""
    try:
        agent_module = _SCRIPT_DIR / "sf_initial_setup_agent.py"
        if agent_module.is_file():
            for line in agent_module.read_text().splitlines():
                if line.startswith("AGENT_VERSION"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "unknown"


def write_sync_state(project_dir: Path, alias: str, summary: dict) -> Path:
    """Write <project>/.5gl-sync-state.json. Per master decisions log (2026-05-02)."""
    org_id = ""
    username = ""
    try:
        proc = subprocess.run(
            ["sf", "org", "display", "--target-org", alias, "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            org_data = json.loads(proc.stdout).get("result", {})
            org_id = org_data.get("id", "")
            username = org_data.get("username", "")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    payload = {
        "alias": alias,
        "org_id": org_id,
        "username": username,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent_name": AGENT_NAME,
        "agent_version": _agent_version(),
        "manifest_hash": _manifest_hash(project_dir / "manifest"),
        "file_count": summary.get("totals", {}).get("files_retrieved", 0),
    }
    sync_path = project_dir / ".5gl-sync-state.json"
    with sync_path.open("w") as f:
        json.dump(payload, f, indent=2)
    return sync_path


# ── App lifecycle ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    sess = state.session
    if sess and sess.proc and sess.proc.returncode is None:
        sess.proc.terminate()
        try:
            await asyncio.wait_for(sess.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            sess.proc.kill()


app = FastAPI(title="sf-initial-setup-agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── Routes: wizard ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if state.session is not None and state.session.state == "running":
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("wizard.html", {
        "request": request,
        "config": state.config,
    })


@app.post("/wizard")
async def wizard_submit(
    alias: str = Form(...),
    project_parent_dir: str = Form(...),
    include_managed: str = Form(""),
    exclude_expired_packages: str = Form(""),
    exclude_namespaces: str = Form(""),
    concurrency: int = Form(6),
    chunk_size: int = Form(1500),
    wait_minutes: int = Form(60),
):
    state.config = {
        "alias": alias.strip(),
        "project_parent_dir": project_parent_dir.strip(),
        "include_managed": include_managed == "on",
        "exclude_expired_packages": exclude_expired_packages == "on",
        "exclude_namespaces": exclude_namespaces.strip(),
        "concurrency": concurrency,
        "chunk_size": chunk_size,
        "wait_minutes": wait_minutes,
    }
    _save_config(state.config)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/list-orgs")
async def list_orgs():
    """Run `sf org list --json` and return a flat list of authed orgs."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "sf", "org", "list", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (asyncio.TimeoutError, FileNotFoundError) as e:
        return JSONResponse({"orgs": [], "error": f"sf org list failed: {e}"})

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        return JSONResponse({"orgs": [], "error": "could not parse `sf org list` output"})

    result = data.get("result", {}) if isinstance(data.get("result"), dict) else {}
    orgs: list[dict] = []
    seen_aliases: set[str] = set()
    for category in ("nonScratchOrgs", "sandboxes", "scratchOrgs", "devHubs", "other"):
        for o in result.get(category, []) or []:
            alias = (o.get("alias") or "").strip()
            if not alias or alias in seen_aliases:
                continue
            seen_aliases.add(alias)
            instance_url = o.get("instanceUrl") or ""
            is_sandbox = (
                category == "sandboxes"
                or "sandbox" in instance_url.lower()
                or "test.salesforce.com" in instance_url.lower()
            )
            orgs.append({
                "alias": alias,
                "username": o.get("username") or "",
                "instance_url": instance_url,
                "is_sandbox": is_sandbox,
                "is_default": bool(o.get("isDefaultUsername") or o.get("isDefault")),
            })
    orgs.sort(key=lambda o: o["alias"].lower())
    return JSONResponse({"orgs": orgs})


@app.post("/auth-org")
async def auth_org(
    alias: str = Form(...),
    sandbox: str = Form(""),
    instance_url: str = Form(""),
):
    """Run `sf org login web` as a subprocess. Blocks until the user completes OAuth in their browser."""
    alias = alias.strip()
    if not alias:
        raise HTTPException(status_code=400, detail="alias is required")

    cmd = ["sf", "org", "login", "web", "--alias", alias]
    instance_url = instance_url.strip()
    if instance_url:
        cmd += ["--instance-url", instance_url]
    elif sandbox == "on":
        cmd += ["--instance-url", "https://test.salesforce.com"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise HTTPException(status_code=408,
                            detail="OAuth flow timed out (10 min). Cancel any open Salesforce login tab and try again.")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="`sf` CLI not found on PATH.")

    if proc.returncode != 0:
        err = (stderr.decode(errors="replace") or stdout.decode(errors="replace")).strip()
        raise HTTPException(status_code=400,
                            detail=f"sf org login web failed: {err[-500:] or 'unknown error'}")

    return JSONResponse({"alias": alias, "success": True})


@app.post("/pick-project-dir")
async def pick_project_dir():
    """Trigger native folder picker. macOS only for now (per master memory: macOS-only agent)."""
    sysname = platform.system()
    if sysname != "Darwin":
        raise HTTPException(
            status_code=501,
            detail=f"Folder picker not implemented for {sysname}; type the path manually.",
        )
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "Pick project parent directory")'],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Folder picker timed out")
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail="Folder selection cancelled")
    path = result.stdout.strip().rstrip("/")
    return JSONResponse({"path": path})


# ── Routes: dashboard / retrieve control ────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "config": state.config,
        "session": state.session,
        "project_dir": str(_project_dir_from_config(state.config)) if state.config.get("alias") else "",
    })


@app.post("/start-retrieve")
async def start_retrieve():
    if state.session is not None and state.session.state == "running":
        raise HTTPException(status_code=409, detail="Retrieve already running")
    cfg = state.config
    if not cfg.get("alias") or not cfg.get("project_parent_dir"):
        raise HTTPException(status_code=400, detail="Configure alias and project parent dir via the wizard first")

    project_dir = _project_dir_from_config(cfg)
    try:
        ensure_scaffolded(project_dir, cfg["alias"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Project setup failed: {e}")

    args = [
        sys.executable, str(_RETRIEVE_SCRIPT),
        "--alias", cfg["alias"],
        "--directory", str(project_dir),
        "--include-managed", "true" if cfg.get("include_managed", True) else "false",
        "--concurrency", str(cfg.get("concurrency", 6)),
        "--chunk-size", str(cfg.get("chunk_size", 1500)),
        "--wait-minutes", str(cfg.get("wait_minutes", 60)),
    ]
    if cfg.get("exclude_expired_packages"):
        args.append("--exclude-expired-packages")
    if cfg.get("exclude_namespaces"):
        args.extend(["--exclude-namespaces", cfg["exclude_namespaces"]])

    sess = RetrieveSession(config=cfg, started_at=datetime.now(timezone.utc))
    state.session = sess
    sess.proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    asyncio.create_task(_consume_subprocess(sess, project_dir))
    return JSONResponse({"started": True, "pid": sess.proc.pid})


@app.post("/cancel-retrieve")
async def cancel_retrieve():
    sess = state.session
    if not sess or sess.state != "running":
        raise HTTPException(status_code=409, detail="No retrieve to cancel")
    if sess.proc and sess.proc.returncode is None:
        sess.proc.terminate()
        sess.state = "cancelled"
        await sess.push({
            "event": "cancelled",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    return JSONResponse({"cancelled": True})


@app.get("/summary", response_class=HTMLResponse)
async def summary(request: Request):
    sess = state.session
    return templates.TemplateResponse("summary.html", {
        "request": request,
        "session": sess,
        "summary": sess.summary if sess else None,
    })


# ── WebSocket ───────────────────────────────────────────────────────────────────

@app.websocket("/ws/progress")
async def ws_progress(ws: WebSocket):
    await ws.accept()
    sess = state.session
    if sess is None:
        await ws.send_json({"event": "no_session"})
        await ws.close()
        return

    for event in list(sess.events):
        await ws.send_json(event)

    queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
    sess.subscribers.add(queue)
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        sess.subscribers.discard(queue)


# ── Subprocess plumbing ─────────────────────────────────────────────────────────

async def _consume_subprocess(sess: RetrieveSession, project_dir: Path) -> None:
    """Read stderr (event JSON-lines) and stdout (summary text) from retrieve_metadata.py."""
    proc = sess.proc
    assert proc is not None

    async def reader(stream: Optional[asyncio.StreamReader], kind: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            if kind == "stderr":
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    event = {"event": "log", "stream": "stderr", "message": text}
                await sess.push(event)
            else:
                await sess.push({"event": "stdout", "message": text})

    await asyncio.gather(reader(proc.stdout, "stdout"), reader(proc.stderr, "stderr"))
    rc = await proc.wait()
    sess.returncode = rc
    if sess.state == "running":
        sess.state = "done" if rc == 0 else "failed"

    summary_path = project_dir / "manifest" / "retrieve-summary.json"
    if summary_path.is_file():
        try:
            with summary_path.open() as f:
                sess.summary = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    failed = sess.summary.get("totals", {}).get("failed", 0) if sess.summary else 0
    if failed == 0 and rc == 0:
        try:
            sync_path = write_sync_state(project_dir, sess.config["alias"], sess.summary or {})
            await sess.push({"event": "sync_state_written", "path": str(sync_path)})
        except Exception as e:
            await sess.push({"event": "sync_state_error", "error": str(e)})

    if failed > 0:
        if _has_api_key():
            await sess.push({"event": "troubleshooter_starting", "failed_count": failed})
            await _run_troubleshooter(sess, project_dir)
            if summary_path.is_file():
                try:
                    with summary_path.open() as f:
                        sess.summary = json.load(f)
                except (OSError, json.JSONDecodeError):
                    pass
            new_failed = sess.summary.get("totals", {}).get("failed", 0) if sess.summary else failed
            if new_failed == 0:
                sess.state = "done"
                try:
                    sync_path = write_sync_state(project_dir, sess.config["alias"], sess.summary or {})
                    await sess.push({"event": "sync_state_written", "path": str(sync_path)})
                except Exception as e:
                    await sess.push({"event": "sync_state_error", "error": str(e)})
        else:
            await sess.push({
                "event": "troubleshooter_skipped",
                "reason": "No Anthropic API key (env or ~/.5gl-agents-env). Inspect logs in manifest/logs/ manually.",
            })

    await sess.push({
        "event": "process_exited",
        "returncode": rc,
        "state": sess.state,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


async def _run_troubleshooter(sess: RetrieveSession, project_dir: Path) -> None:
    cmd = [
        sys.executable, str(_TROUBLESHOOT_SCRIPT),
        "--alias", sess.config["alias"],
        "--directory", str(project_dir),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    sess.proc = proc  # cancel still works mid-troubleshooter

    async def reader(stream: Optional[asyncio.StreamReader], kind: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            if kind == "stderr":
                try:
                    event = json.loads(text)
                    event["source"] = "troubleshooter"
                except json.JSONDecodeError:
                    event = {"event": "log", "source": "troubleshooter", "message": text}
                await sess.push(event)
            else:
                await sess.push({"event": "stdout", "source": "troubleshooter", "message": text})

    await asyncio.gather(reader(proc.stdout, "stdout"), reader(proc.stderr, "stderr"))
    rc = await proc.wait()
    await sess.push({
        "event": "troubleshooter_exited",
        "returncode": rc,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


# ── Server entry points ─────────────────────────────────────────────────────────

def find_free_port(start: int = DEFAULT_PORT, attempts: int = PORT_ATTEMPTS) -> int:
    for offset in range(attempts):
        candidate = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError(f"No free port in {start}..{start + attempts - 1}")


def start_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    port = find_free_port()
    print(f"Web UI: http://127.0.0.1:{port}")
    start_server("127.0.0.1", port)
