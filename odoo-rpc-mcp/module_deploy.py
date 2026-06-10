"""
v3 Module Deploy (B.2) — industrialize the Odoo addon deploy recipe:
  rsync addon → ephemeral `-u <module> --stop-after-init` → restart → runtime gate.

The recipe (from hard-won field experience) is otherwise done by hand per client:
  1. rsync the module dir to the host's addon path (with a pre-deploy backup tar
     of the existing dir, for rollback).
  2. (optional) scrub stale view arch_db via psql -f (NEVER heredoc-over-ssh-stdin).
  3. ephemeral upgrade: docker stop main → docker run --rm ... -u <module>
     --stop-after-init [--i18n-overwrite] → docker start main.
  4. runtime gate: module state==installed, installed_version==local manifest,
     no ERROR/CRITICAL in ir.logging since start, and /web/health green.

SAFETY:
  * DRY_RUN ON by default (MCP_MODULE_DEPLOY_DRY_RUN=1): plan only, no rsync,
    no docker, no SQL. Real runs need MCP_MODULE_DEPLOY_DRY_RUN=0 or dry_run=false.
  * Admin-principal gated in server.py (same bar as provision_/fleet_/secrets_).
  * Reuses server SSH transport (_ssh_execute bridge+ControlMaster), the Odoo RPC
    client (OdooConnection.execute_kw), provisioning_engine.wait_for_health, and
    the fleet_manager JSONL ledger pattern.

Deploy target config lives in connections.json under the alias, in a `deploy`
sub-dict (addons_path, container, db_container, ephemeral_image, network, db) —
falling back to legacy top-level keys (container/db_container/compose_path).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Callable

import provisioning_engine as pe

logger = logging.getLogger("module_deploy")

DEPLOY_OPS_FILE = Path(os.environ.get(
    "MODULE_DEPLOY_OPS_FILE", "/data/module_deploy_ops.jsonl"))
DRY_RUN = os.environ.get("MCP_MODULE_DEPLOY_DRY_RUN", "1") == "1"
REPOS_DIR = os.environ.get("REPOS_DIR", "/repos")
UPGRADE_TIMEOUT = int(os.environ.get("MCP_MODULE_DEPLOY_TIMEOUT", "900"))
HEALTH_PATH = os.environ.get("MCP_MODULE_DEPLOY_HEALTH_PATH", "/web/health")
# connections.json candidate paths (mirror server._get_ssh_config).
_CONN_CANDIDATES = [
    Path(os.environ.get("CONNECTIONS_FILE", "/data/connections.json")),
    Path("/config/connections.json"),
    Path.home() / "Проекти" / "odoo" / "odoo-18.0" / "claude.ai"
        / ".odoo_connections" / "connections.json",
]

# Callbacks wired from server.py.
_ssh_execute: Callable | None = None
_ensure_ssh_master: Callable | None = None
_get_conn: Callable | None = None


def wire(*, ssh_execute: Callable | None = None,
         ensure_ssh_master: Callable | None = None,
         get_conn: Callable | None = None) -> None:
    """Inject server.py transport + RPC. Call once at startup."""
    global _ssh_execute, _ensure_ssh_master, _get_conn
    _ssh_execute = ssh_execute
    _ensure_ssh_master = ensure_ssh_master
    _get_conn = get_conn


# ─── Op ledger (mirrors fleet_manager) ─────────────────────────────────────

def _ops_replay() -> list[dict]:
    out: list[dict] = []
    if not DEPLOY_OPS_FILE.is_file():
        return out
    try:
        with open(DEPLOY_OPS_FILE, "r", encoding="utf-8") as f:
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
    DEPLOY_OPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DEPLOY_OPS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(DEPLOY_OPS_FILE, 0o600)
    except OSError:
        pass


def _last_op(target: str, module: str, kind: str | None = None) -> dict | None:
    for rec in reversed(_ops_replay()):
        if rec.get("target") == target and rec.get("module") == module \
                and (kind is None or rec.get("kind") == kind):
            return rec
    return None


# ─── Target config ─────────────────────────────────────────────────────────

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


def _get_deploy_config(alias: str) -> dict:
    """Merged deploy config: `deploy` sub-dict + legacy top-level keys + ssh."""
    entry = _load_entry(alias)
    if not entry:
        return {}
    dep = dict(entry.get("deploy", {}) or {})
    # Legacy fallbacks (4 connections carry these at top level).
    for k in ("container", "db_container", "compose_path", "compose_dir",
              "config_path", "db"):
        if k not in dep and entry.get(k):
            dep[k] = entry[k]
    dep["ssh"] = entry.get("ssh", {}) or {}
    return dep


def _find_local_module(module: str, source: str | None) -> dict:
    """Resolve the local module dir + manifest version."""
    if source:
        path = Path(source)
    else:
        # Glob $REPOS_DIR/**/<module>/__manifest__.py (first hit).
        path = None
        base = Path(REPOS_DIR)
        if base.is_dir():
            for mf in base.glob(f"**/{module}/__manifest__.py"):
                path = mf.parent
                break
    if not path or not (path / "__manifest__.py").is_file():
        return {"error": "local_module_not_found", "module": module,
                "searched": source or REPOS_DIR}
    version = ""
    try:
        txt = (path / "__manifest__.py").read_text(encoding="utf-8")
        # cheap manifest version extraction (avoid exec of arbitrary manifest)
        import ast
        node = ast.literal_eval(txt) if txt.strip().startswith("{") else {}
        version = str(node.get("version", "")) if isinstance(node, dict) else ""
    except Exception:
        version = ""
    return {"path": str(path), "version": version}


# ─── SSH helper ────────────────────────────────────────────────────────────

def _ssh(ssh_cfg: dict, command: str, timeout: int = 60) -> dict:
    if _ssh_execute is None:
        return {"status": "error", "error": "ssh transport not wired"}
    host = ssh_cfg.get("host", "")
    user = ssh_cfg.get("user", "")
    port = int(ssh_cfg.get("port", 22) or 22)
    if not host or not user:
        return {"status": "error", "error": "incomplete ssh config (host/user)"}
    return _ssh_execute(host, user, command, port=port, timeout=timeout)


def _ctrl_path(ssh_cfg: dict) -> str | None:
    if _ensure_ssh_master is None:
        return None
    try:
        return _ensure_ssh_master(ssh_cfg.get("user", ""), ssh_cfg.get("host", ""),
                                  int(ssh_cfg.get("port", 22) or 22),
                                  ssh_cfg.get("identity_file"))
    except Exception:
        return None


# ─── Deploy steps ──────────────────────────────────────────────────────────

def _rsync_module(dep: dict, module: str, src: str, dry_run: bool) -> dict:
    ssh_cfg = dep.get("ssh", {})
    addons = dep.get("addons_path", "")
    if not addons:
        return {"error": "no_addons_path",
                "hint": "add deploy.addons_path to the connection entry"}
    host = ssh_cfg.get("host", "")
    user = ssh_cfg.get("user", "")
    port = int(ssh_cfg.get("port", 22) or 22)
    dest = f"{user}@{host}:{addons.rstrip('/')}/{module}/"
    ctrl = _ctrl_path(ssh_cfg)
    ssh_opt = f"ssh -p {port}" + (f" -o ControlPath={shlex.quote(ctrl)}" if ctrl else "")
    cmd = ["rsync", "-az", "--delete", "--exclude", "__pycache__",
           "--exclude", "*.pyc", "-e", ssh_opt,
           f"{src.rstrip('/')}/", dest]
    plan = {"rsync": " ".join(shlex.quote(c) for c in cmd), "dest": dest}
    if dry_run:
        return {"ok": True, "dry_run": True, **plan}
    # Pre-deploy backup of the existing remote module dir (for rollback).
    backup = f"{addons.rstrip('/')}/.mcp_backup/{module}.{int(time.time())}.tar.gz"
    bcmd = (f"mkdir -p {shlex.quote(addons.rstrip('/') + '/.mcp_backup')} && "
            f"if [ -d {shlex.quote(addons.rstrip('/') + '/' + module)} ]; then "
            f"tar czf {shlex.quote(backup)} -C {shlex.quote(addons.rstrip('/'))} "
            f"{shlex.quote(module)}; fi")
    b = _ssh(ssh_cfg, bcmd, timeout=300)
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0
        return {"ok": ok, "rcode": r.returncode, "backup": backup if b.get("status") == "ok" else None,
                "stderr": r.stderr[-500:] if not ok else "", **plan}
    except Exception as e:
        return {"error": "rsync_failed", "detail": str(e), **plan}


def _ephemeral_upgrade(dep: dict, module: str, i18n_overwrite: bool,
                       dry_run: bool) -> dict:
    ssh_cfg = dep.get("ssh", {})
    container = dep.get("container", "")
    db = dep.get("db", "")
    image = dep.get("ephemeral_image", "")
    network = dep.get("network", f"container:{container}" if container else "")
    addons = dep.get("addons_path", "")
    if not container or not db or not image:
        return {"error": "incomplete_deploy_config",
                "need": ["container", "db", "ephemeral_image"],
                "have": {k: dep.get(k) for k in ("container", "db", "ephemeral_image")}}
    flags = f"-u {shlex.quote(module)} --stop-after-init"
    if i18n_overwrite:
        flags += " --i18n-overwrite"
    vol = f"-v {shlex.quote(addons)}:{shlex.quote(addons)}:ro" if addons else ""
    net = f"--network {shlex.quote(network)}" if network else ""
    up_cmd = (f"docker run --rm {net} {vol} {shlex.quote(image)} "
              f"odoo -d {shlex.quote(db)} {flags}")
    steps = {
        "stop": f"docker stop {shlex.quote(container)}",
        "upgrade": up_cmd,
        "start": f"docker start {shlex.quote(container)}",
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "steps": steps}
    out = {}
    s = _ssh(ssh_cfg, steps["stop"], timeout=120)
    out["stop"] = s
    u = _ssh(ssh_cfg, steps["upgrade"], timeout=UPGRADE_TIMEOUT)
    out["upgrade"] = u
    # Always attempt to restart main, even if upgrade failed.
    st = _ssh(ssh_cfg, steps["start"], timeout=120)
    out["start"] = st
    up_log = (u.get("stdout", "") + u.get("stderr", ""))
    failed = (u.get("status") != "ok" or u.get("exit_code", 0) not in (0, None)
              or any(m in up_log for m in ("Traceback", "CRITICAL", "Failed to load")))
    out["ok"] = not failed
    return out


def _runtime_gate(alias: str, module: str, dep: dict, since_ts: int,
                  local_version: str) -> dict:
    checks = {}
    # 1+2: module state + installed version via RPC.
    if _get_conn is not None:
        try:
            conn = _get_conn(alias)
            rows = conn.execute_kw(
                "ir.module.module", "search_read",
                [[["name", "=", module]]],
                {"fields": ["state", "installed_version", "latest_version"]})
            if rows:
                checks["state"] = rows[0].get("state")
                checks["installed_version"] = rows[0].get("installed_version")
                checks["state_ok"] = rows[0].get("state") == "installed"
                checks["version_ok"] = (not local_version) or \
                    (str(rows[0].get("installed_version", "")).endswith(local_version)
                     or local_version in str(rows[0].get("installed_version", "")))
            else:
                checks["state"] = "absent"
                checks["state_ok"] = False
            # 3: ERROR/CRITICAL in ir.logging since deploy start.
            try:
                from datetime import datetime, timezone
                since = datetime.fromtimestamp(since_ts, tz=timezone.utc)\
                    .strftime("%Y-%m-%d %H:%M:%S")
                errs = conn.execute_kw(
                    "ir.logging", "search_count",
                    [[["level", "in", ["ERROR", "CRITICAL"]],
                      ["create_date", ">=", since]]])
                checks["log_errors"] = errs
                checks["log_ok"] = (errs == 0)
            except Exception as e:
                checks["log_ok"] = None
                checks["log_note"] = str(e)
        except Exception as e:
            checks["rpc_error"] = str(e)
    # 4: health.
    conn_url = ""
    try:
        conn_url = (_get_conn(alias).url if _get_conn else "")
    except Exception:
        conn_url = ""
    if conn_url:
        checks["health"] = pe.wait_for_health(conn_url.rstrip("/") + HEALTH_PATH,
                                              timeout=30)
    passed = (checks.get("state_ok") is True
              and checks.get("version_ok") is not False
              and checks.get("log_ok") is not False
              and (checks.get("health", {}).get("healthy") is not False))
    checks["passed"] = bool(passed)
    return checks


# ─── Public ops ────────────────────────────────────────────────────────────

def deploy_module(target: str, module: str, *, source: str | None = None,
                  i18n_overwrite: bool = False, dry_run: bool | None = None) -> dict:
    """rsync → ephemeral -u → restart → runtime gate, with op ledger."""
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    dep = _get_deploy_config(target)
    if not dep:
        return {"error": "unknown_target_or_no_deploy_config", "target": target}
    if not dep.get("ssh", {}).get("host"):
        return {"error": "no_ssh_config_for_target", "target": target}
    local = _find_local_module(module, source)
    if local.get("error"):
        return local
    started = int(time.time())
    base = {"target": target, "module": module, "kind": "deploy",
            "local_version": local.get("version"), "ts": started,
            "i18n_overwrite": i18n_overwrite}

    rs = _rsync_module(dep, module, local["path"], is_dry)
    up = _ephemeral_upgrade(dep, module, i18n_overwrite, is_dry)

    if is_dry:
        rec = {**base, "dry_run": True, "rsync": rs, "upgrade": up}
        _ops_append(rec)
        return {"ok": True, "dry_run": True, "target": target, "module": module,
                "plan": {"rsync": rs, "upgrade": up},
                "hint": "Set MCP_MODULE_DEPLOY_DRY_RUN=0 (or dry_run=false) to apply."}

    if rs.get("error") or rs.get("ok") is False:
        _ops_append({**base, "result": "rsync_failed", "rsync": rs})
        return {"error": "rsync_failed", "detail": rs, "target": target}
    if up.get("ok") is False:
        _ops_append({**base, "result": "upgrade_failed", "rsync": rs, "upgrade": up,
                     "backup": rs.get("backup")})
        return {"error": "upgrade_failed", "detail": up, "target": target,
                "backup": rs.get("backup"),
                "hint": "module_deploy_rollback restores the pre-deploy backup."}

    gate = _runtime_gate(target, module, dep, started, local.get("version", ""))
    result = "ok" if gate.get("passed") else "gate_failed"
    _ops_append({**base, "result": result, "rsync": rs, "upgrade": up,
                 "gate": gate, "backup": rs.get("backup")})
    return {"ok": gate.get("passed", False), "target": target, "module": module,
            "from_version": None, "to_version": gate.get("installed_version"),
            "gate": gate, "backup": rs.get("backup")}


def deploy_status(target: str, module: str) -> dict:
    """Read-only: run the runtime gate only, no mutation."""
    dep = _get_deploy_config(target)
    if not dep:
        return {"error": "unknown_target_or_no_deploy_config", "target": target}
    local = _find_local_module(module, None)
    gate = _runtime_gate(target, module, dep, 0, local.get("version", ""))
    return {"target": target, "module": module,
            "local_version": local.get("version"), "gate": gate}


def deploy_rollback(target: str, module: str, dry_run: bool | None = None) -> dict:
    """Restore the pre-deploy backup tar from the last deploy op + re-upgrade."""
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    dep = _get_deploy_config(target)
    if not dep:
        return {"error": "unknown_target_or_no_deploy_config", "target": target}
    last = _last_op(target, module, kind="deploy")
    backup = (last or {}).get("backup")
    if not backup:
        return {"error": "no_backup_to_restore", "target": target, "module": module}
    ssh_cfg = dep.get("ssh", {})
    addons = dep.get("addons_path", "")
    restore = (f"tar xzf {shlex.quote(backup)} -C {shlex.quote(addons.rstrip('/'))}")
    if is_dry:
        return {"ok": True, "dry_run": True, "target": target, "module": module,
                "restore_cmd": restore, "backup": backup}
    r = _ssh(ssh_cfg, restore, timeout=300)
    up = _ephemeral_upgrade(dep, module, False, False)
    _ops_append({"target": target, "module": module, "kind": "rollback",
                 "backup": backup, "restore": r, "upgrade": up,
                 "ts": int(time.time())})
    return {"ok": (r.get("status") == "ok" and up.get("ok")),
            "target": target, "module": module, "restore": r, "upgrade": up}


def deploy_history(target: str | None = None) -> dict:
    recs = _ops_replay()
    if target:
        recs = [r for r in recs if r.get("target") == target]
    # compact view
    out = [{"target": r.get("target"), "module": r.get("module"),
            "kind": r.get("kind"), "result": r.get("result", "dry_run" if r.get("dry_run") else "?"),
            "ts": r.get("ts")} for r in recs]
    return {"count": len(out), "ops": out}


# ─── Registration triple ───────────────────────────────────────────────────

def get_admin_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(
            name="module_deploy",
            description=("ADMIN: deploy an Odoo addon to a target — rsync the "
                         "module, run an ephemeral `-u` upgrade, restart, then a "
                         "runtime gate (module installed + version + no log errors "
                         "+ health). DRY-RUN by default (plan only) unless "
                         "MCP_MODULE_DEPLOY_DRY_RUN=0 or dry_run=false."),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "connection alias"},
                    "module": {"type": "string"},
                    "source": {"type": "string",
                               "description": "local module dir (default: search REPOS_DIR)"},
                    "i18n_overwrite": {"type": "boolean", "default": False},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["target", "module"],
            },
        ),
        Tool(
            name="module_deploy_status",
            description=("ADMIN: read-only runtime gate for a module on a target "
                         "(state, installed vs local version, log errors, health)."),
            inputSchema={
                "type": "object",
                "properties": {"target": {"type": "string"},
                               "module": {"type": "string"}},
                "required": ["target", "module"],
            },
        ),
        Tool(
            name="module_deploy_rollback",
            description=("ADMIN: restore the pre-deploy backup of a module on a "
                         "target and re-run the ephemeral upgrade. DRY-RUN by default."),
            inputSchema={
                "type": "object",
                "properties": {"target": {"type": "string"},
                               "module": {"type": "string"},
                               "dry_run": {"type": "boolean"}},
                "required": ["target", "module"],
            },
        ),
        Tool(
            name="module_deploy_history",
            description="ADMIN: list past deploy/rollback ops (optionally per target).",
            inputSchema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
            },
        ),
    ]


ADMIN_TOOL_NAMES = {"module_deploy", "module_deploy_status",
                    "module_deploy_rollback", "module_deploy_history"}


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "module_deploy":
        return deploy_module(arguments.get("target", ""),
                             arguments.get("module", ""),
                             source=arguments.get("source"),
                             i18n_overwrite=arguments.get("i18n_overwrite", False),
                             dry_run=arguments.get("dry_run"))
    if name == "module_deploy_status":
        return deploy_status(arguments.get("target", ""),
                             arguments.get("module", ""))
    if name == "module_deploy_rollback":
        return deploy_rollback(arguments.get("target", ""),
                               arguments.get("module", ""),
                               arguments.get("dry_run"))
    if name == "module_deploy_history":
        return deploy_history(arguments.get("target"))
    return {"error": f"unknown module_deploy tool: {name}"}
