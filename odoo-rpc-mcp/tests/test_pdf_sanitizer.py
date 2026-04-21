"""Tests for pdf_sanitizer.sanitize_pdf.

Constructs minimal PDFs via pypdf with known hostile keys so the
sanitizer can demonstrate it removes them. Tests skip when pypdf is
absent — the production code path is supposed to stay usable without
it, just not sanitizing.
"""
from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

import pytest

_TMP_ROOT = Path("/tmp/mcp-test-pdfsan")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_sanitizer  # noqa: E402

pypdf = pytest.importorskip("pypdf")


def _minimal_pdf_bytes(extra_catalog: dict | None = None) -> bytes:
    """Build a 1-page PDF with optional extra catalog entries."""
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if extra_catalog:
        root = writer._root_object
        for k, v in extra_catalog.items():
            root[NameObject(k)] = v
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── Baseline / availability ─────────────────────────────────

class TestBasicBehaviour:
    def test_available_flag_true(self):
        pdf = _minimal_pdf_bytes()
        _out, report = pdf_sanitizer.sanitize_pdf(pdf)
        assert report.available is True

    def test_empty_bytes_returns_empty(self):
        out, report = pdf_sanitizer.sanitize_pdf(b"")
        assert out == b""
        assert report.pages == 0

    def test_clean_pdf_passes_through_unchanged_semantics(self):
        pdf = _minimal_pdf_bytes()
        out, report = pdf_sanitizer.sanitize_pdf(pdf)
        assert report.parse_error is None
        assert report.removed_doc_actions == []
        assert report.removed_names == []
        assert report.pages == 1
        # Output is still a valid PDF.
        reader = pypdf.PdfReader(BytesIO(out))
        assert len(reader.pages) == 1

    def test_corrupt_pdf_returns_original(self):
        junk = b"%PDF-1.4 not really a pdf %%EOF"
        out, report = pdf_sanitizer.sanitize_pdf(junk)
        assert out == junk  # fallback to original
        assert report.parse_error is not None


# ── Active content removal ──────────────────────────────────

class TestActiveContentRemoval:
    def test_removes_openaction(self):
        from pypdf.generic import (
            ArrayObject, DictionaryObject, NameObject, NumberObject,
        )
        action = DictionaryObject({
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): NameObject("/* hostile */"),
        })
        pdf = _minimal_pdf_bytes(extra_catalog={"/OpenAction": action})
        _out, report = pdf_sanitizer.sanitize_pdf(pdf)
        assert "/OpenAction" in report.removed_doc_actions

    def test_removes_doc_level_js(self):
        from pypdf.generic import TextStringObject
        pdf = _minimal_pdf_bytes(
            extra_catalog={"/JS": TextStringObject("alert(1)")},
        )
        _out, report = pdf_sanitizer.sanitize_pdf(pdf)
        assert "/JS" in report.removed_doc_actions

    def test_removes_embedded_files_branch(self):
        from pypdf.generic import DictionaryObject, NameObject
        names = DictionaryObject({
            NameObject("/EmbeddedFiles"): DictionaryObject({
                NameObject("/Names"): NameObject("/leak.pdf"),
            }),
        })
        pdf = _minimal_pdf_bytes(extra_catalog={"/Names": names})
        _out, report = pdf_sanitizer.sanitize_pdf(pdf)
        assert "/EmbeddedFiles" in report.removed_names

    def test_removes_named_javascript_tree(self):
        from pypdf.generic import DictionaryObject, NameObject
        names = DictionaryObject({
            NameObject("/JavaScript"): DictionaryObject({
                NameObject("/Names"): NameObject("/doEvil"),
            }),
        })
        pdf = _minimal_pdf_bytes(extra_catalog={"/Names": names})
        _out, report = pdf_sanitizer.sanitize_pdf(pdf)
        assert "/JavaScript" in report.removed_names

    def test_report_flags_modification(self):
        from pypdf.generic import TextStringObject
        pdf = _minimal_pdf_bytes(
            extra_catalog={"/JS": TextStringObject("x")},
        )
        _out, report = pdf_sanitizer.sanitize_pdf(pdf)
        assert report.any_removed() is True
        assert report.to_dict()["modified"] is True

    def test_report_clean_pdf_not_modified(self):
        pdf = _minimal_pdf_bytes()
        _out, report = pdf_sanitizer.sanitize_pdf(pdf)
        assert report.any_removed() is False
        assert report.to_dict()["modified"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
