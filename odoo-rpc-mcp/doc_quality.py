"""Какъв е документът и колко е четлив — за избора на модел при разчитане на фактури.

Без нови зависимости (в образа няма Pillow/NumPy): PDF през pypdf, картинките по
заглавката на файла.

- ``digital``: PDF с текстов слой (изваден от програма) — текстът е точен, стига евтин модел;
- ``scan``: PDF без текст (сканиран) — ефективната резолюция от вградените картинки;
- ``photo``: JPG/PNG/… — размерите от заглавката, компресията като байтове на пиксел.

Качество ``poor``: малка резолюция по късата страна или силна компресия — там
по-слаб модел бърка цифри или си измисля документ (сверка 21.10.2026, 8 фактури).
"""
from __future__ import annotations

import io
import logging
import struct

logger = logging.getLogger(__name__)

# PDF: средно знаци на страница, над които приемаме, че има текстов слой
DIGITAL_MIN_CHARS_PER_PAGE = 200
# Сканиран PDF: ефективни точки на инч под това ⇒ лош
POOR_MAX_DPI = 150
# Снимка: къса страна в пиксели под това ⇒ лоша
POOR_MAX_SHORT_SIDE_PX = 1000
# JPEG: байтове на пиксел под това ⇒ силно компресирана (артефакти около цифрите)
POOR_MAX_BYTES_PER_PX = 0.06


def image_size(data: bytes) -> tuple[int, int] | None:
    """(ширина, височина) от заглавката на JPEG или PNG; None за други формати."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        # SOF0..SOF15 без DHT (C4), JPG (C8), DAC (CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + seg_len
    return None


def _photo_quality(size: tuple[int, int] | None, nbytes: int) -> tuple[str, dict]:
    if not size:
        return "good", {"reason": "unknown image format — treated as good"}
    width, height = size
    short_side = min(width, height)
    bpp = nbytes / float(width * height) if width and height else 0.0
    info = {"width": width, "height": height, "short_side_px": short_side, "bytes_per_px": round(bpp, 3)}
    if short_side < POOR_MAX_SHORT_SIDE_PX:
        return "poor", dict(info, reason="low resolution")
    if bpp < POOR_MAX_BYTES_PER_PX:
        return "poor", dict(info, reason="heavy compression")
    return "good", info


def _pdf_class(data: bytes) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = len(reader.pages) or 1
    chars = 0
    dpis = []
    for page in reader.pages:
        try:
            chars += len("".join((page.extract_text() or "").split()))
        except Exception:  # noqa: BLE001 — счупен текстов слой = без текст
            pass
        try:
            page_w_in = float(page.mediabox.width) / 72.0
            xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
            widths = [int(xobjects[name].get_object().get("/Width") or 0) for name in xobjects
                      if xobjects[name].get_object().get("/Subtype") == "/Image"]
            if widths and page_w_in > 0:
                dpis.append(max(widths) / page_w_in)
        except Exception:  # noqa: BLE001
            pass
    per_page = chars / pages
    info = {"pages": pages, "text_chars_per_page": round(per_page)}
    if per_page >= DIGITAL_MIN_CHARS_PER_PAGE:
        return dict(info, kind="digital", quality="good")
    dpi = round(min(dpis)) if dpis else None
    info["dpi"] = dpi
    if dpi is not None and dpi < POOR_MAX_DPI:
        return dict(info, kind="scan", quality="poor", reason="low resolution")
    return dict(info, kind="scan", quality="good")


def classify_document(data: bytes, mimetype: str) -> dict:
    """{kind: digital|scan|photo, quality: good|poor, …подробности}. Никога не хвърля."""
    try:
        if mimetype == "application/pdf" or data[:5] == b"%PDF-":
            return _pdf_class(data)
        quality, info = _photo_quality(image_size(data), len(data))
        return dict(info, kind="photo", quality=quality, pages=1)
    except Exception as exc:  # noqa: BLE001 — класификацията не спира разчитането
        logger.warning("doc_quality: classification failed: %s", exc)
        return {"kind": "unknown", "quality": "good", "reason": "classification failed"}
