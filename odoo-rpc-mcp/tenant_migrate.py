"""
v3 Tenant Migration (B.7) — assess + plan a tenant version migration (e.g.
Odoo 18 → 19). The riskiest operation, so this module is deliberately
PLAN-HEAVY: it backs up, restores to STAGING, assesses, smoke-gates, and emits
an ordered cutover plan. It NEVER auto-cuts-over production — the operator runs
the cutover steps explicitly.

Pipeline (migrate_assess):
  1. backup the source tenant (backup_manager.tenant_backup).
  2. restore the dump to a STAGING db (backup_manager.tenant_restore, to_staging).
  3. assess: staging health (health_monitor.stack_health if a staging stack
     exists) + record the source/target Odoo versions from config.
  4. emit a structured cutover plan (ordered, manual) + risk notes.

SAFETY:
  * DRY_RUN ON by default (MCP_MIGRATE_DRY_RUN=1) — backup/restore run in their
    own dry-run, the plan is computed without touching anything.
  * No auto-cutover: migrate_plan returns steps; there is no "apply" verb.
  * Admin-principal gated in server.py.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("tenant_migrate")

MIGRATE_OPS_FILE = Path(os.environ.get("MIGRATE_OPS_FILE", "/data/migrate_ops.jsonl"))
DRY_RUN = os.environ.get("MCP_MIGRATE_DRY_RUN", "1") == "1"
_CONN_CANDIDATES = [
    Path(os.environ.get("CONNECTIONS_FILE", "/data/connections.json")),
    Path("/config/connections.json"),
    Path.home() / "Проекти" / "odoo" / "odoo-18.0" / "claude.ai"
        / ".odoo_connections" / "connections.json",
]

_backup = None      # backup_manager module
_health = None      # health_monitor module


def wire(*, backup_manager=None, health_monitor=None) -> None:
    global _backup, _health
    _backup = backup_manager
    _health = health_monitor


def _ops_append(record: dict) -> None:
    MIGRATE_OPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MIGRATE_OPS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(MIGRATE_OPS_FILE, 0o600)
    except OSError:
        pass


def _ops_replay() -> list[dict]:
    out: list[dict] = []
    if not MIGRATE_OPS_FILE.is_file():
        return out
    try:
        with open(MIGRATE_OPS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return out


def _load_entry(alias: str) -> dict:
    for p in _CONN_CANDIDATES:
        try:
            if p.exists():
                conns = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(conns, dict) and alias in conns:
                    return conns[alias]
        except Exception:
            continue
    return {}


def _odoo_version(entry: dict) -> str:
    dep = entry.get("deploy", {}) or {}
    return str(dep.get("odoo_version") or entry.get("odoo_version") or "unknown")


def _cutover_plan(source: str, target_staging: str, from_v: str, to_v: str,
                  backup_artifact: str | None) -> list[dict]:
    return [
        {"step": 1, "action": "freeze_writes",
         "detail": f"Put {source} into maintenance / stop inbound writes."},
        {"step": 2, "action": "final_backup",
         "detail": f"Fresh backup of {source} (artifact base: {backup_artifact})."},
        {"step": 3, "action": "restore_to_target",
         "detail": f"Restore the final dump onto the {to_v} stack ({target_staging})."},
        {"step": 4, "action": "run_migration_scripts",
         "detail": f"Run the {from_v}→{to_v} upgrade (-u all on the {to_v} image, "
                   "OpenUpgrade/odoo upgrade as applicable)."},
        {"step": 5, "action": "smoke_gate",
         "detail": "stack_health on the target must be 'healthy' (HTTP + MCP probe); "
                   "verify key modules installed + no ERROR/CRITICAL in ir.logging."},
        {"step": 6, "action": "dns_cutover",
         "detail": "Point the tenant hostname at the target stack (Cloudflare)."},
        {"step": 7, "action": "monitor",
         "detail": "health_scan + watch for 24h; keep the source stack stopped but "
                   "intact for rollback."},
        {"step": 8, "action": "rollback_if_needed",
         "detail": "If the smoke gate or monitor fails: re-point DNS to source, "
                   "restart source — the pre-cutover backup is the safety net."},
    ]


def migrate_assess(source: str, target_staging: str = "",
                   to_version: str = "", dry_run: bool | None = None) -> dict:
    """Backup → restore to staging → assess → emit cutover plan."""
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    if _backup is None:
        return {"error": "tenant_migrate not wired (backup_manager missing)"}
    src_entry = _load_entry(source)
    if not src_entry:
        return {"error": "unknown_source", "source": source}
    from_v = _odoo_version(src_entry)
    tgt_entry = _load_entry(target_staging) if target_staging else {}
    to_v = to_version or (_odoo_version(tgt_entry) if tgt_entry else "unknown")

    started = int(time.time())
    steps: dict = {}

    # 1. backup source
    bk = _backup.tenant_backup(source, dry_run=is_dry)
    steps["backup"] = bk
    if bk.get("error"):
        return {"error": "backup_failed", "detail": bk}
    artifact = bk.get("artifact")

    # 2. restore to staging (the migrate target, as a staging db)
    restore_target = target_staging or source
    rs = _backup.tenant_restore(restore_target, artifact=artifact or "",
                                to_staging=True, dry_run=is_dry)
    steps["restore_staging"] = rs

    # 3. assess staging health (if a staging stack + health monitor exist)
    health = None
    if _health is not None and target_staging:
        try:
            health = _health.stack_health(target_staging)
        except Exception as e:
            health = {"error": str(e)}
    steps["staging_health"] = health

    plan = _cutover_plan(source, target_staging or f"{source}-staging",
                         from_v, to_v, artifact)
    risk_notes = [
        "Migration scripts (OpenUpgrade/odoo upgrade) are NOT auto-run — operator-driven.",
        "Back-dated transactions can break valuation recompute; verify after step 4.",
        "Keep the source stack stopped-but-intact until step 7 passes (rollback net).",
        "Custom modules: confirm each has a target-version branch before cutover.",
    ]
    rec = {"source": source, "target_staging": target_staging,
           "from_version": from_v, "to_version": to_v,
           "artifact": artifact, "ts": started,
           "dry_run": is_dry, "result": "assessed"}
    _ops_append(rec)
    return {"ok": True, "dry_run": is_dry, "source": source,
            "target_staging": target_staging or f"{source}-staging",
            "from_version": from_v, "to_version": to_v,
            "steps": steps, "cutover_plan": plan, "risk_notes": risk_notes,
            "hint": "Review the plan; cutover is operator-driven (no auto-apply). "
                    "Run migrate_assess with dry_run=false to actually stage."}


def migrate_history(source: str | None = None) -> dict:
    recs = _ops_replay()
    if source:
        recs = [r for r in recs if r.get("source") == source]
    return {"count": len(recs),
            "ops": [{"source": r.get("source"),
                     "to_version": r.get("to_version"),
                     "result": r.get("result"), "ts": r.get("ts")} for r in recs]}


# ─── registration ──────────────────────────────────────────────────────────

def get_admin_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="migrate_assess",
             description=("ADMIN: assess a tenant version migration (e.g. Odoo "
                          "18→19) — backup source, restore to staging, check "
                          "staging health, and emit an ordered MANUAL cutover "
                          "plan + risk notes. NEVER auto-cuts-over prod. DRY-RUN "
                          "by default."),
             inputSchema={"type": "object", "properties": {
                 "source": {"type": "string", "description": "source connection alias"},
                 "target_staging": {"type": "string",
                                    "description": "target/staging connection alias"},
                 "to_version": {"type": "string", "description": "e.g. '19.0'"},
                 "dry_run": {"type": "boolean"}},
                 "required": ["source"]}),
        Tool(name="migrate_history",
             description="ADMIN: list past migration assessments. Read-only.",
             inputSchema={"type": "object",
                          "properties": {"source": {"type": "string"}}}),
    ]


ADMIN_TOOL_NAMES = {"migrate_assess", "migrate_history"}


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "migrate_assess":
        return migrate_assess(arguments.get("source", ""),
                              arguments.get("target_staging", ""),
                              arguments.get("to_version", ""),
                              arguments.get("dry_run"))
    if name == "migrate_history":
        return migrate_history(arguments.get("source"))
    return {"error": f"unknown migrate tool: {name}"}
