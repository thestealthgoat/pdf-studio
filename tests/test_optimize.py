from __future__ import annotations

import zlib

import numpy as np
import pdfplumber
import pikepdf
import pypdfium2 as pdfium
import pytest
from pikepdf import Array, Dictionary, Name

from pdfcore.ops import optimize as op
from pdfcore.ops._common import gate_options_from


def _flt(x) -> list[str]:
    f = x.get("/Filter")
    return [] if f is None else [str(f)] if isinstance(f, Name) else [str(v) for v in f]


def _render(path, i=0, dpi=72) -> np.ndarray:
    doc = pdfium.PdfDocument(str(path))
    try:
        return np.asarray(doc[i].render(scale=dpi / 72).to_pil().convert("RGB"), dtype=np.int16)
    finally:
        doc.close()


_KEEP: list = []


def _images(path) -> list[pikepdf.Stream]:
    pdf = pikepdf.open(str(path))
    _KEEP.append(pdf)  # keep the document alive while the test inspects its objects
    return [x for x in pdf.objects if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == "/Image"]


# ------------------------------------------------------------------ image_heavy (50 MB, 300 dpi lossless)
def test_image_heavy_screen(work):
    src = work("image_heavy")
    before = src.stat().st_size
    res = op.optimize(src, dpi=150, quality=75, options=gate_options_from(min_image_dpi=150), quiet=True)
    assert res.ok, res.gate.details()
    after = res.output.stat().st_size
    assert after < before / 10, (before, after)
    d = res.data
    assert d["before_bytes"] == before and d["after_bytes"] == after and d["saved_pct"] > 90
    assert d["images_downsampled"] == 4 and d["jpeg"] == 4
    with pdfplumber.open(str(res.output)) as pdf:
        for page in pdf.pages:
            for im in page.images:
                dpi = im["srcsize"][0] / ((im["x1"] - im["x0"]) / 72)
                assert 149.5 <= dpi < 160
    a, b = _render(src), _render(res.output)
    assert a.shape == b.shape
    assert np.abs(a - b).mean() < 4.0
    # text untouched
    with pdfplumber.open(str(res.output)) as pdf:
        assert "Gallery page 1" in pdf.pages[0].extract_text()


def test_size_budget(work):
    src = work("image_heavy")
    res = op.optimize(src, target_mb=0.3, quiet=True)
    assert res.ok, res.gate.details()
    assert res.data["budget_met"] and res.output.stat().st_size <= 0.3e6
    assert len(res.data["attempts"]) >= 2  # first setting (~0.45 MB) was not enough
    impossible = op.optimize(src, target_mb=0.01, out=src.with_name("tiny.pdf"), quiet=True)
    assert not impossible.data["budget_met"]
    assert not impossible.ok and "budget" in impossible.line()
    sizes = [a["bytes"] for a in impossible.data["attempts"]]
    assert impossible.output.stat().st_size == min(sizes)


# ------------------------------------------------------------------ synthetic mixed-image file
def _mixed_pdf(path) -> None:
    rng = np.random.default_rng(3)
    pdf = pikepdf.new()
    W = H = 1200
    yy, xx = np.mgrid[0:H, 0:W]
    photo = np.stack([xx * 255 // W, yy * 255 // H, (xx + yy) * 255 // (W + H)], -1).astype(np.int16)
    photo = np.clip(photo + rng.normal(0, 14, photo.shape), 0, 255).astype(np.uint8)
    alpha = np.clip(255 - np.hypot(xx - W / 2, yy - H / 2) * 255 / (W / 2), 0, 255).astype(np.uint8)
    smask = pdf.make_stream(zlib.compress(alpha.tobytes()), Type=Name.XObject, Subtype=Name.Image, Width=W, Height=H,
                            ColorSpace=Name.DeviceGray, BitsPerComponent=8, Filter=Name.FlateDecode)

    def img(arr, cs, bpc=8, **kw):
        return pdf.make_stream(zlib.compress(arr.tobytes()), Type=Name.XObject, Subtype=Name.Image, Width=arr.shape[1],
                               Height=arr.shape[0], ColorSpace=cs, BitsPerComponent=bpc, Filter=Name.FlateDecode, **kw)

    im_photo = img(photo, Name.DeviceRGB, SMask=smask)
    im_dup = img(photo, Name.DeviceRGB, SMask=smask)  # byte-identical duplicate object
    line = np.full((1500, 1500), 255, np.uint8)
    line[::50, :] = 0
    line[:, ::75] = 0
    line[700:800, 300:1200] = 128
    im_line = img(line[:, :, None], Name.DeviceGray)
    cmyk = np.zeros((800, 800, 4), np.uint8)
    cmyk[..., 1] = (np.mgrid[0:800, 0:800][1] * 255 // 800).astype(np.uint8)  # magenta ramp
    cmyk[..., 2] = 200
    cmyk = np.clip(cmyk + rng.normal(0, 10, cmyk.shape), 0, 255).astype(np.uint8)
    im_cmyk = img(cmyk, Name.DeviceCMYK)
    g16 = np.stack([xx[:600, :600] * 109, yy[:600, :600] * 109, np.full((600, 600), 30000)], -1).astype(">u2")
    g16 = (g16.astype(np.int32) + rng.normal(0, 900, g16.shape)).clip(0, 65535).astype(">u2")
    im16 = img(g16, Name.DeviceRGB, bpc=16)
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(XObject=Dictionary(P=im_photo, D=im_dup, L=im_line, C=im_cmyk, S=im16))
    page.Contents = pdf.make_stream(
        b"q 0.9 0.9 0.6 rg 0 0 612 792 re f Q\n"
        b"q 144 0 0 144 54 600 cm /P Do Q\n"        # 1200 px over 2 in = 600 dpi
        b"q 144 0 0 144 300 600 cm /D Do Q\n"
        b"q 180 0 0 180 54 380 cm /L Do Q\n"        # 1500 px over 2.5 in = 600 dpi
        b"q 144 0 0 144 300 380 cm /C Do Q\n"       # 800 px over 2 in = 400 dpi
        b"q 0.5 0 0 0.5 0 0 cm q 216 0 0 216 108 200 cm /S Do Q Q\n"  # nested cm: 600 px over 108 pt = 400 dpi
    )
    pdf.docinfo["/Title"] = "Mixed images"
    pdf.save(path)


def test_mixed_images(tmp_path):
    src = tmp_path / "mixed.pdf"
    _mixed_pdf(src)
    res = op.optimize(src, dpi=150, quiet=True)
    assert res.ok, res.gate.details()
    assert res.data["duplicates_merged"] >= 1
    assert res.data["after_bytes"] < res.data["before_bytes"] / 3
    ims = _images(res.output)
    photo = next(x for x in ims if "/SMask" in x)
    assert (int(photo.Width), int(photo.Height)) == (300, 300)  # 1200 px over 2 in → 150 dpi
    assert _flt(photo) == ["/DCTDecode"]
    sm = photo.SMask  # transparency kept, mask downsampled with it
    assert (int(sm.Width), int(sm.Height)) == (300, 300)
    assert sum(1 for x in ims if "/SMask" in x) == 1  # duplicate merged
    line = next(x for x in ims if x.ColorSpace == Name.DeviceGray and "/SMask" not in x and int(x.Width) == 375)
    assert _flt(line) == ["/FlateDecode"]  # line art stays lossless
    cmyk = next(x for x in ims if x.ColorSpace == Name.DeviceCMYK)
    assert (int(cmyk.Width), int(cmyk.Height)) == (300, 300) and _flt(cmyk) == ["/DCTDecode"]
    s16 = next(x for x in ims if int(x.Width) == 225)  # 600 px over 1.5 in (nested cm) → 225 px at 150 dpi
    assert int(s16.BitsPerComponent) == 8
    a, b = _render(src), _render(res.output)
    diff = np.abs(a - b)
    assert diff.mean() < 3.0, diff.mean()
    # per-region check: CMYK image colours survive the JPEG round trip (Adobe inversion handled)
    cy0, cx0 = 792 - 380 - 144, 300
    assert diff[cy0 + 10: cy0 + 134, cx0 + 10: cx0 + 134].mean() < 8


def test_lossless_keeps_flate(tmp_path):
    src = tmp_path / "mixed.pdf"
    _mixed_pdf(src)
    res = op.optimize(src, settings=op.Settings(dpi=150, lossless=True), quiet=True)
    assert res.ok, res.gate.details()
    for x in _images(res.output):
        assert "/DCTDecode" not in _flt(x)


# ------------------------------------------------------------------ fonts
def _full_font_pdf(path) -> None:
    from fontTools.ttLib import TTFont

    from pdfcore.fonts import font_path

    pdf = pikepdf.new()
    inter = font_path("Inter-Regular.ttf").read_bytes()
    serif = font_path("SourceSerif4-Regular.ttf").read_bytes()
    tt = TTFont(font_path("Inter-Regular.ttf"))
    upm = tt["head"].unitsPerEm
    cmap = tt.getBestCmap()
    order = tt.getGlyphOrder()
    text = "Hello Type0 world"
    gids = [order.index(cmap[ord(ch)]) for ch in text]
    w_arr = Array()
    for g in sorted(set(gids)):
        w_arr.extend([g, Array([round(tt["hmtx"][order[g]][0] * 1000 / upm)])])
    tu = "/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def\n1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
    pairs = sorted({(g, ch) for g, ch in zip(gids, text)})
    tu += f"{len(pairs)} beginbfchar\n" + "".join(f"<{g:04X}> <{ord(ch):04X}>\n" for g, ch in pairs)
    tu += "endbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
    desc = pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name("/Inter-Regular"), Flags=32,
                                        FontBBox=Array([-800, -300, 2600, 1100]), ItalicAngle=0, Ascent=969, Descent=-241,
                                        CapHeight=727, StemV=80,
                                        FontFile2=pdf.make_stream(inter, Length1=len(inter))))
    cid = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.CIDFontType2, BaseFont=Name("/Inter-Regular"),
                                       CIDSystemInfo=Dictionary(Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0),
                                       FontDescriptor=desc, CIDToGIDMap=Name.Identity, DW=600, W=w_arr))
    t0 = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type0, BaseFont=Name("/Inter-Regular"),
                                      Encoding=Name("/Identity-H"), DescendantFonts=Array([cid]),
                                      ToUnicode=pdf.make_stream(tu.encode())))
    st = TTFont(font_path("SourceSerif4-Regular.ttf"))
    s_upm, s_cmap, s_order = st["head"].unitsPerEm, st.getBestCmap(), st.getGlyphOrder()
    widths = [round(st["hmtx"][s_cmap.get(c, ".notdef")][0] * 1000 / s_upm) for c in range(32, 127)]
    sdesc = pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name("/SourceSerif4-Regular"), Flags=32,
                                         FontBBox=Array([-400, -300, 1500, 1000]), ItalicAngle=0, Ascent=918, Descent=-335,
                                         CapHeight=670, StemV=80, FontFile2=pdf.make_stream(serif, Length1=len(serif))))
    simple = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.TrueType, BaseFont=Name("/SourceSerif4-Regular"),
                                          FirstChar=32, LastChar=126, Widths=Array(widths), Encoding=Name.WinAnsiEncoding,
                                          FontDescriptor=sdesc))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=t0, F2=simple))
    hexs = "".join(f"{g:04X}" for g in gids)
    page.Contents = pdf.make_stream(
        f"BT /F1 28 Tf 72 680 Td <{hexs}> Tj ET BT /F2 22 Tf 72 600 Td (Simple TrueType, fully embedded!) Tj ET".encode())
    pdf.docinfo["/Title"] = "Full fonts"
    pdf.save(path)


def test_font_resubset(tmp_path):
    src = tmp_path / "fonts.pdf"
    _full_font_pdf(src)
    res = op.optimize(src, dpi=None, quiet=True)
    assert res.ok, res.gate.details()
    fonts = {f["font"]: f for f in res.data["fonts"]}
    assert fonts["Inter-Regular"]["status"] == "subset"
    assert fonts["SourceSerif4-Regular"]["status"] == "subset"
    assert fonts["Inter-Regular"]["after"] < fonts["Inter-Regular"]["before"] / 4
    assert res.data["after_bytes"] < res.data["before_bytes"] / 3
    with pikepdf.open(res.output) as pdf:
        names = sorted(str(f.BaseFont) for f in pdf.pages[0].Resources.Font.values())
        assert all("+" in n for n in names)  # subset tag added
    # renders identically, text still extracts
    a, b = _render(src, dpi=100), _render(res.output, dpi=100)
    assert np.abs(a - b).max() < 40 and np.abs(a - b).mean() < 0.05
    with pdfplumber.open(str(res.output)) as pdf:
        t = pdf.pages[0].extract_text()
    assert "Hello Type0 world" in t and "Simple TrueType, fully embedded!" in t
    # second pass: already a subset → skipped with a reason
    again = op.optimize(res.output, dpi=None, quiet=True)
    assert all(f["status"].startswith("skipped") for f in again.data["fonts"])


def test_corpus_fonts_are_reported(work):
    res = op.optimize(work("letter"), quiet=True)
    assert res.ok, res.gate.details()
    assert res.data["fonts"] and all("already a subset" in f["status"] for f in res.data["fonts"])
    assert res.output.stat().st_size <= res.data["before_bytes"]


# ------------------------------------------------------------------ strip / linearize / CLI
def test_strip_and_linearize(work):
    src = work("report_tables")
    res = op.optimize(src, settings=op.Settings(strip_metadata=True, strip_private=True, linearize=True), quiet=True)
    assert res.ok, res.gate.details()
    with pikepdf.open(res.output) as pdf:
        assert pdf.is_linearized
        assert "/Metadata" not in pdf.Root
        assert set(pdf.docinfo.keys()) == {"/Title"}
    assert res.data["linearized"] is True


def test_encrypted_input_stays_encrypted(work):
    src = work("encrypted")
    res = op.optimize(src, password="test", quiet=True)
    assert res.ok, res.gate.details()
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(res.output)


def test_cli(work, capsys):
    from pdfcore.cli import main

    src = work("image_heavy")
    assert main(["optimize", str(src), "--preset", "screen", "--linearize"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "MB →" in out
    assert src.with_name("image_heavy.optimized.pdf").exists()
