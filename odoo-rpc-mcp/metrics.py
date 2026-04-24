"""Prometheus metrics scaffold — 2.x baseline.

Exposes counters and gauges describing MCP activity. The `/metrics`
endpoint is bound inside the main ASGI app (see server.py) and returns
Prometheus text format with no auth — convention for Prometheus
scrapers. In production deployments the endpoint should be reachable
only from the backend network.

Full Prometheus + Grafana integration (scraping config, dashboards,
alerts) is deferred to the 3.x track. This module only ships the
instrumentation surface so call sites stop changing after 2.24.0.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("metrics")

try:
    from prometheus_client import (CollectorRegistry, Counter, Gauge,
                                   generate_latest, CONTENT_TYPE_LATEST)
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    logger.warning("prometheus_client not installed; /metrics endpoint will return 503")

METRICS_ENABLED = os.environ.get("MCP_METRICS_ENABLED", "1") == "1" and _AVAILABLE

registry: Optional["CollectorRegistry"] = None
tool_calls: Optional["Counter"] = None
proxy_discoveries: Optional["Counter"] = None
backup_writes: Optional["Counter"] = None
active_sessions: Optional["Gauge"] = None
http_requests: Optional["Counter"] = None
build_info: Optional["Gauge"] = None


def init(version: str = "unknown") -> None:
    """Create the registry + metrics objects. Idempotent."""
    global registry, tool_calls, proxy_discoveries, backup_writes
    global active_sessions, http_requests, build_info
    if not METRICS_ENABLED:
        return
    if registry is not None:
        return
    registry = CollectorRegistry()
    tool_calls = Counter(
        "mcp_tool_calls_total",
        "MCP tool invocations",
        ("tool", "status"),
        registry=registry,
    )
    proxy_discoveries = Counter(
        "mcp_proxy_discoveries_total",
        "Proxy plugin tool discovery outcomes",
        ("service", "outcome"),
        registry=registry,
    )
    backup_writes = Counter(
        "mcp_backup_writes_total",
        "JSON backup snapshots written to /backups (S3-backed or local)",
        ("operation", "tenant"),
        registry=registry,
    )
    active_sessions = Gauge(
        "mcp_active_sessions",
        "Currently active MCP sessions",
        registry=registry,
    )
    http_requests = Counter(
        "mcp_http_requests_total",
        "HTTP requests hitting the MCP ASGI app",
        ("method", "path_group", "status"),
        registry=registry,
    )
    build_info = Gauge(
        "mcp_build_info",
        "Build metadata; value is always 1, labels carry version.",
        ("version",),
        registry=registry,
    )
    build_info.labels(version=version).set(1)
    logger.info("metrics initialised (version=%s)", version)


# ── Call-site helpers (no-ops when metrics disabled) ──

def observe_tool_call(tool: str, status: str = "ok") -> None:
    if METRICS_ENABLED and tool_calls is not None:
        tool_calls.labels(tool=tool, status=status).inc()


def observe_proxy_discovery(service: str, outcome: str = "ok") -> None:
    if METRICS_ENABLED and proxy_discoveries is not None:
        proxy_discoveries.labels(service=service, outcome=outcome).inc()


def observe_backup_write(operation: str, tenant: str) -> None:
    if METRICS_ENABLED and backup_writes is not None:
        backup_writes.labels(operation=operation, tenant=tenant).inc()


def observe_session_count(n: int) -> None:
    if METRICS_ENABLED and active_sessions is not None:
        active_sessions.set(n)


def observe_http_request(method: str, path: str, status: int) -> None:
    if METRICS_ENABLED and http_requests is not None:
        grp = _path_group(path)
        http_requests.labels(method=method, path_group=grp, status=str(status)).inc()


def _path_group(path: str) -> str:
    """Collapse paths to low-cardinality groups for Prometheus labels."""
    if path.startswith("/admin"):
        return "/admin"
    if path.startswith("/mcp"):
        return "/mcp"
    if path.startswith("/sse") or path.startswith("/messages"):
        return "/sse"
    if path.startswith("/ollama"):
        return "/ollama"
    if path.startswith("/api/"):
        return "/api"
    if path.startswith("/oauth") or path.startswith("/.well-known"):
        return "/oauth"
    if path == "/health":
        return "/health"
    if path == "/metrics":
        return "/metrics"
    return "other"


# ── Render endpoint body ──

def render() -> tuple[bytes, str, int]:
    """Return (body, content_type, status). 503 when disabled."""
    if not METRICS_ENABLED or registry is None:
        return (b"# metrics disabled\n", "text/plain", 503)
    try:
        body = generate_latest(registry)
        return (body, CONTENT_TYPE_LATEST, 200)
    except Exception as exc:
        logger.exception("metrics render failed")
        return (f"# error: {exc}\n".encode(), "text/plain", 500)
