"""Bulgarian accounting identifier validators + normalisers.

Kept as pure functions so the pipeline can call them both in the
post-extraction step (to clean up what Claude produced) and in
pytest without any Odoo / Anthropic dependency.

Covered identifiers:

  * ЕИК (Единен идентификационен код) — 9 digits for businesses,
    13 digits for traders (ET) and sole proprietors. The 10th–13th
    digits for 13-char EIKs are a branch indicator; the first 9 are
    the parent entity.

  * ДДС номер (BG VAT) — country prefix ``BG`` followed by the 9 or
    10 digits of the EIK. Odoo stores this on ``res.partner.vat``.
    Vendors often write the naked EIK in the VAT field, or embed
    spaces ("BG 123 456 789").

  * MRN (Movement Reference Number) — 18 characters on customs
    declarations. Format: ``YY`` (2-digit year) + ``CC`` (ISO-2
    country code, all caps) + 14 chars of alphanumerics + 1
    check char. We only validate the shape; authoritative validation
    lives on the customs office side.
"""
from __future__ import annotations

import re


# ──────────────────────────────────────────────────────────
# Compiled regexes (module-level for cheap repeated use)
# ──────────────────────────────────────────────────────────

_EIK_9 = re.compile(r"^\d{9}$")
_EIK_13 = re.compile(r"^\d{13}$")

# BG VAT: optional leading "BG", then 9 or 10 digits (VIES allows 10
# even if the canonical EIK is 9 — tax-registered branches add a digit).
_BG_VAT = re.compile(r"^BG\d{9,10}$")

# MRN — 18 characters, must start with 2 digits (year) + 2 letters
# (country). The remainder is alphanumeric in upper-case. This is a
# pragmatic regex; actual validation would re-compute the check char.
_MRN = re.compile(r"^\d{2}[A-Z]{2}[A-Z0-9]{14}$")


# ──────────────────────────────────────────────────────────
# Public helpers
# ──────────────────────────────────────────────────────────

def strip_vat(value: str | None) -> str:
    """Uppercase + remove all whitespace and punctuation commonly seen
    in printed VAT numbers (``BG 123 456 789``, ``BG-123456789``)."""
    if not value:
        return ""
    return re.sub(r"[\s\-\.]+", "", str(value)).upper()


def is_valid_eik(value: str | None) -> bool:
    """True iff the normalised value is 9 or 13 pure digits."""
    v = strip_vat(value)
    return bool(_EIK_9.match(v) or _EIK_13.match(v))


def is_valid_bg_vat(value: str | None) -> bool:
    """True iff the normalised value looks like BG + 9/10 digits."""
    v = strip_vat(value)
    return bool(_BG_VAT.match(v))


def normalize_bg_vat(value: str | None) -> str | None:
    """Canonicalise a Bulgarian VAT number; return None if unusable.

    Rules:
      * Strip whitespace / punctuation, uppercase.
      * If the result is 9 or 13 digits → it's a naked EIK; prefix BG.
      * If it already starts with BG and has 9–10 following digits → ok.
      * Anything else → None (caller leaves the field as-is so human
        review can catch non-BG VATs; we only auto-fix the BG case).
    """
    v = strip_vat(value)
    if not v:
        return None
    if _EIK_9.match(v) or _EIK_13.match(v):
        # Naked EIK — prefix BG. Use the first 9 digits for BG + 10+.
        # Entries with 13 digits keep only the parent 9 on the VAT.
        return "BG" + v[:9]
    if _BG_VAT.match(v):
        return v
    return None


def eik_from_bg_vat(value: str | None) -> str | None:
    """Return the EIK (9 digits) derived from a normalised BG VAT.

    Accepts either a canonical BG VAT (``BG123456789``) or a naked EIK
    (9 or 13 digits). Returns None on anything that can't be resolved.
    """
    v = strip_vat(value)
    if not v:
        return None
    if _EIK_9.match(v):
        return v
    if _EIK_13.match(v):
        return v[:9]
    if _BG_VAT.match(v):
        return v[2:11]
    return None


def strip_mrn(value: str | None) -> str:
    """Uppercase + remove whitespace from an MRN for regex compare."""
    if not value:
        return ""
    return re.sub(r"\s+", "", str(value)).upper()


def is_valid_mrn(value: str | None) -> bool:
    """True iff the normalised value matches the 18-char MRN shape."""
    return bool(_MRN.match(strip_mrn(value)))


# ──────────────────────────────────────────────────────────
# Post-extraction rewriter (pure, no Odoo / HTTP)
# ──────────────────────────────────────────────────────────


# l10n_bg_document_type codes starting with "117" identify self-billing
# protocols under art. 117 of the Bulgarian VAT Act. They must only be
# used when the supplier is outside Bulgaria. See l10n_bg_tax_admin.
_ART_117_PREFIX = "117"


def normalize_extracted_bg_fields(
    data: dict,
) -> dict:
    """Rewrite a vision-extracted invoice dict in place + return a report.

    Idempotent — running twice is a no-op. Mutates ``data`` because the
    pipeline already hands it around by reference; the report is a
    sibling log entry used by the chatter/log trace.

    Operations:
      * ``partner_vat`` normalised via ``normalize_bg_vat``. If the
        original could not be normalised it is left alone; this lets
        non-BG VAT numbers survive for manual review.
      * ``partner_eik`` derived when missing but ``partner_vat`` is
        a normalisable BG VAT.
      * ``customs_mrn`` validated (shape only). Invalid entries are
        left in place with a warning flag so the accountant sees them.
      * Heuristic art. 117 hint: if the partner_vat is obviously
        non-BG (starts with a non-BG EU country code), flag that the
        expected ``l10n_bg_document_type`` is a 117_* protocol.
    """
    report: dict = {
        "vat_changed": False,
        "eik_filled": False,
        "mrn_valid": None,
        "art_117_hint": False,
    }
    if not isinstance(data, dict):
        return report

    # ── partner VAT / EIK ─────────────────────────────────
    raw_vat = data.get("partner_vat")
    canon_vat = normalize_bg_vat(raw_vat)
    if canon_vat and canon_vat != raw_vat:
        data["partner_vat"] = canon_vat
        report["vat_changed"] = True

    current_eik = strip_vat(data.get("partner_eik"))
    if not current_eik:
        derived = eik_from_bg_vat(data.get("partner_vat"))
        if derived:
            data["partner_eik"] = derived
            report["eik_filled"] = True

    # ── customs MRN ───────────────────────────────────────
    mrn = data.get("customs_mrn")
    if mrn:
        canon_mrn = strip_mrn(mrn)
        if canon_mrn != mrn:
            data["customs_mrn"] = canon_mrn
        report["mrn_valid"] = is_valid_mrn(canon_mrn)

    # ── art. 117 heuristic ────────────────────────────────
    doc_type = (data.get("l10n_bg_document_type") or "")
    vat = strip_vat(data.get("partner_vat"))
    non_bg_vat = bool(vat) and not vat.startswith("BG")
    bg_vat = vat.startswith("BG")
    art_117 = str(doc_type).startswith(_ART_117_PREFIX)
    if non_bg_vat and not art_117:
        report["art_117_hint"] = True
        report["art_117_note"] = (
            "Supplier VAT is non-BG; consider l10n_bg_document_type "
            "= 117_* (self-billing under art. 117 ЗДДС)."
        )
    elif art_117 and bg_vat:
        report["art_117_hint"] = True
        report["art_117_note"] = (
            "document_type is a 117_* self-billing protocol but the "
            "supplier VAT is Bulgarian — likely a misclassification."
        )

    return report
