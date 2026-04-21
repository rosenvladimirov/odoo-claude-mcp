"""Tests for few-shot RAG injection (Gap 2.1, Phase 1).

Covers:
  * _step_retrieve_few_shot_examples — skip path + happy path
  * _format_few_shot_block — JSON shape
  * build_messages — injects cached user/assistant pair when examples
    exist; leaves the request alone when they don't
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-fewshot")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_invoice_engine  # noqa: E402
import ai_vision_service  # noqa: E402


# ── _format_few_shot_block ──────────────────────────────────

class TestFormatFewShotBlock:
    def test_empty_returns_empty_string(self):
        assert ai_vision_service._format_few_shot_block([]) == ""
        assert ai_vision_service._format_few_shot_block(None) == ""  # type: ignore[arg-type]

    def test_slim_shape(self):
        block = ai_vision_service._format_few_shot_block([{
            "partner_name": "Vivacom",
            "invoice_number": "INV-001",
            "invoice_date": "2026-03-01",
            "amount_total": 100.0,
            "amount_tax": 20.0,
            "amount_untaxed": 80.0,
            "currency": "BGN",
            "lines": [
                {"description": "Monthly plan", "quantity": 1,
                 "price_unit": 80.0, "amount_subtotal": 80.0},
            ],
        }])
        parsed = json.loads(block)
        assert parsed[0]["partner_name"] == "Vivacom"
        assert parsed[0]["lines"][0]["description"] == "Monthly plan"

    def test_truncates_to_three_examples_and_three_lines_each(self):
        big_examples = [
            {
                "partner_name": f"Vendor {i}",
                "lines": [
                    {"description": f"Line {j}", "quantity": 1,
                     "price_unit": 10.0, "amount_subtotal": 10.0}
                    for j in range(10)
                ],
            }
            for i in range(10)
        ]
        parsed = json.loads(
            ai_vision_service._format_few_shot_block(big_examples),
        )
        assert len(parsed) == 3
        for row in parsed:
            assert len(row["lines"]) == 3

    def test_long_description_truncated(self):
        parsed = json.loads(ai_vision_service._format_few_shot_block([{
            "partner_name": "X",
            "lines": [{"description": "A" * 500, "quantity": 1,
                       "price_unit": 1.0, "amount_subtotal": 1.0}],
        }]))
        assert len(parsed[0]["lines"][0]["description"]) <= 120


# ── build_messages with/without examples ────────────────────

class TestBuildMessagesFewShot:
    def test_no_examples_single_user_message(self):
        _system, msgs = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
        )
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_with_examples_adds_user_assistant_pair(self):
        _system, msgs = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
            few_shot_examples=[{"partner_name": "Vivacom"}],
        )
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["role"] == "user"
        assert "REFERENCE" in msgs[0]["content"][0]["text"]

    def test_few_shot_block_is_cache_controlled(self):
        _system, msgs = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
            few_shot_examples=[{"partner_name": "X"}],
        )
        assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_empty_list_same_as_no_examples(self):
        _sys_a, msgs_a = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
        )
        _sys_b, msgs_b = ai_vision_service.build_messages(
            file_b64="", mimetype="application/pdf",
            few_shot_examples=[],
        )
        assert len(msgs_a) == len(msgs_b) == 1


# ── _step_retrieve_few_shot_examples ────────────────────────

def _make_ctx(move=None):
    ctx = ai_invoice_engine.PipelineContext(
        odoo_conn=MagicMock(),
        tenant_code="t",
        move_id=99,
        attachment_id=1,
    )
    ctx.move = move or {}
    return ctx


class TestRetrieveFewShotStep:
    def test_skips_when_no_partner(self):
        ctx = _make_ctx(move={"partner_id": False})
        rv = ai_invoice_engine._step_retrieve_few_shot_examples(ctx)
        assert "skipped" in rv
        assert "few_shot_examples" not in ctx.data

    def test_skips_when_no_past_bills(self):
        ctx = _make_ctx(move={"partner_id": [7, "Vendor"]})
        ctx.odoo_conn.execute_kw.return_value = []  # empty search_read
        rv = ai_invoice_engine._step_retrieve_few_shot_examples(ctx)
        assert rv == {"found": 0}
        assert "few_shot_examples" not in ctx.data

    def test_happy_path_formats_examples(self):
        ctx = _make_ctx(move={"partner_id": [7, "Vivacom"]})

        def fake_execute_kw(model, method, args, kwargs=None):
            if model == "account.move" and method == "search_read":
                return [
                    {
                        "id": 10, "name": "BILL/001", "ref": "V-INV-1",
                        "invoice_date": "2026-03-01",
                        "partner_id": [7, "Vivacom"],
                        "amount_untaxed": 80.0, "amount_tax": 20.0,
                        "amount_total": 100.0,
                        "currency_id": [1, "BGN"],
                    },
                ]
            if model == "account.move.line":
                return [
                    {
                        "move_id": [10, "BILL/001"],
                        "name": "Monthly plan",
                        "quantity": 1.0,
                        "price_unit": 80.0,
                        "price_subtotal": 80.0,
                    },
                ]
            return []

        ctx.odoo_conn.execute_kw.side_effect = fake_execute_kw
        rv = ai_invoice_engine._step_retrieve_few_shot_examples(ctx)
        assert rv["found"] == 1
        assert rv["partner_id"] == 7
        examples = ctx.data["few_shot_examples"]
        assert examples[0]["partner_name"] == "Vivacom"
        assert examples[0]["amount_total"] == 100.0
        assert examples[0]["lines"][0]["description"] == "Monthly plan"

    def test_query_excludes_current_move(self):
        ctx = _make_ctx(move={"partner_id": [7, "V"]})
        calls = []

        def fake_execute_kw(model, method, args, kwargs=None):
            calls.append((model, method, args))
            return []

        ctx.odoo_conn.execute_kw.side_effect = fake_execute_kw
        ai_invoice_engine._step_retrieve_few_shot_examples(ctx)
        search_call = next(
            c for c in calls if c[0] == "account.move" and c[1] == "search_read"
        )
        domain = search_call[2][0]
        # id != current move
        assert ["id", "!=", 99] in domain

    def test_integer_partner_id_also_handled(self):
        """Some Odoo flows return partner_id as int instead of [id, name]."""
        ctx = _make_ctx(move={"partner_id": 7})
        ctx.odoo_conn.execute_kw.return_value = []
        rv = ai_invoice_engine._step_retrieve_few_shot_examples(ctx)
        assert rv == {"found": 0}  # not "skipped"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
