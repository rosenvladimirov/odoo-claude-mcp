"""
SQL classifier for odoo_sql_query (v3 role-based security).

Parses an arbitrary SQL string with sqlglot (PostgreSQL dialect), extracts the
operation and affected tables, and decides whether the call is allowed for
USER role or requires ADMIN.

Role policy (matches project_mcp_security_role_model.md):
  SELECT                       → user
  INSERT/UPDATE/DELETE          → user IF no protected_hits, else admin
  CREATE/DROP/ALTER/TRUNCATE   → admin (always)
  parse error                  → admin (safe default)

Protected tables are derived from PROTECTED_FROM_UNLINK (model name with
dot → underscore: res.users → res_users). Override via env
MCP_PROTECTED_TABLES (CSV).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("sql_classifier")

# Mirrors PROTECTED_FROM_UNLINK from tool_security (model → table name).
DEFAULT_PROTECTED_TABLES = {
    # Tier 1 — system meta
    "res_company", "res_users", "res_groups",
    "ir_module_module", "ir_model", "ir_model_fields",
    "ir_model_access", "ir_cron", "ir_config_parameter",
    "ir_sequence", "ir_actions_server",
    # Tier 2 — accounting core
    "account_account", "account_journal",
    "account_tax", "account_tax_group", "account_fiscal_position",
    # Tier 3 — BG localization
    "l10n_bg_tax_office",
}


def _load_protected() -> set[str]:
    raw = os.environ.get("MCP_PROTECTED_TABLES", "").strip()
    if not raw:
        return set(DEFAULT_PROTECTED_TABLES)
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


PROTECTED_TABLES = _load_protected()

# NOTE: sqlglot uses lowercase concatenated keys (e.g. 'truncatetable',
# not 'truncate'). Be liberal: accept both prefix matches and isinstance
# checks via _ast_bucket() below.
DDL_KEYS = {"create", "drop", "alter", "truncate", "truncatetable",
            "rename", "renametable", "comment", "command"}
WRITE_KEYS = {"insert", "update", "delete", "merge", "upsert"}
READ_KEYS = {"select", "with", "union", "intersect", "except"}


def classify_sql(query: str) -> dict:
    """Return {op, tables, protected_hits, role_required, parse_error}.

    op = 'select' | 'insert' | 'update' | 'delete' | 'ddl' | 'unknown'
    tables = list of (lowercased) table names referenced
    protected_hits = subset of `tables` that match PROTECTED_TABLES
    role_required = 'user' | 'admin'
    parse_error = optional error string
    """
    if not query or not query.strip():
        return {
            "op": "unknown", "tables": [], "protected_hits": [],
            "role_required": "admin", "parse_error": "empty query",
        }

    try:
        import sqlglot
        from sqlglot import expressions as exp
    except ImportError as e:
        return {
            "op": "unknown", "tables": [], "protected_hits": [],
            "role_required": "admin",
            "parse_error": f"sqlglot not installed: {e}",
        }

    try:
        parsed = sqlglot.parse_one(query, dialect="postgres")
    except Exception as e:
        logger.warning("SQL parse failed: %s", e)
        return {
            "op": "unknown", "tables": [], "protected_hits": [],
            "role_required": "admin", "parse_error": str(e),
        }

    # Reject multi-statement up front. sqlglot and psycopg disagree on
    # statement boundaries (e.g. `SELECT 1 -- ;\n DELETE ...` is one harmless
    # SELECT to sqlglot but two live statements to PG), so the only safe
    # stance is single-statement-only for the gated path. Fail-closed.
    try:
        _stmts = [s for s in sqlglot.parse(query, dialect="postgres") if s is not None]
    except Exception as e:
        return {
            "op": "unknown", "tables": [], "protected_hits": [],
            "role_required": "admin", "parse_error": f"multi-parse failed: {e}",
        }
    if len(_stmts) > 1:
        return {
            "op": "multi", "tables": [], "protected_hits": [],
            "role_required": "admin",
            "parse_error": "multiple statements not allowed",
        }

    op_key = (parsed.key or "").lower() if hasattr(parsed, "key") else ""

    # Bucket by category — first by AST type (more reliable across sqlglot
    # versions), then fall back to string key match.
    if isinstance(parsed, (exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)):
        bucket = "ddl"
    elif isinstance(parsed, exp.Insert):
        bucket = "insert"
    elif isinstance(parsed, exp.Update):
        bucket = "update"
    elif isinstance(parsed, exp.Delete):
        bucket = "delete"
    elif isinstance(parsed, (exp.Select, exp.Union, exp.With)):
        bucket = "select"
    elif op_key in DDL_KEYS:
        bucket = "ddl"
    elif op_key in WRITE_KEYS:
        bucket = op_key
    elif op_key in READ_KEYS:
        bucket = "select"
    else:
        bucket = "unknown"

    # Extract tables. For SELECT we only care for completeness — they don't
    # gate role. For writes we need the *target* table (the one being mutated).
    tables: set[str] = set()
    target_tables: set[str] = set()

    if bucket in ("insert", "update", "delete"):
        # Target of a write op is `parsed.this` (or first Table in the tree).
        target = parsed.find(exp.Table)
        if target is not None:
            target_tables.add(target.name.lower())
        # Also collect everything for visibility.
        for t in parsed.find_all(exp.Table):
            tables.add(t.name.lower())
    else:
        for t in parsed.find_all(exp.Table):
            tables.add(t.name.lower())

    # FULL-AST mutation scan — a top-level SELECT/WITH can still hide a
    # data-modifying CTE (`WITH t AS (DELETE ... RETURNING id) SELECT ...`),
    # which top-node bucketing classifies as a harmless read. Walk the WHOLE
    # tree for any write/DDL node and reclassify. Also catch `SELECT ... INTO`
    # (creates a table = DDL-equivalent).
    _WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
    _DDL_NODES = (exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)
    _mutating = list(parsed.find_all(*_WRITE_NODES))
    _ddl_any = list(parsed.find_all(*_DDL_NODES))
    _select_into = bool(parsed.args.get("into")) if isinstance(parsed, exp.Select) else False

    if _ddl_any or _select_into:
        bucket = "ddl"
    elif _mutating and bucket == "select":
        # write smuggled inside a read — reclassify and collect its targets
        bucket = "write_in_read"
        for _node in _mutating:
            _tgt = _node.find(exp.Table)
            if _tgt is not None:
                target_tables.add(_tgt.name.lower())

    protected_hits = sorted(target_tables & PROTECTED_TABLES) if target_tables \
        else sorted(tables & PROTECTED_TABLES) if bucket == "ddl" else []

    # Decide role.
    if bucket == "select":
        role = "user"
    elif bucket in ("ddl", "multi"):
        role = "admin"
    elif bucket == "write_in_read":
        # A write disguised as a read is, by construction, an evasion attempt:
        # always require admin regardless of target table. A USER who legitimately
        # needs to write should issue a plain INSERT/UPDATE/DELETE.
        role = "admin"
    elif bucket in ("insert", "update", "delete"):
        role = "admin" if protected_hits else "user"
    else:  # unknown
        role = "admin"

    return {
        "op": bucket,
        "tables": sorted(tables),
        "protected_hits": protected_hits,
        "role_required": role,
    }


def is_allowed_for_role(query: str, role: str) -> tuple[bool, dict]:
    """Convenience wrapper. Returns (allowed, classification_dict)."""
    info = classify_sql(query)
    if role == "admin":
        return True, info
    return info["role_required"] == "user", info
