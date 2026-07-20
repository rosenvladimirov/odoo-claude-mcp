"""
v3 Session Handoff (A / B.8) — controlled transfer of a working session to
another Claude/principal.

Decisions (Rosen, 2026-06-10):
  * connection handoff = shared connection-registry REFERENCE with an ACL grant
    (NO plaintext credential copy to the new owner).
  * mode = 'transfer' | 'share'; default 'transfer' → the offering session is
    revoked once the target accepts.
  * v3-only (v2 is frozen).

Principles (from the 2026-06-10 security audit):
  * NO credential transfer — the server re-materializes only what the target is
    entitled to (or an explicit ACL grant for a connection).
  * Two-phase consent: offer → accept. Nobody receives a session without
    accepting it themselves.
  * Fail-closed + JSONL audit (like elevation).
  * Target is established by PRINCIPAL only — a handoff for principal X can be
    accepted only by a session whose bound principal is X. (No name claiming.)
  * Non-transferable by definition: elevation, API keys, OAuth tokens, Telethon
    sessions. Those are never part of `include`.

State is in-memory keyed by handoff_id with TTL (ephemeral; cleared on restart).
Materialization is performed via callbacks wired from server.py; each is
best-effort and recorded in the result, never silently assumed.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger("session_handoff")

HANDOFF_AUDIT = Path(os.environ.get(
    "HANDOFF_AUDIT_FILE", "/data/handoff_audit.log"))
DEFAULT_TTL = int(os.environ.get("MCP_HANDOFF_TTL", "600"))
MAX_TTL = int(os.environ.get("MCP_HANDOFF_MAX_TTL", "3600"))

VALID_INCLUDE = {"connection", "tenant", "telegram_subs", "memory", "note"}

# offer records: handoff_id -> dict
_offers: dict[str, dict] = {}

# wired callbacks (all optional; materialization is best-effort)
_set_tenant: Callable | None = None          # (session_key, tenant_name) -> None
_grant_connection: Callable | None = None    # (principal, alias) -> dict
_share_memory: Callable | None = None        # (filename) -> dict
_revoke_session: Callable | None = None       # (session_key) -> dict


def wire(*, set_tenant: Callable | None = None,
         grant_connection: Callable | None = None,
         share_memory: Callable | None = None,
         revoke_session: Callable | None = None) -> None:
    global _set_tenant, _grant_connection, _share_memory, _revoke_session
    _set_tenant = set_tenant
    _grant_connection = grant_connection
    _share_memory = share_memory
    _revoke_session = revoke_session


def _audit(action: str, **extra) -> None:
    payload = {"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
               "action": action, **extra}
    try:
        HANDOFF_AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with open(HANDOFF_AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("handoff audit write failed: %s", e)


def _now() -> float:
    return time.time()


def _expired(rec: dict) -> bool:
    return rec.get("expires_at", 0) <= _now()


def _purge() -> None:
    for hid in [h for h, r in _offers.items() if _expired(r)]:
        _offers.pop(hid, None)


# ─── offer ─────────────────────────────────────────────────────────────────

def offer(from_session_key: str | None, from_principal: str | None,
          to_principal: str, include: list | None = None,
          payload: dict | None = None, ttl: int | None = None,
          mode: str = "transfer", note: str = "") -> dict:
    """Create a handoff offer from the current session to `to_principal`."""
    if not from_session_key or not from_principal:
        return {"error": "no_identity",
                "hint": "handoff offer requires an authenticated session"}
    if not to_principal:
        return {"error": "to_principal is required"}
    if to_principal == from_principal:
        return {"error": "cannot_handoff_to_self"}
    mode = (mode or "transfer").strip().lower()
    if mode not in ("transfer", "share"):
        return {"error": "invalid_mode", "valid": ["transfer", "share"]}
    include = [i for i in (include or ["note"]) if i in VALID_INCLUDE]
    if not include:
        include = ["note"]
    ttl = DEFAULT_TTL if ttl is None else max(1, min(int(ttl), MAX_TTL))
    _purge()
    hid = "ho_" + _secrets.token_hex(8)
    rec = {
        "handoff_id": hid,
        "from_session_key": from_session_key,
        "from_principal": from_principal,
        "to_principal": to_principal,
        "include": include,
        "payload": payload or {},     # {tenant, connection, telegram_subs, memory, note}
        "mode": mode,
        "note": note,
        "created_at": _now(),
        "expires_at": _now() + ttl,
        "status": "pending",
    }
    _offers[hid] = rec
    _audit("OFFERED", handoff_id=hid, from_principal=from_principal,
           to_principal=to_principal, include=include, mode=mode)
    return {"handoff_id": hid, "to_principal": to_principal, "include": include,
            "mode": mode, "expires_in": ttl, "status": "pending"}


# ─── accept ────────────────────────────────────────────────────────────────

def accept(handoff_id: str, accepting_session_key: str | None,
           accepting_principal: str | None) -> dict:
    """Accept an offer addressed to the accepting principal."""
    _purge()
    rec = _offers.get(handoff_id)
    if not rec:
        return {"error": "unknown_or_expired_handoff", "handoff_id": handoff_id}
    if rec["status"] != "pending":
        return {"error": "handoff_not_pending", "status": rec["status"]}
    if not accepting_principal:
        return {"error": "no_identity"}
    if accepting_principal != rec["to_principal"]:
        # Do not leak existence to the wrong principal beyond a generic refusal.
        _audit("ACCEPT_DENIED", handoff_id=handoff_id,
               by=accepting_principal, expected=rec["to_principal"])
        return {"error": "not_addressed_to_you"}

    materialized: dict = {}
    payload = rec.get("payload", {})
    # connection — ACL grant of a registry reference (no credential copy).
    if "connection" in rec["include"]:
        alias = payload.get("connection")
        if alias and _grant_connection is not None:
            try:
                materialized["connection"] = _grant_connection(accepting_principal, alias)
            except Exception as e:
                materialized["connection"] = {"error": str(e)}
        else:
            materialized["connection"] = {"alias": alias, "granted": False,
                                          "note": "no grant callback wired"}
    # tenant — set the accepting session's active tenant.
    if "tenant" in rec["include"]:
        tenant = payload.get("tenant")
        if tenant and _set_tenant is not None and accepting_session_key:
            try:
                _set_tenant(accepting_session_key, tenant)
                materialized["tenant"] = {"active": tenant}
            except Exception as e:
                materialized["tenant"] = {"error": str(e)}
        else:
            materialized["tenant"] = {"tenant": tenant, "applied": False}
    # telegram_subs — allow-list entries (recorded; applied by wired cb if any).
    if "telegram_subs" in rec["include"]:
        materialized["telegram_subs"] = payload.get("telegram_subs", [])
    # memory — share named files so the target can pull them.
    if "memory" in rec["include"]:
        files = payload.get("memory", [])
        shared = []
        for fn in files:
            if _share_memory is not None:
                try:
                    shared.append({fn: _share_memory(fn)})
                except Exception as e:
                    shared.append({fn: {"error": str(e)}})
            else:
                shared.append({fn: {"shared": False}})
        materialized["memory"] = shared
    # note — always carried.
    materialized["note"] = rec.get("note") or payload.get("note", "")

    # transfer mode → revoke the offering session.
    revoked = None
    if rec["mode"] == "transfer" and _revoke_session is not None:
        try:
            revoked = _revoke_session(rec["from_session_key"])
        except Exception as e:
            revoked = {"error": str(e)}

    rec["status"] = "accepted"
    rec["accepted_at"] = _now()
    rec["accepted_by"] = accepting_principal
    _audit("ACCEPTED", handoff_id=handoff_id, by=accepting_principal,
           mode=rec["mode"], revoked=bool(revoked))
    return {"ok": True, "handoff_id": handoff_id, "mode": rec["mode"],
            "from_principal": rec["from_principal"],
            "materialized": materialized,
            "offering_session_revoked": bool(revoked) if rec["mode"] == "transfer" else False}


# ─── status / cancel ───────────────────────────────────────────────────────

def status(handoff_id: str | None = None,
           principal: str | None = None) -> dict:
    _purge()
    if handoff_id:
        rec = _offers.get(handoff_id)
        if not rec:
            return {"error": "unknown_or_expired_handoff"}
        # Only the two parties may see it.
        if principal and principal not in (rec["from_principal"], rec["to_principal"]):
            return {"error": "not_a_party"}
        return {"handoff_id": handoff_id, "status": rec["status"],
                "from_principal": rec["from_principal"],
                "to_principal": rec["to_principal"],
                "include": rec["include"], "mode": rec["mode"],
                "expires_in": int(rec["expires_at"] - _now())}
    # list pending offers addressed to / from this principal
    out = []
    for r in _offers.values():
        if principal and principal in (r["from_principal"], r["to_principal"]):
            out.append({"handoff_id": r["handoff_id"], "status": r["status"],
                        "from_principal": r["from_principal"],
                        "to_principal": r["to_principal"], "mode": r["mode"],
                        "expires_in": int(r["expires_at"] - _now())})
    return {"count": len(out), "handoffs": out}


def cancel(handoff_id: str, principal: str | None = None) -> dict:
    rec = _offers.get(handoff_id)
    if not rec:
        return {"error": "unknown_or_expired_handoff"}
    if principal and principal != rec["from_principal"]:
        return {"error": "only_offerer_can_cancel"}
    _offers.pop(handoff_id, None)
    _audit("CANCELLED", handoff_id=handoff_id, by=principal)
    return {"ok": True, "handoff_id": handoff_id, "status": "cancelled"}


# ─── registration (NOT admin-only — any authenticated principal) ───────────

CONTROL_TOOL_NAMES = {"session_handoff_offer", "session_handoff_accept",
                      "session_handoff_status", "session_handoff_cancel"}


def get_control_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="session_handoff_offer",
             description=("Offer your working session to another principal "
                          "(two-phase consent). include: connection (ACL ref, no "
                          "credential copy), tenant, telegram_subs, memory, note. "
                          "mode=transfer (default; your session is revoked on "
                          "accept) or share. NEVER transfers elevation/keys/tokens."),
             inputSchema={"type": "object", "properties": {
                 "to_principal": {"type": "string"},
                 "include": {"type": "array", "items": {"type": "string",
                             "enum": sorted(VALID_INCLUDE)}},
                 "payload": {"type": "object",
                             "description": "{tenant, connection, telegram_subs, memory, note}"},
                 "mode": {"type": "string", "enum": ["transfer", "share"]},
                 "ttl": {"type": "integer"},
                 "note": {"type": "string"}},
                 "required": ["to_principal"]}),
        Tool(name="session_handoff_accept",
             description=("Accept a handoff addressed to you. Materializes the "
                          "included items into YOUR session; if mode=transfer the "
                          "offering session is revoked."),
             inputSchema={"type": "object",
                          "properties": {"handoff_id": {"type": "string"}},
                          "required": ["handoff_id"]}),
        Tool(name="session_handoff_status",
             description=("Show a handoff (by id) or list pending handoffs "
                          "involving you. Read-only."),
             inputSchema={"type": "object",
                          "properties": {"handoff_id": {"type": "string"}}}),
        Tool(name="session_handoff_cancel",
             description="Cancel an offer you created (offerer only).",
             inputSchema={"type": "object",
                          "properties": {"handoff_id": {"type": "string"}},
                          "required": ["handoff_id"]}),
    ]


def handle(name: str, arguments: dict | None,
           session_key: str | None = None, principal: str | None = None) -> dict:
    arguments = arguments or {}
    if name == "session_handoff_offer":
        return offer(session_key, principal,
                     arguments.get("to_principal", ""),
                     arguments.get("include"),
                     arguments.get("payload"),
                     arguments.get("ttl"),
                     arguments.get("mode", "transfer"),
                     arguments.get("note", ""))
    if name == "session_handoff_accept":
        return accept(arguments.get("handoff_id", ""), session_key, principal)
    if name == "session_handoff_status":
        return status(arguments.get("handoff_id"), principal)
    if name == "session_handoff_cancel":
        return cancel(arguments.get("handoff_id", ""), principal)
    return {"error": f"unknown handoff tool: {name}"}
