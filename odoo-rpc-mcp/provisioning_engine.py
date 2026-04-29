"""
v3 client provisioning engine.

Orchestrates (Phase 2):
  1. Generate client_id + secrets
  2. Substitute compose template
  3. [DRY_RUN: write to /tmp/preview | REAL: Portainer create stack]
  4. [Cloudflare: create CNAME for mcp-<id>.<domain> + add tunnel ingress]
  5. Wait for /health (exponential backoff up to timeout)
  6. Generate AES-encrypted ZIP с tenant config
  7. Persist multi-stage state for idempotent resume

Idempotency by tenant_slug — state persisted в /data/provisioning_state.jsonl.
Re-running with same slug returns cached ZIP (re-encrypted с new password)
or resumes from the last completed stage.

Env vars:
  MCP_PROVISIONING_DRY_RUN=1            — skip real Portainer/Cloudflare (default 1 in alpha)
  MCP_PORTAINER_URL=https://...         — Portainer instance URL
  MCP_PORTAINER_API_KEY=...             — Portainer API key (X-API-Key)
  MCP_PORTAINER_ENDPOINT_ID=2           — Docker endpoint id
  MCP_PROVISIONING_STATE_FILE=/data/provisioning_state.jsonl
  MCP_DOMAIN_BASE=mcpworks.net          — used for mcp-<id>.<domain>
  MCP_CLOUDFLARE_API_TOKEN=...          — Cloudflare API token (Phase 2)
  MCP_CLOUDFLARE_ACCOUNT_ID=...
  MCP_CLOUDFLARE_ZONE_ID=...
  MCP_CLOUDFLARE_TUNNEL_ID=...
  MCP_PROVISIONING_HEALTH_TIMEOUT=180   — health probe timeout seconds (default 180)
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("provisioning_engine")

TEMPLATE_FILE = Path(__file__).parent / "client_stack.template.yml"
STATE_FILE = Path(os.environ.get(
    "MCP_PROVISIONING_STATE_FILE", "/data/provisioning_state.jsonl"))
DOMAIN_BASE = os.environ.get("MCP_DOMAIN_BASE", "mcpworks.net").strip()
DRY_RUN = os.environ.get("MCP_PROVISIONING_DRY_RUN", "1") == "1"


# ─── Helpers ───────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_VAT_RE = re.compile(r"[^a-z0-9]+")


def normalize_slug(value: str) -> str:
    """email/domain → safe slug (alphanumeric + hyphen, max 32 chars)."""
    s = (value or "").strip().lower()
    if "@" in s:
        s = s.split("@", 1)[0]
    s = _SLUG_RE.sub("-", s).strip("-")
    return s[:32] or "client"


def normalize_vat(vat: str) -> str:
    """VAT number → DNS/container-safe id.

    'BG 123 456 789' → 'bg123456789'
    'BG123456789'    → 'bg123456789'
    Country prefix preserved to disambiguate cross-border identical numbers.
    Returns "" if input has no usable alphanumerics.
    """
    s = (vat or "").strip().lower()
    s = _VAT_RE.sub("", s)
    return s[:32]


def generate_client_id(vat: str = "") -> str:
    """Derive client_id from normalized VAT, or random 9-digit fallback.

    VAT-derived ids are preferred — they're stable across re-provisioning
    and immediately tell the operator which tenant a stack belongs to.
    Random fallback only when VAT is empty / invalid.
    """
    if vat:
        normalized = normalize_vat(vat)
        if normalized and len(normalized) >= 4:
            return normalized
    return str(secrets.randbelow(900_000_000) + 100_000_000)


def generate_secret_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ─── Template rendering ────────────────────────────────────────────────────

def render_compose(client_id: str, secret_token: str,
                   admin_token: str, anthropic_key: str = "",
                   mcp_port: int = 8094) -> str:
    if not TEMPLATE_FILE.is_file():
        raise FileNotFoundError(f"template not found: {TEMPLATE_FILE}")
    raw = TEMPLATE_FILE.read_text(encoding="utf-8")
    return (
        raw
        .replace("{{CLIENT_ID}}", client_id)
        .replace("{{MCP_SECRET_TOKEN}}", secret_token)
        .replace("{{MCP_OAUTH_CLIENT_ID}}", f"odoo-rpc-mcp-{client_id}")
        .replace("{{MCP_ADMIN_TOKEN}}", admin_token)
        .replace("{{ANTHROPIC_API_KEY}}", anthropic_key)
        .replace("{{MCP_PORT}}", str(mcp_port))
    )


# ─── State persistence (idempotent retry) ─────────────────────────────────

def _state_replay() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not STATE_FILE.is_file():
        return out
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                slug = rec.get("slug")
                if slug:
                    out[slug] = rec
    except OSError:
        pass
    return out


def _state_append(record: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


def get_state(slug: str) -> dict | None:
    return _state_replay().get(slug)


# ─── Portainer call (real mode) ────────────────────────────────────────────

def portainer_create_stack(stack_name: str, compose: str) -> dict:
    """POST stack to Portainer. Returns stack info on success."""
    portainer_url = os.environ.get("MCP_PORTAINER_URL", "").rstrip("/")
    api_key = os.environ.get("MCP_PORTAINER_API_KEY", "")
    endpoint_id = os.environ.get("MCP_PORTAINER_ENDPOINT_ID", "2")
    if not portainer_url or not api_key:
        return {"error": "MCP_PORTAINER_URL or MCP_PORTAINER_API_KEY not set"}

    import httpx
    payload = {
        "name": stack_name,
        "stackFileContent": compose,
        "env": [],
    }
    url = f"{portainer_url}/api/stacks/create/standalone/string"
    params = {"endpointId": endpoint_id}
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    try:
        resp = httpx.post(url, params=params, json=payload,
                          headers=headers, timeout=60.0)
        if resp.status_code >= 300:
            return {"error": "portainer_create_failed",
                    "status": resp.status_code,
                    "body": resp.text[:500]}
        return {"ok": True, "stack": resp.json()}
    except Exception as e:
        return {"error": "portainer_request_failed", "detail": str(e)}


# ─── Portainer stack delete ────────────────────────────────────────────────

def portainer_delete_stack(stack_name: str, stack_id: int | None = None) -> dict:
    """DELETE a stack by id (preferred) or by name lookup.

    Portainer's stack DELETE endpoint requires a numeric stack id. If
    `stack_id` is missing (legacy state records), we list /api/stacks and
    locate the matching name. Returns `{"ok": True}` on success or when
    the stack is already absent (idempotent).
    """
    portainer_url = os.environ.get("MCP_PORTAINER_URL", "").rstrip("/")
    api_key = os.environ.get("MCP_PORTAINER_API_KEY", "")
    endpoint_id = os.environ.get("MCP_PORTAINER_ENDPOINT_ID", "2")
    if not portainer_url or not api_key:
        return {"error": "MCP_PORTAINER_URL or MCP_PORTAINER_API_KEY not set"}

    import httpx
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    if stack_id is None:
        try:
            r = httpx.get(f"{portainer_url}/api/stacks",
                          headers=headers, timeout=30.0)
            if r.status_code >= 300:
                return {"error": "portainer_list_failed",
                        "status": r.status_code, "body": r.text[:500]}
            for s in r.json() or []:
                if s.get("Name") == stack_name:
                    stack_id = s.get("Id")
                    break
        except Exception as e:
            return {"error": "portainer_list_request_failed", "detail": str(e)}
        if stack_id is None:
            return {"ok": True, "already_absent": True, "stack_name": stack_name}

    url = f"{portainer_url}/api/stacks/{stack_id}"
    params = {"endpointId": endpoint_id}
    try:
        r = httpx.delete(url, params=params, headers=headers, timeout=60.0)
        if r.status_code == 404:
            return {"ok": True, "already_absent": True, "stack_id": stack_id}
        if r.status_code >= 300:
            return {"error": "portainer_delete_failed",
                    "status": r.status_code, "body": r.text[:500]}
        return {"ok": True, "stack_id": stack_id}
    except Exception as e:
        return {"error": "portainer_delete_request_failed", "detail": str(e)}


# ─── Health probe (exponential backoff) ────────────────────────────────────

def wait_for_health(url: str, timeout: int | None = None) -> dict:
    """Poll /health until 200 or timeout. Backoff 2s → 4s → 8s → cap 16s."""
    if timeout is None:
        timeout = int(os.environ.get("MCP_PROVISIONING_HEALTH_TIMEOUT", "180"))
    import httpx
    started = time.time()
    last_err = ""
    delay = 2
    attempts = 0
    while time.time() - started < timeout:
        try:
            r = httpx.get(url, timeout=5.0)
            if r.status_code == 200:
                return {"healthy": True,
                        "elapsed_s": int(time.time() - started),
                        "attempts": attempts + 1}
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        attempts += 1
        time.sleep(delay)
        delay = min(delay * 2, 16)
    return {"healthy": False,
            "elapsed_s": int(time.time() - started),
            "attempts": attempts,
            "last_error": last_err}


# ─── ZIP generation (mirrors mcp_terminal_get_config format) ──────────────

def generate_config_zip(client_id: str, mcp_url: str, secret_token: str,
                        anthropic_key: str, password: str) -> dict:
    """Build a pyzipper AES-encrypted ZIP с tenant config; returns base64."""
    import pyzipper

    config = {
        "company": {
            "claude_mcp_url": mcp_url,
            "claude_mcp_token": secret_token,
            "claude_mcp_client_id": f"odoo-rpc-mcp-{client_id}",
            "claude_mcp_api_key": "",
            "claude_qdrant_url": "",
            "claude_qdrant_api_key": "",
            "claude_qdrant_collection_prefix": client_id,
            "claude_ollama_url": "",
            "claude_ollama_model": "llama3.2:latest",
            "claude_embedding_provider": "ollama",
            "claude_embedding_api_key": "",
            "claude_terminal_url": "",
        },
        "users": {
            "claude_api_key": anthropic_key or "",
        },
    }
    payload = json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")

    buf = io.BytesIO()
    with pyzipper.AESZipFile(buf, "w",
                             compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.writestr("config.json", payload)

    raw = buf.getvalue()
    return {
        "zip_base64": base64.b64encode(raw).decode("ascii"),
        "zip_size_bytes": len(raw),
        "zip_filename": f"claude_terminal_config_{client_id}.zip",
    }


# ─── Top-level orchestration ───────────────────────────────────────────────

def provision(slug_hint: str, password: str, email: str,
              anthropic_key: str = "", vat: str = "") -> dict:
    """Orchestrate: validate → idempotency check → render → deploy → ZIP.

    `vat` (e.g. "BG123456789") is preferred over `slug_hint`. When provided
    it becomes both the tenant slug AND the client_id, so:
      stack_name   = mcp-client-bg123456789
      container    = odoo-rpc-mcp-bg123456789
      hostname     = mcp-bg123456789.mcpworks.net
    """
    if not password or len(password) < 8:
        return {"error": "password must be at least 8 characters"}

    # VAT-first slug derivation. Falls back to slug_hint/email for legacy callers.
    if vat:
        slug = normalize_vat(vat)
        if not slug or len(slug) < 4:
            return {"error": "invalid VAT — must contain ≥4 alphanumeric chars",
                    "vat_received": vat}
    else:
        slug = normalize_slug(slug_hint or email)
        if not slug:
            return {"error": "invalid slug/email — provide vat or slug"}

    started = time.time()

    # Idempotency: if already provisioned, regen ZIP with new password.
    existing = get_state(slug)
    if existing and existing.get("status") == "completed":
        logger.info("provisioning: idempotent hit for slug=%s", slug)
        zip_info = generate_config_zip(
            existing["client_id"], existing["mcp_url"],
            existing["secret_token"], anthropic_key, password,
        )
        return {
            "status": "already_provisioned",
            "slug": slug,
            "client_id": existing["client_id"],
            "mcp_url": existing["mcp_url"],
            **zip_info,
        }

    # New provisioning. client_id = VAT-derived (consistent с slug) or random.
    client_id = generate_client_id(vat=vat)
    secret_token = generate_secret_token()
    admin_token = generate_secret_token()
    mcp_url = f"https://mcp-{client_id}.{DOMAIN_BASE}"

    compose = render_compose(client_id, secret_token, admin_token,
                             anthropic_key=anthropic_key)

    if DRY_RUN:
        # Save preview to /tmp for inspection.
        preview_dir = Path("/tmp/v3-provisioning-preview")
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / f"{slug}-{client_id}.yml").write_text(compose, encoding="utf-8")
        portainer_info = {"dry_run": True, "preview_path": str(preview_dir)}
        cf_info: dict = {"dry_run": True}
        health: dict = {"skipped": True, "dry_run": True}
    else:
        # 1) Portainer create stack
        portainer_info = portainer_create_stack(
            stack_name=f"mcp-client-{client_id}", compose=compose)
        if portainer_info.get("error"):
            _state_append({
                "slug": slug, "client_id": client_id, "status": "failed",
                "stage": "portainer", "error": portainer_info,
                "ts": int(time.time()), "email": email,
            })
            return {"error": "stack_create_failed", "detail": portainer_info}

        # 2) Cloudflare DNS + tunnel ingress
        try:
            import cloudflare_provisioning as cf
            cf_info = {}
            if cf.is_configured():
                hostname = f"mcp-{client_id}.{DOMAIN_BASE}"
                # CNAME first.
                dns = cf.create_dns_record(hostname)
                cf_info["dns"] = dns
                # Tunnel ingress (internal target = container by name on backend network).
                ingress = cf.add_tunnel_ingress(
                    hostname, f"http://odoo-rpc-mcp-{client_id}:8094")
                cf_info["ingress"] = ingress
                if not (dns.get("ok") and ingress.get("ok")):
                    _state_append({
                        "slug": slug, "client_id": client_id, "status": "failed",
                        "stage": "cloudflare", "cloudflare": cf_info,
                        "portainer": portainer_info,
                        "ts": int(time.time()), "email": email,
                    })
                    return {"error": "cloudflare_failed", "detail": cf_info}
            else:
                cf_info = {"skipped": True,
                           "reason": "MCP_CLOUDFLARE_* env not configured"}
                logger.warning(
                    "Cloudflare not configured — skipping DNS/tunnel for %s. "
                    "DNS must be configured manually before /health probe.",
                    client_id,
                )
        except ImportError as e:
            cf_info = {"skipped": True, "reason": f"cloudflare module: {e}"}

        # 3) /health probe (long timeout — DNS may take ~30s to propagate).
        health = wait_for_health(f"{mcp_url}/health")
        if not health.get("healthy"):
            _state_append({
                "slug": slug, "client_id": client_id, "status": "unhealthy",
                "stage": "health", "health": health, "portainer": portainer_info,
                "cloudflare": cf_info,
                "ts": int(time.time()), "email": email,
            })
            return {"error": "stack_unhealthy", "detail": health,
                    "cloudflare": cf_info}

    # Generate ZIP.
    zip_info = generate_config_zip(
        client_id, mcp_url, secret_token, anthropic_key, password)

    # Persist state.
    portainer_stack_id = None
    if isinstance(portainer_info, dict):
        stack_obj = portainer_info.get("stack") or {}
        if isinstance(stack_obj, dict):
            portainer_stack_id = stack_obj.get("Id")
    _state_append({
        "slug": slug,
        "client_id": client_id,
        "status": "completed",
        "stage": "completed",
        "mcp_url": mcp_url,
        "secret_token": secret_token,    # stored для idempotent re-encryption
        "admin_token": admin_token,
        "email": email,
        "ts": int(time.time()),
        "elapsed_s": int(time.time() - started),
        "dry_run": DRY_RUN,
        "portainer_stack_id": portainer_stack_id,
        "cloudflare": cf_info,
        "health": health,
    })

    return {
        "status": "completed",
        "slug": slug,
        "client_id": client_id,
        "mcp_url": mcp_url,
        "elapsed_s": int(time.time() - started),
        "dry_run": DRY_RUN,
        "portainer": portainer_info,
        "cloudflare": cf_info,
        "health": health,
        **zip_info,
    }


# ─── Destroy (tear down stack + DNS + tunnel ingress) ─────────────────────

def destroy(slug_hint: str = "", vat: str = "",
            client_id: str = "") -> dict:
    """Tear down a previously provisioned stack — Portainer + Cloudflare.

    Identification (precedence): explicit `client_id` → VAT-derived slug →
    free-form `slug_hint`. The state record is the source of truth для
    `stack_id` + `dns record_id` + `hostname`. Best-effort: Cloudflare
    cleanup failures are logged but do NOT abort the Portainer DELETE,
    because a half-removed Cloudflare record is preferable to a stuck
    container.

    Idempotent — re-running on an already destroyed slug is a no-op
    (returns `{"status": "already_destroyed"}`).
    """
    # Resolve slug (mirror provision()'s logic).
    if vat:
        slug = normalize_vat(vat)
        if not slug or len(slug) < 4:
            return {"error": "invalid VAT — must contain ≥4 alphanumeric chars",
                    "vat_received": vat}
    elif client_id:
        slug = normalize_vat(client_id) or normalize_slug(client_id)
    else:
        slug = normalize_slug(slug_hint)
        if not slug:
            return {"error": "provide slug_hint, vat, or client_id"}

    started = time.time()
    state = get_state(slug)
    if not state:
        return {"error": "not_found", "slug": slug}

    if state.get("status") == "destroyed":
        return {"status": "already_destroyed", "slug": slug,
                "destroyed_at": state.get("ts")}

    target_client_id = state.get("client_id") or client_id
    hostname = f"mcp-{target_client_id}.{DOMAIN_BASE}" if target_client_id else None
    stack_name = f"mcp-client-{target_client_id}" if target_client_id else None

    if DRY_RUN:
        cf_info: dict = {"dry_run": True}
        portainer_info: dict = {"dry_run": True, "stack_name": stack_name}
        _state_append({
            "slug": slug,
            "client_id": target_client_id,
            "status": "destroyed",
            "stage": "destroyed",
            "ts": int(time.time()),
            "elapsed_s": int(time.time() - started),
            "dry_run": True,
            "cloudflare": cf_info,
            "portainer": portainer_info,
        })
        return {
            "status": "destroyed",
            "slug": slug,
            "client_id": target_client_id,
            "hostname": hostname,
            "dry_run": True,
            "portainer": portainer_info,
            "cloudflare": cf_info,
        }

    # 1) Cloudflare — best-effort. Order: tunnel ingress first, then DNS,
    # so a half-removed state can't leave a hostname pointing at a tunnel
    # rule that no longer exists (which would 502 indefinitely).
    cf_info = {}
    try:
        import cloudflare_provisioning as cf
        if cf.is_configured() and hostname:
            cf_info["ingress"] = cf.remove_tunnel_ingress(hostname)
            prior_dns = (state.get("cloudflare") or {}).get("dns") or {}
            record_id = prior_dns.get("record_id")
            if record_id:
                cf_info["dns"] = cf.delete_dns_record(record_id)
            else:
                cf_info["dns"] = {"skipped": True,
                                  "reason": "no record_id in state"}
        else:
            cf_info = {"skipped": True,
                       "reason": "cloudflare_not_configured" if hostname else "no hostname"}
    except ImportError as e:
        cf_info = {"skipped": True, "reason": f"cloudflare module: {e}"}
    except Exception as e:
        logger.exception("cloudflare cleanup raised — continuing")
        cf_info = {"error": "cloudflare_cleanup_raised", "detail": str(e)}

    # 2) Portainer DELETE — by stack_id from state if present, else by name.
    portainer_info: dict = {}
    if stack_name:
        portainer_info = portainer_delete_stack(
            stack_name=stack_name,
            stack_id=state.get("portainer_stack_id"),
        )
    else:
        portainer_info = {"skipped": True, "reason": "no client_id in state"}

    # 3) Persist state — destroyed (или partial if Portainer failed).
    overall_ok = portainer_info.get("ok", False)
    _state_append({
        "slug": slug,
        "client_id": target_client_id,
        "status": "destroyed" if overall_ok else "destroy_failed",
        "stage": "destroyed" if overall_ok else "portainer",
        "ts": int(time.time()),
        "elapsed_s": int(time.time() - started),
        "dry_run": False,
        "cloudflare": cf_info,
        "portainer": portainer_info,
    })

    if not overall_ok:
        return {"error": "portainer_delete_failed",
                "slug": slug,
                "client_id": target_client_id,
                "portainer": portainer_info,
                "cloudflare": cf_info}

    return {
        "status": "destroyed",
        "slug": slug,
        "client_id": target_client_id,
        "hostname": hostname,
        "elapsed_s": int(time.time() - started),
        "portainer": portainer_info,
        "cloudflare": cf_info,
    }
