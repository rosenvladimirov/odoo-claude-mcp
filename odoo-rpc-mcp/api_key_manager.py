"""
v3 provisioning API key manager — RBAC edition (3.0.0-alpha.2).

Schema (one JSON object per line, append-only audit trail):
  {
    "key_id": "k_<16hex>",
    "key_hash": "<HMAC-SHA256 hex>",          // pepper-keyed; not argon2
    "email": "x@y",
    "created_ts": 1714291200,
    "status": "active",
    "role": "admin" | "tenant",
    "scope": ["client_id_1", ...] | ["*"],   // ["*"] only for admin
    "capabilities": ["provision", "destroy", "read", "issue_keys"]
  }
  {"key_id": "k_<...>", "status": "revoked", "revoked_ts": ..., "reason": "..."}

Plaintext key format: ``mcpv3_<key_id_hex>_<random_token>`` — the prefix
permits O(1) lookup on verify (no scan over all records). The random
suffix is the actual entropy (43 chars base64 = 256 bits).

Verification = HMAC-SHA256(plaintext, MCP_KEY_PEPPER). Pepper is read
from env; without it, verification fails closed (every key invalid).

Legacy keys (argon2 hashes, no role/scope) are rejected on verify and
must be revoked + re-issued via the migration script. This is by design:
auto-promoting silent legacy keys to admin would be a critical security
regression (any leaked old key = root takeover).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger("api_key_manager")

API_KEYS_FILE = Path(os.environ.get("API_KEYS_FILE", "/data/api_keys.jsonl"))
PEPPER_ENV = "MCP_KEY_PEPPER"

ROLE_ADMIN = "admin"
ROLE_TENANT = "tenant"
VALID_ROLES = (ROLE_ADMIN, ROLE_TENANT)

# Capabilities (action verbs). Admin gets all; tenant defaults to destroy-only.
CAP_PROVISION = "provision"
CAP_DESTROY = "destroy"
CAP_READ = "read"
CAP_ISSUE_KEYS = "issue_keys"
ALL_CAPABILITIES = (CAP_PROVISION, CAP_DESTROY, CAP_READ, CAP_ISSUE_KEYS)
ADMIN_DEFAULT_CAPS = list(ALL_CAPABILITIES)
TENANT_DEFAULT_CAPS = [CAP_DESTROY]

KEY_PREFIX = "mcpv3_"


def _pepper() -> bytes:
    p = os.environ.get(PEPPER_ENV, "")
    if not p or len(p) < 32:
        return b""  # fail closed
    return p.encode("utf-8")


def _hmac_hex(plaintext: str) -> str:
    p = _pepper()
    if not p:
        return ""
    return hmac.new(p, plaintext.encode("utf-8"), hashlib.sha256).hexdigest()


def _parse_plaintext(provided: str) -> tuple[str, str] | None:
    """Returns (key_id, full_plaintext) if format matches, else None."""
    if not provided or not provided.startswith(KEY_PREFIX):
        return None
    rest = provided[len(KEY_PREFIX):]
    parts = rest.split("_", 1)
    if len(parts) != 2:
        return None
    key_id_hex, _secret = parts
    if not key_id_hex.startswith("k_"):
        # New format: key_id is just the hex (no "k_" prefix in plaintext)
        # but the stored key_id has the "k_" prefix — normalize.
        if all(c in "0123456789abcdef" for c in key_id_hex):
            return f"k_{key_id_hex}", provided
        return None
    return key_id_hex, provided


def _replay() -> dict[str, dict]:
    """Rebuild active key map from the audit log (latest record wins)."""
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


def _validate_role_scope_caps(role: str, scope: list[str], capabilities: list[str]) -> str | None:
    if role not in VALID_ROLES:
        return f"invalid role: {role!r}; expected one of {VALID_ROLES}"
    if not isinstance(scope, list) or not scope:
        return "scope must be a non-empty list"
    if role == ROLE_ADMIN:
        if scope != ["*"]:
            return 'admin role requires scope=["*"]'
    else:
        if "*" in scope:
            return "tenant role cannot use wildcard scope"
        if any(not s or not isinstance(s, str) for s in scope):
            return "tenant scope entries must be non-empty strings"
    if not isinstance(capabilities, list) or not capabilities:
        return "capabilities must be a non-empty list"
    bad = [c for c in capabilities if c not in ALL_CAPABILITIES]
    if bad:
        return f"unknown capabilities: {bad}; allowed {list(ALL_CAPABILITIES)}"
    if role == ROLE_TENANT and CAP_PROVISION in capabilities:
        return "tenant role cannot have 'provision' capability"
    if role == ROLE_TENANT and CAP_ISSUE_KEYS in capabilities:
        return "tenant role cannot have 'issue_keys' capability"
    return None


def issue(
    email: str,
    role: str = ROLE_ADMIN,
    scope: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> dict:
    """Generate a new API key. Returns plaintext ONCE — store immediately.

    Args:
      email: audit identifier (any RFC-loose email).
      role: "admin" or "tenant".
      scope: ["*"] for admin; non-empty list of client_id strings for tenant.
      capabilities: subset of ALL_CAPABILITIES. Defaults: admin=all, tenant=[destroy].
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"error": "valid email required"}

    if not _pepper():
        return {"error": f"{PEPPER_ENV} not set or too short (need >=32 chars)"}

    role = (role or ROLE_ADMIN).strip().lower()
    if scope is None:
        scope = ["*"] if role == ROLE_ADMIN else []
    if capabilities is None:
        capabilities = list(ADMIN_DEFAULT_CAPS) if role == ROLE_ADMIN else list(TENANT_DEFAULT_CAPS)

    err = _validate_role_scope_caps(role, scope, capabilities)
    if err:
        return {"error": err}

    key_id_hex = secrets.token_hex(8)            # 16 hex chars
    key_id = f"k_{key_id_hex}"
    secret = secrets.token_urlsafe(32)
    plaintext = f"{KEY_PREFIX}{key_id_hex}_{secret}"
    key_hash = _hmac_hex(plaintext)
    if not key_hash:
        return {"error": "pepper unavailable; cannot hash"}

    record = {
        "key_id": key_id,
        "key_hash": key_hash,
        "email": email,
        "created_ts": int(time.time()),
        "status": "active",
        "role": role,
        "scope": list(scope),
        "capabilities": list(capabilities),
    }
    _append(record)
    logger.info(
        "api_key issued: id=%s email=%s role=%s scope=%s caps=%s",
        key_id, email, role, scope, capabilities,
    )
    return {
        "key_id": key_id,
        "api_key": plaintext,           # SHOWN ONCE
        "email": email,
        "role": role,
        "scope": scope,
        "capabilities": capabilities,
        "warning": "Store this key NOW. It will never be shown again.",
    }


def verify(provided_key: str) -> dict | None:
    """O(1) verify via key_id prefix + HMAC-SHA256+pepper.

    Returns the active key record on match (with role/scope/capabilities),
    else None. Legacy argon2 records are rejected (no fallback).
    """
    parsed = _parse_plaintext(provided_key)
    if not parsed:
        return None
    key_id, full = parsed
    keys = _replay()
    rec = keys.get(key_id)
    if not rec:
        return None
    stored_hash = rec.get("key_hash", "")
    if not stored_hash or stored_hash.startswith("$argon2"):
        # Legacy record — refuse. Force re-issue.
        logger.warning(
            "rejected legacy argon2 key on verify: key_id=%s email=%s",
            key_id, rec.get("email", "?"),
        )
        return None
    expected = _hmac_hex(full)
    if not expected:
        return None
    if not hmac.compare_digest(stored_hash, expected):
        return None
    return rec


def has_capability(record: dict | None, capability: str) -> bool:
    if not record:
        return False
    caps = record.get("capabilities") or []
    return capability in caps


def has_scope(record: dict | None, client_id: str) -> bool:
    """True if the key's scope authorizes this client_id (admin '*' or explicit match)."""
    if not record or not client_id:
        return False
    scope = record.get("scope") or []
    if scope == ["*"]:
        return True
    return client_id in scope


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
            "role": rec.get("role", "?"),
            "scope": rec.get("scope", []),
            "capabilities": rec.get("capabilities", []),
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
                "POST /provision or /destroy. Returns the plaintext key ONCE — "
                "store it immediately. Logged to /data/api_keys.jsonl.\n"
                "Defaults to role=admin scope=['*'] caps=all. For tenant key, "
                "set role='tenant' and scope=['<client_id>'] (caps default to "
                "['destroy'])."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Client email (audit)"},
                    "role": {
                        "type": "string",
                        "enum": list(VALID_ROLES),
                        "default": ROLE_ADMIN,
                    },
                    "scope_csv": {
                        "type": "string",
                        "description": (
                            "Comma-separated scope. Admin must use '*'. "
                            "Tenant must list one or more client_ids."
                        ),
                        "default": "*",
                    },
                    "capabilities_csv": {
                        "type": "string",
                        "description": (
                            "Comma-separated capabilities from "
                            f"{list(ALL_CAPABILITIES)}. Empty/omitted = role default."
                        ),
                        "default": "",
                    },
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


def _csv_to_list(value: str) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def handle(name: str, arguments: dict | None) -> dict:
    """Dispatcher for provision_* admin tools."""
    arguments = arguments or {}
    if name == "provision_issue_api_key":
        role = (arguments.get("role") or ROLE_ADMIN).strip().lower()
        scope = _csv_to_list(arguments.get("scope_csv", "*"))
        if not scope:
            scope = ["*"] if role == ROLE_ADMIN else []
        caps = _csv_to_list(arguments.get("capabilities_csv", ""))
        return issue(
            email=arguments.get("email", ""),
            role=role,
            scope=scope,
            capabilities=caps if caps else None,
        )
    if name == "provision_revoke_api_key":
        return revoke(arguments.get("key_id", ""), arguments.get("reason", ""))
    if name == "provision_list_api_keys":
        return {"keys": list_active(), "count": len(list_active())}
    return {"error": f"unknown api_key tool: {name}"}
