"""Unit tests for prompt v2 schema parsing + arithmetic reconciliation.

Covers ai_vision_service._extract_field_confidence and
ai_invoice_engine._check_arithmetic — the two pure-Python pieces of the
Trust Foundation cluster that don't need an Odoo runtime.

Run from project root:
    pytest tests/test_vision_v2_schema.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-vision-v2")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_invoice_engine  # noqa: E402
import ai_vision_service  # noqa: E402


# ── _extract_field_confidence ────────────────────────────────

class TestExtractFieldConfidence:
    def test_v2_confidence_parsed(self):
        extracted = {
            "partner_name": "X",
            "_confidence": {
                "partner_name": 0.95, "invoice_date": 0.88,
            },
        }
        conf = ai_vision_service._extract_field_confidence(extracted)
        assert conf == {"partner_name": 0.95, "invoice_date": 0.88}

    def test_v1_response_returns_empty(self):
        extracted = {"partner_name": "X", "invoice_date": "2026-04-21"}
        assert ai_vision_service._extract_field_confidence(extracted) == {}

    def test_missing_confidence_key_returns_empty(self):
        assert ai_vision_service._extract_field_confidence({}) == {}
        assert ai_vision_service._extract_field_confidence(None) == {}

    def test_non_dict_confidence_returns_empty(self):
        extracted = {"_confidence": "not a dict"}
        assert ai_vision_service._extract_field_confidence(extracted) == {}

    def test_non_numeric_values_dropped(self):
        extracted = {
            "_confidence": {
                "good": 0.8, "bad": "high", "also_bad": None, "okay": 1,
            },
        }
        conf = ai_vision_service._extract_field_confidence(extracted)
        assert conf == {"good": 0.8, "okay": 1.0}

    def test_out_of_range_clamped(self):
        extracted = {
            "_confidence": {
                "too_low": -0.5, "too_high": 1.5, "ok": 0.7,
            },
        }
        conf = ai_vision_service._extract_field_confidence(extracted)
        assert conf == {"too_low": 0.0, "too_high": 1.0, "ok": 0.7}


# ── _check_arithmetic ────────────────────────────────────────

class TestCheckArithmetic:
    def test_all_consistent(self):
        data = {
            "lines": [
                {"amount_subtotal": 100.0},
                {"amount_subtotal": 50.0},
            ],
            "amount_untaxed": 150.0,
            "amount_tax": 30.0,
            "amount_total": 180.0,
        }
        ok, note = ai_invoice_engine._check_arithmetic(data)
        assert ok is True
        assert "OK" in note

    def test_lines_dont_match_untaxed(self):
        data = {
            "lines": [{"amount_subtotal": 100.0}],
            "amount_untaxed": 150.0,  # 50 off
            "amount_tax": 30.0,
            "amount_total": 180.0,
        }
        ok, note = ai_invoice_engine._check_arithmetic(data)
        assert ok is False
        assert "sum(lines)" in note
        assert "untaxed" in note

    def test_totals_dont_add_up(self):
        data = {
            "lines": [{"amount_subtotal": 150.0}],
            "amount_untaxed": 150.0,
            "amount_tax": 30.0,
            "amount_total": 200.0,  # should be 180
        }
        ok, note = ai_invoice_engine._check_arithmetic(data)
        assert ok is False
        assert "untaxed+tax" in note

    def test_tolerance_absorbs_rounding(self):
        data = {
            "lines": [{"amount_subtotal": 100.01}],
            "amount_untaxed": 100.0,  # 1 cent diff
            "amount_tax": 20.0,
            "amount_total": 120.0,
        }
        ok, _note = ai_invoice_engine._check_arithmetic(data, tolerance=0.02)
        assert ok is True

    def test_tolerance_tight(self):
        data = {
            "lines": [{"amount_subtotal": 100.10}],
            "amount_untaxed": 100.0,  # 10 cents off
            "amount_tax": 20.0,
            "amount_total": 120.10,
        }
        ok, _note = ai_invoice_engine._check_arithmetic(data, tolerance=0.02)
        assert ok is False

    def test_missing_totals_is_ok(self):
        # confidence scoring catches missing fields; arithmetic only
        # complains when numbers are definitely inconsistent.
        data = {"lines": [{"amount_subtotal": 100.0}]}
        ok, _note = ai_invoice_engine._check_arithmetic(data)
        assert ok is True

    def test_empty_data_is_ok(self):
        ok, _note = ai_invoice_engine._check_arithmetic({})
        assert ok is True

    def test_non_numeric_values_skipped(self):
        data = {
            "lines": [{"amount_subtotal": "N/A"}],
            "amount_untaxed": "???",
            "amount_tax": 0,
            "amount_total": 0,
        }
        ok, note = ai_invoice_engine._check_arithmetic(data)
        assert ok is True
        assert "skipped" in note or "OK" in note


# ── Prompt version wiring ────────────────────────────────────

class TestPromptVersionWiring:
    def test_default_is_v4(self):
        assert ai_vision_service.DEFAULT_PROMPT_VERSION == "v4"

    def test_v1_still_resolvable(self):
        sys_v1 = ai_vision_service._PROMPT_VERSIONS["v1"]
        assert "document_type" in sys_v1
        assert "_confidence" not in sys_v1  # v1 has no confidence block

    def test_v2_has_confidence_block(self):
        sys_v2 = ai_vision_service._PROMPT_VERSIONS["v2"]
        assert "_confidence" in sys_v2
        assert "confidence in the range" in sys_v2.lower() or "0.0 to 1.0" in sys_v2

    def test_v3_has_bg_specific_fields(self):
        sys_v3 = ai_vision_service._PROMPT_VERSIONS["v3"]
        assert "partner_eik" in sys_v3
        assert "customs_mrn" in sys_v3
        assert "117" in sys_v3  # art. 117 guidance

    def test_v4_has_multi_page_guidance(self):
        sys_v4 = ai_vision_service._PROMPT_VERSIONS["v4"]
        assert "partner_eik" in sys_v4  # v3 content carried over
        assert "Multi-page" in sys_v4
        assert "last page" in sys_v4.lower() or "LAST page" in sys_v4

    def test_build_messages_uses_v4_by_default(self):
        system, _msgs = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
        )
        assert "_confidence" in system
        assert "partner_eik" in system
        assert "Multi-page" in system  # v4 marker

    def test_build_messages_falls_back_on_unknown_version(self):
        system, _msgs = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf", prompt_version="v999",
        )
        # Unknown version → defaults to latest (v4) prompt
        assert "Multi-page" in system


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
