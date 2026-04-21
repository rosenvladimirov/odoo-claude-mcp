"""Tests for scan_pending requested_only filter.

The MCP cron / manual driver uses requested_only=True to pick up only
the moves that the glue attachment hook flagged — not every legacy
draft that happens to have a PDF sitting on it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-scan-pending")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_invoice_engine  # noqa: E402
import ai_usage_log  # noqa: E402


@pytest.fixture
def fake_conn():
    """Connection that records execute_kw args for inspection."""
    conn = MagicMock()
    # Default: no moves, no attachments
    conn.execute_kw.return_value = []
    return conn


def test_requested_only_adds_domain_leaf(fake_conn, monkeypatch):
    """requested_only=True must add ai_pipeline_requested domain leaf."""
    monkeypatch.setattr(ai_usage_log, "query", lambda **kw: [])
    ai_invoice_engine.scan_pending(
        conn=fake_conn, tenant_code="test",
        requested_only=True,
    )
    # First call is account.move search — inspect its domain.
    first_call = fake_conn.execute_kw.call_args_list[0]
    _model, _method, args, _kwargs = (
        first_call.args[0], first_call.args[1],
        first_call.args[2], first_call.args[3],
    )
    domain = args[0]
    assert ["ai_pipeline_requested", "=", True] in domain


def test_requested_only_false_omits_leaf(fake_conn, monkeypatch):
    """Default (requested_only=False) must NOT add the domain leaf."""
    monkeypatch.setattr(ai_usage_log, "query", lambda **kw: [])
    ai_invoice_engine.scan_pending(
        conn=fake_conn, tenant_code="test",
        requested_only=False,
    )
    first_call = fake_conn.execute_kw.call_args_list[0]
    domain = first_call.args[2][0]
    assert not any(
        isinstance(leaf, list) and leaf[:2] == ["ai_pipeline_requested", "="]
        for leaf in domain
    )


def test_scan_pending_filters_existing_successful_logs(monkeypatch):
    """Moves with successful log rows must be filtered out."""
    conn = MagicMock()

    def fake_execute_kw(model, method, args, kwargs=None):
        if model == "account.move" and method == "search":
            return [10, 20, 30]
        if model == "ir.attachment":
            return [
                {"id": 1, "res_id": 10, "name": "a.pdf",
                 "mimetype": "application/pdf", "file_size": 100,
                 "create_date": "2026-04-20"},
                {"id": 2, "res_id": 20, "name": "b.pdf",
                 "mimetype": "application/pdf", "file_size": 200,
                 "create_date": "2026-04-21"},
                {"id": 3, "res_id": 30, "name": "c.pdf",
                 "mimetype": "application/pdf", "file_size": 300,
                 "create_date": "2026-04-21"},
            ]
        return []

    conn.execute_kw.side_effect = fake_execute_kw
    # move 20 already has a successful log
    monkeypatch.setattr(
        ai_usage_log, "query",
        lambda **kw: [{"move_id": 20, "state": "success"}],
    )

    pending = ai_invoice_engine.scan_pending(conn=conn, tenant_code="t")
    ids = {p["move_id"] for p in pending}
    assert ids == {10, 30}  # 20 filtered


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
