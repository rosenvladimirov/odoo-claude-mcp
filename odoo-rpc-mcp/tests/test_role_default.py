"""B.0-1: MCP_ROLE default must be fail-closed ('user'), not fail-open ('admin').

Admin is granted only on explicit MCP_ROLE, or a verified admin principal.
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


def test_default_is_user(ts):
    """FAIL-CLOSED: unset MCP_ROLE → 'user'."""
    assert ts.get_role() == "user"


def test_explicit_admin(monkeypatch):
    monkeypatch.setenv("MCP_ROLE", "admin")
    import tool_security
    importlib.reload(tool_security)
    assert tool_security.get_role() == "admin"


def test_explicit_legacy(monkeypatch):
    monkeypatch.setenv("MCP_ROLE", "legacy")
    import tool_security
    importlib.reload(tool_security)
    assert tool_security.get_role() == "legacy"


def test_admin_principal_elevates(ts):
    """Verified admin principal → admin even with MCP_ROLE unset."""
    admins = {"rosen"}
    assert ts.get_role(principal="rosen", admin_principals=admins) == "admin"
    assert ts.get_role(principal="client_x", admin_principals=admins) == "user"
    assert ts.get_role(principal=None, admin_principals=admins) == "user"


def test_default_user_blocks_protected_write(ts):
    """End-to-end: default role denies res.users write via odoo_execute."""
    ok, info = ts.check_call(
        "odoo_execute", {"model": "res.users", "method": "write"})
    assert ok is False
    assert "protected_write" in info["reason"]


def test_explicit_env_overrides_admin_principal(monkeypatch):
    """An explicit MCP_ROLE wins over principal logic (operator intent)."""
    monkeypatch.setenv("MCP_ROLE", "user")
    import tool_security
    importlib.reload(tool_security)
    # even a listed admin principal stays user when env explicitly says user
    assert tool_security.get_role(principal="rosen",
                                  admin_principals={"rosen"}) == "user"
