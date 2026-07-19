# Test: retrieve worker isolation ends the .git/index race (issue #3)

Issue: digadop-ai/sf-initial-setup-agent#3
PR: #5 (branch `feat/3-clionyx`)
Change under test: each retrieve worker runs in its own throwaway SFDX project so
`sf`'s source tracking no longer writes a shared `.git/index` concurrently.

## Two layers of verification

This is a CLI + Salesforce-org test, not a browser test. It has two parts:

1. A pure unit + concurrency test that needs NO `sf` and NO org (run anywhere).
2. A real large-sandbox retrieve that needs the `sf` CLI and an authed sandbox
   with many chunks. This is the one that actually reproduces the original bug.

## Part 1: unit + concurrency test (no org, no sf)

From the repo root on branch `feat/3-clionyx`:

```
python3 test_retrieve_race.py
```

Expected: every test line prints `PASS` and the final line reads
`6/6 passed` (exit code 0). The `test_concurrent_merges_land_every_file` case is
the important one: it drives 40 threads through the same lock-guarded merge path
the retrieve workers use and asserts all 40 files land. That is the property the
old shared-`.git/index` design violated.

## Part 2: real large-sandbox retrieve (needs sf CLI + authed sandbox)

Preconditions (STOP if not met):
- `sf --version` works and is 2.133.x or newer (the versions that showed the bug).
- An authed sandbox org alias with many metadata members. The original report
  reproduced worst on `bench-wasula1` (135K members, 458 chunks, 410 failed) and
  `WebDev1a` (119K members, 271 chunks, 235 failed). Any sandbox that produced a
  high failure count on `main` is a valid target.
- A scratch SFDX project directory scaffolded with `sfdx-project.json`
  (the web UI / orchestrator does this; or copy an existing one).

Steps:

1. Check out the PR branch and confirm you are on it:
   ```
   git checkout feat/3-clionyx
   git log --oneline -1
   ```
   Expected: the top commit mentions "isolate each worker in its own SFDX project".

2. Run a full retrieve against the large sandbox at the default concurrency (15):
   ```
   python3 retrieve_metadata.py --alias <sandbox-alias> --directory <project-dir> --concurrency 15
   ```

3. While it runs (or after), watch for the failure modes this fix targets. NONE
   of these strings should appear in any chunk log under
   `<project-dir>/manifest/logs/` or in the summary:
   - `Index file is empty`
   - `Invalid checksum in GitIndex buffer`
   - `MetadataTransferError` caused by either of the above

   Expected: chunk failures, if any, drop to the dev-edition baseline (roughly
   1 to 3 out of hundreds, from unrelated causes like managed-package internals),
   NOT the hundreds-of-failures pattern from the issue table.

4. Confirm the retrieved source landed in the central project:
   ```
   find <project-dir>/force-app -type f | wc -l
   ```
   Expected: a non-trivial file count consistent with the members retrieved
   (comparable to what a same-org retrieve produced before, minus the members
   that the race was silently failing).

5. Confirm cleanup: the workspace base must be gone after the run.
   ```
   ls -la <project-dir>/.retrieve-workspaces 2>&1
   ```
   Expected: "No such file or directory" (the run removes it on exit).

## Regression check

The fix must not change what ends up in `force-app` for an org that was already
succeeding. Run the same retrieve against a small dev-edition org (source
tracking off, so it never hit the race) and confirm the retrieved file set and
the `retrieve-summary.json` totals match a pre-change run of the same org.

## Results

| # | Case | PASS/FAIL | Notes |
|---|------|-----------|-------|
| 1 | Unit + concurrency test (`6/6 passed`) | | |
| 2 | Large-sandbox retrieve: no index-corruption errors | | |
| 3 | Large-sandbox retrieve: failure count at dev-edition baseline | | |
| 4 | `force-app` populated with retrieved source | | |
| 5 | `.retrieve-workspaces` cleaned up after run | | |
| 6 | Regression: dev-edition org unchanged file set / totals | | |

## On full PASS

Merge PR #5 to `main` per the standing verified-equals-merge consent and update
the board.

## On FAIL

Leave PR #5 open, comment the failing case and the chunk log excerpt, and route
back to the owner window (`clionyx`) for a fix on the same branch.
