"""Strict per-session state store for the MCP server (v2.30.0).

SQLite-backed source of truth for session identity and declarative state
(principal, active connection descriptor, telegram phone, web cookies),
plus an in-process SessionRuntime cache for live objects.
Fail-closed: no fallbacks — absence of session data is an error.
"""

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("session_store")

# ─── Defaults (env-overridable) ──────────────────────────────
DEFAULT_TTL = int(os.environ.get("MCP_SESSION_TTL", "86400"))
DEFAULT_RETENTION_DAYS = int(os.environ.get("MCP_SESSION_RETENTION_DAYS", "7"))
DEFAULT_DB_PATH = os.environ.get("MCP_SESSIONS_DB", "/data/mcp_sessions.db")


# ─── Exceptions ──────────────────────────────────────────────
class SessionError(Exception):
    """Base error for session failures — carries a stable error_code
    and a how_to_fix hint, serializable as a normal tool result."""

    def __init__(self, error_code: str, message: str, how_to_fix: str = ""):
        self.error_code = error_code
        self.how_to_fix = how_to_fix
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "error": str(self),
            "error_code": self.error_code,
            "how_to_fix": self.how_to_fix,
        }


class SessionNotFound(SessionError):
    """No usable session row for the given session key."""

    def __init__(self, message: str, how_to_fix: str = ""):
        super().__init__("MCP_NO_SESSION", message, how_to_fix)


class SessionPrincipalMismatch(SessionError):
    """The session is already bound to a different principal."""

    def __init__(self, message: str, how_to_fix: str = ""):
        super().__init__("MCP_SESSION_PRINCIPAL_MISMATCH", message, how_to_fix)


# ─── Row snapshot ────────────────────────────────────────────
@dataclass(frozen=True)
class SessionRow:
    """Immutable snapshot of a sessions row."""

    session_key: str
    transport: str
    principal: str | None
    principal_src: str | None
    status: str
    created_at: str
    last_seen: str
    expires_at: str
    orphaned_at: str | None
    orphan_reason: str | None


# ─── Schema migrations ───────────────────────────────────────
# Each entry: (version, sql_script). Applied sequentially inside _init_db
# for every version greater than the stored schema_meta.schema_version.
_SCHEMA_V1 = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions (
    session_key   TEXT PRIMARY KEY,           -- 'mcp:<hex>' | 'sse:<hex>'
    transport     TEXT NOT NULL CHECK (transport IN ('streamable_http','sse')),
    principal     TEXT,                       -- mcp_user safe name; NULL until bind
    principal_src TEXT CHECK (principal_src IN ('unified_auth','identify')),
    auth_fp       TEXT,                       -- sha256(url|db|login|api_key) for unified_auth
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','orphaned','revoked')),
    created_at    TEXT NOT NULL, last_seen TEXT NOT NULL, expires_at TEXT NOT NULL,
    orphaned_at   TEXT, orphan_reason TEXT,   -- 'ttl_expired'|'server_restart'|'admin_revoke'|'principal_mismatch'
    client_info   TEXT                        -- JSON {user_agent, remote}
);
CREATE INDEX idx_sessions_principal ON sessions(principal);
CREATE INDEX idx_sessions_status_exp ON sessions(status, expires_at);
CREATE TABLE session_state (
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
    namespace   TEXT NOT NULL,   -- 'connection' | 'telegram' | 'web'
    key         TEXT NOT NULL,   -- 'active' | 'phone' | <alias>
    value       TEXT NOT NULL,   -- JSON
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (session_key, namespace, key)
);
"""

_MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── SessionStore ────────────────────────────────────────────
class SessionStore:
    """SQLite source of truth for MCP sessions and their declarative state.

    Connection-per-call; ISO-8601 UTC timestamps compared lexicographically.
    """

    def __init__(self, db_path, ttl_seconds: int = DEFAULT_TTL,
                 retention_days: int = DEFAULT_RETENTION_DAYS):
        self.db_path = Path(db_path)
        self.ttl_seconds = ttl_seconds
        self.retention_days = retention_days
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        try:
            os.chmod(self.db_path, 0o600)  # web cookies are secrets at rest
        except OSError:
            pass

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                current = int(row["value"]) if row else 0
            except sqlite3.OperationalError:
                current = 0  # fresh database, schema_meta does not exist yet
            for version, script in _MIGRATIONS:
                if version <= current:
                    continue
                conn.executescript(script)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) "
                    "VALUES ('schema_version', ?)",
                    (str(version),),
                )
                conn.commit()
                logger.info(f"session_store: schema migrated to v{version}")

    @staticmethod
    def _row_to_session(row) -> SessionRow:
        return SessionRow(
            session_key=row["session_key"],
            transport=row["transport"],
            principal=row["principal"],
            principal_src=row["principal_src"],
            status=row["status"],
            created_at=row["created_at"],
            last_seen=row["last_seen"],
            expires_at=row["expires_at"],
            orphaned_at=row["orphaned_at"],
            orphan_reason=row["orphan_reason"],
        )

    # ── Lifecycle ────────────────────────────────────────────
    def create(self, session_key: str, transport: str, *, principal: str = None,
               principal_src: str = None, auth_fp: str = None,
               client_info: dict = None) -> SessionRow | None:
        """Insert a new active session. On a race with an existing row,
        return resolve() instead — i.e. the live row, or None when an
        orphaned/revoked row holds this key (fail-closed for the caller)."""
        now = _utcnow()
        now_s = now.isoformat()
        exp_s = (now + timedelta(seconds=self.ttl_seconds)).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions
                    (session_key, transport, principal, principal_src, auth_fp,
                     status, created_at, last_seen, expires_at, client_info)
                    VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        session_key, transport, principal, principal_src, auth_fp,
                        now_s, now_s, exp_s,
                        json.dumps(client_info) if client_info is not None else None,
                    ),
                )
        except sqlite3.IntegrityError:
            # Race: the row already exists — defer to resolve() (None when
            # the key is held by an orphaned/revoked row)
            return self.resolve(session_key)
        return SessionRow(
            session_key=session_key, transport=transport,
            principal=principal, principal_src=principal_src,
            status="active", created_at=now_s, last_seen=now_s,
            expires_at=exp_s, orphaned_at=None, orphan_reason=None,
        )

    def resolve(self, session_key: str) -> SessionRow | None:
        """Return the live session row, or None if missing, not active,
        or expired (an expired-but-active row is marked orphaned)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        if row is None:
            return None
        if row["status"] != "active":
            return None
        if row["expires_at"] < _utcnow().isoformat():
            self.mark_orphaned(session_key, "ttl_expired")
            return None
        return self._row_to_session(row)

    def touch(self, session_key: str):
        """Slide the TTL window: last_seen=now, expires_at=now+ttl."""
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen = ?, expires_at = ? "
                "WHERE session_key = ?",
                (
                    now.isoformat(),
                    (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
                    session_key,
                ),
            )

    def bind_principal(self, session_key: str, principal: str, src: str,
                       auth_fp: str = None):
        """Bind a principal to the session. Idempotent for the same principal;
        a different principal orphans the session and raises."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT principal FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is None:
                raise SessionNotFound(
                    f"Session '{session_key}' not found.",
                    how_to_fix=(
                        "Re-initialize the MCP connection (the client must "
                        "echo the Mcp-Session-Id header)."
                    ),
                )
            current = row["principal"]
            if current is None:
                conn.execute(
                    "UPDATE sessions SET principal = ?, principal_src = ?, "
                    "auth_fp = ? WHERE session_key = ?",
                    (principal, src, auth_fp, session_key),
                )
                return
            if current == principal:
                return  # idempotent re-bind
            # Different principal — poison the session and refuse
            conn.execute(
                "UPDATE sessions SET status = 'orphaned', orphaned_at = ?, "
                "orphan_reason = 'principal_mismatch' WHERE session_key = ?",
                (_utcnow().isoformat(), session_key),
            )
            conn.commit()
            raise SessionPrincipalMismatch(
                f"Session is already bound to principal '{current}'; "
                f"cannot rebind to '{principal}'.",
                how_to_fix="Reconnect the MCP client to start a fresh session.",
            )

    def revoke(self, session_key: str, reason: str = "admin_revoke"):
        """Hard-revoke a session (admin action)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'revoked', orphaned_at = ?, "
                "orphan_reason = ? WHERE session_key = ?",
                (_utcnow().isoformat(), reason, session_key),
            )

    def mark_orphaned(self, session_key: str, reason: str):
        """Mark an active session as orphaned (no-op for non-active rows)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'orphaned', orphaned_at = ?, "
                "orphan_reason = ? WHERE session_key = ? AND status = 'active'",
                (_utcnow().isoformat(), reason, session_key),
            )

    def mark_all_active_orphaned(self, reason: str = "server_restart") -> list[str]:
        """Orphan every active session (server startup). Returns affected keys."""
        with self._connect() as conn:
            keys = [
                r["session_key"]
                for r in conn.execute(
                    "SELECT session_key FROM sessions WHERE status = 'active'"
                )
            ]
            if keys:
                conn.execute(
                    "UPDATE sessions SET status = 'orphaned', orphaned_at = ?, "
                    "orphan_reason = ? WHERE status = 'active'",
                    (_utcnow().isoformat(), reason),
                )
        return keys

    # ── Declarative state ────────────────────────────────────
    def get_state(self, session_key: str, namespace: str, key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM session_state "
                "WHERE session_key = ? AND namespace = ? AND key = ?",
                (session_key, namespace, key),
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_state(self, session_key: str, namespace: str, key: str, value):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_state (session_key, namespace, key, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_key, namespace, key)
                DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (session_key, namespace, key, json.dumps(value),
                 _utcnow().isoformat()),
            )

    def list_state(self, session_key: str, namespace: str = None) -> list[dict]:
        """List state rows for a session as [{namespace, key, value, updated_at}]."""
        with self._connect() as conn:
            if namespace is None:
                rows = conn.execute(
                    "SELECT namespace, key, value, updated_at FROM session_state "
                    "WHERE session_key = ? ORDER BY namespace, key",
                    (session_key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT namespace, key, value, updated_at FROM session_state "
                    "WHERE session_key = ? AND namespace = ? ORDER BY key",
                    (session_key, namespace),
                ).fetchall()
        return [{"namespace": r["namespace"], "key": r["key"],
                 "value": json.loads(r["value"]), "updated_at": r["updated_at"]}
                for r in rows]

    def delete_state(self, session_key: str, namespace: str, key: str = None):
        """Delete one state key, or the whole namespace when key is None."""
        with self._connect() as conn:
            if key is None:
                conn.execute(
                    "DELETE FROM session_state "
                    "WHERE session_key = ? AND namespace = ?",
                    (session_key, namespace),
                )
            else:
                conn.execute(
                    "DELETE FROM session_state "
                    "WHERE session_key = ? AND namespace = ? AND key = ?",
                    (session_key, namespace, key),
                )

    # ── Introspection / maintenance ──────────────────────────
    def list_sessions(self, principal: str = None,
                      include_orphaned: bool = True) -> list[dict]:
        """List session rows as dicts with a state_count of their state rows."""
        sql = (
            "SELECT s.*, (SELECT COUNT(*) FROM session_state st "
            "WHERE st.session_key = s.session_key) AS state_count "
            "FROM sessions s"
        )
        where, params = [], []
        if principal is not None:
            where.append("s.principal = ?")
            params.append(principal)
        if not include_orphaned:
            where.append("s.status = 'active'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY s.last_seen DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def cleanup(self, now: datetime = None) -> dict:
        """Reaper pass: orphan TTL-expired active sessions and purge
        orphaned/revoked rows past retention (CASCADE drops their state)."""
        if now is None:
            now = _utcnow()
        now_s = now.isoformat()
        cutoff_s = (now - timedelta(days=self.retention_days)).isoformat()
        with self._connect() as conn:
            newly = [
                r["session_key"]
                for r in conn.execute(
                    "SELECT session_key FROM sessions "
                    "WHERE status = 'active' AND expires_at < ?",
                    (now_s,),
                )
            ]
            if newly:
                conn.execute(
                    "UPDATE sessions SET status = 'orphaned', orphaned_at = ?, "
                    "orphan_reason = 'ttl_expired' "
                    "WHERE status = 'active' AND expires_at < ?",
                    (now_s, now_s),
                )
            purged = [
                r["session_key"]
                for r in conn.execute(
                    "SELECT session_key FROM sessions "
                    "WHERE status IN ('orphaned','revoked') AND orphaned_at < ?",
                    (cutoff_s,),
                )
            ]
            if purged:
                conn.execute(
                    "DELETE FROM sessions "
                    "WHERE status IN ('orphaned','revoked') AND orphaned_at < ?",
                    (cutoff_s,),
                )
        return {"newly_orphaned": newly, "purged": purged}

    def phone_refcount(self, principal: str, phone: str) -> int:
        """Count active sessions of this principal whose telegram/phone
        state value carries the given phone."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT st.value FROM session_state st
                JOIN sessions s ON s.session_key = st.session_key
                WHERE s.status = 'active' AND s.principal = ?
                  AND st.namespace = 'telegram' AND st.key = 'phone'
                """,
                (principal,),
            ).fetchall()
        count = 0
        for r in rows:
            try:
                val = json.loads(r["value"])
            except (ValueError, TypeError):
                continue
            if isinstance(val, dict) and val.get("phone") == phone:
                count += 1
        return count


# ─── SessionRuntime ──────────────────────────────────────────
class SessionRuntime:
    """In-process cache of live objects keyed by session.

    Holds OdooConnection per session_key and web sessions per
    (session_key, alias). Telegram clients live in TelegramRegistry —
    the runtime only evicts; actual teardown is injected via on_evict.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._conns: dict[str, object] = {}
        self._web: dict[tuple[str, str], object] = {}

    # ── Odoo connections ─────────────────────────────────────
    def get_conn(self, session_key: str):
        with self._lock:
            return self._conns.get(session_key)

    def set_conn(self, session_key: str, conn):
        with self._lock:
            self._conns[session_key] = conn

    def pop_conn(self, session_key: str):
        with self._lock:
            return self._conns.pop(session_key, None)

    # ── Web sessions ─────────────────────────────────────────
    def get_web(self, session_key: str, alias: str):
        with self._lock:
            return self._web.get((session_key, alias))

    def set_web(self, session_key: str, alias: str, web):
        with self._lock:
            self._web[(session_key, alias)] = web

    def pop_web(self, session_key: str, alias: str):
        with self._lock:
            return self._web.pop((session_key, alias), None)

    # ── Eviction ─────────────────────────────────────────────
    def evict(self, session_keys: list, on_evict=None):
        """Drop all cached objects for the given sessions.

        on_evict(kind, key, obj) is called for each removed entry
        (kind is 'conn' or 'web'); teardown is best-effort — callback
        exceptions are logged and swallowed.
        """
        keys = set(session_keys)
        with self._lock:
            conns = [(k, self._conns.pop(k)) for k in list(self._conns) if k in keys]
            webs = [(k, self._web.pop(k)) for k in list(self._web) if k[0] in keys]
        if on_evict is None:
            return
        for k, obj in conns:
            try:
                on_evict("conn", k, obj)
            except Exception as exc:
                logger.warning(f"session_store: evict callback failed for conn {k}: {exc}")
        for k, obj in webs:
            try:
                on_evict("web", k, obj)
            except Exception as exc:
                logger.warning(f"session_store: evict callback failed for web {k}: {exc}")
