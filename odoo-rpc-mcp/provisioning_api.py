"""
v3 provisioning HTTP API.

POST /provision — invoked by client Odoo (l10n_bg_claude_terminal wizard) to
create a new MCP stack on the central poligroup VPS. Authenticated by API key
issued via provision_issue_api_key (admin MCP tool).

Audit log: /data/provisioning_audit.log (jsonl).

Wire-up: server.py imports this module and adds get_routes() to its admin
or top-level Starlette routes (it is intentionally OUTSIDE the /admin
prefix so client Odoo doesn't need MCP_ADMIN_TOKEN — only the per-tenant
provisioning API key).
"""

from __future__ import annotations

import json
import logging
import os
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


def _audit(action: str, **extra) -> None:
    payload = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "action": action,
        **extra,
    }
    try:
        PROVISIONING_AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with open(PROVISIONING_AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("provisioning audit write failed: %s", e)


def _err(reason: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"error": reason, **extra}, status_code=status)


async def _provision_handler(req: Request):
    started = time.time()
    client_ip = req.client.host if req.client else "?"

    # Parse body.
    try:
        body = await req.json()
    except Exception:
        _audit("REJECTED", reason="bad_json", ip=client_ip)
        return _err("invalid_json", 400)

    api_key = (body.get("api_key") or "").strip()
    password = (body.get("password") or "").strip()
    email = (body.get("email") or "").strip().lower()
    slug = (body.get("slug") or body.get("tenant_slug") or "").strip()
    vat = (body.get("vat") or body.get("company_vat") or "").strip()
    anthropic_key = (body.get("anthropic_api_key") or "").strip()

    if not api_key:
        _audit("REJECTED", reason="missing_api_key", ip=client_ip)
        return _err("api_key required", 401)

    # Authenticate.
    key_record = api_key_manager.verify(api_key)
    if not key_record:
        _audit("REJECTED", reason="invalid_api_key", ip=client_ip,
               key_prefix=api_key[:12] + "…")
        return _err("invalid_api_key", 401)

    # Use authenticated email if not provided.
    if not email:
        email = key_record.get("email", "")

    if not password or len(password) < 8:
        _audit("REJECTED", reason="weak_password", ip=client_ip,
               key_id=key_record["key_id"])
        return _err("password must be at least 8 characters", 400)

    _audit("STARTED", ip=client_ip, key_id=key_record["key_id"],
           email=email, slug_hint=slug, vat=vat)

    # Run engine (sync — provisioning может to take 30-60s).
    try:
        result = provisioning_engine.provision(
            slug_hint=slug or email,
            password=password,
            email=email,
            anthropic_key=anthropic_key,
            vat=vat,
        )
    except Exception as e:
        logger.exception("provisioning engine crashed")
        _audit("FAILED", ip=client_ip, key_id=key_record["key_id"],
               email=email, error=str(e))
        return _err("engine_crashed", 500, detail=str(e))

    elapsed_ms = int((time.time() - started) * 1000)

    if "error" in result:
        _audit("FAILED", ip=client_ip, key_id=key_record["key_id"],
               email=email, slug=result.get("slug", slug),
               error=result["error"], elapsed_ms=elapsed_ms)
        return _err(result["error"], 500, **{k: v for k, v in result.items() if k != "error"})

    _audit("COMPLETED" if result.get("status") == "completed" else "IDEMPOTENT",
           ip=client_ip, key_id=key_record["key_id"],
           email=email, slug=result["slug"],
           client_id=result["client_id"], elapsed_ms=elapsed_ms,
           dry_run=result.get("dry_run", False))

    # Strip server-internal fields before returning to client.
    safe_result = {
        "status": result["status"],
        "client_id": result["client_id"],
        "mcp_url": result["mcp_url"],
        "zip_base64": result["zip_base64"],
        "zip_filename": result["zip_filename"],
        "zip_size_bytes": result["zip_size_bytes"],
        "elapsed_s": result.get("elapsed_s"),
        "dry_run": result.get("dry_run", False),
    }
    return JSONResponse(safe_result, status_code=200)


async def _destroy_handler(req: Request):
    """POST /destroy — tear down a previously provisioned stack.

    Body: {api_key, slug | vat | client_id}. Same auth surface as
    /provision (per-tenant API key issued via `provision_issue_api_key`).
    Idempotent: destroying an already-destroyed stack is a no-op.
    """
    started = time.time()
    client_ip = req.client.host if req.client else "?"

    try:
        body = await req.json()
    except Exception:
        _audit("DESTROY_REJECTED", reason="bad_json", ip=client_ip)
        return _err("invalid_json", 400)

    api_key = (body.get("api_key") or "").strip()
    slug = (body.get("slug") or body.get("tenant_slug") or "").strip()
    vat = (body.get("vat") or body.get("company_vat") or "").strip()
    client_id = (body.get("client_id") or "").strip()

    if not api_key:
        _audit("DESTROY_REJECTED", reason="missing_api_key", ip=client_ip)
        return _err("api_key required", 401)

    key_record = api_key_manager.verify(api_key)
    if not key_record:
        _audit("DESTROY_REJECTED", reason="invalid_api_key", ip=client_ip,
               key_prefix=api_key[:12] + "…")
        return _err("invalid_api_key", 401)

    if not (slug or vat or client_id):
        _audit("DESTROY_REJECTED", reason="no_identifier", ip=client_ip,
               key_id=key_record["key_id"])
        return _err("provide slug, vat, or client_id", 400)

    _audit("DESTROY_STARTED", ip=client_ip, key_id=key_record["key_id"],
           slug=slug, vat=vat, client_id=client_id)

    try:
        result = provisioning_engine.destroy(
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
        # not_found should be 404, others 500.
        status = 404 if result["error"] == "not_found" else 500
        return _err(result["error"], status,
                    **{k: v for k, v in result.items() if k != "error"})

    _audit(
        "DESTROY_COMPLETED" if result.get("status") == "destroyed" else "DESTROY_NOOP",
        ip=client_ip, key_id=key_record["key_id"],
        slug=result["slug"], client_id=result.get("client_id"),
        elapsed_ms=elapsed_ms,
        dry_run=result.get("dry_run", False),
    )

    safe_result = {
        "status": result["status"],
        "slug": result["slug"],
        "client_id": result.get("client_id"),
        "hostname": result.get("hostname"),
        "elapsed_s": result.get("elapsed_s"),
        "dry_run": result.get("dry_run", False),
    }
    return JSONResponse(safe_result, status_code=200)


def get_routes() -> list:
    """Returns Starlette routes to mount at server top-level (not under /admin)."""
    return [
        Route("/provision", _provision_handler, methods=["POST"]),
        Route("/destroy", _destroy_handler, methods=["POST"]),
    ]
