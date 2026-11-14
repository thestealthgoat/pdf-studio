"""plans issue: revision clouds + delta tags, issue stamps, seal placement, revision-table rows (ARCH D corpus set)."""

from __future__ import annotations

from pathlib import Path

import pdfplumber
import pikepdf
import pytest

from pdfcore.cli import main
from pdfcore.ops.plans import issue
from pdfcore.types import Rect

IN = 72.0
W, H = 36 * IN, 24 * IN
TB_X = W - 4.5 * IN  # corpus title-block divider


def _sample_seal(path: Path) -> Path:
    """A placeholder image standing in for the user's seal file (tests only — not a seal)."""
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((8, 8, 392, 392), outline=(20, 20, 20, 255), width=8)
    d.ellipse((70, 70, 330, 330), outline=(20, 20, 20, 255), width=4)
    d.text((150, 190), "SAMPLE", fill=(20, 20, 20, 255))
    im.save(path)
    return path


def _annots(path: Path, page: int) -> list[pikepdf.Dictionary]:
    with pikepdf.open(path) as pdf:
        return [dict(a.items()) for a in (pdf.pages[page].obj.get("/Annots") or [])]


def _words(path: Path, page: int) -> list[dict]:
    with pdfplumber.open(path) as pdf:
        return pdf.pages[page].extract_words()


def test_cloud_annotations_with_delta(work):
    src = work("drawings_rev0")
    res = issue.cloud(src, [Rect(17.3 * IN, 4.4 * IN, 19.0 * IN, 6.1 * IN)], rev=1, sheet="E-101", quiet=True)
    assert res.ok, res.line()
    assert res.output.name == "drawings_rev0.clouded.pdf"
    ann = _annots(res.output, 1)  # E-101 is page 2
    clouds = [a for a in ann if str(a["/Subtype"]) == "/Polygon" and "/BE" in a]
    assert len(clouds) == 1
    assert str(clouds[0]["/BE"]["/S"]) == "/C" and str(clouds[0]["/IT"]) == "/PolygonCloud"
    deltas = [a for a in ann if str(a.get("/Subj", "")) == "Revision Delta"]
    assert len(deltas) == 1 and str(deltas[0]["/Contents"]) == "Revision 1"
    assert len(deltas[0]["/Vertices"]) == 6  # a triangle
    # delta sits at the cloud's top-right corner
    llx, lly, urx, ury = (float(v) for v in deltas[0]["/Rect"])
    assert 19.0 * IN - 5 < (llx + urx) / 2 < 19.0 * IN + 40
    assert _annots(res.output, 0) == []  # other sheets untouched


def test_cloud_flatten_burns_in(work):
    src = work("drawings_rev0")
    res = issue.cloud(src, [[17.3 * IN, 4.4 * IN, 19.0 * IN, 6.1 * IN], [6 * IN, 15 * IN, 9 * IN, 17 * IN]], rev="2",
                      sheet="E-101", flatten=True, quiet=True)
    assert res.ok, res.line()
    assert _annots(res.output, 1) == []
    ones = [w for w in _words(res.output, 1) if w["text"] == "2" and w["x0"] > 18.5 * IN and w["top"] < 5 * IN]
    assert ones, "delta number not on the page"
    from pdfcore import engines

    img = engines.get("render").render(res.output, 1, dpi=30).convert("RGB")
    k = 30 / 72
    red = sum(1 for x in range(int(17 * IN * k), int(19.5 * IN * k)) for y in range(int(4 * IN * k), int(6.5 * IN * k))
              if (lambda p: p[0] > 150 and p[1] < 110 and p[2] < 110)(img.getpixel((x, y))))
    assert red > 20


def test_issue_stamp_every_sheet_flattened(work):
    src = work("drawings_rev0")
    res = issue.issue(src, stamp="ISSUED FOR CONSTRUCTION", date="2026-11-13", flatten=True, quiet=True)
    assert res.ok, res.line()
    with pdfplumber.open(res.output) as pdf:
        assert len(pdf.pages) == 5
        for page in pdf.pages:
            text = page.extract_text()
            assert "ISSUED FOR CONSTRUCTION" in text and "DATE: 2026-11-13" in text
            w = [x for x in page.extract_words(extra_attrs=["size"]) if x["text"] == "CONSTRUCTION"][0]
            assert w["x0"] > TB_X  # inside the title block column
            assert w["size"] > 10  # scaled up for an ARCH D sheet
    for pg in res.data["pages"]:
        assert pg["title_block"][0] == pytest.approx(TB_X, abs=1)
        st = Rect(*pg["stamp"])
        assert st.x0 > TB_X and st.x1 < W - 36


def test_issue_stamp_annotations_selected_sheets(work):
    src = work("drawings_rev0")
    res = issue.issue(src, stamp="NOT FOR CONSTRUCTION", sheets="E-101,E-301", position="top-left", quiet=True)
    assert res.ok, res.line()
    pages = sorted(p["page"] for p in res.data["pages"])
    assert pages == [2, 4]
    for pi in range(5):
        stamps = [a for a in _annots(res.output, pi) if str(a["/Subtype"]) == "/Stamp"]
        assert len(stamps) == (1 if pi in (1, 3) else 0)
    with pikepdf.open(res.output) as pdf:
        a = pdf.pages[1].Annots[0]
        assert float(a.Rect[0]) < 2 * IN and float(a.Rect[3]) > H - 2 * IN  # top-left corner
        assert "/AP" in a


def test_issue_seal_and_revision_row(work, tmp_path):
    src = work("drawings_rev0")
    before = _words(src, 1)
    seal = _sample_seal(tmp_path / "seal.png")
    res = issue.issue(src, stamp="ISSUED FOR CONSTRUCTION", date="2026-11-13", seal=seal, signer="J. Doe, P.E.",
                      revision={"rev": "1", "description": "Issued for construction"}, sheets="E-101", flatten=True, quiet=True)
    assert res.ok, res.line()
    rec = res.data["pages"][0]
    sl, st = Rect(*rec["seal"]), Rect(*rec["stamp"])
    # seal in the title block, clear of the stamp and of every word already on the sheet
    assert sl.x0 > TB_X and sl.x1 < W - 36 and not sl.intersects(st)
    for w in before:
        assert not sl.intersects(Rect(w["x0"], w["top"], w["x1"], w["bottom"])), w["text"]
    with pdfplumber.open(res.output) as pdf:
        page = pdf.pages[1]
        text = page.extract_text()
        assert "SIGNATURE" in text and "J. DOE, P.E." in text
        assert any(abs(im["x0"] - sl.x0) < sl.width for im in page.images)  # the supplied image is placed
        lines = text.splitlines()
        assert "0 2026-09-15 ISSUED FOR BID" in lines
        assert "1 2026-11-13 ISSUED FOR CONSTRUCTION" in lines
        # new row sits one row pitch under the existing one, same columns
        row0 = [w for w in page.extract_words() if w["text"] == "2026-09-15" and w["x0"] > TB_X]
        row1 = [w for w in page.extract_words() if w["text"] == "2026-11-13" and abs(w["x0"] - row0[0]["x0"]) < 1]
        assert row1 and 10 < row1[0]["top"] - row0[0]["top"] < 18
    assert rec["revision_row"].startswith("1 | 2026-11-13")


def test_issue_seal_vector_pdf_and_explicit_position(work, tmp_path):
    from reportlab.pdfgen import canvas

    seal_pdf = tmp_path / "seal.pdf"
    c = canvas.Canvas(str(seal_pdf), pagesize=(144, 144))
    c.circle(72, 72, 68)
    c.circle(72, 72, 50)
    c.save()
    src = work("drawings_rev0")
    res = issue.issue(src, stamp=None, seal=seal_pdf, seal_at=(30 * IN, 14 * IN), signature=False, pages="1", quiet=True)
    assert res.ok, res.line()
    assert Rect(*res.data["pages"][0]["seal"]).x0 == pytest.approx(30 * IN)
    seals = [a for a in _annots(res.output, 0) if str(a.get("/Subj", "")) == "Seal"]
    assert len(seals) == 1
    with pytest.raises(FileNotFoundError):
        issue.issue(src, seal=tmp_path / "missing.png", quiet=True)


def test_issue_on_typeset_drawing_sheet(tmp_path):
    """Our own drawing-sheet template: title block found, revision row appended under its table."""
    import yaml

    from pdfcore.ops.typeset import make

    y = tmp_path / "sheet.yaml"
    y.write_text(yaml.safe_dump({"sheet": "E-001", "title": "NOTES", "project": "TEST", "size": "arch-d",
                                 "notes": ["ALL WORK PER NEC."], "revisions": [{"rev": "0", "date": "2026-10-01", "description": "BID"}]}))
    sheet = make("drawing-sheet", y, out=tmp_path / "sheet.pdf", quiet=True).output
    res = issue.issue(sheet, stamp="PRELIMINARY", date="2026-11-13", revision={"rev": "1"}, flatten=True, quiet=True)
    assert res.ok, res.line()
    rec = res.data["pages"][0]
    assert rec["title_block"] is not None and rec["title_block"][0] > 30 * IN
    assert Rect(*rec["stamp"]).x0 >= rec["title_block"][0]
    assert rec["revision_row"].startswith("1 | 2026-11-13")


def test_cli_cloud_and_issue(work, tmp_path):
    src = work("drawings_rev0")
    assert main(["plans", "cloud", str(src), "--sheet", "E-101", "--rect", "17.3,4.4,19,6.1", "--units", "in",
                 "--delta", "1", "--flatten", "-o", str(tmp_path / "c.pdf")]) == 0
    seal = _sample_seal(tmp_path / "seal.png")
    assert main(["plans", "issue", str(tmp_path / "c.pdf"), "--stamp", "ISSUED FOR BID", "--date", "2026-11-13",
                 "--seal", str(seal), "--rev", "1", "-o", str(tmp_path / "i.pdf")]) == 0
    assert (tmp_path / "i.pdf").exists()
    with pdfplumber.open(tmp_path / "i.pdf") as pdf:
        assert "1 2026-11-13 ISSUED FOR BID" in pdf.pages[3].extract_text().splitlines()
