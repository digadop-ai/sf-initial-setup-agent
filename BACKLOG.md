# sf-initial-setup-agent — backlog

Tracked enhancement requests. Items in the "Deferred" section are not yet implemented; items in "Done in v0.3.1" were closed in this iteration. Newly-deferred items belong above the "Done" section.

## Deferred

*(none currently — all v0.3.x backlog items below shipped in v0.3.1.)*

## Done in v0.3.1

### #1. Keep server alive while browser is open; close only when browser closes — **Done**

Implemented via `_shutdown_watchdog` in `web_ui.py`: server stays up while ≥1 WebSocket subscriber is connected; when all disconnect, a configurable grace timer starts and `shutdown_scheduled` events fire with countdown. Operator can press "Stay open" to defer 10 minutes (`/defer-shutdown`) or "Quit now" for immediate shutdown (`/shutdown-now`). The dashboard banner makes the "kept alive because dashboard is open" state explicitly visible so the cause is never mysterious.

### #2. Launch from an HTML link instead of a Terminal window — **Done (locally-built .app)**

`launcher.applescript` is the canonical source; `setup.sh` compiles it to `Launch sf-initial-setup-agent.app` on first install (or when source is newer than the bundle). The .app is gitignored — built locally rather than shipped as a binary, which avoids macOS code-signing/notarization concerns and Drive-sync flakiness on bundle directories.

Behavior: double-click the .app → health-checks `127.0.0.1:8765` → if alive, opens the browser at that URL; otherwise boots `run.sh` in the background via `nohup` (no visible Terminal window) with a widened PATH for sf/node resolution. The Python orchestrator picks a free port and opens the browser itself.

Not done: the custom URL scheme (`sf-agent://`) and browser-side bootstrap variants — punted; the .app covers the primary "Dock-able launcher" need.

### #3. Retrieve by metadata type, with per-type log entries and per-type summary — **Done**

`build_chunks` now emits one chunk per metadata type, sub-chunked when `len(members) > chunk_size`. Profile retains its shape-driver bundling (Profile + CustomObject/ApexClass/Layout/etc. in the same chunk for permission completeness). Dashboard chunk table is keyed by metadata-type name; per-type aggregation appears in `retrieve-summary.json` and the summary page. Log entries read as *"Retrieving CustomObject (1,247 members)…"* → *"Retrieved CustomObject — 1,247 files in 12.4s"*.

### #4. Display the final destination folder, not just the parent — **Done**

Dashboard renders `{{ project_parent }}/{{ project_leaf }}` so the actual destination is visible (the leaf is the `<alias>-metadata` folder the SFDX project was scaffolded into, the parent is what the wizard captured).

### #5. Start-retrieve UI should update instantly — **Done**

`static/app.js` `startBtn` handler now disables the button, clears the chunks table, resets counters, and flips state to `running` BEFORE the `await fetch('/start-retrieve')`. On HTTP error, the optimistic UI state is rolled back to idle.

### Folder-based per-item enumeration — **Done**

The wildcard `<members>FolderName/*</members>` syntax is rejected by the Metadata API for Report, Dashboard, EmailTemplate, and Document — every member returns `Entity of type 'X' named 'Folder/*' cannot be found`. The agent now enumerates each item explicitly via SOQL (`SELECT DeveloperName, FolderName/FolderId, NamespacePrefix FROM <Type>`) and emits explicit `<members>FolderDev/ItemDev</members>` entries. Validated against `stonyp-production`: went from 0 → 579 reports, 0 → 9 dashboards, 0 → 61 email templates, 0 → 14 documents on disk.

### Chunk-level warning surface — **Done**

`sf project retrieve start --json` can return `result.messages[]` containing per-member rejections even when the overall chunk reports `success: true`. The agent now parses these, classifies each by pattern (`experience_bundle_unsupported_template`, `entity_not_found`, `not_accessible`, `api_version_mismatch`, `other`), surfaces a `chunk_warnings` event with samples and counts, and includes the warnings in `retrieve-summary.json`. `chunk_done` event payload gains `warnings_count` and `warnings_by_category` fields. Dashboard log row downgrades the chunk's outcome from success-green to warn-yellow when any warning fired.

### Experience Cloud template-incompatibility warning — **Done**

When the ExperienceBundle chunk receives `"ExperienceBundle Metadata API doesn't support the template of <SiteName>"` (common for legacy Tabs+VF / Self-Service / Recruiting / Partners template sites), the agent emits a dedicated `warn_experience_cloud_template` event listing the affected sites and noting that the substantive community metadata is still captured by sibling types (`Community`, `Network`, `NetworkBranding`, `sites`, `siteDotComSites`, `navigationMenus`, `audiences`). High-signal output so the operator knows exactly which communities don't ExperienceBundle and where to find their content instead.

### SUPPLEMENTAL_METADATA_TYPES coverage expansion — **Done**

Added: modern Experience Cloud (`DigitalExperience`, `DigitalExperienceBundle`, `NavigationLinkSet` — distinct from the older `ExperienceBundle`/`NavigationMenu` types), org-wide `Translations` (the language pack, distinct from per-object `CustomObjectTranslation`), `PermissionSetLicenseDefinition`, Service Cloud Voice (`CallCenter`, `CallCenterRoutingMap`), and Einstein legacy (`AssistantContextItem`, `AssistantDefinition`).
