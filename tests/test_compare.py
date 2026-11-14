from __future__ import annotations

import json

import numpy as np
import pikepdf
import pytest
from PIL import Image, ImageDraw

from pdfcore import engines
from pdfcore.cli import main
from pdfcore.ops import compare
from pdfcore.types import Rect

IN = 72.0
TITLE_BLOCK_X = (36 - 4.5) * IN  # drawings: title block is the right-hand 4.5 in


# ---------------------------------------------------------------- find_changes (unit)
def _canvas(w=400, h=300):
    im = Image.new("RGB", (w, h), "white")
    return im, ImageDraw.Draw(im)


def test_find_changes_regions_merge_and_specks():
    a, _ = _canvas()
    b, d = _canvas()
    d.rectangle([20, 20, 60, 40], fill="black")
    d.rectangle([66, 22, 90, 38], fill="black")  # 6 px from the first → merged
    d.rectangle([250, 200, 300, 260], outline="black", width=2)
    d.point((380, 10), fill="black")  # one-pixel speck → dropped
    rs = compare.find_changes(a, b, dpi=72)
    assert len(rs) == 2
    assert rs[0].x0 == pytest.approx(20, abs=1) and rs[0].x1 == pytest.approx(91, abs=1)
    assert rs[1].top == pytest.approx(200, abs=1) and rs[1].bottom == pytest.approx(261, abs=1)
    # px → pt scaling and ignore regions
    rs2 = compare.find_changes(a, b, dpi=144)
    assert rs2[0].x1 == pytest.approx(91 / 2, abs=1)
    assert len(compare.find_changes(a, b, dpi=72, ignore=[Rect(200, 150, 400, 300)])) == 1
    assert compare.find_changes(a, a.copy()) == []


def test_find_changes_different_sizes_and_arrays():
    a, _ = _canvas(300, 200)
    b, d = _canvas(400, 300)
    d.rectangle([320, 250, 340, 270], fill="black")  # outside a's area
    rs = compare.find_changes(np.asarray(a), np.asarray(b))
    assert len(rs) == 1 and rs[0].x0 == pytest.approx(320, abs=1)


def test_composite_colors():
    a, da = _canvas(60, 20)
    b, db = _canvas(60, 20)
    da.rectangle([0, 0, 19, 19], fill="black")  # old only
    db.rectangle([20, 0, 39, 19], fill="black")  # new only
    da.rectangle([40, 0, 59, 19], fill="black")
    db.rectangle([40, 0, 59, 19], fill="black")  # both
    px = np.asarray(compare.composite(a, b))
    assert tuple(px[10, 10]) == compare.OLD_RGB
    assert tuple(px[10, 30]) == compare.NEW_RGB
    assert tuple(px[10, 50]) == compare.SAME_RGB


# ---------------------------------------------------------------- text diff
def test_text_diff_contract(work):
    v1, v2 = work("contract_v1"), work("contract_v2")
    res = compare.text_diff(v1, v2, quiet=True)
    assert res.ok, res.line()
    s = res.data["summary"]
    assert s["insertions"] >= 1 and s["deletions"] >= 1
    ins = [c for c in res.data["changes"] if "$48,500" in c["new_text"]]
    assert ins and ins[0]["kind"] in ("insert", "change")
    assert ins[0]["new_text"].startswith("Compensation is increased to $48,500")
    assert ins[0]["new_page"] == 0 and ins[0]["new_rects"]
    dels = [c for c in res.data["changes"] if c["kind"] == "delete"]
    assert dels and max(c["old_words"] for c in dels) >= 40  # the section-7 paragraph
    big = max(dels, key=lambda c: c["old_words"])
    assert big["old_page"] == 2 and big["context_before"] and big["context_after"]
    # nothing else differs in the body
    assert len(res.data["changes"]) == 2
    # footer "version 1" → "version 2" is reported; shifting page numbers are not
    lines = {(f["where"], f["line"]) for f in res.data["furniture"]}
    assert ("removed", "Services Agreement — version 1") in lines
    assert ("added", "Services Agreement — version 2") in lines
    assert len(lines) == 2
    js = json.loads(res.extra_outputs[0].read_text())
    assert js["summary"] == s
    import pdfplumber

    with pdfplumber.open(res.output) as pdf:
        txt = "\n".join(p.extract_text() or "" for p in pdf.pages)
    assert "Compensation is increased to $48,500" in txt and "Deleted" in txt


def test_text_diff_identical(work):
    v1 = work("contract_v1")
    res = compare.text_diff(v1, v1, out=v1.with_name("same.pdf"), quiet=True)
    assert res.ok and res.data["changes"] == [] and res.data["furniture"] == []


# ---------------------------------------------------------------- visual overlay
@pytest.fixture(scope="module")
def drawings_overlay(tmp_path_factory, corpus):
    import shutil

    d = tmp_path_factory.mktemp("ov")
    r0, r1 = d / "drawings_rev0.pdf", d / "drawings_rev1.pdf"
    shutil.copy(corpus["drawings_rev0"], r0)
    shutil.copy(corpus["drawings_rev1"], r1)
    return compare.visual_overlay(r0, r1, quiet=True)


def _hits(boxes, area: Rect) -> bool:
    return any(Rect(*b).intersects(area) for b in boxes)


def test_overlay_finds_expected_changes(drawings_overlay):
    res = drawings_overlay
    assert res.ok, res.line()
    pages = {p["page"]: p["boxes"] for p in res.data["pages"]}
    assert len(pages) == 5
    # E-001: added note 7 (baseline H - (2.1 + 0.35*6) in) and the EF-1 row (H - (6.45 + 0.6) in)
    assert _hits(pages[0], Rect(1.2 * IN, 4.0 * IN, 6 * IN, 4.3 * IN))
    assert _hits(pages[0], Rect(6.3 * IN, 6.85 * IN, 9 * IN, 7.15 * IN))
    # E-101: SPD box at x 17.5–18.7 in, top 4.7–5.5 in
    assert _hits(pages[1], Rect(17.5 * IN, 4.7 * IN, 18.7 * IN, 5.5 * IN))
    # E-201: row 7 (circuit 7) changed: H - (2.45 + 0.28*7) in from the bottom
    y7 = 2.45 * IN + 0.28 * 7 * IN
    assert _hits(pages[2], Rect(2.0 * IN, y7 - 12, 9 * IN, y7 + 4))
    # E-301 and E-501: only the title block (rev table row, date, rev number) changes
    for pi in (3, 4):
        assert pages[pi], "title block date/rev changes are expected on every sheet"
        assert all(b[0] >= TITLE_BLOCK_X for b in pages[pi]), pages[pi]
    # every sheet's title block changed (date / rev): expected, not a false positive
    for pi in range(5):
        assert any(b[0] >= TITLE_BLOCK_X for b in pages[pi])
    assert all(b[0] < TITLE_BLOCK_X for b in pages[1] if b[1] < 6 * IN)  # body change on E-101 is outside it


def test_overlay_pixels_and_clouds(drawings_overlay):
    res = drawings_overlay
    img = np.asarray(engines.get("render").render(res.output, 1, dpi=50)).astype(int)
    s = 50 / 72
    spd = img[int(4.7 * IN * s): int(5.5 * IN * s), int(17.5 * IN * s): int(18.7 * IN * s)]
    blue = (spd[..., 2] > 150) & (spd[..., 0] < 120)
    assert blue.any()  # new-only content drawn blue
    whole = img
    red = (whole[..., 0] > 150) & (whole[..., 1] < 90) & (whole[..., 2] < 90)
    assert red.any()  # old rev "0"/date in the title block drawn red
    with pikepdf.open(res.output) as pdf:
        for k, pg in enumerate(res.data["pages"]):
            clouds = [a for a in pdf.pages[k].get("/Annots", []) if a.Subtype == "/Polygon" and "/BE" in a]
            assert len(clouds) == pg["changes"]
            assert all(isinstance(c.AP.N, pikepdf.Stream) for c in clouds)


def test_overlay_identical_has_no_changes(work):
    v1 = work("contract_v1")
    res = compare.visual_overlay(v1, v1, out=v1.with_name("ov.pdf"), dpi=60, quiet=True)
    assert res.ok and res.data["total_changes"] == 0


def test_overlay_page_count_and_size_mismatch(work, tmp_path):
    a, b = work("letter"), work("contract_v1")  # 1 page letter vs multi-page contract
    res = compare.visual_overlay(a, b, out=tmp_path / "mm.pdf", dpi=50, align="fit", quiet=True)
    assert res.ok, res.line()
    pages = res.data["pages"]
    assert len(pages) >= 2 and pages[1]["old_missing"] and pages[1]["changes"] == 1


def test_cli_compare(work):
    v1, v2 = work("contract_v1"), work("contract_v2")
    assert main(["compare", str(v1), str(v2), "--mode", "text"]) == 0
    assert main(["compare", str(v1), str(v2), "--dpi", "50", "-o", str(v2.with_name("both.pdf"))]) == 0
    assert v2.with_name("both.pdf").exists() and v2.with_name("both.diff.pdf").exists()
