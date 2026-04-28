"""
v3 elevation — temporary admin window for USER role.

Single-process global state (singleton) with auto-expire and audit log.
Two control tools (mcp_elevate / mcp_drop_elevation) wired into server.py.

Per-session scope is a follow-up; current scope is per-server. For
multi-developer deployments document this explicitly.

Audit format (jsonl, /data/elevation_audit.log):
  {"ts":"...","action":"GRANTED","ttl":300,"reason":"...","by":"<host/user if available>"}
  {"ts":"...","action":"DROPPED","ts_granted":"..."}
  {"ts":"...","action":"EXPIRED","ts_granted":"...","ttl":300}
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
_state = {
    "granted_at": 0.0,
    "expires_at": 0.0,
    "ttl": 0,
    "reason": "",
}


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


def is_elevated() -> bool:
    with _lock:
        if _state["expires_at"] <= 0:
            return False
        if time.time() >= _state["expires_at"]:
            # Auto-expire.
            _audit("EXPIRED",
                   ts_granted=_state.get("granted_at"),
                   ttl=_state.get("ttl"))
            _state["granted_at"] = 0.0
            _state["expires_at"] = 0.0
            _state["ttl"] = 0
            _state["reason"] = ""
            return False
        return True


def grant(reason: str, ttl: int | None = None) -> dict:
    if ttl is None:
        ttl = DEFAULT_TTL
    ttl = max(1, min(int(ttl), MAX_TTL))
    reason = (reason or "").strip()
    if not reason:
        return {"error": "reason is required"}
    now = time.time()
    with _lock:
        _state["granted_at"] = now
        _state["expires_at"] = now + ttl
        _state["ttl"] = ttl
        _state["reason"] = reason
    _audit("GRANTED", ttl=ttl, reason=reason)
    logger.info("elevation: granted ttl=%ds reason=%r", ttl, reason)
    return {
        "elevated": True,
        "ttl": ttl,
        "expires_in": ttl,
        "reason": reason,
    }


def drop() -> dict:
    with _lock:
        if _state["expires_at"] <= 0:
            return {"elevated": False, "msg": "not currently elevated"}
        ts_granted = _state["granted_at"]
        _state["granted_at"] = 0.0
        _state["expires_at"] = 0.0
        _state["ttl"] = 0
        _state["reason"] = ""
    _audit("DROPPED", ts_granted=ts_granted)
    logger.info("elevation: dropped")
    return {"elevated": False}


def status() -> dict:
    with _lock:
        if _state["expires_at"] <= 0 or time.time() >= _state["expires_at"]:
            is_elevated()  # trigger expiry
            return {"elevated": False}
        return {
            "elevated": True,
            "ttl": _state["ttl"],
            "expires_in": int(_state["expires_at"] - time.time()),
            "reason": _state["reason"],
        }


def get_control_tools():
    from mcp.types import Tool
    return [
        Tool(
            name="mcp_elevate",
            description=(
                "Grant the current MCP server temporary admin rights. While "
                "elevated, USER-role gates (destructive tools, protected "
                "unlink, protected SQL writes) are bypassed. Auto-expires "
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


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "mcp_elevate":
        return grant(arguments.get("reason", ""), arguments.get("ttl"))
    if name == "mcp_drop_elevation":
        return drop()
    if name == "mcp_elevation_status":
        return status()
    return {"error": f"unknown elevation tool: {name}"}
