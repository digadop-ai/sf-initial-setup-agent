#!/usr/bin/env python3
"""Tests for the issue #3 fix: per-worker isolated projects + serialized merge.

These exercise the pure helpers (`read_package_dir`, `scaffold_worker_project`,
`merge_retrieved`) without touching `sf` or a live org, so they run anywhere.
The concurrency test drives many threads through the same merge path the retrieve
workers use and asserts every file lands intact, which is the property the old
shared-`.git/index` design violated.

Run: python3 test_retrieve_race.py   (no pytest required)
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import tempfile
from pathlib import Path

import retrieve_metadata as rm


def test_read_package_dir_default_entry():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        (proj / "sfdx-project.json").write_text(json.dumps({
            "packageDirectories": [
                {"path": "unpackaged"},
                {"path": "force-app", "default": True},
            ],
            "sourceApiVersion": "62.0",
        }))
        assert rm.read_package_dir(proj) == "force-app"


def test_read_package_dir_falls_back():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        # No sfdx-project.json at all.
        assert rm.read_package_dir(proj) == "force-app"
        # Present but no packageDirectories -> still the safe default.
        (proj / "sfdx-project.json").write_text(json.dumps({"sourceApiVersion": "62.0"}))
        assert rm.read_package_dir(proj) == "force-app"
        # No default flag -> first entry wins.
        (proj / "sfdx-project.json").write_text(json.dumps({
            "packageDirectories": [{"path": "src"}],
        }))
        assert rm.read_package_dir(proj) == "src"


def test_scaffold_worker_project_is_isolated():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ws"
        a = rm.scaffold_worker_project(base, "62.0", "force-app")
        b = rm.scaffold_worker_project(base, "62.0", "force-app")
        assert a != b, "each worker must get its own project dir"
        for w in (a, b):
            proj = json.loads((w / "sfdx-project.json").read_text())
            assert proj["sourceApiVersion"] == "62.0"
            assert proj["packageDirectories"] == [{"path": "force-app", "default": True}]
            assert (w / "force-app").is_dir()


def test_merge_retrieved_copies_disjoint_files():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        src = root / "src" / "force-app"
        dest = root / "dest" / "force-app"
        dest.mkdir(parents=True)
        f = src / "main" / "default" / "classes" / "Foo.cls"
        f.parent.mkdir(parents=True)
        f.write_text("public class Foo {}")
        copied = rm.merge_retrieved(src, dest)
        assert copied == 1
        assert (dest / "main" / "default" / "classes" / "Foo.cls").read_text() == "public class Foo {}"


def test_merge_retrieved_missing_src_is_noop():
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "force-app"
        dest.mkdir()
        assert rm.merge_retrieved(Path(d) / "does-not-exist", dest) == 0


def test_concurrent_merges_land_every_file():
    """The core issue #3 property: N workers merging in parallel lose nothing.

    Each 'worker' scaffolds its own project (private tree), drops a unique file,
    then merges into one shared central project through `merge_retrieved` (the
    same lock-guarded path the retrieve workers use). All files must arrive.
    """
    n = 40
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        base = root / rm.WORKSPACE_SUBDIR
        central = root / "central" / "force-app"
        central.mkdir(parents=True)

        def one(i: int) -> None:
            w = rm.scaffold_worker_project(base, "62.0", "force-app")
            try:
                p = w / "force-app" / "main" / "default" / "classes" / f"C{i}.cls"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"class C{i} {{}}")
                rm.merge_retrieved(w / "force-app", central)
            finally:
                import shutil
                shutil.rmtree(w, ignore_errors=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as pool:
            list(pool.map(one, range(n)))

        landed = sorted(p.name for p in (central / "main" / "default" / "classes").glob("*.cls"))
        assert landed == sorted(f"C{i}.cls" for i in range(n)), landed


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
