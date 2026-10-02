#!/usr/bin/env python3
"""Unit tests for terry_ingest.py (digadop-ai#12024). No network access, no
Anthropic/Bedrock SDK calls, no paid calls of any kind: every urllib call is
dependency-injected with a fake opener."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import terry_ingest  # noqa: E402


_TERRY_ENV_NAMES = (
    "TERRY_RUN_ID", "TERRY_CORRELATION_ID", "TERRY_PERSONA", "TERRY_PROJECT_REF",
    "TERRY_TENANT", "TERRY_ENV", "DIGADOP_ENV", "TERRY_INGEST_URL", "TERRY_INGEST_SECRET",
)


class _ClearTerryEnv(unittest.TestCase):
    """Base case that guarantees a clean slate: a stray TERRY_* var left set by
    the real shell (or a previous test) must never leak into another test."""

    def setUp(self) -> None:
        self._saved = {name: os.environ.pop(name, None) for name in _TERRY_ENV_NAMES}

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class AttributionDefaultsTest(_ClearTerryEnv):
    def test_defaults_with_no_env_set(self):
        attr = terry_ingest.Attribution()
        self.assertEqual(attr.tenant, "0")
        self.assertEqual(attr.env, "desktop")
        self.assertIsNone(attr.persona)
        self.assertIsNone(attr.project_ref)
        self.assertTrue(attr.run_id.startswith("sfts-run-"))
        self.assertTrue(attr.correlation_id.startswith("sfts-"))
        # Never synthetic in a way that collides across runs.
        self.assertNotEqual(terry_ingest.Attribution().correlation_id, attr.correlation_id)

    def test_env_overrides_are_honored_exactly(self):
        os.environ["TERRY_RUN_ID"] = "run-123"
        os.environ["TERRY_CORRELATION_ID"] = "corr-456"
        os.environ["TERRY_PERSONA"] = "nabu"
        os.environ["TERRY_PROJECT_REF"] = "digadop-ai/digadop-ai#12024"
        os.environ["TERRY_TENANT"] = "7"
        os.environ["TERRY_ENV"] = "qa"
        attr = terry_ingest.Attribution()
        self.assertEqual(attr.run_id, "run-123")
        self.assertEqual(attr.correlation_id, "corr-456")
        self.assertEqual(attr.persona, "nabu")
        self.assertEqual(attr.project_ref, "digadop-ai/digadop-ai#12024")
        self.assertEqual(attr.tenant, "7")
        self.assertEqual(attr.env, "qa")

    def test_digadop_env_fallback_when_terry_env_unset(self):
        os.environ["DIGADOP_ENV"] = "prod"
        self.assertEqual(terry_ingest.Attribution().env, "prod")


class EstimateCostTest(unittest.TestCase):
    def test_known_model_computes_expected_cost(self):
        # 1,000,000 input + 1,000,000 output tokens at $3/$15 per million = $18.00.
        cost = terry_ingest.estimate_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
        self.assertEqual(cost, 18.0)

    def test_cache_tokens_priced_separately(self):
        cost = terry_ingest.estimate_cost_usd(
            "claude-sonnet-4-6", 0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000
        )
        self.assertEqual(cost, 0.3 + 3.75)

    def test_unknown_model_returns_none_never_a_false_zero(self):
        self.assertIsNone(terry_ingest.estimate_cost_usd("some-future-model", 100, 100))

    def test_zero_tokens_is_a_real_zero_not_none(self):
        self.assertEqual(terry_ingest.estimate_cost_usd("claude-sonnet-4-6", 0, 0), 0.0)


class IsConfiguredTest(_ClearTerryEnv):
    def test_false_when_unset(self):
        self.assertFalse(terry_ingest.is_configured())

    def test_true_when_env_set(self):
        os.environ["TERRY_INGEST_URL"] = "https://example.invalid/ingest"
        self.assertTrue(terry_ingest.is_configured())


class EnvFileFallbackTest(_ClearTerryEnv):
    """~/.digadop-agents-env is the same fallback DIGADOP_ANTHROPIC_API_KEY already
    uses; this proves TERRY_INGEST_URL/SECRET are resolvable from it too, with no
    new configuration mechanism to document or support."""

    def test_reads_ingest_url_from_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "digadop-agents-env"
            env_file.write_text(
                'export DIGADOP_ANTHROPIC_API_KEY="sk-unrelated"\n'
                'export TERRY_INGEST_URL="https://example.invalid/ingest"\n'
                'TERRY_INGEST_SECRET=shh\n'
            )
            with mock.patch.object(terry_ingest, "_ENV_FILE", env_file):
                self.assertEqual(
                    terry_ingest._resolve_env_value(("TERRY_INGEST_URL",)),
                    "https://example.invalid/ingest",
                )
                self.assertEqual(
                    terry_ingest._resolve_env_value(("TERRY_INGEST_SECRET",)), "shh"
                )
                self.assertTrue(terry_ingest.is_configured())

    def test_env_var_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "digadop-agents-env"
            env_file.write_text('export TERRY_INGEST_URL="https://from-file.invalid"\n')
            os.environ["TERRY_INGEST_URL"] = "https://from-env.invalid"
            with mock.patch.object(terry_ingest, "_ENV_FILE", env_file):
                self.assertEqual(
                    terry_ingest._resolve_env_value(("TERRY_INGEST_URL",)),
                    "https://from-env.invalid",
                )


class _FakeResponse:
    def __init__(self, status: int = 204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ReportLlmCallTest(_ClearTerryEnv):
    def test_red_before_unconfigured_makes_zero_network_calls(self):
        """The 'red' case this fix closes: before TERRY_INGEST_URL is set, no POST
        is ever attempted (the opener below asserts it is never invoked), and the
        function still returns a well-formed, non-raising status."""

        def _opener_must_not_be_called(*_a, **_k):
            raise AssertionError("urlopen must not be called when unconfigured")

        attr = terry_ingest.Attribution()
        result = terry_ingest.report_llm_call(
            attr, model="claude-sonnet-4-6", input_tokens=10, output_tokens=5,
            _urlopen=_opener_must_not_be_called,
        )
        self.assertFalse(result["reported"])
        self.assertIn("TERRY_INGEST_URL", result["reason"])

    def test_success_path_posts_expected_body_and_header(self):
        os.environ["TERRY_INGEST_URL"] = "https://example.invalid/ingest"
        os.environ["TERRY_INGEST_SECRET"] = "topsecret"
        os.environ["TERRY_TENANT"] = "0"
        os.environ["TERRY_PERSONA"] = "nabu"

        captured = {}

        def _fake_opener(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["header"] = req.get_header("X-terry-ingest-secret") or req.get_header(
                "X-Terry-Ingest-Secret"
            )
            import json as _json

            captured["body"] = _json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeResponse(204)

        attr = terry_ingest.Attribution()
        result = terry_ingest.report_llm_call(
            attr, model="claude-sonnet-4-6", input_tokens=100, output_tokens=50,
            latency_ms=1234, _urlopen=_fake_opener,
        )

        self.assertTrue(result["reported"])
        self.assertEqual(result["status"], 204)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["header"], "topsecret")
        body = captured["body"]
        self.assertEqual(body["kind"], "metered_event")
        self.assertEqual(body["product"], "sf-initial-setup-agent")
        self.assertEqual(body["component"], "troubleshoot")
        self.assertEqual(body["env"], "desktop")
        self.assertEqual(body["metric"], "llm_call")
        self.assertEqual(body["quantity"], 150)
        self.assertEqual(body["tenant"], "0")
        self.assertEqual(body["runId"], attr.run_id)
        self.assertEqual(body["correlationId"], attr.correlation_id)
        self.assertEqual(body["latencyMs"], 1234)
        self.assertAlmostEqual(body["costUsd"], (100 * 3.0 + 50 * 15.0) / 1_000_000)
        self.assertEqual(body["metadata"]["model"], "claude-sonnet-4-6")
        self.assertEqual(body["metadata"]["backend"], "anthropic-direct")
        self.assertEqual(body["metadata"]["persona"], "nabu")
        self.assertEqual(body["metadata"]["inputTokens"], 100)
        self.assertEqual(body["metadata"]["outputTokens"], 50)

    def test_no_secret_header_when_secret_unset(self):
        os.environ["TERRY_INGEST_URL"] = "https://example.invalid/ingest"
        captured = {}

        def _fake_opener(req, timeout):
            captured["header"] = req.get_header("X-terry-ingest-secret") or req.get_header(
                "X-Terry-Ingest-Secret"
            )
            return _FakeResponse(204)

        terry_ingest.report_llm_call(
            terry_ingest.Attribution(), model="claude-sonnet-4-6",
            input_tokens=1, output_tokens=1, _urlopen=_fake_opener,
        )
        self.assertIsNone(captured["header"])

    def test_unpriced_model_omits_cost_usd_field_entirely(self):
        os.environ["TERRY_INGEST_URL"] = "https://example.invalid/ingest"
        captured = {}

        def _fake_opener(req, timeout):
            import json as _json

            captured["body"] = _json.loads(req.data.decode("utf-8"))
            return _FakeResponse(204)

        terry_ingest.report_llm_call(
            terry_ingest.Attribution(), model="some-future-model",
            input_tokens=1, output_tokens=1, _urlopen=_fake_opener,
        )
        self.assertNotIn("costUsd", captured["body"])

    def test_http_error_never_raises(self):
        os.environ["TERRY_INGEST_URL"] = "https://example.invalid/ingest"

        def _raising_opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

        result = terry_ingest.report_llm_call(
            terry_ingest.Attribution(), model="claude-sonnet-4-6",
            input_tokens=1, output_tokens=1, _urlopen=_raising_opener,
        )
        self.assertFalse(result["reported"])
        self.assertEqual(result["status"], 401)

    def test_network_error_never_raises(self):
        os.environ["TERRY_INGEST_URL"] = "https://example.invalid/ingest"

        def _raising_opener(req, timeout):
            raise TimeoutError("timed out")

        result = terry_ingest.report_llm_call(
            terry_ingest.Attribution(), model="claude-sonnet-4-6",
            input_tokens=1, output_tokens=1, _urlopen=_raising_opener,
        )
        self.assertFalse(result["reported"])
        self.assertIn("error", result)

    def test_truly_unexpected_exception_is_caught_not_propagated(self):
        """The deliberate catch-all: a shape of failure none of the specific
        except clauses anticipate (e.g. a ValueError from a future urllib change)
        must still be reported, not raised, matching Terry's own 'measurement is
        fire-and-forget and never throws' rule in terry/src/client.ts."""
        os.environ["TERRY_INGEST_URL"] = "https://example.invalid/ingest"

        def _raising_opener(req, timeout):
            raise ValueError("something urllib never documented")

        result = terry_ingest.report_llm_call(
            terry_ingest.Attribution(), model="claude-sonnet-4-6",
            input_tokens=1, output_tokens=1, _urlopen=_raising_opener,
        )
        self.assertFalse(result["reported"])
        self.assertIn("unexpected ValueError", result["error"])


if __name__ == "__main__":
    unittest.main()
