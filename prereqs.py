#!/usr/bin/env python3
"""
prereqs.py — Detect and (with consent) install prerequisites for sf-initial-setup-agent.

Standalone:    python prereqs.py [--check-only] [--quiet] [--yes]
Importable:    from prereqs import check_all, install_one, PREREQS

Targets macOS primarily; also handles Windows-via-WSL Ubuntu (adds `wslu` for browser launching).

Stdlib-only by design: this script must run BEFORE Python deps are installed, so it
cannot depend on rich, requests, or anything outside the standard library.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REQUIREMENTS = os.path.join(_SCRIPT_DIR, "requirements.txt")
_PIP_INSTALL_CMD = f'"{sys.executable}" -m pip install -r "{_REQUIREMENTS}"'


# ── ANSI colors ─────────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def green(t):  return _c(t, "32")
def red(t):    return _c(t, "31")
def yellow(t): return _c(t, "33")
def cyan(t):   return _c(t, "36")
def dim(t):    return _c(t, "2")
def bold(t):   return _c(t, "1")


# ── Platform detection ──────────────────────────────────────────────────────────

def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _is_wsl() -> bool:
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except (FileNotFoundError, PermissionError):
        return False


# ── Detection helpers ───────────────────────────────────────────────────────────

def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def _run(cmd: list[str], timeout: int = 10) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return ((out.stdout or "") + (out.stderr or "")).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def detect_python() -> Optional[str]:
    if sys.version_info < (3, 9):
        return None
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def detect_command(cmd: str, version_args: Optional[list[str]] = None) -> Optional[str]:
    if not _which(cmd):
        return None
    out = _run([cmd] + (version_args or ["--version"]))
    if not out:
        return "present"
    return out.splitlines()[0]


def detect_java() -> Optional[str]:
    if not _which("java"):
        return None
    out = _run(["java", "-version"])
    return out.splitlines()[0] if out else "present"


def detect_pip_pkg(pkg: str) -> Optional[str]:
    out = _run([sys.executable, "-m", "pip", "show", pkg])
    if not out:
        return None
    for line in out.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def detect_python_deps() -> Optional[str]:
    required = ["anthropic", "fastapi", "uvicorn", "jinja2"]
    missing = [pkg for pkg in required if not detect_pip_pkg(pkg)]
    if missing:
        return None
    return f"all {len(required)} installed"


def detect_anthropic_key() -> Optional[str]:
    for n in ("DIGADOP_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        if os.environ.get(n):
            return f"set ({n})"
    key_file = os.path.expanduser("~/.digadop-agents-env")
    if os.path.isfile(key_file):
        try:
            with open(key_file) as f:
                content = f.read()
            if "ANTHROPIC_API_KEY" in content:
                return "in ~/.digadop-agents-env"
        except OSError:
            pass
    return None


# ── Prereq list ─────────────────────────────────────────────────────────────────

@dataclass
class Prereq:
    name: str
    why: str
    detect: Callable[[], Optional[str]]
    install_cmd: Optional[str] = None
    optional: bool = False
    notes: Optional[str] = None


PREREQS: list[Prereq] = [
    Prereq(
        name="Python 3.9+",
        why="runs the agent",
        detect=detect_python,
        install_cmd=None,
        notes="Already present on macOS. If too old: brew install python@3.12",
    ),
    Prereq(
        name="Homebrew",
        why="package manager used for node/jq/java",
        detect=lambda: detect_command("brew"),
        install_cmd='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
    ),
    Prereq(
        name="Node.js",
        why="needed to install Salesforce CLI via npm",
        detect=lambda: detect_command("node"),
        install_cmd="brew install node",
    ),
    Prereq(
        name="Salesforce CLI (sf)",
        why="org auth and metadata retrieve — the core of this agent",
        detect=lambda: detect_command("sf"),
        install_cmd="npm install -g @salesforce/cli",
    ),
    Prereq(
        name="Java (Temurin 17+)",
        why="some sf retrieves invoke a JVM-backed metadata packager",
        detect=detect_java,
        install_cmd="brew install --cask temurin",
    ),
    Prereq(
        name="jq",
        why="JSON parsing in shell helpers",
        detect=lambda: detect_command("jq"),
        install_cmd="brew install jq",
    ),
    Prereq(
        name="Python deps",
        why="anthropic, fastapi, uvicorn, jinja2 (in current Python env)",
        detect=detect_python_deps,
        install_cmd=_PIP_INSTALL_CMD,
    ),
    Prereq(
        name="Anthropic API key",
        why="needed only by the AI troubleshooter; happy path runs without it",
        detect=detect_anthropic_key,
        install_cmd=None,
        optional=True,
        notes="Agent prompts and persists to ~/.digadop-agents-env on first run if missing.",
    ),
]


if _is_wsl():
    PREREQS.append(Prereq(
        name="wslu (browser launcher)",
        why="provides `wslview` so run.sh can open the Windows-side browser",
        detect=lambda: detect_command("wslview"),
        install_cmd="sudo apt install -y wslu",
    ))


# ── Main flow ───────────────────────────────────────────────────────────────────

def _confirm(prompt: str) -> bool:
    try:
        ans = input(prompt + " [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def install_one(p: Prereq) -> bool:
    if not p.install_cmd:
        print(red(f"  ✗ no automatic install for {p.name}"))
        if p.notes:
            print(dim(f"    {p.notes}"))
        return False
    print(cyan(f"  → running: {p.install_cmd}"))
    try:
        result = subprocess.run(p.install_cmd, shell=True, check=False)
    except KeyboardInterrupt:
        print()
        print(red("  ✗ install interrupted"))
        return False
    if result.returncode == 0:
        print(green(f"  ✓ {p.name} installed"))
        return True
    print(red(f"  ✗ install of {p.name} failed (exit {result.returncode})"))
    if p.notes:
        print(dim(f"    {p.notes}"))
    return False


def check_all() -> list[tuple[Prereq, Optional[str]]]:
    """Pure check — no side effects. Returns (prereq, current_version_or_None)."""
    return [(p, p.detect()) for p in PREREQS]


def print_status(results: list[tuple[Prereq, Optional[str]]]) -> None:
    print()
    print(bold("Prerequisite check"))
    print()
    name_w = max(len(p.name) for p, _ in results) + 2
    for p, v in results:
        if v:
            mark, detail = green("✓"), dim(str(v))
        elif p.optional:
            mark, detail = yellow("○"), yellow("missing (optional)")
        else:
            mark, detail = red("✗"), red("missing")
        print(f"  {mark} {p.name:<{name_w}} {detail}")
    print()


def install_missing_interactively(
    missing: list[tuple["Prereq", Optional[str]]],
    auto_yes: bool = False,
) -> None:
    """Prompt-and-install loop. Used by main() and by the orchestrator."""
    already_run: set[str] = set()
    for p, _ in missing:
        print()
        print(bold(f"• {p.name}"))
        print(f"  {dim(p.why)}")
        if p.notes:
            print(f"  {dim(p.notes)}")
        if not p.install_cmd:
            print(red("  ✗ no automatic install — install manually then re-run"))
            continue
        if p.install_cmd in already_run:
            print(dim("  (install command already run for an earlier item)"))
            continue
        do_install = auto_yes or _confirm(f"  Install? Will run: {p.install_cmd}")
        if do_install:
            install_one(p)
            already_run.add(p.install_cmd)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect and install prerequisites for sf-initial-setup-agent."
    )
    parser.add_argument("--check-only", action="store_true",
                        help="Only check; don't prompt to install.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress the status table.")
    parser.add_argument("--yes", action="store_true",
                        help="Auto-confirm all install prompts.")
    args = parser.parse_args(argv)

    if not (_is_macos() or _is_wsl()):
        print(yellow(
            f"Warning: macOS and Windows-via-WSL only. Current: "
            f"{platform.system()} {platform.release()}"
        ))

    results = check_all()
    if not args.quiet:
        print_status(results)

    missing_required = [(p, v) for p, v in results if not v and not p.optional]
    missing_optional = [(p, v) for p, v in results if not v and p.optional]

    if not missing_required and not missing_optional:
        print(green(bold("All prerequisites satisfied.")))
        return 0

    if args.check_only:
        if missing_required:
            print(red(bold(
                f"{len(missing_required)} required prerequisite(s) missing."
            )))
            return 1
        return 0

    print(bold("Missing:"))
    if missing_required:
        print(f"  required: {len(missing_required)}")
    if missing_optional:
        print(f"  optional: {len(missing_optional)}")

    install_missing_interactively(missing_required + missing_optional, auto_yes=args.yes)

    print()
    print(bold("Re-checking..."))
    results2 = check_all()
    if not args.quiet:
        print_status(results2)

    still_missing_required = [p for p, v in results2 if not v and not p.optional]
    if still_missing_required:
        print(red(bold(
            f"{len(still_missing_required)} required prerequisite(s) still missing:"
        )))
        for p in still_missing_required:
            print(red(f"  ✗ {p.name}"))
        return 1
    print(green(bold("All required prerequisites satisfied.")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
