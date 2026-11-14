"""pdfcore-words/1: schema, determinism, BAR fixtures, and the extraction rules in docs/pdfcore-words-1.md."""

from __future__ import annotations

import json
from itertools import groupby
from pathlib import Path

import pikepdf
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from pdfcore.words import WordsError, _norm, dumps, schema_path, words_document

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "fixtures" / "bar").glob("*.pdf"))


@pytest.fixture(scope="module")
def validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(schema_path().read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def lines_of(doc, page=0):
    ws = [w for w in doc["words"] if w["page"] == page]
    return [" ".join(w["text"] for w in g) for _, g in groupby(ws, key=lambda w: w["line"])]


def make_pdf(path: Path, draw, size=letter) -> Path:
    c = canvas.Canvas(str(path), pagesize=size)
    draw(c)
    c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------- fixtures + schema + determinism
def test_bar_fixtures_present():
    assert len(FIXTURES) >= 4


@pytest.mark.parametrize("pdf", FIXTURES, ids=lambda p: p.stem)
def test_bar_fixture_matches_expected(pdf, validator):
    doc = words_document(pdf, version="0.0.0")
    validator.validate(doc)
    expected = pdf.with_suffix("").with_suffix(".words.json")
    assert dumps(doc) == expected.read_text(encoding="utf-8"), "regenerate with tools/make_bar_fixtures.py if intended"


def test_output_is_deterministic_bytes(corpus):
    for name in ("text", "two_column", "rotated"):
        if name in corpus:
            a = dumps(words_document(corpus[name], version="1.2.3"))
            b = dumps(words_document(corpus[name], version="1.2.3"))
            assert a == b and a.endswith("\n") and "\r" not in a


def test_corpus_outputs_validate(corpus, validator):
    for name, path in corpus.items():
        try:
            doc = words_document(path)
        except WordsError as e:
            assert e.code in {"Encrypted", "Damaged", "NotPdf", "TooLarge", "Timeout"}
            continue
        validator.validate(doc)
        for w in doc["words"]:
            assert w["x0"] <= w["x1"] and w["top"] < w["bottom"]


# ---------------------------------------------------------------- reading order on the estimate fixtures
def test_estimate_table_reads_row_by_row():
    doc = json.loads((ROOT / "fixtures/bar/qbo-lookalike-multiline.words.json").read_text(encoding="utf-8"))
    ls = lines_of(doc)
    i = ls.index("Control Panel")
    assert ls[i + 1].startswith("UL 508A duplex pump control panel")
    assert ls[i + 6: i + 9] == ["1", "18,420.00", "18,420.00"]  # 5 description lines, then qty / rate / amount
    assert ls[i + 9] == "SCADA"
    assert ls.index("ACTIVITY") < ls.index("DESCRIPTION") < ls.index("QTY") < ls.index("RATE") < ls.index("AMOUNT") < i


def test_multipage_fixture_pages():
    doc = json.loads((ROOT / "fixtures/bar/qbo-lookalike-multipage.words.json").read_text(encoding="utf-8"))
    assert doc["source"]["pageCount"] == len(doc["pages"]) >= 2
    assert "Estimate 1049 (continued)" in lines_of(doc, 1)


# ---------------------------------------------------------------- rules
def test_ligatures_expand_then_nfc():
    assert _norm("ﬁﬂﬀﬃﬄﬅﬆ") == "fiflffffifflstst"
    assert _norm("é") == "é"  # NFC
    assert _norm("①") == "①"  # never NFKC


def test_word_gap_rule(tmp_path):
    def draw(c):
        c.setFont("Helvetica", 10)
        c.drawString(72, 700, "AB")
        x = 72 + c.stringWidth("AB", "Helvetica", 10) + 1.5  # 0.15·size gap: same word
        c.drawString(x, 700, "CD")
        c.drawString(x + c.stringWidth("CD", "Helvetica", 10) + 3, 700, "EF")  # 0.3·size gap: new word
    doc = words_document(make_pdf(tmp_path / "gap.pdf", draw))
    assert [w["text"] for w in doc["words"]] == ["ABCD", "EF"]


def test_two_columns_read_column_by_column(tmp_path):
    def draw(c):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(72, 720, "Title across the page")
        c.setFont("Helvetica", 10)
        for k in range(8):
            c.drawString(72, 690 - 12 * k, f"left {k}")
            c.drawString(320, 690 - 12 * k, f"right {k}")
    ls = lines_of(words_document(make_pdf(tmp_path / "cols.pdf", draw)))
    assert ls == ["Title across the page"] + [f"left {k}" for k in range(8)] + [f"right {k}" for k in range(8)]


def test_cropbox_origin_and_rotation(tmp_path):
    def draw(c):
        c.setFont("Helvetica", 12)
        c.drawString(100, 700, "Hello")
    src = make_pdf(tmp_path / "base.pdf", draw)
    base = words_document(src)["words"][0]

    with pikepdf.open(src) as pdf:
        pdf.pages[0].CropBox = [50, 40, 562, 752]
        pdf.save(tmp_path / "crop.pdf")
    d = words_document(tmp_path / "crop.pdf")
    assert d["pages"][0]["width"] == 512 and d["pages"][0]["height"] == 712
    w = d["words"][0]
    assert w["x0"] == pytest.approx(base["x0"] - 50, abs=0.02)
    assert w["top"] == pytest.approx(base["top"] - (792 - 752), abs=0.02)

    with pikepdf.open(src) as pdf:
        pdf.pages[0].Rotate = 90
        pdf.save(tmp_path / "rot.pdf")
    d = words_document(tmp_path / "rot.pdf")
    p = d["pages"][0]
    assert (p["width"], p["height"], p["rotation"]) == (792, 612, 90)
    w = d["words"][0]
    assert w["text"] == "Hello" and w["x0"] < w["x1"] and w["top"] < w["bottom"]
    # 90° clockwise: unrotated (u, v) -> (H - v, u); the word's left edge maps to its top edge
    assert w["top"] == pytest.approx(base["x0"], abs=0.5)
    assert w["x1"] == pytest.approx(792 - base["top"], abs=0.5)


def test_subset_prefix_stripped(tmp_path):
    from pdfcore.fonts import register_reportlab

    F = register_reportlab()

    def draw(c):
        c.setFont(F["sans"], 11)
        c.drawString(72, 700, "Subset")
    w = words_document(make_pdf(tmp_path / "sub.pdf", draw))["words"][0]
    assert "+" not in w["font"] and w["font"]


def test_errors(tmp_path):
    def draw(c):
        c.setFont("Helvetica", 12)
        c.drawString(72, 700, "secret")
    src = make_pdf(tmp_path / "plain.pdf", draw)
    with pikepdf.open(src) as pdf:
        pdf.save(tmp_path / "user.pdf", encryption=pikepdf.Encryption(user="u", owner="o"))
        pdf.save(tmp_path / "owner.pdf", encryption=pikepdf.Encryption(user="", owner="o"))
    with pytest.raises(WordsError) as e:
        words_document(tmp_path / "user.pdf")
    assert e.value.code == "Encrypted"
    assert words_document(tmp_path / "user.pdf", password="u")["words"][0]["text"] == "secret"
    owner = words_document(tmp_path / "owner.pdf")
    assert owner["source"]["encrypted"] is True and owner["words"]

    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(WordsError) as e:
        words_document(tmp_path / "notes.txt")
    assert e.value.code == "NotPdf"
    with pytest.raises(WordsError) as e:
        words_document(src, max_mb=0.0001)
    assert e.value.code == "TooLarge"
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 50)
    with pytest.raises(WordsError) as e:
        words_document(tmp_path / "broken.pdf")
    assert e.value.code == "Damaged"


def test_no_text_layer(tmp_path):
    def draw(c):
        c.rect(72, 72, 200, 200, fill=1)
    doc = words_document(make_pdf(tmp_path / "blank.pdf", draw))
    assert doc["source"]["hasTextLayer"] is False and doc["words"] == []


def test_pages_selection(tmp_path):
    p = tmp_path / "three.pdf"
    c = canvas.Canvas(str(p), pagesize=letter)
    for k in range(3):
        c.setFont("Helvetica", 12)
        c.drawString(72, 700, f"page{k}")
        c.showPage()
    c.save()
    doc = words_document(p, pages=[2])
    assert [pg["index"] for pg in doc["pages"]] == [2]
    assert [(w["page"], w["text"]) for w in doc["words"]] == [(2, "page2")]
