"""MCP Cloudflare plugin — Tunnel + DNS management.

Exposes stdio MCP tools wrapping Cloudflare API v4. Wrapped by supergateway
into SSE for consumption by mcp-odoo-rpc (main stack).

Env:
  CF_API_TOKEN       required — token with Account:Cloudflare Tunnel:Edit +
                     Zone:DNS:Edit + Zone:Read on target zones
  CF_ACCOUNT_ID      optional default — can be overridden per call
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

API = "https://api.cloudflare.com/client/v4"
TOKEN = os.environ.get("CF_API_TOKEN", "")
DEFAULT_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")

mcp = FastMCP("cloudflare")


def _headers() -> dict[str, str]:
    if not TOKEN:
        raise RuntimeError("CF_API_TOKEN is not set")
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _account(acct: Optional[str]) -> str:
    a = acct or DEFAULT_ACCOUNT
    if not a:
        raise ValueError("account_id required (no CF_ACCOUNT_ID default)")
    return a


async def _req(method: str, path: str, json: Any = None) -> Any:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.request(method, f"{API}{path}", headers=_headers(), json=json)
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            return {"raw": r.text}
        if not data.get("success", False):
            return {"error": data.get("errors", []), "http_status": r.status_code}
        return data.get("result")


# ---------- Zones ----------
@mcp.tool()
async def cf_zones_list(name: Optional[str] = None) -> Any:
    """List zones (optional filter by name)."""
    q = f"?name={name}" if name else ""
    return await _req("GET", f"/zones{q}")


# ---------- DNS ----------
@mcp.tool()
async def cf_dns_list(zone_id: str, name: Optional[str] = None,
                      type: Optional[str] = None) -> Any:
    """List DNS records in a zone."""
    params = []
    if name:
        params.append(f"name={name}")
    if type:
        params.append(f"type={type}")
    q = ("?" + "&".join(params)) if params else ""
    return await _req("GET", f"/zones/{zone_id}/dns_records{q}")


@mcp.tool()
async def cf_dns_create(zone_id: str, type: str, name: str, content: str,
                        proxied: bool = True, ttl: int = 1,
                        comment: Optional[str] = None) -> Any:
    """Create DNS record. ttl=1 means Auto."""
    body = {"type": type, "name": name, "content": content,
            "proxied": proxied, "ttl": ttl}
    if comment:
        body["comment"] = comment
    return await _req("POST", f"/zones/{zone_id}/dns_records", body)


@mcp.tool()
async def cf_dns_update(zone_id: str, record_id: str, **fields: Any) -> Any:
    """Patch a DNS record. Pass any of: type, name, content, proxied, ttl."""
    return await _req("PATCH", f"/zones/{zone_id}/dns_records/{record_id}", fields)


@mcp.tool()
async def cf_dns_delete(zone_id: str, record_id: str) -> Any:
    """Delete a DNS record."""
    return await _req("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")


# ---------- Tunnels ----------
@mcp.tool()
async def cf_tunnel_list(account_id: Optional[str] = None,
                         include_deleted: bool = False) -> Any:
    """List cloudflared tunnels in an account."""
    a = _account(account_id)
    q = "?is_deleted=false" if not include_deleted else ""
    return await _req("GET", f"/accounts/{a}/cfd_tunnel{q}")


@mcp.tool()
async def cf_tunnel_config_get(tunnel_id: str,
                               account_id: Optional[str] = None) -> Any:
    """Get the current ingress/config of a tunnel."""
    a = _account(account_id)
    return await _req("GET", f"/accounts/{a}/cfd_tunnel/{tunnel_id}/configurations")


@mcp.tool()
async def cf_tunnel_config_put(tunnel_id: str, ingress: list[dict],
                               account_id: Optional[str] = None,
                               warp_routing: bool = False) -> Any:
    """Replace the full ingress config. Last entry must be {'service': 'http_status:404'}.
    Each route: {'hostname': 'foo.example.com', 'service': 'http://svc:8080'}.
    """
    a = _account(account_id)
    body = {"config": {"ingress": ingress, "warp-routing": {"enabled": warp_routing}}}
    return await _req("PUT", f"/accounts/{a}/cfd_tunnel/{tunnel_id}/configurations", body)


@mcp.tool()
async def cf_tunnel_route_add(tunnel_id: str, zone_id: str, hostname: str,
                              service: str, account_id: Optional[str] = None,
                              create_dns: bool = True,
                              comment: Optional[str] = None) -> Any:
    """Add (or replace) one ingress rule + create CNAME record.
    hostname: FQDN e.g. mcp-208609891.mcpworks.net
    service:  e.g. http://odoo-rpc-mcp-208609891:8098
    """
    a = _account(account_id)
    cfg = await _req("GET", f"/accounts/{a}/cfd_tunnel/{tunnel_id}/configurations")
    if isinstance(cfg, dict) and cfg.get("error"):
        return cfg
    ingress = cfg.get("config", {}).get("ingress", [])
    ingress = [r for r in ingress if r.get("hostname") != hostname]
    catch_all = [r for r in ingress if not r.get("hostname")]
    specific = [r for r in ingress if r.get("hostname")]
    specific.append({"hostname": hostname, "service": service})
    new_ingress = specific + (catch_all or [{"service": "http_status:404"}])

    put = await _req("PUT",
                     f"/accounts/{a}/cfd_tunnel/{tunnel_id}/configurations",
                     {"config": {"ingress": new_ingress,
                                 "warp-routing": cfg.get("config", {}).get("warp-routing", {"enabled": False})}})

    dns = None
    if create_dns:
        short = hostname.split(f".{(await _zone_name(zone_id)) or ''}")[0]
        body = {"type": "CNAME", "name": short,
                "content": f"{tunnel_id}.cfargotunnel.com",
                "proxied": True, "ttl": 1}
        if comment:
            body["comment"] = comment
        dns = await _req("POST", f"/zones/{zone_id}/dns_records", body)
    return {"config": put, "dns": dns}


@mcp.tool()
async def cf_tunnel_route_remove(tunnel_id: str, zone_id: str, hostname: str,
                                 account_id: Optional[str] = None,
                                 delete_dns: bool = True) -> Any:
    """Remove ingress rule by hostname + delete its CNAME record."""
    a = _account(account_id)
    cfg = await _req("GET", f"/accounts/{a}/cfd_tunnel/{tunnel_id}/configurations")
    if isinstance(cfg, dict) and cfg.get("error"):
        return cfg
    ingress = cfg.get("config", {}).get("ingress", [])
    ingress = [r for r in ingress if r.get("hostname") != hostname]
    if not any(not r.get("hostname") for r in ingress):
        ingress.append({"service": "http_status:404"})

    put = await _req("PUT",
                     f"/accounts/{a}/cfd_tunnel/{tunnel_id}/configurations",
                     {"config": {"ingress": ingress,
                                 "warp-routing": cfg.get("config", {}).get("warp-routing", {"enabled": False})}})

    dns_del = None
    if delete_dns:
        recs = await _req("GET", f"/zones/{zone_id}/dns_records?name={hostname}")
        if isinstance(recs, list):
            for r in recs:
                if r.get("type") == "CNAME":
                    await _req("DELETE", f"/zones/{zone_id}/dns_records/{r['id']}")
                    dns_del = r["id"]
                    break
    return {"config": put, "dns_deleted": dns_del}


async def _zone_name(zone_id: str) -> Optional[str]:
    res = await _req("GET", f"/zones/{zone_id}")
    if isinstance(res, dict):
        return res.get("name")
    return None


if __name__ == "__main__":
    mcp.run(transport="stdio")
