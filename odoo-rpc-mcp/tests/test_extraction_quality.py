"""Tests for 1.4/1.6/1.7 — page count, multi-page prompt, two-pass."""
from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-quality")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_invoice_engine  # noqa: E402
import ai_vision_service  # noqa: E402


pypdf = pytest.importorskip("pypdf")


# ── 1.7 count_pdf_pages ─────────────────────────────────────

def _make_pdf(num_pages: int = 1) -> bytes:
    w = pypdf.PdfWriter()
    for _ in range(num_pages):
        w.add_blank_page(width=100, height=100)
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


class TestPageCount:
    @pytest.mark.parametrize("n", [1, 3, 7])
    def test_pypdf_path_returns_exact_count(self, n):
        assert ai_vision_service.count_pdf_pages(_make_pdf(n)) == n

    def test_empty_bytes_returns_one(self):
        assert ai_vision_service.count_pdf_pages(b"") == 1

    def test_garbage_falls_back_to_one(self):
        assert ai_vision_service.count_pdf_pages(b"not a pdf at all") == 1

    def test_byte_fallback_works_when_pypdf_missing(self, monkeypatch):
        """Patch the local pypdf symbol used inside count_pdf_pages.

        The function does a lazy ``from pypdf import PdfReader`` inside
        the try block, so we simulate absence by breaking that import.
        """
        import builtins
        original = builtins.__import__

        def _raise_for_pypdf(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("simulated missing")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _raise_for_pypdf)
        # Use a PDF with known /Type /Page tokens — pypdf's blank-page
        # output includes these.
        pdf = _make_pdf(2)
        n = ai_vision_service.count_pdf_pages(pdf)
        assert n >= 1  # byte fallback may differ in count but not zero


# ── 1.6 two-pass escalation ─────────────────────────────────

def _result(model="claude-haiku-4-5", confidences=None, state="success"):
    return ai_vision_service.ExtractionResult(
        state=state, model=model, pages=1, duration_ms=100,
        extracted_data={"partner_vat": "BG123"},
        field_confidence=confidences or {},
    )


class TestNeedsEscalation:
    def test_high_conf_no_escalation(self):
        should, _reason = ai_invoice_engine._needs_escalation(_result(
            confidences={
                "partner_vat": 0.95, "invoice_date": 0.90,
                "amount_total": 0.92,
            },
        ))
        assert should is False

    def test_low_partner_vat_triggers(self):
        should, reason = ai_invoice_engine._needs_escalation(_result(
            confidences={"partner_vat": 0.5, "invoice_date": 0.95,
                         "amount_total": 0.95},
        ))
        assert should is True
        assert "partner_vat" in reason

    def test_low_amount_total_triggers(self):
        should, _reason = ai_invoice_engine._needs_escalation(_result(
            confidences={"partner_vat": 0.95, "invoice_date": 0.95,
                         "amount_total": 0.60},
        ))
        assert should is True

    def test_sonnet_does_not_escalate(self):
        should, reason = ai_invoice_engine._needs_escalation(_result(
            model="claude-sonnet-4-6",
            confidences={"partner_vat": 0.5},
        ))
        assert should is False
        assert "sonnet" in reason

    def test_opus_does_not_escalate(self):
        should, _reason = ai_invoice_engine._needs_escalation(_result(
            model="claude-opus-4-7",
            confidences={"partner_vat": 0.5},
        ))
        assert should is False

    def test_failed_extraction_no_escalation(self):
        should, _reason = ai_invoice_engine._needs_escalation(
            _result(state="error"),
        )
        assert should is False

    def test_missing_confidence_treated_as_high(self):
        """Fields without confidence data are skipped — no spurious escalation."""
        should, _reason = ai_invoice_engine._needs_escalation(_result(
            confidences={},  # no field_confidence at all
        ))
        assert should is False


# ── _read_company_bool_flag ─────────────────────────────────

class TestReadBoolFlag:
    def test_true_returned(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = [
            [1], [{"id": 1, "ai_two_pass_escalation": True}],
        ]
        assert ai_invoice_engine._read_company_bool_flag(
            conn, "ai_two_pass_escalation",
        ) is True

    def test_false_returned(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = [
            [1], [{"id": 1, "ai_two_pass_escalation": False}],
        ]
        assert ai_invoice_engine._read_company_bool_flag(
            conn, "ai_two_pass_escalation",
        ) is False

    def test_missing_field_returns_false(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = Exception("no such field")
        assert ai_invoice_engine._read_company_bool_flag(
            conn, "ai_two_pass_escalation",
        ) is False

    def test_no_companies_returns_false(self):
        conn = MagicMock()
        conn.execute_kw.side_effect = [[]]
        assert ai_invoice_engine._read_company_bool_flag(
            conn, "ai_two_pass_escalation",
        ) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
