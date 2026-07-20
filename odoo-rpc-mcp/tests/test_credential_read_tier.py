"""B.0 follow-up: USER may not read shared credential stores, but CAN read its
own data on ordinary models. (Rosen: "само собствените си".)
"""
from __future__ import annotations
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def ts(monkeypatch):
    monkeypatch.delenv("MCP_ROLE", raising=False)
    import tool_security
    importlib.reload(tool_security)
    return tool_security


CRED_MODELS = ["ir.config_parameter", "ir.mail_server", "fetchmail.server",
               "res.users.apikeys", "auth.totp.user"]
READ_TOOLS = ["odoo_read", "odoo_search", "odoo_search_read",
              "odoo_search_count"]


@pytest.mark.parametrize("model", CRED_MODELS)
@pytest.mark.parametrize("tool", READ_TOOLS)
def test_user_credential_read_denied(ts, tool, model):
    ok, info = ts.check_call(tool, {"model": model}, role="user")
    assert ok is False
    assert info["reason"] == "protected_credential_read"


@pytest.mark.parametrize("model", CRED_MODELS)
def test_user_credential_read_via_execute_denied(ts, model):
    ok, info = ts.check_call(
        "odoo_execute", {"model": model, "method": "read"}, role="user")
    assert ok is False
    assert info["reason"] == "protected_credential_read"


def test_user_can_read_own_ordinary_models(ts):
    # ordinary models stay readable for USER (own data via Odoo record rules)
    for model in ("res.partner", "res.users", "account.move", "sale.order"):
        ok, _ = ts.check_call("odoo_search_read", {"model": model}, role="user")
        assert ok is True, model
        ok2, _ = ts.check_call("odoo_execute",
                               {"model": model, "method": "search_read"},
                               role="user")
        assert ok2 is True, model


def test_admin_reads_credentials(ts):
    ok, _ = ts.check_call("odoo_read", {"model": "ir.config_parameter"},
                          role="admin")
    assert ok is True


def test_elevated_user_reads_credentials(ts):
    ok, info = ts.check_call("odoo_read", {"model": "ir.config_parameter"},
                             role="user", elevated=True)
    assert ok is True
    assert info.get("elevated") is True


def test_credential_write_still_blocked_first(ts):
    # writing a credential model is caught by the write gate, not the read tier
    ok, info = ts.check_call(
        "odoo_execute", {"model": "ir.config_parameter", "method": "write"},
        role="user")
    assert ok is False
    assert "protected_write" in info["reason"]
