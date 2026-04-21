"""Audit-trail tests for ai_usage_log.mark_billed (Gap 3.6)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-audit")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_usage_log  # noqa: E402

_TEST_DB = _TMP_ROOT / "ai_usage_audit.db"
ai_usage_log.DB_PATH = _TEST_DB


@pytest.fixture(autouse=True)
def _clean_db():
    ai_usage_log.DB_PATH = _TEST_DB
    if _TEST_DB.exists():
        _TEST_DB.unlink()
    ai_usage_log._initialized = False
    yield
    if _TEST_DB.exists():
        _TEST_DB.unlink()


def _log_one(tenant="t"):
    return ai_usage_log.log_extraction(
        tenant_code=tenant, odoo_url="http://x", odoo_db="db",
        move_id=None, attachment_id=None, source="api",
        model="claude-haiku-4-5", state="success",
        cost_millicents=1000, billed=True,
    )


class TestMarkBilledAudit:
    def test_returns_row_count(self):
        rowid = _log_one()
        n = ai_usage_log.mark_billed([rowid], False, reason="test")
        assert n == 1

    def test_warning_log_with_reason(self, caplog):
        rowid = _log_one()
        with caplog.at_level(logging.WARNING, logger=ai_usage_log.logger.name):
            ai_usage_log.mark_billed(
                [rowid], False,
                reason="Reconciliation — Anthropic Admin API disputed row",
            )
        warn_lines = [
            r for r in caplog.records
            if r.levelname == "WARNING"
            and "mark_billed" in r.getMessage()
        ]
        assert warn_lines
        assert "Reconciliation" in warn_lines[0].getMessage()

    def test_info_log_without_reason(self, caplog):
        rowid = _log_one()
        with caplog.at_level(logging.INFO, logger=ai_usage_log.logger.name):
            ai_usage_log.mark_billed([rowid], False)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("no reason given" in m for m in msgs)

    def test_empty_ids_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger=ai_usage_log.logger.name):
            n = ai_usage_log.mark_billed([], True, reason="noop")
        assert n == 0
        # No mark_billed lines emitted because nothing ran.
        assert not any(
            "mark_billed" in r.getMessage() for r in caplog.records
        )

    def test_flag_actually_flips(self):
        rowid = _log_one()
        rows = ai_usage_log.query(tenant_code="t", limit=5)
        original = [r for r in rows if r["id"] == rowid][0]
        assert original["billed"] == 1

        ai_usage_log.mark_billed([rowid], False, reason="test")
        rows = ai_usage_log.query(tenant_code="t", limit=5)
        after = [r for r in rows if r["id"] == rowid][0]
        assert after["billed"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
