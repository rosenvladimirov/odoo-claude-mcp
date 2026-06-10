"""
v3 Fleet Manager (B.1) — inventory + orchestrated image upgrade/rollback of the
deployed MCP stacks.

Design goals (multi-client deployment):
  * fleet_list    — one row per stack: name, client_id, current image tag,
                    portainer stack id, health, tenant token env var.
  * fleet_status  — deep status of one stack: live /health + MCP discovery probe.
  * fleet_upgrade — canary upgrade ONE stack: snapshot current compose+tag →
                    swap image tag → Portainer PUT (pullImage) → health gate →
                    on failure auto-rollback to the snapshot → append op ledger.
  * fleet_rollback— re-deploy the previous compose snapshot from the op ledger.

SAFETY:
  * DRY_RUN is ON by default (MCP_FLEET_DRY_RUN=1). In dry-run, upgrade/rollback
    compute the new compose + a tag diff and write a preview, but make NO
    Portainer mutation. Real runs require MCP_FLEET_DRY_RUN=0.
  * These tools mutate live infrastructure → server.py gates them to a verified
    admin principal (same gate as provision_*), NOT mere elevation.
  * Reuses provisioning_engine for Portainer auth, health-wait, and the
    append-only JSONL ledger pattern (latest-record-wins).

Portainer note: provisioning_engine only has create/delete. The UPDATE primitive
(PUT /api/stacks/{id}?endpointId=... with pullImage) lives here — it is the only
new Portainer call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

import provisioning_engine as pe

logger = logging.getLogger("fleet_manager")

FLEET_OPS_FILE = Path(os.environ.get("FLEET_OPS_FILE", "/data/fleet_ops.jsonl"))
DRY_RUN = os.environ.get("MCP_FLEET_DRY_RUN", "1") == "1"
DRY_RUN_PREVIEW_DIR = Path(os.environ.get(
    "MCP_FLEET_PREVIEW_DIR", "/tmp/v3-fleet-preview"))
HEALTH_PATH = os.environ.get("MCP_FLEET_HEALTH_PATH", "/health")
# The MCP server image whose tag we bump on upgrade.
IMAGE_REPO = os.environ.get("MCP_FLEET_IMAGE_REPO", "vladimirovrosen/odoo-rpc-mcp")

# Callbacks wired from server.py (avoid circular import).
_get_proxy_services: Callable[[], dict] | None = None
_discover_one: Callable[[str], list] | None = None


def wire(*, get_proxy_services: Callable[[], dict] | None = None,
         discover_one: Callable[[str], list] | None = None) -> None:
    """Inject server.py dependencies. Call once at startup."""
    global _get_proxy_services, _discover_one
    _get_proxy_services = get_proxy_services
    _discover_one = discover_one


# ─── Op ledger (mirrors provisioning_engine state pattern) ─────────────────

def _ops_replay() -> list[dict]:
    out: list[dict] = []
    if not FLEET_OPS_FILE.is_file():
        return out
    try:
        with open(FLEET_OPS_FILE, "r", encoding="utf-8") as f:
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
    FLEET_OPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FLEET_OPS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(FLEET_OPS_FILE, 0o600)
    except OSError:
        pass


def _last_op(stack_name: str, kind: str | None = None) -> dict | None:
    """Most recent ledger op for a stack (optionally filtered by kind)."""
    for rec in reversed(_ops_replay()):
        if rec.get("stack") == stack_name and (kind is None or rec.get("kind") == kind):
            return rec
    return None


# ─── Portainer helpers ─────────────────────────────────────────────────────

def _portainer_env() -> tuple[str, str, str] | None:
    url = os.environ.get("MCP_PORTAINER_URL", "").rstrip("/")
    key = os.environ.get("MCP_PORTAINER_API_KEY", "")
    eid = os.environ.get("MCP_PORTAINER_ENDPOINT_ID", "2")
    if not url or not key:
        return None
    return url, key, eid


def _portainer_list_stacks() -> dict:
    """GET /api/stacks → {ok, stacks:[{Id,Name,Status,...}]} or {error}."""
    env = _portainer_env()
    if env is None:
        return {"error": "MCP_PORTAINER_URL or MCP_PORTAINER_API_KEY not set"}
    url, key, _ = env
    import httpx
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    try:
        r = httpx.get(f"{url}/api/stacks", headers=headers, timeout=30.0)
        if r.status_code >= 300:
            return {"error": "portainer_list_failed",
                    "status": r.status_code, "body": r.text[:500]}
        return {"ok": True, "stacks": r.json() or []}
    except Exception as e:
        return {"error": "portainer_list_request_failed", "detail": str(e)}


def _portainer_get_stack_file(stack_id: int) -> dict:
    """GET /api/stacks/{id}/file → {ok, compose} or {error}."""
    env = _portainer_env()
    if env is None:
        return {"error": "MCP_PORTAINER_URL or MCP_PORTAINER_API_KEY not set"}
    url, key, _ = env
    import httpx
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    try:
        r = httpx.get(f"{url}/api/stacks/{stack_id}/file",
                      headers=headers, timeout=30.0)
        if r.status_code >= 300:
            return {"error": "portainer_get_file_failed",
                    "status": r.status_code, "body": r.text[:500]}
        body = r.json() or {}
        return {"ok": True, "compose": body.get("StackFileContent", "")}
    except Exception as e:
        return {"error": "portainer_get_file_request_failed", "detail": str(e)}


def portainer_update_stack(stack_id: int, compose: str,
                           env: list | None = None,
                           pull_image: bool = True) -> dict:
    """PUT /api/stacks/{id}?endpointId= — redeploy with new compose (+pull).

    This is the one Portainer primitive missing from provisioning_engine.
    """
    p = _portainer_env()
    if p is None:
        return {"error": "MCP_PORTAINER_URL or MCP_PORTAINER_API_KEY not set"}
    url, key, eid = p
    import httpx
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    payload = {
        "stackFileContent": compose,
        "env": env or [],
        "prune": False,
        "pullImage": bool(pull_image),
    }
    try:
        r = httpx.put(f"{url}/api/stacks/{stack_id}",
                      params={"endpointId": eid}, json=payload,
                      headers=headers, timeout=120.0)
        if r.status_code >= 300:
            return {"error": "portainer_update_failed",
                    "status": r.status_code, "body": r.text[:500]}
        return {"ok": True, "stack_id": stack_id}
    except Exception as e:
        return {"error": "portainer_update_request_failed", "detail": str(e)}


# ─── Inventory helpers ─────────────────────────────────────────────────────

_CLIENT_ID_RE = re.compile(r"mcp-(?:client-)?([0-9a-zA-Z]+)")
_IMAGE_TAG_RE = re.compile(
    re.escape(IMAGE_REPO) + r":([A-Za-z0-9_.\-]+)")


def _client_id_from(name: str, url: str = "") -> str | None:
    """Derive client_id from a stack name (mcp-client-<id>) or hostname."""
    for src in (name or "", url or ""):
        m = _CLIENT_ID_RE.search(src)
        if m:
            return m.group(1)
    return None


def _current_tag(compose: str) -> str | None:
    m = _IMAGE_TAG_RE.search(compose or "")
    return m.group(1) if m else None


def _swap_tag(compose: str, target_tag: str) -> tuple[str, int]:
    """Replace every `IMAGE_REPO:<tag>` with the target tag. Returns (new, count)."""
    new, n = _IMAGE_TAG_RE.subn(f"{IMAGE_REPO}:{target_tag}", compose or "")
    return new, n


def _proxy_stacks() -> dict:
    """name -> proxy entry, from proxy_services (the canonical fleet list)."""
    if _get_proxy_services is None:
        return {}
    try:
        return dict(_get_proxy_services() or {})
    except Exception:
        return {}


# ─── Public fleet ops ──────────────────────────────────────────────────────

def fleet_list() -> dict:
    """Inventory: merge proxy_services (canonical) + Portainer stacks + state."""
    proxy = _proxy_stacks()
    pstacks = _portainer_list_stacks()
    by_name = {}
    if pstacks.get("ok"):
        for s in pstacks["stacks"]:
            by_name[s.get("Name")] = s
    rows = []
    consumed = set()  # Portainer stack names already matched to a proxy entry
    for name, entry in proxy.items():
        url = (entry.get("url") if isinstance(entry, dict) else "") or ""
        cid = _client_id_from(name, url)
        pname = f"mcp-client-{cid}" if cid else None
        pst = (by_name.get(pname) if pname else None) or by_name.get(name) or {}
        if pst:
            consumed.add(pst.get("Name"))
        rows.append({
            "name": name,
            "client_id": cid,
            "url": url,
            "portainer_stack_id": pst.get("Id"),
            "portainer_status": pst.get("Status"),
            "in_portainer": bool(pst),
        })
    # Portainer stacks not present in proxy_services (orphans / main)
    for name, s in by_name.items():
        if name in consumed:
            continue
        rows.append({
            "name": name, "client_id": _client_id_from(name),
            "url": "", "portainer_stack_id": s.get("Id"),
            "portainer_status": s.get("Status"), "in_portainer": True,
            "note": "in Portainer but not in proxy_services",
        })
    return {"count": len(rows), "stacks": rows,
            "portainer_reachable": bool(pstacks.get("ok")),
            "dry_run": DRY_RUN}


def _resolve_stack(name_or_id: str) -> dict:
    """Resolve a stack to {name, stack_id, url, client_id} from inventory."""
    inv = fleet_list()
    for row in inv["stacks"]:
        if row["name"] == name_or_id or str(row.get("client_id")) == str(name_or_id) \
                or str(row.get("portainer_stack_id")) == str(name_or_id):
            return row
    return {}


def fleet_status(name_or_id: str) -> dict:
    """Deep status of one stack: current image tag + /health + MCP probe."""
    row = _resolve_stack(name_or_id)
    if not row:
        return {"error": "unknown_stack", "query": name_or_id}
    out = dict(row)
    sid = row.get("portainer_stack_id")
    if sid is not None:
        f = _portainer_get_stack_file(int(sid))
        if f.get("ok"):
            out["image_tag"] = _current_tag(f["compose"])
    # Live /health (reuse provisioning_engine backoff probe, short timeout).
    if row.get("url"):
        h = pe.wait_for_health(row["url"].rstrip("/") + HEALTH_PATH, timeout=8)
        out["health"] = h
    # Secondary: MCP tool discovery probe (proves it actually serves MCP).
    if _discover_one is not None and row.get("name"):
        try:
            tools = _discover_one(row["name"])
            out["mcp_probe"] = {"healthy": bool(tools), "tool_count": len(tools or [])}
        except Exception as e:
            out["mcp_probe"] = {"healthy": False, "error": str(e)}
    return out


def _write_preview(stack: str, compose: str, tag_from, tag_to) -> str:
    DRY_RUN_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = DRY_RUN_PREVIEW_DIR / f"{stack}.upgrade.yml"
    path.write_text(compose, encoding="utf-8")
    return str(path)


def fleet_upgrade(name_or_id: str, target_tag: str,
                  dry_run: bool | None = None) -> dict:
    """Canary upgrade ONE stack to `target_tag` with auto-rollback on failure."""
    if not target_tag:
        return {"error": "target_tag is required"}
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    row = _resolve_stack(name_or_id)
    if not row:
        return {"error": "unknown_stack", "query": name_or_id}
    sid = row.get("portainer_stack_id")
    if sid is None:
        return {"error": "no_portainer_stack_id", "stack": row["name"]}
    sid = int(sid)

    snap = _portainer_get_stack_file(sid)
    if not snap.get("ok"):
        return {"error": "snapshot_failed", "detail": snap}
    old_compose = snap["compose"]
    old_tag = _current_tag(old_compose)
    new_compose, n = _swap_tag(old_compose, target_tag)
    if n == 0:
        return {"error": "image_repo_not_found_in_compose",
                "image_repo": IMAGE_REPO, "stack": row["name"]}
    if old_tag == target_tag:
        return {"ok": True, "stack": row["name"], "no_change": True,
                "tag": target_tag}

    base = {"stack": row["name"], "kind": "upgrade", "client_id": row.get("client_id"),
            "stack_id": sid, "from_tag": old_tag, "to_tag": target_tag,
            "ts": int(time.time())}

    if is_dry:
        preview = _write_preview(row["name"], new_compose, old_tag, target_tag)
        rec = {**base, "dry_run": True, "preview": preview,
               "snapshot_compose": old_compose}
        _ops_append(rec)
        return {"ok": True, "dry_run": True, "stack": row["name"],
                "from_tag": old_tag, "to_tag": target_tag, "preview": preview,
                "hint": "Set MCP_FLEET_DRY_RUN=0 (or dry_run=false) to apply."}

    # Real upgrade.
    upd = portainer_update_stack(sid, new_compose, pull_image=True)
    if not upd.get("ok"):
        _ops_append({**base, "result": "update_failed", "detail": upd,
                     "snapshot_compose": old_compose})
        return {"error": "update_failed", "detail": upd, "stack": row["name"]}

    health = {"healthy": None}
    if row.get("url"):
        health = pe.wait_for_health(row["url"].rstrip("/") + HEALTH_PATH)
    if health.get("healthy") is False:
        # Auto-rollback to the snapshot.
        rb = portainer_update_stack(sid, old_compose, pull_image=False)
        rb_health = pe.wait_for_health(row["url"].rstrip("/") + HEALTH_PATH) \
            if row.get("url") else {"healthy": None}
        _ops_append({**base, "result": "rolled_back", "health": health,
                     "rollback": rb, "rollback_health": rb_health,
                     "snapshot_compose": old_compose})
        return {"error": "upgrade_unhealthy_rolled_back", "stack": row["name"],
                "from_tag": old_tag, "attempted_tag": target_tag,
                "health": health, "rollback": rb, "rollback_health": rb_health}

    _ops_append({**base, "result": "ok", "health": health,
                 "snapshot_compose": old_compose})
    return {"ok": True, "stack": row["name"], "from_tag": old_tag,
            "to_tag": target_tag, "health": health}


def fleet_rollback(name_or_id: str, dry_run: bool | None = None) -> dict:
    """Re-deploy the previous compose snapshot from the last upgrade op."""
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    row = _resolve_stack(name_or_id)
    if not row:
        return {"error": "unknown_stack", "query": name_or_id}
    last = _last_op(row["name"], kind="upgrade")
    if not last or not last.get("snapshot_compose"):
        return {"error": "no_snapshot_to_roll_back_to", "stack": row["name"]}
    sid = row.get("portainer_stack_id")
    if sid is None:
        return {"error": "no_portainer_stack_id", "stack": row["name"]}
    sid = int(sid)
    snap_compose = last["snapshot_compose"]
    snap_tag = _current_tag(snap_compose)
    base = {"stack": row["name"], "kind": "rollback", "stack_id": sid,
            "to_tag": snap_tag, "ts": int(time.time())}
    if is_dry:
        preview = _write_preview(row["name"], snap_compose, None, snap_tag)
        _ops_append({**base, "dry_run": True, "preview": preview})
        return {"ok": True, "dry_run": True, "stack": row["name"],
                "to_tag": snap_tag, "preview": preview}
    upd = portainer_update_stack(sid, snap_compose, pull_image=False)
    health = pe.wait_for_health(row["url"].rstrip("/") + HEALTH_PATH) \
        if row.get("url") else {"healthy": None}
    _ops_append({**base, "result": "ok" if upd.get("ok") else "failed",
                 "detail": upd, "health": health})
    return {"ok": upd.get("ok", False), "stack": row["name"],
            "to_tag": snap_tag, "health": health, "detail": upd}


# ─── Control-plane Tool definitions (mirror api_key_manager) ───────────────

def get_admin_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(
            name="fleet_list",
            description=("ADMIN: inventory of all deployed MCP stacks — name, "
                         "client_id, Portainer stack id, status. Read-only."),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="fleet_status",
            description=("ADMIN: deep status of one stack — current image tag, "
                         "live /health, and an MCP tool-discovery probe. Read-only."),
            inputSchema={
                "type": "object",
                "properties": {"stack": {"type": "string",
                               "description": "stack name, client_id, or stack id"}},
                "required": ["stack"],
            },
        ),
        Tool(
            name="fleet_upgrade",
            description=("ADMIN: canary-upgrade ONE stack to a target image tag. "
                         "Snapshots current compose, swaps the tag, redeploys with "
                         "image pull, health-gates, and AUTO-ROLLS-BACK on failure. "
                         "DRY-RUN by default (writes a preview, no mutation) unless "
                         "MCP_FLEET_DRY_RUN=0 or dry_run=false."),
            inputSchema={
                "type": "object",
                "properties": {
                    "stack": {"type": "string"},
                    "target_tag": {"type": "string",
                                   "description": "e.g. '3.0.0-alpha.10'"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["stack", "target_tag"],
            },
        ),
        Tool(
            name="fleet_rollback",
            description=("ADMIN: redeploy a stack's previous compose snapshot from "
                         "the last upgrade op. DRY-RUN by default."),
            inputSchema={
                "type": "object",
                "properties": {"stack": {"type": "string"},
                               "dry_run": {"type": "boolean"}},
                "required": ["stack"],
            },
        ),
    ]


ADMIN_TOOL_NAMES = {"fleet_list", "fleet_status", "fleet_upgrade", "fleet_rollback"}


def handle(name: str, arguments: dict | None) -> dict:
    """Dispatcher for fleet_* admin tools."""
    arguments = arguments or {}
    if name == "fleet_list":
        return fleet_list()
    if name == "fleet_status":
        return fleet_status(arguments.get("stack", ""))
    if name == "fleet_upgrade":
        return fleet_upgrade(arguments.get("stack", ""),
                             arguments.get("target_tag", ""),
                             arguments.get("dry_run"))
    if name == "fleet_rollback":
        return fleet_rollback(arguments.get("stack", ""),
                              arguments.get("dry_run"))
    return {"error": f"unknown fleet tool: {name}"}
