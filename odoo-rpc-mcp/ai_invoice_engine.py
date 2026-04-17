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
    result = ai_vision_service.extract_invoice(
        file_bytes=file_bytes,
        mimetype=att.get("mimetype") or "application/pdf",
        api_key=ctx.api_key,
        base_url=ctx.base_url,
        tenant_tier=ctx.tenant_tier,
    )
    ctx.data["vision_result"] = result
    ctx.data["attachment_name"] = att.get("name")
    ctx.data["attachment_size"] = att.get("file_size")
    if result.state == "error":
        ctx.data["next_step"] = STEP_RETRY_EXTRACTION
    return {
        "state": result.state, "model": result.model,
        "tokens_in": result.input_tokens, "tokens_out": result.output_tokens,
        "cost_eur_mc": result.cost_eur_millicents,
    }


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


def _step_write_back_move(ctx: PipelineContext) -> dict:
    """Apply extracted fields to draft account.move (empty-field only)."""
    import json as _json
    result = ctx.data.get("vision_result")
    if not result or result.state != "success" or not result.extracted_data:
        return {"skipped": "no successful extraction"}
    move = ctx.move or {}
    if move.get("state") != "draft":
        return {"skipped": f"state={move.get('state')} not draft"}
    data = result.extracted_data
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
    # Always post to chatter (audit)
    body = (
        "<b>🤖 AI Extracted Invoice Data</b>"
        f"<br/>Pipeline run | attachment #{ctx.attachment_id}"
        "<pre style='font-size:11px;white-space:pre-wrap'>"
        + _json.dumps(data, indent=2, ensure_ascii=False)[:3000]
        + "</pre>"
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
    ctx.data["writeback"] = {"fields_written": list(writes.keys()), "chatter": chatter}
    ctx.data["next_step"] = STEP_AWAIT_SKILL_POST
    return ctx.data["writeback"]


def _step_invoke_posting_skill(ctx: PipelineContext) -> dict:
    """Fire vendor-bill-posting-bg skill if glue module is installed."""
    result = ctx.data.get("vision_result")
    if not result or result.state != "success":
        return {"skipped": "no successful extraction"}
    try:
        rv = ctx.odoo_conn.execute_kw(
            "account.move", "_skill_post_vendor_bill", [[ctx.move_id]], {},
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
    name="guard_already_extracted",
    sequence=20,
    execute=_step_guard_already_extracted,
    applies_when=lambda c: c.move_id is not None and c.source != "force",
    description="Short-circuit if move was already extracted successfully",
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
) -> list[dict]:
    """Find draft vendor-bill moves that have an attachment and no log row."""
    domain = [["state", "=", "draft"], ["move_type", "in", list(move_types)]]
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
