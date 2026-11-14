from __future__ import annotations

import openpyxl
import pdfplumber
import pikepdf
import pytest
from PIL import Image, ImageDraw

from pdfcore.cli import main
from pdfcore.ops import convert, extract

soffice = pytest.mark.skipif(convert.find_soffice() is None, reason="LibreOffice (soffice) not installed")


def _text(pdf) -> str:
    with pdfplumber.open(str(pdf)) as d:
        return "\n".join(p.extract_text() or "" for p in d.pages)


def _sizes(pdf) -> list[tuple[float, float]]:
    with pikepdf.open(pdf) as d:
        return [(round(float(p.mediabox[2]), 1), round(float(p.mediabox[3]), 1)) for p in d.pages]


def _xobj(page):
    return next(iter(page.Resources.XObject.values()))


def _drawn(size, mode="RGB", color=(30, 90, 160)) -> Image.Image:
    im = Image.new(mode, size, "white" if mode != "1" else 1)
    d = ImageDraw.Draw(im)
    fill = 0 if mode in ("1", "L") else color
    d.rectangle([size[0] // 5, size[1] // 5, size[0] * 4 // 5, size[1] * 3 // 5], fill=fill)
    return im


# ------------------------------------------------------------------ office → PDF (LibreOffice)


@pytest.mark.external
@soffice
def test_xlsx_and_txt_to_pdf(tmp_path, capsys):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item", "Qty", "Cost"])
    ws.append(["Anchor bolts", 48, 2.75])
    ws.append(["Conduit 3/4in", 120, 4.1])
    ws.column_dimensions["A"].width = 30
    xlsx = tmp_path / "budget.xlsx"
    wb.save(xlsx)
    txt = tmp_path / "notes.txt"
    txt.write_text("Site walk notes\nPanel LP-1 relocated to east wall.\n")

    res = convert.office_to_pdf(xlsx)
    assert res.ok and res.output == tmp_path / "budget.pdf"
    assert "Anchor bolts" in _text(res.output)
    res2 = convert.to_pdf(txt)
    assert res2.ok and res2.output.name == "notes.pdf"
    assert "Panel LP-1 relocated" in _text(res2.output)
    # a second conversion never overwrites the first
    res3 = convert.to_pdf(txt, quiet=True)
    assert res3.output.name == "notes.converted.pdf"
    assert capsys.readouterr().out.count("PASS") == 2


def test_office_missing_input(tmp_path):
    with pytest.raises(FileNotFoundError):
        convert.office_to_pdf(tmp_path / "nope.docx")


def test_soffice_missing_message(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    with pytest.raises(RuntimeError, match="LibreOffice"):
        convert.office_to_pdf(f, soffice=str(tmp_path / "no-such-soffice"))
    assert not (tmp_path / "a.pdf").exists()


# ------------------------------------------------------------------ images → PDF


def test_rendered_pages_round_trip_to_pdf(work):
    src = work("report_tables")
    pages = extract.render(src, dpi=100, quiet=True).extra_outputs
    res = convert.images_to_pdf(pages, quiet=True)
    assert res.ok and res.data["pages"] == 3
    assert _sizes(res.output) == [(612.0, 792.0)] * 3


def test_jpeg_is_embedded_unchanged(work):
    src = work("report_tables")
    jpg = extract.render(src, pages="1", dpi=72, fmt="jpeg", quiet=True).output
    res = convert.images_to_pdf(jpg, quiet=True)
    with pikepdf.open(res.output) as d:
        xo = _xobj(d.pages[0])
        assert xo.Filter == pikepdf.Name.DCTDecode
        assert xo.read_raw_bytes() == jpg.read_bytes()
    assert _sizes(res.output) == [(612.0, 792.0)]


def test_multipage_tiff_dpi_and_bilevel(tmp_path):
    a = _drawn((300, 200), "1")
    b = _drawn((200, 300), "L")
    tif = tmp_path / "scan.tif"
    a.save(tif, save_all=True, append_images=[b], dpi=(100, 100), compression="tiff_deflate")
    res = convert.images_to_pdf(tif, quiet=True)
    assert res.ok, res.line()
    assert _sizes(res.output) == [(216.0, 144.0), (144.0, 216.0)]
    with pikepdf.open(res.output) as d:
        bpc = [int(_xobj(p).BitsPerComponent) for p in d.pages]
    assert bpc == [1, 8]


def test_png_alpha_and_fit_to_letter(tmp_path):
    im = _drawn((800, 400), "RGBA", (200, 30, 30))
    im.putalpha(200)
    png = tmp_path / "logo.png"
    im.save(png)
    res = convert.images_to_pdf(png, page_size="letter", margin=36, quiet=True)
    assert _sizes(res.output) == [(792.0, 612.0)]  # landscape image → landscape page
    with pikepdf.open(res.output) as d:
        xo = _xobj(d.pages[0])
        assert "/SMask" in xo


def test_exif_rotation_uses_page_rotate(tmp_path):
    im = _drawn((400, 200))
    exif = Image.Exif()
    exif[0x0112] = 6  # display rotated 90° clockwise
    jpg = tmp_path / "photo.jpg"
    im.save(jpg, dpi=(200, 200), exif=exif)
    res = convert.images_to_pdf(jpg, quiet=True)
    with pikepdf.open(res.output) as d:
        assert int(d.pages[0].Rotate) == 90
    assert _sizes(res.output) == [(144.0, 72.0)]


def test_images_to_pdf_cli(tmp_path, capsys):
    paths = []
    for k in range(2):
        p = tmp_path / f"img{k}.png"
        _drawn((150, 150)).save(p, dpi=(150, 150))
        paths.append(str(p))
    assert main(["images-to-pdf", *paths, "-o", str(tmp_path / "both.pdf")]) == 0
    assert capsys.readouterr().out.strip().endswith("PASS")
    assert _sizes(tmp_path / "both.pdf") == [(72.0, 72.0)] * 2


# ------------------------------------------------------------------ dispatch, HTML, PDF → images


def test_html_needs_weasyprint(tmp_path, monkeypatch):
    h = tmp_path / "page.html"
    h.write_text("<h1>Hello</h1>")
    monkeypatch.setattr(convert, "find_soffice", lambda: None)  # LibreOffice is the fallback; hide it
    try:
        import weasyprint  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match=r"pdf-studio\[html\]"):
            convert.to_pdf(h)
    else:
        assert convert.to_pdf(h, quiet=True).ok


def test_to_pdf_rejects_pdf_and_unknown(tmp_path, work):
    with pytest.raises(ValueError, match="already a PDF"):
        convert.to_pdf(work("letter"))
    f = tmp_path / "x.unknown"
    f.write_text("?")
    with pytest.raises(ValueError):
        convert.to_pdf(f)


def test_pdf_to_images_alias(work):
    res = convert.pdf_to_images(work("letter"), dpi=30, quiet=True)
    assert res.output.suffix == ".png" and res.extra_outputs
