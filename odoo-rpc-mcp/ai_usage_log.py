"""AI Usage Log — SQLite-backed billing ledger.

One row per AI document extraction. Source of truth for per-tenant billing,
reconciled daily against Anthropic Admin API (workspace cost).

Counting rules (see memory/bl_ai_ocr_pricing_model.md):
  - 1 row = 1 successful account.move extraction
  - Unique constraint on (tenant_code, move_id)
  - Gmail dedup via unique (tenant_code, source_message_id) when source='gmail'
  - Failures / re-extracts / cache hits follow `billed` boolean
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("AI_USAGE_DB", "/data/logs/ai_usage.db"))

_lock = threading.RLock()
_initialized = False


# ─── Schema ───────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT    NOT NULL,     -- ISO-8601 UTC
    tenant_code             TEXT    NOT NULL,
    odoo_url                TEXT    NOT NULL,
    odoo_db                 TEXT    NOT NULL,
    move_id                 INTEGER,              -- account.move.id
    attachment_id           INTEGER,              -- ir.attachment.id
    source                  TEXT    NOT NULL,     -- upload|gmail|terminal|api
    source_message_id       TEXT,                 -- Gmail Message-ID for dedup
    model                   TEXT    NOT NULL,     -- claude-haiku-4-5 | sonnet-4-6 | opus-4-7
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_millicents         INTEGER NOT NULL DEFAULT 0,   -- EUR millicents (1/1000¢ = $0.00001)
    cost_usd_millicents     INTEGER NOT NULL DEFAULT 0,   -- USD millicents, source of truth for precision
    pages                   INTEGER DEFAULT 1,
    duration_ms             INTEGER DEFAULT 0,
    prompt_version          TEXT    DEFAULT 'v1',
    state                   TEXT    NOT NULL,     -- success|error|skipped|cached
    billed                  INTEGER NOT NULL DEFAULT 1,  -- bool (0/1)
    error_message           TEXT,
    extra                   TEXT                  -- JSON extra metadata
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_usage_move
  ON ai_usage_log(tenant_code, move_id) WHERE move_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ix_usage_gmail
  ON ai_usage_log(tenant_code, source_message_id)
  WHERE source = 'gmail' AND source_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_usage_tenant_date
  ON ai_usage_log(tenant_code, created_at);

CREATE INDEX IF NOT EXISTS ix_usage_state
  ON ai_usage_log(state);
"""


def _ensure_db() -> None:
    global _initialized
    with _lock:
        if _initialized and DB_PATH.exists():
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
        _initialized = True
        logger.info("ai_usage_log DB ready at %s", DB_PATH)


def _connect() -> sqlite3.Connection:
    _ensure_db()
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ─── Write ────────────────────────────────────────────────


def log_extraction(
    *,
    tenant_code: str,
    odoo_url: str,
    odoo_db: str,
    move_id: int | None,
    attachment_id: int | None,
    source: str,
    model: str,
    state: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_millicents: int = 0,
    cost_usd_millicents: int = 0,
    pages: int = 1,
    duration_ms: int = 0,
    prompt_version: str = "v1",
    billed: bool = True,
    source_message_id: str | None = None,
    error_message: str | None = None,
    extra: dict | None = None,
) -> int | None:
    """Insert one usage row. Returns row id, or None on unique-constraint conflict."""
    row = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tenant_code": tenant_code,
        "odoo_url": odoo_url,
        "odoo_db": odoo_db,
        "move_id": move_id,
        "attachment_id": attachment_id,
        "source": source,
        "source_message_id": source_message_id,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cost_millicents": cost_millicents,
        "cost_usd_millicents": cost_usd_millicents,
        "pages": pages,
        "duration_ms": duration_ms,
        "prompt_version": prompt_version,
        "state": state,
        "billed": 1 if billed else 0,
        "error_message": error_message,
        "extra": json.dumps(extra, ensure_ascii=False) if extra else None,
    }
    try:
        with _lock, _connect() as conn:
            cur = conn.execute(
                f"INSERT INTO ai_usage_log ({','.join(row.keys())}) "
                f"VALUES ({','.join('?' * len(row))})",
                tuple(row.values()),
            )
            conn.commit()
            return cur.lastrowid
    except sqlite3.IntegrityError as e:
        logger.info("ai_usage_log dedup: %s (move=%s, msg_id=%s)",
                    e, move_id, source_message_id)
        return None


# ─── Query ────────────────────────────────────────────────


def query(
    *,
    tenant_code: str | None = None,
    state: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    billed_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Filter rows. Date params are ISO-8601 strings or None."""
    clauses: list[str] = []
    params: list[Any] = []
    if tenant_code:
        clauses.append("tenant_code = ?")
        params.append(tenant_code)
    if state:
        clauses.append("state = ?")
        params.append(state)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if date_from:
        clauses.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?")
        params.append(date_to)
    if billed_only:
        clauses.append("billed = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT * FROM ai_usage_log {where} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ─── Aggregations ─────────────────────────────────────────


def stats(tenant_code: str, period: str = "month") -> dict:
    """KPI bundle for dashboard. Period: 'day'|'week'|'month'|'year'|'all'."""
    now = datetime.now(timezone.utc)
    date_from = _period_start(period, now)
    from_iso = date_from.isoformat(timespec="seconds") if date_from else None

    with _lock, _connect() as conn:
        base_args = (tenant_code, from_iso) if from_iso else (tenant_code,)
        base_where = "tenant_code = ?" + (" AND created_at >= ?" if from_iso else "")

        # Totals
        row = conn.execute(
            f"""
            SELECT
                COUNT(*)                                      AS total_rows,
                SUM(CASE WHEN state='success' THEN 1 ELSE 0 END)   AS success,
                SUM(CASE WHEN state='error'   THEN 1 ELSE 0 END)   AS errors,
                SUM(CASE WHEN state='cached'  THEN 1 ELSE 0 END)   AS cache_hits,
                SUM(CASE WHEN billed=1 THEN 1 ELSE 0 END)     AS billed_docs,
                COALESCE(SUM(input_tokens),0)                 AS input_tokens,
                COALESCE(SUM(output_tokens),0)                AS output_tokens,
                COALESCE(SUM(cache_read_tokens),0)            AS cache_read,
                COALESCE(SUM(cache_creation_tokens),0)        AS cache_creation,
                COALESCE(SUM(cost_millicents),0)              AS cost_eur_millicents,
                COALESCE(SUM(cost_usd_millicents),0)          AS cost_usd_millicents,
                COALESCE(AVG(duration_ms),0)                  AS avg_duration_ms
            FROM ai_usage_log WHERE {base_where}
            """,
            base_args,
        ).fetchone()
        totals = dict(row) if row else {}

        # Per-model breakdown
        by_model = [
            dict(r) for r in conn.execute(
                f"""
                SELECT model,
                       COUNT(*) AS n,
                       COALESCE(SUM(cost_millicents),0) AS cost_eur_millicents
                FROM ai_usage_log WHERE {base_where}
                GROUP BY model ORDER BY n DESC
                """,
                base_args,
            ).fetchall()
        ]

        # Per-day timeseries (last 30 days)
        ts_from = (now - timedelta(days=30)).isoformat(timespec="seconds")
        timeseries = [
            dict(r) for r in conn.execute(
                """
                SELECT DATE(created_at) AS day,
                       COUNT(*)         AS docs,
                       SUM(CASE WHEN billed=1 THEN 1 ELSE 0 END) AS billed,
                       COALESCE(SUM(cost_millicents),0) AS cost_eur_millicents
                FROM ai_usage_log
                WHERE tenant_code = ? AND created_at >= ?
                GROUP BY DATE(created_at) ORDER BY day ASC
                """,
                (tenant_code, ts_from),
            ).fetchall()
        ]

        # Top errors (last 30 days)
        errors_sample = [
            dict(r) for r in conn.execute(
                """
                SELECT created_at, move_id, model, error_message
                FROM ai_usage_log
                WHERE tenant_code = ? AND state = 'error' AND created_at >= ?
                ORDER BY created_at DESC LIMIT 10
                """,
                (tenant_code, ts_from),
            ).fetchall()
        ]

    # Derived: cache hit rate, avg cost per billed doc
    success = totals.get("success") or 0
    cache_hits = totals.get("cache_hits") or 0
    billed_docs = totals.get("billed_docs") or 0
    cost_eur_mc = totals.get("cost_eur_millicents") or 0
    hit_rate = (cache_hits / (success + cache_hits)) if (success + cache_hits) else 0.0
    avg_cost_per_doc_cents = (
        (cost_eur_mc / 1000) / billed_docs if billed_docs else 0
    )

    return {
        "tenant_code": tenant_code,
        "period": period,
        "date_from": from_iso,
        "generated_at": now.isoformat(timespec="seconds"),
        "totals": totals,
        "derived": {
            "cache_hit_rate": round(hit_rate, 4),
            "avg_cost_per_billed_doc_cents": round(avg_cost_per_doc_cents, 4),
            "cost_eur_total_cents": round(cost_eur_mc / 1000, 2),
            "cost_eur_total_display": f"€{cost_eur_mc / 100_000:.2f}",
        },
        "by_model": by_model,
        "timeseries_30d": timeseries,
        "recent_errors": errors_sample,
    }


def _period_start(period: str, now: datetime) -> datetime | None:
    period = (period or "month").lower()
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


# ─── Export ───────────────────────────────────────────────


def export_csv(
    *,
    tenant_code: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    rows = query(
        tenant_code=tenant_code,
        date_from=date_from,
        date_to=date_to,
        limit=100_000,
    )
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ─── Reconciliation helper ────────────────────────────────


def daily_totals(tenant_code: str, day: str) -> dict:
    """Used by daily reconciliation vs Anthropic Admin API.

    `day` is ISO date 'YYYY-MM-DD' (UTC).
    """
    with _lock, _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                AS rows,
                SUM(CASE WHEN billed=1 THEN 1 ELSE 0 END) AS billed_docs,
                COALESCE(SUM(cost_millicents),0)        AS cost_eur_millicents,
                COALESCE(SUM(cost_usd_millicents),0)    AS cost_usd_millicents,
                COALESCE(SUM(input_tokens),0)           AS input_tokens,
                COALESCE(SUM(output_tokens),0)          AS output_tokens
            FROM ai_usage_log
            WHERE tenant_code = ? AND DATE(created_at) = ?
            """,
            (tenant_code, day),
        ).fetchone()
    return dict(row) if row else {}


def mark_billed(ids: Iterable[int], billed: bool) -> int:
    """Flip billed flag on a batch of rows (for manual corrections)."""
    id_list = list(ids)
    if not id_list:
        return 0
    placeholders = ",".join("?" * len(id_list))
    with _lock, _connect() as conn:
        cur = conn.execute(
            f"UPDATE ai_usage_log SET billed = ? WHERE id IN ({placeholders})",
            [1 if billed else 0, *id_list],
        )
        conn.commit()
        return cur.rowcount
