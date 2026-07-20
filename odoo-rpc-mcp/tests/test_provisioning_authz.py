"""B.0-6: provision_* tools must require a verified admin principal.

A direct call_tool('provision_issue_api_key', ...) by a non-admin (or
no-identity) session must be denied BEFORE api_key_manager.handle runs —
otherwise any session self-mints an admin API key. Elevation control tools
must stay reachable by a plain USER.
"""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as srv  # noqa: E402


def _ctx(principal):
    return srv.SessionContext(
        session_key="mcp:test", transport="streamable_http",
        principal=principal, principal_src="unified_auth", caller=None)


def _run(name, args, principal, monkeypatch, admins="rosen"):
    monkeypatch.setenv("MCP_ADMIN_PRINCIPALS", admins)

    async def _fake_resolve():
        return _ctx(principal)

    monkeypatch.setattr(srv, "resolve_session_context", _fake_resolve)
    out = asyncio.run(srv.call_tool(name, args))
    return json.loads(out[0].text)


def test_no_principal_issue_denied(monkeypatch):
    out = _run("provision_issue_api_key", {"email": "evil@x.com"}, None, monkeypatch)
    assert out.get("error") == "denied"
    assert out.get("reason") == "no_identity"
    assert "api_key" not in out


def test_user_principal_not_admin_denied(monkeypatch):
    out = _run("provision_issue_api_key", {"email": "evil@x.com"},
               "lyubomir", monkeypatch)
    assert out.get("error") == "denied"
    assert out.get("reason") == "not_admin_principal"
    assert "api_key" not in out


def test_role_admin_env_alone_does_not_bypass(monkeypatch):
    """MCP_ROLE=admin must NOT substitute for an admin principal."""
    monkeypatch.setenv("MCP_ROLE", "admin")
    out = _run("provision_issue_api_key", {"email": "evil@x.com"},
               "lyubomir", monkeypatch)
    assert out.get("error") == "denied"
    assert "api_key" not in out


def test_admin_principal_issue_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_KEY_PEPPER", "x" * 40)
    monkeypatch.setattr("api_key_manager.API_KEYS_FILE",
                        tmp_path / "api_keys.jsonl")
    out = _run("provision_issue_api_key",
               {"email": "ok@x.com", "role": "tenant", "scope_csv": "client_a"},
               "rosen", monkeypatch)
    assert out.get("error") != "denied"
    assert out.get("api_key", "").startswith("mcpv3_")


def test_list_revoke_user_denied(monkeypatch):
    out = _run("provision_list_api_keys", {}, "lyubomir", monkeypatch)
    assert out.get("error") == "denied"
    out2 = _run("provision_revoke_api_key", {"key_id": "k_dead"},
                "lyubomir", monkeypatch)
    assert out2.get("error") == "denied"


def test_fleet_tools_user_denied(monkeypatch):
    out = _run("fleet_list", {}, "lyubomir", monkeypatch)
    assert out.get("error") == "denied"
    out2 = _run("fleet_upgrade", {"stack": "x", "target_tag": "y"},
                "lyubomir", monkeypatch)
    assert out2.get("error") == "denied"


def test_secrets_tools_user_denied(monkeypatch):
    out = _run("secrets_list", {}, "lyubomir", monkeypatch)
    assert out.get("error") == "denied"
    out2 = _run("secrets_rotate", {"target_stack": "x", "kind": "stack_token"},
                "lyubomir", monkeypatch)
    assert out2.get("error") == "denied"


def test_fleet_list_admin_allowed(monkeypatch):
    out = _run("fleet_list", {}, "rosen", monkeypatch)
    # admin principal passes the gate; with no Portainer env it returns a
    # structured result (not a 'denied' authz error).
    assert out.get("error") != "denied"


def test_module_deploy_tools_user_denied(monkeypatch):
    out = _run("module_deploy", {"target": "x", "module": "m"},
               "lyubomir", monkeypatch)
    assert out.get("error") == "denied"
    out2 = _run("module_deploy_history", {}, "lyubomir", monkeypatch)
    assert out2.get("error") == "denied"


def test_module_deploy_history_admin_allowed(monkeypatch):
    out = _run("module_deploy_history", {}, "rosen", monkeypatch)
    assert out.get("error") != "denied"


def test_onboard_tools_user_denied(monkeypatch):
    out = _run("client_onboard", {"slug_or_vat": "x", "email": "a@b.com"},
               "lyubomir", monkeypatch)
    assert out.get("error") == "denied"
    out2 = _run("client_onboard_status", {"slug_or_vat": "x"},
                "lyubomir", monkeypatch)
    assert out2.get("error") == "denied"


def test_backup_health_tools_user_denied(monkeypatch):
    for tool, args in (("tenant_backup", {"target": "x"}),
                       ("tenant_restore", {"target": "x"}),
                       ("health_scan", {}),
                       ("stack_health", {"stack": "x"})):
        out = _run(tool, args, "lyubomir", monkeypatch)
        assert out.get("error") == "denied", tool


def test_migrate_tools_user_denied(monkeypatch):
    out = _run("migrate_assess", {"source": "x"}, "lyubomir", monkeypatch)
    assert out.get("error") == "denied"
    out2 = _run("migrate_history", {}, "lyubomir", monkeypatch)
    assert out2.get("error") == "denied"


def test_handoff_reachable_by_user(monkeypatch):
    """session_handoff is a control plane — a plain USER can offer/list."""
    out = _run("session_handoff_status", {}, "lyubomir", monkeypatch)
    assert out.get("error") != "denied"


def test_user_can_request_elevation(monkeypatch):
    """The elevation control plane stays reachable by a plain USER."""
    out = _run("mcp_elevate", {"reason": "need to fix data", "ttl": 60},
               "lyubomir", monkeypatch)
    assert out.get("elevated") is True
    # cleanup so we don't leak elevation into other tests of this session key
    _run("mcp_drop_elevation", {}, "lyubomir", monkeypatch)
