"""The ``pdfcore convert`` front door: every required (source → target) pair on generated inputs.

Tests whose external program (LibreOffice, sips, a DWG engine) is missing are skipped.
"""

from __future__ import annotations

import io
import json
import shutil
import xml.etree.ElementTree as ET
from importlib.metadata import distribution
from pathlib import Path

import numpy as np
import openpyxl
import pdfplumber
import pikepdf
import pytest
from PIL import Image, ImageChops, ImageDraw

from pdfcore.cli import main
from pdfcore.ops import convert, export

soffice = pytest.mark.skipif(convert.find_soffice() is None, reason="LibreOffice (soffice) not installed")
sips = pytest.mark.skipif(convert.find_sips() is None, reason="macOS sips not available")


def _dwg_reader() -> bool:
    from pdfcore import engines

    try:
        engines.dwg_converter()
        return True
    except RuntimeError:
        return False


def _dwg_writer() -> bool:
    try:
        convert.dwg_writer()
        return True
    except RuntimeError:
        return False


SVG_NS = "{http://www.w3.org/2000/svg}"


def _text(pdf) -> str:
    with pdfplumber.open(str(pdf)) as d:
        return "\n".join(p.extract_text() or "" for p in d.pages)


def _npages(pdf) -> int:
    with pikepdf.open(pdf) as d:
        return len(d.pages)


def _picture(size=(320, 200), mode="RGB") -> Image.Image:
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    d.rectangle([size[0] // 5, size[1] // 5, size[0] * 4 // 5, size[1] * 3 // 5], fill=(30, 90, 160))
    d.ellipse([10, 10, 60, 60], fill=(200, 60, 40))
    return im.convert(mode) if mode != "RGB" else im


@pytest.fixture
def report(tmp_path, corpus) -> Path:
    p = tmp_path / "report.pdf"
    shutil.copy(corpus["report_tables"], p)
    return p


@pytest.fixture
def letter(tmp_path, corpus) -> Path:
    p = tmp_path / "letter.pdf"
    shutil.copy(corpus["letter"], p)
    return p


# ====================================================================== images → PDF


@pytest.mark.parametrize("name,fmt,kw", [
    ("a.png", "PNG", {}),
    ("b.JPG", "JPEG", {"quality": 92}),
    ("c.JPEG", "JPEG", {}),
    ("d.jpe", "JPEG", {}),
    ("e.gif", "GIF", {}),
    ("f.bmp", "BMP", {}),
    ("g.webp", "WEBP", {"quality": 90}),
    ("h.avif", "AVIF", {}),
    ("i.jp2", "JPEG2000", {}),
    ("j.TIF", "TIFF", {}),
])
def test_image_to_pdf(tmp_path, name, fmt, kw):
    src = tmp_path / name
    _picture().save(src, fmt, **kw)
    res = convert.convert(src, "pdf", quiet=True)
    assert res.ok, res.line()
    assert res.output == src.with_suffix(".pdf") and _npages(res.output) == 1


def test_multipage_tiff_to_pdf(tmp_path):
    src = tmp_path / "scan.tiff"
    pages = [_picture(), _picture((200, 320)), _picture((300, 300), "L")]
    pages[0].save(src, "TIFF", save_all=True, append_images=pages[1:], compression="tiff_deflate")
    res = convert.convert(src, "pdf", quiet=True)
    assert res.ok and _npages(res.output) == 3


@sips
def test_heic_to_pdf_and_jpg(tmp_path):
    png = tmp_path / "photo.png"
    _picture((400, 300)).save(png)
    heic = tmp_path / "photo.HEIC"
    convert.sips_convert(png, "heic", heic)
    res = convert.convert(heic, "pdf", quiet=True)
    assert res.ok and _npages(res.output) == 1
    jpg = convert.convert(heic, "jpeg", quiet=True).output
    assert jpg.suffix == ".jpg"
    with Image.open(jpg) as im:
        assert im.size == (400, 300) and im.format == "JPEG"
    back = convert.convert(png, "heic", out=tmp_path / "again.heic", quiet=True).output
    assert b"ftyp" in back.read_bytes()[:32]


def test_heic_without_sips_is_a_clear_error(tmp_path, monkeypatch):
    f = tmp_path / "x.heic"
    f.write_bytes(b"\x00\x00\x00\x18ftypheic")
    monkeypatch.setattr(convert, "find_sips", lambda: None)
    with pytest.raises(RuntimeError, match=r"sips.*LGPL"):
        convert.convert(f, "pdf", quiet=True)


# ====================================================================== SVG / CAD / Markdown / text / HTML → PDF


SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200" viewBox="0 0 400 200">
<rect x="10" y="10" width="380" height="180" fill="#eef3f8" stroke="#1d4a6e" stroke-width="4"/>
<path d="M30 160 L120 60 L210 160 Z" fill="#b4532a"/>
<circle cx="300" cy="100" r="50" fill="none" stroke="#000" stroke-width="3"/>
<text x="230" y="40" font-family="Inter" font-size="20">Vector label</text>
</svg>"""


def test_svg_to_pdf_is_vector(tmp_path):
    src = tmp_path / "Logo.SVG"
    src.write_text(SVG)
    res = convert.convert(src, "pdf", quiet=True)
    assert res.ok, res.line()
    with pdfplumber.open(str(res.output)) as d:
        p = d.pages[0]
        assert (round(p.width), round(p.height)) == (300, 150)  # 400 × 200 px at 96 px/in = 300 × 150 pt
        assert p.curves or p.rects or p.lines  # vectors, not a raster
        assert not p.images
    png = convert.convert(src, "png", quiet=True).output  # SVG → PNG goes through the PDF
    with Image.open(png) as im:
        assert im.getpixel((im.width // 2, im.height // 2)) != (255, 255, 255)


def _dxf(path: Path) -> Path:
    import ezdxf

    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 1
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (20, 0), (20, 10), (0, 10)], close=True)
    msp.add_line((0, 0), (20, 10))
    msp.add_circle((10, 5), 3)
    msp.add_text("PANEL CP-1", height=0.8).set_placement((1, 8))
    doc.saveas(path)
    return path


def test_dxf_and_hpgl_to_pdf(tmp_path):
    res = convert.convert(_dxf(tmp_path / "plan.DXF"), "pdf", quiet=True)
    assert res.ok and "PANEL CP-1" in _text(res.output)
    plt = tmp_path / "plot.plt"
    plt.write_bytes(b"IN;SP1;PU0,0;PD4000,0,4000,3000,0,3000,0,0;PU500,500;PD3500,2500;")
    res2 = convert.convert(plt, "pdf", quiet=True)
    assert res2.output.suffix == ".pdf" and _npages(res2.output) == 1
    dxf = convert.convert(plt, "dxf", quiet=True).output
    import ezdxf

    assert len(ezdxf.readfile(str(dxf)).modelspace()) > 0


@pytest.mark.skipif(not _dwg_reader(), reason="no DWG engine installed (ODA File Converter)")
def test_dwg_to_pdf_and_dxf(tmp_path):
    if not _dwg_writer():
        pytest.skip("need a DWG writer to make a test DWG")
    dwg = convert.convert(_dxf(tmp_path / "plan.dxf"), "dwg", out=tmp_path / "plan.dwg", quiet=True).output
    assert convert.convert(dwg, "pdf", out=tmp_path / "from_dwg.pdf", quiet=True).ok
    back = convert.convert(dwg, "dxf", out=tmp_path / "back.dxf", quiet=True).output
    import ezdxf

    assert ezdxf.readfile(str(back)).modelspace().query("TEXT")


MD = """---
author: Morgan Lee
date: 2026-11-13
---
# Pump Station Notes

Lifts **raw water** to the plant — about $64,367 (see <https://example.com/r>). C# uses `#include <x>` // not a comment, @team.

## Findings

- Starters are obsolete; snake_case_name stays.
  - nested item
1. Replace CP-1.
2. Add VFDs.

> The MDP has no surge protective device.

### Budget

| Item | Qty | Cost |
|:--|--:|--:|
| Panel CP-1 | 1 | 19,525.00 |
| Drives \\| field | 2 | 35,432.00 |

```python
print("[brackets] {braces} $x")
```

![Layout](chart.png)
"""


def test_markdown_to_pdf_house_style(tmp_path):
    _picture((600, 300)).save(tmp_path / "chart.png", dpi=(150, 150))
    src = tmp_path / "notes.md"
    src.write_text(MD)
    res = convert.convert(src, "pdf", quiet=True)
    assert res.ok, res.line()
    t = _text(res.output)
    for s in ("Pump Station Notes", "Morgan Lee", "Findings", "$64,367", "C#", "#include <x>", "// not a comment",
              "@team", "snake_case_name", "Panel CP-1", "Drives | field", "[brackets] {braces} $x"):
        assert s in t, s
    with pdfplumber.open(str(res.output)) as d:
        assert sum(len(p.images) for p in d.pages) == 1
    with pikepdf.open(res.output) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Pump Station Notes"


def test_txt_to_pdf_typeset(tmp_path):
    src = tmp_path / "notes.TXT"
    src.write_text("Site walk notes\n\nPanel LP-1 relocated to the east wall; feeders re-pulled in 3/4\" EMT "
                   "with #10 THHN, see sheet E-201 for the revised one-line and panel schedule.\n"
                   "Second line of the same paragraph keeps flowing into the first one here.\n\n"
                   "Contacts:\n  Morgan Lee   (555) 010-0100\n  Jordan Blake (555) 734-2201\n")
    res = convert.convert(src, "pdf", quiet=True)
    assert res.ok, res.line()
    t = _text(res.output)
    assert "Site walk notes" in t and "#10 THHN" in t and "Jordan Blake" in t
    with pdfplumber.open(str(res.output)) as d:
        fonts = {c["fontname"].split("+")[-1] for p in d.pages for c in p.chars}
    assert not any("Mono" in f or "Courier" in f for f in fonts), fonts


def test_html_to_pdf_weasyprint_or_libreoffice(tmp_path):
    h = tmp_path / "page.HTM"
    h.write_text("<html><body><h1>Hello HTML</h1><p>Paragraph text.</p></body></html>")
    try:
        import weasyprint  # noqa: F401
        have = True
    except ImportError:
        have = convert.find_soffice() is not None
    if not have:
        with pytest.raises(RuntimeError, match="WeasyPrint"):
            convert.convert(h, "pdf", quiet=True)
        return
    res = convert.convert(h, "pdf", quiet=True)
    assert res.ok and "Hello HTML" in _text(res.output)


@pytest.mark.external
@soffice
def test_office_to_pdf(tmp_path):
    import docx

    d = docx.Document()
    d.add_heading("Scope of Work", 1)
    d.add_paragraph("Furnish and install panel CP-1.")
    d.save(tmp_path / "scope.DOCX")
    wb = openpyxl.Workbook()
    wb.active.append(["Item", "Qty"])
    wb.active.append(["Anchor bolts", 48])
    wb.active.column_dimensions["A"].width = 30
    wb.save(tmp_path / "budget.xlsx")
    (tmp_path / "list.csv").write_text("Tag,Desc\nP-1,Raw water pump\n")
    (tmp_path / "memo.rtf").write_text(r"{\rtf1\ansi {\b Memo} to the crew.\par}")
    for name, needle in (("scope.DOCX", "Furnish and install"), ("budget.xlsx", "Anchor bolts"),
                         ("list.csv", "Raw water pump"), ("memo.rtf", "to the crew")):
        res = convert.convert(tmp_path / name, "pdf", quiet=True)
        assert res.ok and needle in _text(res.output), name


# ====================================================================== PDF → images


@pytest.mark.parametrize("fmt,ext,pil", [
    ("png", ".png", "PNG"), ("jpg", ".jpg", "JPEG"), ("jpeg", ".jpg", "JPEG"), ("tiff", ".tif", "TIFF"),
    ("webp", ".webp", "WEBP"), ("gif", ".gif", "GIF"), ("bmp", ".bmp", "BMP"), ("avif", ".avif", "AVIF"),
    ("jp2", ".jp2", "JPEG2000"),
])
def test_pdf_to_images(report, fmt, ext, pil):
    res = convert.convert(report, fmt, dpi=40, quiet=True)
    files = [res.output, *res.extra_outputs]
    assert len(files) == _npages(report)
    assert files[0].parent.name == "report_pages" and files[0].name == f"report_p001{ext}"
    for f in files:
        with Image.open(f) as im:
            assert im.format == pil
    one = convert.convert(report, fmt, pages="2", dpi=40, quiet=True).output
    assert one == report.with_suffix(ext)  # a single page lands next to the input


def test_pdf_to_multipage_tiff(report):
    res = convert.convert(report, "tiff", dpi=40, multipage=True, quiet=True)
    assert res.output.name == "report.tif"
    with Image.open(res.output) as im:
        assert im.n_frames == _npages(report)
        assert round(im.info["dpi"][0]) == 40


# ====================================================================== PDF → SVG


def test_pdf_to_svg_text_paths_images(tmp_path, report, corpus):
    res = convert.convert(report, "svg", pages="1", quiet=True)
    root = ET.parse(res.output).getroot()
    assert root.tag == f"{SVG_NS}svg" and root.get("viewBox")
    paths = root.findall(f".//{SVG_NS}path")
    texts = root.findall(f".//{SVG_NS}text")
    assert len(paths) >= 5 and len(texts) >= 20
    joined = " ".join(t.text or "" for t in texts)
    assert "Quarterly Operations Report" in joined
    t0 = next(t for t in texts if "Quarterly" in (t.text or ""))
    assert t0.get("font-size") and t0.get("font-family") and t0.get("transform") and t0.get("x")
    # images become data URIs
    img_pdf = tmp_path / "photo.pdf"
    _picture((300, 200)).save(tmp_path / "photo.png", dpi=(100, 100))
    convert.images_to_pdf([tmp_path / "photo.png"], out=img_pdf, quiet=True)
    sv = convert.convert(img_pdf, "svg", out=tmp_path / "photo.svg", quiet=True).output
    imgs = ET.parse(sv).getroot().findall(f".//{SVG_NS}image")
    assert imgs and imgs[0].get("href", "").startswith("data:image/")


def test_pdf_to_svg_round_trip_looks_the_same(tmp_path, report):
    """PDF → SVG → PDF (Typst) renders like the original page."""
    svg = convert.convert(report, "svg", pages="1", quiet=True).output
    back = convert.convert(svg, "pdf", out=tmp_path / "back.pdf", quiet=True)
    assert back.ok
    a = export.pdf_to_raster(report, "png", pages="1", dpi=50, out=tmp_path / "a.png", quiet=True).output
    b = export.pdf_to_raster(back.output, "png", dpi=50, out=tmp_path / "b.png", quiet=True).output
    ia, ib = Image.open(a).convert("L"), Image.open(b).convert("L")
    assert ia.size == ib.size
    diff = np.asarray(ImageChops.difference(ia, ib))
    assert (diff > 64).mean() < 0.03, (diff > 64).mean()


# ====================================================================== PDF → CAD


def test_pdf_to_dxf(corpus, tmp_path):
    import ezdxf

    src = tmp_path / "set.pdf"
    shutil.copy(corpus["drawings_rev0"], src)
    res = convert.convert(src, "dxf", pages="1", quiet=True)
    doc = ezdxf.readfile(str(res.output))
    assert len(doc.modelspace()) > 10


def test_pdf_and_dxf_to_dwg(tmp_path, letter):
    dxf = _dxf(tmp_path / "plan.dxf")
    if not _dwg_writer():
        with pytest.raises(RuntimeError, match="ODA File Converter"):
            convert.convert(dxf, "dwg", quiet=True)
        with pytest.raises(RuntimeError, match="ODA File Converter"):
            convert.convert(letter, "dwg", quiet=True)
        assert not list(tmp_path.glob("*.dwg"))
        return
    out = convert.convert(dxf, "dwg", quiet=True).output
    assert out.read_bytes()[:3] == b"AC1"
    out2 = convert.convert(letter, "dwg", quiet=True).output
    assert out2.read_bytes()[:3] == b"AC1"


# ====================================================================== PDF → documents and data


def test_pdf_to_markdown(report):
    res = convert.convert(report, "md", quiet=True)
    md = res.output.read_text()
    assert res.output.name == "report.md"
    assert md.startswith("# Quarterly Operations Report")
    assert "## 1 Summary" in md
    assert "| Item | Qty | Unit cost ($) | Total ($) |" in md and "| --- | ---: | ---: | ---: |" in md
    assert "| Summary line 1 | 325 | 748.78 | 243,354.77 |" in md


def test_pdf_to_html(report):
    res = convert.convert(report, "html", quiet=True)
    root = ET.fromstring(res.output.read_bytes())
    body = root.find("body")
    assert root.find("head/title").text == "Quarterly Operations Report"
    assert body.find(".//h1") is not None and body.find(".//h2") is not None
    tables = body.findall(".//table")
    assert len(tables) >= 3 and tables[0].find("thead/tr/th").text == "Item"


def test_pdf_to_docx(report):
    import docx

    lic = distribution("python-docx").metadata
    assert lic.get("License") == "MIT" or "MIT License" in " ".join(lic.get_all("Classifier") or [])
    res = convert.convert(report, "word", quiet=True)
    d = docx.Document(str(res.output))
    heads = [p.text for p in d.paragraphs if p.style.name.startswith("Heading")]
    assert "Quarterly Operations Report" in heads and "1 Summary" in heads
    assert len(d.tables) >= 3 and d.tables[0].cell(1, 0).text == "Summary line 1"
    assert d.core_properties.title == "Quarterly Operations Report"


def test_pdf_lists_and_images_to_docx_and_md(tmp_path):
    """A generated PDF with a bullet list and a picture keeps both."""
    _picture((400, 250)).save(tmp_path / "chart.png", dpi=(150, 150))
    (tmp_path / "doc.md").write_text("# Site Visit\n\nIntro paragraph for the visit.\n\n- First finding\n- Second finding\n\n"
                                     "![Chart](chart.png)\n\nClosing paragraph.\n")
    pdf = convert.convert(tmp_path / "doc.md", "pdf", quiet=True).output
    md = convert.convert(pdf, "md", quiet=True)
    text = md.output.read_text()
    assert "- First finding" in text and "- Second finding" in text
    assert md.extra_outputs and md.extra_outputs[0].suffix in (".png", ".jpg") and "](doc.converted_files/" in text
    import docx

    d = docx.Document(str(convert.convert(pdf, "docx", quiet=True).output))
    assert [p.text for p in d.paragraphs if p.style.name == "List Bullet"] == ["First finding", "Second finding"]
    assert len(d.inline_shapes) == 1


def test_pdf_to_xlsx_csv_txt_json(report):
    x = convert.convert(report, "excel", quiet=True).output
    wb = openpyxl.load_workbook(x)
    assert len(wb.sheetnames) >= 3 and wb.worksheets[0]["A2"].value == "Summary line 1"
    c = convert.convert(report, "csv", quiet=True)
    csvs = [c.output, *c.extra_outputs]
    assert len(csvs) >= 3 and csvs[0].read_text(encoding="utf-8-sig").startswith("Item,Qty")
    t = convert.convert(report, "txt", quiet=True).output
    assert t.name == "report.txt" and "Quarterly Operations Report" in t.read_text()
    j = json.loads(convert.convert(report, "json", quiet=True).output.read_text())
    w = j["pages"][0]["items"][0]
    assert {"text", "bbox", "font", "size"} <= set(w)


# ====================================================================== image ↔ image


def test_image_to_image(tmp_path):
    png = tmp_path / "logo.png"
    rgba = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    ImageDraw.Draw(rgba).rectangle([20, 20, 100, 60], fill=(29, 74, 110, 255))
    rgba.save(png, dpi=(200, 200))
    webp = convert.convert(png, "webp", quiet=True).output
    with Image.open(webp) as im:
        assert im.format == "WEBP" and im.mode == "RGBA"
    jpg = convert.convert(png, "JPEG", quiet=True).output
    with Image.open(jpg) as im:
        assert im.format == "JPEG" and im.getpixel((2, 2)) == (255, 255, 255)  # alpha flattened on white
        assert round(im.info["dpi"][0]) == 200
    tif = tmp_path / "scan.tif"
    _picture().save(tif, save_all=True, append_images=[_picture((100, 100))])
    res = convert.convert(tif, "png", quiet=True)
    assert len([res.output, *res.extra_outputs]) == 2 and res.output.parent.name == "scan_frames"
    gif = convert.convert(tif, "gif", quiet=True).output
    with Image.open(gif) as im:
        assert im.n_frames == 2
    with pytest.raises(ValueError, match="already"):
        convert.convert(jpg, "jpeg", quiet=True)


# ====================================================================== chains, batch, merge, CLI


@pytest.mark.external
@soffice
def test_docx_to_png_through_pdf(tmp_path):
    import docx

    d = docx.Document()
    d.add_paragraph("One page memo.")
    d.save(tmp_path / "memo.docx")
    res = convert.convert(tmp_path / "memo.docx", "png", dpi=30, quiet=True)
    assert res.output == tmp_path / "memo.png" and res.data["via"] == "pdf"


def test_markdown_to_docx_through_pdf(tmp_path):
    (tmp_path / "a.md").write_text("# Title\n\n## Part one\n\nSome text here.\n")
    res = convert.convert(tmp_path / "a.md", "docx", quiet=True)
    assert res.output == tmp_path / "a.docx" and not (tmp_path / "a.pdf").exists()


def test_batch_folder_glob_and_merge(tmp_path, letter, capsys):
    d = tmp_path / "in"
    d.mkdir()
    for k, name in enumerate(["b.png", "a.JPG", "c.md"]):
        if name.endswith(".md"):
            (d / name).write_text("# Third\n\nMarkdown page.\n")
        else:
            _picture((200 + k * 10, 150)).save(d / name)
    (d / "skip.xyz").write_text("?")
    assert main(["convert", str(d)]) == 0
    assert sorted(p.name for p in d.glob("*.pdf")) == ["a.pdf", "b.pdf", "c.pdf"]
    out = tmp_path / "pngs"
    out.mkdir()
    assert main(["convert", str(d / "*.pdf"), "--to", "png", "--dpi", "20", "-o", str(out)]) == 0
    assert len(list(out.glob("*.png"))) == 3
    merged = tmp_path / "packet.pdf"
    assert main(["convert", str(d / "a.JPG"), str(letter), str(d / "c.md"), "--to", "pdf", "--merge",
                 "-o", str(merged)]) == 0
    assert _npages(merged) == 1 + _npages(letter) + 1
    with pdfplumber.open(str(merged)) as m:
        assert "Third" in (m.pages[-1].extract_text() or "")
    assert main(["convert", str(d / "a.JPG"), str(d / "b.png"), "-o", str(tmp_path / "x.pdf")]) == 2


def test_cli_list_target_from_out_and_errors(tmp_path, report, capsys):
    assert main(["convert", "--list"]) == 0
    listed = capsys.readouterr().out
    for word in ("heic", "svg", "dwg", "docx", "xlsx", "Typst", "LibreOffice", "pypdfium2"):
        assert word in listed
    assert main(["convert", str(report), "--pages", "1", "-o", str(tmp_path / "cover.webp")]) == 0
    assert (tmp_path / "cover.webp").is_file()
    capsys.readouterr()
    assert main(["convert", str(report)]) == 2  # a PDF needs a target
    assert "--to" in capsys.readouterr().err
    assert main(["convert", str(report), "--to", "pptx"]) == 2
    # never overwrite: a second run gets a new name
    first = convert.convert(report, "md", quiet=True).output
    second = convert.convert(report, "md", quiet=True).output
    assert first != second and first.exists() and second.exists()


def test_batch_continues_after_a_failure(tmp_path, capsys):
    good = tmp_path / "ok.png"
    _picture().save(good)
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    rc = main(["convert", str(bad), str(good)])
    assert rc == 2 and (tmp_path / "ok.pdf").is_file()
    assert "1 of 2 conversions failed" in capsys.readouterr().err
