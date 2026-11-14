"""Scale, measurement, takeoff, sheet sizes, resize/tile and layers (pdfcore.ops.plans.measure)."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pdfplumber
import pikepdf
import pytest

from pdfcore import engines
from pdfcore.cli import main
from pdfcore.ops.plans import measure as M

IN = 72.0
# E-301 (corpus/build.py, ARCH D 36x24 in): building outline 22x14 in at (3, 4) in bottom-left → top-left points
OUTLINE = [(216.0, 432.0), (1800.0, 432.0), (1800.0, 1440.0), (216.0, 1440.0)]
BAR = ((288.0, 1512.0), (576.0, 1512.0))  # scale bar 4 in = 16 ft


@pytest.fixture
def scaled(work) -> Path:
    src = work("drawings_rev0")
    res = M.set_scale(src, sheet="E-301", calibrate_with="bar", quiet=True)
    assert res.ok, res.line()
    return res.output


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,ratio,display", [
    ('1/4"=1\'-0"', 48, "ft-in"), ('1/8" = 1\'', 96, "ft-in"), ('3/16"=1\'-0"', 64, "ft-in"),
    ('1"=20\'', 240, "ft"), ("1:100", 100, "m"), ("1:50 (mm)", 50, "mm"), ('SCALE: 1/4" = 1\'-0"', 48, "ft-in"),
    ('1-1/2"=1\'-0"', 8, "ft-in"), ("1 in = 4 ft", 48, "ft-in"), ("SCALE: 1″ = 30′", 360, "ft"),
])
def test_parse_scale(text, ratio, display):
    s = M.parse_scale(text)
    assert s is not None and s.ratio == pytest.approx(ratio) and s.display == display


@pytest.mark.parametrize("text", ["NTS", "NONE", "N.T.S.", "Not to scale", "SCALE: NONE", "AS NOTED"])
def test_parse_scale_none(text):
    assert M.parse_scale(text) is None


def test_parse_length_and_format():
    assert M.parse_length("12'-6 1/2\"")[0] == pytest.approx(150.5)
    assert M.parse_length("16ft") == (192.0, "ft")
    assert M.parse_length("4.88m")[1] == "m"
    assert M.format_ft_in(12 + 6.5 / 12) == "12'-6 1/2\""
    assert M.format_ft_in(88) == "88'-0\""
    assert M.format_length(4.88, "m") == "4.88 m"
    assert M.format_area(4928, "ft") == "4,928.00 sq ft"
    with pytest.raises(ValueError):
        M.parse_scale("SEE 2/E-501")


def test_calibrate_two_points_snaps_to_standard():
    sc, measured = M.calibrate(*BAR, "16ft")
    assert measured == pytest.approx(48) and sc.label == '1/4" = 1\'-0"'
    sc, measured = M.calibrate((0, 0), (100, 0), "4.88m")
    assert sc.real_unit == "m" and sc.family == "custom"  # 1:175.7 is not a standard metric scale
    assert measured == pytest.approx(4880 / 25.4 / (100 / 72), rel=1e-6)


# ---------------------------------------------------------------- reading the sheet
def test_scale_bar_and_note_on_e301(work):
    src = work("drawings_rev0")
    pi = M.find_sheet_page(src, "E-301")
    assert pi == 3
    bar = M.find_scale_bar(src, pi)
    assert bar is not None and bar["paper_pt"] == pytest.approx(4 * IN)
    assert bar["ratio"] == pytest.approx(48) and bar["scale"].label == '1/4" = 1\'-0"'
    labels = M.find_scale_labels(src, pi)
    assert [lb["scale"].ratio for lb in labels] == [48]
    e501 = M.find_scale_labels(src, M.find_sheet_page(src, "E-501"))
    assert len(e501) == 3 and all(lb["scale"] is None for lb in e501)  # SCALE: NONE under each detail


def test_calibrate_e301_writes_readable_viewport(scaled):
    rows = M.read_scales(scaled)
    assert len(rows) == 1
    vp = rows[0]
    assert vp["page"] == 3 and vp["ratio"] == pytest.approx(48, rel=1e-9) and vp["label"] == '1/4" = 1\'-0"'
    with pikepdf.open(scaled) as pdf:
        v = pdf.pages[3].obj.VP[0]
        assert v.Type == "/Viewport"
        m = v.Measure
        assert m.Type == "/Measure" and m.Subtype == "/RL" and str(m.R) == "1 in = 4 ft"
        assert str(m.X[0].U) == "ft" and float(m.X[0].C) == pytest.approx(4 / 72, rel=1e-10)
        assert [str(d.U) for d in m.D] == ["ft", "in"] and m.D[1].F == "/F" and int(m.D[1].D) == 16
        assert str(m.A[0].U) == "sq ft"
        assert [float(x) for x in v.BBox] == [0, 0, 36 * IN, 24 * IN]
    for pi in (0, 1, 2, 4):  # other sheets untouched
        with pikepdf.open(scaled) as pdf:
            assert "/VP" not in pdf.pages[pi].obj


def test_auto_scale_from_sheet_text(work):
    src = work("drawings_rev0")
    res = M.set_scale(src, quiet=True)  # whole set: only E-301 carries a scale
    assert res.ok, res.line()
    assert [(v["page"], v["ratio"]) for v in res.data["viewports"]] == [(3, pytest.approx(48))]
    with pytest.raises(ValueError, match="not to scale"):
        M.set_scale(src, sheet="E-501", quiet=True)


# ---------------------------------------------------------------- measure
def test_measure_outline(scaled):
    res = M.measure(scaled, sheet="E-301", length=[OUTLINE[0], OUTLINE[1]], area=OUTLINE, perimeter=OUTLINE,
                    count=[(360, 1296), (648, 1296), (936, 1296)], quiet=True)
    ms = {m["kind"]: m for m in res.data["measurements"]}
    assert res.output is None
    assert ms["length"]["value"] == pytest.approx(88) and ms["length"]["text"] == "88'-0\""
    assert ms["area"]["value"] == pytest.approx(4928) and ms["area"]["unit"] == "sq ft"
    assert ms["area"]["text"] == "4,928.00 sq ft"
    assert ms["perimeter"]["value"] == pytest.approx(2 * (88 + 56))
    assert ms["count"]["value"] == 3
    side = M.measure(scaled, sheet="E-301", length=[OUTLINE[1], OUTLINE[2]], quiet=True).data["measurements"][0]
    assert side["value"] == pytest.approx(56)
    # holes and unit conversion
    hole = [(400, 600), (500, 600), (500, 700), (400, 700)]
    a = M.measure(scaled, page=3, area=OUTLINE, holes=[hole], quiet=True).data["measurements"][0]
    assert a["value"] == pytest.approx(4928 - (100 / 72 * 4) ** 2)
    m = M.measure(scaled, page=3, length=[OUTLINE[0], OUTLINE[1]], units="m", quiet=True).data["measurements"][0]
    assert m["value"] == pytest.approx(88 * 0.3048) and m["text"] == "26.82 m"


def test_measure_needs_scale(work):
    src = work("drawings_rev0")
    with pytest.raises(ValueError, match="viewport"):
        M.measure(src, page=3, length=OUTLINE[:2], quiet=True)
    m = M.measure(src, page=3, length=OUTLINE[:2], scale='1/4"=1\'-0"', quiet=True).data["measurements"][0]
    assert m["value"] == pytest.approx(88)


def _render(path: Path, page: int, dpi: float = 72) -> np.ndarray:
    return np.asarray(engines.get("render").render(path, page, dpi=dpi).convert("RGB")).astype(int)


def test_measurement_annotations_render_with_captions(scaled, tmp_path):
    res = M.measure(scaled, sheet="E-301", length=[OUTLINE[0], OUTLINE[1]], area=OUTLINE,
                    perimeter=[(216, 1500), (1000, 1500), (1000, 1600)], count=[(360, 1296), (648, 1296)],
                    annotate=True, name="Outline", category="Shell", quiet=True)
    assert res.ok, res.line()
    with pikepdf.open(res.output) as pdf:
        annots = list(pdf.pages[3].obj.Annots)
        its = {str(a.get("/IT", "")) for a in annots}
        assert {"/LineDimension", "/PolygonDimension", "/PolyLineDimension"} <= its
        line = next(a for a in annots if a.Subtype == "/Line")
        assert line.Measure.Subtype == "/RL" and str(line.Contents) == "88'-0\"" and line.Cap is True
        poly = next(a for a in annots if a.Subtype == "/Polygon")
        assert str(poly.Contents) == "4,928.00 sq ft" and "/N" in poly.AP
        assert sum(1 for a in annots if a.Subtype == "/Circle") == 2
    before, after = _render(scaled, 3), _render(res.output, 3)
    # area caption at the outline centroid: dark red text on a white box where there was a plain drawing
    cx, cy = 1008, 936
    reg_a = after[cy - 8: cy + 8, cx - 40: cx + 40]
    reg_b = before[cy - 8: cy + 8, cx - 40: cx + 40]
    red = (reg_a[..., 0] > 120) & (reg_a[..., 1] < 90) & (reg_a[..., 2] < 90)
    assert red.sum() > 20 and not np.array_equal(reg_a, reg_b)
    # length caption centred above the top edge
    top = after[432 - 26: 432 - 2, 1008 - 30: 1008 + 30]
    assert ((top[..., 0] > 120) & (top[..., 1] < 90)).sum() > 15


def test_multiple_viewports_per_sheet(work, tmp_path):
    """Two details at different scales on one sheet: notes found, framed, and the right one measures."""
    from reportlab.pdfgen import canvas

    src = tmp_path / "details.pdf"
    c = canvas.Canvas(str(src), pagesize=(24 * IN, 18 * IN))
    c.setFont("Helvetica", 10)
    c.rect(1 * IN, 6 * IN, 9 * IN, 10 * IN)
    c.drawString(1.2 * IN, 5.6 * IN, 'DETAIL 1   SCALE: 1/2" = 1\'-0"')
    c.rect(12 * IN, 6 * IN, 10 * IN, 10 * IN)
    c.drawString(12.2 * IN, 5.6 * IN, 'DETAIL 2   SCALE: 1" = 1\'-0"')
    c.drawString(14 * IN, 1 * IN, "SCALE: AS NOTED")
    c.save()
    res = M.set_scale(src, page=0, quiet=True)
    assert res.ok, res.line()
    labels = sorted(v["label"] for v in res.data["viewports"])
    assert labels == ['1" = 1\'-0"', '1/2" = 1\'-0"']
    H = 18 * IN
    # 2 in of paper in each detail: 4 ft at 1/2", 2 ft at 1"
    d1 = M.measure(res.output, page=0, length=[(2 * IN, H - 10 * IN), (4 * IN, H - 10 * IN)], quiet=True)
    d2 = M.measure(res.output, page=0, length=[(13 * IN, H - 10 * IN), (15 * IN, H - 10 * IN)], quiet=True)
    assert d1.data["measurements"][0]["value"] == pytest.approx(4)
    assert d2.data["measurements"][0]["value"] == pytest.approx(2)
    # an explicit extra viewport via bbox, metric
    res2 = M.set_scale(res.output, page=0, scale="1:50 (mm)", bbox=(1 * IN, 1 * IN, 9 * IN, 4 * IN), quiet=True)
    m = M.measure(res2.output, page=0, length=[(2 * IN, 2 * IN), (3 * IN, 2 * IN)], quiet=True)
    assert m.data["measurements"][0]["unit"] == "mm" and m.data["measurements"][0]["value"] == pytest.approx(1270)
    assert len(M.read_scales(res2.output)) == 3


def test_rotated_page_measure(work):
    src = work("drawings_rev0")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[3].obj.Rotate = 90
        pdf.save(src)
    with pikepdf.open(src) as pdf:
        g = M._geom(pdf.pages[3])
        to_vis = lambda x, top: g.visual(x, 24 * IN - top)  # unrotated top-left → displayed top-left  # noqa: E731
        bar = [to_vis(*p) for p in BAR]
        outline = [to_vis(*p) for p in OUTLINE]
    res = M.set_scale(src, page=3, calibrate_with=(bar[0], bar[1], "16ft"), quiet=True)
    assert res.ok
    m = M.measure(res.output, page=3, length=outline[:2], area=outline, quiet=True).data["measurements"]
    assert m[0]["value"] == pytest.approx(88) and m[1]["value"] == pytest.approx(4928)


# ---------------------------------------------------------------- takeoff
def _evaluate(wb) -> dict:
    """Tiny evaluator for the formulas the takeoff writer emits (products, SUM, SUBTOTAL 9, SUMIF(S))."""
    from openpyxl.utils import column_index_from_string, range_boundaries

    cache: dict = {}

    def val(ws, ref):
        key = (ws.title, ref)
        if key in cache:
            return cache[key]
        v = ws[ref].value
        if isinstance(v, str) and v.startswith("="):
            v = ev(ws, v[1:])
        cache[key] = v
        return v

    def rng(ws, r):
        if "!" in r:
            sh, r = r.split("!")
            ws = wb[sh]
        c0, r0, c1, r1 = range_boundaries(r.replace("$", ""))
        return [[(ws, ws.cell(rr, cc).coordinate) for cc in range(c0, c1 + 1)] for rr in range(r0, r1 + 1)]

    def num(x):
        return x if isinstance(x, (int, float)) else 0.0

    def ev(ws, f):
        m = re.fullmatch(r"(SUMIFS|SUMIF|SUBTOTAL|SUM)\((.*)\)", f)
        if m:
            args = [a.strip() for a in m.group(2).split(",")]
            if m.group(1) == "SUM":
                return sum(num(val(*c)) for row in rng(ws, args[0]) for c in row)
            if m.group(1) == "SUBTOTAL":
                cells = [c for row in rng(ws, args[1]) for c in row]
                return sum(num(val(*c)) for c in cells
                           if not str(c[0][c[1]].value or "").startswith("=SUBTOTAL"))
            if m.group(1) == "SUMIF":
                crit, target = rng(ws, args[0]), rng(ws, args[2])
                want = val(ws, args[1].replace("$", ""))
                return sum(num(val(*t[0])) for c, t in zip(crit, target) if val(*c[0]) == want)
            target = rng(ws, args[0])
            pairs = [(rng(ws, args[i]), args[i + 1]) for i in range(1, len(args), 2)]
            tot = 0.0
            for k, t in enumerate(target):
                ok = True
                for crit, w in pairs:
                    want = w.strip('"') if w.startswith('"') else val(ws, w.replace("$", ""))
                    ok &= val(*crit[k][0]) == want
                if ok:
                    tot += num(val(*t[0]))
            return tot
        expr = re.sub(r"\b([A-Z]{1,2}\d+)\b", lambda mm: repr(num(val(ws, mm.group(1)))), f)
        return eval(expr)  # noqa: S307 — only our own arithmetic formulas

    out = {}
    for ws in wb:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    out[(ws.title, c.coordinate)] = val(ws, c.coordinate)
    _ = column_index_from_string
    return out


SPEC = """
annotate: true
items:
  - {type: area, name: Slab on grade, category: Concrete, sheet: E-301, rects: [[216, 432, 1800, 1440]], waste: 5, unit_cost: 6.5}
  - {type: length, name: Exterior wall, category: Framing, sheet: E-301, rects: [[216, 432, 1800, 1440]], unit_cost: 40}
  - {type: length, name: Top edge, category: Framing, page: 3, points: "216,432;1800,432", waste: 10}
  - {type: count, name: Receptacles, category: Devices, sheet: E-301, points: [[360,1296],[648,1296],[936,1296]], unit_cost: 85}
  - {type: area, name: Slab metric, category: Concrete, unit: sq m, sheet: E-301, rects: [[216, 432, 1800, 1440]]}
"""


def test_takeoff_xlsx_formulas_and_totals(scaled, tmp_path):
    from openpyxl import load_workbook

    spec = tmp_path / "takeoff.yaml"
    spec.write_text(SPEC)
    res = M.takeoff(scaled, spec, quiet=True)
    assert res.ok, res.line()
    assert res.output is not None and res.output.suffix == ".pdf"  # annotate: true
    xlsx = res.extra_outputs[0]
    assert xlsx.exists() and xlsx.suffix == ".xlsx"
    items = {r["name"]: r for r in res.data["items"]}
    assert items["Slab on grade"]["quantity"] == pytest.approx(4928)
    assert items["Exterior wall"]["quantity"] == pytest.approx(288)
    assert items["Top edge"]["total"] == pytest.approx(88 * 1.1)
    assert items["Slab metric"]["quantity"] == pytest.approx(4928 * 0.3048 ** 2) and items["Slab metric"]["unit"] == "sq m"
    wb = load_workbook(xlsx)
    assert wb.sheetnames == ["Takeoff", "Summary"]
    tk, sm = wb["Takeoff"], wb["Summary"]
    formulas = [c.value for ws in wb for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert any(f.startswith("=SUMIFS(") for f in formulas) and any(f.startswith("=SUMIF(") for f in formulas)
    assert any(f.startswith("=SUBTOTAL(9,") for f in formulas)
    headers = [c.value for c in sm[4]]
    assert "Area (sq ft)" in headers and "Length (ft)" in headers and "Count (ea)" in headers and "Area (sq m)" in headers
    vals = _evaluate(wb)
    cost = 4928 * 1.05 * 6.5 + 288 * 40 + 3 * 85
    total_row = res.data["totals"]["total_row"]
    assert vals[("Takeoff", f"L{total_row}")] == pytest.approx(cost)
    # summary by category: Concrete sq ft, Framing ft
    col = {h: i + 1 for i, h in enumerate(headers)}
    rows = {sm.cell(r, 1).value: r for r in range(5, 12)}
    from openpyxl.utils import get_column_letter as L

    assert vals[("Summary", f"{L(col['Area (sq ft)'])}{rows['Concrete']}")] == pytest.approx(4928 * 1.05)
    assert vals[("Summary", f"{L(col['Length (ft)'])}{rows['Framing']}")] == pytest.approx(288 + 88 * 1.1)
    assert vals[("Summary", f"{L(col['Cost ($)'])}{rows['Total']}")] == pytest.approx(cost)
    assert res.data["totals"]["grand_cost"] == pytest.approx(cost)
    assert tk["J5"].value == "=G5*(1+I5)"


def test_takeoff_harvests_annotations(scaled, tmp_path):
    spec = tmp_path / "t.yaml"
    spec.write_text(SPEC)
    annotated = M.takeoff(scaled, spec, quiet=True).output
    res = M.takeoff(annotated, None, xlsx=tmp_path / "harvest.xlsx", quiet=True)
    assert res.output is None and res.extra_outputs == [tmp_path / "harvest.xlsx"]
    got = {(r["name"], r["type"]): r for r in res.data["items"]}
    assert got[("Slab on grade", "area")]["quantity"] == pytest.approx(4928)
    assert got[("Exterior wall", "perimeter")]["quantity"] == pytest.approx(288)
    assert got[("Top edge", "length")]["quantity"] == pytest.approx(88)
    assert got[("Receptacles", "count")]["quantity"] == 3
    assert got[("Slab metric", "area")]["unit"] == "sq m"


# ---------------------------------------------------------------- sizes / resize / tile
def test_detect_sizes():
    assert M.detect_size(36 * IN, 24 * IN)["name"] == "ARCH D"
    b = M.detect_size(17 * IN, 11 * IN)
    assert b["name"] == "ANSI B" and "Tabloid" in b["aliases"] and b["orientation"] == "landscape"
    assert M.detect_size(612, 792)["name"] == "Letter"
    assert M.detect_size(595.3, 841.9)["name"] == "A4"
    assert M.detect_size(841 / 25.4 * IN, 1189 / 25.4 * IN)["name"] == "A0"
    assert M.detect_size(14 * IN, 8.5 * IN)["name"] == "Legal"
    assert M.detect_size(23.9 * IN, 36.1 * IN)["name"] == "ARCH D"  # tolerance
    assert M.detect_size(20 * IN, 30 * IN)["name"] is None


def _outline_after(res, page=3):
    t = res.data["pages"][page]
    k, dx, dy = t["factor"], t["dx"], t["dy"]
    return [(k * x + dx, k * y + dy) for x, y in OUTLINE]


def test_resize_arch_d_to_ansi_b_keeps_measurements(scaled):
    res = M.resize(scaled, "ansi-b", quiet=True)
    assert res.ok, res.line()
    with pdfplumber.open(res.output) as pl:
        assert all((round(p.width / IN, 3), round(p.height / IN, 3)) == (17, 11) for p in pl.pages)
    assert M.sheet_sizes(res.output)[3]["name"] == "ANSI B"
    pts = _outline_after(res)
    m = M.measure(res.output, page=3, length=pts[:2], area=pts, quiet=True).data["measurements"]
    assert m[0]["value"] == pytest.approx(88) and m[1]["value"] == pytest.approx(4928)
    # ARCH D at 50 % (18x12) would cut the title block on 17x11, so the content-preserving fit is used
    assert res.data["pages"][3]["factor"] == pytest.approx(11 / 24)


def test_half_size_set_scale_label_halves(scaled):
    res = M.resize(scaled, "arch-b", quiet=True)  # ARCH D → ARCH B is an exact 50 % half-size set
    assert res.ok, res.line()
    assert res.data["pages"][3]["factor"] == 0.5
    vp = M.read_scales(res.output)[0]
    assert vp["label"] == '1/8" = 1\'-0"' and vp["ratio"] == pytest.approx(96) and vp["r"] == "1 in = 8 ft"
    pts = _outline_after(res)
    assert M.measure(res.output, page=3, length=pts[:2], quiet=True).data["measurements"][0]["text"] == "88'-0\""
    forced = M.resize(scaled, "ansi-b", factor=0.5, quiet=True)  # forced 50 % on 17x11: title block trimmed
    assert forced.data["pages"][3]["trimmed_in"] == [1.0, 1.0]
    assert not forced.ok and "clipped" in forced.line()
    pts = _outline_after(forced)
    assert M.measure(forced.output, page=3, length=pts[:2], quiet=True).data["measurements"][0]["value"] == pytest.approx(88)


def test_resize_moves_annotations(scaled):
    ann = M.measure(scaled, page=3, area=OUTLINE, annotate=True, quiet=True).output
    res = M.resize(ann, "arch-b", quiet=True)
    rows = M.harvest_measurements(res.output)
    assert rows[0]["type"] == "area" and rows[0]["quantity"] == pytest.approx(4928)
    with pikepdf.open(res.output) as pdf:
        a = pdf.pages[3].obj.Annots[0]
        v = [float(x) for x in a.Vertices]
        assert v[0] == pytest.approx(108) and float(a.Measure.X[0].C) == pytest.approx(8 / 72)


def test_tile_grid_and_labels(scaled, tmp_path):
    res = M.tile(scaled, paper="letter", pages=[3], quiet=True)
    assert res.ok, res.line()
    # ARCH D 36x24 on Letter landscape, 0.5 in margins (window 10 x 7.5) and 0.5 in overlap → 4 x 4
    assert res.data["grid"] == {3: [4, 4]}
    labels = [t["label"] for t in res.data["tiles"]]
    assert labels == [f"{r}{c}" for r in "ABCD" for c in range(1, 5)]
    with pikepdf.open(res.output) as pdf:
        assert len(pdf.pages) == 16
        for p in pdf.pages:
            cb = [float(v) for v in p.obj.CropBox]
            assert (round(cb[2] - cb[0], 2), round(cb[3] - cb[1], 2)) == (792, 612)
    with pdfplumber.open(res.output) as pl:
        p = pl.pages[1].crop([float(v) for v in pl.pages[1].cropbox])
        assert "A2" in p.extract_text() and "row A of D" in p.extract_text()
    img = _render(res.output, 1)
    assert img.shape[:2] == (612, 792)
    assert (img.min(axis=2) < 100).sum() > 200  # crop marks, label, key map, drawing
    # tabloid at 50 %
    res2 = M.tile(scaled, paper="tabloid", pages=[3], scale=0.5, quiet=True)
    assert res2.data["grid"] == {3: [1, 2]} or res2.data["grid"] == {3: [2, 1]}


# ---------------------------------------------------------------- layers
def test_layers_list_off_and_flatten(work):
    src = work("layers")
    names = [(l["name"], l["on"]) for l in M.list_layers(src)]
    assert names == [("Electrical", True), ("Mechanical", True), ("Notes", True)]
    off = M.set_layers(src, off=["MECHANICAL"], quiet=True)
    assert off.ok
    assert [(l["name"], l["on"]) for l in M.list_layers(off.output)][1] == ("Mechanical", False)
    flat = M.set_layers(off.output, flatten=True, quiet=True)
    assert flat.ok, flat.line()
    with pdfplumber.open(flat.output) as pl:
        txt = pl.pages[0].extract_text()
    assert "MECHANICAL LAYER" not in txt and "ELECTRICAL LAYER" in txt and "General notes layer" in txt
    with pikepdf.open(flat.output) as pdf:
        assert "/OCProperties" not in pdf.Root
        assert b"/OC" not in pdf.pages[0].obj.Contents.read_bytes()
    with pytest.raises(ValueError, match="unknown layer"):
        M.set_layers(src, off=["PLUMBING"], quiet=True)


def test_flatten_handles_form_xobjects_and_annotations(work):
    src = work("layers")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        ocgs = list(pdf.Root.OCProperties.OCGs)
        mech = ocgs[1]
        page = pdf.pages[0]
        font = page.obj.Resources.Font.F1
        form = pdf.make_stream(b"BT /F1 12 Tf 100 300 Td (MECH FORM) Tj ET", Type=pikepdf.Name.XObject,
                               Subtype=pikepdf.Name.Form, BBox=[0, 0, 612, 792],
                               Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)), OC=mech)
        keep = pdf.make_stream(b"BT /F1 12 Tf 100 250 Td (KEEP FORM) Tj ET", Type=pikepdf.Name.XObject,
                               Subtype=pikepdf.Name.Form, BBox=[0, 0, 612, 792],
                               Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)), OC=ocgs[0])
        page.obj.Resources.XObject = pikepdf.Dictionary(Fm=form, Fk=keep)
        page.contents_add(pdf.make_stream(b"q /Fm Do Q q /Fk Do Q"))
        ap = pdf.make_stream(b"0 0 1 rg 0 0 20 20 re f", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form,
                             BBox=[0, 0, 20, 20])
        annot = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Square,
                                                     Rect=[300, 300, 320, 320], OC=mech, AP=pikepdf.Dictionary(N=ap)))
        page.obj.Annots = pdf.make_indirect(pikepdf.Array([annot]))
        pdf.save(src)
    res = M.set_layers(src, off=["Mechanical"], flatten=True, quiet=True)
    assert res.ok, res.line()
    assert res.data["stats"] == {"sequences": 1, "xobjects": 1, "annotations": 1}
    with pdfplumber.open(res.output) as pl:
        txt = pl.pages[0].extract_text()
    assert "MECH FORM" not in txt and "KEEP FORM" in txt
    with pikepdf.open(res.output) as pdf:
        assert len(pdf.pages[0].obj.get("/Annots", [])) == 0
        assert "/OC" not in pdf.pages[0].obj.Resources.XObject.Fk


# ---------------------------------------------------------------- CLI
def test_cli_end_to_end(work, tmp_path, capsys):
    src = work("drawings_rev0")
    out = tmp_path / "s.pdf"
    assert main(["plans", "scale", str(src), "--sheet", "E-301", "--calibrate", "288,1512,576,1512=16ft",
                 "-o", str(out)]) == 0
    assert main(["plans", "scale", str(out), "--list"]) == 0
    assert "1 in = 4 ft" in capsys.readouterr().out
    assert main(["plans", "measure", str(out), "--sheet", "E-301", "--length", "216,432;1800,432",
                 "--rect", "216,432,1800,1440", "--annotate"]) == 0
    o = capsys.readouterr().out
    assert "88'-0\"" in o and "4,928.00 sq ft" in o and "PASS" in o
    assert main(["plans", "sizes", str(out)]) == 0
    assert "ARCH D" in capsys.readouterr().out
    assert main(["plans", "resize", str(out), "--to", "arch-b", "--pages", "4"]) == 0
    assert main(["plans", "tile", str(out), "--sheet", "E-301", "--paper", "tabloid"]) == 0
    assert main(["plans", "layers", str(work("layers")), "--off", "Mechanical", "--flatten"]) == 0
    assert main(["plans", "scale", str(src), "--sheet", "E-501"]) == 2  # SCALE: NONE
