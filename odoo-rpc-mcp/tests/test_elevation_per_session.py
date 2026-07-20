"""B.0-5: elevation must be per-session — an mcp_elevate() in session A must
not elevate session B. TTL, drop, status and audit are all per session.
"""
from __future__ import annotations
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def elev(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVATION_AUDIT_FILE", str(tmp_path / "audit.log"))
    monkeypatch.setenv("MCP_ELEVATION_TTL", "300")
    monkeypatch.setenv("MCP_ELEVATION_MAX_TTL", "3600")
    import elevation
    importlib.reload(elevation)
    return elevation


def test_isolated_per_session(elev):
    A, B = "mcp:aaa", "mcp:bbb"
    r = elev.grant(A, reason="fix", ttl=300, principal="rosen")
    assert r["elevated"] is True
    assert elev.is_elevated(A) is True
    assert elev.is_elevated(B) is False      # the fix


def test_ttl_expiry(elev):
    A = "mcp:ttl"
    elev.grant(A, reason="x", ttl=1)
    assert elev.is_elevated(A) is True
    time.sleep(1.1)
    assert elev.is_elevated(A) is False
    assert elev.status(A) == {"elevated": False}


def test_drop_only_current_session(elev):
    A, B = "mcp:a", "mcp:b"
    elev.grant(A, reason="x", ttl=300)
    elev.grant(B, reason="y", ttl=300)
    elev.drop(A)
    assert elev.is_elevated(A) is False
    assert elev.is_elevated(B) is True       # B untouched


def test_status_per_session(elev):
    A, B = "mcp:a", "mcp:b"
    elev.grant(A, reason="why", ttl=300, principal="rosen")
    assert elev.status(A)["elevated"] is True
    assert elev.status(A)["reason"] == "why"
    assert elev.status(B) == {"elevated": False}


def test_audit_records_principal_and_session(elev):
    A = "mcp:audit"
    elev.grant(A, reason="r", ttl=300, principal="rosen")
    elev.drop(A)
    lines = [json.loads(l) for l in
             Path(elev.ELEVATION_AUDIT).read_text().splitlines()]
    granted = next(l for l in lines if l["action"] == "GRANTED")
    assert granted["session"] == A and granted["principal"] == "rosen"
    dropped = next(l for l in lines if l["action"] == "DROPPED")
    assert dropped["session"] == A and dropped["principal"] == "rosen"


def test_no_session_key_fails_closed(elev):
    assert elev.is_elevated(None) is False
    assert "error" in elev.grant(None, reason="x")


def test_handle_routes_with_session_key(elev):
    A = "mcp:h"
    elev.handle("mcp_elevate", {"reason": "r", "ttl": 300}, key=A, principal="rosen")
    assert elev.handle("mcp_elevation_status", {}, key=A)["elevated"] is True
    assert elev.handle("mcp_elevation_status", {}, key="mcp:other")["elevated"] is False
    elev.handle("mcp_drop_elevation", {}, key=A)
    assert elev.is_elevated(A) is False
