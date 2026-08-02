"""Unit tests for session_store — strict per-session state store (v2.30.0).

Covers:
  * create/resolve roundtrip, touch, sliding TTL expiry
  * bind_principal (set / idempotent / mismatch → orphan)
  * revoke, mark_all_active_orphaned
  * session_state roundtrip + CASCADE on purge
  * cleanup (newly_orphaned + retention purge)
  * phone_refcount JOIN
  * concurrent writes (16 workers × 200 ops)
  * SessionRuntime cache + evict callback
"""
from __future__ import annotations

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session_store import (  # noqa: E402
    SessionRow,
    SessionRuntime,
    SessionStore,
    SessionPrincipalMismatch,
    _utcnow,
)


def make_store(tmp_path, **kwargs) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db", **kwargs)


def row_of(store, session_key) -> dict | None:
    """Fetch the raw sessions row regardless of status."""
    for r in store.list_sessions():
        if r["session_key"] == session_key:
            return r
    return None


# ─── create / resolve / touch ────────────────────────────────
def test_create_resolve_roundtrip(tmp_path):
    store = make_store(tmp_path)
    created = store.create("mcp:abc", "streamable_http")
    assert isinstance(created, SessionRow)
    assert created.status == "active"

    row = store.resolve("mcp:abc")
    assert row is not None
    assert row.session_key == "mcp:abc"
    assert row.transport == "streamable_http"
    assert row.principal is None
    assert row.expires_at > row.created_at


def test_resolve_unknown_returns_none(tmp_path):
    store = make_store(tmp_path)
    assert store.resolve("mcp:nope") is None


def test_create_race_returns_existing(tmp_path):
    store = make_store(tmp_path)
    first = store.create("mcp:dup", "streamable_http", principal="rosen",
                         principal_src="identify")
    again = store.create("mcp:dup", "streamable_http")
    assert again.session_key == first.session_key
    assert again.principal == "rosen"  # the existing row won


def test_create_over_orphaned_key_revives(tmp_path):
    # A dead-but-not-revoked key must SELF-HEAL on reconnect: the transport
    # session id was already validated by the SDK, so create() revives it
    # instead of wedging the client on MCP_SESSION_ORPHANED.
    store = make_store(tmp_path)
    store.create("mcp:dead", "streamable_http", principal="rosen",
                 principal_src="identify")
    store.mark_orphaned("mcp:dead", "server_restart")
    revived = store.create("mcp:dead", "streamable_http", principal="rosen",
                           principal_src="identify")
    assert isinstance(revived, SessionRow)
    assert revived.status == "active"
    assert store.resolve("mcp:dead") is not None
    raw = row_of(store, "mcp:dead")
    assert raw["status"] == "active"
    assert raw["orphaned_at"] is None
    assert raw["orphan_reason"] is None


def test_revive_drops_connection_and_web_state(tmp_path):
    # Reviving must not let the fresh session inherit a prior principal's
    # Odoo/web session; telegram state is intentionally preserved (its live
    # client was already torn down at orphan time and phone refcounts are shared).
    store = make_store(tmp_path)
    store.create("mcp:reuse", "streamable_http", principal="a")
    store.set_state("mcp:reuse", "connection", "active", {"alias": "x"})
    store.set_state("mcp:reuse", "web", "default", {"sid": "y"})
    store.set_state("mcp:reuse", "telegram", "phone", {"phone": "+359"})
    store.mark_orphaned("mcp:reuse", "ttl_expired")
    store.create("mcp:reuse", "streamable_http", principal="b")
    assert store.get_state("mcp:reuse", "connection", "active") is None
    assert store.get_state("mcp:reuse", "web", "default") is None
    assert store.get_state("mcp:reuse", "telegram", "phone") is not None


def test_create_over_revoked_key_returns_none(tmp_path):
    # An admin revoke is sticky — a reconnect must NEVER auto-revive it.
    store = make_store(tmp_path)
    store.create("mcp:killed", "streamable_http")
    store.revoke("mcp:killed", "admin_revoke")
    assert store.create("mcp:killed", "streamable_http") is None


def test_touch_extends_expiry(tmp_path):
    store = make_store(tmp_path)
    store.create("sse:t1", "sse")
    before = store.resolve("sse:t1")
    time.sleep(0.01)
    store.touch("sse:t1")
    after = store.resolve("sse:t1")
    assert after.expires_at > before.expires_at
    assert after.last_seen > before.last_seen


# ─── TTL expiry ──────────────────────────────────────────────
def test_ttl_expiry_marks_orphaned(tmp_path):
    store = make_store(tmp_path, ttl_seconds=0)
    store.create("mcp:exp", "streamable_http")
    time.sleep(0.01)
    assert store.resolve("mcp:exp") is None
    row = row_of(store, "mcp:exp")
    assert row["status"] == "orphaned"
    assert row["orphan_reason"] == "ttl_expired"
    assert row["orphaned_at"] is not None


# ─── bind_principal ──────────────────────────────────────────
def test_bind_principal_sets_null(tmp_path):
    store = make_store(tmp_path)
    store.create("mcp:b1", "streamable_http")
    store.bind_principal("mcp:b1", "rosen", "unified_auth", auth_fp="fp1")
    row = store.resolve("mcp:b1")
    assert row.principal == "rosen"
    assert row.principal_src == "unified_auth"


def test_bind_principal_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.create("mcp:b2", "streamable_http")
    store.bind_principal("mcp:b2", "rosen", "identify")
    store.bind_principal("mcp:b2", "rosen", "identify")  # no raise
    assert store.resolve("mcp:b2").principal == "rosen"


def test_bind_principal_mismatch_orphans(tmp_path):
    store = make_store(tmp_path)
    store.create("mcp:b3", "streamable_http")
    store.bind_principal("mcp:b3", "rosen", "identify")
    with pytest.raises(SessionPrincipalMismatch) as exc:
        store.bind_principal("mcp:b3", "lyubo", "identify")
    assert exc.value.error_code == "MCP_SESSION_PRINCIPAL_MISMATCH"
    assert exc.value.to_dict()["error_code"] == "MCP_SESSION_PRINCIPAL_MISMATCH"
    assert store.resolve("mcp:b3") is None
    row = row_of(store, "mcp:b3")
    assert row["status"] == "orphaned"
    assert row["orphan_reason"] == "principal_mismatch"


# ─── revoke / mark_all_active_orphaned ───────────────────────
def test_revoke(tmp_path):
    store = make_store(tmp_path)
    store.create("mcp:r1", "streamable_http")
    store.revoke("mcp:r1")
    assert store.resolve("mcp:r1") is None
    row = row_of(store, "mcp:r1")
    assert row["status"] == "revoked"
    assert row["orphan_reason"] == "admin_revoke"


def test_mark_all_active_orphaned(tmp_path):
    store = make_store(tmp_path)
    store.create("mcp:a", "streamable_http")
    store.create("mcp:b", "streamable_http")
    store.create("mcp:c", "streamable_http")
    store.revoke("mcp:c")

    keys = store.mark_all_active_orphaned()
    assert sorted(keys) == ["mcp:a", "mcp:b"]
    for k in ("mcp:a", "mcp:b"):
        row = row_of(store, k)
        assert row["status"] == "orphaned"
        assert row["orphan_reason"] == "server_restart"
    # the revoked one stays revoked
    assert row_of(store, "mcp:c")["status"] == "revoked"


# ─── session_state ───────────────────────────────────────────
def test_state_roundtrip_and_delete(tmp_path):
    store = make_store(tmp_path)
    store.create("mcp:s1", "streamable_http")

    assert store.get_state("mcp:s1", "connection", "active") is None
    store.set_state("mcp:s1", "connection", "active",
                    {"source": "user_profile", "user": "rosen", "alias": "prod"})
    val = store.get_state("mcp:s1", "connection", "active")
    assert val == {"source": "user_profile", "user": "rosen", "alias": "prod"}

    # UPSERT overwrites
    store.set_state("mcp:s1", "connection", "active", {"source": "inline"})
    assert store.get_state("mcp:s1", "connection", "active") == {"source": "inline"}

    # single-key delete
    store.set_state("mcp:s1", "web", "prod", {"uid": 2})
    store.set_state("mcp:s1", "web", "test", {"uid": 7})
    store.delete_state("mcp:s1", "web", "prod")
    assert store.get_state("mcp:s1", "web", "prod") is None
    assert store.get_state("mcp:s1", "web", "test") == {"uid": 7}

    # namespace-wide delete
    store.delete_state("mcp:s1", "web")
    assert store.get_state("mcp:s1", "web", "test") is None
    assert store.get_state("mcp:s1", "connection", "active") == {"source": "inline"}


def test_cascade_on_purge(tmp_path):
    store = make_store(tmp_path, retention_days=0)
    store.create("mcp:cas", "streamable_http")
    store.set_state("mcp:cas", "connection", "active", {"source": "inline"})
    store.set_state("mcp:cas", "telegram", "phone", {"phone": "+359888"})
    store.mark_orphaned("mcp:cas", "admin_revoke")

    result = store.cleanup(now=_utcnow() + timedelta(seconds=1))
    assert "mcp:cas" in result["purged"]

    # state rows must be gone via ON DELETE CASCADE
    with sqlite3.connect(tmp_path / "sessions.db") as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM session_state WHERE session_key = 'mcp:cas'"
        ).fetchone()[0]
    assert count == 0


# ─── cleanup ─────────────────────────────────────────────────
def test_cleanup_orphans_and_purges(tmp_path):
    db = tmp_path / "sessions.db"
    expired_store = SessionStore(db, ttl_seconds=0, retention_days=0)
    live_store = SessionStore(db, ttl_seconds=3600, retention_days=0)

    expired_store.create("mcp:old", "streamable_http")
    live_store.create("mcp:new", "streamable_http")
    time.sleep(0.01)

    result = live_store.cleanup()
    assert result["newly_orphaned"] == ["mcp:old"]
    assert result["purged"] == []
    assert live_store.resolve("mcp:new") is not None

    # second pass with retention 0 purges the orphaned row
    result = live_store.cleanup(now=_utcnow() + timedelta(seconds=1))
    assert result["purged"] == ["mcp:old"]
    assert row_of(live_store, "mcp:old") is None


# ─── phone_refcount ──────────────────────────────────────────
def test_phone_refcount(tmp_path):
    store = make_store(tmp_path)
    store.create("mcp:p1", "streamable_http", principal="rosen",
                 principal_src="identify")
    store.create("mcp:p2", "streamable_http", principal="rosen",
                 principal_src="identify")
    store.create("mcp:p3", "streamable_http", principal="lyubo",
                 principal_src="identify")
    store.set_state("mcp:p1", "telegram", "phone", {"phone": "+359888"})
    store.set_state("mcp:p2", "telegram", "phone", {"phone": "+359888"})
    store.set_state("mcp:p3", "telegram", "phone", {"phone": "+359888"})

    assert store.phone_refcount("rosen", "+359888") == 2
    assert store.phone_refcount("rosen", "+359777") == 0
    assert store.phone_refcount("lyubo", "+359888") == 1

    store.mark_orphaned("mcp:p1", "ttl_expired")
    assert store.phone_refcount("rosen", "+359888") == 1


# ─── concurrency ─────────────────────────────────────────────
def test_concurrent_writes(tmp_path):
    store = make_store(tmp_path)
    n_ops = 200

    def op(i):
        key = f"mcp:conc-{i}"
        store.create(key, "streamable_http", principal="rosen",
                     principal_src="identify")
        store.touch(key)
        store.set_state(key, "connection", "active", {"i": i})
        return key

    with ThreadPoolExecutor(max_workers=16) as pool:
        keys = list(pool.map(op, range(n_ops)))  # raises if any op failed

    assert len(keys) == n_ops
    sessions = store.list_sessions(principal="rosen")
    assert len(sessions) == n_ops
    assert all(s["state_count"] == 1 for s in sessions)


# ─── SessionRuntime ──────────────────────────────────────────
def test_runtime_conn_and_web(tmp_path):
    rt = SessionRuntime()
    conn_obj, web_obj = object(), object()

    assert rt.get_conn("mcp:x") is None
    rt.set_conn("mcp:x", conn_obj)
    assert rt.get_conn("mcp:x") is conn_obj
    assert rt.pop_conn("mcp:x") is conn_obj
    assert rt.get_conn("mcp:x") is None
    assert rt.pop_conn("mcp:x") is None

    rt.set_web("mcp:x", "prod", web_obj)
    assert rt.get_web("mcp:x", "prod") is web_obj
    assert rt.get_web("mcp:x", "other") is None
    assert rt.pop_web("mcp:x", "prod") is web_obj
    assert rt.get_web("mcp:x", "prod") is None


def test_runtime_evict_with_callback(tmp_path):
    rt = SessionRuntime()
    rt.set_conn("mcp:e1", "conn-e1")
    rt.set_conn("mcp:e2", "conn-e2")
    rt.set_web("mcp:e1", "prod", "web-e1-prod")
    rt.set_web("mcp:e1", "test", "web-e1-test")
    rt.set_web("mcp:e3", "prod", "web-e3")

    seen = []
    rt.evict(["mcp:e1"], on_evict=lambda kind, key, obj: seen.append((kind, key, obj)))

    assert ("conn", "mcp:e1", "conn-e1") in seen
    assert ("web", ("mcp:e1", "prod"), "web-e1-prod") in seen
    assert ("web", ("mcp:e1", "test"), "web-e1-test") in seen
    assert len(seen) == 3
    # untouched sessions stay cached
    assert rt.get_conn("mcp:e2") == "conn-e2"
    assert rt.get_web("mcp:e3", "prod") == "web-e3"
    assert rt.get_conn("mcp:e1") is None


def test_runtime_evict_callback_errors_swallowed(tmp_path):
    rt = SessionRuntime()
    rt.set_conn("mcp:err", "conn-err")

    def boom(kind, key, obj):
        raise RuntimeError("teardown failed")

    rt.evict(["mcp:err"], on_evict=boom)  # must not raise
    assert rt.get_conn("mcp:err") is None
