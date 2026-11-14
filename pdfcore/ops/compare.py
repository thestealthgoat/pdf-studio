"""Compare two PDFs: a word-level text diff report and a visual overlay with revision clouds.

Text diff
    Words from every page (:mod:`pdfcore.text`) form one stream per document, so reflow across page breaks
    does not produce false changes. ``difflib`` aligns the streams; each insertion / deletion / change is
    reported with its page and position in both files and a few words of context. Running headers and
    footers (words in a band at the top/bottom of each page) are compared separately as lines with digits
    masked, so a page number shifting is not a change but "version 1" → "version 2" is.

Visual overlay
    Pages are rendered (pypdfium2, default 100 dpi) and composited Bluebeam-style: ink only in the old file
    is red, only in the new file is blue, in both is gray. :func:`find_changes` turns the pixel difference
    into change regions (threshold → cell grid → dilate → connected components → merge nearby → drop specks);
    each region gets a revision-cloud annotation and a numbered delta tag.

Pages are 0-based in returned data and JSON; reports print 1-based page numbers.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path
from pdfcore.result import OpResult
from pdfcore.types import Rect

__all__ = ["find_changes", "composite", "text_diff", "visual_overlay", "compare", "Token"]

OLD_RGB = (215, 30, 30)
NEW_RGB = (25, 75, 230)
SAME_RGB = (95, 95, 95)
CLOUD_RGB = (0.82, 0.06, 0.6)


# ====================================================================== change regions
def _gray(img: Image.Image | np.ndarray) -> np.ndarray:
    if isinstance(img, np.ndarray):
        a = img.astype(np.float32)
        if a.ndim == 3:
            a = a[..., :3] @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        return a
    return np.asarray(img.convert("L"), dtype=np.float32)


def _pad(a: np.ndarray, h: int, w: int) -> np.ndarray:
    if a.shape == (h, w):
        return a
    out = np.full((h, w), 255.0, dtype=np.float32)
    out[: a.shape[0], : a.shape[1]] = a[:h, :w]
    return out


def _dilate(grid: np.ndarray, r: int) -> np.ndarray:
    """Binary dilation with a (2r+1)² square, separable, numpy only."""
    if r <= 0:
        return grid
    out = grid.copy()
    for axis in (0, 1):
        src = out.copy()
        for s in range(1, r + 1):
            if axis == 0:
                out[s:, :] |= src[:-s, :]
                out[:-s, :] |= src[s:, :]
            else:
                out[:, s:] |= src[:, :-s]
                out[:, :-s] |= src[:, s:]
    return out


def _label(grid: np.ndarray) -> tuple[np.ndarray, int]:
    """8-connected component labels via row runs + union-find. Returns (labels, count)."""
    h, w = grid.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent: list[int] = [0]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    prev_runs: list[tuple[int, int, int]] = []
    for y in range(h):
        row = grid[y]
        if not row.any():
            prev_runs = []
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        runs: list[tuple[int, int, int]] = []
        j = 0
        for s, e in zip(starts.tolist(), ends.tolist()):  # run covers [s, e)
            while j < len(prev_runs) and prev_runs[j][1] < s:  # prev run ends left of s-1: no contact
                j += 1
            lab = 0
            k = j
            while k < len(prev_runs) and prev_runs[k][0] <= e:  # 8-connected: touches columns s-1 … e
                root = find(prev_runs[k][2])
                if lab == 0:
                    lab = root
                elif root != lab:
                    lo, hi = min(root, lab), max(root, lab)
                    parent[hi] = lo
                    lab = lo
                k += 1
            if lab == 0:
                lab = len(parent)
                parent.append(lab)
            labels[y, s:e] = lab
            runs.append((s, e, lab))
        prev_runs = runs
    if len(parent) == 1:
        return labels, 0
    roots = np.array([find(i) for i in range(len(parent))], dtype=np.int32)
    uniq, compact = np.unique(roots, return_inverse=True)
    lab = compact.astype(np.int32)  # root 0 → 0 because label 0 is its own root and smallest
    return lab[labels], len(uniq) - 1


def _merge_boxes(boxes: list[list[float]], gap: float) -> list[list[float]]:
    boxes = [b[:] for b in boxes]
    changed = True
    while changed:
        changed = False
        out: list[list[float]] = []
        for b in boxes:
            for o in out:
                if not (b[0] > o[2] + gap or b[2] < o[0] - gap or b[1] > o[3] + gap or b[3] < o[1] - gap):
                    o[0], o[1], o[2], o[3] = min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])
                    changed = True
                    break
            else:
                out.append(b)
        boxes = out
    return boxes


def find_changes(
    img_a: Image.Image | np.ndarray,
    img_b: Image.Image | np.ndarray,
    *,
    dpi: float = 72.0,
    threshold: float = 48.0,
    cell_pt: float = 2.0,
    dilate_pt: float = 4.0,
    merge_pt: float = 14.0,
    min_pixels: int = 4,
    min_size_pt: float = 1.5,
    ignore: Sequence[Rect] | None = None,
) -> list[Rect]:
    """Regions where two page renders differ, as Rects in points (top-left origin; px→pt via *dpi*).

    Images may differ in size (they are aligned top-left, padded white). *threshold*: gray-level difference
    (0–255) that counts as changed. Changed pixels are pooled into *cell_pt* cells, dilated by *dilate_pt*,
    split into connected components, boxes closer than *merge_pt* are merged, and regions with fewer than
    *min_pixels* changed pixels or smaller than *min_size_pt* on both sides are dropped as specks.
    *ignore*: regions (points) whose differences are discarded (e.g. a title block).
    """
    a, b = _gray(img_a), _gray(img_b)
    h, w = max(a.shape[0], b.shape[0]), max(a.shape[1], b.shape[1])
    a, b = _pad(a, h, w), _pad(b, h, w)
    mask = np.abs(a - b) > threshold
    s = dpi / 72.0
    for r in ignore or []:
        mask[max(0, int(r.top * s)): max(0, int(math.ceil(r.bottom * s))), max(0, int(r.x0 * s)): max(0, int(math.ceil(r.x1 * s)))] = False
    if not mask.any():
        return []
    k = max(1, int(round(cell_pt * s)))
    gh, gw = -(-h // k), -(-w // k)
    pm = np.zeros((gh * k, gw * k), dtype=bool)
    pm[:h, :w] = mask
    counts = pm.reshape(gh, k, gw, k).sum(axis=(1, 3))
    grid = counts > 0
    grid = _dilate(grid, max(0, int(math.ceil(dilate_pt / max(cell_pt, 1e-6)))))
    labels, n = _label(grid)
    if n == 0:
        return []
    # per-label pixel counts and cell bounding boxes
    flat = labels.ravel()
    px = np.bincount(flat, weights=counts.ravel(), minlength=n + 1)
    ys, xs = np.nonzero(labels)
    lab = labels[ys, xs]
    y0 = np.full(n + 1, gh, dtype=np.int64)
    x0 = np.full(n + 1, gw, dtype=np.int64)
    y1 = np.zeros(n + 1, dtype=np.int64)
    x1 = np.zeros(n + 1, dtype=np.int64)
    np.minimum.at(y0, lab, ys)
    np.minimum.at(x0, lab, xs)
    np.maximum.at(y1, lab, ys)
    np.maximum.at(x1, lab, xs)
    boxes: list[list[float]] = []
    for i in range(1, n + 1):
        if px[i] < min_pixels:
            continue
        # tighten to the changed pixels inside the component's cell box
        py0, py1, px0, px1 = y0[i] * k, min(h, (y1[i] + 1) * k), x0[i] * k, min(w, (x1[i] + 1) * k)
        sub = mask[py0:py1, px0:px1]
        rows, cols = np.flatnonzero(sub.any(axis=1)), np.flatnonzero(sub.any(axis=0))
        if not len(rows):
            continue
        bx0, by0 = (px0 + cols[0]) / s, (py0 + rows[0]) / s
        bx1, by1 = (px0 + cols[-1] + 1) / s, (py0 + rows[-1] + 1) / s
        boxes.append([bx0, by0, bx1, by1])
    boxes = _merge_boxes(boxes, merge_pt)
    out = [Rect(*bb) for bb in boxes if (bb[2] - bb[0]) >= min_size_pt or (bb[3] - bb[1]) >= min_size_pt]
    return sorted(out, key=lambda r: (round(r.top / 12), r.x0))


def composite(img_old: Image.Image | np.ndarray, img_new: Image.Image | np.ndarray, *,
              old_rgb: tuple[int, int, int] = OLD_RGB, new_rgb: tuple[int, int, int] = NEW_RGB,
              same_rgb: tuple[int, int, int] = SAME_RGB) -> Image.Image:
    """Overlay image: ink only in old → red, only in new → blue, in both → gray. Aligned top-left."""
    a, b = _gray(img_old), _gray(img_new)
    h, w = max(a.shape[0], b.shape[0]), max(a.shape[1], b.shape[1])
    ia, ib = 1 - _pad(a, h, w) / 255.0, 1 - _pad(b, h, w) / 255.0
    both = np.minimum(ia, ib)
    oa, ob = ia - both, ib - both
    out = np.empty((h, w, 3), dtype=np.float32)
    for c in range(3):
        out[..., c] = 255 - both * (255 - same_rgb[c]) - oa * (255 - old_rgb[c]) - ob * (255 - new_rgb[c])
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


# ====================================================================== visual overlay
def _fit(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    sc = min(size[0] / img.width, size[1] / img.height)
    if abs(sc - 1) < 1e-3:
        return img
    return img.resize((max(1, round(img.width * sc)), max(1, round(img.height * sc))), Image.LANCZOS)


def _ink_box(img: Image.Image, dpi: float, threshold: float = 48) -> list[Rect]:
    g = _gray(img)
    m = g < 255 - threshold
    if not m.any():
        return []
    rows, cols = np.flatnonzero(m.any(axis=1)), np.flatnonzero(m.any(axis=0))
    s = dpi / 72
    return [Rect(cols[0] / s, rows[0] / s, (cols[-1] + 1) / s, (rows[-1] + 1) / s)]


def _page_count(path: Path, password: str | None) -> int:
    from pdfcore import engines

    return engines.get("render").page_count(path, password=password)


def visual_overlay(
    old: str | Path,
    new: str | Path,
    *,
    out: str | Path | None = None,
    dpi: float = 100.0,
    align: str = "top-left",
    pages: Sequence[int] | None = None,
    clouds: bool = True,
    tags: bool = True,
    threshold: float = 48.0,
    merge_pt: float = 14.0,
    min_pixels: int = 4,
    ignore: dict[int, list[Rect]] | list[Rect] | None = None,
    password: str | None = None,
    new_password: str | None = None,
    options=None,
    quiet: bool = False,
) -> OpResult:
    """Overlay PDF (red = old only, blue = new only, gray = both) with revision clouds and delta tags.

    *align*: ``top-left`` (pages at native size, aligned at the top-left) or ``fit`` (old page scaled
    uniformly to fit the new page size). *pages*: 0-based page indexes to compare (default: all, paired by
    index; a page missing on one side is reported as a single change covering its content). *ignore*:
    regions in points to exclude from change detection — a list for every page or {page: [Rect…]}.

    data: ``pages`` = [{"page", "changes", "boxes": [[x0, top, x1, bottom], …]}], ``total_changes``, ``dpi``.
    """
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    import pikepdf

    from pdfcore import engines
    from pdfcore.fonts import register_reportlab
    from pdfcore.ops import markup

    register_reportlab()
    old, new = Path(old), Path(new)
    dst = output_path(new, "overlay", out=out)
    render = engines.get("render")
    n_old, n_new = _page_count(old, password), _page_count(new, new_password or password)
    idx = list(pages) if pages is not None else list(range(max(n_old, n_new)))
    s = dpi / 72.0
    per_page: list[dict[str, Any]] = []
    c = canvas.Canvas(str(dst), pageCompression=1, initialFontName="Inter")
    c.setTitle(f"Overlay — {old.name} vs {new.name}")
    c.setAuthor("PDF Studio")
    c.setSubject("Visual comparison overlay")
    c.setCreator("pdfcore compare")
    for i in idx:
        ia = render.render(old, i, dpi=dpi, password=password) if i < n_old else None
        ib = render.render(new, i, dpi=dpi, password=new_password or password) if i < n_new else None
        if ia is None and ib is None:
            continue
        if ia is not None and ib is not None and align == "fit":
            ia = _fit(ia, ib.size)
        ref = ib if ib is not None else ia
        assert ref is not None
        blank = Image.new("RGB", ref.size, "white")
        A, B = ia if ia is not None else blank, ib if ib is not None else blank
        W, H = max(A.width, B.width), max(A.height, B.height)
        ign = ignore.get(i, []) if isinstance(ignore, dict) else (ignore or [])
        if ia is None or ib is None:
            boxes = _ink_box(A if ia is not None else B, dpi)
        else:
            boxes = find_changes(A, B, dpi=dpi, threshold=threshold, merge_pt=merge_pt, min_pixels=min_pixels, ignore=ign)
        comp = composite(A, B)
        pw, ph = W / s, H / s
        c.setPageSize((pw, ph))
        buf = io.BytesIO()
        comp.save(buf, "PNG")
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, pw, ph)
        tag_size = max(12.0, min(26.0, 0.009 * max(pw, ph)))
        if tags:
            for k, r in enumerate(boxes):
                cr, diam = _cloud_geom(r, pw, ph)
                _delta_tag(c, k + 1, cr.expand(diam / 2 + 1), pw, ph, tag_size)
        _legend(c, old.name, new.name, pw, ph, len(boxes), i)
        c.showPage()
        per_page.append({"page": i, "changes": len(boxes), "boxes": [[round(v, 2) for v in (r.x0, r.top, r.x1, r.bottom)] for r in boxes],
                         "old_missing": ia is None, "new_missing": ib is None, "size": [round(pw, 2), round(ph, 2)]})
    c.save()
    if clouds:
        with pikepdf.open(str(dst), allow_overwriting_input=True) as pdf:
            for k, pg in enumerate(per_page):
                pw, ph = pg["size"]
                for j, bb in enumerate(pg["boxes"]):
                    r, diam = _cloud_geom(Rect(*bb), pw, ph)
                    markup.add_cloud(pdf, k, r, color=CLOUD_RGB, width=1.6, scallop=diam, author="pdfcore compare",
                                     subject="Revision cloud", contents=f"Change {j + 1} (page {pg['page'] + 1})")
            pdf.save(str(dst))
    total = sum(p["changes"] for p in per_page)
    data = {"pages": per_page, "total_changes": total, "dpi": dpi, "align": align,
            "pages_changed": [p["page"] for p in per_page if p["changes"]]}
    return finalize(dst, baseline=[old, new], options=options, quiet=quiet, data=data)


def _cloud_geom(box: Rect, pw: float, ph: float) -> tuple[Rect, float]:
    """Cloud outline rect (change box plus padding, kept on the page) and scallop diameter."""
    pad = max(5.0, 0.004 * max(pw, ph))
    diam = max(7.0, min(18.0, 0.12 * min(box.width, box.height) + 0.004 * max(pw, ph) + 4))
    m = diam / 2 + 2
    r = Rect(max(m, box.x0 - pad), max(m, box.top - pad), min(pw - m, box.x1 + pad), min(ph - m, box.bottom + pad))
    return r, diam


def _delta_tag(c, n: int, r: Rect, pw: float, ph: float, size: float) -> None:
    """Numbered triangle just outside the top-right corner of a change region (drawing space y-up)."""
    x = min(pw - size - 2, r.x1 + 2)
    top = max(2.0, r.top - size - 2)
    y = ph - top - size
    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.setStrokeColorRGB(*CLOUD_RGB)
    c.setLineWidth(max(1.0, size / 14))
    p = c.beginPath()
    p.moveTo(x, y)
    p.lineTo(x + size, y)
    p.lineTo(x + size / 2, y + size * 0.9)
    p.close()
    c.drawPath(p, stroke=1, fill=1)
    c.setFillColorRGB(*CLOUD_RGB)
    label = str(n)
    fs = size * (0.42 if len(label) < 3 else 0.32)
    c.setFont("Inter-Bold", fs)
    c.drawCentredString(x + size / 2, y + size * 0.16, label)
    c.restoreState()


def _legend(c, old_name: str, new_name: str, pw: float, ph: float, n: int, page: int) -> None:
    fs = max(6.5, min(10.0, 0.0045 * max(pw, ph)))
    lines = [(OLD_RGB, f"Only in old: {old_name}"), (NEW_RGB, f"Only in new: {new_name}"),
             (SAME_RGB, "Unchanged"), (tuple(int(v * 255) for v in CLOUD_RGB), f"{n} change region(s) · page {page + 1}")]
    from reportlab.pdfbase.pdfmetrics import stringWidth

    tw = max(stringWidth(t, "Inter", fs) for _, t in lines)
    bw, bh = tw + fs * 3.2, fs * 1.45 * len(lines) + fs
    x, y = fs, fs
    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.setFillAlpha(0.88)
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.setLineWidth(0.5)
    c.rect(x, y, bw, bh, stroke=1, fill=1)
    c.setFillAlpha(1)
    for k, (rgb, t) in enumerate(lines):
        yy = y + bh - fs * 1.45 * (k + 1)
        c.setFillColorRGB(*(v / 255 for v in rgb))
        c.rect(x + fs * 0.6, yy, fs, fs * 0.8, stroke=0, fill=1)
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.setFont("Inter", fs)
        c.drawString(x + fs * 2.1, yy + fs * 0.05, t)
    c.restoreState()


# ====================================================================== text diff
@dataclass(slots=True)
class Token:
    text: str
    norm: str
    page: int
    rect: Rect


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", t)
    return t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-")


def _tokens(path: Path, band_pt: float, password: str | None) -> tuple[list[Token], list[tuple[int, str]]]:
    """Body word tokens for the whole document + header/footer lines (page, text) from the margin bands."""
    from pdfcore import engines, text

    eng = engines.get("text")
    body: list[Token] = []
    furniture: list[tuple[int, str]] = []
    n = _page_count(path, password)
    for pi in range(n):
        _w, H = eng.page_size(path, pi, password=password)
        ws = text.words(path, pi, password=password)
        band_words = [w for w in ws if band_pt > 0 and (w.rect.bottom > H - band_pt or w.rect.top < band_pt)]
        ids = {id(w) for w in band_words}
        for w in ws:
            if id(w) not in ids:
                body.append(Token(w.text, _norm(w.text), pi, w.rect))
        for ln in text.group_lines(band_words):
            furniture.append((pi, ln.text))
    return body, furniture


def _line_rects(toks: Sequence[Token]) -> list[dict]:
    """Group tokens into per-page, per-line rects."""
    out: list[dict] = []
    for t in toks:
        if out and out[-1]["page"] == t.page:
            r = out[-1]["rect"]
            if abs(r.top - t.rect.top) < 0.5 * max(t.rect.height, 1) and t.rect.x0 >= r.x0 - 1:
                out[-1]["rect"] = Rect.union([r, t.rect])
                continue
        out.append({"page": t.page, "rect": t.rect})
    return [{"page": d["page"], "rect": [round(v, 2) for v in (d["rect"].x0, d["rect"].top, d["rect"].x1, d["rect"].bottom)]} for d in out]


def _ctx(toks: Sequence[Token], i: int, j: int, n: int = 8) -> tuple[str, str]:
    before = " ".join(t.text for t in toks[max(0, i - n): i])
    after = " ".join(t.text for t in toks[j: j + n])
    return before, after


def _diff(a: list[Token], b: list[Token], join_gap: int = 2) -> list[dict]:
    sm = difflib.SequenceMatcher(None, [t.norm for t in a], [t.norm for t in b], autojunk=False)
    ops = [list(o) for o in sm.get_opcodes()]
    # merge edits separated by tiny equal runs (≤ join_gap words) into one change
    merged: list[list] = []
    for o in ops:
        if o[0] == "equal":
            merged.append(o)
            continue
        if len(merged) >= 2 and merged[-1][0] == "equal" and merged[-2][0] != "equal" and (merged[-1][2] - merged[-1][1]) <= join_gap:
            eq = merged.pop()
            prev = merged.pop()
            merged.append(["replace", prev[1], o[2], prev[3], o[4]])
            del eq
        else:
            merged.append(o)
    changes: list[dict] = []
    for tag, i1, i2, j1, j2 in merged:
        if tag == "equal":
            continue
        kind = {"insert": "insert", "delete": "delete", "replace": "change"}[tag]
        old_t, new_t = a[i1:i2], b[j1:j2]
        # page anchors even for pure inserts/deletes (where the change sits in the other file)
        old_page = old_t[0].page if old_t else (a[i1 - 1].page if i1 > 0 else (a[0].page if a else 0))
        new_page = new_t[0].page if new_t else (b[j1 - 1].page if j1 > 0 else (b[0].page if b else 0))
        ctx_src, ci, cj = (b, j1, j2) if kind != "delete" else (a, i1, i2)
        before, after = _ctx(ctx_src, ci, cj)
        changes.append({
            "kind": kind, "old_text": " ".join(t.text for t in old_t), "new_text": " ".join(t.text for t in new_t),
            "old_page": old_page, "new_page": new_page, "old_words": len(old_t), "new_words": len(new_t),
            "old_rects": _line_rects(old_t), "new_rects": _line_rects(new_t),
            "context_before": before, "context_after": after,
        })
    return changes


def _furniture_diff(fa: list[tuple[int, str]], fb: list[tuple[int, str]]) -> list[dict]:
    """Header/footer lines only in one document. Numbers are masked only in lines whose numbers vary from page
    to page within a document (page counters), so "Page 3 of 9" shifting is ignored but "version 1" → "version 2"
    is reported."""

    def key(s: str) -> str:
        return re.sub(r"\d+", "#", _norm(s)).strip()

    def canon(lines: list[tuple[int, str]]) -> dict[str, str]:
        variants: dict[str, set[str]] = {}
        for _, t in lines:
            variants.setdefault(key(t), set()).add(_norm(t).strip())
        out: dict[str, str] = {}
        for _, t in lines:
            k = key(t)
            out.setdefault(k if len(variants[k]) > 1 else _norm(t).strip(), t)
        return out

    ca, cb = canon(fa), canon(fb)
    # a canonical form that is a counter template in one file may be a constant line in the other
    def present(form: str, other: dict[str, str]) -> bool:
        return form in other or key(form) in other or any(key(o) == form for o in other)

    out: list[dict] = []
    for form, line in ca.items():
        if not present(form, cb):
            out.append({"where": "removed", "line": line})
    for form, line in cb.items():
        if not present(form, ca):
            out.append({"where": "added", "line": line})
    return out


def text_diff(
    old: str | Path,
    new: str | Path,
    *,
    out: str | Path | None = None,
    json_out: str | Path | None = None,
    band_in: float = 0.6,
    password: str | None = None,
    new_password: str | None = None,
    options=None,
    quiet: bool = False,
) -> OpResult:
    """Word-level diff report (PDF + JSON). *band_in*: header/footer band height in inches (0 = none).

    data: ``summary`` (insertions, deletions, changes, words_added, words_removed, pages_changed_old/new,
    furniture_changes), ``changes`` [{kind, old_text, new_text, old_page, new_page, old_rects, new_rects,
    context_before, context_after}], ``furniture``.
    """
    old, new = Path(old), Path(new)
    band = band_in * 72
    a, fa = _tokens(old, band, password)
    b, fb = _tokens(new, band, new_password or password)
    changes = _diff(a, b)
    furn = _furniture_diff(fa, fb)
    summary = {
        "insertions": sum(c["kind"] == "insert" for c in changes),
        "deletions": sum(c["kind"] == "delete" for c in changes),
        "changes": sum(c["kind"] == "change" for c in changes),
        "words_added": sum(c["new_words"] for c in changes),
        "words_removed": sum(c["old_words"] for c in changes),
        "words_old": len(a), "words_new": len(b),
        "pages_changed_old": sorted({c["old_page"] for c in changes}),
        "pages_changed_new": sorted({c["new_page"] for c in changes}),
        "furniture_changes": len(furn),
    }
    data = {"old": str(old), "new": str(new), "summary": summary, "changes": changes, "furniture": furn,
            "pages_are": "0-based"}
    dst = output_path(new, "diff", out=out)
    jpath = Path(json_out) if json_out is not None else dst.with_suffix(".json")
    jpath.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    _diff_report(dst, old, new, summary, changes, furn)
    from pdfcore.gate import GateOptions

    opts = options or GateOptions(profile="typeset", margins_in=0.5)
    return finalize(dst, baseline=None, options=opts, extra=[jpath], quiet=quiet, data=data)


def _diff_report(dst: Path, old: Path, new: Path, summary: dict, changes: list[dict], furn: list[dict]) -> None:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from pdfcore.fonts import register_reportlab

    register_reportlab()
    base = ParagraphStyle("b", fontName="Inter", fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=10.5, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=base, fontName="Inter-Bold", fontSize=18, leading=22, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=base, fontName="Inter-Bold", fontSize=12.5, leading=16, spaceBefore=12,
                        spaceAfter=4, keepWithNext=1)
    red, blue = "#c81e1e", "#1a4bd6"

    def cut(s: str, n: int = 600) -> str:
        return s if len(s) <= n else s[:n] + " …"

    story: list[Any] = [
        Paragraph("Text comparison", h1),
        Paragraph(escape(f"Old: {old.name}    New: {new.name}    Generated {datetime.now():%Y-%m-%d %H:%M}"), small),
        Spacer(1, 8),
    ]
    rows = [["Insertions", str(summary["insertions"])], ["Deletions", str(summary["deletions"])],
            ["Changes", str(summary["changes"])], ["Words added / removed", f"{summary['words_added']} / {summary['words_removed']}"],
            ["Pages with changes (new)", ", ".join(str(p + 1) for p in summary["pages_changed_new"]) or "none"],
            ["Header/footer differences", str(summary["furniture_changes"])]]
    t = Table([[Paragraph(escape(k), base), Paragraph(escape(v), base)] for k, v in rows], colWidths=[2.4 * inch, 4.6 * inch])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    story.append(Paragraph("Changes", h2))
    if not changes:
        story.append(Paragraph("No text differences in the body of the documents.", base))
    for n, ch in enumerate(changes, 1):
        label = {"insert": "Inserted", "delete": "Deleted", "change": "Changed"}[ch["kind"]]
        where = f"old p.{ch['old_page'] + 1} → new p.{ch['new_page'] + 1}"
        head = Paragraph(f"<font name='Inter-Bold'>{n}. {label}</font> <font color='#666666'>· {where}</font>", base)
        mid = ""
        if ch["old_text"]:
            mid += f"<font color='{red}'><strike>{escape(cut(ch['old_text']))}</strike></font> "
        if ch["new_text"]:
            mid += f"<font color='{blue}'><u>{escape(cut(ch['new_text']))}</u></font>"
        body = Paragraph(f"<font color='#666666'>… {escape(ch['context_before'])}</font> {mid} "
                         f"<font color='#666666'>{escape(ch['context_after'])} …</font>", base)
        story.append(KeepTogether([head, body, Spacer(1, 6)]))
    if furn:
        story.append(Paragraph("Header / footer differences", h2))
        for f in furn:
            story.append(Paragraph(escape(f"{f['where'].capitalize()}: “{f['line']}”"), base))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"Legend: <font color='{red}'><strike>deleted</strike></font> · "
                           f"<font color='{blue}'><u>inserted</u></font>. Running headers/footers are compared as lines "
                           "with page numbers masked.", small))

    def footer(c, doc):
        c.saveState()
        c.setFont("Inter", 8)
        c.setFillColor(colors.HexColor("#666666"))
        c.drawString(0.75 * inch, 0.45 * inch, f"Text comparison — {old.name} vs {new.name}")
        c.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"Page {doc.page}")
        c.restoreState()

    doc = SimpleDocTemplate(str(dst), pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.75 * inch, bottomMargin=0.75 * inch, title=f"Text comparison — {old.name} vs {new.name}",
                            author="PDF Studio", subject="Text comparison report", creator="pdfcore compare", lang="en-US")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


# ====================================================================== combined
def compare(old: str | Path, new: str | Path, *, mode: str = "both", out: str | Path | None = None,
            dpi: float = 100.0, align: str = "top-left", clouds: bool = True, password: str | None = None,
            options=None, quiet: bool = False) -> OpResult:
    """Run the text diff, the visual overlay, or both. With both, ``output`` is the overlay, the text report
    and its JSON are in ``extra_outputs``, and data has keys ``text`` and ``visual``. *out* may be a directory."""
    if mode == "text":
        return text_diff(old, new, out=out, password=password, options=options, quiet=quiet)
    if mode == "visual":
        return visual_overlay(old, new, out=out, dpi=dpi, align=align, clouds=clouds, password=password,
                              options=options, quiet=quiet)
    if mode != "both":
        raise ValueError(f"mode must be text, visual or both, not {mode!r}")
    o = Path(out) if out is not None else None
    t_out = o if o is not None and o.is_dir() else (o.with_name(o.stem + ".diff.pdf") if o is not None else None)
    tj_out = None if t_out is None or t_out.is_dir() else t_out.with_suffix(".json")
    tr = text_diff(old, new, out=t_out, json_out=tj_out, password=password, quiet=quiet)
    vr = visual_overlay(old, new, out=out, dpi=dpi, align=align, clouds=clouds, password=password, options=options,
                        quiet=quiet)
    gate = tr.gate if (vr.ok and not tr.ok) else vr.gate
    return OpResult(vr.output, gate, [*( [tr.output] if tr.output else []), *tr.extra_outputs, *vr.extra_outputs],
                    {"text": tr.data, "visual": vr.data})


@command("compare", "Compare two PDFs: text diff report and/or visual overlay with revision clouds", group="markup")
def _cli(p: argparse.ArgumentParser):
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--mode", choices=["both", "text", "visual"], default="both")
    p.add_argument("--dpi", type=float, default=100.0, help="overlay render resolution (default 100)")
    p.add_argument("--align", choices=["top-left", "fit"], default="top-left")
    p.add_argument("--no-clouds", action="store_true")
    add_common(p)

    def run(ns):
        res = compare(ns.old, ns.new, mode=ns.mode, out=ns.out, dpi=ns.dpi, align=ns.align, clouds=not ns.no_clouds,
                      password=ns.password, options=gate_options_from(ns) if ns.mode == "visual" else None)
        d = res.data
        t = d.get("text", d if ns.mode == "text" else None)
        v = d.get("visual", d if ns.mode == "visual" else None)
        if t and "summary" in t:
            s = t["summary"]
            print(f"text: {s['insertions']} inserted, {s['deletions']} deleted, {s['changes']} changed "
                  f"(+{s['words_added']}/-{s['words_removed']} words)")
        if v and "pages" in v:
            print("visual: " + ", ".join(f"p.{pg['page'] + 1}: {pg['changes']}" for pg in v["pages"]))
        return res

    return run
