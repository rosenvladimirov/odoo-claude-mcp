"""3.3.9: refresh tokens — the connector stops falling out every day.

Before this, `/oauth/token` handed out a 24h access token and nothing else, so
claude.ai had to re-run the whole authorize flow once a day (measured on
mcp.ussmed.com: 401 + /oauth/register + /oauth/token on 11.09, 12.09, 13.09 and
three times within 13 minutes on 15.09). Issued tokens also lived only in
memory, so a pod restart logged every client out.

What is pinned here:
  1. a refresh token works exactly once and is rotated on use;
  2. it survives a restart (only its sha256 is persisted);
  3. the token itself never reaches the disk;
  4. an unwritable store degrades to memory, it must not raise.

Like the 3.3.6 tests, the helpers are lifted out of server.py by source instead
of importing it — importing pulls in the whole MCP stack.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets_mod
import threading as _threading_mod
import time as _time_mod
from pathlib import Path
from typing import Any

import pytest

SERVER_PY = Path(__file__).resolve().parent.parent / "server.py"
_FUNCS = (
    "_oauth_refresh_ttl",
    "_oauth_refresh_store_path",
    "_oauth_refresh_hash",
    "_oauth_refresh_load",
    "_oauth_refresh_save",
    "_oauth_refresh_issue",
    "_oauth_refresh_consume",
)


class _SilentLogger:
    def warning(self, *args, **kwargs):
        pass

    info = warning


@pytest.fixture()
def srv(tmp_path, monkeypatch) -> dict:
    """Namespace with just the 3.3.9 refresh helpers and a per-test store."""
    src = SERVER_PY.read_text(encoding="utf-8")
    ns: dict[str, Any] = {
        "os": os, "json": json, "logger": _SilentLogger(), "Any": Any,
        "_secrets_mod": _secrets_mod, "_time_mod": _time_mod,
        "_threading_mod": _threading_mod,
        "_oauth_refresh": {}, "_oauth_refresh_lock": _threading_mod.Lock(),
    }
    for fn in _FUNCS:
        match = re.search(rf"^def {fn}\(.*?(?=^\S)", src, re.S | re.M)
        assert match, f"{fn} missing from server.py — did the 3.3.9 fix get reverted?"
        exec(compile(match.group(0), fn, "exec"), ns)  # noqa: S102 - our own source
    monkeypatch.setenv("MCP_OAUTH_REFRESH_STORE", str(tmp_path / "oauth_refresh.json"))
    monkeypatch.delenv("MCP_OAUTH_REFRESH_TTL", raising=False)
    return ns


# ── single use + rotation ──────────────────────────────────────────────────

def test_fresh_token_is_accepted(srv):
    assert srv["_oauth_refresh_consume"](srv["_oauth_refresh_issue"]()) is True


def test_second_use_is_refused(srv):
    """Rotation: a replayed refresh token must not mint another access token."""
    token = srv["_oauth_refresh_issue"]()
    assert srv["_oauth_refresh_consume"](token) is True
    assert srv["_oauth_refresh_consume"](token) is False


def test_two_tokens_are_independent(srv):
    first, second = srv["_oauth_refresh_issue"](), srv["_oauth_refresh_issue"]()
    assert srv["_oauth_refresh_consume"](first) is True
    assert srv["_oauth_refresh_consume"](second) is True


@pytest.mark.parametrize("value", ["", None, "not-a-token"])
def test_unknown_tokens_refused(srv, value):
    assert srv["_oauth_refresh_consume"](value) is False


def test_expired_token_refused(srv):
    token = srv["_oauth_refresh_issue"]()
    srv["_oauth_refresh"].clear()
    srv["_oauth_refresh_save"]({srv["_oauth_refresh_hash"](token): _time_mod.time() - 1})
    assert srv["_oauth_refresh_consume"](token) is False


# ── survives a restart ─────────────────────────────────────────────────────

def test_token_survives_process_restart(srv):
    """THE point of 3.3.9: a pod restart used to log every connector out."""
    token = srv["_oauth_refresh_issue"]()
    srv["_oauth_refresh"].clear()          # what a restart does to the memory store
    assert srv["_oauth_refresh_consume"](token) is True


def test_restart_does_not_resurrect_a_used_token(srv):
    token = srv["_oauth_refresh_issue"]()
    assert srv["_oauth_refresh_consume"](token) is True
    srv["_oauth_refresh"].clear()
    assert srv["_oauth_refresh_consume"](token) is False


# ── nothing usable on disk ─────────────────────────────────────────────────

def test_only_the_hash_reaches_the_disk(srv):
    token = srv["_oauth_refresh_issue"]()
    written = Path(srv["_oauth_refresh_store_path"]()).read_text(encoding="utf-8")
    assert token not in written
    assert srv["_oauth_refresh_hash"](token) in written


def test_store_is_private(srv):
    srv["_oauth_refresh_issue"]()
    mode = Path(srv["_oauth_refresh_store_path"]()).stat().st_mode & 0o777
    assert mode == 0o600


def test_unwritable_store_degrades_to_memory(srv, monkeypatch):
    """No /data mount must not break the OAuth flow, only its persistence."""
    monkeypatch.setenv("MCP_OAUTH_REFRESH_STORE", "/nonexistent-dir/oauth_refresh.json")
    token = srv["_oauth_refresh_issue"]()
    assert srv["_oauth_refresh_consume"](token) is True


def test_corrupt_store_is_ignored(srv):
    path = Path(srv["_oauth_refresh_store_path"]())
    path.write_text("{ this is not json", encoding="utf-8")
    token = srv["_oauth_refresh_issue"]()
    assert srv["_oauth_refresh_consume"](token) is True


# ── lifetime ───────────────────────────────────────────────────────────────

def test_default_ttl_is_90_days(srv):
    assert srv["_oauth_refresh_ttl"]() == 90 * 86400


@pytest.mark.parametrize("value,expected", [
    ("3600", 3600), ("0", 60), ("-5", 60), ("junk", 90 * 86400), ("", 90 * 86400),
])
def test_ttl_override(srv, monkeypatch, value, expected):
    monkeypatch.setenv("MCP_OAUTH_REFRESH_TTL", value)
    assert srv["_oauth_refresh_ttl"]() == expected
