"""AI Vision Service — Anthropic Messages API client with model routing.

Entry point: extract_invoice(pdf_bytes, mimetype, tenant_config) → dict

Flow:
  1. Count pages / choose model based on complexity
  2. Build prompt (Bulgarian invoice schema)
  3. Call Anthropic Messages API (direct or via Cloudflare AI Gateway)
  4. Parse JSON response + compute cost in EUR cents
  5. Return structured result for caller + logger

Logging is done by the caller (see ai_usage_log.log_extraction) so this
service stays pure and testable. It returns all data needed for a log row.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ─── Pricing table ────────────────────────────────────────
#
# USD per 1M tokens — approximate rates as of 2026-04. Configurable per-tenant
# via `tenant_config["pricing_override"]` if Anthropic adjusts.
#
# Source of truth: Anthropic Admin API usage report (daily reconciliation).
# These constants are used for real-time estimation in the logger; final
# billing amount comes from the API usage report.

PRICING_USD_PER_M_TOKENS: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {
        "input": 0.80, "output": 4.00,
        "cache_read": 0.08, "cache_creation": 1.00,
    },
    "claude-sonnet-4-6": {
        "input": 3.00, "output": 15.00,
        "cache_read": 0.30, "cache_creation": 3.75,
    },
    "claude-opus-4-7": {
        "input": 15.00, "output": 75.00,
        "cache_read": 1.50, "cache_creation": 18.75,
    },
}

# EUR/USD assumption for cents conversion. Override via tenant_config["eur_usd"]
# for invoiced currency accuracy — dashboard uses EUR as display default.
DEFAULT_EUR_USD = 0.92


# ─── Model routing ────────────────────────────────────────


def choose_model(
    *,
    pages: int,
    size_bytes: int,
    tenant_tier: str = "business",
    routing_enabled: bool = True,
) -> str:
    """Pick the cheapest model that can handle this document safely.

    Rules (see bl_ai_ocr_pricing_model.md):
      - 85% of invoices are 1-2 pages simple → haiku (20× cheaper)
      - 3-10 pages / complex → sonnet (balanced)
      - 10+ pages or tenant_tier='enterprise' customs → opus (premium)
    """
    if not routing_enabled:
        return "claude-sonnet-4-6"

    if pages <= 2 and size_bytes < 500 * 1024:
        return "claude-haiku-4-5"

    if pages <= 10:
        return "claude-sonnet-4-6"

    if tenant_tier == "enterprise":
        return "claude-opus-4-7"

    return "claude-sonnet-4-6"  # cap non-enterprise at sonnet


def count_pdf_pages(pdf_bytes: bytes) -> int:
    """Return the true PDF page count; fall back to a byte heuristic.

    Accurate count matters for model routing — the haiku/sonnet/opus
    cutoffs (see ``choose_model``) are driven by this number. The old
    byte scan undercounted by 2–3× on PDFs whose /Pages tree listed
    intermediate nodes, over-routing tiny invoices to sonnet.

    Order of attempts:
      1. pypdf — authoritative, handles encrypted / cross-ref tables.
      2. Byte heuristic — ``/Type /Page`` minus ``/Type /Pages``; used
         when pypdf is absent or raises. Good enough to keep routing
         sane until the operator installs pypdf.
      3. Hard fallback 1 — so extract_invoice always routes to a model.
    """
    if not pdf_bytes:
        return 1
    try:  # ① pypdf
        from io import BytesIO as _BytesIO

        from pypdf import PdfReader as _PdfReader
        reader = _PdfReader(_BytesIO(pdf_bytes))
        n = len(reader.pages)
        if n > 0:
            return n
    except Exception as e:  # noqa: BLE001
        logger.debug("pypdf page count failed, falling back to byte scan: %s", e)
    try:  # ② byte heuristic
        n = pdf_bytes.count(b"/Type /Page") - pdf_bytes.count(b"/Type /Pages")
        if n >= 1:
            return n
    except Exception:  # noqa: BLE001
        pass
    return 1  # ③ hard fallback


# ─── Cost calculation ─────────────────────────────────────


def compute_cost_usd_millicents(model: str, usage: dict) -> int:
    """Return cost in USD millicents (1/1000 of a cent) — integer.

    Precision matters: one small invoice is ~1-2¢. Rounding to cent loses
    30-40% per row; aggregated over 500 docs = significant billing drift.
    Millicents give us $0.00001 precision while staying integer for SQLite
    aggregation and unique indexes.

    usage dict keys: input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens.
    """
    rates = PRICING_USD_PER_M_TOKENS.get(model) or PRICING_USD_PER_M_TOKENS[
        "claude-sonnet-4-6"
    ]
    cost_usd = (
        (usage.get("input_tokens", 0) / 1e6) * rates["input"]
        + (usage.get("output_tokens", 0) / 1e6) * rates["output"]
        + (usage.get("cache_read_input_tokens", 0) / 1e6) * rates["cache_read"]
        + (usage.get("cache_creation_input_tokens", 0) / 1e6)
        * rates["cache_creation"]
    )
    return round(cost_usd * 100_000)  # USD → millicents


def usd_millicents_to_eur_millicents(
    usd_millicents: int, eur_usd: float = DEFAULT_EUR_USD
) -> int:
    return round(usd_millicents * eur_usd)


def millicents_to_cents(millicents: int) -> float:
    """Display helper: 12_345 millicents → 12.345 cents = $0.12345."""
    return millicents / 1000


# ─── Prompt (Bulgarian invoice extraction) ────────────────

_SYSTEM_PROMPT_V1 = """You are a Bulgarian accounting document scanner.

Extract data from the attached invoice or credit note. Return a SINGLE JSON \
object matching this schema:

{
  "document_type": "in_invoice" | "in_refund" | "out_invoice" | "out_refund",
  "l10n_bg_document_type": "01" | "02" | "03" | ... | "98",
  "partner_name": string,
  "partner_vat": string | null,
  "partner_address": string | null,
  "invoice_number": string,
  "invoice_date": "YYYY-MM-DD",
  "delivery_date": "YYYY-MM-DD" | null,
  "payment_term_days": integer | null,
  "currency": "BGN" | "EUR" | "USD" | ...,
  "amount_untaxed": number,
  "amount_tax": number,
  "amount_total": number,
  "lines": [
    {
      "description": string,
      "quantity": number,
      "uom": string | null,
      "price_unit": number,
      "tax_rate": 0 | 9 | 20,
      "amount_subtotal": number
    }
  ]
}

Rules:
- Numbers MUST be numeric types, not strings.
- Dates in ISO YYYY-MM-DD only.
- Cyrillic company names: preserve exactly.
- BG VAT format: "BG" + 9 or 10 digits (strip spaces).
- If a field is ambiguous or missing: null. Do NOT invent values.
- Output ONLY the JSON object, no markdown, no commentary."""


_SYSTEM_PROMPT_V2 = """You are a Bulgarian accounting document scanner.

Extract data from the attached invoice or credit note. Return a SINGLE JSON \
object matching this schema:

{
  "document_type": "in_invoice" | "in_refund" | "out_invoice" | "out_refund",
  "l10n_bg_document_type": "01" | "02" | "03" | ... | "98",
  "partner_name": string,
  "partner_vat": string | null,
  "partner_address": string | null,
  "invoice_number": string,
  "invoice_date": "YYYY-MM-DD",
  "delivery_date": "YYYY-MM-DD" | null,
  "payment_term_days": integer | null,
  "currency": "BGN" | "EUR" | "USD" | ...,
  "amount_untaxed": number,
  "amount_tax": number,
  "amount_total": number,
  "lines": [
    {
      "description": string,
      "quantity": number,
      "uom": string | null,
      "price_unit": number,
      "tax_rate": 0 | 9 | 20,
      "amount_subtotal": number
    }
  ],
  "_confidence": {
    "document_type": number,
    "partner_name": number,
    "partner_vat": number,
    "invoice_number": number,
    "invoice_date": number,
    "amount_untaxed": number,
    "amount_tax": number,
    "amount_total": number,
    "lines_overall": number
  }
}

Rules:
- Numbers MUST be numeric types, not strings.
- Dates in ISO YYYY-MM-DD only.
- Cyrillic company names: preserve exactly.
- BG VAT format: "BG" + 9 or 10 digits (strip spaces).
- If a field is ambiguous or missing: null. Do NOT invent values.
- The `_confidence` object gives your per-field confidence in the range
  0.0 to 1.0. Be honest: 1.0 means "I am certain this is correct",
  0.5 means "I am guessing", 0.0 means "I could not extract this".
  For `lines_overall` assess whether every line item was captured cleanly
  (missing rows, merged rows, unclear UoM all reduce the score).
  Do NOT output confidence for fields whose value is null.
- Output ONLY the JSON object, no markdown, no commentary."""


_SYSTEM_PROMPT_V4 = """You are a Bulgarian accounting document scanner.

Extract data from the attached invoice, credit note, or customs \
declaration. Return a SINGLE JSON object matching this schema:

{
  "document_type": "in_invoice" | "in_refund" | "out_invoice" | "out_refund",
  "l10n_bg_document_type": "01" | "02" | "03" | "117_protocol_117_1" | \
"117_protocol_82_2_1" | ... | "in_customs" | "out_customs" | "98",
  "partner_name": string,
  "partner_vat": string | null,
  "partner_eik": string | null,
  "partner_address": string | null,
  "customs_mrn": string | null,
  "invoice_number": string,
  "invoice_date": "YYYY-MM-DD",
  "delivery_date": "YYYY-MM-DD" | null,
  "payment_term_days": integer | null,
  "currency": "BGN" | "EUR" | "USD" | ...,
  "amount_untaxed": number,
  "amount_tax": number,
  "amount_total": number,
  "lines": [
    {
      "description": string,
      "quantity": number,
      "uom": string | null,
      "price_unit": number,
      "tax_rate": 0 | 9 | 20,
      "amount_subtotal": number
    }
  ],
  "_confidence": {
    "document_type": number,
    "partner_name": number,
    "partner_vat": number,
    "invoice_number": number,
    "invoice_date": number,
    "amount_untaxed": number,
    "amount_tax": number,
    "amount_total": number,
    "lines_overall": number
  }
}

Multi-page guidance (IMPORTANT):
- The grand total (amount_untaxed / amount_tax / amount_total) typically
  appears on the LAST page of a multi-page invoice. Before filling it,
  scan the final pages — do not report a per-page subtotal as the total.
- "Continued…" / "Продължава…" / page numbering like "1 of 3" signals
  a multi-sheet document; collect lines from all pages before reporting.
- If totals appear on an intermediate page (quote-style preview), they
  are NOT the final totals — look further.
- When a rounding row is present ("Закръгляване" / "Rounding"), include
  it as a separate line and use the printed final total, not the sum of
  the other lines.

Bulgarian-specific guidance:
- BG VAT format: "BG" + 9 or 10 digits. Strip spaces and dashes. If the
  printed value is just 9/13 digits (naked ЕИК), return it as-is in
  partner_vat and also populate partner_eik — the post-processor will
  normalise.
- partner_eik is the 9-digit Единен идентификационен код (company
  registry code). 13-digit EIKs (sole traders) truncate to the first 9
  on the VAT number; keep the full 13 in partner_eik if printed.
- customs_mrn: 18-character Movement Reference Number on import/export
  customs declarations (2-digit year + 2-letter country + 14 alphanum).
  Populate only when the document is an actual customs declaration —
  NOT when an invoice merely mentions an MRN in its text.
- Art. 117 (self-billing): when the supplier VAT is non-Bulgarian (EU
  or third-country), the document_type is typically a 117_* protocol
  code (e.g. "117_protocol_117_1" for EU services reverse-charge, or
  "117_protocol_82_2_1" for third-country services). Pick the closest
  fit; the accountant will confirm.

Rules:
- Numbers MUST be numeric types, not strings.
- Dates in ISO YYYY-MM-DD only.
- Cyrillic company names: preserve exactly.
- If a field is ambiguous or missing: null. Do NOT invent values.
- The `_confidence` object gives your per-field confidence in the range
  0.0 to 1.0. Be honest: 1.0 = certain, 0.5 = guessing, 0.0 = could
  not extract. Do NOT output confidence for fields whose value is null.
- Output ONLY the JSON object, no markdown, no commentary."""


_SYSTEM_PROMPT_V3 = """You are a Bulgarian accounting document scanner.

Extract data from the attached invoice, credit note, or customs \
declaration. Return a SINGLE JSON object matching this schema:

{
  "document_type": "in_invoice" | "in_refund" | "out_invoice" | "out_refund",
  "l10n_bg_document_type": "01" | "02" | "03" | "117_protocol_117_1" | \
"117_protocol_82_2_1" | ... | "in_customs" | "out_customs" | "98",
  "partner_name": string,
  "partner_vat": string | null,
  "partner_eik": string | null,
  "partner_address": string | null,
  "customs_mrn": string | null,
  "invoice_number": string,
  "invoice_date": "YYYY-MM-DD",
  "delivery_date": "YYYY-MM-DD" | null,
  "payment_term_days": integer | null,
  "currency": "BGN" | "EUR" | "USD" | ...,
  "amount_untaxed": number,
  "amount_tax": number,
  "amount_total": number,
  "lines": [
    {
      "description": string,
      "quantity": number,
      "uom": string | null,
      "price_unit": number,
      "tax_rate": 0 | 9 | 20,
      "amount_subtotal": number
    }
  ],
  "_confidence": {
    "document_type": number,
    "partner_name": number,
    "partner_vat": number,
    "invoice_number": number,
    "invoice_date": number,
    "amount_untaxed": number,
    "amount_tax": number,
    "amount_total": number,
    "lines_overall": number
  }
}

Bulgarian-specific guidance:
- BG VAT format: "BG" + 9 or 10 digits. Strip spaces and dashes. If the
  printed value is just 9/13 digits (naked ЕИК), return it as-is in
  partner_vat and also populate partner_eik — the post-processor will
  normalise.
- partner_eik is the 9-digit Единен идентификационен код (company
  registry code). 13-digit EIKs (sole traders) truncate to the first 9
  on the VAT number; keep the full 13 in partner_eik if printed.
- customs_mrn: 18-character Movement Reference Number on import/export
  customs declarations (2-digit year + 2-letter country + 14 alphanum).
  Populate only when the document is an actual customs declaration —
  NOT when an invoice merely mentions an MRN in its text.
- Art. 117 (self-billing): when the supplier VAT is non-Bulgarian (EU
  or third-country), the document_type is typically a 117_* protocol
  code (e.g. "117_protocol_117_1" for EU services reverse-charge, or
  "117_protocol_82_2_1" for third-country services). Pick the closest
  fit; the accountant will confirm.

Rules:
- Numbers MUST be numeric types, not strings.
- Dates in ISO YYYY-MM-DD only.
- Cyrillic company names: preserve exactly.
- If a field is ambiguous or missing: null. Do NOT invent values.
- The `_confidence` object gives your per-field confidence in the range
  0.0 to 1.0. Be honest: 1.0 = certain, 0.5 = guessing, 0.0 = could
  not extract. Do NOT output confidence for fields whose value is null.
- Output ONLY the JSON object, no markdown, no commentary."""


# Map prompt_version -> system prompt text.  New versions get appended here;
# consumers default to the newest.
_PROMPT_VERSIONS = {
    "v1": _SYSTEM_PROMPT_V1,
    "v2": _SYSTEM_PROMPT_V2,
    "v3": _SYSTEM_PROMPT_V3,
    "v4": _SYSTEM_PROMPT_V4,
}
DEFAULT_PROMPT_VERSION = "v4"


def _format_partner_account_hints(hints: list[dict]) -> str:
    """Render partner→account histogram as a terse bullet list.

    Caller passes the output of ``ai_invoice_engine._collect_partner_account_histogram``
    — already sorted descending + truncated to the top accounts. Format
    optimised for prompt compactness: one line per account, "share%
    (count lines): Account Label". The model uses this as a soft
    prior when suggesting line coding, NOT as a hard override.
    """
    if not hints:
        return ""
    lines = []
    for h in hints[:5]:
        share_pct = (h.get("share") or 0) * 100
        label = h.get("account_label") or f"id={h.get('account_id')}"
        lines.append(
            f"- {share_pct:.0f}% ({h.get('count', 0)} lines): {label}"
        )
    return "\n".join(lines)


def _format_few_shot_block(examples: list[dict]) -> str:
    """Render past invoices as a compact JSON reference for the model.

    Kept as JSON (not narrative prose) so Claude can pattern-match
    partner naming, line descriptions, UoM conventions deterministically.
    Truncated per-example to a few fields — the goal is priming on
    vendor vocabulary, not exhaustive replay.
    """
    if not examples:
        return ""
    slim = [{
        "partner_name": e.get("partner_name") or "",
        "invoice_number_example": e.get("invoice_number") or "",
        "invoice_date_example": e.get("invoice_date") or "",
        "amount_untaxed": e.get("amount_untaxed") or 0.0,
        "amount_tax": e.get("amount_tax") or 0.0,
        "amount_total": e.get("amount_total") or 0.0,
        "currency": e.get("currency") or "",
        "lines": [{
            "description": (ln.get("description") or "")[:120],
            "quantity": ln.get("quantity") or 0,
            "price_unit": ln.get("price_unit") or 0.0,
            "amount_subtotal": ln.get("amount_subtotal") or 0.0,
        } for ln in (e.get("lines") or [])[:3]],
    } for e in examples[:3]]
    return json.dumps(slim, ensure_ascii=False, indent=2)


def build_messages(
    *,
    file_b64: str,
    mimetype: str,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    few_shot_examples: list[dict] | None = None,
    partner_account_hints: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Return (system, messages) tuple for Anthropic Messages API.

    When ``few_shot_examples`` or ``partner_account_hints`` is provided,
    a leading user→assistant pair is prepended carrying vendor-specific
    context. The pair gets ``cache_control`` so repeat extractions on
    the same vendor hit the prompt cache and amortise the tokens.
    """
    system = _PROMPT_VERSIONS.get(prompt_version, _SYSTEM_PROMPT_V4)
    messages: list[dict] = []

    fs_text = (
        _format_few_shot_block(few_shot_examples)
        if few_shot_examples else ""
    )
    hints_text = (
        _format_partner_account_hints(partner_account_hints)
        if partner_account_hints else ""
    )
    if fs_text or hints_text:
        parts: list[str] = []
        if fs_text:
            parts.append(
                "REFERENCE — past invoices from this vendor (for "
                "partner-name spelling, line description style, UoM "
                "conventions, currency). Do NOT copy values; extract "
                "what the current scan shows:\n" + fs_text
            )
        if hints_text:
            parts.append(
                "ACCOUNT CODING HISTORY — most frequent account codes "
                "this vendor's lines landed on previously. Treat as a "
                "soft prior for the `account_id` on new lines unless "
                "the line description clearly says otherwise:\n"
                + hints_text
            )
        messages.append({
            "role": "user",
            "content": [{
                "type": "text",
                "text": "\n\n".join(parts),
                "cache_control": {"type": "ephemeral"},
            }],
        })
        messages.append({
            "role": "assistant",
            "content": (
                "Understood — I'll use the reference only for "
                "formatting cues and extract fresh values from "
                "the attached scan."
            ),
        })

    content: list[dict] = []

    if mimetype == "application/pdf":
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": mimetype, "data": file_b64},
            "cache_control": {"type": "ephemeral"},  # cache identical PDFs
        })
    elif mimetype.startswith("image/"):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mimetype, "data": file_b64},
            "cache_control": {"type": "ephemeral"},
        })
    else:
        raise ValueError(f"Unsupported mimetype for vision: {mimetype}")

    content.append({
        "type": "text",
        "text": "Extract the invoice data as specified. Return ONLY the JSON object.",
    })

    messages.append({"role": "user", "content": content})
    return system, messages


# ─── Extraction ───────────────────────────────────────────


@dataclass
class ExtractionResult:
    state: str                        # success | error | cached
    model: str
    pages: int
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd_millicents: int = 0
    cost_eur_millicents: int = 0
    prompt_version: str = DEFAULT_PROMPT_VERSION
    extracted_data: dict | None = None
    # Per-field confidence pulled out of extracted_data["_confidence"].
    # Empty for prompt v1 responses (no confidence info in schema).
    field_confidence: dict[str, float] = field(default_factory=dict)
    raw_response: dict | None = field(default=None, repr=False)
    error_message: str | None = None

    def to_log_kwargs(self) -> dict:
        """Shape for ai_usage_log.log_extraction()."""
        return {
            "model": self.model,
            "state": self.state,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cost_millicents": self.cost_eur_millicents,
            "cost_usd_millicents": self.cost_usd_millicents,
            "pages": self.pages,
            "duration_ms": self.duration_ms,
            "prompt_version": self.prompt_version,
            "billed": self.state in ("success", "cached"),
            "error_message": self.error_message,
        }


def _extract_field_confidence(extracted: dict) -> dict[str, float]:
    """Pull the `_confidence` sub-dict, coerce values to floats in [0,1].

    Returns {} for v1 responses (no `_confidence` key). Unparseable entries
    are dropped silently — callers treat missing as "no info".
    """
    raw = (extracted or {}).get("_confidence") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        out[str(k)] = max(0.0, min(1.0, f))
    return out


def extract_invoice(
    *,
    file_bytes: bytes,
    mimetype: str,
    api_key: str,
    base_url: str = "https://api.anthropic.com",
    tenant_tier: str = "business",
    routing_enabled: bool = True,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    eur_usd: float = DEFAULT_EUR_USD,
    timeout: float = 60.0,
    max_tokens: int = 2000,
    model_override: str | None = None,
    few_shot_examples: list[dict] | None = None,
    partner_account_hints: list[dict] | None = None,
) -> ExtractionResult:
    """Call Anthropic Vision, parse JSON, return structured result.

    `base_url` — Anthropic direct or CF AI Gateway prefix
    (e.g. https://gateway.ai.cloudflare.com/v1/<acct>/<gateway>/anthropic).

    `api_key` — tenant-specific Anthropic API key (from Workspace).

    Returns ExtractionResult. Does NOT log — caller logs via
    ai_usage_log.log_extraction(**result.to_log_kwargs()).
    """
    t0 = time.monotonic()
    pages = count_pdf_pages(file_bytes) if mimetype == "application/pdf" else 1
    size_bytes = len(file_bytes)

    model = model_override or choose_model(
        pages=pages,
        size_bytes=size_bytes,
        tenant_tier=tenant_tier,
        routing_enabled=routing_enabled,
    )

    file_b64 = base64.b64encode(file_bytes).decode("ascii")
    system, messages = build_messages(
        file_b64=file_b64,
        mimetype=mimetype,
        prompt_version=prompt_version,
        few_shot_examples=few_shot_examples,
        partner_account_hints=partner_account_hints,
    )

    url = f"{base_url.rstrip('/')}/v1/messages"
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        duration_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code != 200:
            return ExtractionResult(
                state="error",
                model=model,
                pages=pages,
                duration_ms=duration_ms,
                error_message=f"HTTP {resp.status_code}: {resp.text[:400]}",
                prompt_version=prompt_version,
            )

        data = resp.json()
        usage = data.get("usage", {}) or {}
        text_parts = [
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ]
        raw_text = "".join(text_parts).strip()

        # Strip optional markdown fence if model disobeys
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.lower().startswith("json"):
                raw_text = raw_text[4:].strip()

        try:
            extracted = json.loads(raw_text)
        except json.JSONDecodeError as e:
            return ExtractionResult(
                state="error",
                model=model,
                pages=pages,
                duration_ms=duration_ms,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                cost_usd_millicents=compute_cost_usd_millicents(model, usage),
                cost_eur_millicents=usd_millicents_to_eur_millicents(
                    compute_cost_usd_millicents(model, usage), eur_usd
                ),
                error_message=f"Invalid JSON from model: {e}. Head: {raw_text[:200]}",
                raw_response=data,
                prompt_version=prompt_version,
            )

        # Cache hit detection — large cache_read portion and no cache_creation
        is_cached = (
            usage.get("cache_read_input_tokens", 0) > 0
            and usage.get("cache_creation_input_tokens", 0) == 0
            and usage.get("input_tokens", 0) < 200
        )

        cost_usd_mc = compute_cost_usd_millicents(model, usage)
        cost_eur_mc = usd_millicents_to_eur_millicents(cost_usd_mc, eur_usd)

        return ExtractionResult(
            state="cached" if is_cached else "success",
            model=model,
            pages=pages,
            duration_ms=duration_ms,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            cost_usd_millicents=cost_usd_mc,
            cost_eur_millicents=cost_eur_mc,
            prompt_version=prompt_version,
            extracted_data=extracted,
            field_confidence=_extract_field_confidence(extracted),
            raw_response=data,
        )

    except httpx.HTTPError as e:
        return ExtractionResult(
            state="error",
            model=model,
            pages=pages,
            duration_ms=int((time.monotonic() - t0) * 1000),
            error_message=f"{type(e).__name__}: {e}",
            prompt_version=prompt_version,
        )
    except Exception as e:  # noqa: BLE001 — want to log any surprise
        logger.exception("Vision extractor unexpected failure")
        return ExtractionResult(
            state="error",
            model=model,
            pages=pages,
            duration_ms=int((time.monotonic() - t0) * 1000),
            error_message=f"{type(e).__name__}: {e}",
            prompt_version=prompt_version,
        )
