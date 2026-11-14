from __future__ import annotations

import shutil

import numpy as np
import pikepdf
import pdfplumber
import pypdfium2 as pdfium
import pytest
from PIL import Image

from pdfcore import text
from pdfcore.ops import ocr

needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


def _render(path, dpi=100):
    doc = pdfium.PdfDocument(str(path))
    try:
        return doc[0].render(scale=dpi / 72).to_pil().convert("L")
    finally:
        doc.close()


def _hit_is_on_ink(path, word: str, dpi: int = 100) -> None:
    # case=True: pdfcore.text.search currently fails silently with the default case=False (see report)
    hits = text.search(path, word, case=True)
    assert hits, f"{word!r} not found by search"
    img = np.asarray(_render(path, dpi), dtype=np.float32)
    s = dpi / 72
    r = hits[0].rects[0]
    assert r.width > 5 and r.height > 3
    box = img[int(r.top * s): int(r.bottom * s) + 1, int(r.x0 * s): int(r.x1 * s) + 1]
    dark = float((box < 128).mean())
    page_dark = float((img < 128).mean())
    assert dark > 0.08, f"hit box for {word!r} covers no ink (dark fraction {dark:.3f})"
    assert dark > 4 * page_dark
    # the same box shifted one box-height down/up lands mostly between lines: much less ink
    h = int((r.bottom - r.top) * s)
    below = img[int(r.top * s) + 2 * h: int(r.bottom * s) + 2 * h + 1, int(r.x0 * s): int(r.x1 * s) + 1]
    assert dark > float((below < 128).mean())


def _check_letter(res):
    assert res.ok, res.line()
    with pdfplumber.open(res.output) as pdf:
        t = pdf.pages[0].extract_text()
    assert "Northfield Electrical Supply" in t
    assert "Sincerely" in t
    _hit_is_on_ink(res.output, "Northfield")
    _hit_is_on_ink(res.output, "Sincerely")


@needs_tesseract
def test_ocr_scanned_letter_deskews(work):
    src = work("scanned_letter")
    res = ocr.ocr(src, quiet=True)
    assert res.output != src and src.exists()
    _check_letter(res)
    p = res.data["pages"][0]
    assert p["status"] == "ocr" and p["rotation"] == 0
    assert abs(p["deskew"] + 1.6) <= 0.3  # scan was skewed +1.6° (counter-clockwise)
    assert p["confidence"] > 80
    with pikepdf.open(res.output) as pdf:
        assert [round(float(v)) for v in pdf.pages[0].mediabox] == [0, 0, 612, 792]


@needs_tesseract
def test_ocr_scanned_rotated_autorotates(work):
    src = work("scanned_rotated")
    res = ocr.ocr(src, quiet=True)
    _check_letter(res)
    p = res.data["pages"][0]
    assert p["rotation"] == 90
    with pikepdf.open(res.output) as pdf:  # landscape scan became an upright portrait page
        box = [round(float(v)) for v in pdf.pages[0].mediabox]
        assert box == [0, 0, 612, 792]
        assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 0


@needs_tesseract
def test_ocr_text_layer_is_invisible_and_embedded(work):
    res = ocr.ocr(work("scanned_letter"), quiet=True, deskew=False, rotate=False)
    with pdfplumber.open(res.output) as pdf:
        fonts = {c["fontname"] for c in pdf.pages[0].chars}
    assert fonts and all("Inter" in f for f in fonts)
    with pikepdf.open(res.output) as pdf:
        blob = b""
        for _, x in pdf.pages[0].Resources.XObject.items():
            if x.get("/Subtype") == "/Form":
                blob += x.read_bytes()
        assert b"3 Tr" in blob and b"Tz" in blob


@needs_tesseract
def test_ocr_skips_pages_with_text(work):
    res = ocr.ocr(work("letter"), quiet=True)
    assert res.data["pages"][0]["status"] == "skipped-text"
    assert res.data["ocr_pages"] == 0
    assert res.ok


@needs_tesseract
def test_ocr_keep_original_keeps_image(work):
    src = work("scanned_letter")
    with pikepdf.open(src) as pdf:
        before = {bytes(x.read_raw_bytes()) for _, x in pdf.pages[0].Resources.XObject.items()}
    res = ocr.ocr(src, keep_original=True, quiet=True)
    assert res.ok, res.line()
    assert res.data["pages"][0]["deskew"] == 0.0
    with pikepdf.open(res.output) as pdf:
        imgs = {bytes(x.read_raw_bytes()) for _, x in pdf.pages[0].Resources.XObject.items() if x.get("/Subtype") == "/Image"}
    assert before & imgs  # original image bytes untouched
    with pdfplumber.open(res.output) as pdf:
        assert "Northfield" in pdf.pages[0].extract_text()


@needs_tesseract
def test_ocr_unknown_language(work):
    with pytest.raises(ValueError):
        ocr.ocr(work("scanned_letter"), language="xxx", quiet=True)


def test_detect_skew_synthetic(corpus):
    doc = pdfium.PdfDocument(str(corpus["letter"]))
    img = doc[0].render(scale=150 / 72).to_pil().convert("L")
    doc.close()
    assert abs(ocr.detect_skew(img)) <= 0.15
    for ang in (3.0, -2.2):
        tilted = img.rotate(ang, resample=Image.BICUBIC, fillcolor=255)
        assert abs(ocr.detect_skew(tilted) + ang) <= 0.25
