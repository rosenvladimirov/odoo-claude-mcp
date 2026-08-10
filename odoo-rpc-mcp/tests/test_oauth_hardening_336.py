"""3.3.6: OAuth hardening + credential redaction.

Closes the anonymous DCR bypass found in the 2026-08-02 authN audit:
`POST /oauth/register` (public) -> `POST /oauth/token` with
grant_type=client_credentials -> a valid Bearer accepted on `/mcp`.

Two independent locks:
  1. `client_credentials` is refused unless explicitly re-enabled.
  2. `/oauth/authorize` demands PKCE S256, and `/oauth/token` verifies the
     code_verifier against the bound challenge.

Plus: `who_am_i` no longer hands out live secrets.

The server module pulls in the whole MCP stack on import, so these tests
extract the pure helpers by source rather than importing it — the point is
to pin the security logic, not to boot a server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from pathlib import Path
from typing import Any

import pytest

SERVER_PY = Path(__file__).resolve().parent.parent / "server.py"
_FUNCS = (
    "_oauth_pkce_required",
    "_oauth_client_credentials_allowed",
    "_oauth_verify_pkce",
    "_redact_connection_secrets",
)


@pytest.fixture(scope="module")
def srv() -> dict:
    """Namespace holding just the 3.3.6 helpers lifted out of server.py."""
    src = SERVER_PY.read_text(encoding="utf-8")
    ns: dict[str, Any] = {"os": os, "hmac": hmac, "Any": Any}
    for fn in _FUNCS:
        match = re.search(rf"^def {fn}\(.*?(?=^\S)", src, re.S | re.M)
        assert match, f"{fn} missing from server.py — did the fix get reverted?"
        exec(compile(match.group(0), fn, "exec"), ns)
    keys = re.search(r"^_SECRET_CONN_KEYS = \((.*?)\)", src, re.S | re.M)
    assert keys, "_SECRET_CONN_KEYS missing"
    ns["_SECRET_CONN_KEYS"] = eval(f"({keys.group(1)})")  # noqa: S307 - our own literal
    return ns


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"


# ── client_credentials kill switch ────────────────────────────────────────

def test_client_credentials_off_by_default(srv, monkeypatch):
    """FAIL-CLOSED: the grant that enabled anonymous Bearer minting."""
    monkeypatch.delenv("MCP_OAUTH_ALLOW_CLIENT_CREDENTIALS", raising=False)
    assert srv["_oauth_client_credentials_allowed"] is not None
    assert srv["_oauth_client_credentials_allowed"]() is False


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("True", True),
    ("0", False), ("false", False), ("", False), ("yes", False),
])
def test_client_credentials_opt_in(srv, monkeypatch, value, expected):
    monkeypatch.setenv("MCP_OAUTH_ALLOW_CLIENT_CREDENTIALS", value)
    assert srv["_oauth_client_credentials_allowed"]() is expected


# ── PKCE requirement ──────────────────────────────────────────────────────

def test_pkce_required_by_default(srv, monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_REQUIRE_PKCE", raising=False)
    assert srv["_oauth_pkce_required"]() is True


@pytest.mark.parametrize("value,expected", [
    ("0", False), ("false", False), ("False", False),
    ("1", True), ("anything-else", True),
])
def test_pkce_opt_out_is_explicit(srv, monkeypatch, value, expected):
    monkeypatch.setenv("MCP_OAUTH_REQUIRE_PKCE", value)
    assert srv["_oauth_pkce_required"]() is expected


# ── PKCE verification (RFC 7636) ──────────────────────────────────────────

def test_correct_verifier_passes(srv):
    assert srv["_oauth_verify_pkce"](_s256(VERIFIER), "S256", VERIFIER) is True


def test_wrong_verifier_rejected(srv):
    assert srv["_oauth_verify_pkce"](_s256(VERIFIER), "S256", "not-the-verifier") is False


def test_empty_verifier_rejected_when_challenge_bound(srv):
    """A stolen code alone must not be redeemable."""
    assert srv["_oauth_verify_pkce"](_s256(VERIFIER), "S256", "") is False


def test_no_challenge_is_permissive(srv):
    """Codes issued before the upgrade carry no challenge — don't break them."""
    assert srv["_oauth_verify_pkce"]("", "S256", "") is True


def test_plain_method_supported(srv):
    assert srv["_oauth_verify_pkce"](VERIFIER, "PLAIN", VERIFIER) is True


@pytest.mark.parametrize("method", ["MD5", "SHA1", "s512", "junk"])
def test_unknown_method_rejected(srv, method):
    assert srv["_oauth_verify_pkce"](_s256(VERIFIER), method, VERIFIER) is False


def test_method_is_case_insensitive(srv):
    assert srv["_oauth_verify_pkce"](_s256(VERIFIER), "s256", VERIFIER) is True


def test_real_claude_ai_challenge_needs_its_own_verifier(srv):
    """Challenge captured from a live claude.ai flow (2026-08-09)."""
    live = "WSpdiR_A-p-TU07lGQ1H_M1geglRfxmMcER4VBdF5cU"
    assert srv["_oauth_verify_pkce"](live, "S256", VERIFIER) is False


# ── who_am_i redaction ────────────────────────────────────────────────────

def _sample_conn() -> dict:
    return {
        "alias": "ussmed",
        "url": "https://www.ussmed.com",
        "db": "ussmed-ee-pro",
        "user": "someone@example.com",
        "api_key": "LIVE-ODOO-KEY",
        "ssh": {"host": "erp.example.com", "user": "root", "port": 22},
        "portainer": {"url": "https://admin.example.com",
                      "token": "ptr_LIVE_TOKEN", "read_only": False},
    }


def test_top_level_api_key_masked(srv):
    out = srv["_redact_connection_secrets"](_sample_conn())
    assert out["api_key"] == "<set>"


def test_nested_portainer_token_masked(srv):
    """The audit's fix covered /api/user/connections but missed this path."""
    out = srv["_redact_connection_secrets"](_sample_conn())
    assert out["portainer"]["token"] == "<set>"


def test_non_secret_fields_survive(srv):
    out = srv["_redact_connection_secrets"](_sample_conn())
    assert out["url"] == "https://www.ussmed.com"
    assert out["db"] == "ussmed-ee-pro"
    assert out["ssh"]["host"] == "erp.example.com"
    assert out["portainer"]["read_only"] is False


def test_redaction_does_not_mutate_the_original(srv):
    """It must not corrupt the in-memory connection store."""
    conn = _sample_conn()
    srv["_redact_connection_secrets"](conn)
    assert conn["api_key"] == "LIVE-ODOO-KEY"
    assert conn["portainer"]["token"] == "ptr_LIVE_TOKEN"


def test_empty_secret_stays_empty(srv):
    """Distinguish 'no key' from 'key present' — '<set>' would be a lie."""
    out = srv["_redact_connection_secrets"]({"api_key": "", "url": "u"})
    assert out["api_key"] == ""


def test_list_of_connections_redacted(srv):
    out = srv["_redact_connection_secrets"]([_sample_conn(), _sample_conn()])
    assert all(c["api_key"] == "<set>" for c in out)


def test_no_live_secret_survives_anywhere(srv):
    """Belt-and-braces: no known secret value may appear in the output."""
    rendered = repr(srv["_redact_connection_secrets"](_sample_conn()))
    assert "LIVE-ODOO-KEY" not in rendered
    assert "ptr_LIVE_TOKEN" not in rendered
