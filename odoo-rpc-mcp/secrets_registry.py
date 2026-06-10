"""
v3 Secrets Registry (B.4) — track + rotate per-stack secrets (stack tokens,
admin tokens, OAuth secrets) so a redeploy never silently resets them.

Storage mirrors api_key_manager: append-only JSONL (latest-record-wins),
chmod 600, pepper-derived crypto, fail-closed when MCP_KEY_PEPPER is unset/weak.

Two crypto layers off the SAME pepper (MCP_KEY_PEPPER, ≥32 chars):
  * HMAC-SHA256 fingerprint — for non-recoverable verification / dedup.
  * Fernet ciphertext       — for RECOVERABLE secrets (a stack token must be
                              re-injected into the compose on rotation), key
                              derived from the pepper via SHA256→urlsafe-b64.

`list_secrets` NEVER returns plaintext/ciphertext — only id, target, kind,
fingerprint, status, timestamps. `reveal(secret_id)` returns the plaintext and
is intended to be called only by the orchestrated rotation path (admin-gated in
server.py, like provision_*).

ROTATION COUPLING: rotating a stack token requires redeploying that stack with
the new token AND updating v3's own proxy bearer for the tenant. `rotate()`
returns the new plaintext + a `requires_redeploy` flag; the caller (server.py /
a future orchestrated tool) pairs it with fleet_manager.fleet_upgrade. The
registry itself performs NO deploy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import time
from pathlib import Path

logger = logging.getLogger("secrets_registry")

SECRETS_FILE = Path(os.environ.get("SECRETS_REGISTRY_FILE", "/data/secrets_registry.jsonl"))
PEPPER_ENV = "MCP_KEY_PEPPER"

VALID_KINDS = {"stack_token", "admin_token", "oauth_secret", "anthropic_key", "other"}


# ─── Crypto (pepper-derived, fail-closed) ──────────────────────────────────

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


def _fernet():
    """Fernet keyed off the pepper. None when pepper is unset/weak (fail-closed)."""
    p = _pepper()
    if not p:
        return None
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(p).digest())
    return Fernet(key)


def _encrypt(plaintext: str) -> str | None:
    f = _fernet()
    if f is None:
        return None
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str | None:
    f = _fernet()
    if f is None or not token:
        return None
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        return None


# ─── Storage (append-only JSONL, latest-record-wins) ───────────────────────

def _replay() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not SECRETS_FILE.is_file():
        return out
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = rec.get("secret_id")
                if not sid:
                    continue
                if rec.get("status") == "revoked":
                    out.pop(sid, None)
                else:
                    out[sid] = rec
    except OSError as e:
        logger.warning("secrets read failed: %s", e)
    return out


def _append(record: dict) -> None:
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SECRETS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass


def _new_id() -> str:
    return "s_" + _secrets.token_hex(8)


def _public(rec: dict) -> dict:
    """Strip recoverable material — safe for listing."""
    return {
        "secret_id": rec.get("secret_id"),
        "target_stack": rec.get("target_stack"),
        "kind": rec.get("kind"),
        "fingerprint": rec.get("fingerprint"),
        "status": rec.get("status"),
        "created_ts": rec.get("created_ts"),
        "rotated_from": rec.get("rotated_from"),
        "recoverable": bool(rec.get("enc")),
    }


# ─── Public API ────────────────────────────────────────────────────────────

def register(target_stack: str, kind: str, value: str = "",
             recoverable: bool = True) -> dict:
    """Record a secret for a stack. Generates one if `value` is empty.

    Stores an HMAC fingerprint always; stores a Fernet ciphertext when
    `recoverable` (so it can be re-injected on rotation). Fail-closed without a
    strong pepper.
    """
    if not _pepper():
        return {"error": "weak_or_missing_pepper",
                "hint": f"set {PEPPER_ENV} (>=32 chars)"}
    if kind not in VALID_KINDS:
        return {"error": "invalid_kind", "valid": sorted(VALID_KINDS)}
    if not target_stack:
        return {"error": "target_stack is required"}
    plaintext = value or _secrets.token_urlsafe(32)
    enc = _encrypt(plaintext) if recoverable else None
    if recoverable and enc is None:
        return {"error": "encryption_unavailable",
                "hint": "cryptography/Fernet not available or pepper weak"}
    rec = {
        "secret_id": _new_id(),
        "target_stack": target_stack,
        "kind": kind,
        "fingerprint": _hmac_hex(plaintext),
        "enc": enc,
        "status": "active",
        "created_ts": int(time.time()),
        "rotated_from": None,
    }
    _append(rec)
    out = _public(rec)
    out["value"] = plaintext  # returned ONCE to the caller
    return out


def get(secret_id: str) -> dict | None:
    rec = _replay().get(secret_id)
    return _public(rec) if rec else None


def reveal(secret_id: str) -> dict:
    """Return the plaintext of a recoverable secret (admin-gated upstream)."""
    rec = _replay().get(secret_id)
    if not rec:
        return {"error": "unknown_secret", "secret_id": secret_id}
    if not rec.get("enc"):
        return {"error": "not_recoverable", "secret_id": secret_id}
    pt = _decrypt(rec["enc"])
    if pt is None:
        return {"error": "decrypt_failed", "secret_id": secret_id}
    return {"secret_id": secret_id, "target_stack": rec.get("target_stack"),
            "kind": rec.get("kind"), "value": pt}


def list_secrets(target_stack: str | None = None) -> dict:
    recs = list(_replay().values())
    if target_stack:
        recs = [r for r in recs if r.get("target_stack") == target_stack]
    recs.sort(key=lambda r: r.get("created_ts", 0))
    return {"count": len(recs), "secrets": [_public(r) for r in recs]}


def rotate(target_stack: str, kind: str) -> dict:
    """Generate a new secret of `kind` for a stack, retiring the previous one.

    Returns the new plaintext ONCE plus `requires_redeploy=True` — the caller
    must redeploy the stack (fleet_manager) so the new value takes effect and
    update v3's own proxy bearer for the tenant. The registry deploys nothing.
    """
    if not _pepper():
        return {"error": "weak_or_missing_pepper"}
    if kind not in VALID_KINDS:
        return {"error": "invalid_kind", "valid": sorted(VALID_KINDS)}
    # Find the current active secret of this kind for the stack.
    prev = None
    for r in _replay().values():
        if r.get("target_stack") == target_stack and r.get("kind") == kind:
            if prev is None or r.get("created_ts", 0) > prev.get("created_ts", 0):
                prev = r
    plaintext = _secrets.token_urlsafe(32)
    enc = _encrypt(plaintext)
    if enc is None:
        return {"error": "encryption_unavailable"}
    rec = {
        "secret_id": _new_id(),
        "target_stack": target_stack,
        "kind": kind,
        "fingerprint": _hmac_hex(plaintext),
        "enc": enc,
        "status": "active",
        "created_ts": int(time.time()),
        "rotated_from": prev.get("secret_id") if prev else None,
    }
    _append(rec)
    # Retire the previous one.
    if prev:
        _append({**prev, "status": "rotated", "rotated_ts": int(time.time())})
    out = _public(rec)
    out["value"] = plaintext
    out["requires_redeploy"] = True
    out["hint"] = ("Redeploy the stack with this new value (fleet_upgrade with "
                   "updated env) AND update v3's proxy bearer for the tenant, "
                   "else the tenant connection breaks.")
    return out


def revoke(secret_id: str, reason: str = "") -> dict:
    rec = _replay().get(secret_id)
    if not rec:
        return {"error": "unknown_secret", "secret_id": secret_id}
    _append({**rec, "status": "revoked", "reason": reason,
             "revoked_ts": int(time.time())})
    return {"ok": True, "secret_id": secret_id, "status": "revoked"}


# ─── Control-plane Tool definitions ────────────────────────────────────────

def get_admin_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(
            name="secrets_list",
            description=("ADMIN: list registered per-stack secrets (id, stack, "
                         "kind, fingerprint, status) — NEVER returns plaintext."),
            inputSchema={
                "type": "object",
                "properties": {"target_stack": {"type": "string"}},
            },
        ),
        Tool(
            name="secrets_register",
            description=("ADMIN: record (or generate) a secret for a stack. "
                         "Stores an HMAC fingerprint + an encrypted copy "
                         "(recoverable) for later rotation. Returns the value once."),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_stack": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": sorted(VALID_KINDS)},
                    "value": {"type": "string",
                              "description": "omit to auto-generate"},
                    "recoverable": {"type": "boolean", "default": True},
                },
                "required": ["target_stack", "kind"],
            },
        ),
        Tool(
            name="secrets_rotate",
            description=("ADMIN: generate a new secret of a kind for a stack and "
                         "retire the previous one. Returns the new value once + "
                         "requires_redeploy — pair with fleet_upgrade to apply."),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_stack": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(VALID_KINDS)},
                },
                "required": ["target_stack", "kind"],
            },
        ),
        Tool(
            name="secrets_revoke",
            description="ADMIN: mark a secret revoked (audit).",
            inputSchema={
                "type": "object",
                "properties": {"secret_id": {"type": "string"},
                               "reason": {"type": "string"}},
                "required": ["secret_id"],
            },
        ),
    ]


ADMIN_TOOL_NAMES = {"secrets_list", "secrets_register", "secrets_rotate",
                    "secrets_revoke"}


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "secrets_list":
        return list_secrets(arguments.get("target_stack"))
    if name == "secrets_register":
        return register(arguments.get("target_stack", ""),
                        arguments.get("kind", ""),
                        arguments.get("value", ""),
                        arguments.get("recoverable", True))
    if name == "secrets_rotate":
        return rotate(arguments.get("target_stack", ""),
                      arguments.get("kind", ""))
    if name == "secrets_revoke":
        return revoke(arguments.get("secret_id", ""),
                      arguments.get("reason", ""))
    return {"error": f"unknown secrets tool: {name}"}
