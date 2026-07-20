"""B.0-4: active tenant must be per-session — tenant_use() in one session must
not change another session's active tenant (cross-tenant takeover fix).
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

import pytest
from mcp.types import Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tenant_router as tr  # noqa: E402


@pytest.fixture
def wired(monkeypatch):
    state = {}  # (skey, ns, key) -> value
    services = {"main": {}, "clientA": {}, "clientB": {}}

    def discover_one(name):
        return [Tool(name=f"{name}__ping", description="",
                     inputSchema={"type": "object", "properties": {}})]

    monkeypatch.setattr(tr, "ALWAYS_ON", {"main"})
    monkeypatch.setattr(tr, "_cache", {}, raising=False)
    monkeypatch.setattr(tr, "_health", {}, raising=False)
    tr.wire(get_proxy_services=lambda: services, discover_one=discover_one,
            get_session_state=lambda sk, ns, k: state.get((sk, ns, k)),
            set_session_state=lambda sk, ns, k, v: state.__setitem__((sk, ns, k), v))
    return state


def test_two_sessions_isolated(wired):
    asyncio.run(tr.set_active_tenant("clientA", "mcp:S1"))
    asyncio.run(tr.set_active_tenant("clientB", "mcp:S2"))
    assert tr.get_active_tenant("mcp:S1") == "clientA"
    assert tr.get_active_tenant("mcp:S2") == "clientB"
    a = {t.name for t in tr.active_tools("mcp:S1")}
    b = {t.name for t in tr.active_tools("mcp:S2")}
    assert a == {"clientA__ping"} and b == {"clientB__ping"}
    assert a.isdisjoint(b)


def test_no_session_fallback(wired):
    asyncio.run(tr.set_active_tenant("clientA", "mcp:S1"))
    assert tr.get_active_tenant(None) is None
    assert tr.active_tools(None) == []


def test_persistence_within_session(wired):
    asyncio.run(tr.set_active_tenant("clientA", "mcp:S1"))
    for _ in range(3):
        assert tr.get_active_tenant("mcp:S1") == "clientA"
        assert {t.name for t in tr.active_tools("mcp:S1")} == {"clientA__ping"}


def test_clear_is_per_session(wired):
    asyncio.run(tr.set_active_tenant("clientA", "mcp:S1"))
    asyncio.run(tr.set_active_tenant("clientB", "mcp:S2"))
    asyncio.run(tr.clear_active_tenant("mcp:S1"))
    assert tr.get_active_tenant("mcp:S1") is None
    assert tr.get_active_tenant("mcp:S2") == "clientB"


def test_always_on_rejected_as_active(wired):
    res = asyncio.run(tr.set_active_tenant("main", "mcp:S1"))
    assert "error" in res
    assert tr.active_tools("mcp:S1") == []


def test_handle_threads_session_key(wired):
    asyncio.run(tr.handle("tenant_use", {"name": "clientA"}, "mcp:S1"))
    cur = asyncio.run(tr.handle("tenant_current", {}, "mcp:S1"))
    assert cur["active"] == "clientA"
    cur2 = asyncio.run(tr.handle("tenant_current", {}, "mcp:S2"))
    assert cur2["active"] is None


def test_set_without_session_denied(wired):
    res = asyncio.run(tr.set_active_tenant("clientA", ""))
    assert "error" in res
