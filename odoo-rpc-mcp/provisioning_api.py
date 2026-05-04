"""
v3 provisioning HTTP API — RBAC edition (3.0.0-alpha.2).

POST /provision — invoked by client Odoo (l10n_bg_claude_terminal wizard) to
create a new MCP stack on the central poligroup VPS. Caller must hold a key
with the ``provision`` capability (admin role by default). On success, the
response also includes a freshly-issued ``tenant_api_key`` scoped to the
new client_id with destroy-only capability — the client stack saves it for
later teardown.

POST /destroy — tear down a stack. Tenant keys must include their bound
``client_id`` in the body (slug alone is insufficient — slug → client_id is
resolved AFTER the scope check rejects mismatches). Successful destroy from
a tenant key auto-revokes that key (clean teardown). Admin keys never
auto-revoke.

Audit log: /data/provisioning_audit.log (jsonl).

Wire-up: server.py imports this module and adds get_routes() to its
top-level Starlette routes (intentionally OUTSIDE the /admin prefix).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import api_key_manager
import provisioning_engine

logger = logging.getLogger("provisioning_api")

PROVISIONING_AUDIT = Path(os.environ.get(
    "PROVISIONING_AUDIT_FILE", "/data/provisioning_audit.log"))

# Append-only ledger of provisioning attempts. One JSONL row per stage
# transition (`stage1_done`, `stage2_done`, `partial`, `complete`,
# `failed`). Reaper / `/provision/resume` (Phase B) reads this to find
# half-finished provisions. Operator-readable; one line per event.
PROVISIONING_LEDGER = Path(os.environ.get(
    "PROVISIONING_LEDGER_FILE", "/data/provisioning_ledger.jsonl"))

# ── Rate limiting (per-IP token bucket + failed-auth lockout) ───────
PROVISION_BODY_MAX_BYTES = int(os.environ.get("MCP_PROVISION_BODY_MAX", "4096"))
AUDIT_LOG_MAX_BYTES = int(os.environ.get("MCP_AUDIT_LOG_MAX_BYTES", str(50 * 1024 * 1024)))
AUDIT_LOG_KEEP = int(os.environ.get("MCP_AUDIT_LOG_KEEP", "5"))

_RATE_CONFIG = {
    # capacity = max burst; refill_per_sec = sustained rate
    "provision": {"capacity": 5, "refill_per_sec": 5 / 60.0},
    "destroy": {"capacity": 10, "refill_per_sec": 10 / 60.0},
}
_FAIL_THRESHOLD = int(os.environ.get("MCP_PROVISION_FAIL_THRESHOLD", "20"))
_FAIL_WINDOW_SEC = int(os.environ.get("MCP_PROVISION_FAIL_WINDOW_SEC", "3600"))
_LOCKOUT_SEC = int(os.environ.get("MCP_PROVISION_LOCKOUT_SEC", "3600"))

_rate_buckets: dict[str, dict] = {}
_fail_lockouts: dict[str, dict] = {}
_rate_lock = threading.Lock()

# Serializes audit log rotate+write across worker threads. Without
# this, two concurrent _audit() callers can both observe size >= MAX
# and race on rename → log entries shift twice or once depending on
# interleaving. Multi-process workers need fcntl.flock instead — but
# the v3 default is single-worker uvicorn, so a threading.Lock is
# sufficient for the in-process thread pool used by asyncio.to_thread.
_audit_lock = threading.Lock()
_ledger_lock = threading.Lock()


def _trusted_internal_nets() -> list[ipaddress._BaseNetwork]:
    """Docker-standard private CIDRs that bypass rate limiting.

    Defaults follow RFC 1918 ranges that Docker uses for its bridges:
      - 172.16.0.0/12 (default Docker bridge + user-defined networks)
      - 10.0.0.0/8    (overlay/swarm networks)
      - 127.0.0.0/8   (loopback)
      - ::1/128       (IPv6 loopback)
    192.168.0.0/16 is INTENTIONALLY excluded — that range is residential
    LAN, often the source of unintended public-facing traffic on
    misconfigured deployments.

    Override via MCP_TRUSTED_INTERNAL_NETS=10.5.0.0/16,172.20.0.0/16
    """
    raw = os.environ.get("MCP_TRUSTED_INTERNAL_NETS", "").strip()
    if raw:
        nets = []
        for cidr in raw.split(","):
            cidr = cidr.strip()
            if not cidr:
                continue
            try:
                nets.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                logger.warning("ignoring invalid CIDR in MCP_TRUSTED_INTERNAL_NETS: %s", cidr)
        return nets
    return [
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    ]


_TRUSTED_NETS = _trusted_internal_nets()


def _is_trusted_internal(ip: str) -> bool:
    """True if `ip` is in a Docker-standard internal network."""
    if ip == "?":
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_NETS)


def _client_ip_for(req: Request) -> str:
    """Return the originating client IP, honouring X-Forwarded-For only
    when the immediate hop is itself a trusted reverse proxy.

    Threat: if Cloudflare tunnel / nginx forwards XFF and the gateway
    blindly trusts the header, an attacker on the public internet
    appears as 127.0.0.1 (proxy loopback) and bypasses rate limits.

    Rule: trust XFF *only* when `req.client.host` is itself an internal
    address. Take the LEFTMOST entry in XFF (the original client) —
    each hop appends to the right. If XFF is absent, fall back to
    the direct connection's IP.
    """
    direct = req.client.host if req.client else "?"
    if not _is_trusted_internal(direct):
        return direct
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return direct


def _check_rate(ip: str, route: str) -> tuple[bool, str]:
    """Token bucket per IP per route. Lockouts override.

    Returns (allowed, denial_reason). On allow, denial_reason is empty.
    Internal-Docker IPs (RFC 1918 minus 192.168/16) bypass.
    """
    if _is_trusted_internal(ip):
        return True, ""
    cfg = _RATE_CONFIG.get(route)
    if not cfg:
        return True, ""
    now = time.time()
    with _rate_lock:
        # Lockout takes priority
        lock = _fail_lockouts.get(ip)
        if lock and lock.get("locked_until", 0) > now:
            return False, f"locked_out_for_{int(lock['locked_until'] - now)}s"

        key = f"{ip}|{route}"
        bucket = _rate_buckets.get(key)
        if bucket is None:
            bucket = {"tokens": float(cfg["capacity"]), "last": now}
            _rate_buckets[key] = bucket
        else:
            elapsed = now - bucket["last"]
            bucket["tokens"] = min(
                float(cfg["capacity"]),
                bucket["tokens"] + elapsed * cfg["refill_per_sec"],
            )
            bucket["last"] = now

        if bucket["tokens"] < 1.0:
            return False, "rate_limit_exceeded"
        bucket["tokens"] -= 1.0
        return True, ""


def _record_failure(ip: str) -> None:
    """Track auth failures per IP. Trip lockout at threshold."""
    if _is_trusted_internal(ip):
        return
    now = time.time()
    with _rate_lock:
        rec = _fail_lockouts.get(ip)
        if rec is None or now - rec.get("first", now) > _FAIL_WINDOW_SEC:
            rec = {"fails": 0, "first": now, "locked_until": 0.0}
            _fail_lockouts[ip] = rec
        rec["fails"] += 1
        if rec["fails"] >= _FAIL_THRESHOLD:
            rec["locked_until"] = now + _LOCKOUT_SEC


async def _read_capped_body(req: Request, max_bytes: int) -> bytes:
    """Read request body with a hard size cap. Raises ValueError if exceeded."""
    declared = req.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ValueError(f"declared content-length {declared} exceeds cap {max_bytes}")
    body = bytearray()
    async for chunk in req.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError(f"body exceeds cap {max_bytes} bytes")
    return bytes(body)


def _rotate_audit_log() -> None:
    """Size-based rotation: at AUDIT_LOG_MAX_BYTES → .1, keep AUDIT_LOG_KEEP."""
    try:
        if not PROVISIONING_AUDIT.exists():
            return
        if PROVISIONING_AUDIT.stat().st_size < AUDIT_LOG_MAX_BYTES:
            return
        # Shift .{i} → .{i+1}, drop oldest
        for i in range(AUDIT_LOG_KEEP - 1, 0, -1):
            src = PROVISIONING_AUDIT.with_suffix(f".log.{i}")
            dst = PROVISIONING_AUDIT.with_suffix(f".log.{i+1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        PROVISIONING_AUDIT.rename(PROVISIONING_AUDIT.with_suffix(".log.1"))
    except Exception as e:
        logger.warning("audit log rotation failed: %s", e)


def _audit(action: str, **extra) -> None:
    payload = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "action": action,
        **extra,
    }
    try:
        PROVISIONING_AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock:
            _rotate_audit_log()
            with open(PROVISIONING_AUDIT, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("provisioning audit write failed: %s", e)


def _ledger_record(request_id: str, event: str, **fields) -> None:
    """Append a single JSONL line to the provisioning ledger.

    The ledger is the recovery-state store for partial provisions —
    `/provision/resume` (Phase B) and the reaper cron read it to find
    stages completed vs failed. Append-only; tail-friendly.

    Events: started, stage1_done, stage2_done, complete, partial, failed.
    """
    payload = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "request_id": request_id,
        "event": event,
        **fields,
    }
    try:
        PROVISIONING_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with _ledger_lock:
            with open(PROVISIONING_LEDGER, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("provisioning ledger write failed: %s", e)


def _err(reason: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"error": reason, **extra}, status_code=status)


def _truncate_key(api_key: str) -> str:
    """Return a forensic-useful but minimal-leak preview of an API key.

    Format: `<first 8>…<last 4>`. Compared to the previous `[:18]+…`
    (which leaked the entire key_id portion of `mcpv3_<key_id>_<random>`,
    enabling targeted key-revocation lookups by anyone with audit-log
    access), this surfaces just enough to correlate to a specific key
    record without exposing the prefix tail.
    """
    if not api_key:
        return "<empty>"
    if len(api_key) <= 12:
        return "<short>"
    return f"{api_key[:8]}…{api_key[-4:]}"


# ── Input validation ────────────────────────────────────────────────
# Length caps protect downstream filesystem/Odoo paths and audit log.
# Patterns aim to reject obvious injection vectors (shell, path
# separators, HTML, control chars) rather than enforce strict
# semantic correctness — that lives in provisioning_engine.

_RE_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_RE_SLUG = re.compile(r"^[a-zA-Z0-9_\-]{1,50}$")
_RE_VAT = re.compile(r"^[A-Z]{2}[A-Za-z0-9]{2,14}$")
_RE_ANTHROPIC_KEY = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,200}$")

_MIN_PASSWORD_LEN = int(os.environ.get("MCP_PROVISION_MIN_PASSWORD_LEN", "14"))
_MAX_PASSWORD_LEN = int(os.environ.get("MCP_PROVISION_MAX_PASSWORD_LEN", "256"))


def _validate_password(pw: str) -> str:
    """NIST 800-63B-aligned password policy: length-only, no composition.

    Composition rules (force upper+lower+digit+symbol) increase user
    burden without measurable strength gain — users substitute predictable
    patterns ('Password1!') that lookups break trivially. Modern guidance:
    enforce length, screen against breached lists out-of-band, and accept
    any UTF-8 of reasonable size.

    Future hardening: Have-I-Been-Pwned k-anonymity check (Phase 2).

    Returns "" if OK, else a denial reason.
    """
    if not pw:
        return "password required"
    if len(pw) < _MIN_PASSWORD_LEN:
        return f"password must be at least {_MIN_PASSWORD_LEN} characters"
    if len(pw) > _MAX_PASSWORD_LEN:
        return f"password too long (>{_MAX_PASSWORD_LEN} chars)"
    # Reject the obvious zero-entropy degenerates.
    if len(set(pw)) < 4:
        return "password too repetitive (use at least 4 distinct characters)"
    return ""


def _validate_provision_inputs(body: dict) -> tuple[dict, str]:
    """Strict validation of /provision body. Returns (clean_dict, error_reason).

    On error, error_reason is non-empty and clean_dict is partial.
    """
    out: dict = {}
    out["api_key"] = (body.get("api_key") or "").strip()
    out["password"] = (body.get("password") or "").strip()
    out["email"] = (body.get("email") or "").strip().lower()
    out["slug"] = (body.get("slug") or body.get("tenant_slug") or "").strip()
    out["vat"] = (body.get("vat") or body.get("company_vat") or "").strip().upper()
    out["anthropic_key"] = (body.get("anthropic_api_key") or "").strip()

    if out["email"]:
        if len(out["email"]) > 254 or not _RE_EMAIL.match(out["email"]):
            return out, "invalid_email_format"
    if out["slug"]:
        if not _RE_SLUG.match(out["slug"]):
            return out, "invalid_slug_format (alphanumeric, dash, underscore; max 50)"
    if out["vat"]:
        if not _RE_VAT.match(out["vat"]):
            return out, "invalid_vat_format (expect 2-letter country code + alphanumeric)"
    if out["anthropic_key"] and not _RE_ANTHROPIC_KEY.match(out["anthropic_key"]):
        return out, "invalid_anthropic_api_key_format"
    if out["password"]:
        pw_err = _validate_password(out["password"])
        if pw_err:
            return out, pw_err
    return out, ""


def _validate_destroy_inputs(body: dict) -> tuple[dict, str]:
    """Strict validation of /destroy body."""
    out: dict = {}
    out["api_key"] = (body.get("api_key") or "").strip()
    out["slug"] = (body.get("slug") or body.get("tenant_slug") or "").strip()
    out["vat"] = (body.get("vat") or body.get("company_vat") or "").strip().upper()
    out["client_id"] = (body.get("client_id") or "").strip()

    if out["slug"] and not _RE_SLUG.match(out["slug"]):
        return out, "invalid_slug_format"
    if out["vat"] and not _RE_VAT.match(out["vat"]):
        return out, "invalid_vat_format"
    if out["client_id"] and not _RE_SLUG.match(out["client_id"]):
        # client_id is server-derived; same charset as slug
        return out, "invalid_client_id_format"
    return out, ""


async def _provision_handler(req: Request):
    started = time.time()
    request_id = secrets.token_hex(16)
    client_ip = _client_ip_for(req)

    # 1. Rate limit (per-IP token bucket + lockout)
    allowed, reason = _check_rate(client_ip, "provision")
    if not allowed:
        _audit("REJECTED", reason=reason, ip=client_ip, route="provision")
        return _err(reason, 429)

    # 2. Body size cap (avoid memory DoS via huge payload)
    try:
        raw = await _read_capped_body(req, PROVISION_BODY_MAX_BYTES)
    except ValueError as e:
        _audit("REJECTED", reason="body_too_large", ip=client_ip, detail=str(e))
        return _err("body too large", 413)
    try:
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        _audit("REJECTED", reason="bad_json", ip=client_ip)
        return _err("invalid_json", 400)

    # 3. Strict input validation (regex + length caps)
    clean, val_err = _validate_provision_inputs(body)
    if val_err:
        _audit("REJECTED", reason=val_err, ip=client_ip)
        return _err(val_err, 400)

    api_key = clean["api_key"]
    password = clean["password"]
    email = clean["email"]
    slug = clean["slug"]
    vat = clean["vat"]
    anthropic_key = clean["anthropic_key"]

    if not api_key:
        _record_failure(client_ip)
        _audit("REJECTED", reason="missing_api_key", ip=client_ip)
        return _err("api_key required", 401)

    key_record = api_key_manager.verify(api_key)
    if not key_record:
        _record_failure(client_ip)
        _audit("REJECTED", reason="invalid_api_key", ip=client_ip,
               key_prefix=_truncate_key(api_key))
        return _err("invalid_api_key", 401)

    if not api_key_manager.has_capability(key_record, api_key_manager.CAP_PROVISION):
        _audit("REJECTED", reason="missing_capability", ip=client_ip,
               key_id=key_record["key_id"], required="provision")
        return _err("forbidden: 'provision' capability required", 403)

    # Use authenticated email if not provided.
    if not email:
        email = key_record.get("email", "")

    # Password must be present + meet strength rules.
    pw_err = _validate_password(password)
    if pw_err:
        _audit("REJECTED", reason="weak_password", ip=client_ip,
               key_id=key_record["key_id"], detail=pw_err)
        return _err(pw_err, 400)

    _audit("STARTED", ip=client_ip, key_id=key_record["key_id"],
           role=key_record.get("role"), email=email, slug_hint=slug, vat=vat,
           request_id=request_id)
    _ledger_record(request_id, "started", ip=client_ip,
                   key_id=key_record["key_id"], email=email,
                   slug_hint=slug, vat=vat)

    try:
        # provisioning_engine.provision is sync (httpx + Portainer +
        # Cloudflare). Run in thread to avoid blocking the event loop
        # for 5-60 s while other requests stall.
        result = await asyncio.to_thread(
            provisioning_engine.provision,
            slug_hint=slug or email,
            password=password,
            email=email,
            anthropic_key=anthropic_key,
            vat=vat,
        )
    except Exception as e:
        logger.exception("provisioning engine crashed")
        _audit("FAILED", ip=client_ip, key_id=key_record["key_id"],
               email=email, error=str(e), request_id=request_id)
        _ledger_record(request_id, "failed", stage=1, error=str(e))
        return _err("engine_crashed", 500, detail=str(e), request_id=request_id)

    elapsed_ms = int((time.time() - started) * 1000)

    if "error" in result:
        _audit("FAILED", ip=client_ip, key_id=key_record["key_id"],
               email=email, slug=result.get("slug", slug),
               error=result["error"], elapsed_ms=elapsed_ms,
               request_id=request_id)
        _ledger_record(request_id, "failed", stage=1,
                       error=result["error"], slug=result.get("slug", slug))
        return _err(result["error"], 500, request_id=request_id,
                    **{k: v for k, v in result.items() if k != "error"})

    new_client_id = result.get("client_id", "")

    # Stage 1 completed (provisioning engine returned success).
    _ledger_record(request_id, "stage1_done",
                   client_id=new_client_id,
                   status=result.get("status"),
                   slug=result.get("slug"),
                   mcp_url=result.get("mcp_url"))

    # Stage 2: auto-issue destroy-only tenant key bound to this client_id.
    # If issuance fails, return HTTP 409 (Conflict) with structured
    # recovery info — NOT 500. Caller should hit /provision/resume
    # (Phase B) with the request_id to retry only Stage 2; auto-rollback
    # is unsafe here because Stage 1 is idempotent on client_id and
    # may be a re-use of a legitimate prior provision.
    # Reference: brandur.org idempotency keys, Stripe roll-forward,
    # AWS Step Functions saga (compensator must escalate, not destroy).
    tenant_key_info: dict = {}
    if new_client_id and result.get("status") in ("completed", "idempotent"):
        issued = api_key_manager.issue(
            email=email,
            role=api_key_manager.ROLE_TENANT,
            scope=[new_client_id],
            capabilities=[api_key_manager.CAP_DESTROY],
        )
        if "error" in issued:
            logger.error(
                "[PARTIAL] tenant key issuance failed after Stage 1 "
                "success client_id=%s request_id=%s: %s",
                new_client_id, request_id, issued["error"],
            )
            _audit("TENANT_KEY_ISSUE_FAILED", ip=client_ip,
                   key_id=key_record["key_id"], client_id=new_client_id,
                   error=issued["error"], stack_orphaned=True,
                   request_id=request_id)
            _ledger_record(request_id, "partial",
                           client_id=new_client_id,
                           mcp_url=result.get("mcp_url"),
                           failed_stage=2,
                           error=issued["error"])
            return JSONResponse(
                {
                    "status": "partial",
                    "error": "tenant_key_issuance_failed",
                    "request_id": request_id,
                    "client_id": new_client_id,
                    "mcp_url": result.get("mcp_url"),
                    "retry_endpoint": "/provision/resume",
                    "hint": (
                        f"Stack is provisioned but tenant teardown key "
                        f"could not be issued. POST /provision/resume "
                        f"with {{request_id, api_key}} to retry Stage 2 "
                        f"only. Or use admin key on /destroy with "
                        f"client_id='{new_client_id}'."
                    ),
                    "detail": issued["error"],
                },
                status_code=409,
            )
        tenant_key_info = {
            "tenant_api_key": issued["api_key"],
            "tenant_key_id": issued["key_id"],
            "tenant_key_capabilities": issued["capabilities"],
            "tenant_key_scope": issued["scope"],
        }
        _audit("TENANT_KEY_ISSUED", ip=client_ip,
               key_id=key_record["key_id"],
               tenant_key_id=issued["key_id"],
               client_id=new_client_id, request_id=request_id)
        _ledger_record(request_id, "stage2_done",
                       client_id=new_client_id,
                       tenant_key_id=issued["key_id"])

    _audit("COMPLETED" if result.get("status") == "completed" else "IDEMPOTENT",
           ip=client_ip, key_id=key_record["key_id"],
           email=email, slug=result["slug"],
           client_id=new_client_id, elapsed_ms=elapsed_ms,
           dry_run=result.get("dry_run", False),
           request_id=request_id)
    _ledger_record(request_id, "complete",
                   client_id=new_client_id, slug=result["slug"],
                   elapsed_ms=elapsed_ms,
                   status=result["status"])

    safe_result = {
        "status": result["status"],
        "request_id": request_id,
        "client_id": new_client_id,
        "mcp_url": result["mcp_url"],
        "zip_base64": result["zip_base64"],
        "zip_filename": result["zip_filename"],
        "zip_size_bytes": result["zip_size_bytes"],
        "elapsed_s": result.get("elapsed_s"),
        "dry_run": result.get("dry_run", False),
        **tenant_key_info,
    }
    return JSONResponse(safe_result, status_code=200)


async def _destroy_handler(req: Request):
    """POST /destroy — tear down a previously provisioned stack.

    Body: ``{api_key, client_id, slug?, vat?}``.

    Auth model:
      - admin role: any of slug/vat/client_id accepted.
      - tenant role: ``client_id`` REQUIRED in body and MUST equal the key's
        single bound scope entry. Slug-only requests are rejected because
        slug → client_id resolution happens INSIDE the engine, after the
        scope check would already have to commit.

    Successful destroy with a tenant key auto-revokes the key (one-shot).
    """
    started = time.time()
    client_ip = _client_ip_for(req)

    # 1. Rate limit
    allowed, reason = _check_rate(client_ip, "destroy")
    if not allowed:
        _audit("DESTROY_REJECTED", reason=reason, ip=client_ip, route="destroy")
        return _err(reason, 429)

    # 2. Body size cap
    try:
        raw = await _read_capped_body(req, PROVISION_BODY_MAX_BYTES)
    except ValueError as e:
        _audit("DESTROY_REJECTED", reason="body_too_large", ip=client_ip, detail=str(e))
        return _err("body too large", 413)
    try:
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        _audit("DESTROY_REJECTED", reason="bad_json", ip=client_ip)
        return _err("invalid_json", 400)

    # 3. Strict input validation
    clean, val_err = _validate_destroy_inputs(body)
    if val_err:
        _audit("DESTROY_REJECTED", reason=val_err, ip=client_ip)
        return _err(val_err, 400)

    api_key = clean["api_key"]
    slug = clean["slug"]
    vat = clean["vat"]
    client_id = clean["client_id"]

    if not api_key:
        _record_failure(client_ip)
        _audit("DESTROY_REJECTED", reason="missing_api_key", ip=client_ip)
        return _err("api_key required", 401)

    key_record = api_key_manager.verify(api_key)
    if not key_record:
        _record_failure(client_ip)
        _audit("DESTROY_REJECTED", reason="invalid_api_key", ip=client_ip,
               key_prefix=_truncate_key(api_key))
        return _err("invalid_api_key", 401)

    if not api_key_manager.has_capability(key_record, api_key_manager.CAP_DESTROY):
        _audit("DESTROY_REJECTED", reason="missing_capability", ip=client_ip,
               key_id=key_record["key_id"], required="destroy")
        return _err("forbidden: 'destroy' capability required", 403)

    role = key_record.get("role")
    if role == api_key_manager.ROLE_TENANT:
        if not client_id:
            _audit("DESTROY_REJECTED", reason="tenant_must_send_client_id",
                   ip=client_ip, key_id=key_record["key_id"])
            return _err("tenant key requires explicit 'client_id' in body", 400)
        if not api_key_manager.has_scope(key_record, client_id):
            _audit("DESTROY_REJECTED", reason="scope_mismatch",
                   ip=client_ip, key_id=key_record["key_id"],
                   requested_client_id=client_id,
                   key_scope=key_record.get("scope"))
            return _err("forbidden: client_id not in key scope", 403)
    elif not (slug or vat or client_id):
        _audit("DESTROY_REJECTED", reason="no_identifier",
               ip=client_ip, key_id=key_record["key_id"])
        return _err("provide slug, vat, or client_id", 400)

    _audit("DESTROY_STARTED", ip=client_ip, key_id=key_record["key_id"],
           role=role, slug=slug, vat=vat, client_id=client_id)

    try:
        # Same async-blocking concern as /provision.
        result = await asyncio.to_thread(
            provisioning_engine.destroy,
            slug_hint=slug, vat=vat, client_id=client_id,
        )
    except Exception as e:
        logger.exception("destroy engine crashed")
        _audit("DESTROY_FAILED", ip=client_ip, key_id=key_record["key_id"],
               slug=slug, error=str(e))
        return _err("engine_crashed", 500, detail=str(e))

    elapsed_ms = int((time.time() - started) * 1000)

    if "error" in result:
        _audit("DESTROY_FAILED", ip=client_ip, key_id=key_record["key_id"],
               slug=result.get("slug", slug), error=result["error"],
               elapsed_ms=elapsed_ms)
        status = 404 if result["error"] == "not_found" else 500
        return _err(result["error"], status,
                    **{k: v for k, v in result.items() if k != "error"})

    # Auto-revoke tenant key after successful teardown.
    auto_revoked = False
    if role == api_key_manager.ROLE_TENANT and result.get("status") in (
        "destroyed", "already_destroyed",
    ):
        rev = api_key_manager.revoke(
            key_record["key_id"],
            reason=f"auto-revoke after /destroy of {result.get('client_id','?')}",
        )
        auto_revoked = bool(rev.get("revoked"))
        _audit("TENANT_KEY_AUTO_REVOKED", ip=client_ip,
               key_id=key_record["key_id"], result=rev)

    _audit(
        "DESTROY_COMPLETED" if result.get("status") == "destroyed" else "DESTROY_NOOP",
        ip=client_ip, key_id=key_record["key_id"],
        slug=result["slug"], client_id=result.get("client_id"),
        elapsed_ms=elapsed_ms,
        dry_run=result.get("dry_run", False),
        auto_revoked=auto_revoked,
    )

    safe_result = {
        "status": result["status"],
        "slug": result["slug"],
        "client_id": result.get("client_id"),
        "hostname": result.get("hostname"),
        "elapsed_s": result.get("elapsed_s"),
        "dry_run": result.get("dry_run", False),
        "tenant_key_revoked": auto_revoked,
    }
    return JSONResponse(safe_result, status_code=200)


def get_routes() -> list:
    """Returns Starlette routes to mount at server top-level (not under /admin)."""
    return [
        Route("/provision", _provision_handler, methods=["POST"]),
        Route("/destroy", _destroy_handler, methods=["POST"]),
    ]
