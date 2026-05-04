#!/usr/bin/env python3
"""
retrieve_metadata.py — Standalone parallel metadata retriever for sf-initial-setup-agent.

Runnable directly with no LLM dependency:
    python retrieve_metadata.py --alias myorg --directory ~/projects/myorg-metadata \
        [--include-managed=true] [--exclude-expired-packages] \
        [--exclude-namespaces=ns1,ns2] [--concurrency=6] \
        [--chunk-size=1500] [--wait-minutes=60]

Emits structured progress events on stderr (one JSON object per line) for the
orchestrator / web UI to consume. Final summary on stdout.

Design notes:
- Wildcard `<members>*</members>` retrieves only the org's OWN namespace; managed
  metadata does NOT come back via wildcard. So we list members explicitly via
  `sf org list metadata` (which returns all namespaces) and put exact names in
  each chunk manifest.
- Folder-based types (Report/Dashboard/Document/EmailTemplate) need a SOQL pass
  on the `Folder` table — `sf org list metadata` doesn't enumerate folder contents.
- StandardValueSet doesn't appear in `sf org list metadata` at all — we hardcode
  the documented list.
- Profile retrieve is shape-driven: the contents you get back depend on what
  CustomObject/ApexClass/Layout/etc are in the SAME package.xml. We bundle Profile
  with all its shape drivers in a dedicated chunk.

Stdlib-only EXCEPT this is intended to run inside the agent's venv where deps
are installed. No third-party imports here, though, by choice — keeps it portable.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET


# ── Constants ───────────────────────────────────────────────────────────────────

DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CONCURRENCY = 6
DEFAULT_WAIT_MINUTES = 60
PROGRESS_INTERVAL_SECONDS = 5

XMLNS = "http://soap.sforce.com/2006/04/metadata"

# Folder.Type column → metadata API type name. Note Folder.Type uses 'Email'
# (not 'EmailTemplate') for email template folders.
FOLDER_TYPE_TO_METADATA_TYPE = {
    "Report": "Report",
    "Dashboard": "Dashboard",
    "Document": "Document",
    "Email": "EmailTemplate",
}

# Types whose members are enumerated via SOQL on Folder, not `sf org list metadata`.
FOLDER_BASED_METADATA_TYPES = set(FOLDER_TYPE_TO_METADATA_TYPE.values())

# Profile retrieve fidelity depends on these types being in the same package.xml.
PROFILE_SHAPE_DRIVERS = [
    "CustomObject",
    "ApexClass",
    "CustomApplication",
    "CustomTab",
    "CustomPermission",
    "Layout",
    "RecordType",
]

# Experience Cloud (formerly Communities). Pages, themes, branding sets, and
# component configs live as JSON files inside `ExperienceBundle` — NOT in
# `FlexiPage`. FlexiPage is for Lightning App Builder pages only (record pages,
# app pages, home pages); confusing the two is a common gotcha. The generic
# enumeration path below picks up every type returned by `sf org list metadata-types`,
# so all of these flow through correctly without special-casing — listed here so
# the design rationale is explicit and a sanity-check warning fires if the org
# has `Network` (= Experience Cloud is enabled) but no `ExperienceBundle` (= might
# indicate an old API version or a Site.com-only legacy site).
EXPERIENCE_CLOUD_TYPES = (
    "Network",                         # site definition: members, navigation, settings
    "ExperienceBundle",                # the bundle: pages (JSON), themes, branding, components
    "CustomSite",                      # site URL configuration
    "SiteDotCom",                      # legacy Site.com sites (pre-LWR/Aura templates)
    "NavigationMenu",                  # community navigation menus
    "ManagedTopics",                   # community topics
    "Branding",                        # branding sets
    "CommunityTemplateDefinition",     # legacy community templates
    "CommunityThemeDefinition",        # legacy community themes
)


# StandardValueSet doesn't appear in `sf org list metadata`. Hardcoded canonical list.
# Source: Salesforce metadata API docs, "Standard Value Set Names".
STANDARD_VALUE_SETS = [
    "AccountContactMultiRoles", "AccountContactRole", "AccountOwnership", "AccountRating",
    "AccountType", "AddressCountryCode", "AddressStateCode", "AssetStatus",
    "CampaignMemberStatus", "CampaignStatus", "CampaignType", "CaseContactRole",
    "CaseOrigin", "CasePriority", "CaseReason", "CaseStatus", "CaseType",
    "ContactRole", "ContractContactRole", "ContractStatus", "EntitlementType",
    "EventSubject", "EventType", "FiscalYearPeriodName", "FiscalYearPeriodPrefix",
    "FiscalYearQuarterName", "FiscalYearQuarterPrefix", "IdeaCategory1",
    "IdeaMultiCategory", "IdeaStatus", "IdeaThemeStatus", "Industry",
    "InvoiceStatus", "LeadSource", "LeadStatus", "OpportunityCompetitor",
    "OpportunityStage", "OpportunityType", "OrderStatus", "OrderType",
    "PartnerRole", "Product2Family", "QuestionOrigin1", "QuickTextCategory",
    "QuickTextChannel", "QuoteStatus", "RoleInTerritory2", "SalesTeamRole",
    "Salutation", "ServiceContractApprovalStatus", "SocialPostClassification",
    "SocialPostEngagementLevel", "SocialPostReviewedStatus", "SolutionStatus",
    "TaskPriority", "TaskStatus", "TaskSubject", "TaskType",
    "WorkOrderLineItemStatus", "WorkOrderPriority", "WorkOrderStatus",
]


# ── Progress events (stderr JSON-lines) ─────────────────────────────────────────

_event_lock = threading.Lock()


def emit(event_type: str, **fields) -> None:
    """Emit one JSON event line on stderr. Thread-safe."""
    payload = {
        "event": event_type,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    }
    line = json.dumps(payload, separators=(",", ":"))
    with _event_lock:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


# ── sf CLI wrappers ─────────────────────────────────────────────────────────────

class SfError(Exception):
    pass


def sf_json(args: list[str], timeout: int = 300) -> dict:
    """Run `sf <args> --json` and return parsed result. Raises SfError on failure."""
    cmd = ["sf"] + args + ["--json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SfError(f"sf command timed out: {' '.join(cmd)}")
    except FileNotFoundError:
        raise SfError("`sf` CLI not found on PATH. Install Salesforce CLI v2 first.")
    if not result.stdout.strip():
        raise SfError(f"sf returned empty output (exit {result.returncode}): {result.stderr}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise SfError(f"sf returned non-JSON: {result.stdout[:500]}") from e
    if data.get("status", 0) != 0:
        msg = data.get("message") or data.get("name") or "unknown error"
        raise SfError(f"sf reported error: {msg}")
    return data.get("result", data)


def sf_query(alias: str, soql: str, use_tooling: bool = False) -> list[dict]:
    args = ["data", "query", "--target-org", alias, "-q", soql]
    if use_tooling:
        args.append("--use-tooling-api")
    result = sf_json(args)
    return result.get("records", [])


def list_metadata_types(alias: str) -> list[str]:
    result = sf_json(["org", "list", "metadata-types", "--target-org", alias])
    types = result.get("metadataObjects", [])
    return sorted({t["xmlName"] for t in types if t.get("xmlName")})


def list_metadata_members(alias: str, type_name: str) -> list[dict]:
    """Returns list of {fullName, namespacePrefix, ...} across ALL namespaces."""
    try:
        result = sf_json(["org", "list", "metadata", "-m", type_name, "--target-org", alias])
    except SfError:
        return []
    if isinstance(result, list):
        return result
    return result.get("metadataObjects") or result.get("result") or []


# ── Org enumeration ─────────────────────────────────────────────────────────────

@dataclass
class OrgEnumeration:
    members_by_type: dict[str, list[str]] = field(default_factory=dict)
    folders: list[dict] = field(default_factory=list)
    installed_packages: list[dict] = field(default_factory=list)
    package_licenses: list[dict] = field(default_factory=list)


def enumerate_folders(alias: str, include_managed: bool) -> list[dict]:
    """Returns Folder rows usable for Report/Dashboard/Document/EmailTemplate retrieve."""
    soql = (
        "SELECT Id, Name, DeveloperName, Type, NamespacePrefix "
        "FROM Folder "
        "WHERE Type IN ('Report','Dashboard','Document','Email') "
        "AND DeveloperName != null"
    )
    rows = sf_query(alias, soql)
    if not include_managed:
        rows = [r for r in rows if not r.get("NamespacePrefix")]
    return rows


def enumerate_packages(alias: str) -> list[dict]:
    soql = (
        "SELECT Id, SubscriberPackageId, "
        "SubscriberPackage.Name, SubscriberPackage.NamespacePrefix "
        "FROM InstalledSubscriberPackage"
    )
    try:
        return sf_query(alias, soql, use_tooling=True)
    except SfError:
        return []


def enumerate_package_licenses(alias: str) -> list[dict]:
    soql = (
        "SELECT Id, NamespacePrefix, Status, AllowedLicenses, "
        "UsedLicenses, ExpirationDate "
        "FROM PackageLicense"
    )
    try:
        return sf_query(alias, soql)
    except SfError:
        return []


def enumerate_org(alias: str, include_managed: bool) -> OrgEnumeration:
    emit("enumerate_started")
    org = OrgEnumeration()

    types = list_metadata_types(alias)
    emit("metadata_types_listed", count=len(types))

    for t in types:
        if t in FOLDER_BASED_METADATA_TYPES:
            continue
        if t == "StandardValueSet":
            continue
        if t == "InstalledPackage" and not include_managed:
            continue
        members = list_metadata_members(alias, t)
        names: list[str] = []
        for m in members:
            full = m.get("fullName")
            if not full:
                continue
            ns = m.get("namespacePrefix")
            if ns and not include_managed:
                continue
            names.append(full)
        if names:
            org.members_by_type[t] = sorted(set(names))

    org.members_by_type["StandardValueSet"] = list(STANDARD_VALUE_SETS)

    org.folders = enumerate_folders(alias, include_managed)
    folder_members_by_type = _folder_members(org.folders)
    for type_name, members in folder_members_by_type.items():
        if members:
            org.members_by_type[type_name] = sorted(set(members))

    org.installed_packages = enumerate_packages(alias)
    org.package_licenses = enumerate_package_licenses(alias)

    # Experience Cloud sanity check: if Network is present (= EC is enabled in
    # the org) but ExperienceBundle isn't, the modern bundle metadata won't be
    # captured. Could mean the org pre-dates ExperienceBundle (API <47.0) or
    # uses Site.com-only legacy sites. Warn so the operator notices.
    if "Network" in org.members_by_type and "ExperienceBundle" not in org.members_by_type:
        emit("warn_experience_cloud",
             message=("Network present but ExperienceBundle missing; "
                      "Experience Cloud page content may not be captured. "
                      "Check API version and whether sites are LWR/Aura (ExperienceBundle) "
                      "or legacy Site.com (SiteDotCom)."),
             ec_types_found=sorted(t for t in EXPERIENCE_CLOUD_TYPES if t in org.members_by_type))

    total_members = sum(len(v) for v in org.members_by_type.values())
    emit("enumerate_done",
         type_count=len(org.members_by_type),
         member_count=total_members,
         package_count=len(org.installed_packages))
    return org


def _folder_members(folders: list[dict]) -> dict[str, list[str]]:
    """Convert Folder rows into per-type member lists.

    For each non-managed folder, emit '<FolderName>' and '<FolderName>/*' so
    both the folder itself and all items inside it retrieve. Manually add
    'unfiled$public' for Report and EmailTemplate.
    """
    out: dict[str, list[str]] = {t: [] for t in FOLDER_BASED_METADATA_TYPES}
    for row in folders:
        folder_type = row.get("Type")
        meta_type = FOLDER_TYPE_TO_METADATA_TYPE.get(folder_type)
        if not meta_type:
            continue
        name = row.get("DeveloperName") or row.get("Name")
        if not name:
            continue
        out[meta_type].append(name)
        out[meta_type].append(f"{name}/*")
    for meta_type in ("Report", "EmailTemplate"):
        out[meta_type].append("unfiled$public")
    return out


# ── Filtering ───────────────────────────────────────────────────────────────────

def expired_or_suspended_namespaces(licenses: list[dict]) -> set[str]:
    return {
        lic["NamespacePrefix"]
        for lic in licenses
        if lic.get("NamespacePrefix") and lic.get("Status") in ("Expired", "Suspended")
    }


def filter_members(
    members_by_type: dict[str, list[str]],
    excluded_namespaces: set[str],
) -> dict[str, list[str]]:
    if not excluded_namespaces:
        return members_by_type
    prefixes = tuple(f"{ns}__" for ns in excluded_namespaces)
    out: dict[str, list[str]] = {}
    for t, members in members_by_type.items():
        kept = [m for m in members if not m.startswith(prefixes)]
        if kept:
            out[t] = kept
    return out


# ── Manifest building & chunking ────────────────────────────────────────────────

def read_api_version(directory: Path) -> str:
    """Read sourceApiVersion from sfdx-project.json. Required."""
    sfdx_proj = directory / "sfdx-project.json"
    if not sfdx_proj.is_file():
        raise SfError(
            f"{sfdx_proj} not found. Run setup / orchestrator first to scaffold "
            f"the SFDX project before retrieving."
        )
    with sfdx_proj.open() as f:
        data = json.load(f)
    api = data.get("sourceApiVersion")
    if not api:
        raise SfError(
            f"{sfdx_proj} has no sourceApiVersion. Pin it before retrieving."
        )
    return api


def write_manifest(path: Path, members_by_type: dict[str, list[str]], api_version: str) -> int:
    """Write a package.xml manifest. Returns total member count."""
    ET.register_namespace("", XMLNS)
    pkg = ET.Element("Package", {"xmlns": XMLNS})
    total = 0
    for type_name in sorted(members_by_type):
        members = members_by_type[type_name]
        if not members:
            continue
        types_el = ET.SubElement(pkg, "types")
        for m in sorted(set(members)):
            mem_el = ET.SubElement(types_el, "members")
            mem_el.text = m
            total += 1
        name_el = ET.SubElement(types_el, "name")
        name_el.text = type_name
    version_el = ET.SubElement(pkg, "version")
    version_el.text = api_version
    tree = ET.ElementTree(pkg)
    ET.indent(tree, space="    ")
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="UTF-8", xml_declaration=True)
    return total


@dataclass
class Chunk:
    chunk_id: str
    members_by_type: dict[str, list[str]]
    member_count: int
    manifest_path: Optional[Path] = None


def build_chunks(
    members_by_type: dict[str, list[str]],
    chunk_size: int,
) -> list[Chunk]:
    """Pack types into chunks of approximately `chunk_size` members.

    Special bundle: Profile + all PROFILE_SHAPE_DRIVERS go into the SAME chunks
    so Profile retrieves with full fidelity. The shape drivers may also appear
    in subsequent chunks (where they go on their own); duplication is harmless
    because the retrieve writes to the same files either way.
    """
    members_by_type = {k: list(v) for k, v in members_by_type.items()}
    chunks: list[Chunk] = []

    profiles = members_by_type.pop("Profile", [])
    if profiles:
        bundled: dict[str, list[str]] = {"Profile": list(profiles)}
        for driver in PROFILE_SHAPE_DRIVERS:
            members = members_by_type.get(driver, [])
            if members:
                bundled[driver] = list(members)
        chunks.extend(_pack_into_chunks(bundled, chunk_size, prefix="profile"))

    chunks.extend(_pack_into_chunks(members_by_type, chunk_size, prefix="chunk"))
    return chunks


def _pack_into_chunks(
    members_by_type: dict[str, list[str]],
    chunk_size: int,
    prefix: str,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: dict[str, list[str]] = {}
    current_count = 0

    def flush():
        nonlocal current, current_count
        if current_count == 0:
            return
        chunk_id = f"{prefix}-{len(chunks) + 1:03d}"
        chunks.append(Chunk(chunk_id=chunk_id, members_by_type=current, member_count=current_count))
        current = {}
        current_count = 0

    for type_name in sorted(members_by_type):
        members = list(members_by_type[type_name])
        while members:
            room = chunk_size - current_count
            if room <= 0:
                flush()
                room = chunk_size
            take = members[:room]
            members = members[room:]
            current.setdefault(type_name, []).extend(take)
            current_count += len(take)
            if current_count >= chunk_size:
                flush()
    flush()
    return chunks


# ── Parallel retrieve ───────────────────────────────────────────────────────────

@dataclass
class ChunkResult:
    chunk_id: str
    success: bool
    files_retrieved: int = 0
    elapsed_s: float = 0.0
    log_path: Optional[Path] = None
    error: Optional[str] = None
    retried: bool = False


def _watch_chunk(chunk_id: str, started_at: float, stop: threading.Event) -> None:
    """Emit periodic chunk_progress events while the chunk runs."""
    while not stop.wait(PROGRESS_INTERVAL_SECONDS):
        emit("chunk_progress",
             chunk_id=chunk_id,
             elapsed_s=round(time.time() - started_at, 1))


def retrieve_one(
    alias: str,
    chunk: Chunk,
    project_dir: Path,
    log_dir: Path,
    wait_minutes: int,
) -> ChunkResult:
    log_path = log_dir / f"{chunk.chunk_id}.log"
    started_at = time.time()
    emit("chunk_started", chunk_id=chunk.chunk_id, members=chunk.member_count)

    stop = threading.Event()
    watcher = threading.Thread(
        target=_watch_chunk,
        args=(chunk.chunk_id, started_at, stop),
        daemon=True,
    )
    watcher.start()

    try:
        cmd = [
            "sf", "project", "retrieve", "start",
            "--manifest", str(chunk.manifest_path),
            "--target-org", alias,
            "--wait", str(wait_minutes),
            "--ignore-conflicts",
            "--json",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_dir,
            timeout=wait_minutes * 60 + 60,
        )
        elapsed = time.time() - started_at
        with log_path.open("w") as f:
            f.write(f"# {chunk.chunk_id}  elapsed={elapsed:.1f}s  exit={proc.returncode}\n")
            f.write(f"# cmd: {' '.join(cmd)}\n\n")
            f.write("=== STDOUT ===\n")
            f.write(proc.stdout)
            f.write("\n=== STDERR ===\n")
            f.write(proc.stderr)

        files_retrieved = 0
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
            files = data.get("result", {}).get("files", [])
            files_retrieved = len(files)
        except json.JSONDecodeError:
            pass

        if proc.returncode == 0:
            emit("chunk_done",
                 chunk_id=chunk.chunk_id,
                 elapsed_s=round(elapsed, 1),
                 files=files_retrieved)
            return ChunkResult(
                chunk_id=chunk.chunk_id, success=True,
                files_retrieved=files_retrieved,
                elapsed_s=elapsed, log_path=log_path,
            )
        err = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        err_msg = err[0] if err else f"exit {proc.returncode}"
        emit("chunk_failed",
             chunk_id=chunk.chunk_id,
             elapsed_s=round(elapsed, 1),
             error=err_msg,
             log_path=str(log_path))
        return ChunkResult(
            chunk_id=chunk.chunk_id, success=False,
            elapsed_s=elapsed, log_path=log_path, error=err_msg,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - started_at
        emit("chunk_failed",
             chunk_id=chunk.chunk_id,
             elapsed_s=round(elapsed, 1),
             error="subprocess timeout (wait window exceeded)")
        return ChunkResult(
            chunk_id=chunk.chunk_id, success=False,
            elapsed_s=elapsed, error="subprocess timeout",
        )
    finally:
        stop.set()


def retry_split(
    alias: str,
    failed: Chunk,
    project_dir: Path,
    log_dir: Path,
    manifest_dir: Path,
    api_version: str,
    wait_minutes: int,
) -> list[ChunkResult]:
    """Split a failed chunk in half, retry both halves once each. Returns 2 results."""
    flat: list[tuple[str, str]] = [
        (t, m) for t, members in failed.members_by_type.items() for m in members
    ]
    if len(flat) <= 1:
        return []
    mid = len(flat) // 2
    halves = (flat[:mid], flat[mid:])
    results: list[ChunkResult] = []
    for i, half in enumerate(halves, start=1):
        sub_id = f"{failed.chunk_id}.retry-{i}"
        sub_members: dict[str, list[str]] = {}
        for t, m in half:
            sub_members.setdefault(t, []).append(m)
        manifest_path = manifest_dir / f"{sub_id}.xml"
        write_manifest(manifest_path, sub_members, api_version)
        sub_chunk = Chunk(
            chunk_id=sub_id,
            members_by_type=sub_members,
            member_count=len(half),
            manifest_path=manifest_path,
        )
        result = retrieve_one(alias, sub_chunk, project_dir, log_dir, wait_minutes)
        result.retried = True
        results.append(result)
    return results


def parallel_retrieve(
    alias: str,
    chunks: list[Chunk],
    project_dir: Path,
    manifest_dir: Path,
    log_dir: Path,
    api_version: str,
    concurrency: int,
    wait_minutes: int,
) -> list[ChunkResult]:
    log_dir.mkdir(parents=True, exist_ok=True)

    results: list[ChunkResult] = []
    failed_for_retry: list[Chunk] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(retrieve_one, alias, c, project_dir, log_dir, wait_minutes): c
            for c in chunks
        }
        for fut in concurrent.futures.as_completed(futures):
            chunk = futures[fut]
            res = fut.result()
            results.append(res)
            if not res.success:
                failed_for_retry.append(chunk)

    if failed_for_retry:
        emit("retry_started", count=len(failed_for_retry))
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(retry_split, alias, c, project_dir, log_dir, manifest_dir, api_version, wait_minutes): c
                for c in failed_for_retry
            }
            for fut in concurrent.futures.as_completed(futures):
                results.extend(fut.result())

    return results


# ── Summary ─────────────────────────────────────────────────────────────────────

def write_summary(results: list[ChunkResult], summary_path: Path) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chunks": [
            {
                "chunk_id": r.chunk_id,
                "success": r.success,
                "files_retrieved": r.files_retrieved,
                "elapsed_s": round(r.elapsed_s, 1),
                "log_path": str(r.log_path) if r.log_path else None,
                "error": r.error,
                "retried": r.retried,
            }
            for r in results
        ],
        "totals": {
            "succeeded": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
            "files_retrieved": sum(r.files_retrieved for r in results if r.success),
        },
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w") as f:
        json.dump(payload, f, indent=2)


def print_package_summary(
    packages: list[dict],
    licenses: list[dict],
    excluded_namespaces: set[str],
) -> None:
    if not packages:
        return
    license_by_ns = {l.get("NamespacePrefix"): l for l in licenses if l.get("NamespacePrefix")}
    print()
    print("Package summary")
    print(f"{'Namespace':<25} {'Name':<35} {'Status':<12} {'Used/Allowed':<15} Excluded?")
    print("-" * 100)
    for pkg in sorted(packages, key=lambda p: (p.get("SubscriberPackage", {}) or {}).get("NamespacePrefix") or ""):
        sub = pkg.get("SubscriberPackage") or {}
        ns = sub.get("NamespacePrefix") or ""
        name = sub.get("Name") or ""
        lic = license_by_ns.get(ns) or {}
        status = lic.get("Status") or "—"
        used = lic.get("UsedLicenses")
        allowed = lic.get("AllowedLicenses")
        used_allowed = f"{used}/{allowed}" if used is not None and allowed is not None else "—"
        excluded = "yes" if ns in excluded_namespaces else ""
        print(f"{ns:<25} {name[:34]:<35} {status:<12} {used_allowed:<15} {excluded}")


# ── Main ────────────────────────────────────────────────────────────────────────

def _parse_bool(s: str) -> bool:
    return s.strip().lower() in ("1", "true", "yes", "y", "t")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Standalone parallel Salesforce metadata retriever.",
    )
    parser.add_argument("--alias", required=True, help="sf CLI org alias (already authed).")
    parser.add_argument("--directory", required=True,
                        help="Path to the SFDX project directory (must have sfdx-project.json).")
    parser.add_argument("--include-managed", default="true", type=_parse_bool,
                        help="Include managed-package metadata (default: true).")
    parser.add_argument("--exclude-expired-packages", action="store_true",
                        help="Drop members from packages whose PackageLicense is Expired or Suspended.")
    parser.add_argument("--exclude-namespaces", default="",
                        help="Comma-separated namespace prefixes to exclude.")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Worker count (default {DEFAULT_CONCURRENCY}).")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help=f"Members per chunk (default {DEFAULT_CHUNK_SIZE}).")
    parser.add_argument("--wait-minutes", type=int, default=DEFAULT_WAIT_MINUTES,
                        help=f"Per-chunk retrieve wait window in minutes (default {DEFAULT_WAIT_MINUTES}).")
    args = parser.parse_args(argv)

    project_dir = Path(args.directory).expanduser().resolve()
    if not project_dir.is_dir():
        print(f"error: directory does not exist: {project_dir}", file=sys.stderr)
        return 2
    manifest_dir = project_dir / "manifest"
    log_dir = manifest_dir / "logs"

    try:
        api_version = read_api_version(project_dir)
    except SfError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    emit("api_version", value=api_version)

    excluded_namespaces: set[str] = {
        n.strip() for n in args.exclude_namespaces.split(",") if n.strip()
    }

    try:
        org = enumerate_org(args.alias, args.include_managed)
    except SfError as e:
        print(f"error during enumeration: {e}", file=sys.stderr)
        return 3

    if args.exclude_expired_packages:
        expired_ns = expired_or_suspended_namespaces(org.package_licenses)
        excluded_namespaces |= expired_ns
        if expired_ns:
            emit("excluded_expired_namespaces", namespaces=sorted(expired_ns))

    members = filter_members(org.members_by_type, excluded_namespaces)

    chunks = build_chunks(members, args.chunk_size)
    for c in chunks:
        c.manifest_path = manifest_dir / f"{c.chunk_id}.xml"
        write_manifest(c.manifest_path, c.members_by_type, api_version)
    emit("manifests_written", count=len(chunks))

    results = parallel_retrieve(
        alias=args.alias,
        chunks=chunks,
        project_dir=project_dir,
        manifest_dir=manifest_dir,
        log_dir=log_dir,
        api_version=api_version,
        concurrency=args.concurrency,
        wait_minutes=args.wait_minutes,
    )

    summary_path = manifest_dir / "retrieve-summary.json"
    write_summary(results, summary_path)

    succeeded = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    total_files = sum(r.files_retrieved for r in results if r.success)
    emit("all_done", succeeded=succeeded, failed=failed, total_files=total_files)

    print_package_summary(org.installed_packages, org.package_licenses, excluded_namespaces)

    print()
    print(f"Retrieve summary: {succeeded} chunk(s) succeeded, {failed} failed.")
    print(f"Total files retrieved: {total_files}")
    print(f"Per-chunk logs:       {log_dir}")
    print(f"Summary JSON:         {summary_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
