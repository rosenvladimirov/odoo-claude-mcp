"""Tests for the monthly budget circuit-breaker.

Covers:
  * ai_usage_log.monthly_cost_eur_mc — month boundary + billed filter
  * ai_invoice_engine._read_company_budget_eur_mc — EUR → millicents
  * _step_guard_monthly_budget — no cap / under / at-cap / over-cap
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-budget")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_invoice_engine  # noqa: E402
import ai_usage_log  # noqa: E402

# Pin DB_PATH after import — another test module may have imported
# ai_usage_log first with a different (or default) DB_PATH; env var
# alone isn't enough because DB_PATH is module-load-time constant.
_TEST_DB = _TMP_ROOT / "ai_usage_budget_test.db"
ai_usage_log.DB_PATH = _TEST_DB


@pytest.fixture(autouse=True)
def _clean_db():
    """Rebuild the sqlite DB for every test so counters are isolated."""
    ai_usage_log.DB_PATH = _TEST_DB
    if _TEST_DB.exists():
        _TEST_DB.unlink()
    ai_usage_log._initialized = False
    yield
    if _TEST_DB.exists():
        _TEST_DB.unlink()


def _log(tenant="t", cost_mc=1000, billed=True, when=None):
    """Insert one row with optional timestamp override (via created_at SQL)."""
    import sqlite3
    rowid = ai_usage_log.log_extraction(
        tenant_code=tenant, odoo_url="http://x", odoo_db="db",
        move_id=None, attachment_id=None, source="api",
        model="claude-haiku-4-5", state="success",
        cost_millicents=cost_mc, billed=billed,
    )
    if when is not None:
        with sqlite3.connect(_TEST_DB) as c:
            c.execute(
                "UPDATE ai_usage_log SET created_at = ? WHERE id = ?",
                (when.isoformat(timespec="seconds"), rowid),
            )
            c.commit()
    return rowid


# ── monthly_cost_eur_mc ─────────────────────────────────────

class TestMonthlyCost:
    def test_empty_tenant_returns_zero(self):
        assert ai_usage_log.monthly_cost_eur_mc("nobody") == 0

    def test_sums_current_month_only(self):
        now = datetime.now(timezone.utc)
        # This month: 1000 + 2500 = 3500
        _log(cost_mc=1000)
        _log(cost_mc=2500)
        # Previous month (1st of prev month): should be excluded
        prev_month = (now.replace(day=1) - __import__("datetime").timedelta(days=1))
        _log(cost_mc=99_999, when=prev_month)
        assert ai_usage_log.monthly_cost_eur_mc("t") == 3500

    def test_unbilled_rows_skipped(self):
        _log(cost_mc=1000, billed=True)
        _log(cost_mc=5000, billed=False)
        assert ai_usage_log.monthly_cost_eur_mc("t") == 1000

    def test_per_tenant_isolation(self):
        _log(tenant="a", cost_mc=1000)
        _log(tenant="b", cost_mc=5000)
        assert ai_usage_log.monthly_cost_eur_mc("a") == 1000
        assert ai_usage_log.monthly_cost_eur_mc("b") == 5000


# ── _read_company_budget_eur_mc ─────────────────────────────

class TestReadBudget:
    def test_eur_converted_to_millicents(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = [
            [1],  # search → [company_id]
            [{"id": 1, "ai_monthly_budget_eur": 50.0}],  # read
        ]
        assert ai_invoice_engine._read_company_budget_eur_mc(conn) == 5_000_000

    def test_zero_means_disabled(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = [
            [1], [{"id": 1, "ai_monthly_budget_eur": 0}],
        ]
        assert ai_invoice_engine._read_company_budget_eur_mc(conn) == 0

    def test_missing_field_returns_zero(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = Exception(
            "field does not exist on res.company",
        )
        assert ai_invoice_engine._read_company_budget_eur_mc(conn) == 0

    def test_no_companies_returns_zero(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = [[]]
        assert ai_invoice_engine._read_company_budget_eur_mc(conn) == 0

    def test_non_numeric_value_returns_zero(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = [
            [1], [{"ai_monthly_budget_eur": "not a number"}],
        ]
        assert ai_invoice_engine._read_company_budget_eur_mc(conn) == 0


# ── _step_guard_monthly_budget ──────────────────────────────

def _make_ctx(tenant="t", move_id=42):
    ctx = ai_invoice_engine.PipelineContext(
        odoo_conn=MagicMock(),
        tenant_code=tenant,
        move_id=move_id,
        attachment_id=1,
        tenant_tier="business",
        api_key="sk-test",
    )
    return ctx


class TestBudgetGuardStep:
    def test_no_budget_configured_skips_enforcement(self, monkeypatch):
        ctx = _make_ctx()
        monkeypatch.setattr(
            ai_invoice_engine, "_read_company_budget_eur_mc", lambda c: 0,
        )
        rv = ai_invoice_engine._step_guard_monthly_budget(ctx)
        assert rv["enforced"] is False
        assert ctx.abort is False

    def test_under_budget_runs_through(self, monkeypatch):
        ctx = _make_ctx()
        _log(cost_mc=1000)  # 1000 mc = 1 cent spent
        monkeypatch.setattr(
            ai_invoice_engine, "_read_company_budget_eur_mc",
            lambda c: 5_000_000,  # 50 EUR
        )
        rv = ai_invoice_engine._step_guard_monthly_budget(ctx)
        assert rv["enforced"] is True
        assert rv["spent_eur_mc"] == 1000
        assert rv["remaining_eur_mc"] == 4_999_000
        assert ctx.abort is False

    def test_at_or_over_budget_aborts(self, monkeypatch):
        ctx = _make_ctx()
        _log(cost_mc=5_000_000)  # 50 EUR already spent
        monkeypatch.setattr(
            ai_invoice_engine, "_read_company_budget_eur_mc",
            lambda c: 5_000_000,  # cap = 50 EUR
        )
        # Fake out the Odoo side-effects so we don't hit MagicMock surprises.
        ctx.odoo_conn.execute_kw.return_value = {"ai_review_reason": {}}
        rv = ai_invoice_engine._step_guard_monthly_budget(ctx)
        assert rv["aborted"] is True
        assert ctx.abort is True
        assert ctx.abort_reason == "budget_exceeded"

    def test_abort_writes_move_and_posts_chatter(self, monkeypatch):
        ctx = _make_ctx()
        _log(cost_mc=5_000_000)
        monkeypatch.setattr(
            ai_invoice_engine, "_read_company_budget_eur_mc",
            lambda c: 5_000_000,
        )
        calls: list[tuple] = []

        def fake_execute_kw(model, method, args, kwargs=None):
            calls.append((model, method))
            if method == "fields_get":
                return {"ai_review_reason": {"string": "AI Review Reason"}}
            return True

        ctx.odoo_conn.execute_kw.side_effect = fake_execute_kw
        ai_invoice_engine._step_guard_monthly_budget(ctx)
        method_names = {c[1] for c in calls}
        assert "write" in method_names
        assert "message_post" in method_names


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
