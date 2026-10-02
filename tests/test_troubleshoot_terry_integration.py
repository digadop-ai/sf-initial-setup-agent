#!/usr/bin/env python3
"""Integration test: proves troubleshoot.py's main loop actually calls
terry_ingest.report_llm_call() with the right tokens and attribution after each
Claude turn. No real Anthropic call and no real Terry ingest POST happen: the
Anthropic turn is faked at call_api_with_retry() (never reaches the network),
and terry_ingest.report_llm_call is faked at its own entry point. Safe to run
anywhere, including CI with no credentials and no network access."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import troubleshoot  # noqa: E402


def _fake_summary(tmp_dir: Path) -> Path:
    manifest_dir = tmp_dir / "manifest"
    manifest_dir.mkdir(parents=True)
    summary = {
        "chunks": [
            {"chunk_id": "chunk-001", "success": False, "error": "boom",
             "members_attempted": 3, "elapsed_s": 1.2},
        ],
        "totals": {"succeeded": 0, "failed": 1, "files_retrieved": 0},
    }
    summary_path = manifest_dir / "retrieve-summary.json"
    summary_path.write_text(json.dumps(summary))
    return summary_path


def _fake_response(text: str = "Nothing more to do.", input_tokens: int = 42, output_tokens: int = 7):
    """A minimal stand-in for an anthropic.types.Message with no tool_use blocks,
    so the loop ends after exactly one round."""
    block = types.SimpleNamespace(type="text", text=text)
    usage = types.SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return types.SimpleNamespace(content=[block], stop_reason="end_turn", usage=usage)


class TroubleshooterTerryIntegrationTest(unittest.TestCase):
    def test_reports_one_call_with_correct_tokens_and_attribution_when_configured(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            _fake_summary(project_dir)

            reports = []

            def _fake_report_llm_call(attribution, **kwargs):
                reports.append({"attribution": attribution, **kwargs})
                return {"reported": True, "status": 204, "correlation_id": attribution.correlation_id}

            with mock.patch.object(troubleshoot, "_resolve_api_key", return_value="sk-test-dummy"), \
                 mock.patch.object(troubleshoot, "call_api_with_retry", return_value=_fake_response()), \
                 mock.patch.object(troubleshoot.terry_ingest, "is_configured", return_value=True), \
                 mock.patch.object(troubleshoot.terry_ingest, "report_llm_call", side_effect=_fake_report_llm_call):
                rc = troubleshoot.run_troubleshooter(alias="test-org", project_dir=project_dir)

            self.assertEqual(rc, 0)
            self.assertEqual(len(reports), 1)
            call = reports[0]
            self.assertEqual(call["model"], troubleshoot.MODEL)
            self.assertEqual(call["input_tokens"], 42)
            self.assertEqual(call["output_tokens"], 7)
            self.assertIsInstance(call["latency_ms"], int)
            self.assertGreaterEqual(call["latency_ms"], 0)
            # The attribution instance used for the call is the one resolved once
            # for the whole run, not a fresh one per round (stable correlation id).
            self.assertTrue(call["attribution"].correlation_id.startswith("sfts-"))

    def test_skips_reporting_entirely_when_not_configured(self):
        """When terry_ingest.is_configured() is False (the common desktop case),
        report_llm_call must not be called at all, and the run still succeeds
        unmetered rather than failing or refusing to run."""
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            _fake_summary(project_dir)

            with mock.patch.object(troubleshoot, "_resolve_api_key", return_value="sk-test-dummy"), \
                 mock.patch.object(troubleshoot, "call_api_with_retry", return_value=_fake_response()), \
                 mock.patch.object(troubleshoot.terry_ingest, "is_configured", return_value=False), \
                 mock.patch.object(troubleshoot.terry_ingest, "report_llm_call") as fake_report:
                rc = troubleshoot.run_troubleshooter(alias="test-org", project_dir=project_dir)

            self.assertEqual(rc, 0)
            fake_report.assert_not_called()

    def test_no_api_key_still_short_circuits_before_terry_is_touched(self):
        """Pre-existing behavior (rc 3, no key) must survive the change untouched:
        terry_ingest must never be reached when there is nothing to troubleshoot
        with in the first place."""
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            _fake_summary(project_dir)

            with mock.patch.object(troubleshoot, "_resolve_api_key", return_value=None), \
                 mock.patch.object(troubleshoot.terry_ingest, "report_llm_call") as fake_report:
                rc = troubleshoot.run_troubleshooter(alias="test-org", project_dir=project_dir)

            self.assertEqual(rc, 3)
            fake_report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
