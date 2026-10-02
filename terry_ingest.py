#!/usr/bin/env python3
"""
terry_ingest.py: best-effort usage reporting to the central Terry ledger (digadop-ai#12024).

troubleshoot.py calls the Anthropic SDK directly with DIGADOP_ANTHROPIC_API_KEY
(decision D-5: OURS, see docs/design/terry-coverage-2-design-2026-09-29.md section 8:
"it spends our key today... OURS changes nothing about who pays and only adds the
metering"). This module does not touch that call. It reports each response's token
usage and attribution to Terry's generic metering-plane ingest endpoint
(terry.metered_event, digadop-ai#592) as a side channel, so the spend is visible in
the ledger under a driven correlation id.

Why metered_event and not the LLM ledger (terry.usage / LedgerRecord): Terry's
LLMBackend enum is frozen at 'cli' | 'bedrock' | 'stub' (terry/src/types.ts, enforced
again at the ingest boundary by terry/src/ingest/validate.ts LEDGER_BACKENDS), and none
of the three describes a direct Anthropic-API-key call. 'cli' bills a Claude Max
subscription via the local `claude` binary (a different payer mechanism, and the binary
and its OAuth login are not present on an operator's or customer's desktop). 'bedrock'
calls AWS Bedrock directly (requires AWS credentials this desktop tool does not hold,
and would silently change which provider actually serves the call and at what price).
Claiming either value in a LedgerRecord would misattribute the transport, the exact
thing Terry Coverage 2 exists to make truthful. metered_event has no backend field, so
it reports tokens, an estimated cost, and full attribution honestly, with no ledger or
ingest contract change. See the SOURCE READY receipt on digadop-ai#12024 for the open
design question (whether a 4th LLMBackend value is worth adding) this leaves for the TA.

Never raises. Never blocks the troubleshooting loop: report_llm_call() always returns a
status dict, even on a network failure. A no-op when TERRY_INGEST_URL is unset (today's
default on an operator's or customer's desktop), the "local run warns and continues"
case per design section 4.3, as opposed to a deployed service's refuse-to-start.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

PRODUCT = "sf-initial-setup-agent"
COMPONENT = "troubleshoot"

INGEST_SECRET_HEADER = "x-terry-ingest-secret"
_ENV_FILE = Path("~/.digadop-agents-env").expanduser()
_INGEST_URL_NAMES = ("TERRY_INGEST_URL",)
_INGEST_SECRET_NAMES = ("TERRY_INGEST_SECRET",)

# Mirrors terry/src/registry.ts MODELS['claude-sonnet-4-6'] (USD per 1,000,000 tokens,
# Anthropic's published cache multipliers: read 0.1x input, write 1.25x input). This
# tool is Python and has no access to the TypeScript registry, so the rate is
# duplicated here; if troubleshoot.py's MODEL constant changes, update this table too.
_PRICE_PER_MILLION = {
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cache_read": 0.3, "cache_write": 3.75},
}


def _resolve_env_value(names: tuple[str, ...]) -> Optional[str]:
    """Same two-source lookup as troubleshoot.py's _resolve_api_key: env vars first,
    then the operator's ~/.digadop-agents-env file. Lets TERRY_INGEST_URL/SECRET be
    provisioned the same way DIGADOP_ANTHROPIC_API_KEY already is, with no new
    configuration mechanism to document or support."""
    for name in names:
        v = os.environ.get(name)
        if v:
            return v
    if _ENV_FILE.is_file():
        try:
            for raw in _ENV_FILE.read_text().splitlines():
                line = raw.strip()
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() in names:
                    return v.strip().strip('"').strip("'")
        except OSError:
            pass
    return None


def is_configured() -> bool:
    """Whether a Terry ingest endpoint is configured. False is the expected,
    unremarkable state on most desktops today; callers should warn once and
    continue, never refuse to run the troubleshooter over it."""
    return bool(_resolve_env_value(_INGEST_URL_NAMES))


class Attribution:
    """Resolved once per troubleshooter run. Env var names match the shared-client
    convention proposed for R-C4-1B (TERRY_RUN_ID, TERRY_CORRELATION_ID, TERRY_PERSONA,
    TERRY_PROJECT_REF, TERRY_TENANT), so a parent process (an orchestrator, a fleet
    window) that already sets them for other Terry-metered children attributes this
    one the same way, with no SFTS-specific wiring. `tenant` is never synthetic: it
    defaults to fleet tenant 0 (D-5; Stony Point is tenant 0 per the design's section
    2.6) because this tool has no per-request tenant concept of its own to fall back
    to. It is a single-operator, single-org desktop CLI, not a multi-tenant service."""

    __slots__ = ("run_id", "correlation_id", "persona", "project_ref", "tenant", "env")

    def __init__(self) -> None:
        self.run_id = os.environ.get("TERRY_RUN_ID") or f"sfts-run-{uuid.uuid4()}"
        self.correlation_id = os.environ.get("TERRY_CORRELATION_ID") or f"sfts-{uuid.uuid4()}"
        self.persona = os.environ.get("TERRY_PERSONA") or None
        self.project_ref = os.environ.get("TERRY_PROJECT_REF") or None
        self.tenant = os.environ.get("TERRY_TENANT") or "0"
        # Free-text field at the ingest boundary (unlike `backend`, `env` carries no
        # enum check in terry/src/ingest/validate.ts), so "desktop" is honest and
        # distinguishes this surface from our own dev/qa/prod deployments without
        # needing a contract change. TERRY_ENV / DIGADOP_ENV override it for a context
        # (e.g. a future QA harness) that wants this run folded into dev/qa/prod instead.
        self.env = os.environ.get("TERRY_ENV") or os.environ.get("DIGADOP_ENV") or "desktop"


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Optional[float]:
    """Best-effort estimate from the table above. Mirrors Terry's own rule (terry#74,
    terry#75): an unpriced model returns None, never a false 0, so an unpriced row
    stays visibly unpriced instead of looking free."""
    price = _PRICE_PER_MILLION.get(model)
    if price is None:
        return None
    usd = (
        input_tokens * price["in"]
        + output_tokens * price["out"]
        + cache_read_tokens * price["cache_read"]
        + cache_write_tokens * price["cache_write"]
    ) / 1_000_000
    return round(usd, 8)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def report_llm_call(
    attribution: Attribution,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    latency_ms: int = 0,
    outcome: str = "ok",
    timeout_s: float = 3.0,
    _urlopen: Callable = urllib.request.urlopen,
) -> dict:
    """POST one metered_event (digadop-ai#592) to Terry's ingest endpoint for this
    call. Returns a small status dict for the caller to emit as a progress event.
    Never raises: a dead or misconfigured ingest endpoint must never interrupt
    troubleshooting, the thing this tool exists to do.

    `_urlopen` is injectable for tests only (never pass it in product code); it
    defaults to the real urllib opener.
    """
    url = _resolve_env_value(_INGEST_URL_NAMES)
    if not url:
        return {"reported": False, "reason": "TERRY_INGEST_URL not configured"}
    secret = _resolve_env_value(_INGEST_SECRET_NAMES)

    cost = estimate_cost_usd(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    body: dict = {
        "kind": "metered_event",
        "at": _now_iso(),
        "product": PRODUCT,
        "component": COMPONENT,
        "env": attribution.env,
        "metric": "llm_call",
        "unit": "token",
        "quantity": max(0, int(input_tokens)) + max(0, int(output_tokens)),
        "outcome": outcome,
        "latencyMs": max(0, int(latency_ms)),
        "tenant": attribution.tenant,
        "runId": attribution.run_id,
        "correlationId": attribution.correlation_id,
        "metadata": {
            "model": model,
            # Not a terry LLMBackend value (no such value exists for this transport);
            # see the module docstring. A free-form metadata field, not the ledger's
            # enum-checked `backend` column, so this does not need the frozen value set.
            "backend": "anthropic-direct",
            "inputTokens": max(0, int(input_tokens)),
            "outputTokens": max(0, int(output_tokens)),
            "cacheReadInputTokens": max(0, int(cache_read_tokens)),
            "cacheWriteInputTokens": max(0, int(cache_write_tokens)),
            "persona": attribution.persona,
            "projectRef": attribution.project_ref,
        },
    }
    if cost is not None:
        body["costUsd"] = cost

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"content-type": "application/json"}
    )
    if secret:
        req.add_header(INGEST_SECRET_HEADER, secret)
    try:
        with _urlopen(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", 200)
        return {"reported": 200 <= status < 300, "status": status, "correlation_id": attribution.correlation_id}
    except urllib.error.HTTPError as e:
        return {"reported": False, "status": e.code, "error": f"{type(e).__name__}: {e.reason}"}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {"reported": False, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001
        # Deliberate suppression, named: this is a side-channel metering report, not
        # the troubleshooting work itself. Terry's own TypeScript client states the
        # same rule verbatim (terry/src/client.ts: "Measurement is fire-and-forget
        # and never throws: a ledger failure cannot break an LLM call"). An
        # unanticipated exception here (a bad response object shape, a future
        # urllib change) must not crash the troubleshooter; it is still surfaced
        # loudly, via the returned dict, which the caller emits as a progress event
        # the web UI and operator both see, so it is reported, not silently dropped.
        return {"reported": False, "error": f"unexpected {type(e).__name__}: {e}"}
