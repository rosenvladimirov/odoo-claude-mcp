"""
odoo_sql_query backend — direct PostgreSQL execution with savepoint isolation.

Wired into server.py as a single tool. Reads connection params from
MCP_PG_* env vars (lazy connect on first call). Each call runs in a
savepoint; on any error the savepoint rolls back and the parent
transaction stays clean.

Security:
  Classifier (sql_classifier.classify_sql) decides USER vs ADMIN role.
  In USER mode the wrapper refuses queries whose role_required == 'admin'.
  In ADMIN mode classifier output is informational only.

  For v2 USER deployments, configure MCP_PG_USER to a PostgreSQL role with
  GRANT SELECT only (defense in depth — the DB itself rejects writes even
  if the classifier missed something).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import sql_classifier

logger = logging.getLogger("sql_executor")

# Lazy global connection (psycopg connection objects are not thread-safe;
# we use a per-call cursor to keep things simple).
_conn = None
_conn_error: str | None = None


def _get_conn():
    """Lazy psycopg connection. Returns conn or raises with a friendly message."""
    global _conn, _conn_error
    if _conn is not None:
        return _conn
    try:
        import psycopg
    except ImportError as e:
        _conn_error = f"psycopg not installed: {e}"
        raise RuntimeError(_conn_error)

    host = os.environ.get("MCP_PG_HOST", "").strip()
    db = os.environ.get("MCP_PG_DB", "").strip()
    user = os.environ.get("MCP_PG_USER", "").strip()
    if not (host and db and user):
        _conn_error = (
            "odoo_sql_query requires MCP_PG_HOST, MCP_PG_DB, MCP_PG_USER "
            "(and usually MCP_PG_PASSWORD) env vars to be set on the v3 "
            "container."
        )
        raise RuntimeError(_conn_error)

    port = int(os.environ.get("MCP_PG_PORT", "5432"))
    password = os.environ.get("MCP_PG_PASSWORD", "")

    _conn = psycopg.connect(
        host=host, port=port, dbname=db, user=user, password=password,
        autocommit=False,                              # explicit tx control
        application_name="odoo-mcp-v3-sql-executor",
        connect_timeout=10,
    )
    logger.info("sql_executor: connected to %s:%s/%s as %s", host, port, db, user)
    return _conn


def execute(query: str, params: list | None = None,
            fetch: bool = True, timeout: int = 30,
            role: str = "admin") -> dict:
    """Run a SQL query under savepoint protection + classifier check.

    Returns a dict — never raises. Every failure path returns
    {'error': ..., 'reason': ...} so the MCP wrapper can serialise it.
    """
    info = sql_classifier.classify_sql(query)

    # Role gate.
    if role != "admin" and info["role_required"] == "admin":
        return {
            "error": "denied",
            "reason": "protected_table" if info["protected_hits"] else "ddl_or_unparseable",
            "op": info["op"],
            "tables": info["tables"],
            "protected_hits": info["protected_hits"],
            "hint": (
                "Use mcp_elevate(reason='...') to gain temporary admin rights, "
                "or rephrase the query to avoid protected tables / DDL."
            ),
        }

    # Connection.
    try:
        conn = _get_conn()
    except RuntimeError as e:
        return {"error": "no_connection", "reason": str(e)}

    started = time.time()
    rows: list[dict] | None = None
    rowcount = -1

    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT mcp_sql")
            try:
                if timeout and timeout > 0:
                    cur.execute("SET LOCAL statement_timeout = %s",
                                (timeout * 1000,))
                cur.execute(query, params or [])
                rowcount = cur.rowcount
                if fetch and cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                cur.execute("RELEASE SAVEPOINT mcp_sql")
                conn.commit()
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT mcp_sql")
                conn.rollback()
                logger.warning("sql_executor: query failed: %s", e)
                return {
                    "error": "execution_failed",
                    "reason": str(e),
                    "op": info["op"],
                    "tables": info["tables"],
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
    except Exception as e:
        # Connection-level error (e.g. connection lost). Force re-connect next call.
        global _conn
        _conn = None
        return {"error": "connection_failed", "reason": str(e)}

    elapsed = int((time.time() - started) * 1000)
    return {
        "op": info["op"],
        "tables": info["tables"],
        "protected_hits": info["protected_hits"],
        "role_required": info["role_required"],
        "rowcount": rowcount,
        "rows": rows,
        "elapsed_ms": elapsed,
    }


def get_tool_def():
    """Returns the MCP Tool definition for odoo_sql_query."""
    from mcp.types import Tool
    return Tool(
        name="odoo_sql_query",
        description=(
            "Execute a raw PostgreSQL query against the Odoo database with "
            "savepoint isolation. SELECT is allowed for any role. "
            "INSERT/UPDATE/DELETE on non-protected tables is allowed for USER. "
            "Writes touching protected tables (res_users, res_company, "
            "ir_module_module, account.account, …) and any DDL require "
            "ADMIN role (use mcp_elevate first). Connection params come "
            "from MCP_PG_HOST/PORT/DB/USER/PASSWORD env. Returns "
            "{op, tables, protected_hits, role_required, rowcount, rows, elapsed_ms} "
            "on success, {error, reason, …} on denial or failure."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SQL statement"},
                "params": {
                    "type": "array",
                    "description": "Parameterised values for %s placeholders",
                    "default": [],
                },
                "fetch": {
                    "type": "boolean",
                    "description": "Return rows for SELECT (ignored for writes)",
                    "default": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Statement timeout in seconds",
                    "default": 30,
                },
            },
            "required": ["query"],
        },
    )
