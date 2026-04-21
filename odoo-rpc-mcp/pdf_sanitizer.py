"""Defensive PDF sanitizer — strips active content before vision OCR.

The main attack surface in a PDF routed to a third-party API is active
content attached to the document structure:
  * /JavaScript and /JS action dictionaries
  * /OpenAction (auto-run on open)
  * /AA (additional actions on pages + the document catalog)
  * /Names → /EmbeddedFiles (attached files, often abused for phishing)
  * /Names → /JavaScript (named scripts)

Anthropic's vision API does not execute any of this, so the ingress
risk to OUR side is low. Two reasons to strip anyway:

  1. Defense in depth — the same PDF may be downloaded later from the
     chatter audit trail and opened by an accountant in a local reader
     that DOES execute JS/AA.
  2. Tenant hygiene — embedded files can leak previously-attached
     confidential PDFs that we don't want re-shipped to Claude.

The sanitizer is best-effort: when ``pypdf`` is unavailable, or the
document is malformed, we log a warning and hand back the original
bytes so extraction still works. Blocking extraction on a parse error
would be a worse user outcome than passing the raw PDF through.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from io import BytesIO

logger = logging.getLogger(__name__)

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject
    _PYPDF_AVAILABLE = True
except Exception as e:  # noqa: BLE001 — catch any import-time failure
    logger.info("pypdf not available; PDF sanitization disabled: %s", e)
    _PYPDF_AVAILABLE = False


# Keys whose presence on the document catalog (or page objects) indicate
# active content we want to remove.  Names are PDF ``/Name`` keys, not
# plain strings — pypdf stores them as NameObject('/JS'), etc.
_DOC_LEVEL_DROP_KEYS = (
    "/OpenAction",
    "/AA",
    "/JavaScript",
    "/JS",
)
_NAMES_DROP_KEYS = (
    "/JavaScript",
    "/EmbeddedFiles",
)
_PAGE_DROP_KEYS = (
    "/AA",           # page-level additional actions
    "/PresSteps",    # presentation steps can carry navigation actions
)
# Annotation actions — link annotations with /URI or JS handlers.
_ANNOT_DROP_KEYS = (
    "/A",            # action dictionary
    "/AA",
    "/JS",
)


@dataclass
class SanitizeReport:
    """Summary of what the sanitizer removed. Attached to logs + chatter."""
    available: bool = _PYPDF_AVAILABLE
    pages: int = 0
    removed_doc_actions: list[str] = field(default_factory=list)
    removed_names: list[str] = field(default_factory=list)
    removed_page_actions: int = 0
    removed_annotation_actions: int = 0
    parse_error: str | None = None

    def any_removed(self) -> bool:
        return bool(
            self.removed_doc_actions
            or self.removed_names
            or self.removed_page_actions
            or self.removed_annotation_actions
        )

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "pages": self.pages,
            "removed_doc_actions": list(self.removed_doc_actions),
            "removed_names": list(self.removed_names),
            "removed_page_actions": self.removed_page_actions,
            "removed_annotation_actions": self.removed_annotation_actions,
            "parse_error": self.parse_error,
            "modified": self.any_removed(),
        }


def sanitize_pdf(pdf_bytes: bytes) -> tuple[bytes, SanitizeReport]:
    """Return a PDF copy with active content stripped + a report.

    On any parsing failure we return the ORIGINAL bytes and note the
    error on the report — the caller treats "sanitize failed" as
    "carry on with the original, log the anomaly".
    """
    report = SanitizeReport()
    if not _PYPDF_AVAILABLE or not pdf_bytes:
        return pdf_bytes, report

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except Exception as e:  # noqa: BLE001
        report.parse_error = f"reader: {type(e).__name__}: {e}"
        return pdf_bytes, report

    try:
        writer = PdfWriter(clone_from=reader)
    except Exception as e:  # noqa: BLE001
        # Some malformed PDFs can be read but not cloned; fall back.
        report.parse_error = f"clone: {type(e).__name__}: {e}"
        return pdf_bytes, report

    try:
        # ── Document catalog ─────────────────────────────────
        # pypdf's writer exposes the root object as ._root_object
        # (stable public-ish field in 4.x/5.x/6.x).
        root = getattr(writer, "_root_object", None)
        if root is not None:
            for key in _DOC_LEVEL_DROP_KEYS:
                name = NameObject(key)
                if name in root:
                    del root[name]
                    report.removed_doc_actions.append(key)

            # /Names dictionary holds /JavaScript and /EmbeddedFiles
            # sub-trees — both can ship hostile payloads.
            names_dict = root.get(NameObject("/Names"))
            if names_dict is not None:
                for key in _NAMES_DROP_KEYS:
                    name = NameObject(key)
                    if name in names_dict:
                        del names_dict[name]
                        report.removed_names.append(key)

        # ── Per-page pass ────────────────────────────────────
        report.pages = len(writer.pages)
        for page in writer.pages:
            for key in _PAGE_DROP_KEYS:
                name = NameObject(key)
                if name in page:
                    del page[name]
                    report.removed_page_actions += 1

            annots = page.get(NameObject("/Annots"))
            if annots:
                for annot_ref in list(annots):
                    try:
                        annot = annot_ref.get_object() if hasattr(
                            annot_ref, "get_object",
                        ) else annot_ref
                    except Exception:  # noqa: BLE001
                        continue
                    for key in _ANNOT_DROP_KEYS:
                        name = NameObject(key)
                        if name in annot:
                            del annot[name]
                            report.removed_annotation_actions += 1
    except Exception as e:  # noqa: BLE001
        report.parse_error = f"strip: {type(e).__name__}: {e}"
        return pdf_bytes, report

    # Serialize the cleaned copy back to bytes.
    try:
        out = BytesIO()
        writer.write(out)
        return out.getvalue(), report
    except Exception as e:  # noqa: BLE001
        report.parse_error = f"write: {type(e).__name__}: {e}"
        return pdf_bytes, report
