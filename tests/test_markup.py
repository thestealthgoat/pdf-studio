from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pdfplumber
import pikepdf
import pytest

from pdfcore import engines, text
from pdfcore.cli import main
from pdfcore.ops import markup
from pdfcore.types import Rect

DPI = 72


def _render(path: Path, page: int = 0, dpi: float = DPI) -> np.ndarray:
    return np.asarray(engines.get("render").render(path, page, dpi=dpi)).astype(int)


def _region(img: np.ndarray, r: Rect, dpi: float = DPI) -> np.ndarray:
    s = dpi / 72
    return img[int(r.top * s): int(r.bottom * s) + 1, int(r.x0 * s): int(r.x1 * s) + 1]


ALL_SPECS = [
    {"type": "highlight", "page": 0, "rects": [[72, 300, 200, 312]]},
    {"type": "underline", "page": 0, "rects": [[72, 330, 200, 342]]},
    {"type": "strikeout", "page": 0, "rects": [[72, 360, 200, 372]]},
    {"type": "squiggly", "page": 0, "rects": [[72, 390, 200, 402]]},
    {"type": "note", "page": 0, "at": [540, 40], "contents": "Check the address", "author": "Reviewer A"},
    {"type": "freetext", "page": 0, "rect": [300, 420, 540, 470], "text": "Free text — Ünïcode ok", "fill": "#ffffcc",
     "color": "blue"},
    {"type": "callout", "page": 0, "rect": [380, 500, 560, 540], "text": "Callout text", "target": [250, 560]},
    {"type": "square", "page": 0, "rect": [60, 600, 160, 660], "fill": "#ffeeee"},
    {"type": "circle", "page": 0, "rect": [180, 600, 280, 660], "color": "green"},
    {"type": "line", "page": 0, "points": [[300, 610], [420, 650]], "heads": ["Circle", "ClosedArrow"]},
    {"type": "polygon", "page": 0, "points": [[440, 600], [520, 620], [480, 670]]},
    {"type": "polyline", "page": 0, "points": [[60, 690], [120, 700], [160, 680]], "heads": ["None", "OpenArrow"]},
    {"type": "cloud", "page": 0, "rect": [200, 690, 380, 740], "contents": "Revise this"},
    {"type": "ink", "page": 0, "strokes": [[[420, 690], [440, 700], [460, 690], [480, 720]]]},
    {"type": "stamp", "page": 0, "at": [380, 30], "label": "APPROVED", "author": "Owen"},
]


def test_highlight_search_changes_pixels_in_hit_area(work):
    src = work("contract_v1")
    hits = text.search(src, "Contractor")
    assert hits
    res = markup.highlight(src, "Contractor", author="QA", quiet=True)
    assert res.ok, res.line()
    assert res.output != src and res.data["added"] == len(hits)
    h = hits[0]
    before, after = _render(src, h.page), _render(res.output, h.page)
    r = h.rects[0]
    reg_b, reg_a = _region(before, r), _region(after, r)
    # yellow multiply: blue channel drops, red stays high in the hit area
    assert reg_a[..., 2].mean() < reg_b[..., 2].mean() - 40
    assert reg_a[..., 0].mean() > 150
    # an area away from any hit is untouched
    far = Rect(10, 10, 60, 40)
    assert np.array_equal(_region(before, far), _region(after, far))
    # pikepdf and pdfplumber both see it
    with pikepdf.open(res.output) as pdf:
        a = pdf.pages[h.page].Annots[0]
        assert a.Subtype == "/Highlight" and len(a.QuadPoints) == 8 * len(h.rects)
        assert isinstance(a.AP.N, pikepdf.Stream) and str(a.T) == "QA"
        assert str(a.Contents) == h.text
    with pdfplumber.open(res.output) as pdf:
        subs = [an["data"]["Subtype"].name for an in pdf.pages[h.page].annots]
        assert "Highlight" in subs


def test_every_kind_has_appearance_and_passes_gate(work):
    src = work("letter")
    res = markup.annotate(src, ALL_SPECS, quiet=True)
    assert res.ok, res.line()
    assert res.data["added"] == len(ALL_SPECS) and not res.data["skipped"]
    before, after = _render(src), _render(res.output)
    with pikepdf.open(res.output) as pdf:
        annots = [a for a in pdf.pages[0].Annots if a.Subtype != "/Popup"]
        assert len(annots) == len(ALL_SPECS)
        for a in annots:
            assert isinstance(a.AP.N, pikepdf.Stream), a.Subtype
            assert "/CreationDate" in a and "/M" in a and "/NM" in a
        by = {str(a.Subtype): a for a in annots}
        cloud = [a for a in annots if a.Subtype == "/Polygon" and "/BE" in a][0]
        assert cloud.BE.S == "/C" and float(cloud.BE.I) == 1.0
        assert cloud.AP.N.read_bytes().count(b" c\n") + cloud.AP.N.read_bytes().count(b" c ") > 20  # scallops
        assert [str(v) for v in by["/Line"].LE] == ["/Circle", "/ClosedArrow"]
        ft = [a for a in annots if a.Subtype == "/FreeText" and "/IT" not in a][0]
        assert "Tf" in str(ft.DA)
        co = [a for a in annots if a.get("/IT") == "/FreeTextCallout"][0]
        assert len(co.CL) == 6 and co.LE == "/OpenArrow"
    # every spec changed pixels somewhere inside its geometry
    for spec in ALL_SPECS:
        if "rect" in spec or "rects" in spec:
            r = Rect(*(spec.get("rect") or spec["rects"][0]))
        elif "points" in spec:
            xs, ys = [p[0] for p in spec["points"]], [p[1] for p in spec["points"]]
            r = Rect(min(xs), min(ys), max(xs), max(ys)).expand(2)
        elif "strokes" in spec:
            pts = spec["strokes"][0]
            r = Rect(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)).expand(2)
        else:
            r = Rect(spec["at"][0], spec["at"][1], spec["at"][0] + 20, spec["at"][1] + 20)
        assert not np.array_equal(_region(before, r), _region(after, r)), spec["type"]


def test_text_in_appearances_uses_embedded_font(work):
    src = work("letter")
    res = markup.annotate(src, [s for s in ALL_SPECS if s["type"] in ("freetext", "callout", "stamp")], quiet=True)
    assert res.ok, res.line()
    from pdfcore.gate.checks import check_fonts

    with pikepdf.open(res.output) as pdf:
        assert check_fonts(pdf) == []
        names = set()
        for a in pdf.pages[0].Annots:
            for _, x in a.AP.N.Resources.get("/XObject", {}).items():
                for _, f in x.Resources.get("/Font", {}).items():
                    names.add(str(f.BaseFont))
        assert any("Inter" in n for n in names)


def test_opacity_in_dict_and_extgstate(work):
    src = work("letter")
    res = markup.annotate(src, [{"type": "square", "page": 0, "rect": [100, 100, 200, 200], "fill": "red",
                                 "opacity": 0.5}], quiet=True)
    with pikepdf.open(res.output) as pdf:
        a = pdf.pages[0].Annots[0]
        assert float(a.CA) == 0.5
        gs = list(a.AP.N.Resources.ExtGState.values())[0]
        assert float(gs.ca) == 0.5 and float(gs.CA) == 0.5
    px = _render(res.output)[150, 150]
    assert 100 < px[1] < 200  # half-transparent red over white


def test_rect_position_unrotated_matches_user_space(work):
    src = work("letter")
    res = markup.annotate(src, [{"type": "square", "page": 0, "rect": [100, 50, 200, 80]}], quiet=True)
    with pikepdf.open(res.output) as pdf:
        H = float(pdf.pages[0].mediabox[3])
        rect = [float(v) for v in pdf.pages[0].Annots[0].Rect]
    pad = 1.5 / 2 + 1
    assert rect == pytest.approx([100 - pad, H - 80 - pad, 200 + pad, H - 50 + pad], abs=0.01)
    info = markup.list_annotations(res.output)[0]
    assert info["rect"] == pytest.approx([100, 50, 200, 80], abs=0.05)


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_rotated_page_highlight_lands_on_visual_rect(work, rot):
    src = work("letter")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Rotate = rot
        pdf.save(src)
    r = Rect(150, 200, 260, 230)  # visual, top-left of the displayed page
    res = markup.annotate(src, [{"type": "highlight", "page": 0, "rects": [[r.x0, r.top, r.x1, r.bottom]]},
                                {"type": "freetext", "page": 0, "rect": [300, 300, 500, 340], "text": "upright"}],
                          quiet=True)
    assert res.ok, res.line()
    img = _render(res.output)
    reg = _region(img, r.expand(-2))
    assert (reg[..., 2] < 200).mean() > 0.5  # yellow where we asked, in the rendered (rotated) view
    info = markup.list_annotations(res.output)
    assert info[0]["rects"][0] == pytest.approx([r.x0, r.top, r.x1, r.bottom], abs=0.05)
    with pikepdf.open(res.output) as pdf:
        ap = pdf.pages[0].Annots[1].AP.N.read_bytes().decode()
        assert ap.splitlines()[0].endswith("cm") and not ap.startswith("1 0 0 1")


def test_list_delete_flatten(work):
    src = work("letter")
    specs = [
        {"type": "highlight", "page": 0, "rects": [[72, 300, 200, 312]], "author": "Ann", "contents": "check"},
        {"type": "note", "page": 0, "at": [500, 60], "contents": "address?", "author": "Bob"},
        {"type": "cloud", "page": 0, "rect": [200, 690, 380, 740], "author": "Ann"},
    ]
    for sp in specs:
        sp["created"] = "2026-10-01T09:30:00-05:00"
    a = markup.annotate(src, specs, quiet=True).output
    items = markup.list_annotations(a)
    assert [i["type"] for i in items] == ["highlight", "note", "cloud"]
    assert items[0]["author"] == "Ann" and items[0]["color"] == "#ffeb00"
    assert items[1]["contents"] == "address?" and items[1]["created"].startswith("2026-10-01T09:30")
    d = markup.delete_annotations(a, authors=["ann"], quiet=True)
    assert d.ok and d.data["removed"] == 2
    left = markup.list_annotations(d.output)
    assert [i["type"] for i in left] == ["note"]
    d2 = markup.delete_annotations(a, types=["note"], quiet=True)
    with pikepdf.open(d2.output) as pdf:  # popup went with its note
        assert all(x.Subtype != "/Popup" for x in pdf.pages[0].Annots)
    f = markup.flatten_annotations(a, quiet=True)
    assert f.ok and f.data["flattened"] == 3
    with pikepdf.open(f.output) as pdf:
        assert "/Annots" not in pdf.pages[0].obj
    diff = np.abs(_render(a) - _render(f.output))
    assert diff.max() <= 8  # flattened page looks the same as the annotated page
    assert not np.array_equal(_render(src), _render(f.output))


def test_export_import_roundtrip(work, tmp_path):
    src = work("letter")
    a = markup.annotate(src, ALL_SPECS, quiet=True).output
    e = markup.export_annotations(a, out=tmp_path / "ann.json", quiet=True)
    data = json.loads(e.output.read_text())
    assert len(data["annotations"]) == len(ALL_SPECS)
    i = markup.import_annotations(src, e.output, out=tmp_path / "re.pdf", quiet=True)
    assert i.ok and i.data["added"] == len(ALL_SPECS) and not i.data["skipped"]
    orig = markup.list_annotations(a)
    back = markup.list_annotations(i.output)
    assert [x["type"] for x in back] == [x["type"] for x in orig]
    for x, y in zip(orig, back):
        assert y["rect"] == pytest.approx(x["rect"], abs=0.6), x["type"]
        assert y["contents"] == x["contents"] and y["author"] == x["author"]


def test_comment_summary_report_and_csv(work):
    src = work("letter")
    a = markup.annotate(src, ALL_SPECS[:6] + [ALL_SPECS[12]], quiet=True).output
    res = markup.comment_summary(a, quiet=True)
    assert res.ok, res.line()
    csv_path = res.extra_outputs[0]
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 7 and rows[0]["page"] == "1"
    with pdfplumber.open(res.output) as pdf:
        txt = "\n".join(p.extract_text() or "" for p in pdf.pages)
        assert "Comment summary" in txt and "Check the address" in txt and "Revise this" in txt
        assert len(pdf.pages[0].images) >= 1  # thumbnails


def test_cli_annotate_and_list(work, capsys):
    src = work("contract_v1")
    out = src.with_name("cli.pdf")
    assert main(["annotate", str(src), "--kind", "underline", "--search", "Client", "-o", str(out)]) == 0
    assert main(["annots", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "underline" in printed and "PASS" in printed
    assert main(["annotate", str(out), "--kind", "stamp", "--text", "REJECTED", "--at", "400,40", "--page", "1",
                 "-o", str(src.with_name("cli2.pdf"))]) == 0


def test_parse_color_and_dates():
    assert markup.parse_color("#ff0000") == (1.0, 0.0, 0.0)
    assert markup.parse_color((255, 128, 0))[1] == pytest.approx(128 / 255)
    assert markup.parse_color("none") is None
    d = markup.pdf_date("2026-10-01T09:30:00-05:00")
    assert d == "D:20261001093000-05'00'"
    assert markup.parse_pdf_date(d).isoformat() == "2026-10-01T09:30:00-05:00"
