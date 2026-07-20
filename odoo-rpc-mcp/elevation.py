"""
v3 elevation — temporary admin window for USER role.

PER-SESSION scope: each elevation record is keyed by session_key, so an
mcp_elevate() in one session does NOT elevate any other concurrent session.
Records are ephemeral (in-memory) and cleared on restart — by design.
Two control tools (mcp_elevate / mcp_drop_elevation) wired into server.py.

Audit format (jsonl, /data/elevation_audit.log):
  {"ts":"...","action":"GRANTED","session":"mcp:..","principal":"..","ttl":300,"reason":"..."}
  {"ts":"...","action":"DROPPED","session":"mcp:..","principal":"..","ts_granted":"..."}
  {"ts":"...","action":"EXPIRED","session":"mcp:..","ts_granted":"..","ttl":300}
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("elevation")

ELEVATION_AUDIT = Path(os.environ.get(
    "ELEVATION_AUDIT_FILE", "/data/elevation_audit.log"))
DEFAULT_TTL = int(os.environ.get("MCP_ELEVATION_TTL", "300"))
MAX_TTL = int(os.environ.get("MCP_ELEVATION_MAX_TTL", "3600"))

_lock = threading.RLock()
# Per-session elevation records, keyed by session_key ('mcp:<sid>'|'sse:<sid>').
_sessions: dict[str, dict] = {}


def _audit(action: str, **extra) -> None:
    payload = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "action": action,
        **extra,
    }
    try:
        ELEVATION_AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with open(ELEVATION_AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("elevation audit write failed: %s", e)


def is_elevated(key: str | None) -> bool:
    """True if THIS session currently holds a live elevation window.

    Fail-closed: a missing session key (e.g. list_tools without a resolvable
    session) is treated as not-elevated.
    """
    if not key:
        return False
    with _lock:
        st = _sessions.get(key)
        if not st or st["expires_at"] <= 0:
            return False
        if time.time() >= st["expires_at"]:
            _audit("EXPIRED", session=key, principal=st.get("principal"),
                   ts_granted=st.get("granted_at"), ttl=st.get("ttl"))
            _sessions.pop(key, None)
            return False
        return True


def grant(key: str | None, reason: str, ttl: int | None = None,
          principal: str | None = None) -> dict:
    if not key:
        return {"error": "no MCP session context; cannot elevate"}
    if ttl is None:
        ttl = DEFAULT_TTL
    ttl = max(1, min(int(ttl), MAX_TTL))
    reason = (reason or "").strip()
    if not reason:
        return {"error": "reason is required"}
    now = time.time()
    with _lock:
        _sessions[key] = {
            "granted_at": now, "expires_at": now + ttl,
            "ttl": ttl, "reason": reason, "principal": principal,
        }
    _audit("GRANTED", session=key, principal=principal, ttl=ttl, reason=reason)
    logger.info("elevation: granted session=%s principal=%s ttl=%ds reason=%r",
                key, principal, ttl, reason)
    return {
        "elevated": True,
        "ttl": ttl,
        "expires_in": ttl,
        "reason": reason,
    }


def drop(key: str | None) -> dict:
    if not key:
        return {"elevated": False, "msg": "no MCP session context"}
    with _lock:
        st = _sessions.get(key)
        if not st or st["expires_at"] <= 0:
            return {"elevated": False, "msg": "not currently elevated"}
        ts_granted = st["granted_at"]
        principal = st.get("principal")
        _sessions.pop(key, None)
    _audit("DROPPED", session=key, principal=principal, ts_granted=ts_granted)
    logger.info("elevation: dropped session=%s", key)
    return {"elevated": False}


def status(key: str | None) -> dict:
    if not is_elevated(key):  # also triggers lazy expiry/cleanup
        return {"elevated": False}
    with _lock:
        st = _sessions[key]
        return {
            "elevated": True,
            "ttl": st["ttl"],
            "expires_in": int(st["expires_at"] - time.time()),
            "reason": st["reason"],
        }


def get_control_tools():
    from mcp.types import Tool
    return [
        Tool(
            name="mcp_elevate",
            description=(
                "Grant YOUR CURRENT SESSION temporary admin rights (does not "
                "affect other sessions). While elevated, USER-role gates "
                "(destructive tools, protected unlink, protected SQL writes) "
                "are bypassed. Auto-expires "
                f"after `ttl` seconds (default {DEFAULT_TTL}, max {MAX_TTL}). "
                "Logged to /data/elevation_audit.log. `reason` is required "
                "for audit traceability."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why elevation is needed (audit)"},
                    "ttl": {
                        "type": "integer",
                        "description": f"Seconds (default {DEFAULT_TTL}, max {MAX_TTL})",
                        "default": DEFAULT_TTL,
                    },
                },
                "required": ["reason"],
            },
        ),
        Tool(
            name="mcp_drop_elevation",
            description="End the current elevation window early. Logs to audit.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="mcp_elevation_status",
            description="Return whether the server is currently elevated, and remaining TTL.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


CONTROL_TOOL_NAMES = {"mcp_elevate", "mcp_drop_elevation", "mcp_elevation_status"}


def handle(name: str, arguments: dict | None,
           key: str | None = None, principal: str | None = None) -> dict:
    arguments = arguments or {}
    if name == "mcp_elevate":
        return grant(key, arguments.get("reason", ""), arguments.get("ttl"),
                     principal=principal)
    if name == "mcp_drop_elevation":
        return drop(key)
    if name == "mcp_elevation_status":
        return status(key)
    return {"error": f"unknown elevation tool: {name}"}
