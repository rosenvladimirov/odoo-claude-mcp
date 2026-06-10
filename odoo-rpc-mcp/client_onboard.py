"""
v3 Client Onboarding Wizard (B.3) — new client = one command.

Orchestrates the existing primitives end-to-end so onboarding a tenant is a
single admin tool instead of a manual checklist:

  1. provision the stack (provisioning_engine.provision: compose → Portainer →
     Cloudflare DNS → health → AES config ZIP). Honors its own DRY_RUN.
  2. issue a TENANT-scoped API key (api_key_manager.issue, role=tenant,
     scope=[client_id]) so the client can call /provision-/destroy for itself.
  3. register the stack secrets in the secrets registry (secrets_registry) for
     future rotation.
  4. emit an onboarding MANIFEST + a paste-ready handoff anchor (markdown) for
     the client's Claude, plus tg-listener enroll instructions (the listener
     lives on the operator laptop — the wizard can only print the steps, not
     run them server-side).

SAFETY:
  * DRY_RUN ON by default (MCP_ONBOARD_DRY_RUN=1): plan only — no key/secret is
    written, provisioning runs in its own dry-run. Real runs need
    MCP_ONBOARD_DRY_RUN=0 or dry_run=false AND MCP_PROVISIONING_DRY_RUN=0.
  * Admin-principal gated in server.py (same bar as provision_/fleet_/secrets_).
  * Idempotent: re-running for an already-provisioned slug returns the existing
    stack info (provisioning_engine is idempotent by slug).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import provisioning_engine as pe
import api_key_manager
import secrets_registry

logger = logging.getLogger("client_onboard")

DRY_RUN = os.environ.get("MCP_ONBOARD_DRY_RUN", "1") == "1"
ONBOARD_DIR = Path(os.environ.get("MCP_ONBOARD_DIR", "/data/onboarding"))


def _handoff_markdown(client_id: str, mcp_url: str, slug: str) -> str:
    """Paste-ready handoff anchor for the client's Claude / operator."""
    return f"""# Onboarding — client {slug} ({client_id})

## Stack
- MCP URL: {mcp_url}
- Stack name: mcp-client-{client_id}
- Container: odoo-rpc-mcp-{client_id}

## For the client's Claude (MEMORY/CLAUDE.md)
- Connect via unified-auth: the tenant API key was issued during onboarding
  (delivered in the AES config ZIP — never paste it in plaintext).
- `identify(name=...)` first; the tenant key scopes you to client_id={client_id}.

## tg-listener enrollment (run on the operator laptop, NOT server-side)
- `cd ~/odoo-claude-connections/tg_listener`
- `docker exec tg-listener python -m app.tgctl session add {slug} \\
      --api-id <id> --api-hash <hash>`   # from telegram_config.json
- `docker exec tg-listener python -m app.tgctl sub add {slug} <chat_id>`
- `docker exec tg-listener python -m app.tgctl session login {slug} \\
      --phone +359...`  → code from Rosen → `... session code {slug} <code>`
- Monitor: `tail -n0 -F ~/odoo-claude-connections/tg_listener/inbox/{slug}.ndjson`

## Secrets
- stack_token / admin_token are registered in the secrets registry for rotation
  (`secrets_rotate target_stack=mcp-client-{client_id} kind=stack_token`).
"""


def onboard(slug_or_vat: str, email: str, password: str = "",
            chat_id: str = "", anthropic_key: str = "",
            dry_run: bool | None = None) -> dict:
    """Provision + tenant key + secrets + handoff for a new client."""
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    if not slug_or_vat:
        return {"error": "slug_or_vat is required"}
    if not email or "@" not in email:
        return {"error": "valid email is required"}

    steps: dict = {}

    # ── 1. Provision the stack (idempotent; honors its own DRY_RUN) ──
    pw = password or pe.generate_secret_token(12)
    prov = pe.provision(slug_hint=slug_or_vat, password=pw, email=email,
                        anthropic_key=anthropic_key, vat=slug_or_vat)
    steps["provision"] = prov
    if prov.get("error"):
        return {"error": "provision_failed", "detail": prov}
    client_id = prov.get("client_id")
    mcp_url = prov.get("mcp_url", "")
    slug = prov.get("slug", slug_or_vat)
    stack_name = f"mcp-client-{client_id}" if client_id else None

    if is_dry:
        plan = {
            "slug": slug, "client_id": client_id, "mcp_url": mcp_url,
            "would_issue_tenant_key": {"role": "tenant",
                                       "scope": [client_id] if client_id else []},
            "would_register_secrets": ["stack_token", "admin_token"],
            "handoff_preview": _handoff_markdown(client_id or "<id>",
                                                 mcp_url, slug),
        }
        return {"ok": True, "dry_run": True, "plan": plan,
                "provision": prov,
                "hint": "Set MCP_ONBOARD_DRY_RUN=0 AND MCP_PROVISIONING_DRY_RUN=0 "
                        "(or dry_run=false) to apply."}

    # ── 2. Tenant-scoped API key ──
    key = api_key_manager.issue(email=email, role="tenant",
                                scope=[client_id] if client_id else [])
    steps["tenant_key"] = {"key_id": key.get("key_id"), "error": key.get("error")}
    tenant_key_plaintext = key.get("api_key")  # returned once

    # ── 3. Register stack secrets for rotation ──
    sec = {}
    if stack_name:
        for kind, val in (("stack_token", prov.get("secret_token")),
                          ("admin_token", prov.get("admin_token"))):
            if val:
                r = secrets_registry.register(stack_name, kind, value=val)
                sec[kind] = r.get("secret_id") or r.get("error")
    steps["secrets"] = sec

    # ── 4. Handoff doc ──
    handoff = _handoff_markdown(client_id or "", mcp_url, slug)
    handoff_path = None
    try:
        ONBOARD_DIR.mkdir(parents=True, exist_ok=True)
        p = ONBOARD_DIR / f"{slug}.handoff.md"
        p.write_text(handoff, encoding="utf-8")
        os.chmod(p, 0o600)
        handoff_path = str(p)
    except Exception as e:
        steps["handoff_write_error"] = str(e)

    return {
        "ok": True,
        "slug": slug,
        "client_id": client_id,
        "mcp_url": mcp_url,
        "stack_name": stack_name,
        "tenant_key_issued": bool(key.get("key_id")),
        "tenant_key": tenant_key_plaintext,  # ONCE — deliver via the ZIP, not chat
        "secrets_registered": sec,
        "handoff_path": handoff_path,
        "handoff": handoff,
        "config_zip": prov.get("zip_b64") and "<in provision result>",
        "ts": int(time.time()),
        "steps": steps,
    }


def onboard_status(slug_or_vat: str) -> dict:
    """Read-only: provisioning state + registered secrets for a slug."""
    from provisioning_engine import normalize_vat, normalize_slug
    slug = normalize_vat(slug_or_vat) or normalize_slug(slug_or_vat)
    state = pe.get_state(slug)
    stack_name = None
    secrets = {}
    if state and state.get("client_id"):
        stack_name = f"mcp-client-{state['client_id']}"
        secrets = secrets_registry.list_secrets(stack_name)
    return {"slug": slug, "provisioned": bool(state),
            "state": {k: state.get(k) for k in ("client_id", "mcp_url", "status")}
            if state else None,
            "stack_name": stack_name, "secrets": secrets}


# ─── Registration triple ───────────────────────────────────────────────────

def get_admin_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(
            name="client_onboard",
            description=("ADMIN: onboard a new client in one command — provision "
                         "the stack, issue a tenant-scoped API key, register stack "
                         "secrets for rotation, and emit a handoff doc + tg-listener "
                         "enroll steps. DRY-RUN by default (plan only) unless "
                         "MCP_ONBOARD_DRY_RUN=0 / dry_run=false. Idempotent by slug."),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug_or_vat": {"type": "string",
                                    "description": "VAT (preferred) or slug"},
                    "email": {"type": "string"},
                    "password": {"type": "string",
                                 "description": "ZIP password; auto-generated if omitted"},
                    "chat_id": {"type": "string",
                                "description": "Telegram chat for the handoff doc"},
                    "anthropic_key": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["slug_or_vat", "email"],
            },
        ),
        Tool(
            name="client_onboard_status",
            description=("ADMIN: read-only onboarding state for a slug/VAT "
                         "(provisioning state + registered secrets)."),
            inputSchema={
                "type": "object",
                "properties": {"slug_or_vat": {"type": "string"}},
                "required": ["slug_or_vat"],
            },
        ),
    ]


ADMIN_TOOL_NAMES = {"client_onboard", "client_onboard_status"}


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "client_onboard":
        return onboard(arguments.get("slug_or_vat", ""),
                       arguments.get("email", ""),
                       arguments.get("password", ""),
                       arguments.get("chat_id", ""),
                       arguments.get("anthropic_key", ""),
                       arguments.get("dry_run"))
    if name == "client_onboard_status":
        return onboard_status(arguments.get("slug_or_vat", ""))
    return {"error": f"unknown onboard tool: {name}"}
