from __future__ import annotations

import numpy as np
import pdfplumber
import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfcore.ops import stamp as st


def _render(path, i=0, dpi=40):
    doc = pdfium.PdfDocument(str(path))
    try:
        return np.asarray(doc[i].render(scale=dpi / 72).to_pil().convert("L"), dtype=np.int16)
    finally:
        doc.close()


def _texts(path) -> list[str]:
    with pdfplumber.open(str(path)) as pdf:
        return [p.extract_text() or "" for p in pdf.pages]


def test_tokens_and_lengths():
    assert st.format_tokens("Page {page} of {pages} {x}", {"page": 2, "pages": 5}) == "Page 2 of 5 {x}"
    assert st.format_tokens("{page:03d}", {"page": 7}) == "007"
    assert st.parse_length("0.5in") == 36 and abs(st.parse_length("25.4mm") - 72) < 1e-9 and st.parse_length(10) == 10


def test_page_x_of_y_report(work):
    src = work("report_tables")
    res = st.page_numbers(src, quiet=True)
    assert res.ok, res.gate.details()
    assert res.output != src and res.output.exists()
    txt = _texts(res.output)
    for i, t in enumerate(txt):
        assert f"Page {i + 1} of {len(txt)}" in t


def test_six_positions_slides(work):
    src = work("slides")  # landscape, 6 pages
    texts = {"header-left": "{title}", "header-center": "HC", "header-right": "{date}",
             "footer-left": "{file}", "footer-center": "Page {page} of {pages}", "footer-right": "L{label}"}
    res = st.header_footer(src, texts, pages="2-6", date_format="%Y", quiet=True)
    assert res.ok, res.gate.details()
    txt = _texts(res.output)
    assert "Page 1 of" not in txt[0]  # page 1 not selected
    assert "Page 2 of 6" in txt[1] and "Page 5 of 6" in txt[4]
    assert "Project Kickoff Deck" in txt[2] and "slides.pdf" in txt[2] and "HC" in txt[2] and "L3" in txt[2]
    # header text sits at the top of the landscape page, footer at the bottom
    with pdfplumber.open(str(res.output)) as pdf:
        pg = pdf.pages[1]
        words = pg.extract_words()
        hc = next(w for w in words if w["text"] == "HC")
        foot = next(w for w in words if w["text"] == "Page")
        assert hc["top"] < 40 and foot["bottom"] > float(pg.height) - 40


def test_rotated_page_footer_lands_at_visual_bottom(work, tmp_path):
    src = work("letter")
    rot = tmp_path / "letter_rot.pdf"
    with pikepdf.open(src) as pdf:
        pdf.pages[0].Rotate = 90
        pdf.save(rot)
    res = st.page_numbers(rot, "Page {page} of {pages}", quiet=True)
    assert res.ok, res.gate.details()
    before, after = _render(rot, dpi=100), _render(res.output, dpi=100)
    assert after.shape[1] > after.shape[0]  # visually landscape
    diff = np.abs(after - before) > 60
    ys, xs = np.nonzero(diff)
    assert len(ys) > 20, "nothing drawn"
    H, W = after.shape
    # every changed pixel is in the bottom band of the *visual* page, horizontally centred
    assert ys.min() > H * 0.9, (ys.min(), H)
    assert abs(xs.mean() - W / 2) < W * 0.1
    # and there are dark pixels near the visual bottom edge
    assert (after[int(H * 0.9):, :] < 160).sum() > 20
    assert (after[: int(H * 0.1), :] < 160).sum() == (before[: int(H * 0.1), :] < 160).sum()


def test_bates_continues_across_files(work):
    a, b = work("letter"), work("slides")
    res = st.bates([a, b], prefix="ABC", start=41, digits=6, quiet=True)
    assert res.ok, res.gate.details()
    assert res.data["next"] == 41 + 1 + 6
    assert len(res.extra_outputs) == 1
    r0, r1 = res.data["ranges"]
    assert (r0["first"], r0["last"]) == ("ABC000041", "ABC000041")
    assert (r1["first"], r1["last"]) == ("ABC000042", "ABC000047")
    t1 = _texts(res.extra_outputs[0])
    assert "ABC000042" in t1[0] and "ABC000047" in t1[5]
    with pikepdf.open(res.extra_outputs[0]) as pdf:
        assert "Bates ABC000042-ABC000047" in str(pdf.docinfo["/Keywords"])
        assert str(pdf.docinfo["/BatesLast"]) == "ABC000047"
    # continue from the returned number
    res2 = st.bates(work("report_tables"), pattern="BAR-{n:05d}-X", start=res.data["next"], quiet=True)
    assert res2.data["ranges"][0]["first"] == "BAR-00048-X"


def test_watermark_text_opacity_slides(work):
    src = work("slides")
    res = st.watermark(src, "CONFIDENTIAL", opacity=0.3, color="#C00000", quiet=True)
    assert res.ok, res.gate.details()
    with pikepdf.open(res.output) as pdf:
        raw = repr([list(x.Resources.get("/ExtGState", {}).items()) for _, x in pdf.pages[2].Resources.XObject.items()
                    if x.get("/Subtype") == "/Form"])
        assert "/ca" in raw
    before, after = _render(src, 2), _render(res.output, 2)
    H, W = after.shape
    centre = np.abs(after - before)[H // 3: 2 * H // 3, W // 4: 3 * W // 4]
    assert (centre > 20).sum() > 100
    # opacity: where the watermark lies on white paper it is a light tint, not solid red (L≈92)
    on_white = (before == 255) & (np.abs(after - before) > 20)
    assert on_white.sum() > 50 and after[on_white].min() > 150


def test_watermark_behind_and_image(work, tmp_path):
    from PIL import Image

    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 100), (20, 60, 160, 255)).save(logo)
    src = work("letter")
    res = st.watermark(src, image=logo, opacity=0.25, behind=True, quiet=True)
    assert res.ok, res.gate.details()
    with pikepdf.open(res.output) as pdf:
        first = pdf.pages[0].obj.Contents
        first = first[0] if isinstance(first, pikepdf.Array) else first
        assert b"Do" in first.read_bytes()  # overlay prepended → underneath
    before, after = _render(src), _render(res.output)
    assert (np.abs(after - before) > 10).sum() > 500
    res2 = st.watermark(src, "DRAFT", angle="horizontal", quiet=True)
    assert res2.ok


def test_approval_stamp_drawing_set(work):
    src = work("drawings_rev0")  # ARCH D
    res = st.approval_stamp(src, "ISSUED FOR CONSTRUCTION", name="J. Smith", date="2026-11-13",
                            position="bottom-left", pages="all", quiet=True)
    assert res.ok, res.gate.details()
    assert res.data["approval_pages"] == [1, 2, 3, 4, 5]
    t = _texts(res.output)[3]
    for phrase in ("ISSUED FOR CONSTRUCTION", "BY: J. Smith", "DATE: 2026-11-13"):
        assert phrase in t
    with pdfplumber.open(str(res.output)) as pdf:  # auto-scaled up on the big sheet, near bottom-left
        pg = pdf.pages[3]
        w = max((x for x in pg.extract_words(extra_attrs=["size"]) if x["text"] == "ISSUED"), key=lambda x: x["size"])
        assert w["size"] > 30 and w["x0"] < 200 and w["bottom"] > float(pg.height) - 300
    # rotated stamp stays on the sheet and passes the gate
    res2 = st.approval_stamp(src, "REJECTED", rotation=-25, position="top-right", pages="4", quiet=True)
    assert res2.ok, res2.gate.details()
    before, after = _render(src, 3, 20), _render(res2.output, 3, 20)
    ys, xs = np.nonzero(np.abs(after - before) > 40)
    H, W = after.shape
    assert len(ys) and ys.max() < H * 0.4 and xs.min() > W * 0.6


@pytest.mark.parametrize("label", ["APPROVED", "REVIEWED", "REJECTED", "Checked by QA"])
def test_approval_labels_and_xy(work, label):
    src = work("letter")
    res = st.approval_stamp(src, label, at=(300, 400), date="today", quiet=True)
    assert res.ok, res.gate.details()
    with pdfplumber.open(str(res.output)) as pdf:
        pg = pdf.pages[0]
        w = [x for x in pg.extract_words() if x["text"] == label.split()[0]]
        assert w and 290 < w[0]["x0"] < 340 and float(pg.height) - 470 < w[0]["top"] < float(pg.height) - 400


def test_combined_cli(work, capsys):
    from pdfcore.cli import main

    src = work("report_tables")
    rc = main(["stamp", str(src), "--page-numbers", "--header-right", "{title}", "--bates", "BAR-{n:04d}",
               "--watermark", "DRAFT", "--approve", "REVIEWED", "--approve-name", "Q. Tester"])
    out = capsys.readouterr().out
    assert rc == 0 and "PASS" in out and "next number 4" in out
    outp = src.with_name("report_tables.stamped.pdf")
    t = _texts(outp)
    assert "Page 3 of 3" in t[2] and "BAR-0003" in t[2] and "Q. Tester" in t[0]


def test_bates_cli(work, tmp_path, capsys):
    from pdfcore.cli import main

    a, b = work("letter"), work("contract_v1")
    od = tmp_path / "out"
    od.mkdir()
    rc = main(["bates", str(a), str(b), "--prefix", "X-", "--digits", "4", "--out-dir", str(od)])
    out = capsys.readouterr().out
    assert rc == 0 and "X-0001" in out and "next Bates number" in out
    assert len(list(od.glob("*.pdf"))) == 2


def test_never_overwrites_input(work):
    src = work("letter")
    with pytest.raises(ValueError):
        st.page_numbers(src, out=src, quiet=True)
