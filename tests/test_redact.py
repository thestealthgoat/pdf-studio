from __future__ import annotations

import io

import numpy as np
import pdfplumber
import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from pdfcore.fonts import register_reportlab
from pdfcore.ops.redact import PATTERNS, Scrub, _segment_outside, UBox, redact
from pdfcore.types import Rect

F = register_reportlab()


def all_text(path, password=None):
    with pdfplumber.open(str(path), password=password or "") as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def pdfium_text(path):
    doc = pdfium.PdfDocument(str(path))
    try:
        return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    finally:
        doc.close()


def word_box(path, page, text):
    with pdfplumber.open(str(path)) as pdf:
        for w in pdf.pages[page].extract_words():
            if w["text"] == text:
                return round(w["x0"], 1), round(w["top"], 1)
    return None


# --------------------------------------------------------------------------- patterns and terms
def test_patterns_and_terms_removed_and_layout_kept(work):
    src = work("contract_v1")
    before_or = word_box(src, 0, "or")
    res = redact(src, patterns=["ssn", "phone", "email", "ein"], terms=["Jordan Blake"], label="(b)(6)", quiet=True)
    assert res.ok, res.gate.details()
    for t in (all_text(res.output), pdfium_text(res.output)):
        for secret in ["123-45-6789", "734-2201", "jordan.blake@example.com", "Jordan Blake", "98-7654321", "555.310.9944"]:
            assert secret not in t
        assert "Ridgeline Controls LLC" in t  # unrelated text untouched
    assert word_box(res.output, 0, "or") == before_or  # neighbours keep their exact position
    assert res.data["glyphs_removed"] > 60
    assert res.output != src and src.exists()


def test_pattern_regexes():
    import re

    assert re.search(PATTERNS["ssn"], "SSN 123-45-6789.")
    assert not re.search(PATTERNS["ssn"], "000-12-3456")
    assert re.search(PATTERNS["phone"], "call (555) 734-2201 now")
    assert re.search(PATTERNS["phone"], "555.310.9944")
    assert re.search(PATTERNS["email"], "a.b+c@ex-ample.co.uk")


def test_dry_run_cli(work, capsys):
    from pdfcore.cli import main

    src = work("contract_v1")
    assert main(["redact", str(src), "--pattern", "ssn", "--dry-run"]) == 0
    assert "123-45-6789" in capsys.readouterr().out
    assert not list(src.parent.glob("*.redacted*.pdf"))


# --------------------------------------------------------------------------- images
def _image_pdf(path):
    c = canvas.Canvas(str(path), pagesize=letter)
    arr = np.full((200, 300, 3), 180, np.uint8)
    arr[:, :, 0] = np.linspace(100, 250, 300, dtype=np.uint8)[None, :]
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    buf.seek(0)
    c.drawImage(ImageReader(buf), 100, 400, 300, 200)  # 1 px = 1 pt
    c.setFont(F["sans"], 12)
    c.drawString(100, 380, "Photo caption stays")
    c.save()


def _first_image(path, page=0):
    with pikepdf.open(path) as pdf:
        for _, xo in pdf.pages[page].get_images().items():
            return np.asarray(pikepdf.PdfImage(xo).as_pil_image().convert("RGB")).copy()
    return None


def test_image_pixels_removed(tmp_path):
    src = tmp_path / "img.pdf"
    _image_pdf(src)
    # PDF image spans x 100..400, y(top-left) 792-600=192 .. 392. Redact its left-top quarter.
    area = Rect(100, 192, 250, 292)
    res = redact(src, areas=[(0, area)], quiet=True)
    assert res.ok, res.gate.details()
    assert res.data["images_redacted"] == 1
    px = _first_image(res.output)
    assert px is not None
    assert px[5:95, 5:145].max() == 0  # covered pixels are black in the image data itself
    assert px[150:195, 200:295].mean() > 100  # uncovered pixels kept
    assert "Photo caption stays" in all_text(res.output)


# --------------------------------------------------------------------------- vectors
def _vector_pdf(path):
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setLineWidth(2)
    # a "signature" curve crossing the box, a line fully inside it, a filled rect partly inside
    p = c.beginPath()
    p.moveTo(100, 500)
    p.curveTo(150, 560, 220, 440, 300, 500)
    c.drawPath(p, stroke=1, fill=0)
    c.line(160, 490, 200, 510)
    c.setFillColorRGB(0.2, 0.4, 0.8)
    c.rect(250, 450, 150, 100, stroke=0, fill=1)
    c.save()


def test_vectors_removed_or_cut(tmp_path):
    src = tmp_path / "vec.pdf"
    _vector_pdf(src)
    # box in user space x 150..280, y 470..540  →  top-left coords top = 792-540, bottom = 792-470
    box = Rect(150, 252, 280, 322)
    res = redact(src, areas=[(0, box)], quiet=True)
    assert res.ok, res.gate.details()
    d = res.data
    assert d["paths_removed"] >= 1 and d["paths_cut"] >= 1 and d["paths_clipped"] >= 1
    ub = UBox(150, 470, 280, 540)
    with pikepdf.open(res.output) as pdf:
        for operands, op in pikepdf.parse_content_stream(pdf.pages[0]):
            if str(op) in ("m", "l") and len(operands) == 2:
                x, y = float(operands[0]), float(operands[1])
                # no stroked geometry strictly inside the box (box edges allowed)
                assert not (151 < x < 279 and 471 < y < 539), (op, x, y)


def test_segment_clip_math():
    parts = _segment_outside((0, 0), (10, 0), [UBox(4, -1, 6, 1)])
    assert parts == [((0, 0), (4, 0)), ((6, 0), (10, 0))]


# --------------------------------------------------------------------------- form xobjects
def test_text_inside_form_xobject(tmp_path):
    src = tmp_path / "form.pdf"
    c = canvas.Canvas(str(src), pagesize=letter)
    c.beginForm("hdr")
    c.setFont(F["sans"], 12)
    c.drawString(72, 700, "Account 4417-SECRET-99 belongs to Pat")
    c.endForm()
    c.doForm("hdr")
    c.showPage()
    c.doForm("hdr")  # same form reused on page 2
    c.showPage()
    c.save()
    res = redact(src, terms=["4417-SECRET-99"], quiet=True)
    assert res.ok, res.gate.details()
    t = all_text(res.output)
    assert "4417-SECRET-99" not in t and t.count("belongs to Pat") == 2


# --------------------------------------------------------------------------- scrubbing
def test_scrub_hidden_layers_metadata_attachments(work, tmp_path):
    src = work("layers")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        ocgs = list(pdf.Root.OCProperties.OCGs)
        pdf.Root.OCProperties.D.ON = pikepdf.Array([ocgs[0], ocgs[2]])
        pdf.Root.OCProperties.D.OFF = pikepdf.Array([ocgs[1]])  # Mechanical hidden
        pdf.docinfo["/Author"] = "Secret Author"
        pdf.attachments["notes.txt"] = pikepdf.AttachedFileSpec(pdf, b"private notes")
        pdf.save(src)
    assert "MECHANICAL LAYER" in all_text(src)
    res = redact(src, areas=[(0, Rect(95, 578, 260, 598))], quiet=True)  # "General notes layer" at y=200
    assert res.ok, res.gate.details()
    with pikepdf.open(res.output) as pdf:
        assert "/OCProperties" not in pdf.Root
        assert len(pdf.attachments) == 0
        assert "/Author" not in pdf.docinfo
        raw = pdf.pages[0].Contents.read_bytes() if not isinstance(pdf.pages[0].Contents, pikepdf.Array) else b""
    assert "MECHANICAL LAYER" not in all_text(res.output)
    assert "ELECTRICAL LAYER" in all_text(res.output)
    assert "General notes" not in all_text(res.output)
    assert b"MECHANICAL" not in raw


def test_keep_metadata_option(work):
    src = work("report_tables")
    res = redact(src, terms=["Quarterly"], scrub=Scrub(metadata=False), quiet=True)
    with pikepdf.open(res.output) as pdf:
        assert str(pdf.docinfo.get("/Title", "")) != ""


def test_encrypted_input(work):
    src = work("encrypted")
    res = redact(src, terms=["Summary"], password="test", quiet=True)
    assert "Summary" not in all_text(res.output)


def test_gate_detects_fake_redaction(tmp_path):
    """A box drawn over text without removing it must FAIL the redaction check."""
    from pdfcore.gate import GateOptions, run_gate

    p = tmp_path / "fake.pdf"
    c = canvas.Canvas(str(p), pagesize=letter)
    c.setFont(F["sans"], 12)
    c.drawString(72, 700, "SSN 123-45-6789")
    c.rect(100, 695, 120, 16, stroke=0, fill=1)
    c.save()
    rep = run_gate(p, options=GateOptions(redact_regions=[(0, Rect(100, 76, 220, 97))], redact_terms=["123-45-6789"]))
    assert not rep.passed
    assert any(f.check == "redaction" for f in rep.failures)


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_rotated_pages(work, rot):
    """Regression: on rotated pages search ran in displayed space (or read text backwards) and nothing was removed."""
    from pdfcore.text import open_plumber

    src = work("contract_v1")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        for pg in pdf.pages:
            pg.Rotate = rot
        pdf.save(src)
    res = redact(src, patterns=["ssn"], terms=["Jordan Blake"], quiet=True)
    assert res.ok, res.gate.details()
    assert res.data["glyphs_removed"] > 20
    with open_plumber(res.output, unrotated=True) as pdf:
        t = pdf.pages[0].extract_text()
    assert "123-45-6789" not in t and "Jordan Blake" not in t and "Ridgeline Controls" in t
    assert "123-45-6789" not in pdfium_text(res.output)
