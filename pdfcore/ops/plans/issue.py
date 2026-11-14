"""Issuing drawing sets: revision clouds with delta tags, issue stamps, seal placement, revision-table rows.

``cloud``   revision clouds (markup cloud annotations, or burned into the page with ``flatten``) around
            rectangles on a sheet, each with a delta triangle carrying the revision number.
``issue``   issue stamp ("ISSUED FOR CONSTRUCTION", "ISSUED FOR BID", "NOT FOR CONSTRUCTION",
            "PRELIMINARY", custom) with date, sized to the sheet, placed in the title block's empty space
            (or a corner / exact position) on every or selected sheets; optional engineer's seal placement
            from the user's own image/PDF/SVG with signature and date lines; optional new revision-table row.

Seals are only ever *placed* from a file the user supplies — this module never draws or invents a seal.

Coordinates are points, top-left origin of the page as displayed (as :mod:`pdfcore.ops.markup`).

CLI::

    pdfcore plans cloud set.pdf --sheet E-101 --rect 17,4.5,19,6 --units in --delta 1 [--flatten]
    pdfcore plans issue set.pdf --stamp "ISSUED FOR CONSTRUCTION" --date 2026-11-13 [--seal seal.png]
                                [--sheets E-101,E-201] [--rev 1 --rev-desc "ISSUED FOR CONSTRUCTION"]
"""

from __future__ import annotations

import argparse
import io
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any, Sequence

import pikepdf
from pikepdf import Array, Dictionary, Name

from pdfcore.ops._common import finalize, gate_options_from, output_path, parse_pages
from pdfcore.ops.plans import sub
from pdfcore.result import OpResult
from pdfcore.types import Rect

__all__ = [
    "cloud", "issue", "ISSUE_STAMPS", "find_title_block", "find_empty_rect", "find_revision_table",
    "add_delta", "burn_annotation",
]

INCH = 72.0
ISSUE_STAMPS: dict[str, str] = {
    "ISSUED FOR CONSTRUCTION": "#5B2C83",
    "ISSUED FOR BID": "#1F5FA8",
    "ISSUED FOR PERMIT": "#1F5FA8",
    "ISSUED FOR REVIEW": "#1F5FA8",
    "NOT FOR CONSTRUCTION": "#C62828",
    "PRELIMINARY": "#B25E00",
    "RECORD DRAWING": "#1B7F3B",
    "AS-BUILT": "#1B7F3B",
    "VOID": "#C62828",
}


# ============================================================== geometry helpers
def _hex(c: str) -> tuple[float, float, float]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _open(src: Path, password: str | None) -> pikepdf.Pdf:
    return pikepdf.open(str(src), password=password or "")


def _select_pages(src: Path, n: int, *, sheets: str | None, pages: str | None, password: str | None) -> list[int]:
    if sheets:
        from pdfcore.ops.plans.cad import find_sheet_pages

        return find_sheet_pages(src, [s for s in re.split(r"[,\s]+", sheets) if s], password)
    return parse_pages(pages, n)


def burn_annotation(pdf: pikepdf.Pdf, page: pikepdf.Page, annot: Dictionary) -> None:
    """Paint *annot*'s normal appearance into the page content and remove the annotation."""
    from pdfcore.ops.markup import flatten_matrix

    form = annot.AP.N
    bbox = [float(v) for v in form.get("/BBox")]
    mtx = [float(v) for v in form.get("/Matrix", [1, 0, 0, 1, 0, 0])]
    A = flatten_matrix([float(v) for v in annot.Rect], bbox, mtx)
    res = page.obj.get("/Resources")
    if res is None:
        page.obj.Resources = Dictionary()
        res = page.obj.Resources
    if "/XObject" not in res:
        res.XObject = Dictionary()
    n = 0
    while Name(f"/PdfStudioIssue{n}") in res.XObject:
        n += 1
    name = Name(f"/PdfStudioIssue{n}")
    res.XObject[name] = form
    gs = ""
    ca = float(annot.get("/CA", 1.0))
    if ca < 0.999:
        if "/ExtGState" not in res:
            res.ExtGState = Dictionary()
        g = Name(f"/PdfStudioIssueGS{n}")
        res.ExtGState[g] = Dictionary(Type=Name.ExtGState, CA=ca, ca=ca)
        gs = f"{g} gs "
    page.contents_coalesce()
    data = page.obj.Contents.read_bytes()
    op = f"q {gs}{' '.join(f'{v:.5g}' for v in A)} cm {name} Do Q\n".encode()
    page.obj.Contents = pdf.make_stream(b"q\n" + data + b"\nQ\n" + op)
    annots = page.obj.get("/Annots")
    if annots is not None:
        keep = Array([a for a in annots if not (a.is_indirect and annot.is_indirect and a.objgen == annot.objgen)])
        if len(keep):
            page.obj.Annots = pdf.make_indirect(keep)
        else:
            del page.obj["/Annots"]


def _custom_annot(pdf: pikepdf.Pdf, pg: pikepdf.Page, r: Rect, draw, *, subtype: str, color: tuple[float, float, float],
                  subject: str, contents: str, author: str | None, opacity: float = 1.0, extra_ops: list[str] | None = None,
                  xobjects: dict[str, Any] | None = None, polygon: Sequence[tuple[float, float]] | None = None) -> Dictionary:
    """Annotation at visual Rect *r* whose appearance is drawn by *draw(c)* on a ReportLab canvas of r's size."""
    from pdfcore.ops.markup import PageGeom, _AP, _attach, _common, rl_form

    g = PageGeom(pg)
    x, y, w, h = g.drect(r)
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    if extra_ops:
        ap.add(*extra_ops)
    if xobjects:
        for k, v in xobjects.items():
            ap.xo[k] = v
    if draw is not None:
        ap.form(rl_form(pdf, w, h, draw), x, y)
    bbox = g.user_bbox([(x, y), (x + w, y + h)])
    d = _common(pdf, pg, subtype, bbox, color=color, opacity=opacity, author=author, subject=subject, contents=contents,
                created=None, modified=None)
    if subtype == "Stamp":
        d.Name = Name("/" + re.sub(r"[^A-Za-z0-9]", "", subject.title()) or "Stamp")
    if polygon is not None:
        verts: list[float] = []
        for px, py in polygon:
            verts.extend(g.user_pt(px, py))
        d.Vertices = Array([float(v) for v in verts])
    return _attach(pdf, pg, d, ap.build(bbox))


# ============================================================== page analysis
@dataclass
class _PageInfo:
    W: float
    H: float
    words: list[dict]
    vlines: list[tuple[float, float, float]]  # x, top, bottom
    hlines: list[tuple[float, float, float]]  # y, x0, x1
    border: Rect


def _analyze(src: Path, pi: int, password: str | None) -> _PageInfo:
    import pdfplumber

    with pdfplumber.open(str(src), password=password or "") as pdf:
        p = pdf.pages[pi]
        W, H = float(p.width), float(p.height)
        words = p.extract_words(extra_attrs=["size"], keep_blank_chars=False)
        v: list[tuple[float, float, float]] = []
        hz: list[tuple[float, float, float]] = []
        rects: list[Rect] = []
        for o in p.lines + p.rects + p.curves:
            x0, x1, t, b = float(o["x0"]), float(o["x1"]), float(o["top"]), float(o["bottom"])
            if o["object_type"] == "rect":
                rects.append(Rect(x0, t, x1, b))
                v += [(x0, t, b), (x1, t, b)]
                hz += [(t, x0, x1), (b, x0, x1)]
                continue
            pts = o.get("pts") or []
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                if abs(ax - bx) < 0.8 and abs(ay - by) > 2:
                    v.append(((ax + bx) / 2, min(ay, by), max(ay, by)))
                elif abs(ay - by) < 0.8 and abs(ax - bx) > 2:
                    hz.append(((ay + by) / 2, min(ax, bx), max(ax, bx)))
    big = [r for r in rects if r.width > 0.8 * W and r.height > 0.8 * H]
    if big:
        border = min(big, key=lambda r: r.area)
    else:
        # border drawn as four lines?
        lefts = [x for x, t, b in v if x < 0.1 * W and b - t > 0.8 * H]
        rights = [x for x, t, b in v if x > 0.9 * W and b - t > 0.8 * H]
        tops = [y for y, a, b in hz if y < 0.1 * H and b - a > 0.8 * W]
        bots = [y for y, a, b in hz if y > 0.9 * H and b - a > 0.8 * W]
        if lefts and rights and tops and bots:
            border = Rect(max(lefts), max(tops), min(rights), min(bots))
        else:
            border = Rect(0.5 * INCH, 0.5 * INCH, W - 0.5 * INCH, H - 0.5 * INCH)
    return _PageInfo(W, H, words, v, hz, border)


def find_title_block(info: _PageInfo) -> Rect | None:
    """Title block: a right-hand column (full-height divider in the right 45% of the sheet) or a bottom strip."""
    b = info.border
    cands = [x for x, t, bt in info.vlines
             if b.x0 + 0.55 * b.width < x < b.x1 - 0.6 * INCH and bt - t > 0.85 * b.height]
    if cands:
        return Rect(min(cands), b.top, b.x1, b.bottom)
    hc = [y for y, x0, x1 in info.hlines if b.top + 0.6 * b.height < y < b.bottom - 0.6 * INCH and x1 - x0 > 0.85 * b.width]
    if hc:
        return Rect(b.x0, min(hc), b.x1, b.bottom)
    return None


class _InkMap:
    """Rendered ink mask with an integral image, for finding empty space on a sheet."""

    def __init__(self, src: Path, pi: int, W: float, H: float, password: str | None, dpi: float = 36.0):
        import numpy as np

        from pdfcore import engines

        img = engines.get("render").render(src, pi, dpi=dpi, password=password).convert("L")
        a = (np.asarray(img) < 215).astype(np.int32)
        self.k = img.width / W
        self.ky = img.height / H
        self.ii = np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        self.w, self.h = img.width, img.height
        self.taken: list[Rect] = []

    def ink(self, r: Rect) -> int:
        x0 = max(0, int(math.floor(r.x0 * self.k)))
        x1 = min(self.w, int(math.ceil(r.x1 * self.k)))
        y0 = max(0, int(math.floor(r.top * self.ky)))
        y1 = min(self.h, int(math.ceil(r.bottom * self.ky)))
        if x1 <= x0 or y1 <= y0:
            return 0
        ii = self.ii
        return int(ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0])

    def free(self, r: Rect, pad: float) -> bool:
        rr = r.expand(pad)
        if any(rr.intersects(t) for t in self.taken):
            return False
        return self.ink(rr) == 0


def find_empty_rect(ink: _InkMap, region: Rect, w: float, h: float, *, pad: float = 6.0, prefer: str = "top",
                    step: float = 4.0) -> Rect | None:
    """First w×h rectangle inside *region* with no ink (and clear of already placed items).

    *prefer*: top (scan top→bottom, centred first) | bottom | bottom-right | top-right.
    """
    if w + 2 * pad > region.width or h + 2 * pad > region.height:
        return None
    ys = [region.top + pad + i * step for i in range(int((region.height - h - 2 * pad) // step) + 1)]
    xs_all = [region.x0 + pad + i * step for i in range(int((region.width - w - 2 * pad) // step) + 1)]
    cx = region.x0 + (region.width - w) / 2
    if prefer.startswith("bottom"):
        ys = ys[::-1]
    if prefer.endswith("right"):
        xs = xs_all[::-1]
    else:
        xs = sorted(xs_all, key=lambda x: abs(x - cx))
    for y in ys:
        for x in xs:
            r = Rect(x, y, x + w, y + h)
            if ink.free(r, pad):
                return r
    return None


# ============================================================== clouds + delta tags
def _delta_side(W: float, H: float) -> float:
    return max(16.0, min(36.0, min(W, H) * 0.0145))


def add_delta(pdf: pikepdf.Pdf, page: int | pikepdf.Page, at: tuple[float, float], rev: str, *, side: float = 22.0,
              color: Any = "red", author: str | None = None) -> Dictionary:
    """Delta (revision triangle) annotation centred at *at* (visual top-left coords) with the rev number inside."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    from pdfcore.ops.markup import parse_color

    pg = pdf.pages[page] if isinstance(page, int) else page
    col = parse_color(color) or (0.86, 0.1, 0.1)
    s = side
    h = s * math.sqrt(3) / 2
    cx, cy = at
    r = Rect(cx - s / 2 - 2, cy - h * 2 / 3 - 2, cx + s / 2 + 2, cy + h / 3 + 2)
    lw = max(1.0, s / 18)

    def draw(c):
        c.setStrokeColorRGB(*col)
        c.setLineWidth(lw)
        c.setLineJoin(1)
        p = c.beginPath()
        p.moveTo(2, 2)
        p.lineTo(2 + s, 2)
        p.lineTo(2 + s / 2, 2 + h)
        p.close()
        c.drawPath(p, stroke=1, fill=0)
        fs = s * 0.46
        while stringWidth(rev, "Inter-Bold", fs) > s * 0.55 and fs > 5:
            fs -= 0.5
        c.setFillColorRGB(*col)
        c.setFont("Inter-Bold", fs)
        c.drawCentredString(2 + s / 2, 2 + h * 0.3 - fs * 0.05, rev)

    tri = [(cx - s / 2, cy + h / 3), (cx + s / 2, cy + h / 3), (cx, cy - h * 2 / 3)]
    return _custom_annot(pdf, pg, r, draw, subtype="Polygon", color=col, subject="Revision Delta",
                         contents=f"Revision {rev}", author=author, polygon=tri)


def cloud(
    src: str | Path,
    rects: Sequence[Rect | Sequence[float]],
    *,
    rev: str | int | None = None,
    sheet: str | None = None,
    pages: str | None = None,
    flatten: bool = False,
    color: Any = "red",
    width: float | None = None,
    delta_at: Sequence[float] | None = None,
    author: str | None = None,
    out: str | Path | None = None,
    password: str | None = None,
    options=None,
    quiet: bool = False,
) -> OpResult:
    """Revision clouds around *rects* (x0, top, x1, bottom in points) on the sheet(s), each with a delta tag.

    Clouds are markup cloud annotations (/BE /S /C) unless *flatten*, which burns cloud and delta into the page.
    The delta sits at the cloud's top-right corner (or *delta_at*, its centre).
    """
    from pdfcore.ops.markup import add_cloud

    src = Path(src)
    dst = output_path(src, "clouded", out=out)
    rs = [r if isinstance(r, Rect) else Rect(*(float(v) for v in r)) for r in rects]
    if not rs:
        raise ValueError("give at least one rectangle")
    placed: list[dict[str, Any]] = []
    with _open(src, password) as pdf:
        idx = _select_pages(src, len(pdf.pages), sheets=sheet, pages=pages, password=password) if (sheet or pages) else [0]
        for pi in idx:
            pg = pdf.pages[pi]
            from pdfcore.overlay import visual_size

            W, H = visual_size(pg)
            s = _delta_side(W, H)
            lw = width or max(1.5, min(W, H) / 900)
            for r in rs:
                if r.x1 > W + 1 or r.bottom > H + 1 or r.x0 < -1 or r.top < -1:
                    raise ValueError(f"rectangle {r} is outside page {pi + 1} ({W:.0f}×{H:.0f} pt)")
                a = add_cloud(pdf, pg, r, color=color, width=lw, author=author, subject="Revision Cloud",
                              contents=f"Revision {rev}" if rev is not None else "Revision", scallop=max(10.0, s * 0.62))
                d = None
                if rev is not None:
                    if delta_at is not None:
                        at = (float(delta_at[0]), float(delta_at[1]))
                    else:
                        at = (r.x1 + s * 0.45, r.top - s * 0.25)
                        if at[0] + s / 2 > W - 4:
                            at = (r.x0 - s * 0.45, r.top - s * 0.25)
                        if at[1] - s * 0.6 < 4:
                            at = (at[0], r.bottom + s * 0.6)
                    d = add_delta(pdf, pg, at, str(rev), side=s, color=color, author=author)
                if flatten:
                    burn_annotation(pdf, pg, a)
                    if d is not None:
                        burn_annotation(pdf, pg, d)
                placed.append({"page": pi + 1, "rect": [r.x0, r.top, r.x1, r.bottom], "rev": rev})
        pdf.save(str(dst))
    return finalize(dst, baseline=src, options=options or gate_options_from(profile="drawing"), quiet=quiet,
                    data={"clouds": placed, "flattened": flatten})


# ============================================================== stamps, seals, revision rows
def _stamp_draw(label: str, lines: list[str], col: tuple[float, float, float], fs: float, w: float, h: float, pad: float):
    from reportlab.pdfbase.pdfmetrics import stringWidth

    def draw(c):
        lw = max(1.2, fs / 9)
        c.setStrokeColorRGB(*col)
        c.setFillColorRGB(*col)
        c.setLineWidth(lw)
        rad = min(6.0, h / 6)
        c.roundRect(lw / 2, lw / 2, w - lw, h - lw, rad, stroke=1, fill=0)
        ins = lw * 2.4
        c.setLineWidth(max(0.5, lw / 3))
        c.roundRect(ins, ins, w - 2 * ins, h - 2 * ins, max(0.5, rad - ins / 2), stroke=1, fill=0)
        c.setFont("Inter-Bold", fs)
        y = h - pad - fs * 0.78
        c.drawCentredString(w / 2, y, label)
        if lines:
            sub = max(7.0, fs * 0.42)
            y -= fs * 0.42
            c.setLineWidth(max(0.5, lw / 2.5))
            c.line(ins + 6, y, w - ins - 6, y)
            y -= sub * 1.25
            c.setFont("Inter-SemiBold", sub)
            for ln in lines:
                ss = sub
                while stringWidth(ln, "Inter-SemiBold", ss) > w - 2 * ins - 8 and ss > 7:
                    ss -= 0.25
                c.setFont("Inter-SemiBold", ss)
                c.drawCentredString(w / 2, y, ln)
                y -= sub * 1.3

    return draw


def _stamp_size(label: str, lines: list[str], target_w: float, max_fs: float) -> tuple[float, float, float, float]:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    from pdfcore.fonts import register_reportlab

    register_reportlab()
    unit = stringWidth(label, "Inter-Bold", 1.0)
    fs = min(max_fs, max(9.0, (target_w - 14) / max(unit + 0.9, 1e-6)))
    pad = max(7.0, fs * 0.45)
    w = unit * fs + 2 * pad + 14
    sub = max(7.0, fs * 0.42)
    h = pad * 2 + fs * 0.95 + (fs * 0.42 + sub * 1.3 * len(lines) + sub * 0.2 if lines else 0)
    return fs, w, h, pad


def _load_seal_form(pdf: pikepdf.Pdf, path: Path) -> tuple[Any, float, float, str]:
    """Return (object, natural_w, natural_h, kind) for a seal file: raster path, or a PDF/SVG form XObject."""
    ext = path.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp"):
        from PIL import Image

        with Image.open(path) as im:
            return path, float(im.width), float(im.height), "raster"
    if ext == ".svg":
        import typst

        with tempfile.TemporaryDirectory(prefix="pdfcore-seal-") as tmp:
            shutil.copy(path, Path(tmp) / "seal.svg")
            (Path(tmp) / "main.typ").write_text('#set page(width: auto, height: auto, margin: 0pt)\n#image("seal.svg", width: 3in)\n', encoding="utf-8")
            data = typst.compile(str(Path(tmp) / "main.typ"), root=tmp)
        return _pdf_form(pdf, data)
    if ext == ".pdf":
        return _pdf_form(pdf, path.read_bytes())
    raise ValueError(f"seal must be PNG/JPG/TIFF, PDF or SVG (got {path.name})")


def _pdf_form(pdf: pikepdf.Pdf, data: bytes) -> tuple[Any, float, float, str]:
    from pdfcore.ops.markup import _materialize

    with pikepdf.open(io.BytesIO(data)) as s:
        pg = s.pages[0]
        box = [float(v) for v in (pg.obj.get("/CropBox") or pg.mediabox)]
        form = pdf.copy_foreign(pg.as_form_xobject())
        _materialize(form, set())
    return form, abs(box[2] - box[0]), abs(box[3] - box[1]), "form"


def _seal_annot(pdf: pikepdf.Pdf, pg: pikepdf.Page, r: Rect, seal: tuple[Any, float, float, str], seal_side: float,
                signature: bool, signer: str | None, date_text: str | None, author: str | None) -> Dictionary:
    from pdfcore.ops.markup import PageGeom

    obj, nw, nh, kind = seal
    f = seal_side / max(nw, nh)
    sw, sh = nw * f, nh * f
    w, h = r.width, r.height
    sig_h = h - seal_side

    def draw(c):
        if kind == "raster":
            c.drawImage(str(obj), (w - sw) / 2, h - seal_side + (seal_side - sh) / 2, sw, sh, mask="auto")
        if signature:
            c.setStrokeColorRGB(0, 0, 0)
            c.setFillColorRGB(0, 0, 0)
            c.setLineWidth(0.6)
            y1 = sig_h - 18
            c.line(4, y1, w * 0.62, y1)
            c.line(w * 0.68, y1, w - 4, y1)
            c.setFont("Inter", 7)
            c.drawString(4, y1 - 9, "SIGNATURE")
            c.drawString(w * 0.68, y1 - 9, "DATE")
            if signer:
                c.setFont("Inter-SemiBold", 7.5)
                c.drawString(4, y1 - 19, signer.upper())
            if date_text:
                c.setFont("Inter", 8)
                c.drawString(w * 0.68 + 2, y1 + 3, date_text)

    extra: list[str] = []
    xo: dict[str, Any] = {}
    if kind == "form":
        g = PageGeom(pg)
        x, y, _w, _h = g.drect(r)
        ox, oy = x + (w - sw) / 2, y + sig_h + (seal_side - sh) / 2
        xo["/Seal0"] = obj
        bb = [float(v) for v in obj.get("/BBox", [0, 0, nw, nh])]
        extra.append(f"q {f:.6g} 0 0 {f:.6g} {ox - bb[0] * f:.4f} {oy - bb[1] * f:.4f} cm /Seal0 Do Q")
    return _custom_annot(pdf, pg, r, draw, subtype="Stamp", color=(0, 0, 0), subject="Seal",
                         contents="Engineer's seal (placed from supplied file)", author=author, extra_ops=extra, xobjects=xo)


def find_revision_table(info: _PageInfo, within: Rect | None = None) -> dict[str, Any] | None:
    """Locate a REV / DATE / DESCRIPTION table: header words on one line, existing rows below.

    Returns {"columns": [(name, x0)], "rows": [[words]], "size", "pitch", "next_top", "x1"} or None.
    """
    words = [w for w in info.words if within is None or within.contains_point((w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2)]
    for w in words:
        if w["text"].upper().rstrip(".") not in ("REV", "REV.", "NO", "NO."):
            continue
        line = [v for v in words if abs(v["top"] - w["top"]) < 2.5 and v["x0"] >= w["x0"] - 1]
        texts = [v["text"].upper() for v in sorted(line, key=lambda v: v["x0"])]
        if "DATE" in texts and "DESCRIPTION" in texts:
            hdr = sorted(line, key=lambda v: v["x0"])
            cols = [(w["text"].upper(), w["x0"])]
            for v in hdr:
                if v["text"].upper() in ("DATE", "DESCRIPTION", "BY", "APPR", "APP'D", "CHK"):
                    cols.append((v["text"].upper(), v["x0"]))
            size = float(w.get("size") or (w["bottom"] - w["top"]))
            x_lo, x_hi = cols[0][1] - 4, max(v["x1"] for v in hdr) + 3 * INCH
            below = sorted([v for v in words if v["top"] > w["bottom"] and x_lo <= v["x0"] <= x_hi], key=lambda v: v["top"])
            rows: list[list[dict]] = []
            last_top = w["top"]
            pitch_guess = size * 1.75
            for v in below:
                if rows and abs(v["top"] - rows[-1][0]["top"]) < 2.5:
                    rows[-1].append(v)
                    continue
                if v["top"] - last_top > pitch_guess * 1.8:
                    break
                rows.append([v])
                last_top = v["top"]
            if len(rows) >= 2:
                pitch = rows[-1][0]["top"] - rows[-2][0]["top"]
            elif rows:
                pitch = min(rows[0][0]["top"] - w["top"], size * 1.75)
            else:
                pitch = size * 1.75
            last = rows[-1][0] if rows else w
            row_size = float((rows[-1][0].get("size") if rows else None) or size)
            return {"columns": cols, "rows": rows, "size": row_size, "pitch": pitch,
                    "next_top": last["top"] + (pitch if rows else pitch), "x1": x_hi, "header_top": w["top"]}
    return None


def _revision_row_xs(tab: dict[str, Any]) -> list[float]:
    cols = tab["columns"]
    xs = [x for _n, x in cols]
    if tab["rows"]:
        # assign each word of the last row to the column whose header starts at or just left of it
        found: dict[int, float] = {}
        for v in sorted(tab["rows"][-1], key=lambda v: v["x0"]):
            k = min(range(len(xs)), key=lambda i: abs(xs[i] - v["x0"]))
            if k not in found:
                found[k] = v["x0"]
        return [found.get(k, x) for k, x in enumerate(xs)]
    return xs


def issue(
    src: str | Path,
    *,
    stamp: str | None = "ISSUED FOR CONSTRUCTION",
    date: str | None = "today",
    by: str | None = None,
    sheets: str | None = None,
    pages: str | None = None,
    position: str = "title-block",
    at: Sequence[float] | None = None,
    color: str | None = None,
    seal: str | Path | None = None,
    seal_at: Sequence[float] | None = None,
    seal_size: float = 1.75 * INCH,
    signature: bool = True,
    signer: str | None = None,
    revision: dict[str, Any] | None = None,
    flatten: bool = False,
    author: str | None = None,
    out: str | Path | None = None,
    password: str | None = None,
    options=None,
    quiet: bool = False,
) -> OpResult:
    """Issue sheets: stamp, optional seal, optional revision-table row.

    *position*: title-block (empty space in the title block, else beside it) | top-left | top-right |
    bottom-left | bottom-right; or *at* = (x, top) of the stamp. The stamp is sized to the title block
    (or sheet). *seal* is the user's seal file (PNG/JPG/PDF/SVG) — placed, never generated — at *seal_at*
    or auto-located in empty title-block space, with signature and date lines.
    *revision*: {"rev": "1", "date": "2026-11-13", "description": "ISSUED FOR CONSTRUCTION"} appends a row to
    a REV/DATE/DESCRIPTION table found on the sheet (drawn as page text, always burned in).
    """
    from pdfcore.overlay import stamp_pages, visual_size

    src = Path(src)
    if not (stamp or seal or revision):
        raise ValueError("nothing to do: give a stamp, a seal or a revision row")
    seal_path = Path(seal) if seal else None
    if seal_path is not None and not seal_path.is_file():
        raise FileNotFoundError(f"seal file not found: {seal_path}")
    dtext = None
    if date:
        dtext = _date.today().isoformat() if str(date).strip().lower() == "today" else str(date)
    label = (stamp or "").strip().upper()
    col = _hex(color or ISSUE_STAMPS.get(label, "#1F3A93"))
    dst = output_path(src, "issued", out=out)
    report: list[dict[str, Any]] = []
    rev_rows: dict[int, tuple[list[tuple[float, float, str, float]], str]] = {}
    with _open(src, password) as pdf:
        idx = _select_pages(src, len(pdf.pages), sheets=sheets, pages=pages, password=password)
        seal_obj = _load_seal_form(pdf, seal_path) if seal_path else None
        for pi in idx:
            pg = pdf.pages[pi]
            W, H = visual_size(pg)
            info = _analyze(src, pi, password)
            tb = find_title_block(info)
            ink = _InkMap(src, pi, info.W, info.H, password)
            rec: dict[str, Any] = {"page": pi + 1, "title_block": [round(v, 1) for v in (tb.x0, tb.top, tb.x1, tb.bottom)] if tb else None}
            drawing_region = Rect(info.border.x0, info.border.top, tb.x0 if tb and tb.top <= info.border.top + 2 else info.border.x1,
                                  tb.top if tb and tb.top > info.border.top + 2 else info.border.bottom)
            # ---- stamp
            if label:
                lines = [ln for ln in ([f"DATE: {dtext}"] if dtext else []) + ([f"BY: {by.upper()}"] if by else [])]
                target = (tb.width - 24) if (tb and position == "title-block" and tb.width < 0.6 * W) else min(W, H) * 0.24
                max_fs = max(14.0, min(40.0, min(W, H) / 45))
                fs, sw, sh, pad = _stamp_size(label, lines, target, max_fs)
                r = None
                if at is not None:
                    r = Rect(float(at[0]), float(at[1]), float(at[0]) + sw, float(at[1]) + sh)
                elif position == "title-block":
                    if tb is not None:
                        r = find_empty_rect(ink, tb, sw, sh, pad=5, prefer="top")
                    if r is None:
                        r = find_empty_rect(ink, drawing_region, sw, sh, pad=8, prefer="bottom-right")
                if r is None:
                    m = 0.75 * INCH
                    pos = position if position != "title-block" else "bottom-right"
                    x = info.border.x0 + m if pos.endswith("left") else drawing_region.x1 - m - sw
                    y = info.border.top + m if pos.startswith("top") else drawing_region.bottom - m - sh
                    r = Rect(x, y, x + sw, y + sh)
                ink.taken.append(r)
                a = _custom_annot(pdf, pg, r, _stamp_draw(label, lines, col, fs, sw, sh, pad), subtype="Stamp", color=col,
                                  subject=label.title(), contents=f"{label}" + (f" {dtext}" if dtext else ""), author=author)
                if flatten:
                    burn_annotation(pdf, pg, a)
                rec["stamp"] = [round(v, 1) for v in (r.x0, r.top, r.x1, r.bottom)]
            # ---- seal
            if seal_obj is not None:
                side = float(seal_size)
                sig_h = 30.0 if signature else 0.0
                bw, bh = max(side, 2.2 * INCH if signature else side), side + sig_h
                r = None
                if seal_at is not None:
                    r = Rect(float(seal_at[0]), float(seal_at[1]), float(seal_at[0]) + bw, float(seal_at[1]) + bh)
                else:
                    if tb is not None:
                        r = find_empty_rect(ink, tb, bw, bh, pad=5, prefer="top")
                    if r is None:
                        r = find_empty_rect(ink, drawing_region, bw, bh, pad=8, prefer="bottom-right")
                if r is None:
                    raise RuntimeError(f"page {pi + 1}: no empty {bw / INCH:.2f}×{bh / INCH:.2f} in area for the seal; give --seal-at x,y")
                ink.taken.append(r)
                a = _seal_annot(pdf, pg, r, seal_obj, side, signature, signer, dtext, author)
                if flatten:
                    burn_annotation(pdf, pg, a)
                rec["seal"] = [round(v, 1) for v in (r.x0, r.top, r.x1, r.bottom)]
            # ---- revision row
            if revision:
                tab = find_revision_table(info, tb)
                if tab is None:
                    rec["revision_row"] = "no revision table found"
                else:
                    xs = _revision_row_xs(tab)
                    vals = [str(revision.get("rev", "")), str(revision.get("date", dtext or "")),
                            str(revision.get("description", label or "")).upper()]
                    extra_cols = [str(revision.get(k, "")) for k in ("by", "appr")]
                    vals += extra_cols[: max(0, len(xs) - 3)]
                    size = tab["size"]
                    top = tab["next_top"]
                    band = Rect(xs[0] - 2, top - 1, tab["x1"], top + size * 1.2)
                    busy = [w for w in info.words if band.intersects(Rect(w["x0"], w["top"], w["x1"], w["bottom"]))]
                    if busy or (tb is not None and band.bottom > tb.bottom - 2):
                        rec["revision_row"] = "no room below the last revision row"
                    else:
                        base = top + size * 0.8  # baseline (top-left coords)
                        rev_rows[pi] = ([(x, base, v, size) for x, v in zip(xs, vals) if v], " ".join(vals))
                        rec["revision_row"] = " | ".join(vals)
            report.append(rec)
        if rev_rows:
            def draw(c, i, w, h):
                rows, _t = rev_rows[i]
                for x, base, v, size in rows:
                    c.setFont("Inter", size)
                    c.setFillColorRGB(0, 0, 0)
                    c.drawString(x, h - base, v)

            from pdfcore.fonts import register_reportlab

            register_reportlab()
            stamp_pages(pdf, draw, pages=sorted(rev_rows))
        pdf.save(str(dst))
    return finalize(dst, baseline=src, options=options or gate_options_from(profile="drawing"), quiet=quiet,
                    data={"pages": report, "stamp": label or None, "date": dtext, "flattened": flatten})


# ============================================================== CLI
def _pts(s: str, units: str) -> list[float]:
    f = {"pt": 1.0, "in": INCH, "mm": INCH / 25.4}[units]
    return [float(v) * f for v in s.split(",")]


@sub("cloud", "Revision clouds with delta tags on a sheet (annotations, or burned in with --flatten)")
def _cli_cloud(p: argparse.ArgumentParser):
    p.add_argument("input")
    p.add_argument("-o", "--out")
    p.add_argument("--sheet", help="sheet number, e.g. E-101")
    p.add_argument("--page", help="page(s), 1-based")
    p.add_argument("--rect", action="append", required=True, help="x0,top,x1,bottom (top-left origin); repeatable")
    p.add_argument("--units", choices=["pt", "in", "mm"], default="pt", help="units of --rect / --delta-at")
    p.add_argument("--delta", help="revision number for the delta triangle")
    p.add_argument("--delta-at", help="x,y centre of the delta (default: cloud's top-right corner)")
    p.add_argument("--color", default="red")
    p.add_argument("--flatten", action="store_true", help="burn clouds and deltas into the page")
    p.add_argument("--author")
    p.add_argument("--password")

    def run(ns):
        rects = [Rect(*_pts(r, ns.units)) for r in ns.rect]
        da = _pts(ns.delta_at, ns.units) if ns.delta_at else None
        return cloud(ns.input, rects, rev=ns.delta, sheet=ns.sheet, pages=ns.page, flatten=ns.flatten, color=ns.color,
                     delta_at=da, author=ns.author, out=ns.out, password=ns.password)

    return run


@sub("issue", "Issue stamp (IFC, bid, preliminary, custom) with date, engineer's seal placement, revision-table row")
def _cli_issue(p: argparse.ArgumentParser):
    p.add_argument("input")
    p.add_argument("-o", "--out")
    p.add_argument("--stamp", default="ISSUED FOR CONSTRUCTION",
                   help='"ISSUED FOR CONSTRUCTION", "ISSUED FOR BID", "NOT FOR CONSTRUCTION", "PRELIMINARY", or custom; "" for none')
    p.add_argument("--date", default="today", help="date printed on the stamp (default today)")
    p.add_argument("--by", help="name on the stamp")
    p.add_argument("--sheets", help="sheet numbers, e.g. E-101,E-201 (default: every page)")
    p.add_argument("--pages", help="pages, 1-based (default: all)")
    p.add_argument("--position", default="title-block",
                   choices=["title-block", "top-left", "top-right", "bottom-left", "bottom-right"])
    p.add_argument("--at", help="x,top of the stamp (overrides --position)")
    p.add_argument("--units", choices=["pt", "in", "mm"], default="pt", help="units of --at / --seal-at / --seal-size")
    p.add_argument("--color", help="stamp colour (#hex); default by label")
    p.add_argument("--seal", help="your seal image (PNG/JPG), PDF or SVG — placed as supplied, never generated")
    p.add_argument("--seal-at", help="x,top of the seal block (default: empty space in the title block)")
    p.add_argument("--seal-size", type=float, help="seal width (default 1.75 in)")
    p.add_argument("--signer", help="name under the signature line")
    p.add_argument("--no-signature", action="store_true", help="no signature/date lines under the seal")
    p.add_argument("--rev", help="append a row to the revision table with this revision number")
    p.add_argument("--rev-desc", help="description for the revision row (default: the stamp text)")
    p.add_argument("--flatten", action="store_true", help="burn stamp and seal into the page")
    p.add_argument("--author")
    p.add_argument("--password")

    def run(ns):
        f = {"pt": 1.0, "in": INCH, "mm": INCH / 25.4}[ns.units]
        rev = {"rev": ns.rev, "description": ns.rev_desc or ns.stamp or ""} if ns.rev else None
        if rev and ns.date and ns.date != "today":
            rev["date"] = ns.date
        return issue(ns.input, stamp=ns.stamp or None, date=ns.date, by=ns.by, sheets=ns.sheets, pages=ns.pages,
                     position=ns.position, at=_pts(ns.at, ns.units) if ns.at else None, color=ns.color, seal=ns.seal,
                     seal_at=_pts(ns.seal_at, ns.units) if ns.seal_at else None,
                     seal_size=(ns.seal_size * f) if ns.seal_size else 1.75 * INCH, signature=not ns.no_signature,
                     signer=ns.signer, revision=rev, flatten=ns.flatten, author=ns.author, out=ns.out, password=ns.password)

    return run
