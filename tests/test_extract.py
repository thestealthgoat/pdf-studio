from __future__ import annotations

import csv
import hashlib
import json
import xml.etree.ElementTree as ET

import openpyxl
import pytest
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from pdfcore.cli import main
from pdfcore.ops import extract

HEADER = ["Item", "Qty", "Unit cost ($)", "Total ($)"]


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------ render


def test_render_png_pages_and_dpi(work):
    src = work("report_tables")
    before = _sha(src)
    res = extract.render(src, pages="1-2", dpi=50, quiet=True)
    assert res.gate is None and res.ok
    assert len(res.extra_outputs) == 2 and res.output == res.extra_outputs[0]
    assert res.output.parent.name == "report_tables_pages"
    for p in res.extra_outputs:
        with Image.open(p) as im:
            assert abs(im.width - 612 * 50 / 72) <= 1 and abs(im.height - 792 * 50 / 72) <= 1
            assert round(im.info["dpi"][0]) == 50
    assert _sha(src) == before
    # a second run does not clobber the first folder
    res2 = extract.render(src, pages="last", dpi=30, fmt="jpeg", quiet=True)
    assert res2.output.parent.name == "report_tables_pages-2" and res2.output.suffix == ".jpg"
    assert res2.output.name.endswith("_p003.jpg")


def test_render_cli_prints_pass(work, capsys):
    src = work("letter")
    assert main(["render", str(src), "--dpi", "40", "--pages", "1"]) == 0
    assert capsys.readouterr().out.strip().endswith("PASS")


# ------------------------------------------------------------------ text


def test_text_levels(work):
    src = work("report_tables")
    for level in ("chars", "words", "lines", "blocks"):
        res = extract.text(src, level=level, pages="1", quiet=True)
        doc = json.loads(res.output.read_text())
        assert doc["level"] == level and doc["pages"][0]["page"] == 1
        items = doc["pages"][0]["items"]
        assert items and all(len(it["bbox"]) == 4 for it in items)
        joined = " ".join(it["text"] for it in items) if level != "chars" else "".join(it["text"] for it in items)
        assert "Quarterly" in joined
    words = json.loads(extract.text(src, level="words", pages="1", quiet=True).output.read_text())
    assert {"line", "block", "font", "size"} <= set(words["pages"][0]["items"][0])


def test_plain_text_reading_order_two_columns(tmp_path):
    src = tmp_path / "cols.pdf"
    c = canvas.Canvas(str(src), pagesize=letter)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, 720, "Two Column Title")
    c.setFont("Helvetica", 10)
    for k in range(12):
        c.drawString(72, 680 - 14 * k, f"left column line {k}")
        c.drawString(320, 680 - 14 * k, f"right column line {k}")
    c.save()
    res = extract.text(src, level="plain", quiet=True)
    t = res.output.read_text()
    assert t.index("Two Column Title") < t.index("left column line 0")
    assert t.index("left column line 11") < t.index("right column line 0")
    lay = extract.text(src, level="plain", layout=True, quiet=True).output.read_text()
    assert "left column line 0" in lay and lay != t


# ------------------------------------------------------------------ search


def test_search_regex_case_whole_word(work, capsys):
    src = work("report_tables")
    labor_rows = [t for t in extract.find_tables(src) if t.rows[1][0].startswith("Labor")][0]
    res = extract.search(src, r"Labor line \d+", regex=True)
    assert res.data["count"] == len(labor_rows.rows) - 1
    hit = res.data["hits"][0]
    assert hit["page"] >= 1 and len(hit["rects"]) == 1 and len(hit["rects"][0]) == 4
    x0, top, x1, bottom = hit["rects"][0]
    assert 0 <= x0 < x1 <= 612 and 0 <= top < bottom <= 792
    assert abs(hit["rects_pdf"][0][1] - (792 - bottom)) < 0.05
    assert extract.search(src, "LABOR", case=True).data["count"] == 0
    assert extract.search(src, "LABOR").data["count"] > 0
    assert extract.search(src, "Lab", whole_word=True).data["count"] == 0
    assert extract.search(src, "Lab").data["count"] > 0
    assert main(["search", str(src), "materials line", "--whole-word"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] >= 5 and out["hits"][0]["rects"]
    res = extract.search(src, "Outlook", out=src.parent, quiet=True)
    assert json.loads(res.output.read_text())["count"] == res.data["count"]


# ------------------------------------------------------------------ vectors


def test_vectors_one_line_diagram(work):
    src = work("drawings_rev0")
    res = extract.vectors(src, pages="2", quiet=True)
    jpath, svg = res.extra_outputs
    doc = json.loads(jpath.read_text())
    page = doc["pages"][0]
    assert page["page"] == 2 and page["width"] == 2592
    items = page["items"]
    bus = [i for i in items if i["kind"] == "line" and i["width"] == 4]
    assert len(bus) == 1 and abs((bus[0]["bbox"][2] - bus[0]["bbox"][0]) - 16 * 72) < 1
    assert bus[0]["stroke"] == "#000000" and bus[0]["path"][0][0] == "M"
    boxes = [i for i in items if i["kind"] == "rect" and abs(i["bbox"][2] - i["bbox"][0] - 57.6) < 0.5]
    assert len(boxes) == 4 and all(b["width"] == 1.8 for b in boxes)
    circles = [i for i in items if i["kind"] == "curve"]
    assert circles and any(c[0] == "C" for c in circles[0]["path"])
    # SVG: well-formed, page-sized, one <path> per painted item, keeps widths
    root = ET.parse(svg).getroot()
    assert root.get("viewBox") == "0 0 2592 1728"
    ns = "{http://www.w3.org/2000/svg}"
    paths = root.findall(f".//{ns}path")
    assert len(paths) == sum(1 for i in items if i["stroke"] or i["fill"])
    assert any(p.get("stroke-width") == "4" for p in paths)
    assert any(p.get("class") == "curve" and " C " in p.get("d") for p in paths)


def test_vectors_svg_with_text(work):
    src = work("drawings_rev0")
    res = extract.vectors(src, pages="2", fmt="svg", include_text=True, quiet=True)
    root = ET.parse(res.output).getroot()
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert "MDP" in texts


# ------------------------------------------------------------------ images


def test_extract_images_with_placements(work):
    src = work("image_heavy")
    res = extract.images(src, quiet=True)
    man = json.loads(res.output.read_text())
    assert res.output.name == "images.json" and len(man["images"]) == 4
    for k, img in enumerate(man["images"]):
        assert img["width_px"] == 2250 and img["height_px"] == 1650
        pl = img["placements"][0]
        assert pl["page"] == k + 1 and pl["size_pt"] == [540.0, 396.0]
        assert abs(pl["dpi"][0] - 300) < 1
        with Image.open(res.output.parent / img["file"]) as im:
            assert im.size == (2250, 1650)
    png = extract.images(src, pages="1", fmt="png", quiet=True)
    assert png.extra_outputs[1].suffix == ".png"


# ------------------------------------------------------------------ tables


def test_tables_report_sections(work):
    src = work("report_tables")
    found = extract.find_tables(src)
    assert len(found) == 5
    sections = []
    for t in found:
        assert t.header == HEADER
        sections.append(t.rows[1][0].split(" line")[0])
        for row in t.rows[1:]:
            q, u, tot = (extract.to_number(v) for v in row[1:])
            assert isinstance(q, int) and isinstance(u, float) and isinstance(tot, float)
            assert abs(q * u - tot) <= 0.01 * max(1, tot) + 0.01
    assert sections == ["Summary", "Labor", "Materials", "Equipment", "Outlook"]


def test_tables_xlsx_numeric_and_bold(work):
    src = work("report_tables")
    res = extract.tables(src, quiet=True)
    assert res.output.suffix == ".xlsx" and res.data["count"] == 5
    wb = openpyxl.load_workbook(res.output)
    assert len(wb.sheetnames) == 5
    ws = wb[wb.sheetnames[0]]
    assert [c.value for c in ws[1]] == HEADER
    assert all(c.font.bold for c in ws[1])
    for row in ws.iter_rows(min_row=2, values_only=True):
        name, q, u, tot = row
        assert isinstance(name, str) and isinstance(q, int) and isinstance(u, float) and isinstance(tot, float)
        assert abs(q * u - tot) < 0.01 * tot + 0.01


def test_tables_panel_schedule_csv(work):
    src = work("drawings_rev0")
    res = extract.tables(src, pages="3", fmt="csv", quiet=True)
    assert len(res.extra_outputs) == 1
    rows = list(csv.reader(res.output.open(encoding="utf-8-sig")))
    assert rows[0] == ["CKT", "DESCRIPTION", "BKR", "VA"]
    assert len(rows) == 25
    for k, r in enumerate(rows[1:], start=1):
        desc = ["LIGHTING", "RECEPTACLES", "CP-1 CONTROL POWER", "HEATER", "SPARE"][k % 5]
        assert r == [str(k), desc, "20/1", str(0 if desc == "SPARE" else 400 + 60 * k)]
    assert extract.to_number("20/1") is None  # breaker size stays text


def test_tables_ruled_grid_uses_lines(tmp_path):
    src = tmp_path / "grid.pdf"
    data = [["Tag", "Description", "Amps"], ["P-1", "Pump one", "96"], ["P-2", "Pump two\nwith VFD", "96"], ["EF-1", "Fan", "3.4"]]
    t = Table(data, colWidths=[80, 160, 60])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.8, colors.black)]))
    SimpleDocTemplate(str(src), pagesize=letter).build([t])
    found = extract.find_tables(src)
    assert len(found) == 1 and found[0].strategy == "lines"
    assert found[0].rows[0] == ["Tag", "Description", "Amps"]
    assert found[0].rows[2] == ["P-2", "Pump two with VFD", "96"]


def test_tables_no_false_positives(work):
    assert extract.find_tables(work("letter")) == []
    assert extract.find_tables(work("contract_v1")) == []


@pytest.mark.parametrize("s,v", [("1,234.50", 1234.5), ("$12", 12), ("(3.00)", -3.0), ("-7", -7), ("12%", 0.12),
                                 ("0.5", 0.5), ("007", None), ("E-101", None), ("20/1", None), ("", None), ("-", None)])
def test_to_number(s, v):
    assert extract.to_number(s) == v


def test_tables_camelot_guarded(work):
    try:
        import camelot  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="pdf-studio\\[tables\\]"):
            extract.find_tables(work("report_tables"), strategy="camelot")
    else:
        pytest.skip("camelot installed")
