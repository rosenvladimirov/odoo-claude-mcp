"""
v3 Health Monitor (B.6) — scan every deployed stack, classify health, and emit
an alert list the operator (or the tg-listener bridge) can act on.

Reuses fleet_manager.fleet_list (inventory) + fleet_status (per-stack /health +
MCP discovery probe). Read-only — never mutates a stack. No DRY_RUN needed.

Classification per stack:
  * healthy  — /health ok AND MCP discovery returns tools
  * degraded — /health ok but MCP discovery empty/failed (serving HTTP, not MCP)
  * down     — /health unreachable/unhealthy
  * unknown  — no URL / not probeable

`health_scan` returns a report + an `alerts` list (degraded/down). Alerts are
structured so a caller can forward them to Telegram (the tg-listener lives on
the operator laptop; this module does not push — it surfaces).
"""

from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger("health_monitor")

_fleet = None  # fleet_manager module, wired at startup


def wire(*, fleet_manager=None) -> None:
    global _fleet
    _fleet = fleet_manager


def _classify(status: dict) -> str:
    health = status.get("health") or {}
    probe = status.get("mcp_probe") or {}
    healthy = health.get("healthy")
    if healthy is False:
        return "down"
    if healthy is True:
        if probe.get("healthy") is False:
            return "degraded"
        return "healthy"
    # No health info — fall back to MCP probe.
    if probe.get("healthy") is True:
        return "healthy"
    if probe.get("healthy") is False:
        return "degraded"
    return "unknown"


def health_scan(include_healthy: bool = False) -> dict:
    """Probe every stack; classify; return report + alerts."""
    if _fleet is None:
        return {"error": "health_monitor not wired (fleet_manager missing)"}
    inv = _fleet.fleet_list()
    results = []
    alerts = []
    counts = {"healthy": 0, "degraded": 0, "down": 0, "unknown": 0}
    for row in inv.get("stacks", []):
        name = row.get("name")
        try:
            st = _fleet.fleet_status(name)
        except Exception as e:
            st = {"error": str(e)}
        state = _classify(st)
        counts[state] = counts.get(state, 0) + 1
        entry = {"name": name, "client_id": row.get("client_id"),
                 "state": state,
                 "health": (st.get("health") or {}).get("healthy"),
                 "mcp_tools": (st.get("mcp_probe") or {}).get("tool_count"),
                 "image_tag": st.get("image_tag")}
        if state in ("degraded", "down") or (include_healthy and state == "healthy"):
            results.append(entry)
        if state in ("degraded", "down"):
            alerts.append({
                "severity": "critical" if state == "down" else "warning",
                "stack": name, "client_id": row.get("client_id"),
                "state": state,
                "message": f"stack {name} is {state.upper()}",
            })
    return {
        "scanned": len(inv.get("stacks", [])),
        "counts": counts,
        "alerts": alerts,
        "alert_count": len(alerts),
        "results": results,
        "portainer_reachable": inv.get("portainer_reachable"),
        "ts": int(time.time()),
    }


def stack_health(name_or_id: str) -> dict:
    """Deep health of one stack with classification."""
    if _fleet is None:
        return {"error": "health_monitor not wired"}
    st = _fleet.fleet_status(name_or_id)
    if st.get("error"):
        return st
    return {"name": st.get("name"), "client_id": st.get("client_id"),
            "state": _classify(st), "image_tag": st.get("image_tag"),
            "health": st.get("health"), "mcp_probe": st.get("mcp_probe")}


# ─── registration ──────────────────────────────────────────────────────────

def get_admin_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="health_scan",
             description=("ADMIN: probe every deployed stack, classify "
                          "healthy/degraded/down, and return an alert list for "
                          "degraded/down stacks. Read-only."),
             inputSchema={"type": "object",
                          "properties": {"include_healthy": {"type": "boolean",
                                                             "default": False}}}),
        Tool(name="stack_health",
             description=("ADMIN: deep health of one stack (state + /health + MCP "
                          "discovery probe + image tag). Read-only."),
             inputSchema={"type": "object",
                          "properties": {"stack": {"type": "string"}},
                          "required": ["stack"]}),
    ]


ADMIN_TOOL_NAMES = {"health_scan", "stack_health"}


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "health_scan":
        return health_scan(arguments.get("include_healthy", False))
    if name == "stack_health":
        return stack_health(arguments.get("stack", ""))
    return {"error": f"unknown health tool: {name}"}
