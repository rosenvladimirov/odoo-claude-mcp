"""Агент (Клаудчо) като принципал: бял списък, без елевация, res.* само за четене.

MCP_AGENT_PRINCIPALS="bot:read,client:full" (ADR telegram-consult-bot/0008, т. 5).
Проверката за агента стои ПРЕДИ контролните инструменти (елевация, сесии) —
иначе те биха я заобиколили.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as srv  # noqa: E402
import tool_security as ts  # noqa: E402

AGENTS = "kc-bot:read,kc-client:full,kc-typo:superuser"


@pytest.fixture(autouse=True)
def _agents(monkeypatch):
    monkeypatch.setenv("MCP_AGENT_PRINCIPALS", AGENTS)


def allowed(tool, args, mode):
    return ts.check_agent_call(tool, args, mode)[0]


# ─────────────── профилът ───────────────

def test_principals_parse_and_unknown_mode_is_read():
    assert ts.agent_principals() == {"kc-bot": "read", "kc-client": "full", "kc-typo": "read"}
    assert ts.get_agent_mode("kc-client") == "full"
    assert ts.get_agent_mode("rosen") is None and ts.get_agent_mode(None) is None


@pytest.mark.parametrize("mode", ["read", "full"])
def test_both_modes_read_and_use_their_own_methods(mode):
    assert allowed("odoo_search_read", {"model": "res.partner"}, mode)          # res.* се чете
    assert allowed("odoo_execute", {"model": "res.partner", "method": "read"}, mode)
    for m in ("klaudcho_push", "klaudcho_inbox_pull", "klaudcho_inbox_ack", "klaudcho_post", "klaudcho_notify"):
        assert allowed("odoo_execute", {"model": "klaudcho.agent", "method": m}, mode), m
    # В режим read собственият метод минава само на klaudcho.agent (в full всеки бизнес метод е позволен)
    if mode == "read":
        assert not allowed("odoo_execute", {"model": "sale.order", "method": "klaudcho_push"}, mode)


@pytest.mark.parametrize("mode", ["read", "full"])
def test_both_modes_never_reach_infra_sql_elevation_or_secrets(mode):
    for tool in ("odoo_sql_query", "ssh_execute", "mcp_elevate", "session_handoff_offer", "fleet_upgrade",
                 "module_deploy", "secrets_list", "proxy_call", "tenant_use", "identify"):
        assert not allowed(tool, {}, mode), tool
    assert not allowed("odoo_search_read", {"model": "res.users.apikeys"}, mode)   # таен склад
    assert not allowed("odoo_execute", {"model": "ir.config_parameter", "method": "search_read"}, mode)
    assert not allowed("odoo_execute", {"model": "x", "method": "_private"}, mode)
    assert not allowed("odoo_execute", {"model": "ir.module.module", "method": "button_immediate_install"}, mode)


def test_read_mode_writes_nothing():
    for tool, args in [("odoo_write", {"model": "sale.order"}), ("odoo_create", {"model": "sale.order"}),
                       ("odoo_unlink", {"model": "sale.order"}), ("odoo_message_post", {"model": "sale.order"}),
                       ("odoo_execute", {"model": "sale.order", "method": "action_confirm"})]:
        ok, info = ts.check_agent_call(tool, args, "read")
        assert not ok and info["reason"] == "agent_read_only", tool


def test_full_mode_writes_business_data_but_not_res_or_system():
    assert allowed("odoo_write", {"model": "sale.order", "ids": [1], "values": {}}, "full")
    assert allowed("odoo_execute", {"model": "account.move", "method": "action_post"}, "full")
    assert allowed("odoo_create", {"model": "account.account", "values": {}}, "full")  # счетоводството е бизнес
    for tool, args in [("odoo_write", {"model": "res.partner"}), ("odoo_create", {"model": "res.users"}),
                       ("odoo_unlink", {"model": "res.company"}),
                       ("odoo_execute", {"model": "res.partner", "method": "write"}),
                       ("odoo_message_post", {"model": "res.partner"}),
                       ("odoo_write", {"model": "ir.rule"}), ("odoo_create", {"model": "auth.totp.device"})]:
        ok, info = ts.check_agent_call(tool, args, "full")
        assert not ok and info["reason"] == "agent_no_write_model", (tool, args)


def test_tool_list_for_agents():
    class T:
        def __init__(self, name):
            self.name = name
    tools = [T(n) for n in ("odoo_search_read", "odoo_write", "odoo_execute", "odoo_sql_query",
                            "mcp_elevate", "ssh_execute", "who_am_i")]
    assert [t.name for t in ts.filter_tools_for_agent(tools, "read")] == ["odoo_search_read", "odoo_execute", "who_am_i"]
    assert "odoo_write" in [t.name for t in ts.filter_tools_for_agent(tools, "full")]


# ─────────────── сървърът ───────────────

def _ctx(principal):
    # Своя сесия за всеки вик — елевация от един тест не бива да се пренесе в друг
    import uuid
    return srv.SessionContext(session_key=f"mcp:agent-test:{uuid.uuid4().hex}", transport="streamable_http",
                              principal=principal, principal_src="unified_auth", caller=None)


def _call(name, args, principal, monkeypatch):
    async def _fake():
        return _ctx(principal)
    monkeypatch.setattr(srv, "resolve_session_context", _fake)
    out = asyncio.run(srv.call_tool(name, args))
    try:
        return json.loads(out[0].text)
    except ValueError:
        return {"text": out[0].text}


@pytest.mark.parametrize("tool,args", [
    ("mcp_elevate", {"reason": "искам права", "ttl": 300}),          # елевацията е контролен инструмент — пак не
    ("session_handoff_offer", {}),
    ("odoo_sql_query", {"query": "select 1"}),
    ("odoo_write", {"model": "res.partner", "ids": [1], "values": {"vat": "BG1"}}),
])
def test_server_denies_agent_before_control_tools(tool, args, monkeypatch):
    out = _call(tool, args, "kc-bot", monkeypatch)
    assert out.get("error") == "denied" and out["reason"].startswith("agent_"), out


def test_server_lets_the_bot_reach_its_own_method(monkeypatch):
    # Отвъд проверката викът отива към Odoo (тук няма връзка) — важното е, че НЕ е отказан от сигурността
    out = _call("odoo_execute", {"model": "klaudcho.agent", "method": "klaudcho_push", "args": [[1], []]},
                "kc-bot", monkeypatch)
    assert out.get("error") != "denied", out


def test_server_user_role_still_blocks_the_same_method_for_non_agents(monkeypatch):
    # Обикновен потребител без профил на агент: ролевата проверка спира непознатия метод
    out = _call("odoo_execute", {"model": "klaudcho.agent", "method": "klaudcho_push", "args": [[1], []]},
                "lyubomir", monkeypatch)
    assert out.get("error") == "denied" and "readonly_allowlist" in out["reason"], out


def test_list_tools_for_agent_has_no_elevation_or_infra(monkeypatch):
    async def _fake():
        return _ctx("kc-bot")
    monkeypatch.setattr(srv, "resolve_session_context", _fake)
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert "odoo_search_read" in names and "odoo_execute" in names
    for bad in ("mcp_elevate", "odoo_sql_query", "ssh_execute", "odoo_write", "session_handoff_offer"):
        assert bad not in names, bad
