"""B.4: secrets_registry register/list/rotate/revoke + crypto fail-closed."""
from __future__ import annotations
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sr(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_KEY_PEPPER", "p" * 40)
    monkeypatch.setenv("SECRETS_REGISTRY_FILE", str(tmp_path / "secrets.jsonl"))
    import secrets_registry
    importlib.reload(secrets_registry)
    return secrets_registry


def test_register_generates_and_returns_once(sr):
    out = sr.register("mcp-client-x", "stack_token")
    assert out["secret_id"].startswith("s_")
    assert out["target_stack"] == "mcp-client-x"
    assert out["kind"] == "stack_token"
    assert out["fingerprint"]            # HMAC stored
    assert out["value"]                  # plaintext returned once
    assert out["recoverable"] is True


def test_list_never_leaks_plaintext(sr):
    sr.register("mcp-client-x", "stack_token", value="supersecret")
    lst = sr.list_secrets("mcp-client-x")
    assert lst["count"] == 1
    rec = lst["secrets"][0]
    assert "value" not in rec and "enc" not in rec
    assert rec["fingerprint"]


def test_reveal_roundtrip(sr):
    reg = sr.register("mcp-client-x", "admin_token", value="tok-123")
    rev = sr.reveal(reg["secret_id"])
    assert rev["value"] == "tok-123"


def test_rotate_retires_previous(sr):
    first = sr.register("mcp-client-x", "stack_token", value="old")
    rot = sr.rotate("mcp-client-x", "stack_token")
    assert rot["value"] != "old"
    assert rot["rotated_from"] == first["secret_id"]
    assert rot["requires_redeploy"] is True
    # old one is retired (not active)
    active = {r["secret_id"] for r in sr.list_secrets("mcp-client-x")["secrets"]
              if r["status"] == "active"}
    assert rot["secret_id"] in active
    assert first["secret_id"] not in active


def test_revoke(sr):
    reg = sr.register("mcp-client-x", "oauth_secret")
    sr.revoke(reg["secret_id"], reason="leaked")
    assert sr.get(reg["secret_id"]) is None     # revoked → gone from active map


def test_invalid_kind(sr):
    out = sr.register("mcp-client-x", "not_a_kind")
    assert out["error"] == "invalid_kind"


def test_fail_closed_without_pepper(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_KEY_PEPPER", raising=False)
    monkeypatch.setenv("SECRETS_REGISTRY_FILE", str(tmp_path / "s.jsonl"))
    import secrets_registry
    importlib.reload(secrets_registry)
    out = secrets_registry.register("mcp-client-x", "stack_token")
    assert out["error"] == "weak_or_missing_pepper"


def test_handle_dispatch(sr):
    out = sr.handle("secrets_register",
                    {"target_stack": "mcp-client-y", "kind": "stack_token"})
    assert out["value"]
    lst = sr.handle("secrets_list", {"target_stack": "mcp-client-y"})
    assert lst["count"] == 1
