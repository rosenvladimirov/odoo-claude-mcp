"""
v3 Active Tenant Router.

Filters the proxy tool catalogue so only `main__*` tools (always-on) plus the
currently-active tenant's tools are exposed to the MCP client. Reduces token
cost from ~1028 (122 main + 6×151 clients) to ~278 (122 main + 1×151 + 4 control).

Persistence: the ACTIVE-TENANT SELECTION is PER-SESSION — stored in the
session_store `session_state` table (namespace 'tenant', key 'active') via
callbacks wired from server.py. One session's tenant_use() therefore never
changes any other session's active tenant (closes the cross-tenant takeover).
The discovered tool snapshots (_cache/_health) stay process-global and
tenant-keyed — shared discovery is correct and preserves the token-cost win.
Concurrency: threading.RLock around _cache/_health mutation.
Discovery: lazy — tenant tools are discovered on first `tenant_use(name)` and
cached until `tenant_refresh(name)`.

Architecture: this module owns NO knowledge of MCP transport or proxy services
internals. It receives callbacks from server.py for discovery + tool listing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from mcp.types import Tool

logger = logging.getLogger("tenant_router")

ACTIVE_TENANT_FILE = Path(os.environ.get("ACTIVE_TENANT_FILE", "/data/active_tenant.json"))
ALWAYS_ON: set[str] = set(
    s.strip() for s in os.environ.get("TENANT_ALWAYS_ON", "main").split(",") if s.strip()
)

_lock = threading.RLock()
_cache: dict[str, list[Tool]] = {}            # tenant name -> Tool defs (snapshot)
_health: dict[str, dict] = {}                 # tenant name -> {healthy, tool_count, last_refresh, error}

_TENANT_NS, _TENANT_KEY = "tenant", "active"

# Callbacks — wired by server.py at startup so we don't import circularly.
_get_proxy_services: Callable[[], dict] | None = None
_discover_one: Callable[[str], list[Tool]] | None = None
# Per-session selection store callbacks: (session_key, ns, key) -> value / set.
_get_session_state: Callable[[str, str, str], dict | None] | None = None
_set_session_state: Callable[[str, str, str, object], None] | None = None


# ─── Wiring ────────────────────────────────────────────────────────────────

def wire(*, get_proxy_services: Callable[[], dict],
         discover_one: Callable[[str], list[Tool]],
         get_session_state: Callable[[str, str, str], dict | None] | None = None,
         set_session_state: Callable[[str, str, str, object], None] | None = None) -> None:
    """Inject server.py dependencies. Call once at startup."""
    global _get_proxy_services, _discover_one
    global _get_session_state, _set_session_state
    _get_proxy_services = get_proxy_services
    _discover_one = discover_one
    _get_session_state = get_session_state
    _set_session_state = set_session_state


# ─── Per-session selection (session_state table) ─────────────────────────

def _read_active(session_key: str | None) -> str | None:
    """Active tenant for THIS session. None when no session ctx (fail-safe)."""
    if session_key is None or _get_session_state is None:
        return None
    try:
        val = _get_session_state(session_key, _TENANT_NS, _TENANT_KEY)
    except Exception:
        return None
    return (val or {}).get("name") or None


def _write_active(session_key: str, name: str | None) -> None:
    if _set_session_state is None:
        return
    _set_session_state(session_key, _TENANT_NS, _TENANT_KEY, {"name": name})


# ─── Public API ────────────────────────────────────────────────────────────

def always_on() -> set[str]:
    return set(ALWAYS_ON)


def get_active_tenant(session_key: str | None = None) -> str | None:
    return _read_active(session_key)


def active_tools(session_key: str | None = None) -> list[Tool]:
    """Return cached Tool defs of THIS session's active tenant, or []."""
    name = _read_active(session_key)
    if not name or name in ALWAYS_ON:
        return []
    with _lock:
        return list(_cache.get(name, []))


def list_tenants_with_health(session_key: str | None = None) -> list[dict]:
    """Snapshot of every configured tenant's status."""
    if _get_proxy_services is None:
        return []
    services = _get_proxy_services()
    active = _read_active(session_key)
    out = []
    for name, cfg in services.items():
        h = _health.get(name, {})
        out.append({
            "name": name,
            "url": cfg.get("url", ""),
            "transport": cfg.get("transport", ""),
            "always_on": name in ALWAYS_ON,
            "active": name == active,
            "healthy": h.get("healthy"),
            "tool_count": h.get("tool_count"),
            "last_refresh": h.get("last_refresh"),
            "error": h.get("error"),
            "cached": name in _cache,
        })
    return out


def refresh_tenant_tools(name: str) -> dict:
    """Re-discover tools for a single tenant. Updates cache + health."""
    if _discover_one is None or _get_proxy_services is None:
        return {"error": "tenant_router not wired"}
    services = _get_proxy_services()
    if name not in services:
        return {"error": f"unknown tenant: {name}", "available": list(services.keys())}
    try:
        tools = _discover_one(name)
        with _lock:
            _cache[name] = list(tools or [])
            _health[name] = {
                "healthy": bool(tools),
                "tool_count": len(tools or []),
                "last_refresh": int(time.time()),
                "error": None if tools else "discovery returned no tools",
            }
        return {"name": name, "tool_count": len(tools or []), "healthy": bool(tools)}
    except Exception as e:
        with _lock:
            _health[name] = {
                "healthy": False,
                "tool_count": 0,
                "last_refresh": int(time.time()),
                "error": f"{type(e).__name__}: {e}",
            }
        return {"name": name, "error": str(e), "healthy": False}


async def set_active_tenant(name: str, session_key: str, mcp_server=None) -> dict:
    """Validate, persist (per session), warm cache, emit list_changed."""
    if _get_proxy_services is None:
        return {"error": "tenant_router not wired"}
    if not session_key:
        return {"error": "no MCP session context; cannot set active tenant"}
    services = _get_proxy_services()
    if name not in services:
        return {
            "error": f"unknown tenant: {name}",
            "available": [n for n in services if n not in ALWAYS_ON],
        }
    if name in ALWAYS_ON:
        return {"error": f"'{name}' is always-on (cannot be the active tenant)"}

    # Discover if not cached.
    with _lock:
        cached = name in _cache and _health.get(name, {}).get("healthy")
    if not cached:
        result = refresh_tenant_tools(name)
        if not result.get("healthy"):
            return {"error": f"discovery failed for '{name}'", "detail": result}

    previous = _read_active(session_key)
    _write_active(session_key, name)
    logger.info("tenant_router: session=%s active tenant %s -> %s",
                session_key, previous, name)

    # Notify connected MCP client(s) so they re-fetch tools/list.
    notified = False
    if mcp_server is not None:
        try:
            session = mcp_server.request_context.session
            await session.send_tool_list_changed()
            notified = True
        except Exception as e:
            logger.warning("tenant_router: send_tool_list_changed failed: %s", e)

    return {
        "active": name,
        "previous": previous,
        "tool_count": len(_cache.get(name, [])),
        "notified": notified,
    }


async def clear_active_tenant(session_key: str, mcp_server=None) -> dict:
    if not session_key:
        return {"error": "no MCP session context"}
    previous = _read_active(session_key)
    _write_active(session_key, None)
    if mcp_server is not None:
        try:
            await mcp_server.request_context.session.send_tool_list_changed()
        except Exception:
            pass
    return {"active": None, "previous": previous}


# ─── Control plane Tool definitions ────────────────────────────────────────

def get_control_tools() -> list[Tool]:
    return [
        Tool(
            name="tenant_list",
            description=(
                "List all configured remote v2.x tenants with health/cache status. "
                "Always-on tenants (e.g. 'main') are exposed unconditionally; the "
                "rest are gated behind tenant_use."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="tenant_use",
            description=(
                "Set the active tenant. Discovers and caches its tools (if not "
                "cached), persists state to /data/active_tenant.json, and emits "
                "notifications/tools/list_changed so the client re-fetches the "
                "filtered tool list. Use 'null' or empty to clear."
            ),
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        Tool(
            name="tenant_current",
            description="Return the currently active tenant (or null) and its tool count.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="tenant_refresh",
            description=(
                "Re-discover tools for a single tenant (or the active one if name "
                "omitted). Invalidates and rebuilds its cache."
            ),
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        ),
    ]


CONTROL_TOOL_NAMES = {"tenant_list", "tenant_use", "tenant_current", "tenant_refresh"}


async def handle(name: str, arguments: dict, session_key: str | None = None,
                 mcp_server=None) -> dict:
    """Central dispatcher for the 4 control plane tools (per-session selection)."""
    arguments = arguments or {}
    if name == "tenant_list":
        return {"tenants": list_tenants_with_health(session_key),
                "always_on": sorted(ALWAYS_ON)}
    if name == "tenant_use":
        target = arguments.get("name") or ""
        if not target or target.lower() in ("null", "none", ""):
            return await clear_active_tenant(session_key, mcp_server)
        return await set_active_tenant(target, session_key, mcp_server)
    if name == "tenant_current":
        active = get_active_tenant(session_key)
        return {
            "active": active,
            "tool_count": len(_cache.get(active, [])) if active else 0,
            "always_on": sorted(ALWAYS_ON),
        }
    if name == "tenant_refresh":
        target = arguments.get("name") or get_active_tenant(session_key)
        if not target:
            return {"error": "no active tenant and no name provided"}
        result = refresh_tenant_tools(target)
        # If we refreshed the active tenant, notify clients.
        if mcp_server is not None and target == get_active_tenant(session_key):
            try:
                await mcp_server.request_context.session.send_tool_list_changed()
                result["notified"] = True
            except Exception:
                result["notified"] = False
        return result
    return {"error": f"unknown control tool: {name}"}
