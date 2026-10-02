#!/usr/bin/env python3
"""
terry_ingest.py: posts each real troubleshooter LLM call to Terry's central ledger
as a terry_llm_call row (digadop-ai#12024, R-C4-SFTS-BACKEND, TA ruling H3.3).

troubleshoot.py calls the Anthropic SDK directly with DIGADOP_ANTHROPIC_API_KEY
(decision D-5: OURS, see docs/design/terry-coverage-2-design-2026-09-29.md section 8:
"it spends our key today... OURS changes nothing about who pays and only adds the
metering"). This module does not touch that call. It reports each response's token
usage, estimated/actual cost, and full attribution to Terry's ingest endpoint under
one correlation id per troubleshooter run.

Earlier shape (rejected): this module posted `kind: 'metered_event'` to
terry.metered_event, because Terry's LLMBackend enum was frozen at
'cli' | 'bedrock' | 'stub' and none described a direct Anthropic-API-key call. TA
ruling H3.3 (terry PR #355/#357, terry/src/types.ts) added 'anthropic' as a
LEDGER-ONLY LLMBackend value for exactly this caller: a non-SDK consumer that posts
its own terry_llm_call row straight to the HTTP ingest endpoint
(createTerryClient({backend: 'anthropic'}) itself still refuses: no TypeScript
transport implements it). So this module now posts the real terry_llm_call /
LedgerRecord shape (no `kind` wrapper; terry/src/ingest/handler.ts's default,
back-compat branch), the same table Terry's own IngestSink.record() writes to.

Gating (R-C4-SFTS-BACKEND H3.3): because every call this tool makes spends OUR key
(D-5 OURS, never a customer's), a call must never be made in a way that cannot be
attributed on the ledger. check_ingest_readiness() is checked ONCE, before any paid
Anthropic call: if the ingest endpoint is absent, incomplete (no shared secret, or
no DIGADOP_ENV to satisfy the ledger's required `env` field), or cannot be SigV4-signed
(no AWS credentials resolvable, required because the deployed Function URL is
AuthType=AWS_IAM), the caller skips the paid step entirely with a clear reason and
zero paid calls: never a best-effort unmetered spend.

Once a call has actually happened, a later ingest POST failure (network blip, 5xx)
must never retroactively "undo" or block the troubleshooting it already paid for:
report_llm_call() never raises (Terry's own TypeScript client states the identical
rule in terry/src/client.ts: "Measurement is fire-and-forget and never throws: a
ledger failure cannot break an LLM call"), and the caller is expected to surface that
failure loudly (see troubleshoot.py's stderr line) rather than silently swallow it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

PRODUCT = "sf-initial-setup-agent"
COMPONENT = "troubleshoot"

# The only transport this tool has (D-5 OURS): a direct Anthropic-API-key call, never
# Claude Max CLI, never AWS Bedrock. 'anthropic' is a ledger-only LLMBackend value
# (terry/src/types.ts) added for exactly this caller; 'us' is the payer (our key).
BACKEND = "anthropic"
PAYER = "us"

INGEST_SECRET_HEADER = "x-terry-ingest-secret"
_ENV_FILE = Path("~/.digadop-agents-env").expanduser()
_INGEST_URL_NAMES = ("TERRY_INGEST_URL",)
_INGEST_SECRET_NAMES = ("TERRY_INGEST_SECRET",)

# Mirrors terry/src/registry.ts MODELS['claude-sonnet-4-6'] (USD per 1,000,000 tokens,
# Anthropic's published cache multipliers: read 0.1x input, write 1.25x input). This
# tool is Python and has no access to the TypeScript registry, so the rate is
# duplicated here (registry.ts itself names this file as the mirror); if
# troubleshoot.py's MODEL constant changes, update this table too.
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


class Attribution:
    """Resolved once per troubleshooter run. Env var names match the shared-client
    convention proposed for R-C4-1B (TERRY_RUN_ID, TERRY_CORRELATION_ID, TERRY_PERSONA,
    TERRY_PROJECT_REF, TERRY_TENANT), so a parent process (an orchestrator, a fleet
    window) that already sets them for other Terry-metered children attributes this
    one the same way, with no SFTS-specific wiring. `tenant` is never synthetic: it
    defaults to fleet tenant 0 (D-5; Stony Point is tenant 0) because this tool has no
    per-request tenant concept of its own to fall back to. It is a single-operator,
    single-org desktop CLI, not a multi-tenant service.

    `env` is read ONLY from DIGADOP_ENV (R-C4-SFTS-BACKEND H3.3, matching
    terry/src/guardrail.ts resolveEnv(): `env.DIGADOP_ENV || undefined`), never
    defaulted. The ledger's `env` field is required (terry#174, validateLedgerRecord),
    so an unset DIGADOP_ENV makes a real terry_llm_call row impossible to write
    honestly, which is exactly the case check_ingest_readiness() below must catch
    BEFORE any paid call, not paper over with a synthetic "desktop" value.
    """

    __slots__ = ("run_id", "correlation_id", "persona", "project_ref", "tenant", "env")

    def __init__(self) -> None:
        self.run_id = os.environ.get("TERRY_RUN_ID") or f"sfts-run-{uuid.uuid4()}"
        self.correlation_id = os.environ.get("TERRY_CORRELATION_ID") or f"sfts-{uuid.uuid4()}"
        self.persona = os.environ.get("TERRY_PERSONA") or None
        self.project_ref = os.environ.get("TERRY_PROJECT_REF") or None
        self.tenant = os.environ.get("TERRY_TENANT") or "0"
        self.env = os.environ.get("DIGADOP_ENV") or None


def resolve_aws_credentials() -> Optional[dict]:
    """Mirrors terry/src/http-signer.ts resolveAwsCredentials(): the AWS_* env vars
    only (no boto3 dependency: none is in requirements.txt and this PR adds none; no
    shared credentials file, no SSO profile). That is the same set present on a Lambda
    execution environment and the only source IngestSink itself falls back to when no
    credentials provider is supplied, so "can this caller sign like IngestSink" is
    answered identically here."""
    access_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key_id or not secret_access_key:
        return None
    creds = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
    session_token = os.environ.get("AWS_SESSION_TOKEN")
    if session_token:
        creds["session_token"] = session_token
    return creds


def _aws_region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def check_ingest_readiness(attribution: Attribution) -> dict:
    """Whether a real terry_llm_call POST can actually land, checked ONCE before any
    paid Anthropic call. The deployed ingest endpoints are AWS_IAM Function URLs
    (terry/src/ledger/ingest.ts), so a request needs BOTH a SigV4 signature and the
    app-level shared secret; the record also needs `env` (required by
    validateLedgerRecord, terry#174). Returns {"ready": bool, "reason": str | None};
    the reason always starts with "absent", "incomplete", or "cannot be signed", the
    three cases R-C4-SFTS-BACKEND H3.3 names explicitly."""
    if not _resolve_env_value(_INGEST_URL_NAMES):
        return {"ready": False, "reason": "absent: TERRY_INGEST_URL is not configured"}
    if not _resolve_env_value(_INGEST_SECRET_NAMES):
        return {"ready": False, "reason": "incomplete: TERRY_INGEST_SECRET is not configured"}
    if not attribution.env:
        return {
            "ready": False,
            "reason": "incomplete: DIGADOP_ENV is not set (required by the ledger's env field)",
        }
    if resolve_aws_credentials() is None:
        return {
            "ready": False,
            "reason": (
                "cannot be signed: no AWS credentials resolvable "
                "(AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY not set) for the AWS_IAM ingest endpoint"
            ),
        }
    return {"ready": True, "reason": None}


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


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _sign_request(url: str, headers: dict, body: bytes, creds: dict, region: str, service: str) -> None:
    """Mutates `headers` with a SigV4 signature, matching terry/src/http-signer.ts
    signRequest() (same canonical-request construction, same signing key
    derivation): the write path authenticates identically to Terry's own IngestSink
    against the same AWS_IAM Lambda Function URL. Implemented with stdlib
    hmac/hashlib only, zero new dependencies, mirroring http-signer.ts's own
    "ZERO dependencies" rule."""
    parsed = urllib.parse.urlsplit(url)
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    payload_hash = _sha256_hex(body)

    headers["host"] = parsed.netloc
    headers["x-amz-content-sha256"] = payload_hash
    headers["x-amz-date"] = amz_date
    session_token = creds.get("session_token")
    if session_token:
        headers["x-amz-security-token"] = session_token

    query_pairs = sorted(urllib.parse.parse_qsl(parsed.query))
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}" for k, v in query_pairs
    )

    lower_headers = {k.lower(): v for k, v in headers.items()}
    names = sorted(lower_headers)
    canonical_headers = "".join(f"{n}:{lower_headers[n].strip()}\n" for n in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join(
        ["POST", parsed.path or "/", canonical_query, canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical_request.encode("utf-8"))]
    )
    k_date = _hmac_sha256(("AWS4" + creds["secret_access_key"]).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds['access_key_id']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def report_llm_call(
    attribution: Attribution,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    latency_ms: int = 0,
    stop_reason: Optional[str] = None,
    timeout_s: float = 3.0,
    _urlopen: Callable = urllib.request.urlopen,
) -> dict:
    """POST one terry_llm_call ledger record (digadop-ai#12024, R-C4-SFTS-BACKEND) to
    Terry's ingest endpoint for this call. Returns a small status dict for the caller
    to emit as a progress event. Never raises: a dead or misconfigured ingest endpoint
    must never interrupt troubleshooting, the thing this tool exists to do, nor
    retroactively affect a call that already happened.

    Authenticates like Terry's own IngestSink: the shared secret header, plus a SigV4
    signature whenever AWS credentials resolve (required for the deployed AWS_IAM
    Function URL; never a secret value in source or in any receipt this writes).

    `_urlopen` is injectable for tests only (never pass it in product code); it
    defaults to the real urllib opener.
    """
    url = _resolve_env_value(_INGEST_URL_NAMES)
    if not url:
        return {"reported": False, "reason": "TERRY_INGEST_URL not configured"}
    secret = _resolve_env_value(_INGEST_SECRET_NAMES)

    cost = estimate_cost_usd(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    body: dict = {
        "at": _now_iso(),
        "product": PRODUCT,
        "component": COMPONENT,
        "env": attribution.env,
        "backend": BACKEND,
        "model": model,
        "inputTokens": max(0, int(input_tokens)),
        "outputTokens": max(0, int(output_tokens)),
        "cacheReadInputTokens": max(0, int(cache_read_tokens)),
        "cacheWriteInputTokens": max(0, int(cache_write_tokens)),
        # costUsd is the deprecated alias (terry/src/types.ts): the real estimate, or a
        # real 0 when unpriced, never omitted. estimatedCostUsd/actualCostUsd stay None
        # (JSON null) when unpriced so the row is visibly unpriced, never falsely free
        # (terry#74/#75). actualCostUsd == estimatedCostUsd here: payer is 'us' and a
        # direct Anthropic call has no bedrock-style margin to layer on top, so what we
        # estimated is exactly what we paid.
        "costUsd": cost if cost is not None else 0.0,
        "estimatedCostUsd": cost,
        "actualCostUsd": cost,
        "payer": PAYER,
        "latencyMs": max(0, int(latency_ms)),
        "tenant": attribution.tenant,
        "runId": attribution.run_id,
        "correlationId": attribution.correlation_id,
    }
    if attribution.persona:
        body["persona"] = attribution.persona
    if attribution.project_ref:
        body["projectRef"] = attribution.project_ref
    if stop_reason:
        body["stopReason"] = stop_reason

    data = json.dumps(body).encode("utf-8")
    headers: dict = {"content-type": "application/json"}
    if secret:
        headers[INGEST_SECRET_HEADER] = secret
    creds = resolve_aws_credentials()
    if creds is not None:
        _sign_request(url, headers, data, creds, _aws_region(), "lambda")

    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with _urlopen(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", 200)
        return {
            "reported": 200 <= status < 300,
            "status": status,
            "correlation_id": attribution.correlation_id,
        }
    except urllib.error.HTTPError as e:
        return {
            "reported": False,
            "status": e.code,
            "error": f"{type(e).__name__}: {e.reason}",
            "correlation_id": attribution.correlation_id,
        }
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {
            "reported": False,
            "error": f"{type(e).__name__}: {e}",
            "correlation_id": attribution.correlation_id,
        }
    except Exception as e:  # noqa: BLE001
        # Deliberate suppression, named: this is a side-channel metering report, not
        # the troubleshooting work itself. Terry's own TypeScript client states the
        # same rule verbatim (terry/src/client.ts: "Measurement is fire-and-forget
        # and never throws: a ledger failure cannot break an LLM call"). An
        # unanticipated exception here (a bad response object shape, a future
        # urllib change) must not crash the troubleshooter; it is still surfaced,
        # via the returned dict, which the caller emits as a progress event (and,
        # on a post-call failure, a loud stderr line), so it is reported, not
        # silently dropped.
        return {
            "reported": False,
            "error": f"unexpected {type(e).__name__}: {e}",
            "correlation_id": attribution.correlation_id,
        }
