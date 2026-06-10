"""B.3: client_onboard wizard — dry-run plan, real orchestration, idempotency."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import client_onboard as co  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "ONBOARD_DIR", tmp_path / "onboarding")
    monkeypatch.setattr(co, "DRY_RUN", True)
    # provisioning_engine.provision → deterministic fake
    monkeypatch.setattr(co.pe, "provision", lambda **kw: {
        "status": "completed", "slug": "bg123456789",
        "client_id": "bg123456789",
        "mcp_url": "https://mcp-bg123456789.mcpworks.net",
        "secret_token": "stok", "admin_token": "atok", "zip_b64": "ZIP"})
    monkeypatch.setattr(co.pe, "generate_secret_token", lambda n=32: "x" * n)
    return co


def test_onboard_dry_run_plan(env):
    out = env.onboard("BG123456789", "ops@x.com")
    assert out["dry_run"] is True
    plan = out["plan"]
    assert plan["client_id"] == "bg123456789"
    assert plan["would_issue_tenant_key"]["scope"] == ["bg123456789"]
    assert "tg-listener" in plan["handoff_preview"]
    # nothing written in dry-run


def test_onboard_requires_email(env):
    out = env.onboard("BG123", "not-an-email")
    assert out["error"] == "valid email is required"


def test_onboard_real_orchestration(env, monkeypatch):
    monkeypatch.setattr(env, "DRY_RUN", False)
    issued = {}
    monkeypatch.setattr(env.api_key_manager, "issue",
                        lambda **kw: issued.update(kw) or {"key_id": "k_1",
                                                           "api_key": "mcpv3_k_1_sec"})
    registered = []
    monkeypatch.setattr(env.secrets_registry, "register",
                        lambda stack, kind, value="": registered.append((stack, kind))
                        or {"secret_id": f"s_{kind}"})
    out = env.onboard("BG123456789", "ops@x.com", dry_run=False)
    assert out["ok"] is True
    assert out["client_id"] == "bg123456789"
    assert out["tenant_key_issued"] is True
    assert out["tenant_key"] == "mcpv3_k_1_sec"
    # tenant key scoped to the client
    assert issued["role"] == "tenant"
    assert issued["scope"] == ["bg123456789"]
    # both stack secrets registered
    assert ("mcp-client-bg123456789", "stack_token") in registered
    assert ("mcp-client-bg123456789", "admin_token") in registered
    # handoff written
    assert out["handoff_path"] and Path(out["handoff_path"]).is_file()


def test_onboard_provision_failure_aborts(env, monkeypatch):
    monkeypatch.setattr(env.pe, "provision", lambda **kw: {"error": "boom"})
    out = env.onboard("BG123456789", "ops@x.com", dry_run=False)
    assert out["error"] == "provision_failed"


def test_onboard_status(env, monkeypatch):
    monkeypatch.setattr(env.pe, "get_state", lambda slug: {
        "client_id": "bg123456789", "mcp_url": "u", "status": "completed"})
    monkeypatch.setattr(env.secrets_registry, "list_secrets",
                        lambda stack=None: {"count": 2, "secrets": []})
    out = env.onboard_status("BG123456789")
    assert out["provisioned"] is True
    assert out["stack_name"] == "mcp-client-bg123456789"
    assert out["secrets"]["count"] == 2


def test_handle_dispatch(env):
    out = env.handle("client_onboard",
                     {"slug_or_vat": "BG999", "email": "a@b.com"})
    assert out["dry_run"] is True
