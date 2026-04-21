"""Tests for bg_validators + the normalize_bg_fields pipeline step.

Covers:
  * strip_vat / normalize_bg_vat / eik_from_bg_vat edge cases
  * MRN shape validation
  * normalize_extracted_bg_fields end-to-end (idempotence + art.117 hint)
  * _step_normalize_bg_fields pipeline integration
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-bg")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_invoice_engine  # noqa: E402
import ai_vision_service  # noqa: E402
import bg_validators as bv  # noqa: E402


# ── strip_vat / normalize_bg_vat ────────────────────────────

class TestNormalizeBgVat:
    @pytest.mark.parametrize("raw,canon", [
        ("BG123456789", "BG123456789"),
        ("bg123456789", "BG123456789"),
        ("BG 123 456 789", "BG123456789"),
        ("BG-123-456-789", "BG123456789"),
        ("123456789", "BG123456789"),  # naked EIK → prefix
        ("1234567890123", "BG123456789"),  # 13-digit EIK → first 9 + BG
        ("BG1234567890", "BG1234567890"),  # 10-digit variant
    ])
    def test_normalisation_cases(self, raw, canon):
        assert bv.normalize_bg_vat(raw) == canon

    @pytest.mark.parametrize("raw", [
        None, "", "  ", "DE123456789", "abc", "12345",  # short
        "FR12345678901",  # non-BG VAT — leave to human
    ])
    def test_unnormalisable_returns_none(self, raw):
        assert bv.normalize_bg_vat(raw) is None

    def test_strip_vat_handles_none(self):
        assert bv.strip_vat(None) == ""
        assert bv.strip_vat("") == ""

    def test_is_valid_bg_vat(self):
        assert bv.is_valid_bg_vat("BG123456789") is True
        assert bv.is_valid_bg_vat("BG1234567890") is True
        assert bv.is_valid_bg_vat("DE123456789") is False
        assert bv.is_valid_bg_vat("BG12345") is False


# ── EIK derivation ──────────────────────────────────────────

class TestEikFromVat:
    def test_from_bg_vat(self):
        assert bv.eik_from_bg_vat("BG123456789") == "123456789"

    def test_from_naked_9_digit(self):
        assert bv.eik_from_bg_vat("123456789") == "123456789"

    def test_from_13_digit_returns_first_9(self):
        assert bv.eik_from_bg_vat("1234567890123") == "123456789"

    def test_from_10_digit_bg_vat_takes_9(self):
        assert bv.eik_from_bg_vat("BG1234567890") == "123456789"

    def test_non_bg_returns_none(self):
        assert bv.eik_from_bg_vat("DE123456789") is None
        assert bv.eik_from_bg_vat(None) is None
        assert bv.eik_from_bg_vat("abc") is None


# ── MRN validation ──────────────────────────────────────────

class TestMrn:
    def test_valid_shape(self):
        assert bv.is_valid_mrn("26BG1234567890ABCD") is True

    def test_lowercase_is_normalised_before_match(self):
        assert bv.is_valid_mrn("26bg1234567890abcd") is True

    @pytest.mark.parametrize("bad", [
        None, "", "26BG12345", "ABCD12345678901234",  # country not letters
        "26B12345678901234A",  # only one letter
        "26BG1234567890ABCDE",  # 19 chars
    ])
    def test_invalid_shapes(self, bad):
        assert bv.is_valid_mrn(bad) is False


# ── normalize_extracted_bg_fields ───────────────────────────

class TestNormalizeExtracted:
    def test_idempotent(self):
        data = {"partner_vat": "BG 123 456 789"}
        first = bv.normalize_extracted_bg_fields(data)
        assert data["partner_vat"] == "BG123456789"
        assert first["vat_changed"] is True
        second = bv.normalize_extracted_bg_fields(data)
        assert second["vat_changed"] is False  # nothing to change second pass

    def test_fills_missing_eik_from_vat(self):
        data = {"partner_vat": "BG123456789"}
        report = bv.normalize_extracted_bg_fields(data)
        assert data["partner_eik"] == "123456789"
        assert report["eik_filled"] is True

    def test_keeps_existing_eik(self):
        data = {"partner_vat": "BG123456789", "partner_eik": "1234567890123"}
        report = bv.normalize_extracted_bg_fields(data)
        assert data["partner_eik"] == "1234567890123"
        assert report["eik_filled"] is False

    def test_mrn_marked_valid(self):
        data = {"customs_mrn": " 26bg1234567890abcd "}
        report = bv.normalize_extracted_bg_fields(data)
        assert data["customs_mrn"] == "26BG1234567890ABCD"
        assert report["mrn_valid"] is True

    def test_mrn_marked_invalid(self):
        data = {"customs_mrn": "NOTAMRN"}
        report = bv.normalize_extracted_bg_fields(data)
        assert report["mrn_valid"] is False

    def test_art_117_hint_on_non_bg_supplier(self):
        data = {
            "partner_vat": "DE123456789",
            "l10n_bg_document_type": "01",  # regular invoice code
        }
        report = bv.normalize_extracted_bg_fields(data)
        assert report["art_117_hint"] is True
        assert "117" in report["art_117_note"]

    def test_art_117_already_classified_is_quiet(self):
        data = {
            "partner_vat": "DE123456789",
            "l10n_bg_document_type": "117_protocol_117_1",
        }
        report = bv.normalize_extracted_bg_fields(data)
        assert report["art_117_hint"] is False

    def test_art_117_misclassification_on_bg_supplier(self):
        data = {
            "partner_vat": "BG123456789",
            "l10n_bg_document_type": "117_protocol_117_1",
        }
        report = bv.normalize_extracted_bg_fields(data)
        assert report["art_117_hint"] is True
        assert "Bulgarian" in report["art_117_note"]

    def test_non_dict_input_returns_report(self):
        assert bv.normalize_extracted_bg_fields(None)["vat_changed"] is False  # type: ignore[arg-type]


# ── Pipeline step integration ───────────────────────────────

def _make_ctx(extracted=None, state="success"):
    ctx = ai_invoice_engine.PipelineContext(
        odoo_conn=MagicMock(),
        tenant_code="t",
        move_id=1,
        attachment_id=1,
    )
    result = ai_vision_service.ExtractionResult(
        state=state, model="claude-haiku-4-5", pages=1, duration_ms=100,
        extracted_data=extracted or {},
    )
    ctx.data["vision_result"] = result
    return ctx


class TestNormalizeStep:
    def test_success_path_rewrites_data(self):
        ctx = _make_ctx(extracted={"partner_vat": "bg 123 456 789"})
        rv = ai_invoice_engine._step_normalize_bg_fields(ctx)
        assert rv["vat_changed"] is True
        assert (
            ctx.data["vision_result"].extracted_data["partner_vat"]
            == "BG123456789"
        )
        assert ctx.data["bg_normalize"] == rv

    def test_skips_on_non_success(self):
        ctx = _make_ctx(state="error")
        rv = ai_invoice_engine._step_normalize_bg_fields(ctx)
        assert "skipped" in rv

    def test_skips_with_no_vision_result(self):
        ctx = ai_invoice_engine.PipelineContext(
            odoo_conn=MagicMock(), tenant_code="t", move_id=1,
        )
        rv = ai_invoice_engine._step_normalize_bg_fields(ctx)
        assert "skipped" in rv


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
