"""
Cloudflare DNS automation for v3 client provisioning.

Creates a DNS A record for mcp-<CLIENT_ID>.<DOMAIN_BASE> pointing at the
shared Cloudflare tunnel that fronts poligroup. The tunnel ingress is
ALSO updated so the new hostname routes to the new container.

Env:
  MCP_CLOUDFLARE_API_TOKEN=...   — scoped: Zone:DNS:Edit + Account:Cloudflare Tunnel:Edit
  MCP_CLOUDFLARE_ACCOUNT_ID=...  — Cloudflare account UUID
  MCP_CLOUDFLARE_ZONE_ID=...     — zone for mcpworks.net
  MCP_CLOUDFLARE_TUNNEL_ID=...   — existing tunnel id (the one fronting poligroup)
  MCP_CLOUDFLARE_TUNNEL_TARGET=... — internal http://host:port that the tunnel forwards to
                                     (e.g. http://odoo-rpc-mcp-<CLIENT_ID>:8094)

All calls are best-effort and return {ok: bool, ...detail}. Caller is
responsible for cleanup on failure.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("cloudflare_provisioning")

CF_API = "https://api.cloudflare.com/client/v4"


def _env() -> dict:
    return {
        "token": os.environ.get("MCP_CLOUDFLARE_API_TOKEN", "").strip(),
        "account": os.environ.get("MCP_CLOUDFLARE_ACCOUNT_ID", "").strip(),
        "zone": os.environ.get("MCP_CLOUDFLARE_ZONE_ID", "").strip(),
        "tunnel": os.environ.get("MCP_CLOUDFLARE_TUNNEL_ID", "").strip(),
    }


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def is_configured() -> bool:
    cfg = _env()
    return all([cfg["token"], cfg["zone"], cfg["tunnel"], cfg["account"]])


def create_dns_record(hostname: str, content: str = None) -> dict:
    """Create CNAME pointing to the tunnel. content defaults to <tunnel_id>.cfargotunnel.com."""
    cfg = _env()
    if not is_configured():
        return {"ok": False, "error": "cloudflare_not_configured",
                "hint": "set MCP_CLOUDFLARE_API_TOKEN/ACCOUNT_ID/ZONE_ID/TUNNEL_ID"}

    if content is None:
        content = f"{cfg['tunnel']}.cfargotunnel.com"

    import httpx
    payload = {
        "type": "CNAME",
        "name": hostname,
        "content": content,
        "proxied": True,
        "comment": f"v3 provisioning — auto-created for {hostname}",
    }
    url = f"{CF_API}/zones/{cfg['zone']}/dns_records"
    try:
        r = httpx.post(url, headers=_headers(cfg["token"]), json=payload, timeout=15.0)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": "request_failed", "detail": str(e)}
    if not data.get("success"):
        # Already exists? The API returns errors[0].code == 81057 для duplicate.
        errors = data.get("errors", [])
        already = any(e.get("code") == 81057 for e in errors)
        if already:
            return {"ok": True, "already_exists": True, "errors": errors}
        return {"ok": False, "error": "dns_create_failed", "errors": errors}
    return {"ok": True, "record_id": data.get("result", {}).get("id"),
            "hostname": hostname}


def delete_dns_record(record_id: str) -> dict:
    cfg = _env()
    if not is_configured() or not record_id:
        return {"ok": False, "error": "missing_config_or_id"}
    import httpx
    url = f"{CF_API}/zones/{cfg['zone']}/dns_records/{record_id}"
    try:
        r = httpx.delete(url, headers=_headers(cfg["token"]), timeout=15.0)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": "request_failed", "detail": str(e)}
    return {"ok": data.get("success", False), "data": data}


def get_tunnel_config() -> dict:
    """Returns current tunnel ingress configuration (for inspection)."""
    cfg = _env()
    if not is_configured():
        return {"ok": False, "error": "cloudflare_not_configured"}
    import httpx
    url = f"{CF_API}/accounts/{cfg['account']}/cfd_tunnel/{cfg['tunnel']}/configurations"
    try:
        r = httpx.get(url, headers=_headers(cfg["token"]), timeout=15.0)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": "request_failed", "detail": str(e)}
    if not data.get("success"):
        return {"ok": False, "errors": data.get("errors")}
    return {"ok": True, "config": data.get("result", {}).get("config", {})}


def add_tunnel_ingress(hostname: str, service: str) -> dict:
    """Append a new ingress rule to the existing tunnel.

    service is the internal target — typically "http://odoo-rpc-mcp-<CLIENT_ID>:8094"
    when the tunnel runs on the same Docker network as the new stack.
    """
    cfg = _env()
    if not is_configured():
        return {"ok": False, "error": "cloudflare_not_configured"}

    current = get_tunnel_config()
    if not current.get("ok"):
        return {"ok": False, "error": "fetch_tunnel_config_failed",
                "detail": current}

    config = current.get("config") or {}
    ingress = list(config.get("ingress") or [])

    # The catch-all (service: http_status:404) MUST stay last.
    catch_all = None
    for i, rule in enumerate(ingress):
        if rule.get("hostname") is None and rule.get("service", "").startswith("http_status:"):
            catch_all = ingress.pop(i)
            break

    # Don't add a duplicate.
    for rule in ingress:
        if rule.get("hostname") == hostname:
            if catch_all:
                ingress.append(catch_all)
            return {"ok": True, "already_exists": True,
                    "ingress_count": len(ingress)}

    ingress.append({"hostname": hostname, "service": service})
    if catch_all:
        ingress.append(catch_all)
    else:
        ingress.append({"service": "http_status:404"})

    new_config = dict(config)
    new_config["ingress"] = ingress

    import httpx
    url = f"{CF_API}/accounts/{cfg['account']}/cfd_tunnel/{cfg['tunnel']}/configurations"
    try:
        r = httpx.put(url, headers=_headers(cfg["token"]),
                      json={"config": new_config}, timeout=15.0)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": "put_failed", "detail": str(e)}
    if not data.get("success"):
        return {"ok": False, "errors": data.get("errors")}
    return {"ok": True, "ingress_count": len(ingress)}


def remove_tunnel_ingress(hostname: str) -> dict:
    """Remove an ingress rule by hostname."""
    cfg = _env()
    if not is_configured():
        return {"ok": False, "error": "cloudflare_not_configured"}

    current = get_tunnel_config()
    if not current.get("ok"):
        return {"ok": False, "error": "fetch_tunnel_config_failed",
                "detail": current}

    config = current.get("config") or {}
    ingress = list(config.get("ingress") or [])
    new_ingress = [r for r in ingress if r.get("hostname") != hostname]
    if len(new_ingress) == len(ingress):
        return {"ok": True, "removed": False, "reason": "not_found"}

    new_config = dict(config)
    new_config["ingress"] = new_ingress

    import httpx
    url = f"{CF_API}/accounts/{cfg['account']}/cfd_tunnel/{cfg['tunnel']}/configurations"
    try:
        r = httpx.put(url, headers=_headers(cfg["token"]),
                      json={"config": new_config}, timeout=15.0)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": "put_failed", "detail": str(e)}
    return {"ok": data.get("success", False),
            "removed": True, "ingress_count": len(new_ingress)}
