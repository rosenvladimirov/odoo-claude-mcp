"""
v3 Backup / DR (B.5) — per-tenant DB dump + filestore archive → S3, and restore
to a staging target.

Reuses the server SSH transport (_ssh_execute) to run remote docker exec
pg_dump + filestore tar, then (optionally) rclone the artifacts to an S3 remote.
Deploy/backup target config comes from connections.json (`deploy`/legacy keys:
container, db_container, db, filestore_path; plus `backup`: rclone_remote, retention).

SAFETY:
  * DRY_RUN ON by default (MCP_BACKUP_DRY_RUN=1): plan only, no remote command.
  * restore is GUARDED — it never targets a production db unless the target
    entry has deploy.allow_restore=True; default goes to a `<db>_staging` db.
  * Admin-principal gated in server.py.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("backup_manager")

BACKUP_OPS_FILE = Path(os.environ.get("BACKUP_OPS_FILE", "/data/backup_ops.jsonl"))
DRY_RUN = os.environ.get("MCP_BACKUP_DRY_RUN", "1") == "1"
REMOTE_BACKUP_DIR = os.environ.get("MCP_BACKUP_REMOTE_DIR", "/var/backups/mcp")
_CONN_CANDIDATES = [
    Path(os.environ.get("CONNECTIONS_FILE", "/data/connections.json")),
    Path("/config/connections.json"),
    Path.home() / "Проекти" / "odoo" / "odoo-18.0" / "claude.ai"
        / ".odoo_connections" / "connections.json",
]

_ssh_execute: Callable | None = None
_ensure_ssh_master: Callable | None = None


def wire(*, ssh_execute: Callable | None = None,
         ensure_ssh_master: Callable | None = None) -> None:
    global _ssh_execute, _ensure_ssh_master
    _ssh_execute = ssh_execute
    _ensure_ssh_master = ensure_ssh_master


# ─── ledger ────────────────────────────────────────────────────────────────

def _ops_replay() -> list[dict]:
    out: list[dict] = []
    if not BACKUP_OPS_FILE.is_file():
        return out
    try:
        with open(BACKUP_OPS_FILE, "r", encoding="utf-8") as f:
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


def _ops_append(record: dict) -> None:
    BACKUP_OPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BACKUP_OPS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(BACKUP_OPS_FILE, 0o600)
    except OSError:
        pass


# ─── target config ─────────────────────────────────────────────────────────

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


def _cfg(alias: str) -> dict:
    entry = _load_entry(alias)
    if not entry:
        return {}
    dep = dict(entry.get("deploy", {}) or {})
    for k in ("container", "db_container", "compose_path", "db", "filestore_path"):
        if k not in dep and entry.get(k):
            dep[k] = entry[k]
    dep["ssh"] = entry.get("ssh", {}) or {}
    dep["backup"] = entry.get("backup", {}) or {}
    return dep


def _ssh(ssh_cfg: dict, command: str, timeout: int = 120) -> dict:
    if _ssh_execute is None:
        return {"status": "error", "error": "ssh transport not wired"}
    host = ssh_cfg.get("host", "")
    user = ssh_cfg.get("user", "")
    port = int(ssh_cfg.get("port", 22) or 22)
    if not host or not user:
        return {"status": "error", "error": "incomplete ssh config"}
    return _ssh_execute(host, user, command, port=port, timeout=timeout)


# ─── ops ───────────────────────────────────────────────────────────────────

def tenant_backup(target: str, dry_run: bool | None = None) -> dict:
    """pg_dump + filestore tar on the host, optional rclone to S3."""
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    dep = _cfg(target)
    if not dep:
        return {"error": "unknown_target_or_no_config", "target": target}
    db_container = dep.get("db_container")
    db = dep.get("db")
    if not db_container or not db:
        return {"error": "incomplete_backup_config",
                "need": ["db_container", "db"], "have": {
                    "db_container": db_container, "db": db}}
    stamp = int(time.time())
    base = f"{REMOTE_BACKUP_DIR}/{target}/{db}-{stamp}"
    dump_cmd = (f"mkdir -p {shlex.quote(REMOTE_BACKUP_DIR + '/' + target)} && "
                f"docker exec {shlex.quote(db_container)} pg_dump -Fc -d "
                f"{shlex.quote(db)} > {shlex.quote(base + '.dump')}")
    fs = dep.get("filestore_path")
    fs_cmd = (f"tar czf {shlex.quote(base + '.filestore.tar.gz')} -C "
              f"{shlex.quote(fs)} ." if fs else "")
    rclone_remote = dep.get("backup", {}).get("rclone_remote")
    rclone_cmd = (f"rclone copy {shlex.quote(base + '.dump')} "
                  f"{shlex.quote(rclone_remote)}/{target}/" if rclone_remote else "")
    plan = {"dump": dump_cmd, "filestore": fs_cmd, "rclone": rclone_cmd,
            "artifact": base}
    base_rec = {"target": target, "kind": "backup", "db": db, "artifact": base,
                "ts": stamp}
    if is_dry:
        _ops_append({**base_rec, "dry_run": True, "plan": plan})
        return {"ok": True, "dry_run": True, "target": target, "plan": plan,
                "hint": "Set MCP_BACKUP_DRY_RUN=0 (or dry_run=false) to apply."}
    ssh_cfg = dep["ssh"]
    out = {"dump": _ssh(ssh_cfg, dump_cmd, timeout=1800)}
    if fs_cmd:
        out["filestore"] = _ssh(ssh_cfg, fs_cmd, timeout=1800)
    if rclone_cmd:
        out["rclone"] = _ssh(ssh_cfg, rclone_cmd, timeout=1800)
    ok = out["dump"].get("status") == "ok"
    _ops_append({**base_rec, "result": "ok" if ok else "failed", "steps": out})
    try:
        import metrics
        metrics.observe_backup_write("backup", target)
    except Exception:
        pass
    return {"ok": ok, "target": target, "artifact": base, "steps": out}


def tenant_restore(target: str, artifact: str = "", to_staging: bool = True,
                   dry_run: bool | None = None) -> dict:
    """Restore a dump to a staging db (default) — guarded against prod overwrite."""
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    dep = _cfg(target)
    if not dep:
        return {"error": "unknown_target_or_no_config", "target": target}
    db_container = dep.get("db_container")
    db = dep.get("db")
    if not db_container or not db:
        return {"error": "incomplete_backup_config"}
    allow_prod = bool(dep.get("backup", {}).get("allow_restore") or
                      dep.get("allow_restore"))
    # A prod restore (to_staging=False) requires an explicit allow_restore flag;
    # otherwise refuse rather than silently downgrade.
    if not to_staging and not allow_prod:
        return {"error": "prod_restore_blocked",
                "hint": "set deploy.allow_restore=True to restore over prod, "
                        "or use to_staging=True"}
    restore_db = db if (not to_staging and allow_prod) else f"{db}_staging"
    if not artifact:
        last = _last_backup(target)
        if not last:
            return {"error": "no_artifact_and_no_prior_backup", "target": target}
        artifact = last.get("artifact", "")
    create = (f"docker exec {shlex.quote(db_container)} psql -c "
              f"{shlex.quote('CREATE DATABASE ' + restore_db)} || true")
    restore = (f"docker exec -i {shlex.quote(db_container)} pg_restore -d "
               f"{shlex.quote(restore_db)} --clean --if-exists "
               f"< {shlex.quote(artifact + '.dump')}")
    plan = {"create_db": create, "restore": restore, "restore_db": restore_db}
    rec = {"target": target, "kind": "restore", "restore_db": restore_db,
           "artifact": artifact, "ts": int(time.time())}
    if is_dry:
        _ops_append({**rec, "dry_run": True, "plan": plan})
        return {"ok": True, "dry_run": True, "target": target, "plan": plan}
    ssh_cfg = dep["ssh"]
    out = {"create": _ssh(ssh_cfg, create, timeout=120),
           "restore": _ssh(ssh_cfg, restore, timeout=1800)}
    ok = out["restore"].get("status") == "ok"
    _ops_append({**rec, "result": "ok" if ok else "failed", "steps": out})
    return {"ok": ok, "target": target, "restore_db": restore_db, "steps": out}


def _last_backup(target: str) -> dict | None:
    for rec in reversed(_ops_replay()):
        if rec.get("target") == target and rec.get("kind") == "backup" \
                and rec.get("result") == "ok":
            return rec
    return None


def backup_list(target: str | None = None) -> dict:
    recs = [r for r in _ops_replay() if r.get("kind") == "backup"]
    if target:
        recs = [r for r in recs if r.get("target") == target]
    out = [{"target": r.get("target"), "artifact": r.get("artifact"),
            "result": r.get("result", "dry_run" if r.get("dry_run") else "?"),
            "ts": r.get("ts")} for r in recs]
    return {"count": len(out), "backups": out}


def backup_history(target: str | None = None) -> dict:
    recs = _ops_replay()
    if target:
        recs = [r for r in recs if r.get("target") == target]
    return {"count": len(recs),
            "ops": [{"target": r.get("target"), "kind": r.get("kind"),
                     "result": r.get("result", "dry_run" if r.get("dry_run") else "?"),
                     "ts": r.get("ts")} for r in recs]}


# ─── registration ──────────────────────────────────────────────────────────

def get_admin_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="tenant_backup",
             description=("ADMIN: back up a tenant — pg_dump (-Fc) + filestore "
                          "tar on the host, optional rclone to S3. DRY-RUN by "
                          "default."),
             inputSchema={"type": "object",
                          "properties": {"target": {"type": "string"},
                                         "dry_run": {"type": "boolean"}},
                          "required": ["target"]}),
        Tool(name="tenant_restore",
             description=("ADMIN: restore a tenant dump to a STAGING db by default "
                          "(guarded against prod overwrite unless allow_restore). "
                          "DRY-RUN by default."),
             inputSchema={"type": "object",
                          "properties": {"target": {"type": "string"},
                                         "artifact": {"type": "string"},
                                         "to_staging": {"type": "boolean",
                                                        "default": True},
                                         "dry_run": {"type": "boolean"}},
                          "required": ["target"]}),
        Tool(name="backup_list",
             description="ADMIN: list backups (optionally per target). Read-only.",
             inputSchema={"type": "object",
                          "properties": {"target": {"type": "string"}}}),
        Tool(name="backup_history",
             description="ADMIN: list backup/restore ops. Read-only.",
             inputSchema={"type": "object",
                          "properties": {"target": {"type": "string"}}}),
    ]


ADMIN_TOOL_NAMES = {"tenant_backup", "tenant_restore", "backup_list",
                    "backup_history"}


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "tenant_backup":
        return tenant_backup(arguments.get("target", ""), arguments.get("dry_run"))
    if name == "tenant_restore":
        return tenant_restore(arguments.get("target", ""),
                              arguments.get("artifact", ""),
                              arguments.get("to_staging", True),
                              arguments.get("dry_run"))
    if name == "backup_list":
        return backup_list(arguments.get("target"))
    if name == "backup_history":
        return backup_history(arguments.get("target"))
    return {"error": f"unknown backup tool: {name}"}
