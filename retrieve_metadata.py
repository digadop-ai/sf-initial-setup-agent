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

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CONCURRENCY = 15
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


# Supplemental metadata types — feature-gated or recently-added types that some orgs
# have but `sf org list metadata-types` (which calls describeMetadata) sometimes omits
# in its output even when the org's Metadata API can retrieve them. The agent attempts
# `sf org list metadata --metadata-type <T>` for each of these IN ADDITION to whatever
# describeMetadata returns; if a type yields zero members it's silently skipped, so the
# cost of a false positive is one extra empty list call.
#
# Curated snapshot at API v62. Categories: Experience Cloud, Industries Cloud (FSC,
# Health, EPC, Loyalty, Manufacturing, Auto, Media, RLM, etc.), modern OmniStudio,
# Einstein/GenAI/Bots, Data Cloud, modern settings types, and misc gotchas seen in
# real orgs.
SUPPLEMENTAL_METADATA_TYPES = [
    # Experience Cloud
    "ExperienceBundle", "Network", "NetworkBranding", "NavigationMenu",
    "CommunityTemplateDefinition", "CommunityThemeDefinition", "ManagedTopics",
    "SiteDotCom", "CustomSite", "Branding",
    # Experience Cloud — modern "Build Your Own (LWR)" templates (post-Spring '22).
    # `DigitalExperienceBundle` is distinct from `ExperienceBundle` — the former
    # captures LWR site content (pages, components, theme), the latter the older
    # Aura template content. Orgs running modern LWR communities lose all page
    # content if this isn't enumerated. `DigitalExperience` is the parent
    # container type; `NavigationLinkSet` is the modern navigation bundle.
    "DigitalExperience", "DigitalExperienceBundle", "NavigationLinkSet",
    # Industries — FSC
    "IndustriesSettings", "IndustriesManufacturingSettings",
    "IndustriesAutomotiveSettings", "IndustriesEinsteinFeatureSettings",
    "IndustriesEventOrchestrationSettings", "IndustriesGamificationSettings",
    "IndustriesLoyaltySettings", "IndustriesPricingSettings",
    "IndustriesUnifiedPromotionsSettings", "IndustriesContextSettings",
    "FinancialServicesCloudSettings", "InterestTaggingSettings",
    # Industries — Health Cloud / Life Sciences
    "HealthCloudSettings", "LifeSciencesSettings", "PatientMedicationDosage",
    # Industries — EPC / RLM
    "ProductAttributeSet", "ProductSpecificationType", "ProductSpecificationRecType",
    "AttributeDefinition", "AttributeCategory", "AttributePicklist",
    "QualifierDefinition", "BillingPolicy", "BillingTreatment",
    "RevenueLifecycleManagementSettings", "OrderManagementSettings",
    # Industries — Loyalty
    "LoyaltyProgramSetup", "DecisionTable",
    # Industries — Media / Public Sector / Energy
    "MediaCloudSettings", "MarketAuditSettings", "PublicSectorSettings",
    "ConsumptionSchedule", "EnergyAndUtilitiesSettings",
    # Industries — Insurance
    "ClaimAccessGrantStatus", "InsuranceClaimsSettings", "PolicyAdministrationSettings",
    # Modern OmniStudio
    "OmniProcess", "OmniIntegrationProcedure", "OmniDataTransform",
    "OmniUiCard", "OmniScript", "OmniSupportedSettings",
    # Einstein / Bots / GenAI
    "Bot", "BotVersion", "BotBlock", "BotBlockVersion",
    "EinsteinAgent", "GenAiPromptTemplate", "GenAiPlannerBundle", "GenAiFunction",
    "GenAiPlugin", "GenAiPromptVersion", "MlPredictionDefinition",
    "EinsteinAIViewConfig", "EinsteinAssistantSetting",
    # Data Cloud / CDP
    "DataPackageKitDefinition", "DataPackageKitObject", "DataKitObjectTemplate",
    "DataStreamDefinition", "DataStreamTemplate", "DataConnectorIngestApi",
    "DataConnectorS3", "MarketSegment", "MktDataLakeObject", "MktCalcInsightObject",
    # Marketing-adjacent (the bits that DO live in core Metadata API)
    "MarketingAppExtActivity", "MarketingAppExtension", "MobileApplicationDetail",
    # Modern settings types
    "ContextDefinition", "ConversationServiceIntegration", "ConversationVendorInfo",
    "DigitalExperienceConfig", "DiscoveryAIModel", "DocumentChecklistSettings",
    "EmailTemplateSettings", "EventDeliverySettings", "EventSubscription",
    "ExternalCredential", "ExternalDataSource", "FieldRestrictionRule",
    "ForecastingSettings", "FormulaSettings", "InvLatePymntRiskCalcSettings",
    "LightningOnboardingConfig", "MailMergeSettings", "MfgServiceConsoleSettings",
    "PaymentGatewayProvider", "PlatformSlackSettings",
    "PrivacySettings", "RecommendationStrategy", "RetailExecutionSettings",
    "SearchSettings", "ServicePresenceStatus", "ShareSettings",
    "SubscriptionManagementSettings", "TimelineObjectDefinition", "TrialOrgSettings",
    "WaveAutoInstallRequest", "WorkforceEngagementSettings",
    # Misc gotchas
    "ApexEmailNotifications", "AppMenu", "AssignmentRules", "AutoResponseRules",
    "EclairGeoData", "EmbeddedServiceBranding", "EmbeddedServiceConfig",
    "EmbeddedServiceFlowConfig", "EmbeddedServiceLiveAgent", "EmbeddedServiceMenuSettings",
    "ExperiencePropertyTypeBundle", "MutingPermissionSet",
    "PaymentFlexbenefitSetting", "ServiceChannel", "TimeSheetTemplate",
    "TopicsForObjects", "TransactionSecurityPolicy", "WaveApplication",
    "WaveDashboard", "WaveDataflow", "WaveLens", "WaveRecipe", "WaveTemplateBundle",
    "WaveXmd",
    # Org-wide translations (the language pack — distinct from CustomObjectTranslation
    # per-object translations, which describeMetadata does surface).
    "Translations",
    # Permission Set License Definition — separate metadata from PermissionSet and
    # PermissionSetLicense itself; defines what a permission set license grants.
    "PermissionSetLicenseDefinition",
    # Service Cloud Voice / legacy CTI call center config.
    "CallCenter", "CallCenterRoutingMap",
    # Einstein legacy / Service Assistant — still present in some orgs.
    "AssistantContextItem", "AssistantDefinition",
]


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


def enumerate_folder_items(
    alias: str,
    folders: list[dict],
    include_managed: bool,
) -> dict[str, list[tuple[str, str]]]:
    """Enumerate individual reports / dashboards / email templates / documents.

    Returns a dict mapping metadata type → list of (folder_developer_name,
    item_developer_name) tuples. Items in folders we don't have in the public-
    folder enumeration (e.g. personal folders, namespaced folders when
    include_managed is False) are dropped — they aren't deployable as metadata
    via package.xml anyway.

    Why this is needed: the Metadata API rejects `<members>FolderName/*</members>`
    wildcard entries for folder-based types with "Entity not found". Each item
    must be listed explicitly as `<members>FolderDevName/ItemDevName</members>`.
    """
    # Build lookup maps from the folder enumeration.
    folder_id_to_dev: dict[str, str] = {}
    folder_name_to_dev: dict[str, str] = {}
    for f in folders:
        dev = f.get("DeveloperName")
        if not dev:
            continue
        if f.get("Id"):
            folder_id_to_dev[f["Id"]] = dev
        if f.get("Name"):
            folder_name_to_dev[f["Name"]] = dev

    out: dict[str, list[tuple[str, str]]] = {t: [] for t in FOLDER_BASED_METADATA_TYPES}

    # Report: FolderName is the folder's display Name (with spaces); look it
    # up in folder_name_to_dev to get the package.xml-friendly DeveloperName.
    for r in sf_query(
        alias,
        "SELECT DeveloperName, FolderName, NamespacePrefix FROM Report "
        "WHERE DeveloperName != null",
    ):
        if r.get("NamespacePrefix") and not include_managed:
            continue
        folder_dev = folder_name_to_dev.get(r.get("FolderName"))
        if not folder_dev:
            continue
        out["Report"].append((folder_dev, r["DeveloperName"]))

    # Dashboard: same FolderName (display Name) pattern as Report.
    for r in sf_query(
        alias,
        "SELECT DeveloperName, FolderName, NamespacePrefix FROM Dashboard "
        "WHERE DeveloperName != null",
    ):
        if r.get("NamespacePrefix") and not include_managed:
            continue
        folder_dev = folder_name_to_dev.get(r.get("FolderName"))
        if not folder_dev:
            continue
        out["Dashboard"].append((folder_dev, r["DeveloperName"]))

    # EmailTemplate: has FolderId; resolve through folder_id_to_dev.
    for r in sf_query(
        alias,
        "SELECT DeveloperName, FolderId, NamespacePrefix FROM EmailTemplate "
        "WHERE DeveloperName != null",
    ):
        if r.get("NamespacePrefix") and not include_managed:
            continue
        folder_dev = folder_id_to_dev.get(r.get("FolderId"))
        if not folder_dev:
            continue
        out["EmailTemplate"].append((folder_dev, r["DeveloperName"]))

    # Document: has FolderId; resolve through folder_id_to_dev.
    for r in sf_query(
        alias,
        "SELECT DeveloperName, FolderId, NamespacePrefix FROM Document "
        "WHERE DeveloperName != null",
    ):
        if r.get("NamespacePrefix") and not include_managed:
            continue
        folder_dev = folder_id_to_dev.get(r.get("FolderId"))
        if not folder_dev:
            continue
        out["Document"].append((folder_dev, r["DeveloperName"]))

    return out


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


def enumerate_org(
    alias: str,
    include_managed: bool,
    max_workers: int = DEFAULT_CONCURRENCY,
) -> OrgEnumeration:
    emit("enumerate_started")
    org = OrgEnumeration()

    types = list_metadata_types(alias)
    emit("metadata_types_listed", count=len(types))

    # Types we handle outside of `sf org list metadata` (folder-based, hardcoded,
    # or skipped when managed metadata is excluded).
    types_to_enumerate = [
        t for t in types
        if t not in FOLDER_BASED_METADATA_TYPES
        and t != "StandardValueSet"
        and not (t == "InstalledPackage" and not include_managed)
    ]

    def _enum_one(t: str) -> tuple[str, list[str]]:
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
        return t, sorted(set(names)) if names else []

    total = len(types_to_enumerate)
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_enum_one, t) for t in types_to_enumerate]
        for fut in concurrent.futures.as_completed(futures):
            t, names = fut.result()
            if names:
                org.members_by_type[t] = names
            completed += 1
            if completed % 10 == 0 or completed == total:
                emit("enumerate_progress", completed=completed, total=total)

    org.members_by_type["StandardValueSet"] = list(STANDARD_VALUE_SETS)

    # Supplemental pass: try types not surfaced by describeMetadata (feature-gated,
    # newly-added, etc.). For each supplemental type we run `sf org list metadata`;
    # types that don't apply to this org return zero members and are skipped. We
    # ALWAYS emit the start and end events so the user sees this phase in the log
    # even when it's a no-op (i.e., describeMetadata already covered everything in
    # our supplemental list).
    already_seen = set(types) | FOLDER_BASED_METADATA_TYPES | {"StandardValueSet"}
    supplemental_to_try = [t for t in SUPPLEMENTAL_METADATA_TYPES if t not in already_seen]
    total_supp = len(supplemental_to_try)
    emit("metadata_types_supplemental_listed", count=total_supp)
    found = 0
    if total_supp:
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_enum_one, t) for t in supplemental_to_try]
            for fut in concurrent.futures.as_completed(futures):
                t, names = fut.result()
                if names:
                    org.members_by_type[t] = names
                    found += 1
                completed += 1
                if completed % 10 == 0 or completed == total_supp:
                    emit("enumerate_supplemental_progress",
                         completed=completed, total=total_supp)
    emit("metadata_types_supplemented",
         types_tried=total_supp,
         types_with_members=found)

    org.folders = enumerate_folders(alias, include_managed)
    folder_items = enumerate_folder_items(alias, org.folders, include_managed)
    emit("folder_items_enumerated",
         counts={t: len(v) for t, v in folder_items.items()})
    folder_members_by_type = _folder_members(org.folders, folder_items)
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


def _folder_members(
    folders: list[dict],
    folder_items: dict[str, list[tuple[str, str]]] | None = None,
) -> dict[str, list[str]]:
    """Convert Folder rows + per-item enumeration into per-type member lists.

    Emits:
      - '<FolderDeveloperName>' for each public folder (the folder shell)
      - '<FolderDeveloperName>/<ItemDeveloperName>' for each item in the folder,
        from the explicit per-item enumeration (Metadata API rejects wildcard
        '<FolderName>/*' for folder-based types).
      - 'unfiled$public' for Report and EmailTemplate (the special unfiled folder).
    """
    folder_items = folder_items or {}
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
    for meta_type, pairs in folder_items.items():
        for folder_dev, item_dev in pairs:
            out[meta_type].append(f"{folder_dev}/{item_dev}")
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
    chunk_id: str                          # technical id, used for manifest filename + log path
    members_by_type: dict[str, list[str]]
    member_count: int
    type_label: str = ""                   # human-readable label shown in UI / events
    primary_type: str = ""                 # the main metadata type this chunk represents
    sub_index: int = 0                     # 0 = single chunk, ≥1 = sub-chunk index when type splits
    sub_total: int = 0                     # total sub-chunks for this type (if any)
    manifest_path: Optional[Path] = None


def build_chunks(
    members_by_type: dict[str, list[str]],
    chunk_size: int,
) -> list[Chunk]:
    """Build one chunk per metadata type (sub-chunking when count > chunk_size).

    Familiar UX: progress reads as "Retrieving CustomObject (1,247 members)…"
    rather than "chunk-007", matching how Workbench / sfdx surface metadata.

    Special bundle: Profile + all PROFILE_SHAPE_DRIVERS go into the SAME
    chunk(s) so Profile retrieves with full fidelity (Salesforce only populates
    a Profile's contents from members of types that are ALSO in the package.xml).
    Shape drivers also appear in their own per-type chunks afterwards — the
    duplication is harmless because retrieve writes to the same files either way.
    """
    members_by_type = {k: list(v) for k, v in members_by_type.items() if v}
    chunks: list[Chunk] = []

    # Profile bundle first
    profiles = members_by_type.pop("Profile", [])
    if profiles:
        driver_subset: dict[str, list[str]] = {}
        for driver in PROFILE_SHAPE_DRIVERS:
            members = members_by_type.get(driver, [])
            if members:
                driver_subset[driver] = list(members)
        chunks.extend(_pack_profile_chunks(profiles, driver_subset, chunk_size))

    # One chunk per remaining type (sub-chunked if oversized)
    for type_name in sorted(members_by_type):
        members = members_by_type[type_name]
        if not members:
            continue
        chunks.extend(_pack_type_chunks(type_name, members, chunk_size))
    return chunks


def _pack_type_chunks(
    type_name: str,
    members: list[str],
    chunk_size: int,
) -> list[Chunk]:
    """Build one chunk for `type_name`, sub-chunked when len(members) > chunk_size."""
    members = sorted(set(members))
    n = len(members)
    if n <= chunk_size:
        return [Chunk(
            chunk_id=_safe_chunk_id(type_name),
            members_by_type={type_name: members},
            member_count=n,
            type_label=f"{type_name} ({n:,} members)",
            primary_type=type_name,
        )]
    # Oversized type — split into parts
    parts: list[Chunk] = []
    sub_total = (n + chunk_size - 1) // chunk_size
    for i in range(sub_total):
        sub = members[i * chunk_size : (i + 1) * chunk_size]
        sub_index = i + 1
        parts.append(Chunk(
            chunk_id=f"{_safe_chunk_id(type_name)}.{sub_index}",
            members_by_type={type_name: sub},
            member_count=len(sub),
            type_label=f"{type_name} part {sub_index}/{sub_total} ({len(sub):,} members)",
            primary_type=type_name,
            sub_index=sub_index,
            sub_total=sub_total,
        ))
    return parts


def _pack_profile_chunks(
    profiles: list[str],
    driver_subset: dict[str, list[str]],
    chunk_size: int,
) -> list[Chunk]:
    """Profile + ALL shape drivers in each chunk. Sub-chunk by *profile count* only.

    Salesforce only populates a Profile's permission entries for the types that
    share the same package.xml as the Profile. So every Profile chunk MUST
    include the full driver set (CustomObject, ApexClass, Layout, etc.) — we
    can't split drivers across Profile chunks without silently losing
    permissions in the retrieved Profile XML.

    `chunk_size` is therefore interpreted differently here than for normal
    per-type chunks: it caps **profiles per chunk**, not total members. Drivers
    are bundled in every chunk regardless. This makes Profile chunks "fat"
    (e.g. 47 profiles + 2,190 drivers = 2,237 members in a single chunk) but
    avoids the pathology where chunk_size < driver_count produced O(N profiles)
    chunks each duplicating the entire driver set.
    """
    profiles = sorted(set(profiles))
    drivers_clean = {k: sorted(set(v)) for k, v in driver_subset.items()}
    driver_count = sum(len(v) for v in drivers_clean.values())
    n = len(profiles)

    if n == 0:
        return []

    if n <= chunk_size:
        members = {"Profile": list(profiles), **drivers_clean}
        return [Chunk(
            chunk_id="Profile",
            members_by_type=members,
            member_count=n + driver_count,
            type_label=f"Profile ({n:,} profiles + {driver_count:,} shape-driver members)",
            primary_type="Profile",
        )]

    sub_total = (n + chunk_size - 1) // chunk_size
    parts: list[Chunk] = []
    for i in range(sub_total):
        sub_profiles = profiles[i * chunk_size : (i + 1) * chunk_size]
        sub_index = i + 1
        members = {"Profile": list(sub_profiles), **drivers_clean}
        parts.append(Chunk(
            chunk_id=f"Profile.{sub_index}",
            members_by_type=members,
            member_count=len(sub_profiles) + driver_count,
            type_label=f"Profile part {sub_index}/{sub_total} ({len(sub_profiles):,} profiles + {driver_count:,} shape-driver members)",
            primary_type="Profile",
            sub_index=sub_index,
            sub_total=sub_total,
        ))
    return parts


def _safe_chunk_id(type_name: str) -> str:
    """Sanitize a metadata type name for use as a chunk id (filename, log path)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in type_name)
    return safe or "Unknown"


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
    type_label: str = ""
    primary_type: str = ""
    sub_index: int = 0
    sub_total: int = 0
    members_attempted: int = 0
    warnings: list[dict] = field(default_factory=list)


# Known patterns in `result.messages` from `sf project retrieve start`. Each
# pattern fragment, when found in a message's `problem` text, classifies the
# warning into a category that downstream UI / summary code can group on.
_WARNING_PATTERNS: list[tuple[str, str]] = [
    # ExperienceBundle returns this for sites built with templates that the
    # ExperienceBundle Metadata API doesn't serialize (Tabs+VF, Self-Service,
    # legacy Communities templates like Recruiting / Partners). The community's
    # other metadata (Community, Network, NetworkBranding, siteDotComSites,
    # sites, navigationMenus, audiences) still comes through normally.
    ("doesn't support the template", "experience_bundle_unsupported_template"),
    # Member-level rejection: the API doesn't expose this specific entity. Common
    # for managed-package internals (Conga, etc.) and for wildcard members that
    # resolve to nothing.
    ("cannot be found", "entity_not_found"),
    # Permission denied on a specific member.
    ("not accessible", "not_accessible"),
    # API version mismatch on a specific member.
    ("not available in version", "api_version_mismatch"),
]


def _classify_message(problem: str) -> str:
    """Map a `result.messages[i].problem` string to a category tag."""
    p = (problem or "").lower()
    for fragment, category in _WARNING_PATTERNS:
        if fragment in p:
            return category
    return "other"


def _extract_warnings(result_obj: dict) -> list[dict]:
    """Pull `result.messages` out of an sf retrieve JSON response and classify each.

    Returns a list of {category, file_name, problem, member} dicts. `member` is
    the package-member that triggered the warning when SF gives it to us
    (parsed out of file_name like 'unpackaged/package.xml' or
    'unpackaged/ExperienceBundle') — best-effort, missing for terse messages.
    """
    out: list[dict] = []
    for m in result_obj.get("messages", []) or []:
        problem = m.get("problem") or ""
        file_name = m.get("fileName") or ""
        member: Optional[str] = None
        # SF often quotes the offending member name like:
        #   "Entity of type 'Report' named 'Foo/Bar' cannot be found"
        # Pull the second-quoted token out as best-effort.
        if "'" in problem:
            parts = problem.split("'")
            if len(parts) >= 4:
                member = parts[3]
        out.append({
            "category": _classify_message(problem),
            "file_name": file_name,
            "problem": problem,
            "member": member,
        })
    return out


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
    emit("chunk_started",
         chunk_id=chunk.chunk_id,
         type_label=chunk.type_label,
         primary_type=chunk.primary_type,
         sub_index=chunk.sub_index,
         sub_total=chunk.sub_total,
         members=chunk.member_count)

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
        warnings: list[dict] = []
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
            result_obj = data.get("result", {}) or {}
            files = result_obj.get("files", [])
            files_retrieved = len(files)
            warnings = _extract_warnings(result_obj)
        except json.JSONDecodeError:
            pass

        if proc.returncode == 0:
            # Bucket warnings by category for the chunk_done payload.
            warn_counts: dict[str, int] = {}
            for w in warnings:
                warn_counts[w["category"]] = warn_counts.get(w["category"], 0) + 1
            emit("chunk_done",
                 chunk_id=chunk.chunk_id,
                 type_label=chunk.type_label,
                 primary_type=chunk.primary_type,
                 elapsed_s=round(elapsed, 1),
                 files=files_retrieved,
                 warnings_count=len(warnings),
                 warnings_by_category=warn_counts)
            # Emit a separate event with the full warning details so consumers
            # (web UI dashboard, summary page, post-processing scripts) can
            # render them without parsing the chunk log.
            if warnings:
                emit("chunk_warnings",
                     chunk_id=chunk.chunk_id,
                     primary_type=chunk.primary_type,
                     count=len(warnings),
                     by_category=warn_counts,
                     samples=warnings[:20])
                # Promote the Experience Cloud template-incompatibility case to
                # a dedicated, high-signal event. This is the most common
                # "looks like success but silently incomplete" failure mode in
                # real-world retrieves — operators need to know which sites
                # didn't serialize as ExperienceBundles so they can verify the
                # community content was captured by sibling types (Community,
                # Network, NetworkBranding, sites, siteDotComSites).
                ec_warnings = [w for w in warnings
                               if w["category"] == "experience_bundle_unsupported_template"]
                if ec_warnings:
                    affected: list[str] = []
                    for w in ec_warnings:
                        # The 'problem' text looks like:
                        # "ExperienceBundle Metadata API doesn't support the template of SiteName."
                        # Pull SiteName via the trailing "of <Name>." pattern.
                        prob = w.get("problem", "")
                        if "template of " in prob:
                            tail = prob.split("template of ", 1)[1]
                            site = tail.rstrip(".").strip()
                            if site:
                                affected.append(site)
                    emit("warn_experience_cloud_template",
                         chunk_id=chunk.chunk_id,
                         affected_sites=sorted(set(affected)),
                         note=("ExperienceBundle Metadata API doesn't serialize legacy "
                               "templates (Tabs+VF, Self-Service, pre-Lightning Communities). "
                               "Community structural metadata is still captured by Community, "
                               "Network, NetworkBranding, sites, siteDotComSites, "
                               "navigationMenus, and audiences types."))
            return ChunkResult(
                chunk_id=chunk.chunk_id, success=True,
                files_retrieved=files_retrieved,
                elapsed_s=elapsed, log_path=log_path,
                type_label=chunk.type_label,
                primary_type=chunk.primary_type,
                sub_index=chunk.sub_index,
                sub_total=chunk.sub_total,
                members_attempted=chunk.member_count,
                warnings=warnings,
            )
        err = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        err_msg = err[0] if err else f"exit {proc.returncode}"
        emit("chunk_failed",
             chunk_id=chunk.chunk_id,
             type_label=chunk.type_label,
             primary_type=chunk.primary_type,
             elapsed_s=round(elapsed, 1),
             error=err_msg,
             log_path=str(log_path))
        return ChunkResult(
            chunk_id=chunk.chunk_id, success=False,
            elapsed_s=elapsed, log_path=log_path, error=err_msg,
            type_label=chunk.type_label,
            primary_type=chunk.primary_type,
            sub_index=chunk.sub_index,
            sub_total=chunk.sub_total,
            members_attempted=chunk.member_count,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - started_at
        emit("chunk_failed",
             chunk_id=chunk.chunk_id,
             type_label=chunk.type_label,
             primary_type=chunk.primary_type,
             elapsed_s=round(elapsed, 1),
             error="subprocess timeout (wait window exceeded)")
        return ChunkResult(
            chunk_id=chunk.chunk_id, success=False,
            elapsed_s=elapsed, error="subprocess timeout",
            type_label=chunk.type_label,
            primary_type=chunk.primary_type,
            sub_index=chunk.sub_index,
            sub_total=chunk.sub_total,
            members_attempted=chunk.member_count,
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
            type_label=f"{failed.type_label} (retry {i}/2)",
            primary_type=failed.primary_type,
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
    # Aggregate by primary metadata type for the summary page's per-type table.
    by_type: dict[str, dict] = {}
    for r in results:
        key = r.primary_type or "(unknown)"
        bucket = by_type.setdefault(key, {
            "type": key,
            "chunks": 0,
            "chunks_succeeded": 0,
            "chunks_failed": 0,
            "members_attempted": 0,
            "files_retrieved": 0,
            "elapsed_s": 0.0,
            "any_retried": False,
        })
        bucket["chunks"] += 1
        bucket["chunks_succeeded"] += 1 if r.success else 0
        bucket["chunks_failed"] += 0 if r.success else 1
        bucket["members_attempted"] += r.members_attempted or 0
        bucket["files_retrieved"] += r.files_retrieved if r.success else 0
        bucket["elapsed_s"] += r.elapsed_s
        bucket["any_retried"] = bucket["any_retried"] or r.retried
        bucket.setdefault("warnings_count", 0)
        bucket["warnings_count"] += len(r.warnings or [])
    by_type_list = sorted(by_type.values(), key=lambda b: b["type"].lower())
    for b in by_type_list:
        b["elapsed_s"] = round(b["elapsed_s"], 1)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chunks": [
            {
                "chunk_id": r.chunk_id,
                "type_label": r.type_label,
                "primary_type": r.primary_type,
                "sub_index": r.sub_index,
                "sub_total": r.sub_total,
                "members_attempted": r.members_attempted,
                "success": r.success,
                "files_retrieved": r.files_retrieved,
                "elapsed_s": round(r.elapsed_s, 1),
                "log_path": str(r.log_path) if r.log_path else None,
                "error": r.error,
                "retried": r.retried,
                "warnings": r.warnings or [],
            }
            for r in results
        ],
        "by_type": by_type_list,
        "totals": {
            "succeeded": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
            "files_retrieved": sum(r.files_retrieved for r in results if r.success),
            "type_count": len(by_type_list),
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
        org = enumerate_org(args.alias, args.include_managed, max_workers=args.concurrency)
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
