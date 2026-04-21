"""AI Invoice Engine — pluggable pipeline runner.

The engine manages a linear sequence of named **steps** that execute against
a shared **context** (move_id, attachment, tenant config, accumulated data).

Every step is a first-class registered entity — not hard-coded in the engine.
Plugins add new steps without modifying engine code:

    from ai_invoice_engine import Step, registry

    def _my_step(ctx):
        ctx.data["my_output"] = do_something(ctx)
        return True  # or return dict for rich logging

    registry.register(Step(
        name="my_custom_step",
        sequence=250,
        applies_when=lambda ctx: ctx.move_type == "in_invoice",
        execute=_my_step,
    ))

Plugins are auto-discovered at server startup via `load_plugins("/data/plugins/ai_invoice")`
— drop in a .py file with a top-level `register(registry)` function.

## Sequence convention

    000-099  : discovery + probing (find attachments, read Odoo state)
    100-199  : extraction (Vision LLM, caching, retry)
    200-299  : data validation + write-back to Odoo
    300-399  : business logic (skill invocation, matching, enrichment)
    400-499  : notifications + audit
    500+     : user plugins

## Cross-references

- `ai_vision_service.extract_invoice` — called by built-in step `extract_vision`
- `ai_usage_log.log_extraction`      — called by built-in step `log_usage`
- `l10n_bg_ai_invoice_glue` skill     — invoked by built-in step `invoke_posting_skill`

The engine does NOT own HTTP calls — those live in ai_vision_service.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

import ai_usage_log
import ai_vision_service
import bg_validators
import pdf_sanitizer

logger = logging.getLogger(__name__)


# ─── Pipeline vocabulary ──────────────────────────────────

STEP_AWAIT_ATTACHMENT = "awaiting_attachment"
STEP_AWAIT_EXTRACTION = "awaiting_extraction"
STEP_RETRY_EXTRACTION = "retry_extraction"
STEP_AWAIT_REVIEW     = "awaiting_review"
STEP_AWAIT_SKILL_POST = "awaiting_skill_post"
STEP_READY_TO_POST    = "ready_to_post"
STEP_POSTED           = "posted"
STEP_CANCELLED        = "cancelled"
STEP_UNKNOWN          = "unknown"


# ─── Step primitives ──────────────────────────────────────


@dataclass
class PipelineContext:
    """Mutable state passed from one step to the next.

    Steps read `odoo_conn`, `move_id`, `attachment_id`, `tenant_code`,
    `tenant_tier`, `api_key`, `base_url` as invariants; they populate `data`
    and may set `abort=True` to short-circuit the pipeline.
    """
    odoo_conn: Any = None
    tenant_code: str = ""
    tenant_tier: str = "business"
    api_key: str = ""
    base_url: str = "https://api.anthropic.com"

    # Target record
    move_id: int | None = None
    attachment_id: int | None = None
    source: str = "upload"                 # upload | gmail | terminal | api
    source_message_id: str | None = None

    # Accumulator — steps write outputs here
    data: dict = field(default_factory=dict)

    # Control flow
    abort: bool = False
    abort_reason: str = ""

    # Odoo-side cached reads (populated by probe step)
    move: dict = field(default_factory=dict)
    move_type: str | None = None
    attachments: list[dict] = field(default_factory=list)


@dataclass
class Step:
    """A single named step in the pipeline.

    `applies_when`: optional predicate; step is skipped when it returns False.
    `execute`:      callable(ctx) returning True/False or a dict for log detail.
    `on_error`:     abort | skip | retry (how to react when execute raises).
    """
    name: str
    sequence: int
    execute: Callable[[PipelineContext], Any]
    applies_when: Callable[[PipelineContext], bool] | None = None
    on_error: str = "skip"
    description: str = ""


@dataclass
class StepRun:
    """Audit record for one step execution inside a pipeline run."""
    step_name: str
    status: str                            # ok | skip | error | abort
    duration_ms: int
    output: Any = None
    error: str | None = None


@dataclass
class PipelineRun:
    """Result of a full pipeline invocation."""
    tenant_code: str
    move_id: int | None
    attachment_id: int | None
    started_at: float
    finished_at: float
    duration_ms: int
    aborted: bool
    abort_reason: str
    step_runs: list[StepRun] = field(default_factory=list)
    final_next_step: str = STEP_UNKNOWN
    final_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tenant_code": self.tenant_code,
            "move_id": self.move_id,
            "attachment_id": self.attachment_id,
            "duration_ms": self.duration_ms,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "final_next_step": self.final_next_step,
            "step_runs": [asdict(s) for s in self.step_runs],
            "final_data": self.final_data,
        }


# ─── Registry ─────────────────────────────────────────────


class StepRegistry:
    """Ordered collection of steps. Steps must have unique names."""

    def __init__(self) -> None:
        self._steps: dict[str, Step] = {}

    def register(self, step: Step) -> None:
        if step.name in self._steps:
            logger.warning("Step %s already registered — overwriting", step.name)
        self._steps[step.name] = step

    def unregister(self, name: str) -> None:
        self._steps.pop(name, None)

    def all(self) -> list[Step]:
        return sorted(self._steps.values(), key=lambda s: (s.sequence, s.name))

    def names(self) -> list[str]:
        return [s.name for s in self.all()]


registry = StepRegistry()  # module-global, steps register on import


# ─── Pipeline runner ──────────────────────────────────────


def run_pipeline(ctx: PipelineContext) -> PipelineRun:
    """Execute all applicable steps in sequence order. Returns full audit."""
    t0 = time.monotonic()
    step_runs: list[StepRun] = []

    for step in registry.all():
        if ctx.abort:
            step_runs.append(StepRun(
                step_name=step.name, status="abort", duration_ms=0,
                error=f"aborted earlier: {ctx.abort_reason}",
            ))
            continue
        # Applicability gate
        if step.applies_when and not _safe_call_predicate(step.applies_when, ctx):
            step_runs.append(StepRun(
                step_name=step.name, status="skip", duration_ms=0,
                output="applies_when returned False",
            ))
            continue
        # Execute
        t_step = time.monotonic()
        try:
            output = step.execute(ctx)
            step_runs.append(StepRun(
                step_name=step.name, status="ok",
                duration_ms=int((time.monotonic() - t_step) * 1000),
                output=output if not isinstance(output, bool) else None,
            ))
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            logger.exception("Step %s raised", step.name)
            step_runs.append(StepRun(
                step_name=step.name, status="error",
                duration_ms=int((time.monotonic() - t_step) * 1000),
                error=err,
            ))
            if step.on_error == "abort":
                ctx.abort = True
                ctx.abort_reason = f"{step.name}: {err}"
            # on_error='skip' → continue to next step

    t1 = time.monotonic()
    return PipelineRun(
        tenant_code=ctx.tenant_code,
        move_id=ctx.move_id,
        attachment_id=ctx.attachment_id,
        started_at=t0,
        finished_at=t1,
        duration_ms=int((t1 - t0) * 1000),
        aborted=ctx.abort,
        abort_reason=ctx.abort_reason,
        step_runs=step_runs,
        final_next_step=ctx.data.get("next_step", STEP_UNKNOWN),
        final_data=ctx.data,
    )


def _safe_call_predicate(pred: Callable[[PipelineContext], bool], ctx: PipelineContext) -> bool:
    try:
        return bool(pred(ctx))
    except Exception as e:  # noqa: BLE001
        logger.warning("applies_when raised: %s", e)
        return False


# ─── Built-in steps (sequence < 500) ──────────────────────


def _step_probe_move(ctx: PipelineContext) -> dict:
    """Read move + attachments from Odoo into ctx."""
    if not ctx.move_id:
        ctx.abort = True
        ctx.abort_reason = "move_id not set"
        return {"error": "move_id missing"}
    rows = ctx.odoo_conn.execute_kw(
        "account.move", "read", [[ctx.move_id]],
        {"fields": [
            "name", "move_type", "state", "partner_id",
            "invoice_date", "ref", "amount_total", "currency_id",
        ]},
    )
    if not rows:
        ctx.abort = True
        ctx.abort_reason = f"move {ctx.move_id} not found"
        return {"error": ctx.abort_reason}
    ctx.move = rows[0]
    ctx.move_type = rows[0].get("move_type")
    ctx.attachments = ctx.odoo_conn.execute_kw(
        "ir.attachment", "search_read",
        [[["res_model", "=", "account.move"], ["res_id", "=", ctx.move_id]]],
        {"fields": ["id", "name", "mimetype", "file_size"]},
    )
    # Auto-pick attachment if not specified
    if not ctx.attachment_id:
        pdfs = [a for a in ctx.attachments
                if a.get("mimetype") in ("application/pdf",) or
                (a.get("mimetype") or "").startswith("image/")]
        if pdfs:
            ctx.attachment_id = pdfs[0]["id"]
    return {
        "move_name": ctx.move.get("name"),
        "attachments": len(ctx.attachments),
        "picked_attachment_id": ctx.attachment_id,
    }


def _step_guard_already_extracted(ctx: PipelineContext) -> dict:
    """Short-circuit if this move already has a successful/cached log entry."""
    logs = ai_usage_log.query(
        tenant_code=ctx.tenant_code,
        limit=5,
    )
    prior = [
        r for r in logs
        if r.get("move_id") == ctx.move_id
        and r.get("state") in ("success", "cached")
    ]
    if prior:
        ctx.data["already_extracted"] = True
        ctx.data["prior_extractions"] = len(prior)
        ctx.data["next_step"] = STEP_AWAIT_SKILL_POST
        ctx.abort = True
        ctx.abort_reason = "already_extracted"
    return {"prior_extractions": len(prior)}


def _read_company_bool_flag(conn, field_name: str) -> bool:
    """Read a Boolean field from res.company (first by id).

    Defensive — returns False when the field is absent (e.g. glue
    module uninstalled) or the call raises. Paired with
    ``_read_company_budget_eur_mc`` so the MCP pipeline can keep
    per-tenant config reads short and uniform.
    """
    try:
        company_ids = conn.execute_kw(
            "res.company", "search", [[]],
            {"limit": 1, "order": "id asc"},
        )
        if not company_ids:
            return False
        rows = conn.execute_kw(
            "res.company", "read", [company_ids], {"fields": [field_name]},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("company flag read skipped: %s", e)
        return False
    if not rows:
        return False
    return bool(rows[0].get(field_name))


# Critical fields that gate two-pass escalation — low confidence here
# triggers a second extraction pass with sonnet.
_ESCALATION_FIELDS = ("partner_vat", "invoice_date", "amount_total")
_ESCALATION_CONF_THRESHOLD = 0.75


def _needs_escalation(result) -> tuple[bool, str]:
    """Return (should_escalate, reason) based on field_confidence.

    Only haiku-routed successful extractions are eligible; sonnet/opus
    are already the top tiers on this stack.
    """
    if not result or result.state != "success":
        return False, "no successful extraction"
    if result.model != "claude-haiku-4-5":
        return False, f"model={result.model} not haiku"
    conf = result.field_confidence or {}
    low = [
        f for f in _ESCALATION_FIELDS
        if conf.get(f) is not None and conf[f] < _ESCALATION_CONF_THRESHOLD
    ]
    if low:
        return True, "low conf: " + ", ".join(
            f"{f}={conf[f]:.2f}" for f in low
        )
    return False, "all critical fields above threshold"


def _read_company_budget_eur_mc(conn) -> int:
    """Read res.company.ai_monthly_budget_eur from Odoo; convert to EUR mc.

    Budget is stored as a Float (EUR) on the company for UX. Converted
    here to millicents to match ai_usage_log bookkeeping (integer math).
    Returns 0 when the field is missing, unparseable, or set to 0 —
    treated as "no cap, pipeline runs unconstrained".

    Multi-company caveat: reads the FIRST company (lowest id) in the
    database. Most Odoo tenants have a single operating entity; for
    multi-company setups the cap applies globally at the connection
    level, not per-company. A future v2 can accept a company_id hint
    on ctx for per-entity budgets.
    """
    try:
        company_ids = conn.execute_kw(
            "res.company", "search", [[]],
            {"limit": 1, "order": "id asc"},
        )
        if not company_ids:
            return 0
        rows = conn.execute_kw(
            "res.company", "read", [company_ids],
            {"fields": ["ai_monthly_budget_eur"]},
        )
    except Exception as e:  # noqa: BLE001 — field may be absent on legacy DBs
        logger.debug("budget read skipped: %s", e)
        return 0
    if not rows:
        return 0
    val = rows[0].get("ai_monthly_budget_eur")
    try:
        eur = float(val or 0.0)
    except (TypeError, ValueError):
        return 0
    if eur <= 0:
        return 0
    # 1 EUR = 100 cents = 100_000 millicents
    return int(round(eur * 100_000))


def _step_guard_monthly_budget(ctx: PipelineContext) -> dict:
    """Abort the pipeline when this month's spend reached the company cap.

    Runs before extraction so we never bill against a cap that's
    already hit. On hit:
      * post a chatter note with current spend + limit
      * mark the move with ai_review_reason='budget_exceeded' via the
        same field the glue skill uses (direct write — skill won't run)
      * clear ai_pipeline_requested so the move drops out of scan_pending
      * abort the pipeline — no vision call, no usage log row
    Budget = 0 (disabled) skips the check entirely.
    """
    budget_mc = _read_company_budget_eur_mc(ctx.odoo_conn)
    if budget_mc <= 0:
        return {"enforced": False, "budget_eur_mc": 0}
    spent_mc = ai_usage_log.monthly_cost_eur_mc(ctx.tenant_code)
    ctx.data["budget_eur_mc"] = budget_mc
    ctx.data["spent_eur_mc"] = spent_mc
    if spent_mc < budget_mc:
        return {
            "enforced": True,
            "budget_eur_mc": budget_mc,
            "spent_eur_mc": spent_mc,
            "remaining_eur_mc": budget_mc - spent_mc,
        }
    # Budget exceeded — mark + abort.
    ctx.abort = True
    ctx.abort_reason = "budget_exceeded"
    writes = {
        "ai_needs_review": True,
        "ai_pipeline_requested": False,
    }
    # Writing ai_review_reason directly requires the glue module — guard.
    try:
        fields_def = ctx.odoo_conn.execute_kw(
            "account.move", "fields_get", [["ai_review_reason"]],
            {"attributes": ["string"]},
        )
        if "ai_review_reason" in fields_def:
            writes["ai_review_reason"] = "budget_exceeded"
    except Exception as e:  # noqa: BLE001
        logger.debug("ai_review_reason not on this DB: %s", e)
    try:
        ctx.odoo_conn.execute_kw(
            "account.move", "write", [[ctx.move_id], writes],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("budget guard write failed: %s", e)
    body = (
        "<b>🛑 AI pipeline aborted — monthly budget exceeded</b>"
        f"<br/>This month so far: €{spent_mc / 100_000:.2f}"
        f"<br/>Company cap: €{budget_mc / 100_000:.2f}"
        "<br/>Raise the cap in Settings → Companies → AI Invoice Posting, "
        "or wait for the new billing month."
    )
    try:
        ctx.odoo_conn.execute_kw(
            "account.move", "message_post", [[ctx.move_id]],
            {"body": body, "message_type": "comment",
             "subtype_xmlid": "mail.mt_note"},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("budget guard chatter failed: %s", e)
    return {
        "enforced": True,
        "aborted": True,
        "budget_eur_mc": budget_mc,
        "spent_eur_mc": spent_mc,
    }


def _step_retrieve_few_shot_examples(ctx: PipelineContext) -> dict:
    """Pull a few past posted bills from the same partner for in-context priming.

    Phase 1 is deliberately simple: direct Odoo query on account.move filtered
    by partner_id + state='posted' + move_type in (in_invoice, in_refund).
    No Qdrant here — semantic fallback (when partner is unknown or the
    archive is empty) lands in Phase 2 once a first vision pass can surface
    a partner candidate.

    Skipped silently when:
      * the move has no partner_id yet (cold extraction — nothing to anchor
        on; the model will ask for everything)
      * partner has fewer than 1 historical posted bill

    Output shape (stored on ctx.data['few_shot_examples']):
        [
          {"partner_name": "...", "invoice_number": "INV-1",
           "invoice_date": "2026-03-15", "amount_untaxed": 100.0,
           "amount_tax": 20.0, "amount_total": 120.0,
           "currency": "BGN",
           "lines": [{"description": "...", "quantity": ...,
                       "price_unit": ..., "amount_subtotal": ...},
                      ...up to 3 representative lines]},
          ...
        ]
    """
    move = ctx.move or {}
    # move['partner_id'] is an Odoo [id, name] tuple when populated.
    partner_field = move.get("partner_id")
    partner_id: int | None = None
    if isinstance(partner_field, (list, tuple)) and partner_field:
        partner_id = partner_field[0]
    elif isinstance(partner_field, int):
        partner_id = partner_field

    if not partner_id:
        return {"skipped": "no partner_id on move"}

    try:
        past_moves = ctx.odoo_conn.execute_kw(
            "account.move", "search_read",
            [[
                ["partner_id", "=", partner_id],
                ["state", "=", "posted"],
                ["move_type", "in", ["in_invoice", "in_refund"]],
                ["id", "!=", ctx.move_id],
            ]],
            {"fields": [
                "id", "name", "partner_id", "ref",
                "invoice_date", "amount_untaxed", "amount_tax",
                "amount_total", "currency_id",
            ],
             "limit": 3, "order": "invoice_date desc"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("few-shot retrieval skipped: %s", e)
        return {"skipped": f"odoo query error: {e}"}

    if not past_moves:
        return {"found": 0}

    past_ids = [m["id"] for m in past_moves]
    # Pull up to 3 representative lines per past move for shape reference.
    try:
        all_lines = ctx.odoo_conn.execute_kw(
            "account.move.line", "search_read",
            [[
                ["move_id", "in", past_ids],
                ["display_type", "=", False],  # skip section/note rows
            ]],
            {"fields": [
                "move_id", "name", "quantity", "price_unit",
                "price_subtotal", "product_uom_id", "account_id",
            ],
             "order": "move_id asc, sequence asc"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("few-shot line fetch skipped: %s", e)
        all_lines = []

    lines_by_move: dict[int, list[dict]] = {}
    for ln in all_lines:
        move_ref = ln.get("move_id")
        mid = move_ref[0] if isinstance(move_ref, (list, tuple)) else move_ref
        lines_by_move.setdefault(mid, []).append(ln)

    # Partner→account histogram across a bigger window (Gap 2.2).
    # Same partner, last 30 posted expense lines — collapse to frequency.
    account_histogram = _collect_partner_account_histogram(
        ctx.odoo_conn, partner_id, ctx.move_id,
    )

    examples: list[dict] = []
    for m in past_moves:
        currency_ref = m.get("currency_id") or [None, ""]
        partner_ref = m.get("partner_id") or [None, ""]
        lines = lines_by_move.get(m["id"], [])[:3]
        examples.append({
            "partner_name": partner_ref[1] if len(partner_ref) > 1 else "",
            "invoice_number": m.get("ref") or m.get("name") or "",
            "invoice_date": m.get("invoice_date") or "",
            "amount_untaxed": m.get("amount_untaxed") or 0.0,
            "amount_tax": m.get("amount_tax") or 0.0,
            "amount_total": m.get("amount_total") or 0.0,
            "currency": currency_ref[1] if len(currency_ref) > 1 else "",
            "lines": [{
                "description": ln.get("name") or "",
                "quantity": ln.get("quantity") or 0,
                "price_unit": ln.get("price_unit") or 0.0,
                "amount_subtotal": ln.get("price_subtotal") or 0.0,
            } for ln in lines],
        })

    ctx.data["few_shot_examples"] = examples
    if account_histogram:
        ctx.data["partner_account_history"] = account_histogram
    return {
        "found": len(examples),
        "partner_id": partner_id,
        "account_history": len(account_histogram),
    }


def _collect_partner_account_histogram(
    conn, partner_id: int, current_move_id: int | None,
) -> list[dict]:
    """Frequency of account.account codes used on past lines from partner.

    Looks across the last ~30 posted expense moves of the partner and
    counts how often each account.account code appears on their lines.
    The output is pre-sorted descending by frequency, truncated to
    top 5 — this is what the vision model sees as "historically this
    vendor ends up coded to 602xxxxx 80% of the time, so treat it as
    the default suggestion unless the line text clearly says otherwise".
    Returns [] on any query failure — the pipeline never fails because
    analytics are missing.
    """
    try:
        history_move_ids = conn.execute_kw(
            "account.move", "search",
            [[
                ["partner_id", "=", partner_id],
                ["state", "=", "posted"],
                ["move_type", "in", ["in_invoice", "in_refund"]],
                ["id", "!=", current_move_id or 0],
            ]],
            {"limit": 30, "order": "invoice_date desc"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("partner account history skipped: %s", e)
        return []
    if not history_move_ids:
        return []
    try:
        lines = conn.execute_kw(
            "account.move.line", "search_read",
            [[
                ["move_id", "in", history_move_ids],
                ["display_type", "=", False],
                ["account_id", "!=", False],
            ]],
            {"fields": ["account_id"]},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("partner account lines skipped: %s", e)
        return []
    counts: dict[tuple[int, str], int] = {}
    for ln in lines:
        acc_ref = ln.get("account_id")
        if not acc_ref:
            continue
        if isinstance(acc_ref, (list, tuple)):
            key = (acc_ref[0], acc_ref[1] if len(acc_ref) > 1 else "")
        else:
            key = (int(acc_ref), "")
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return []
    total = sum(counts.values()) or 1
    hist = [
        {
            "account_id": acc_id,
            "account_label": acc_label,
            "count": cnt,
            "share": round(cnt / total, 3),
        }
        for (acc_id, acc_label), cnt in sorted(
            counts.items(), key=lambda kv: kv[1], reverse=True,
        )
    ]
    return hist[:5]


def _step_extract_vision(ctx: PipelineContext) -> dict:
    """Call Anthropic Vision via ai_vision_service."""
    import base64 as _b64
    if not ctx.attachment_id:
        ctx.abort = True
        ctx.abort_reason = "no attachment to extract"
        return {"error": ctx.abort_reason}
    atts = ctx.odoo_conn.execute_kw(
        "ir.attachment", "read", [[ctx.attachment_id]],
        {"fields": ["name", "datas", "mimetype", "file_size"]},
    )
    if not atts or not atts[0].get("datas"):
        ctx.abort = True
        ctx.abort_reason = "attachment empty"
        return {"error": ctx.abort_reason}
    att = atts[0]
    file_bytes = _b64.b64decode(att["datas"])
    mimetype = att.get("mimetype") or "application/pdf"

    # Strip JS / embedded files / auto-actions from PDFs before routing
    # to Claude. Graceful on failure — returns originals so a malformed
    # PDF still gets extracted (sanitizer reports the parse error).
    if mimetype == "application/pdf":
        file_bytes, sanitize_report = pdf_sanitizer.sanitize_pdf(file_bytes)
        ctx.data["pdf_sanitize"] = sanitize_report.to_dict()
    else:
        ctx.data["pdf_sanitize"] = {
            "available": True, "modified": False, "skipped": "not a pdf",
        }

    result = ai_vision_service.extract_invoice(
        file_bytes=file_bytes,
        mimetype=mimetype,
        api_key=ctx.api_key,
        base_url=ctx.base_url,
        tenant_tier=ctx.tenant_tier,
        few_shot_examples=ctx.data.get("few_shot_examples") or None,
        partner_account_hints=ctx.data.get("partner_account_history") or None,
    )
    ctx.data["vision_result"] = result
    ctx.data["first_pass_model"] = result.model
    ctx.data["first_pass_state"] = result.state

    # Two-pass escalation: if haiku produced a shaky read and the tenant
    # opted in, re-run with sonnet. Both runs land in ai_usage_log via
    # the log_usage step — we re-log the first pass here so the caller
    # has a record even when sonnet overwrites ctx.data["vision_result"].
    should, reason = _needs_escalation(result)
    if should and _read_company_bool_flag(ctx.odoo_conn, "ai_two_pass_escalation"):
        # Persist the first-pass usage row before overwriting.
        try:
            ai_usage_log.log_extraction(
                tenant_code=ctx.tenant_code,
                odoo_url=ctx.odoo_conn.url,
                odoo_db=ctx.odoo_conn.db,
                move_id=ctx.move_id,
                attachment_id=ctx.attachment_id,
                source=ctx.source,
                extra={"pass": "first", "escalation_reason": reason},
                **result.to_log_kwargs(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("first-pass log failed: %s", e)

        retry = ai_vision_service.extract_invoice(
            file_bytes=file_bytes,
            mimetype=mimetype,
            api_key=ctx.api_key,
            base_url=ctx.base_url,
            tenant_tier=ctx.tenant_tier,
            few_shot_examples=ctx.data.get("few_shot_examples") or None,
            partner_account_hints=ctx.data.get("partner_account_history") or None,
            model_override="claude-sonnet-4-6",
        )
        ctx.data["vision_result"] = retry
        ctx.data["escalated"] = True
        ctx.data["escalation_reason"] = reason
        result = retry
    else:
        ctx.data["escalated"] = False
        if should:
            ctx.data["escalation_reason"] = (
                f"{reason} (escalation disabled on company)"
            )
    ctx.data["attachment_name"] = att.get("name")
    ctx.data["attachment_size"] = att.get("file_size")
    if result.state == "error":
        ctx.data["next_step"] = STEP_RETRY_EXTRACTION
    return {
        "state": result.state, "model": result.model,
        "tokens_in": result.input_tokens, "tokens_out": result.output_tokens,
        "cost_eur_mc": result.cost_eur_millicents,
    }


def _step_normalize_bg_fields(ctx: PipelineContext) -> dict:
    """Clean up BG-specific fields the vision prompt produced.

    Runs between extract and log_usage so both the billed ledger row
    and the chatter audit record reflect the canonical values. See
    bg_validators.normalize_extracted_bg_fields for the concrete
    rules; this step is a thin adapter that also records the
    normalisation report on the context for downstream consumers.
    """
    result = ctx.data.get("vision_result")
    if not result or result.state != "success":
        return {"skipped": "no successful extraction"}
    data = result.extracted_data or {}
    report = bg_validators.normalize_extracted_bg_fields(data)
    ctx.data["bg_normalize"] = report
    return report


def _step_log_usage(ctx: PipelineContext) -> dict:
    """Write ai_usage_log row from vision result."""
    result = ctx.data.get("vision_result")
    if not result:
        return {"skipped": "no vision_result"}
    log_id = ai_usage_log.log_extraction(
        tenant_code=ctx.tenant_code,
        odoo_url=ctx.odoo_conn.url,
        odoo_db=ctx.odoo_conn.db,
        move_id=ctx.move_id,
        attachment_id=ctx.attachment_id,
        source=ctx.source,
        source_message_id=ctx.source_message_id if ctx.source == "gmail" else None,
        extra={
            "attachment_name": ctx.data.get("attachment_name"),
            "attachment_size": ctx.data.get("attachment_size"),
        },
        **result.to_log_kwargs(),
    )
    ctx.data["log_id"] = log_id
    return {"log_id": log_id}


def _fmt_amount(value) -> str:
    """Render a numeric amount for chatter tables; falls back to '-'."""
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_confidence_badge(conf: float) -> str:
    """Coloured span for per-field confidence — green/amber/red."""
    if conf >= 0.85:
        colour = "#1f883d"   # green
    elif conf >= 0.70:
        colour = "#bf8700"   # amber
    else:
        colour = "#d1242f"   # red
    return (
        f'<span style="color:{colour};font-weight:600">'
        f"{conf * 100:.0f}%</span>"
    )


def _render_extraction_chatter(
    *, data: dict, field_confidence: dict | None,
    arithmetic_note: str, attachment_id: int | None,
    model: str, prompt_version: str,
    escalated: bool = False,
    pdf_sanitize: dict | None = None,
) -> str:
    """Build a human-readable HTML summary for the chatter.

    Replaces the old raw ``json.dumps`` dump with a structured table +
    top-5 lines + confidence badges. Keeps a collapsible details block
    with the full JSON for the accountant who wants the raw response.
    """
    import json as _json
    fc = field_confidence or {}
    data = data or {}
    pdf_sanitize = pdf_sanitize or {}

    rows = [
        ("Document type",
         str(data.get("document_type") or "-"),
         fc.get("document_type")),
        ("BG doc type",
         str(data.get("l10n_bg_document_type") or "-"),
         None),
        ("Supplier",
         str(data.get("partner_name") or "-"),
         fc.get("partner_name")),
        ("Supplier VAT",
         str(data.get("partner_vat") or "-"),
         fc.get("partner_vat")),
        ("EIK",
         str(data.get("partner_eik") or "-"),
         None),
        ("Invoice №",
         str(data.get("invoice_number") or "-"),
         fc.get("invoice_number")),
        ("Invoice date",
         str(data.get("invoice_date") or "-"),
         fc.get("invoice_date")),
        ("Currency",
         str(data.get("currency") or "-"),
         None),
        ("Untaxed",
         _fmt_amount(data.get("amount_untaxed")),
         fc.get("amount_untaxed")),
        ("Tax",
         _fmt_amount(data.get("amount_tax")),
         fc.get("amount_tax")),
        ("Total",
         _fmt_amount(data.get("amount_total")),
         fc.get("amount_total")),
    ]
    mrn = data.get("customs_mrn")
    if mrn:
        rows.append(("Customs MRN", str(mrn), None))

    table_rows = "".join(
        f"<tr><th style='text-align:left;padding:2px 8px;white-space:nowrap'>"
        f"{label}</th>"
        f"<td style='padding:2px 8px'>{value}</td>"
        f"<td style='padding:2px 8px;text-align:right'>"
        f"{_fmt_confidence_badge(conf) if conf is not None else ''}</td></tr>"
        for label, value, conf in rows
    )

    lines = data.get("lines") or []
    line_rows_html = ""
    if lines:
        line_rows = []
        for ln in lines[:5]:
            desc = str(ln.get("description") or "")[:80]
            qty = ln.get("quantity") or 0
            price = _fmt_amount(ln.get("price_unit"))
            subtotal = _fmt_amount(ln.get("amount_subtotal"))
            tax_rate = ln.get("tax_rate")
            tax_str = f"{tax_rate}%" if tax_rate not in (None, "") else "-"
            line_rows.append(
                f"<tr>"
                f"<td style='padding:2px 8px'>{desc}</td>"
                f"<td style='padding:2px 8px;text-align:right'>{qty}</td>"
                f"<td style='padding:2px 8px;text-align:right'>{price}</td>"
                f"<td style='padding:2px 8px;text-align:right'>{tax_str}</td>"
                f"<td style='padding:2px 8px;text-align:right'>{subtotal}</td>"
                f"</tr>"
            )
        overflow = ""
        if len(lines) > 5:
            overflow = (
                f"<div style='font-size:10px;color:#666;margin-top:4px'>"
                f"…{len(lines) - 5} more line(s) truncated</div>"
            )
        line_rows_html = (
            "<b>Lines</b>"
            "<table style='font-size:11px;border-collapse:collapse;margin-top:4px'>"
            "<thead><tr>"
            "<th style='text-align:left;padding:2px 8px'>Description</th>"
            "<th style='text-align:right;padding:2px 8px'>Qty</th>"
            "<th style='text-align:right;padding:2px 8px'>Unit</th>"
            "<th style='text-align:right;padding:2px 8px'>VAT</th>"
            "<th style='text-align:right;padding:2px 8px'>Subtotal</th>"
            "</tr></thead><tbody>"
            + "".join(line_rows)
            + "</tbody></table>"
            + overflow
        )

    lines_overall_conf = fc.get("lines_overall")
    lines_conf_html = (
        f"<div style='font-size:11px;margin-top:2px'>"
        f"Lines confidence: {_fmt_confidence_badge(lines_overall_conf)}</div>"
        if lines_overall_conf is not None else ""
    )

    header_badges = []
    header_badges.append(
        f"<span style='background:#e6f0ff;padding:2px 6px;"
        f"border-radius:3px;font-size:10px'>"
        f"model: {model}</span>"
    )
    header_badges.append(
        f"<span style='background:#e6f0ff;padding:2px 6px;"
        f"border-radius:3px;font-size:10px'>"
        f"prompt: {prompt_version}</span>"
    )
    if escalated:
        header_badges.append(
            "<span style='background:#fff4e0;padding:2px 6px;"
            "border-radius:3px;font-size:10px'>two-pass</span>"
        )
    if pdf_sanitize.get("modified"):
        header_badges.append(
            "<span style='background:#ffe0e0;padding:2px 6px;"
            "border-radius:3px;font-size:10px'>PDF sanitised</span>"
        )

    arith_colour = "#1f883d" if arithmetic_note == "arithmetic OK" else "#bf8700"
    arith_html = (
        f"<div style='font-size:11px;margin-top:6px;color:{arith_colour}'>"
        f"Arithmetic: {arithmetic_note}</div>"
    )

    raw_json_html = (
        "<details style='margin-top:8px;font-size:11px'>"
        "<summary>Raw JSON</summary>"
        "<pre style='font-size:10px;white-space:pre-wrap'>"
        + _json.dumps(data, indent=2, ensure_ascii=False)[:4000]
        + "</pre></details>"
    )

    return (
        "<b>🤖 AI Extracted Invoice</b><br/>"
        f"<div style='margin:4px 0'>{' '.join(header_badges)}</div>"
        f"<div style='font-size:10px;color:#666'>"
        f"attachment #{attachment_id}</div>"
        "<table style='font-size:11px;border-collapse:collapse;margin-top:6px'>"
        + table_rows
        + "</table>"
        + lines_conf_html
        + (f"<div style='margin-top:8px'>{line_rows_html}</div>" if line_rows_html else "")
        + arith_html
        + raw_json_html
    )


def _check_arithmetic(data: dict, tolerance: float = 0.02) -> tuple[bool, str]:
    """Sanity-check the extracted totals.

    Returns (ok, note).  Two invariants:
      1. sum(lines[*].amount_subtotal) ≈ amount_untaxed
      2. amount_untaxed + amount_tax      ≈ amount_total

    `tolerance` is absolute, in the invoice's currency. 2 cents is enough
    to absorb rounding but small enough to catch real OCR mistakes.
    Returns ok=True when a value is missing — we only flag definite
    mismatches, not absences (those are caught by confidence scoring).
    """
    notes: list[str] = []
    ok = True
    lines = data.get("lines") or []
    try:
        lines_sum = sum(float(l.get("amount_subtotal") or 0) for l in lines)
    except (TypeError, ValueError):
        lines_sum = None
    untaxed = data.get("amount_untaxed")
    tax = data.get("amount_tax")
    total = data.get("amount_total")
    try:
        untaxed_f = float(untaxed) if untaxed is not None else None
        tax_f = float(tax) if tax is not None else None
        total_f = float(total) if total is not None else None
    except (TypeError, ValueError):
        return True, "arithmetic: non-numeric totals (skipped)"

    if lines_sum is not None and untaxed_f is not None and lines:
        if abs(lines_sum - untaxed_f) > tolerance:
            ok = False
            notes.append(
                f"sum(lines)={lines_sum:.2f} != untaxed={untaxed_f:.2f}"
            )
    if untaxed_f is not None and tax_f is not None and total_f is not None:
        if abs(untaxed_f + tax_f - total_f) > tolerance:
            ok = False
            notes.append(
                f"untaxed+tax={untaxed_f + tax_f:.2f} != total={total_f:.2f}"
            )
    return ok, "; ".join(notes) or "arithmetic OK"


def _step_write_back_move(ctx: PipelineContext) -> dict:
    """Apply extracted fields to draft account.move (empty-field only)."""
    result = ctx.data.get("vision_result")
    if not result or result.state != "success" or not result.extracted_data:
        return {"skipped": "no successful extraction"}
    move = ctx.move or {}
    if move.get("state") != "draft":
        return {"skipped": f"state={move.get('state')} not draft"}
    data = result.extracted_data

    # Arithmetic reconciliation — independent of confidence, always run.
    arith_ok, arith_note = _check_arithmetic(data)
    ctx.data["arithmetic_ok"] = arith_ok
    ctx.data["arithmetic_note"] = arith_note

    writes: dict = {}
    if not move.get("partner_id") and data.get("partner_vat"):
        vat = data["partner_vat"].replace(" ", "").upper()
        partner_ids = ctx.odoo_conn.execute_kw(
            "res.partner", "search", [[["vat", "=", vat]]], {"limit": 1},
        )
        if partner_ids:
            writes["partner_id"] = partner_ids[0]
    if not move.get("invoice_date") and data.get("invoice_date"):
        writes["invoice_date"] = data["invoice_date"]
    if not move.get("ref") and data.get("invoice_number"):
        writes["ref"] = data["invoice_number"]

    if writes:
        ctx.odoo_conn.execute_kw(
            "account.move", "write", [[ctx.move_id], writes],
        )
    # Always post to chatter (audit) — human-readable summary.
    body = _render_extraction_chatter(
        data=data,
        field_confidence=result.field_confidence,
        arithmetic_note=arith_note,
        attachment_id=ctx.attachment_id,
        model=result.model,
        prompt_version=result.prompt_version,
        escalated=ctx.data.get("escalated", False),
        pdf_sanitize=ctx.data.get("pdf_sanitize"),
    )
    try:
        ctx.odoo_conn.execute_kw(
            "account.move", "message_post", [[ctx.move_id]],
            {"body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_note"},
        )
        chatter = True
    except Exception as e:  # noqa: BLE001
        logger.warning("chatter post failed: %s", e)
        chatter = False
    ctx.data["writeback"] = {
        "fields_written": list(writes.keys()),
        "chatter": chatter,
        "arithmetic_ok": arith_ok,
        "arithmetic_note": arith_note,
    }
    ctx.data["next_step"] = STEP_AWAIT_SKILL_POST
    return ctx.data["writeback"]


def _step_invoke_posting_skill(ctx: PipelineContext) -> dict:
    """Fire vendor-bill-posting-bg skill if glue module is installed.

    Passes pipeline hints the skill needs for weighted scoring via a
    ``composite_fields`` dict on the ctx handed to the skill:
      - ``_field_confidence`` : per-field 0..1 from vision prompt v2
      - ``_arithmetic_ok`` / ``_arithmetic_note`` : from write-back step
      - ``_prompt_version`` : which schema version produced the data

    Older skill versions that ignore ``composite_fields`` still work —
    they just fall back to field-presence scoring.
    """
    result = ctx.data.get("vision_result")
    if not result or result.state != "success":
        return {"skipped": "no successful extraction"}
    composite_fields = {
        "_field_confidence": dict(result.field_confidence),
        "_arithmetic_ok": ctx.data.get("arithmetic_ok", True),
        "_arithmetic_note": ctx.data.get("arithmetic_note", ""),
        "_prompt_version": result.prompt_version,
        "_extractor_model": result.model,
    }
    skill_ctx = {"composite_fields": composite_fields}
    try:
        rv = ctx.odoo_conn.execute_kw(
            "account.move", "_skill_post_vendor_bill",
            [[ctx.move_id], skill_ctx], {},
        )
        ctx.data["skill_invoked"] = True
        ctx.data["skill_result"] = rv
        return {"invoked": True, "result": rv}
    except Exception as e:  # noqa: BLE001
        # Module probably not installed — gracefully skip
        msg = str(e)[:200]
        ctx.data["skill_invoked"] = False
        return {"skipped": "skill not available", "error": msg}


# ─── Register built-ins ──────────────────────────────────

registry.register(Step(
    name="probe_move",
    sequence=10,
    execute=_step_probe_move,
    description="Read account.move + attachments from Odoo",
    on_error="abort",
))

registry.register(Step(
    name="guard_monthly_budget",
    sequence=15,
    execute=_step_guard_monthly_budget,
    applies_when=lambda c: c.move_id is not None and c.source != "force",
    description="Abort if this month's spend exceeds company budget cap",
    on_error="continue",
))

registry.register(Step(
    name="guard_already_extracted",
    sequence=20,
    execute=_step_guard_already_extracted,
    applies_when=lambda c: c.move_id is not None and c.source != "force",
    description="Short-circuit if move was already extracted successfully",
))

registry.register(Step(
    name="retrieve_few_shot_examples",
    sequence=50,
    execute=_step_retrieve_few_shot_examples,
    applies_when=lambda c: c.move_id is not None,
    description="Pull top-3 past posted bills from same partner for in-context priming",
    on_error="skip",
))

registry.register(Step(
    name="extract_vision",
    sequence=100,
    execute=_step_extract_vision,
    applies_when=lambda c: c.attachment_id is not None and bool(c.api_key),
    description="Anthropic Vision extraction with model routing",
    on_error="abort",
))

registry.register(Step(
    name="normalize_bg_fields",
    sequence=150,
    execute=_step_normalize_bg_fields,
    applies_when=lambda c: (
        c.data.get("vision_result") is not None
        and c.data["vision_result"].state == "success"
    ),
    description="Normalise BG VAT/EIK/MRN, hint on art.117 misclassification",
    on_error="skip",
))

registry.register(Step(
    name="log_usage",
    sequence=200,
    execute=_step_log_usage,
    applies_when=lambda c: "vision_result" in c.data,
    description="Write billing ledger row (always, success or failure)",
))

registry.register(Step(
    name="write_back_move",
    sequence=210,
    execute=_step_write_back_move,
    applies_when=lambda c: (
        c.move_id is not None
        and c.data.get("vision_result") is not None
        and c.data["vision_result"].state == "success"
    ),
    description="Write empty Odoo fields + post JSON to chatter",
))

registry.register(Step(
    name="invoke_posting_skill",
    sequence=300,
    execute=_step_invoke_posting_skill,
    applies_when=lambda c: (
        c.move_id is not None
        and c.data.get("vision_result") is not None
        and c.data["vision_result"].state == "success"
        and c.move_type in ("in_invoice", "in_refund")
    ),
    description="Invoke l10n_bg_ai_invoice_glue vendor-bill-posting-bg skill",
))


# ─── Plugin auto-discovery ───────────────────────────────


def load_plugins(plugins_dir: str | Path) -> list[str]:
    """Import all *.py files in plugins_dir; each must have register(registry).

    Returns the list of plugin names loaded (by filename stem).
    """
    loaded: list[str] = []
    p = Path(plugins_dir)
    if not p.is_dir():
        logger.info("plugins_dir %s not present — skipping plugin discovery", p)
        return loaded
    for file in sorted(p.glob("*.py")):
        if file.name.startswith("_"):
            continue
        name = f"ai_invoice_plugin_{file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, file)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            if hasattr(mod, "register"):
                mod.register(registry)
                loaded.append(file.stem)
                logger.info("Loaded pipeline plugin: %s", file.stem)
            else:
                logger.warning("Plugin %s has no register(registry) — skipped", file.stem)
        except Exception as e:  # noqa: BLE001
            logger.exception("Plugin %s failed to load: %s", file.stem, e)
    return loaded


# ─── Reporting / dashboard helpers ───────────────────────


@dataclass
class MoveStack:
    """Cross-layer inspection snapshot (for dashboard cards)."""
    move_id: int
    move_name: str | None = None
    move_type: str | None = None
    state: str | None = None
    partner_name: str | None = None
    invoice_date: str | None = None
    ref: str | None = None
    amount_total: float | None = None
    currency: str | None = None
    attachments: list[dict] = field(default_factory=list)
    extraction_history: list[dict] = field(default_factory=list)
    last_extraction: dict | None = None
    successful_extractions: int = 0
    failed_extractions: int = 0
    cache_hits: int = 0
    ai_post_confidence: float | None = None
    ai_needs_review: bool | None = None
    ai_post_log: str | None = None
    company_threshold: float | None = None
    company_autopost: bool | None = None
    next_step: str = STEP_UNKNOWN
    blockers: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def inspect_stack(*, conn, move_id: int, tenant_code: str) -> MoveStack:
    """Read-only introspection of the move across all three layers."""
    # Probe for glue fields
    try:
        fgetfields = conn.execute_kw(
            "account.move", "fields_get", [], {"attributes": ["string"]},
        )
        has_glue = "ai_post_confidence" in fgetfields
    except Exception:
        has_glue = False

    fields = [
        "name", "move_type", "state", "partner_id",
        "invoice_date", "ref", "amount_total", "currency_id",
        "company_id",
    ]
    if has_glue:
        fields += ["ai_post_confidence", "ai_needs_review", "ai_post_log"]

    rows = conn.execute_kw("account.move", "read", [[move_id]], {"fields": fields})
    if not rows:
        s = MoveStack(move_id=move_id)
        s.blockers.append(f"account.move {move_id} not found")
        return s
    m = rows[0]

    partner = m.get("partner_id") or [None, None]
    currency = m.get("currency_id") or [None, None]

    stack = MoveStack(
        move_id=m["id"],
        move_name=m.get("name"),
        move_type=m.get("move_type"),
        state=m.get("state"),
        partner_name=partner[1],
        invoice_date=str(m["invoice_date"]) if m.get("invoice_date") else None,
        ref=m.get("ref"),
        amount_total=m.get("amount_total"),
        currency=currency[1],
        ai_post_confidence=m.get("ai_post_confidence"),
        ai_needs_review=m.get("ai_needs_review"),
        ai_post_log=m.get("ai_post_log"),
    )
    stack.attachments = conn.execute_kw(
        "ir.attachment", "search_read",
        [[["res_model", "=", "account.move"], ["res_id", "=", move_id]]],
        {"fields": ["id", "name", "mimetype", "file_size"]},
    )
    # Logs
    all_logs = ai_usage_log.query(tenant_code=tenant_code, limit=10_000)
    move_logs = [r for r in all_logs if r.get("move_id") == move_id]
    stack.extraction_history = move_logs
    if move_logs:
        stack.last_extraction = move_logs[0]
    stack.successful_extractions = sum(1 for r in move_logs if r.get("state") == "success")
    stack.failed_extractions = sum(1 for r in move_logs if r.get("state") == "error")
    stack.cache_hits = sum(1 for r in move_logs if r.get("state") == "cached")

    # Company config (glue thresholds)
    cid = (m.get("company_id") or [None])[0] if isinstance(m.get("company_id"), list) else None
    if has_glue:
        try:
            c = conn.execute_kw(
                "res.company", "read", [[cid or 1]],
                {"fields": ["ai_post_confidence_threshold", "ai_post_autopost"]},
            )
            if c:
                stack.company_threshold = c[0].get("ai_post_confidence_threshold")
                stack.company_autopost = c[0].get("ai_post_autopost")
        except Exception:
            pass

    stack.next_step, stack.blockers, stack.hints = _decide_next_step(stack)
    return stack


def _decide_next_step(s: MoveStack) -> tuple[str, list[str], list[str]]:
    blockers: list[str] = []
    hints: list[str] = []
    if s.state == "posted":
        return STEP_POSTED, blockers, hints
    if s.state == "cancel":
        return STEP_CANCELLED, blockers, hints
    if s.move_type not in ("in_invoice", "in_refund"):
        hints.append(f"move_type={s.move_type} — skill applies only to vendor bills")
    if not s.attachments:
        return STEP_AWAIT_ATTACHMENT, blockers, ["Upload a PDF/image invoice"]
    if not s.extraction_history:
        return STEP_AWAIT_EXTRACTION, blockers, ["Run ai_invoice_extract on the attachment"]
    last = s.last_extraction or {}
    if last.get("state") == "error":
        blockers.append(f"Last extraction errored: {(last.get('error_message') or '')[:200]}")
        return STEP_RETRY_EXTRACTION, blockers, ["Check API key / try different model_override"]
    if s.ai_post_confidence is None and s.move_type in ("in_invoice", "in_refund"):
        hints.append("Extraction done but glue skill has not fired — is module installed?")
    if s.ai_needs_review:
        return STEP_AWAIT_REVIEW, blockers, ["Accountant review needed"]
    threshold = s.company_threshold or 0.85
    conf = s.ai_post_confidence or 0
    if conf < threshold:
        blockers.append(f"Confidence {conf:.2f} < threshold {threshold:.2f}")
        return STEP_AWAIT_SKILL_POST, blockers, [f"Improve signal or post manually"]
    if not s.company_autopost:
        return STEP_READY_TO_POST, blockers, ["Autopost disabled — post manually"]
    hints.append("Autopost=True & confidence OK — if not posted, check skill log")
    return STEP_READY_TO_POST, blockers, hints


# ─── Batch flows ──────────────────────────────────────────


def scan_pending(
    *, conn, tenant_code: str,
    move_types: tuple[str, ...] = ("in_invoice", "in_refund"),
    limit: int = 50,
    requested_only: bool = False,
) -> list[dict]:
    """Find draft vendor-bill moves that have an attachment and no log row.

    When ``requested_only`` is True, also require
    ``ai_pipeline_requested=True`` on the move — i.e. only moves that
    were explicitly queued by the glue module's attachment auto-trigger.
    Handy for cron workloads: ``scan_pending(requested_only=True)`` picks
    up just the moves that the user (or the auto-trigger) actually asked
    to process, ignoring old drafts that happen to have a PDF attached.
    """
    domain = [["state", "=", "draft"], ["move_type", "in", list(move_types)]]
    if requested_only:
        domain.append(["ai_pipeline_requested", "=", True])
    move_ids = conn.execute_kw(
        "account.move", "search", [domain],
        {"limit": 500, "order": "create_date asc"},
    )
    if not move_ids:
        return []
    atts = conn.execute_kw(
        "ir.attachment", "search_read",
        [[
            ["res_model", "=", "account.move"],
            ["res_id", "in", move_ids],
            ["mimetype", "in", [
                "application/pdf", "image/png", "image/jpeg",
                "image/jpg", "image/gif", "image/webp",
            ]],
        ]],
        {"fields": ["id", "name", "mimetype", "file_size", "res_id", "create_date"],
         "order": "create_date asc"},
    )
    if not atts:
        return []
    logs = ai_usage_log.query(tenant_code=tenant_code, limit=10_000)
    extracted = {
        r["move_id"] for r in logs
        if r.get("move_id") and r.get("state") in ("success", "cached")
    }
    out: list[dict] = []
    seen: set[int] = set()
    for a in atts:
        mid = a["res_id"]
        if mid in extracted or mid in seen:
            continue
        seen.add(mid)
        out.append({
            "move_id": mid,
            "attachment_id": a["id"],
            "attachment_name": a["name"],
            "mimetype": a["mimetype"],
            "file_size": a.get("file_size", 0),
            "create_date": a.get("create_date"),
        })
        if len(out) >= limit:
            break
    return out


# ─── Odoo-driven pipeline executor ───────────────────────
#
# Odoo owns the pipeline definition via `ai.pipeline.step` records
# (pipeline/sequence/model/method/skill_id/trigger_domain/on_error).
# MCP executor reads those records and dispatches each step:
#
#   * model starts with ``mcp.`` or equals ``mcp`` → MCP-local step
#     (looked up in this module's `registry` by `method` name)
#   * otherwise → RPC call `conn.execute_kw(model, method, [ctx])`
#
# This lets Odoo users see the pipeline definition in the UI (see the
# "AI Pipeline → Pipeline Steps" screen) while specific high-latency or
# secret-handling steps run inside MCP.
#
# Skill gating and trigger_domain are evaluated by the MCP side — same
# semantics as ``ai.pipeline.step._matches``.


def _eval_flat_domain(domain: list, ctx: dict) -> bool:
    """Mirror of ai.pipeline.step._eval_flat_domain (Odoo-side)."""
    for leaf in domain or []:
        if not isinstance(leaf, (list, tuple)) or len(leaf) != 3:
            continue
        field_name, op, value = leaf
        left = ctx.get(field_name)
        if op == "=":
            if left != value:
                return False
        elif op == "!=":
            if left == value:
                return False
        elif op == "in":
            if left not in (value or []):
                return False
        elif op == "not in":
            if left in (value or []):
                return False
        else:
            return False
    return True


def _step_matches_odoo(step_rec: dict, ctx: dict) -> tuple[bool, str]:
    """Return (applies, reason). reason is non-empty when skipped."""
    import ast
    skill_id = (step_rec.get("skill_id") or [None])[0] if isinstance(step_rec.get("skill_id"), list) else step_rec.get("skill_id")
    if skill_id:
        matched = ctx.get("matched_skill_ids") or []
        if skill_id not in matched:
            return False, f"skill #{skill_id} not matched"
    dom_raw = step_rec.get("trigger_domain")
    if dom_raw:
        try:
            dom = ast.literal_eval(dom_raw)
        except (ValueError, SyntaxError) as e:
            return False, f"bad trigger_domain: {e}"
        if not _eval_flat_domain(dom, ctx):
            return False, "trigger_domain did not match"
    return True, ""


@dataclass
class OdooStepRun:
    step_id: int
    name: str
    sequence: int
    model: str
    method: str
    status: str                    # ok | skip | error | abort
    duration_ms: int
    message: str = ""
    output_keys: list[str] = field(default_factory=list)


@dataclass
class OdooPipelineRun:
    pipeline: str
    tenant_code: str
    source_model: str
    source_id: int
    duration_ms: int
    aborted: bool
    abort_reason: str
    step_runs: list[OdooStepRun] = field(default_factory=list)
    final_ctx: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pipeline": self.pipeline,
            "tenant_code": self.tenant_code,
            "source_model": self.source_model,
            "source_id": self.source_id,
            "duration_ms": self.duration_ms,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "step_runs": [asdict(s) for s in self.step_runs],
            "final_ctx": _strip_unserializable(self.final_ctx),
        }


def _strip_unserializable(ctx: dict) -> dict:
    """Return a JSON-safe copy of ctx (flatten dataclasses + drop heavy raw blobs)."""
    out = {}
    for k, v in ctx.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, dict, tuple)):
            out[k] = v
        else:
            # Dataclass / object — try asdict, else str()
            try:
                out[k] = asdict(v)
            except Exception:
                out[k] = str(v)[:200]
    return out


def _fetch_odoo_steps(conn, pipeline: str) -> list[dict]:
    return conn.execute_kw(
        "ai.pipeline.step", "search_read",
        [[["pipeline", "=", pipeline], ["active", "=", True]]],
        {"fields": [
            "id", "name", "pipeline", "sequence",
            "model", "method", "skill_id", "trigger_domain",
            "on_error", "module",
        ], "order": "sequence, id"},
    )


def _update_step_runtime(conn, step_id: int, state: str, message: str) -> None:
    from datetime import datetime, timezone
    try:
        conn.execute_kw(
            "ai.pipeline.step", "write",
            [[step_id], {
                "last_run_state": state,
                "last_run_message": (message or "")[:4000],
                "last_run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("pipeline step #%s stats update failed: %s", step_id, e)


def _dispatch_mcp(method: str, ctx: dict, extras: dict) -> dict:
    """Route MCP-native step calls.

    `extras` carries runtime invariants (api_key, base_url, odoo_conn, tenant_*)
    that are not stored in ctx (keep ctx JSON-safe).
    """
    # Lookup in registry by method name (case-insensitive equivalent)
    step = None
    for s in registry.all():
        if s.name == method:
            step = s
            break
    if step is None:
        raise ValueError(f"No MCP step registered with name={method!r}")

    # Build a PipelineContext bridge from the ctx dict
    pctx = PipelineContext(
        odoo_conn=extras["odoo_conn"],
        tenant_code=extras.get("tenant_code", ""),
        tenant_tier=extras.get("tenant_tier", "business"),
        api_key=extras.get("api_key", ""),
        base_url=extras.get("base_url", "https://api.anthropic.com"),
        move_id=ctx.get("source_id") if ctx.get("source_model") == "account.move" else None,
        attachment_id=ctx.get("attachment_id"),
        source=ctx.get("source", "upload"),
        source_message_id=ctx.get("source_message_id"),
        data=ctx.setdefault("mcp_data", {}),
    )
    output = step.execute(pctx)
    # Propagate PipelineContext side-effects back into ctx
    ctx["mcp_data"] = pctx.data
    if pctx.attachment_id and not ctx.get("attachment_id"):
        ctx["attachment_id"] = pctx.attachment_id
    if pctx.abort:
        ctx["mcp_abort"] = pctx.abort_reason
    return output if isinstance(output, dict) else {"ok": True}


def run_odoo_pipeline(
    *,
    conn,
    pipeline: str,
    source_model: str,
    source_id: int,
    tenant_code: str,
    tenant_tier: str = "business",
    api_key: str = "",
    base_url: str = "https://api.anthropic.com",
    extra_ctx: dict | None = None,
    update_step_stats: bool = True,
) -> OdooPipelineRun:
    """Execute the Odoo-defined pipeline step list.

    Returns an `OdooPipelineRun` with a per-step audit. MCP-native steps
    (model starting with ``mcp``) are dispatched to this module's
    `registry`; Odoo-side steps are invoked via RPC.
    """
    t0 = time.monotonic()
    steps = _fetch_odoo_steps(conn, pipeline)

    # Runtime invariants (not serialized into ctx)
    extras = {
        "odoo_conn": conn,
        "tenant_code": tenant_code,
        "tenant_tier": tenant_tier,
        "api_key": api_key,
        "base_url": base_url,
    }

    # Build initial ctx — Odoo-side pipelines follow these conventions
    ctx: dict = {
        "source_model": source_model,
        "source_id": source_id,
        "tenant_code": tenant_code,
        "matched_skill_ids": [],        # populated by `skill_resolution` step
        "log": [],                       # human-readable trail
    }
    if extra_ctx:
        ctx.update(extra_ctx)

    step_runs: list[OdooStepRun] = []
    aborted = False
    abort_reason = ""

    for st in steps:
        if aborted:
            step_runs.append(OdooStepRun(
                step_id=st["id"], name=st["name"], sequence=st["sequence"],
                model=st["model"], method=st["method"],
                status="abort", duration_ms=0,
                message=f"aborted earlier: {abort_reason}",
            ))
            continue

        applies, skip_reason = _step_matches_odoo(st, ctx)
        if not applies:
            step_runs.append(OdooStepRun(
                step_id=st["id"], name=st["name"], sequence=st["sequence"],
                model=st["model"], method=st["method"],
                status="skip", duration_ms=0, message=skip_reason,
            ))
            if update_step_stats:
                _update_step_runtime(conn, st["id"], "skipped", skip_reason)
            continue

        t_step = time.monotonic()
        try:
            if (st["model"] or "").startswith("mcp"):
                output = _dispatch_mcp(st["method"], ctx, extras)
            else:
                # Odoo RPC: env[model].<method>(ctx) → new_ctx
                new_ctx = conn.execute_kw(st["model"], st["method"], [ctx])
                if isinstance(new_ctx, dict):
                    ctx = new_ctx
                    ctx.setdefault("matched_skill_ids", [])
                output = {"rpc_ok": True}
            duration = int((time.monotonic() - t_step) * 1000)
            step_runs.append(OdooStepRun(
                step_id=st["id"], name=st["name"], sequence=st["sequence"],
                model=st["model"], method=st["method"],
                status="ok", duration_ms=duration,
                output_keys=list(output.keys()) if isinstance(output, dict) else [],
            ))
            if update_step_stats:
                _update_step_runtime(conn, st["id"], "ok",
                                     f"output keys: {list(output.keys())[:5] if isinstance(output, dict) else ''}")
            # MCP step may have set an abort signal
            if ctx.get("mcp_abort"):
                aborted = True
                abort_reason = f"MCP step {st['name']}: {ctx['mcp_abort']}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            logger.exception("Pipeline step %s failed", st["name"])
            duration = int((time.monotonic() - t_step) * 1000)
            policy = (st.get("on_error") or "skip").lower()
            status = "error"
            if policy == "abort":
                aborted = True
                abort_reason = f"step {st['name']}: {err}"
                status = "error"
            # retry handled by caller — MVP treats it as error once
            step_runs.append(OdooStepRun(
                step_id=st["id"], name=st["name"], sequence=st["sequence"],
                model=st["model"], method=st["method"],
                status=status, duration_ms=duration, message=err,
            ))
            if update_step_stats:
                _update_step_runtime(conn, st["id"], "error", err)

    return OdooPipelineRun(
        pipeline=pipeline,
        tenant_code=tenant_code,
        source_model=source_model,
        source_id=source_id,
        duration_ms=int((time.monotonic() - t0) * 1000),
        aborted=aborted,
        abort_reason=abort_reason,
        step_runs=step_runs,
        final_ctx=ctx,
    )


def pipeline_summary(*, conn, tenant_code: str) -> dict:
    """Header-card aggregation for dashboard: count moves by next_step."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
    move_ids = conn.execute_kw(
        "account.move", "search",
        [[
            ["state", "in", ["draft", "posted"]],
            ["move_type", "in", ["in_invoice", "in_refund"]],
            ["create_date", ">=", since],
        ]],
        {"limit": 500, "order": "create_date desc"},
    )
    counts: dict[str, int] = {}
    for mid in move_ids:
        try:
            stack = inspect_stack(conn=conn, move_id=mid, tenant_code=tenant_code)
            counts[stack.next_step] = counts.get(stack.next_step, 0) + 1
        except Exception as e:  # noqa: BLE001
            logger.warning("pipeline_summary: %s failed: %s", mid, e)
            counts["unknown"] = counts.get("unknown", 0) + 1
    return {
        "tenant_code": tenant_code,
        "since": since,
        "total_moves": len(move_ids),
        "by_step": counts,
        "registered_steps": registry.names(),
    }
