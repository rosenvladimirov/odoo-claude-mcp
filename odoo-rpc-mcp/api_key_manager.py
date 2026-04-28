"""
v3 provisioning API key manager.

File-based, append-only audit trail (/data/api_keys.jsonl). Hashes with
argon2 (already in requirements). Provides issue/verify/revoke/list ops.

Phase 1: admin-issued keys via MCP tool provision_issue_api_key(email).
Phase 2: signup endpoint will call into the same engine.

Storage format (one JSON object per line):
  {"key_id":"k_<8hex>","key_hash":"$argon2id...","email":"x@y","created_ts":1714291200,"status":"active"}
  {"key_id":"k_<8hex>","status":"revoked","revoked_ts":1714291300,"reason":"..."}

Records are append-only — revocation is a NEW line referencing the
existing key_id. The "active" map is rebuilt on demand by replaying.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger("api_key_manager")

API_KEYS_FILE = Path(os.environ.get("API_KEYS_FILE", "/data/api_keys.jsonl"))


def _hasher():
    from argon2 import PasswordHasher
    return PasswordHasher()


def _replay() -> dict[str, dict]:
    """Rebuild active key map from the audit log."""
    keys: dict[str, dict] = {}
    if not API_KEYS_FILE.is_file():
        return keys
    try:
        with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key_id = rec.get("key_id")
                if not key_id:
                    continue
                if rec.get("status") == "revoked":
                    keys.pop(key_id, None)
                else:
                    keys[key_id] = rec
    except OSError as e:
        logger.warning("api_keys read failed: %s", e)
    return keys


def _append(record: dict) -> None:
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(API_KEYS_FILE, 0o600)
    except OSError:
        pass


def issue(email: str) -> dict:
    """Generate a new API key. Returns plaintext ONCE — store immediately."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"error": "valid email required"}

    plaintext = "mcpv3_" + secrets.token_urlsafe(32)  # 43-char base64
    key_id = "k_" + secrets.token_hex(8)
    key_hash = _hasher().hash(plaintext)

    record = {
        "key_id": key_id,
        "key_hash": key_hash,
        "email": email,
        "created_ts": int(time.time()),
        "status": "active",
    }
    _append(record)
    logger.info("api_key issued: id=%s email=%s", key_id, email)
    return {
        "key_id": key_id,
        "api_key": plaintext,           # SHOWN ONCE
        "email": email,
        "warning": "Store this key NOW. It will never be shown again.",
    }


def verify(provided_key: str) -> dict | None:
    """Returns the active key record if valid, else None."""
    if not provided_key or not provided_key.startswith("mcpv3_"):
        return None
    keys = _replay()
    ph = _hasher()
    for rec in keys.values():
        try:
            ph.verify(rec["key_hash"], provided_key)
            return rec
        except Exception:
            continue
    return None


def revoke(key_id: str, reason: str = "") -> dict:
    keys = _replay()
    if key_id not in keys:
        return {"error": f"unknown or already revoked: {key_id}"}
    record = {
        "key_id": key_id,
        "status": "revoked",
        "revoked_ts": int(time.time()),
        "reason": reason or "manual",
    }
    _append(record)
    logger.info("api_key revoked: id=%s reason=%r", key_id, reason)
    return {"revoked": True, "key_id": key_id}


def list_active() -> list[dict]:
    """Return all active keys (without hashes — UI/audit only)."""
    keys = _replay()
    return [
        {
            "key_id": rec["key_id"],
            "email": rec.get("email", ""),
            "created_ts": rec.get("created_ts", 0),
        }
        for rec in keys.values()
    ]


# ─── MCP tool wrappers (registered in server.py admin path) ─────────────

def get_admin_tools():
    """Returns Tool defs for admin-only MCP wrappers (issue/revoke/list)."""
    from mcp.types import Tool
    return [
        Tool(
            name="provision_issue_api_key",
            description=(
                "ADMIN ONLY: Issue a new API key for a client to call "
                "POST /provision. Returns the plaintext key ONCE — store "
                "it immediately. Logged to /data/api_keys.jsonl."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Client email (audit)"},
                },
                "required": ["email"],
            },
        ),
        Tool(
            name="provision_revoke_api_key",
            description="ADMIN ONLY: Revoke an issued API key by key_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key_id": {"type": "string"},
                    "reason": {"type": "string", "default": ""},
                },
                "required": ["key_id"],
            },
        ),
        Tool(
            name="provision_list_api_keys",
            description="ADMIN ONLY: List active provisioning API keys (without hashes).",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


ADMIN_TOOL_NAMES = {
    "provision_issue_api_key",
    "provision_revoke_api_key",
    "provision_list_api_keys",
}


def handle(name: str, arguments: dict | None) -> dict:
    """Dispatcher for provision_* admin tools."""
    arguments = arguments or {}
    if name == "provision_issue_api_key":
        return issue(arguments.get("email", ""))
    if name == "provision_revoke_api_key":
        return revoke(arguments.get("key_id", ""), arguments.get("reason", ""))
    if name == "provision_list_api_keys":
        return {"keys": list_active(), "count": len(list_active())}
    return {"error": f"unknown api_key tool: {name}"}
