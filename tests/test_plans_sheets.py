"""pdfcore.ops.plans.sheets — title blocks, sheet index, set reading, compare by sheet, batch links, slip sheets."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import openpyxl
import pikepdf
import pytest
from reportlab.pdfgen import canvas

from pdfcore.cli import main
from pdfcore.fonts import register_reportlab
from pdfcore.ops import markup
from pdfcore.ops.navigate import list_links, read_bookmarks
from pdfcore.ops.plans import sheets as S
from pdfcore.types import Rect

IN = 72.0
ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "corpus" / "external"
FLINT = EXT / "flint_wpc_electrical_2023.pdf"
ADDENDUM = EXT / "flint_addendum01_plan_set.pdf"
F = register_reportlab()


# ---------------------------------------------------------------- fixtures / synthetic sets


@pytest.fixture(scope="module")
def drawings(tmp_path_factory, corpus):
    d = tmp_path_factory.mktemp("sets")
    r0, r1 = d / "drawings_rev0.pdf", d / "drawings_rev1.pdf"
    shutil.copy(corpus["drawings_rev0"], r0)
    shutil.copy(corpus["drawings_rev1"], r1)
    return r0, r1


BOTTOM_SHEETS = [
    ("G0.01", "COVER SHEET AND\nDRAWING INDEX"),
    ("S1.01", "FOUNDATION PLAN"),
    ("A1.01", "FLOOR PLAN"),
    ("A5.01", "DETAILS"),
    ("M1.01", "MECHANICAL PLAN"),
]


def _bottom_sheet(c: canvas.Canvas, number: str, title: str) -> None:
    """ANSI D landscape sheet with the title block along the BOTTOM edge."""
    W, H = 34 * IN, 22 * IN
    c.setLineWidth(2)
    c.rect(0.5 * IN, 0.5 * IN, W - IN, H - IN)
    c.setLineWidth(1.5)
    c.line(0.5 * IN, 3 * IN, W - 0.5 * IN, 3 * IN)
    for x in (8, 19, 24.5, 29.5):
        c.line(x * IN, 0.5 * IN, x * IN, 3 * IN)
    c.setFont(F["sans-bold"], 16)
    c.drawString(1 * IN, 2.3 * IN, "NORTHFIELD ARCHITECTS")
    c.setFont(F["sans"], 9)
    c.drawString(1 * IN, 2.05 * IN, "120 Mill Street, Springfield")
    c.setFont(F["sans"], 8)
    c.drawString(1 * IN, 1.4 * IN, "PROJECT")
    c.setFont(F["sans-bold"], 11)
    c.drawString(1 * IN, 1.15 * IN, "HARBOR VIEW BRANCH LIBRARY")
    c.setFont(F["sans"], 8)
    c.drawString(8.4 * IN, 2.5 * IN, "SHEET TITLE")
    c.setFont(F["sans-bold"], 14)
    for k, part in enumerate(title.split("\n")):
        c.drawString(8.4 * IN, (2.15 - 0.25 * k) * IN, part)
    c.setFont(F["sans"], 8)
    c.drawString(19.4 * IN, 2.5 * IN, "DATE")
    c.drawString(21.6 * IN, 2.5 * IN, "SCALE")
    c.drawString(19.4 * IN, 1.5 * IN, "REV")
    c.setFont(F["sans-bold"], 10)
    c.drawString(19.4 * IN, 2.25 * IN, "03/14/2026")
    c.drawString(21.6 * IN, 2.25 * IN, '1/8" = 1\'-0"')
    c.drawString(19.4 * IN, 1.25 * IN, "2")
    c.setFont(F["sans-bold"], 7)
    c.drawString(24.8 * IN, 2.6 * IN, "REV   DATE          DESCRIPTION")
    c.setFont(F["sans"], 7)
    c.drawString(24.8 * IN, 2.4 * IN, "1     01/20/2026    ISSUED FOR PERMIT")
    c.drawString(24.8 * IN, 2.25 * IN, "2     03/14/2026    ISSUED FOR BID")
    c.setFont(F["sans"], 8)
    c.drawString(29.9 * IN, 2.5 * IN, "SHEET")
    c.setFont(F["sans-bold"], 30)
    c.drawString(29.9 * IN, 1.2 * IN, number)


def _bottom_body(c: canvas.Canvas, number: str) -> None:
    H = 22 * IN
    c.setFont(F["sans"], 11)
    if number == "G0.01":
        c.setFont(F["sans-bold"], 16)
        c.drawString(2 * IN, H - 2 * IN, "DRAWING INDEX")
        c.setFont(F["sans"], 11)
        for k, (n, t) in enumerate(BOTTOM_SHEETS):
            c.drawString(2 * IN, H - (2.6 + 0.35 * k) * IN, n)
            c.drawString(4 * IN, H - (2.6 + 0.35 * k) * IN, t.replace("\n", " "))
    elif number == "S1.01":
        c.drawString(2 * IN, H - 2 * IN, "REFER TO A1.01 FOR DIMENSIONS.")
        c.drawString(2 * IN, H - 2.4 * IN, "FOOTING AT ENTRY: SEE 2/A5.01.")
    elif number == "A1.01":
        c.drawString(2 * IN, H - 2 * IN, "SEE A5.01 FOR WALL TYPES. TYPICAL HEAD: 1/A5.01")
        c.drawString(2 * IN, H - 2.4 * IN, "AHU-1 ABOVE CEILING. P-1 AND RTU-2 BY OTHERS.")
        c.drawString(2 * IN, H - 2.8 * IN, "THIS SHEET: A1.01")
        c.rect(3 * IN, 6 * IN, 20 * IN, 10 * IN)
    elif number == "A5.01":
        for k, lab in enumerate(["WALL HEAD", "FOOTING AT ENTRY"]):
            x = (2 + 12 * k) * IN
            c.setLineWidth(1)
            c.rect(x, 8 * IN, 9 * IN, 9 * IN)
            c.circle(x + 0.5 * IN, 7.4 * IN, 0.32 * IN)
            c.setFont(F["sans-bold"], 12)
            c.drawCentredString(x + 0.5 * IN, 7.32 * IN, str(k + 1))
            c.drawString(x + 1.1 * IN, 7.33 * IN, lab)
    elif number == "M1.01":
        c.drawString(2 * IN, H - 2 * IN, "SEE S1.01 FOR STRUCTURE. AHU-1 ON ROOF CURB.")


def make_bottom_set(path: Path, sheets=BOTTOM_SHEETS) -> Path:
    c = canvas.Canvas(str(path), pagesize=(34 * IN, 22 * IN))
    c.setTitle("Harbor View Branch Library")
    for number, title in sheets:
        _bottom_sheet(c, number, title)
        _bottom_body(c, number)
        c.showPage()
    c.save()
    return path


VERT = [("E-100", "ELECTRICAL", "SITE PLAN"), ("E-101", "ELECTRICAL", "ONE-LINE DIAGRAM"),
        ("E-102", "ELECTRICAL", "PANEL SCHEDULES"), ("E-103", "ELECTRICAL", "DETAILS")]


def make_vertical_set(path: Path) -> Path:
    """Right-edge title block whose text runs bottom-to-top (as many CAD exports do); sheet number upright."""
    W, H = 34 * IN, 22 * IN
    c = canvas.Canvas(str(path), pagesize=(W, H))
    for number, head, sub in VERT:
        c.setLineWidth(1.5)
        c.rect(0.5 * IN, 0.5 * IN, W - IN, H - IN)
        c.line(W - 3 * IN, 0.5 * IN, W - 3 * IN, H - 0.5 * IN)

        def vtext(x: float, y: float, txt: str, font: str, size: float) -> None:
            c.saveState()
            c.translate(x, y)
            c.rotate(90)
            c.setFont(font, size)
            c.drawString(0, 0, txt)
            c.restoreState()

        vtext(W - 2.5 * IN, 6 * IN, "RIVERBEND WATER PLANT IMPROVEMENTS", F["sans-bold"], 18)
        vtext(W - 2.1 * IN, 7 * IN, "CITY OF RIVERBEND", F["sans"], 12)
        vtext(W - 1.6 * IN, 7.5 * IN, head, F["sans-bold"], 16)
        vtext(W - 1.3 * IN, 7.5 * IN, sub, F["sans-bold"], 16)
        vtext(W - 0.9 * IN, 1.2 * IN, "7/10/23 FOR BIDDING", F["sans"], 8)
        c.setFont(F["sans-bold"], 32)
        c.drawString(W - 2.6 * IN, 1.0 * IN, number)
        c.setFont(F["sans"], 11)
        c.drawString(2 * IN, H - 2 * IN, "SEE E-102 FOR PANEL SCHEDULES." if number != "E-102" else "SEE E-101.")
        c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------- read_sheets


def test_read_sheets_corpus_right_title_block(drawings):
    r0, r1 = drawings
    sh = S.read_sheets(r1)
    assert [s.number for s in sh] == ["E-001", "E-101", "E-201", "E-301", "E-501"]
    assert [s.title for s in sh] == ["GENERAL NOTES AND SYMBOLS", "ELECTRICAL ONE-LINE DIAGRAM", "PANEL SCHEDULES",
                                     "POWER PLAN - LEVEL 1", "ELECTRICAL DETAILS"]
    for s in sh:
        assert (s.rev, s.date, s.size, s.discipline, s.source) == ("1", "2026-10-20", "ARCH D", "E", "title-block")
        assert s.project == "RIVERSIDE PUMP STATION UPGRADE" and s.confidence >= 0.8
        assert s.title_block.x0 == pytest.approx(31.5 * IN, abs=4) and s.title_block.height > 20 * IN
        assert [r["rev"] for r in s.revisions] == ["0", "1"]
    assert sh[3].scale == '1/4" = 1\'-0"' and sh[1].scale == "NONE"
    assert S.read_sheets(r0)[0].rev == "0"


def test_read_sheets_bottom_title_block(tmp_path):
    src = make_bottom_set(tmp_path / "bottom.pdf")
    sh = S.read_sheets(src)
    assert [s.number for s in sh] == [n for n, _ in BOTTOM_SHEETS]
    assert sh[0].title == "COVER SHEET AND DRAWING INDEX"  # two-line title, not the larger firm name
    assert [s.title for s in sh[1:]] == ["FOUNDATION PLAN", "FLOOR PLAN", "DETAILS", "MECHANICAL PLAN"]
    for s in sh:
        assert (s.rev, s.date, s.size, s.scale) == ("2", "03/14/2026", "ANSI D", '1/8" = 1\'-0"')
        assert s.project == "HARBOR VIEW BRANCH LIBRARY"
        tb = s.title_block
        assert tb.top == pytest.approx(19 * IN, abs=4) and tb.width > 30 * IN  # along the bottom edge
    assert [s.discipline for s in sh] == ["G", "S", "A", "A", "M"]


def test_read_sheets_rotated_pages(drawings, tmp_path):
    rot = tmp_path / "rot.pdf"
    with pikepdf.open(drawings[0]) as pdf:
        for i, a in ((1, 90), (2, 180), (3, 270)):
            pdf.pages[i].Rotate = a
        pdf.save(rot)
    sh = S.read_sheets(rot)
    assert [s.number for s in sh] == ["E-001", "E-101", "E-201", "E-301", "E-501"]
    assert sh[2].title == "PANEL SCHEDULES" and sh[3].title == "POWER PLAN - LEVEL 1"
    # title block reported in displayed space: on the 180° page it is at the displayed left
    assert sh[2].title_block.x1 < 6 * IN


def test_read_sheets_vertical_title_block_text(tmp_path):
    src = make_vertical_set(tmp_path / "vertical.pdf")
    sh = S.read_sheets(src)
    assert [s.number for s in sh] == [v[0] for v in VERT]
    assert [s.title for s in sh] == [f"{h} {t}" for _, h, t in VERT]  # repeated (bigger) project name skipped
    assert all(s.project.startswith("RIVERBEND WATER PLANT IMPROVEMENTS") for s in sh)
    assert all(s.text_dir == 90 and s.date == "7/10/23" for s in sh)
    with S.open_set(src) as d:
        texts = [ln.text for ln in d[0].lines()]
    assert "RIVERBEND WATER PLANT IMPROVEMENTS" in texts and "STNEMEVORPMI" not in " ".join(texts)


def test_helpers():
    assert S.size_name(36 * IN, 24 * IN) == "ARCH D" and S.size_name(22 * IN, 34 * IN) == "ANSI D"
    assert S.size_name(10 * IN, 10 * IN) == "10 x 10 in"
    assert S.discipline_of("FP-101") == "FP" and S.discipline_of("EP2") == "E" and S.discipline_of("A1.01") == "A"
    assert S.norm_number("e-101") == S.norm_number("E101")
    assert all(S.SHEET_RX.match(x) for x in ("E-101", "A1.01", "M-2", "FP-101", "E101"))
    assert not any(S.SHEET_RX.match(x) for x in ("3/E-501", "LP-1-1", "2026"))


# ---------------------------------------------------------------- index


def test_index_corpus(drawings):
    res = S.index(drawings[1], quiet=True)
    assert res.ok and res.output.name == "drawings_rev1.indexed.pdf"
    xlsx = res.extra_outputs[0]
    ws = openpyxl.load_workbook(xlsx)["Drawing Log"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][:7] == ("Sheet No.", "Title", "Rev", "Date", "Page", "Discipline", "Size")
    assert [r[0] for r in rows[1:]] == ["E-001", "E-101", "E-201", "E-301", "E-501"]
    assert rows[2][1:5] == ("ELECTRICAL ONE-LINE DIAGRAM", "1", "2026-10-20", 2)
    bms = read_bookmarks(res.output)
    assert [b.title for b in bms] == ["Electrical (E)"]
    assert [(c.title, c.page) for c in bms[0].children][:2] == [("E-001 — GENERAL NOTES AND SYMBOLS", 0),
                                                                ("E-101 — ELECTRICAL ONE-LINE DIAGRAM", 1)]
    with pikepdf.open(res.output) as pdf:
        assert [p.label for p in pdf.pages] == ["E-001", "E-101", "E-201", "E-301", "E-501"]
    assert drawings[1].exists()  # input untouched


def test_index_discipline_order(tmp_path):
    src = make_bottom_set(tmp_path / "bottom.pdf")
    res = S.index(src, quiet=True)
    bms = read_bookmarks(res.output)
    assert [b.title for b in bms] == ["General (G)", "Architectural (A)", "Structural (S)", "Mechanical (M)"]
    assert [c.title.split(" — ")[0] for c in bms[1].children] == ["A1.01", "A5.01"]
    assert [c.page for c in bms[1].children] == [2, 3]
    xo = S.index(src, bookmark=False, quiet=True)
    assert xo.output.suffix == ".xlsx" and xo.gate is None
    order = [r[0] for r in openpyxl.load_workbook(xo.output)["Drawing Log"].iter_rows(min_row=2, values_only=True)]
    assert order == ["G0.01", "A1.01", "A5.01", "S1.01", "M1.01"]


# ---------------------------------------------------------------- read_set


def test_read_set_corpus(drawings, tmp_path):
    res = S.read_set(drawings[1], out=tmp_path / "set.xlsx", quiet=True)
    d = res.data
    assert d["counts"] == {"equipment_list": 1, "general_notes": 1, "one_line": 1, "panel_schedule": 1}
    by = {it["kind"]: it for it in d["items"]}
    p = by["panel_schedule"]
    assert (p["sheet"], p["panel"], p["voltage"], p["phase"], p["wires"], p["amps"], p["main"]) == \
        ("E-201", "LP-1", "208Y/120V", "3", "4", "225", "MLO")
    assert p["n_circuits"] == 24 and p["circuits"][6] == {"CKT": "7", "DESCRIPTION": "SPD MONITOR", "BKR": "20/1", "VA": "50"}
    n = by["general_notes"]
    assert n["sheet"] == "E-001" and n["n_notes"] == 7 and n["notes"][6]["text"].startswith("ADD SURGE PROTECTIVE")
    assert set(n["refs"]) == {"E-101", "E-201", "E-501"}
    e = by["equipment_list"]
    assert e["title"] == "EQUIPMENT LIST" and e["items"][2] == {"TAG": "EF-1", "DESCRIPTION": "EXHAUST FAN", "HP": "3",
                                                                 "VOLTS": "480", "FLA": "4.8"}
    o = by["one_line"]
    assert o["sheet"] == "E-101" and o["heavy_lines"] >= 3
    xf = next(x for x in o["equipment"] if x["type"] == "transformer")
    assert xf["kva"] == "1500" and xf["voltage"] == "12.47KV-480Y/277V"
    wb = openpyxl.load_workbook(res.output)
    assert wb.sheetnames[0] == "Summary" and "E-201 Panel LP-1" in wb.sheetnames
    ws = wb["E-201 Panel LP-1"]
    assert [c.value for c in ws[1]] == ["CKT", "DESCRIPTION", "BKR", "VA"] and ws["D2"].value == 460
    assert json.loads(res.extra_outputs[1].read_text())["counts"]["panel_schedule"] == 1


# ---------------------------------------------------------------- compare_sets


@pytest.fixture(scope="module")
def compared(drawings):
    return S.compare_sets(drawings[0], drawings[1], quiet=True)


def test_compare_sets_corpus(compared):
    res = compared
    d = res.data
    assert res.ok
    assert d["changed"] == ["E-001", "E-101", "E-201"]
    assert d["unchanged"] == ["E-301", "E-501"] and d["title_block_only"] == ["E-301", "E-501"]
    assert d["added"] == [] and d["removed"] == []
    rows = {r["sheet"]: r for r in d["sheets"]}
    assert all(r["title_block_updated"] for r in rows.values())
    assert rows["E-301"]["title_block_notes"] == ["REV 0 → 1", "DATE 2026-09-15 → 2026-10-20"]
    assert rows["E-101"]["changes"] == 1 and "SPD 480V" in rows["E-101"]["boxes"][0]["new_text"]
    assert any("ADD SURGE PROTECTIVE" in t["new"] for t in rows["E-001"]["text_changes"])
    # no change region may touch the title block (right 4.5 in)
    for r in rows.values():
        assert all(b["box"][2] < 31.5 * IN for b in r["boxes"])
    with pikepdf.open(res.output) as pdf:
        assert len(pdf.pages) == 3
        assert [p.label for p in pdf.pages] == ["E-001", "E-101", "E-201"]
        clouds = sum(1 for p in pdf.pages for a in (p.obj.get("/Annots") or []) if a.get("/Subtype") == "/Polygon")
        assert clouds == sum(r["changes"] for r in rows.values())
    assert [b.title.split(" — ")[0] for b in read_bookmarks(res.output)] == ["E-001", "E-101", "E-201"]


def test_compare_sets_reports(compared):
    rep_pdf, rep_xlsx = compared.extra_outputs
    assert rep_pdf.suffix == ".pdf" and rep_xlsx.suffix == ".xlsx"
    from pdfcore.gate import GateOptions, run_gate

    assert run_gate(rep_pdf, options=GateOptions(profile="typeset", margins_in=0.5)).passed
    with pikepdf.open(rep_pdf) as pdf:
        fonts = {str(f.get("/BaseFont")) for p in pdf.pages for f in p.Resources.Font.values()}
    assert fonts and all("Inter" in f for f in fonts)
    wb = openpyxl.load_workbook(rep_xlsx)
    assert wb.sheetnames == ["Summary", "Changes", "Text changes"]
    summ = {r[0]: r for r in wb["Summary"].iter_rows(min_row=2, values_only=True)}
    assert summ["E-301"][2] == "title block only" and summ["E-101"][2] == "changed"


def test_compare_sets_matches_by_number_not_page(drawings, tmp_path, corpus):
    import sys

    sys.path.insert(0, str(ROOT / "corpus"))
    import build

    extra = tmp_path / "e601.pdf"
    c = canvas.Canvas(str(extra), pagesize=build.ARCH_D)
    build.title_block(c, "E-601", "LIGHTING PLAN", 1, "2026-10-20")
    c.setFont(F["sans"], 12)
    c.drawString(2 * IN, 20 * IN, "LIGHTING FIXTURES PER SCHEDULE")
    c.showPage()
    c.save()
    new = tmp_path / "shuffled.pdf"
    with pikepdf.open(drawings[1]) as pdf, pikepdf.open(extra) as ex:
        out = pikepdf.new()
        for i in (2, 0, 1, 4):  # E-201, E-001, E-101, E-501 — E-301 removed
            out.pages.append(pdf.pages[i])
        out.pages.append(ex.pages[0])
        out.save(new)
    res = S.compare_sets(drawings[0], new, quiet=True)
    d = res.data
    assert d["changed"] == ["E-201", "E-001", "E-101"] and d["removed"] == ["E-301"] and d["added"] == ["E-601"]
    assert d["unchanged"] == ["E-501"]
    with pikepdf.open(res.output) as pdf:
        assert [p.label for p in pdf.pages] == ["E-201", "E-001", "E-101", "E-601"]


def test_overlay_render_has_red_and_blue(compared, tmp_path):
    import numpy as np
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(compared.output))
    img = doc[1].render(scale=50 / 72).to_pil().convert("RGB")
    png = tmp_path / "overlay_e101.png"
    img.save(png)
    a = np.asarray(img).astype(int)
    blue = (a[..., 2] > 180) & (a[..., 0] < 90)
    assert blue.sum() > 30  # the added SPD box is blue
    doc.close()


# ---------------------------------------------------------------- link_set


def _link_targets(path: Path) -> list[dict]:
    return list_links(path)


def _covers(link_rect_pdf, hit: Rect, page_h: float) -> bool:
    llx, lly, urx, ury = link_rect_pdf
    r = Rect(llx, page_h - ury, urx, page_h - lly)
    return r.overlap_fraction(hit) > 0.6


def test_link_set_corpus(drawings, tmp_path):
    src = tmp_path / "set.pdf"
    shutil.copy(drawings[1], src)
    res = S.link_set(src, quiet=True)
    assert res.ok
    links = _link_targets(res.output)
    sheets = {s.number: s.page + 1 for s in S.read_sheets(src, scan_scale=False)}
    from pdfcore.text import search

    hits = search(src, r"SEE (E-201)", regex=True)
    assert hits
    for h in hits:
        tok = search(src, "E-201", pages=[h.page])
        r = next(t.rects[0] for t in tok if t.rects[0].intersects(h.rects[0]))
        assert any(l["page"] == h.page + 1 and l["to_page"] == sheets["E-201"] and _covers(l["rect"], r, 1728)
                   for l in links), f"SEE E-201 on p.{h.page + 1} not linked"
    assert all(l["page"] != l["to_page"] for l in links)  # never to itself
    kinds = {(l["sheet"], l["text"]): l for l in res.data["links"]}
    assert kinds[("E-001", "3/E-501")]["kind"] == "detail" and kinds[("E-001", "3/E-501")]["dest"] == "detail (zoomed)"
    assert kinds[("E-201", "E-101")]["kind"] == "reference"
    assert res.data["per_sheet"]["E-001"]["links"] == 3 and res.data["per_sheet"]["E-501"]["links"] == 0
    # detail destination zooms onto detail 3 (third frame from the left on E-501)
    with pikepdf.open(res.output) as pdf:
        a = next(a for a in pdf.pages[0].Annots if "3/E-501" in str(a.get("/Contents", "")) or
                 (a.get("/Dest") is not None and str(a.Dest[1]) == "/FitR"))
        dest = a.Dest
        assert str(dest[1]) == "/FitR" and dest[0].objgen == pdf.pages[4].obj.objgen
        l, b, r, t = (float(v) for v in dest[2:6])
        assert 21.5 * IN - 30 < l < 21.5 * IN and r > 29.5 * IN and b < 7.1 * IN and t > 18 * IN
    # running again over the linked output adds nothing (no double links)
    again = S.link_set(res.output, quiet=True)
    assert again.data["total"] == 0 and again.data["skipped_existing"] == res.data["total"]


def test_link_set_bottom_set_index_details_and_tags(tmp_path):
    src = make_bottom_set(tmp_path / "bottom.pdf")
    res = S.link_set(src, quiet=True)
    by = [(l["sheet"], l["text"], l["kind"], l["to_sheet"], l["dest"]) for l in res.data["links"]]
    idx = [x for x in by if x[0] == "G0.01"]
    assert [x[3] for x in idx] == ["S1.01", "A1.01", "A5.01", "M1.01"] and all(x[2] == "index" for x in idx)
    assert ("S1.01", "A1.01", "reference", "A1.01", "sheet") in by
    assert ("S1.01", "2/A5.01", "detail", "A5.01", "detail (zoomed)") in by
    assert ("A1.01", "1/A5.01", "detail", "A5.01", "detail (zoomed)") in by
    assert ("M1.01", "S1.01", "reference", "S1.01", "sheet") in by
    assert not [x for x in by if x[0] == "A1.01" and x[3] == "A1.01"]  # no self link
    texts = {x[1] for x in by}
    assert not texts & {"P-1", "AHU-1", "RTU-2"}
    # index rows link number + title
    row = next(l for l in res.data["links"] if l["sheet"] == "G0.01" and l["to_sheet"] == "M1.01")
    assert row["rect"][2] - row["rect"][0] > 2.5 * IN
    # bubbles found on the details sheet
    with S.open_set(src) as d:
        bub = S.detail_bubbles(d[3])
    assert set(bub) >= {"1", "2"} and bub["2"][0]["title"] == "FOOTING AT ENTRY" and bub["2"][0]["region"] is not None


# ---------------------------------------------------------------- slipsheet


def test_slipsheet_carries_markups_and_appends(drawings, tmp_path, corpus):
    import sys

    sys.path.insert(0, str(ROOT / "corpus"))
    import build

    marked = tmp_path / "set.pdf"
    with pikepdf.open(drawings[0]) as pdf:
        markup.add_cloud(pdf, 1, Rect(800, 300, 1200, 500), contents="check SPD location")
        markup.add_free_text(pdf, 1, Rect(800, 520, 1100, 560), "RFI 12")
        pdf.save(marked)
    revised = tmp_path / "revised.pdf"
    extra = tmp_path / "e601.pdf"
    c = canvas.Canvas(str(extra), pagesize=build.ARCH_D)
    build.title_block(c, "E-601", "LIGHTING PLAN", 1, "2026-10-20")
    c.showPage()
    c.save()
    with pikepdf.open(drawings[1]) as r1, pikepdf.open(extra) as ex:
        out = pikepdf.new()
        out.pages.append(r1.pages[1])  # E-101 only
        out.pages.append(ex.pages[0])  # new E-601
        out.save(revised)
    res = S.slipsheet(marked, revised, quiet=True)
    assert res.ok
    d = res.data
    assert [x["sheet"] for x in d["replaced"]] == ["E-101"] and d["replaced"][0]["markups"] == 2
    assert d["added"] == ["E-601"] and d["sheets"] == ["E-001", "E-101", "E-201", "E-301", "E-501", "E-601"]
    with pikepdf.open(res.output) as pdf:
        assert len(pdf.pages) == 6
        assert [p.label for p in pdf.pages] == d["sheets"]
        subs = [str(a.Subtype) for a in pdf.pages[1].Annots]
        assert "/Polygon" in subs and "/FreeText" in subs and "/Text" in subs  # cloud, text, slip note
        cloud = next(a for a in pdf.pages[1].Annots if a.Subtype == "/Polygon")
        assert cloud.P.objgen == pdf.pages[1].obj.objgen
        llx, lly, urx, ury = (float(v) for v in cloud.Rect)
        assert llx < 800 and urx > 1200 and 1728 - ury < 300 and 1728 - lly > 500  # same place
        assert any("E-101" in str(a.get("/Contents", "")) for a in pdf.pages[0].Annots if a.Subtype == "/Text")
    sh = S.read_sheets(res.output, scan_scale=False)
    assert sh[1].rev == "1" and sh[0].rev == "0"  # new E-101, old E-001
    from pdfcore.text import search

    assert search(res.output, "SPD 480V", pages=[1])
    bms = read_bookmarks(res.output)
    assert [c.title.split(" — ")[0] for c in bms[0].children] == d["sheets"]
    assert marked.exists() and revised.exists()


# ---------------------------------------------------------------- CLI


def test_cli_subcommands(drawings, tmp_path, capsys):
    src = tmp_path / "set.pdf"
    shutil.copy(drawings[1], src)
    assert main(["plans", "index", str(src)]) == 0
    out = capsys.readouterr().out
    assert "E-301" in out and "PASS" in out and (tmp_path / "set.sheet-index.xlsx").exists()
    assert main(["plans", "read", str(src)]) == 0
    assert "panel_schedule" in capsys.readouterr().out
    assert main(["plans", "link", str(src)]) == 0
    assert (tmp_path / "set.linked.pdf").exists()
    old = tmp_path / "old.pdf"
    shutil.copy(drawings[0], old)
    assert main(["plans", "compare", str(old), str(src)]) == 0
    out = capsys.readouterr().out
    assert "E-301" in out and "title block only" in out
    assert main(["plans", "slipsheet", str(old), str(src)]) == 0
    assert (tmp_path / "old.slipsheeted.pdf").exists()


# ---------------------------------------------------------------- real bid sets (local only, git-ignored)


@pytest.mark.skipif(not FLINT.exists(), reason="real bid set not available")
def test_real_set_title_blocks_and_links(tmp_path):
    sh = S.read_sheets(FLINT, scan_scale=False)
    assert len(sh) == 50
    found = [s for s in sh if s.source == "title-block"]
    assert len(found) >= 45
    nums = [s.number for s in found]
    assert len(set(nums)) == len(nums)
    assert all(S.SHEET_RX.match(n) for n in nums)
    titles = [s.title for s in found]
    assert all(t and re.search(r"[A-Z]{4,}", t) for t in titles)
    assert not any("LACIRTCELE" in t or "NOITATNEMURTSNI" in t for t in titles)  # not reversed
    assert sum(t.startswith(("ELECTRICAL", "INSTRUMENTATION")) for t in titles) >= 40
    src = tmp_path / "flint.pdf"
    shutil.copy(FLINT, src)
    res = S.link_set(src, quiet=True)
    links = list_links(res.output)
    pages = {s.key: s.page for s in found}
    # every "SEE E-xxx" reference to another sheet of the set is linked to that sheet
    with S.open_set(src) as d:
        refs = []
        for s in found:
            for ln in d[s.page].lines():
                for m in re.finditer(r"\bSEE\s+(?:SHEET\s+|DWG\.?\s+)?([EI]-\d{3})\b", ln.text):
                    k = S.norm_number(m.group(1))
                    if k in pages and pages[k] != s.page:
                        refs.append((s.page, pages[k]))
            d.release(s.page)
    assert refs
    linked = {(l["page"] - 1, l["to_page"] - 1) for l in links}
    assert all(r in linked for r in refs)
    assert res.data["total"] >= len(refs)


@pytest.mark.skipif(not ADDENDUM.exists(), reason="real addendum set not available")
def test_real_addendum_robustness():
    sh = S.read_sheets(ADDENDUM, scan_scale=False)
    assert len(sh) == 30
    found = [s for s in sh if s.source == "title-block"]
    assert len(found) >= 27
    nums = [s.number for s in found]
    assert len(set(nums)) == len(nums) and all(S.SHEET_RX.match(n) for n in nums)
    by = {s.number: s for s in found}
    assert {"E-101", "M-101", "P-101", "S-100"} <= set(by)
    assert by["E-101"].title == "ELECTRICAL SITE PLAN"
