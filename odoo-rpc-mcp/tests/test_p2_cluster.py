"""P2 cluster tests — chatter HTML + partner account histogram + build_messages.

Gaps covered:
  * 4.5 — _render_extraction_chatter produces HTML without raw JSON dump
  * 2.2 — _collect_partner_account_histogram frequency math + top-K
  * 2.2 — _format_partner_account_hints prompt rendering
  * 2.2 — build_messages merges few-shot + account hints into one cached user msg
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-p2")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_invoice_engine  # noqa: E402
import ai_vision_service  # noqa: E402


# ── 4.5 Chatter HTML ────────────────────────────────────────

class TestRenderExtractionChatter:
    def test_basic_fields_rendered(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={
                "document_type": "in_invoice",
                "partner_name": "Vivacom", "partner_vat": "BG123456789",
                "invoice_number": "INV-1", "invoice_date": "2026-04-21",
                "amount_untaxed": 100.0, "amount_tax": 20.0,
                "amount_total": 120.0, "currency": "BGN",
            },
            field_confidence={"partner_name": 0.95, "amount_total": 0.92},
            arithmetic_note="arithmetic OK",
            attachment_id=7,
            model="claude-haiku-4-5",
            prompt_version="v4",
        )
        assert "Vivacom" in html
        assert "BG123456789" in html
        assert "120.00" in html  # formatted total
        assert "claude-haiku-4-5" in html
        assert "arithmetic OK" in html

    def test_confidence_badges_coloured(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={"partner_name": "X"},
            field_confidence={"partner_name": 0.95},
            arithmetic_note="arithmetic OK", attachment_id=1,
            model="m", prompt_version="v4",
        )
        assert "#1f883d" in html  # green for high conf
        assert "95%" in html

    def test_low_confidence_is_red(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={"partner_name": "X"},
            field_confidence={"partner_name": 0.40},
            arithmetic_note="arithmetic OK", attachment_id=1,
            model="m", prompt_version="v4",
        )
        assert "#d1242f" in html  # red
        assert "40%" in html

    def test_lines_table_renders(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={
                "partner_name": "X",
                "lines": [
                    {"description": "Line A", "quantity": 1,
                     "price_unit": 10.0, "amount_subtotal": 10.0,
                     "tax_rate": 20},
                    {"description": "Line B", "quantity": 2,
                     "price_unit": 5.0, "amount_subtotal": 10.0,
                     "tax_rate": 9},
                ],
            },
            field_confidence={},
            arithmetic_note="arithmetic OK", attachment_id=1,
            model="m", prompt_version="v4",
        )
        assert "Line A" in html
        assert "Line B" in html
        assert "20%" in html  # tax rate rendered
        assert "9%" in html

    def test_long_lines_truncated_to_five(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={
                "lines": [
                    {"description": f"L{i}", "quantity": 1,
                     "price_unit": 1.0, "amount_subtotal": 1.0}
                    for i in range(10)
                ],
            },
            field_confidence={},
            arithmetic_note="arithmetic OK", attachment_id=1,
            model="m", prompt_version="v4",
        )
        assert "5 more line" in html

    def test_arithmetic_mismatch_is_amber(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={"partner_name": "X"},
            field_confidence={},
            arithmetic_note="sum(lines)=90.00 != untaxed=100.00",
            attachment_id=1, model="m", prompt_version="v4",
        )
        assert "#bf8700" in html  # amber for mismatch
        assert "sum(lines)" in html

    def test_escalated_badge_shown(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={"partner_name": "X"},
            field_confidence={},
            arithmetic_note="arithmetic OK",
            attachment_id=1,
            model="claude-sonnet-4-6", prompt_version="v4",
            escalated=True,
        )
        assert "two-pass" in html

    def test_pdf_sanitised_badge(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={}, field_confidence={},
            arithmetic_note="arithmetic OK",
            attachment_id=1, model="m", prompt_version="v4",
            pdf_sanitize={"modified": True,
                          "removed_doc_actions": ["/JS"]},
        )
        assert "PDF sanitised" in html

    def test_raw_json_in_collapsible(self):
        html = ai_invoice_engine._render_extraction_chatter(
            data={"partner_name": "Secret", "lines": []},
            field_confidence={}, arithmetic_note="arithmetic OK",
            attachment_id=1, model="m", prompt_version="v4",
        )
        assert "<details" in html
        assert "Secret" in html  # still present, but in collapsible


# ── 2.2 partner_account_histogram ───────────────────────────

class TestPartnerAccountHistogram:
    def test_empty_when_no_history(self):
        conn = MagicMock()
        conn.execute_kw.return_value = []
        assert ai_invoice_engine._collect_partner_account_histogram(
            conn, partner_id=7, current_move_id=99,
        ) == []

    def test_frequency_and_share(self):
        conn = MagicMock()

        def fake_execute_kw(model, method, args, kwargs=None):
            if model == "account.move" and method == "search":
                return [10, 20, 30]
            if model == "account.move.line":
                # 10 rows: 6×602, 3×601, 1×457
                return (
                    [{"account_id": [602, "602 Services"]}] * 6
                    + [{"account_id": [601, "601 Materials"]}] * 3
                    + [{"account_id": [457, "457 VAT"]}]
                )
            return []

        conn.execute_kw.side_effect = fake_execute_kw
        hist = ai_invoice_engine._collect_partner_account_histogram(
            conn, partner_id=7, current_move_id=99,
        )
        assert len(hist) == 3
        # Sorted descending by count
        assert hist[0]["account_id"] == 602
        assert hist[0]["count"] == 6
        assert hist[0]["share"] == 0.6
        assert hist[1]["account_id"] == 601
        assert hist[2]["account_id"] == 457

    def test_truncates_to_top_five(self):
        conn = MagicMock()

        def fake_execute_kw(model, method, args, kwargs=None):
            if model == "account.move" and method == "search":
                return list(range(1, 21))
            if model == "account.move.line":
                # 8 distinct accounts, descending counts
                rows = []
                for i in range(8):
                    rows += [{"account_id": [100 + i, f"Acc {i}"]}] * (10 - i)
                return rows
            return []

        conn.execute_kw.side_effect = fake_execute_kw
        hist = ai_invoice_engine._collect_partner_account_histogram(
            conn, partner_id=1, current_move_id=0,
        )
        assert len(hist) == 5  # truncated

    def test_excludes_current_move(self):
        conn = MagicMock()
        search_domains = []

        def fake_execute_kw(model, method, args, kwargs=None):
            if model == "account.move" and method == "search":
                search_domains.append(args[0])
                return []
            return []

        conn.execute_kw.side_effect = fake_execute_kw
        ai_invoice_engine._collect_partner_account_histogram(
            conn, partner_id=7, current_move_id=42,
        )
        assert ["id", "!=", 42] in search_domains[0]


# ── 2.2 prompt rendering + build_messages ───────────────────

class TestAccountHintsPrompt:
    def test_empty_returns_empty(self):
        assert ai_vision_service._format_partner_account_hints([]) == ""

    def test_renders_bulleted_shares(self):
        out = ai_vision_service._format_partner_account_hints([
            {"account_id": 602, "account_label": "602 Services",
             "count": 6, "share": 0.6},
            {"account_id": 601, "account_label": "601 Materials",
             "count": 3, "share": 0.3},
        ])
        assert "60%" in out
        assert "602 Services" in out
        assert "30%" in out

    def test_build_messages_injects_hints(self):
        _sys, msgs = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
            partner_account_hints=[{
                "account_id": 602, "account_label": "602 Services",
                "count": 10, "share": 0.8,
            }],
        )
        assert len(msgs) == 3  # reference + assistant ack + user scan
        ref_text = msgs[0]["content"][0]["text"]
        assert "ACCOUNT CODING HISTORY" in ref_text
        assert "602 Services" in ref_text

    def test_build_messages_merges_fewshot_and_hints(self):
        _sys, msgs = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
            few_shot_examples=[{"partner_name": "Vivacom"}],
            partner_account_hints=[{
                "account_id": 602, "account_label": "602 Services",
                "count": 10, "share": 0.8,
            }],
        )
        assert len(msgs) == 3
        ref_text = msgs[0]["content"][0]["text"]
        assert "REFERENCE" in ref_text
        assert "ACCOUNT CODING HISTORY" in ref_text
        assert "Vivacom" in ref_text
        assert "602 Services" in ref_text

    def test_build_messages_no_context_single_user_msg(self):
        _sys, msgs = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
        )
        assert len(msgs) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
