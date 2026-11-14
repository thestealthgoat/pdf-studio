"""plans cad: DXF → PDF, PDF → DXF, ladder schematics."""

from __future__ import annotations

import shutil
from pathlib import Path

import ezdxf
import pikepdf
import pdfplumber
import pytest

from pdfcore.cli import main
from pdfcore.ops.plans import cad

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "pdfcore" / "ops" / "plans" / "examples" / "ladder-motor-starter.yaml"


# ------------------------------------------------------------------ helpers
def _fonts(path: Path) -> list[tuple[str, str, bool]]:
    """(BaseFont, Subtype, embedded) for every font on every page, including form XObjects."""
    out = []
    with pikepdf.open(path) as pdf:
        seen = set()

        def walk(res):
            if res is None:
                return
            for _k, f in (res.get("/Font") or {}).items():
                if f.objgen in seen:
                    continue
                seen.add(f.objgen)
                fd = f.get("/FontDescriptor")
                if fd is None and "/DescendantFonts" in f:
                    fd = f.DescendantFonts[0].get("/FontDescriptor")
                emb = fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
                out.append((str(f.get("/BaseFont")), str(f.Subtype), emb or str(f.Subtype) == "/Type3"))
            for _k, x in (res.get("/XObject") or {}).items():
                if x.get("/Subtype") == "/Form":
                    walk(x.get("/Resources"))

        for pg in pdf.pages:
            walk(pg.obj.get("/Resources"))
    return out


def _assert_fonts_ok(path: Path) -> None:
    fonts = _fonts(path)
    assert fonts, "no fonts found"
    assert not [f for f in fonts if f[1] == "/Type3"], fonts
    assert all(f[2] for f in fonts), fonts


def _build_dxf(path: Path) -> Path:
    from ezdxf.enums import TextEntityAlignment

    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 1  # inches
    doc.layers.add("HEAVY", color=1, lineweight=100)
    doc.layers.add("THIN", color=5, lineweight=13)
    doc.layers.add("MEDIUM", color=3, lineweight=35)
    msp = doc.modelspace()
    msp.add_line((0, 0), (20, 0), dxfattribs={"layer": "HEAVY"})          # 1.00 mm by layer
    msp.add_line((0, 2), (20, 2), dxfattribs={"layer": "THIN"})           # 0.13 mm by layer
    msp.add_line((0, 4), (20, 4), dxfattribs={"layer": "THIN", "lineweight": 50})  # entity override 0.50 mm
    blk = doc.blocks.new("TAG")
    blk.add_circle((0, 0), 0.5, dxfattribs={"lineweight": -2})            # BYBLOCK
    msp.add_blockref("TAG", (17, 7), dxfattribs={"layer": "MEDIUM", "lineweight": 70})
    msp.add_circle((5, 8), 2, dxfattribs={"layer": "MEDIUM"})
    msp.add_arc((12, 8), 2, 0, 180, dxfattribs={"layer": "MEDIUM"})
    msp.add_text("PUMP STATION P-1", dxfattribs={"height": 0.5}).set_placement((0, 10.5))
    msp.add_text("ROTATED", dxfattribs={"height": 0.4, "rotation": 30}).set_placement((14, 11), align=TextEntityAlignment.MIDDLE_CENTER)
    msp.add_mtext("NOTE ONE\\PNOTE TWO", dxfattribs={"char_height": 0.3, "insert": (0, 14)})
    h = msp.add_hatch(color=3)
    h.paths.add_polyline_path([(16, 12), (20, 12), (20, 14), (16, 14)], is_closed=True)
    lay = doc.layouts.new("SHEET-1")
    lay.page_setup(size=(431.8, 279.4), margins=(10, 10, 10, 10), units="mm")
    lay.add_viewport(center=(210, 140), size=(380, 240), view_center_point=(10, 7), view_height=18)
    lay.add_text("PAPERSPACE TITLE", dxfattribs={"height": 5}).set_placement((20, 20))
    doc.saveas(path)
    return path


def _dark_runs(img, x: int) -> list[int]:
    """Thicknesses (px) of dark runs down column x of a grayscale render."""
    g = img.convert("L")
    runs, cur = [], 0
    for y in range(g.height):
        if g.getpixel((x, y)) < 160:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


# ------------------------------------------------------------------ DXF → PDF
def test_dxf_to_pdf_lineweights_text_fonts(tmp_path):
    src = _build_dxf(tmp_path / "plan.dxf")
    res = cad.dxf_to_pdf(src, size="ansi-b", quiet=True)
    assert res.ok, res.line()
    assert res.output.name == "plan.plot.pdf" and src.exists()
    _assert_fonts_ok(res.output)
    with pdfplumber.open(res.output) as pdf:
        page = pdf.pages[0]
        assert (page.width, page.height) == (17 * 72, 11 * 72)
        text = page.extract_text()
        assert "PUMP STATION P-1" in text and "NOTE ONE" in text and "NOTE TWO" in text
        widths = sorted({round(o["linewidth"], 2) for o in page.lines + page.curves})
    mm = 72 / 25.4
    for want in (1.00, 0.13, 0.50):  # layer, layer, entity override
        assert any(abs(w - want * mm) < 0.05 for w in widths), (want, widths)
    # visibly different in a render: the three horizontal lines down a clear column
    from pdfcore import engines

    img = engines.get("render").render(res.output, 0, dpi=200)
    runs = _dark_runs(img, int(img.width * 0.85))
    lines = runs[-3:]  # bottom three runs: y=4 (0.50), y=2 (0.13), y=0 (1.00)
    assert lines[2] > lines[0] > lines[1], runs


def test_dxf_to_pdf_policies_scale_mono(tmp_path):
    src = _build_dxf(tmp_path / "plan.dxf")
    # plot to scale: 1"=1'-0" on ARCH D → the 20 in line is 20/12 in on paper
    res = cad.dxf_to_pdf(src, size="arch-d", scale='1"=1\'-0"', lineweights="by-layer", color="mono", quiet=True)
    assert res.ok and res.data["scale_ratio"] == pytest.approx(12)
    with pdfplumber.open(res.output) as pdf:
        page = pdf.pages[0]
        assert (page.width, page.height) == (36 * 72, 24 * 72)
        long = [o for o in page.lines + page.curves if o["bottom"] - o["top"] < 3 and o["x1"] - o["x0"] > 100]
        assert long and all(abs((o["x1"] - o["x0"]) - 20 / 12 * 72) < 3 for o in long), [(o["x0"], o["x1"]) for o in long]
        # by-layer ignores the 0.50 mm entity override on THIN → no 0.50 mm line
        widths = {round(o["linewidth"], 2) for o in long}
        assert not any(abs(w - 0.5 * 72 / 25.4) < 0.05 for w in widths), widths
        # monochrome: every stroke black
        assert all(tuple(o.get("stroking_color") or (0,)) in ((0,), (0, 0, 0), (0.0, 0.0, 0.0)) for o in long)
    # layer table override + relative policy
    res2 = cad.dxf_to_pdf(src, size="ansi-b", lineweights="relative", layer_weights={"HEAVY": 2.0}, out=tmp_path / "rel.pdf", quiet=True)
    assert res2.ok
    with pdfplumber.open(res2.output) as pdf:
        ws = {round(o["linewidth"], 2) for o in pdf.pages[0].lines + pdf.pages[0].curves}
    rel = ((17 ** 2 + 11 ** 2) ** 0.5) / ((34 ** 2 + 22 ** 2) ** 0.5)
    assert any(abs(w - 2.0 * 72 / 25.4 * rel) < 0.05 for w in ws), ws


def test_dxf_layout_and_title_block(tmp_path):
    src = _build_dxf(tmp_path / "plan.dxf")
    res = cad.dxf_to_pdf(src, layout="SHEET-1", quiet=True)
    assert res.ok and res.data["layout"] == "SHEET-1"
    with pdfplumber.open(res.output) as pdf:
        p = pdf.pages[0]
        assert abs(p.width - 17 * 72) < 1 and abs(p.height - 11 * 72) < 1
        assert "PAPERSPACE TITLE" in p.extract_text()
    with pytest.raises(ValueError, match="no layout"):
        cad.dxf_to_pdf(src, layout="NOPE", quiet=True)
    tb = cad.dxf_to_pdf(src, size="ansi-b", scale="1:2", title_block={"sheet": "M-101", "title": "PUMP PLAN", "project": "TEST"},
                        out=tmp_path / "tb.pdf", quiet=True)
    assert tb.ok, tb.line()
    _assert_fonts_ok(tb.output)
    with pdfplumber.open(tb.output) as pdf:
        p = pdf.pages[0]
        txt = p.extract_text()
        assert "M-101" in txt and "PUMP PLAN" in txt and "PUMP STATION P-1" in txt
        # scale survives the title-block placement: 20 in at 1:2 = 10 in
        long = [o for o in p.lines + p.curves if o["bottom"] - o["top"] < 3 and 600 < o["x1"] - o["x0"] < 800]
        assert long and abs((long[0]["x1"] - long[0]["x0"]) - 720) < 2


def _dwg_engine():
    from pdfcore import engines

    try:
        return engines.dwg_converter()
    except RuntimeError:
        return None


def test_dwg_without_converter_and_hpgl(tmp_path, monkeypatch):
    from pdfcore import engines

    dwg = tmp_path / "x.dwg"
    dwg.write_bytes(b"AC1032" + b"\0" * 100)

    def none():
        raise RuntimeError("No DWG converter installed. Options: install the free ODA File Converter")

    monkeypatch.setattr(engines, "dwg_converter", none)
    with pytest.raises(RuntimeError, match="No DWG converter"):
        cad.dxf_to_pdf(dwg, quiet=True)
    plt = tmp_path / "plot.plt"
    plt.write_bytes(b"IN;SP1;PU0,0;PD8000,0,8000,6000,0,6000,0,0;PU2000,2000;PD6000,4000;PU;")
    res = cad.dxf_to_pdf(plt, size="letter", quiet=True)
    assert res.ok
    with pdfplumber.open(res.output) as pdf:
        assert len(pdf.pages[0].lines + pdf.pages[0].curves + pdf.pages[0].rects) >= 1


DWG_SAMPLES = Path("/Users/owensmith/Developer/pdf-studio-local/corpus/dwg")


@pytest.mark.skipif(not (DWG_SAMPLES / "ex2018.dwg").exists() or _dwg_engine() is None,
                    reason="needs the real DWG samples and an installed dwg engine")
def test_dwg_to_pdf_real_samples(tmp_path):
    res = cad.dxf_to_pdf(DWG_SAMPLES / "ex2018.dwg", size="ansi-b", out=tmp_path / "ex2018.pdf", quiet=True)
    assert res.ok, res.line()
    assert res.data["loader"].startswith("dwg:")
    _assert_fonts_ok(res.output)
    with pdfplumber.open(res.output) as pdf:
        p = pdf.pages[0]
        assert len(p.lines + p.curves + p.rects) > 20
        assert p.extract_text().strip()
    lay = cad.dxf_to_pdf(DWG_SAMPLES / "ex2000.dwg", layout="Layout1", out=tmp_path / "ex2000.pdf", quiet=True)
    assert lay.ok and lay.data["layout"] == "Layout1"
    plain = cad.dxf_to_pdf(DWG_SAMPLES / "ex2000.dwg", out=tmp_path / "ex2000-auto.pdf", quiet=True)
    assert plain.ok
    if plain.data["layout_fallback"]:  # empty modelspace → first non-empty layout
        assert plain.data["layout"] != "Model"
    assert main(["plans", "dwg2pdf", str(DWG_SAMPLES / "ex2018.dwg"), "-o", str(tmp_path / "alias.pdf")]) == 0


def test_empty_modelspace_falls_back_to_layout(tmp_path):
    doc = ezdxf.new("R2010")
    lay = doc.layouts.new("PLAN")
    lay.page_setup(size=(431.8, 279.4), units="mm")
    lay.add_line((20, 20), (400, 20))
    lay.add_text("ONLY IN PAPERSPACE", dxfattribs={"height": 6}).set_placement((30, 40))
    doc.saveas(tmp_path / "ps.dxf")
    res = cad.dxf_to_pdf(tmp_path / "ps.dxf", quiet=True)
    assert res.ok and res.data["layout"] == "PLAN" and res.data["layout_fallback"] and res.data["loader"] == "strict"
    with pdfplumber.open(res.output) as pdf:
        assert "ONLY IN PAPERSPACE" in pdf.pages[0].extract_text()


def test_extents_skip_far_outlier(tmp_path):
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for k in range(6):
        msp.add_line((k, 0), (k, 5))
    msp.add_xline((0, 0), (1, 1))
    blk = doc.blocks.new("FAR")
    blk.add_line((0, 0), (1, 1))
    msp.add_blockref("FAR", (0, 0), dxfattribs={"xscale": 5000, "yscale": 5000})
    doc.saveas(tmp_path / "far.dxf")
    auto = cad.dxf_to_pdf(tmp_path / "far.dxf", out=tmp_path / "auto.pdf", quiet=True)
    assert auto.ok and auto.data["dropped_outliers"] and auto.data["scale_ratio"] < 100
    every = cad.dxf_to_pdf(tmp_path / "far.dxf", extents="all", out=tmp_path / "all.pdf", quiet=True)
    assert every.ok and every.data["scale_ratio"] > 10000


def test_scale_and_paper_parsing():
    assert cad.parse_scale('1/4"=1\'-0"').ratio == pytest.approx(48)
    assert cad.parse_scale('1/4"=1\'-0"').units == "ft"
    assert cad.parse_scale("1\"=20'").ratio == pytest.approx(240)
    assert cad.parse_scale("1:50").ratio == 50
    assert cad.parse_scale("fit") is None
    assert cad.paper_size("ARCH D") == (36, 24)
    assert cad.paper_size("ansi-b", "portrait") == (11, 17)
    assert cad.paper_size("841x594mm")[0] == pytest.approx(841 / 25.4)
    assert cad.parse_length_in("1'-6\"") == 18


def test_dxf2pdf_cli(tmp_path):
    src = _build_dxf(tmp_path / "plan.dxf")
    assert main(["plans", "dxf2pdf", str(src), "--size", "arch-c", "--scale", "1:2", "--lineweights", "by-layer",
                 "--grayscale", "-o", str(tmp_path / "cli.pdf")]) == 0
    assert (tmp_path / "cli.pdf").exists()


# ------------------------------------------------------------------ PDF → DXF
def test_pdf_to_dxf_one_line(corpus, tmp_path):
    src = tmp_path / "drawings_rev0.pdf"
    shutil.copy(corpus["drawings_rev0"], src)
    res = cad.pdf_to_dxf(src, sheet="E-101", quiet=True)
    assert res.output.name == "drawings_rev0.E-101.dxf"
    doc = ezdxf.readfile(res.output)
    assert doc.header["$INSUNITS"] == 1
    msp = doc.modelspace()
    # bus: 6 in → 22 in at 18 in above the sheet bottom, 4 pt wide
    bus = [e for e in msp.query("LINE") if abs(e.dxf.start.y - 18) < 0.01 and abs(e.dxf.end.y - 18) < 0.01
           and abs(abs(e.dxf.end.x - e.dxf.start.x) - 16) < 0.01]
    assert len(bus) == 1
    assert doc.layers.get(bus[0].dxf.layer).dxf.lineweight == 140
    boxes = [e for e in msp.query("LWPOLYLINE") if e.closed and len(e) == 4]
    small = []
    for e in boxes:
        xs = [p[0] for p in e.get_points()]
        ys = [p[1] for p in e.get_points()]
        if abs(max(xs) - min(xs) - 0.8) < 0.01 and abs(max(ys) - min(ys) - 0.8) < 0.01:
            small.append(((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2))
    assert sorted(round(x) for x, _y in small) == [8, 12, 16, 20]
    circles = list(msp.query("CIRCLE"))
    assert len(circles) == 1
    c = circles[0]
    assert c.dxf.radius == pytest.approx(0.45, abs=0.005)
    assert (c.dxf.center.x, c.dxf.center.y) == (pytest.approx(14, abs=0.01), pytest.approx(21, abs=0.01))
    texts = {e.dxf.text: e for e in msp.query("TEXT")}
    for lab in ("VFD-1 / P-1 75HP", "VFD-2 / P-2 75HP", "PANEL LP-1 225A", "EF-1", "MDP 2000A 480Y/277V 3PH 4W", "E-101"):
        assert lab in texts, sorted(texts)
    assert texts["VFD-1 / P-1 75HP"].dxf.insert.x == pytest.approx(7.1, abs=0.02)
    assert texts["E-101"].dxf.height > texts["EF-1"].dxf.height  # 30 pt vs 10 pt
    assert not doc.audit().has_errors


def test_pdf_to_dxf_scale_units_and_cli(corpus, tmp_path):
    src = tmp_path / "drawings_rev0.pdf"
    shutil.copy(corpus["drawings_rev0"], src)
    res = cad.pdf_to_dxf(src, sheet="E-301", scale='1/4"=1\'-0"', curves="spline", quiet=True)
    doc = ezdxf.readfile(res.output)
    assert doc.header["$INSUNITS"] == 2 and res.data["units"] == "ft"
    # the 22 in × 14 in building outline at 1/4"=1'-0" is 88 ft × 56 ft
    outl = []
    for e in doc.modelspace().query("LWPOLYLINE"):
        xs = [p[0] for p in e.get_points()]
        ys = [p[1] for p in e.get_points()]
        outl.append((round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)))
    assert (88.0, 56.0) in outl
    assert len(doc.modelspace().query("CIRCLE")) == 5
    assert main(["plans", "pdf2dxf", str(src), "--page", "2", "--layers", "weight", "-o", str(tmp_path / "p2.dxf")]) == 0
    d2 = ezdxf.readfile(tmp_path / "p2.dxf")
    assert any(l.dxf.name.endswith("MM") for l in d2.layers)


# ------------------------------------------------------------------ ladder schematics
def _schematic(tmp_path, data=None, **kw):
    if data is None:
        y = tmp_path / "ladder.yaml"
        shutil.copy(EXAMPLE, y)
        return cad.schematic(y, quiet=True, **kw)
    return cad.schematic(data, out=tmp_path / "s.pdf", quiet=True, **kw)


def test_schematic_example(tmp_path):
    res = _schematic(tmp_path, png_dir=tmp_path / "png")
    assert res.ok, res.line()
    assert res.output.name == "ladder.schematic.pdf"
    _assert_fonts_ok(res.output)
    d = res.data
    # wire numbers: one per node, unique, and both ends of each device on different nodes
    wires = d["wires"]
    assert len(wires) == len(set(wires))
    for dev in d["devices"]:
        a, b = dev["nodes"]
        assert a != "?" and b != "?"
        if dev["kind"] != "terminal":
            assert a != b, dev
        else:
            assert a == b, dev  # terminals pass the wire through
    assert d["devices"][0]["nodes"][0] == "1"  # left rail is wire 1
    assert "1" in wires and "2" in wires
    # seal-in contact M1 is in parallel with START: same nodes
    pb2 = next(x for x in d["devices"] if x["tag"] == "PB2")
    seal = next(x for x in d["devices"] if x["tag"] == "M1" and x["kind"] == "no" and x["line"] == 101)
    assert pb2["nodes"] == seal["nodes"]
    # cross references
    assert d["coils"] == {"M1": 100, "TR1": 103, "CR1": 104}
    assert d["xrefs"]["M1"] == [{"line": 101, "nc": False}, {"line": 105, "nc": False}]
    assert d["xrefs"]["CR1"] == [{"line": 102, "nc": False}]
    with pdfplumber.open(res.output) as pdf:
        page = pdf.pages[0]
        words = page.extract_words()
        texts = [w["text"] for w in words]
        for n in wires:
            assert n in texts, n
        # coil M1's contact list "101 105" sits right of the rails on line 100
        m1_line = [w for w in words if w["text"] == "100"][0]
        row = {w["text"] for w in words if abs(w["top"] - m1_line["top"]) < 3}
        assert {"101", "105", "M1"} <= row | set(texts)
        # contact of CR1 (line 102) shows its coil line 104 beneath
        assert "104" in texts and "103" in texts
        # text at least 7 pt in the drawing (title block column excluded)
        tb_x = page.width - (0.5 + 4 * 0.55) * 72
        small = [c for c in page.chars if c["x1"] < tb_x - 2 and c["text"].strip() and c["size"] < 6.95]
        assert not small, {c["text"] for c in small}
        assert "BILL OF MATERIALS" in page.extract_text() and "TERMINAL LIST" in page.extract_text()
    # BOM rows match devices: one row per tag (contacts fold into their coil) + terminal strip + supply fuse
    tags = {x["tag"] for x in d["devices"] if x["kind"] != "terminal"}
    bom_tags = [r["tag"] for r in d["bom"]]
    assert set(bom_tags) == tags | {"TB-1", "FU1"}
    assert len(bom_tags) == len(set(bom_tags))
    tb1 = next(r for r in d["bom"] if r["tag"] == "TB-1")
    assert tb1["qty"] == 2
    assert next(r for r in d["bom"] if r["tag"] == "M1")["part"] == "AN16SN0AB"
    # terminals carry wire numbers
    terms = {t["number"]: t for t in d["terminals"]}
    assert terms["1"]["wire"] == "1" and "LSH1:C" in terms["1"]["field"]
    assert terms["2"]["wire"] == d["devices"][[x["tag"] for x in d["devices"]].index("TR1")]["nodes"][0]
    # XLSX
    import openpyxl

    xp = res.extra_outputs[0]
    wb = openpyxl.load_workbook(xp)
    assert wb.sheetnames == ["BOM", "Wire List", "Terminals", "Cross-Reference"]
    assert wb["BOM"].max_row - 1 == len(d["bom"])
    assert wb["Wire List"].max_row - 1 == len(wires)
    assert wb["Terminals"].max_row - 1 == 2
    assert list(res.gate.pngs)


def test_schematic_iec_line_numbering_multi_sheet(tmp_path):
    rungs = []
    for k in range(1, 31):
        rungs.append({"desc": f"Output {k}", "devices": [{"tag": "S0", "type": "pb_nc", "label": "STOP"},
                                                         {"tag": f"K{k}", "type": "no"} if k > 1 else {"tag": "S1", "type": "pb_no"},
                                                         {"tag": f"K{k}" if k == 1 else f"Q{k}", "type": "coil"}]})
    data = {"title": "IEC TEST", "sheet": "E-701", "standard": "iec", "rails": ["+24V", "0V"],
            "wires": {"scheme": "line"}, "size": "ansi-b", "rungs": rungs,
            "devices": {"S0": {"desc": "E-STOP"}}}
    # K2..K30 contacts have no coil (only K1, Q2..Q30 coils) → orphan contacts allowed, listed
    res = _schematic(tmp_path, data, terminal_view="strip", xlsx=False)
    assert res.ok, res.line()
    assert res.data["sheets"] >= 2
    labels = list(res.data["wires"])
    assert len(labels) == len(set(labels))
    assert any(l.startswith("101") for l in labels)  # line-based numbers
    assert "K2" in res.data["orphan_contacts"]
    with pikepdf.open(res.output) as pdf:
        assert len(pdf.pages) == res.data["sheets"]
    with pdfplumber.open(res.output) as pdf:
        assert "E-702" in pdf.pages[1].extract_text()


def test_schematic_errors(tmp_path):
    with pytest.raises(ValueError, match="appears twice"):
        _schematic(tmp_path, {"rungs": [{"devices": [{"tag": "CR1", "type": "coil"}]}, {"devices": [{"tag": "CR1", "type": "coil"}]}]})
    with pytest.raises(ValueError, match="unknown device type"):
        _schematic(tmp_path, {"rungs": [{"devices": [{"tag": "X1", "type": "flux-capacitor"}]}]})
    with pytest.raises(ValueError, match="wide"):
        _schematic(tmp_path, {"size": "ansi-a", "rungs": [{"devices": [{"tag": f"C{k}", "type": "no"} for k in range(14)] + [{"tag": "Z", "type": "coil"}]}]})


def test_schematic_cli(tmp_path):
    y = tmp_path / "ladder.yaml"
    shutil.copy(EXAMPLE, y)
    assert main(["plans", "schematic", str(y), "--terminal-view", "strip", "-o", str(tmp_path / "out.pdf")]) == 0
    assert (tmp_path / "out.pdf").exists() and (tmp_path / "out.lists.xlsx").exists()
