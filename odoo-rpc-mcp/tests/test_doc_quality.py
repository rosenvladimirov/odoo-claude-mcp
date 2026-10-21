"""Класът на документа и изборът на модел за разчитане на фактури (doc_quality)."""
import struct

import pytest

pytest.importorskip("pypdf")

import ai_vision_service as vis  # noqa: E402
from doc_quality import classify_document, image_size  # noqa: E402


def _jpeg(width, height, nbytes):
    """Минимален JPEG: SOI + APP0 + SOF0 с размерите + пълнеж до nbytes."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    head = b"\xff\xd8" + app0 + sof0
    return head + b"\x00" * max(0, nbytes - len(head) - 2) + b"\xff\xd9"


def _png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


def _pdf(content=b"", image_width=None):
    """Едностраничен A4 PDF: с текст (Helvetica) или с картинка с дадената ширина в пиксели."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"]
    resources = b"/Font << /F1 5 0 R >>"
    if image_width:
        resources += b" /XObject << /Im1 6 0 R >>"
        content = b"q 595 0 0 842 0 0 cm /Im1 Do Q"
    objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << "
                + resources + b" >> >>")
    objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    if image_width:
        data = b"\x00" * 64
        objs.append(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                    b"/BitsPerComponent 8 /Length %d >>\nstream\n" % (image_width, image_width * 1.41, len(data))
                    + data + b"\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def _text_pdf():
    lines = [b"BT /F1 10 Tf 40 %d Td (Invoice 0110343271 VAT BG831642181 total 31.85 EUR line %d) Tj ET" % (800 - i * 14, i)
             for i in range(12)]
    return _pdf(b"\n".join(lines))


def test_image_size_from_headers():
    assert image_size(_jpeg(1275, 1752, 5000)) == (1275, 1752)
    assert image_size(_png(800, 600)) == (800, 600)
    assert image_size(b"GIF89a....") is None


def test_photo_quality():
    good = classify_document(_jpeg(1275, 1752, int(1275 * 1752 * 0.1)), "image/jpeg")
    assert (good["kind"], good["quality"]) == ("photo", "good")
    small = classify_document(_jpeg(700, 980, int(700 * 980 * 0.3)), "image/jpeg")
    assert (small["quality"], small["reason"]) == ("poor", "low resolution")
    squeezed = classify_document(_jpeg(1275, 1752, int(1275 * 1752 * 0.03)), "image/jpeg")
    assert (squeezed["quality"], squeezed["reason"]) == ("poor", "heavy compression")


def test_pdf_with_text_is_digital():
    doc = classify_document(_text_pdf(), "application/pdf")
    assert (doc["kind"], doc["quality"]) == ("digital", "good")
    assert doc["text_chars_per_page"] >= 200


def test_scanned_pdf_quality_by_dpi():
    low = classify_document(_pdf(image_width=600), "application/pdf")      # A4 при ~72 dpi
    assert (low["kind"], low["quality"], low["dpi"]) == ("scan", "poor", 73)  # 600 / (595 / 72)
    ok = classify_document(_pdf(image_width=2480), "application/pdf")      # A4 при 300 dpi
    assert (ok["kind"], ok["quality"]) == ("scan", "good")


def test_model_per_document_class():
    digital, _ = vis.choose_model_for_document(_text_pdf(), "application/pdf", pages=1)
    scan, _ = vis.choose_model_for_document(_jpeg(1275, 1752, int(1275 * 1752 * 0.1)), "image/jpeg", pages=1)
    poor, _ = vis.choose_model_for_document(_jpeg(700, 980, 200000), "image/jpeg", pages=1)
    long_digital, _ = vis.choose_model_for_document(_text_pdf(), "application/pdf", pages=12)
    assert (digital, scan, poor, long_digital) == (
        vis.MODEL_DIGITAL, vis.MODEL_SCAN, vis.MODEL_POOR_SCAN, vis.MODEL_SCAN)
    assert vis.MODEL_DIGITAL == "claude-haiku-5-5" and vis.MODEL_POOR_SCAN == "claude-opus-5-5"


def test_broken_input_never_raises():
    assert classify_document(b"%PDF-garbage", "application/pdf")["quality"] == "good"


def test_new_models_have_prices():
    for model in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"):
        assert model in vis.PRICING_USD_PER_M_TOKENS
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert vis.compute_cost_usd_millicents("claude-sonnet-5-5", usage) == 2 * 100 * 1000
