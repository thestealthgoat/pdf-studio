"""Markup: create, list, delete, flatten, import/export annotations, and comment-summary reports.

Every annotation we create carries a normal appearance stream (``/AP /N``) drawn by our own code, so it
looks the same in every viewer and in pdfium renders. Text inside appearances (free text, callouts,
stamps) is drawn with an embedded subset of Inter via a small ReportLab-generated form XObject.

Coordinates
-----------
All geometry is in our :class:`~pdfcore.types.Rect` convention: points, **top-left origin of the page as
displayed** (relative to the MediaBox). That is exactly what :func:`pdfcore.text.search` returns (pdfplumber
applies ``/Rotate``), so search hits can be highlighted directly. On rotated pages (``/Rotate`` 90/180/270)
positions are converted to unrotated user space for ``/Rect``, ``/QuadPoints``, ``/Vertices`` …, and each
appearance stream starts with the visual→user matrix, so text in free-text boxes and stamps reads upright
to the reader.

Pages are 0-based in Python and in JSON specs/exports; CLI flags and printed/CSV/report output are 1-based.

Spec dicts (used by :func:`annotate`, :func:`import_annotations`, returned by :func:`list_annotations`)::

    {"type": "highlight", "page": 0, "rects": [[x0, top, x1, bottom], ...]}
    {"type": "highlight", "query": "Contractor"}                 # every match (optionally "pages": [0, 2])
    {"type": "note", "page": 0, "at": [x, top], "contents": "..."}
    {"type": "freetext", "page": 0, "rect": [...], "text": "...", "font_size": 11, "fill": "#ffffcc"}
    {"type": "callout", "page": 0, "rect": [...], "target": [x, top], "text": "..."}
    {"type": "square" | "circle", "page": 0, "rect": [...], "fill": None}
    {"type": "line", "page": 0, "points": [[x, y], [x, y]], "heads": ["None", "OpenArrow"]}
    {"type": "polygon" | "polyline", "page": 0, "points": [[x, y], ...]}
    {"type": "cloud", "page": 0, "rect": [...]} or {"type": "cloud", "points": [...], "intensity": 1}
    {"type": "ink", "page": 0, "strokes": [[[x, y], ...], ...]}
    {"type": "stamp", "page": 0, "at": [x, top] or "rect": [...], "label": "APPROVED"}

Common keys: color, opacity, width, author, subject, contents, created, modified (ISO strings or datetimes).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.result import OpResult
from pdfcore.types import Rect

__all__ = [
    "parse_color", "PageGeom", "add_annotation", "add_text_markup", "add_note", "add_free_text", "add_callout",
    "add_square", "add_circle", "add_line", "add_polygon", "add_polyline", "add_cloud", "add_ink", "add_stamp",
    "cloud_path", "annotate", "highlight", "list_annotations", "delete_annotations", "flatten_annotations",
    "export_annotations", "import_annotations", "comment_summary", "KINDS", "COMMENT_SUBTYPES",
]

RGB = tuple[float, float, float]
Pt = tuple[float, float]

NAMED_COLORS: dict[str, RGB] = {
    "yellow": (1.0, 0.92, 0.0), "red": (0.86, 0.1, 0.1), "green": (0.1, 0.6, 0.2), "blue": (0.1, 0.35, 0.85),
    "orange": (1.0, 0.55, 0.0), "cyan": (0.0, 0.75, 0.9), "magenta": (0.85, 0.1, 0.65), "purple": (0.5, 0.2, 0.75),
    "black": (0.0, 0.0, 0.0), "white": (1.0, 1.0, 1.0), "gray": (0.5, 0.5, 0.5), "grey": (0.5, 0.5, 0.5),
    "pink": (1.0, 0.6, 0.75),
}

# our kind → PDF /Subtype
KINDS: dict[str, str] = {
    "highlight": "Highlight", "underline": "Underline", "strikeout": "StrikeOut", "squiggly": "Squiggly",
    "note": "Text", "freetext": "FreeText", "callout": "FreeText", "square": "Square", "circle": "Circle",
    "line": "Line", "polygon": "Polygon", "polyline": "PolyLine", "cloud": "Polygon", "ink": "Ink", "stamp": "Stamp",
}
DEFAULT_COLOR: dict[str, RGB] = {
    "highlight": NAMED_COLORS["yellow"], "underline": (0.0, 0.45, 0.2), "strikeout": NAMED_COLORS["red"],
    "squiggly": (0.0, 0.45, 0.85), "note": (1.0, 0.82, 0.2), "stamp": (0.75, 0.1, 0.1), "cloud": NAMED_COLORS["red"],
}
# Annotation subtypes treated as "comments" (listed, deleted, flattened by default).
COMMENT_SUBTYPES = {
    "Text", "FreeText", "Line", "Square", "Circle", "Polygon", "PolyLine", "Highlight", "Underline", "Squiggly",
    "StrikeOut", "Caret", "Ink", "Stamp", "FileAttachment", "Sound", "Redact",
}
STD_STAMPS = [
    "Approved", "Experimental", "NotApproved", "AsIs", "Expired", "NotForPublicRelease", "Confidential", "Final",
    "Sold", "Departmental", "ForComment", "TopSecret", "Draft", "ForPublicRelease",
]
_STAMP_COLORS = {"APPROVED": (0.1, 0.5, 0.2), "FINAL": (0.1, 0.5, 0.2), "REJECTED": (0.8, 0.1, 0.1),
                 "NOT APPROVED": (0.8, 0.1, 0.1), "VOID": (0.8, 0.1, 0.1), "CONFIDENTIAL": (0.8, 0.1, 0.1)}
HEADS = ["None", "Square", "Circle", "Diamond", "OpenArrow", "ClosedArrow", "Butt", "ROpenArrow", "RClosedArrow", "Slash"]
FLAG_HIDDEN, FLAG_PRINT, FLAG_NOVIEW = 2, 4, 32


# ============================================================== small helpers
def parse_color(c: Any) -> RGB | None:
    """'yellow' | '#ffcc00' | 'ffcc00' | (r, g, b) floats 0–1 or ints 0–255 | None/'none'."""
    if c is None:
        return None
    if isinstance(c, str):
        s = c.strip().lower()
        if s in ("", "none", "transparent"):
            return None
        if s in NAMED_COLORS:
            return NAMED_COLORS[s]
        h = s.lstrip("#")
        if re.fullmatch(r"[0-9a-f]{6}", h):
            return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)
        if re.fullmatch(r"[0-9a-f]{3}", h):
            return tuple(int(ch * 2, 16) / 255 for ch in h)  # type: ignore[return-value]
        if "," in s:
            return parse_color(tuple(float(v) for v in s.split(",")))
        raise ValueError(f"unknown color {c!r}")
    vals = [float(v) for v in c]
    if len(vals) == 1:
        vals = vals * 3
    if len(vals) == 4:  # CMYK → RGB
        cc, m, y, k = vals
        vals = [(1 - cc) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)]
    if any(v > 1 for v in vals):
        vals = [v / 255 for v in vals]
    return (vals[0], vals[1], vals[2])


def color_hex(c: RGB | None) -> str | None:
    if c is None:
        return None
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in c)


def _f(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _rg(c: RGB, stroke: bool = False) -> str:
    return f"{_f(c[0])} {_f(c[1])} {_f(c[2])} {'RG' if stroke else 'rg'}"


def pdf_date(dt: datetime | str | None = None) -> str:
    if isinstance(dt, str):
        if dt.startswith("D:"):
            return dt
        dt = datetime.fromisoformat(dt)
    dt = dt or datetime.now().astimezone()
    if dt.tzinfo is None:
        dt = dt.astimezone()
    off = dt.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    tz = "Z" if mins == 0 and sign == "+" else f"{sign}{mins // 60:02d}'{mins % 60:02d}'"
    return dt.strftime("D:%Y%m%d%H%M%S") + tz


def parse_pdf_date(s: str | None) -> datetime | None:
    if not s:
        return None
    m = re.match(r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*([Zz+\-])?(\d{2})?'?(\d{2})?'?", str(s).strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss, sign, th, tm = m.groups()
    tz = timezone.utc
    if sign in ("+", "-") and th:
        delta = timedelta(hours=int(th), minutes=int(tm or 0))
        tz = timezone(delta if sign == "+" else -delta)
    try:
        return datetime(int(y), int(mo or 1), int(d or 1), int(hh or 0), int(mi or 0), int(ss or 0), tzinfo=tz)
    except ValueError:
        return None


def _as_rect(r: Rect | Sequence[float]) -> Rect:
    if isinstance(r, Rect):
        return r
    x0, t, x1, b = (float(v) for v in r)
    return Rect(min(x0, x1), min(t, b), max(x0, x1), max(t, b))


def _as_pt(p: Sequence[float]) -> Pt:
    return (float(p[0]), float(p[1]))


# ============================================================== page geometry
class PageGeom:
    """Converts between our visual top-left coordinates and PDF user space for one page.

    *Drawing space* is the visual page with a bottom-left origin (what ``overlay.py`` calls visual
    coordinates). Appearance streams are drawn in drawing space after a ``cm`` with :attr:`matrix`.
    """

    def __init__(self, page: pikepdf.Page):
        mb = [float(v) for v in page.mediabox]
        x0, y0, x1, y1 = min(mb[0], mb[2]), min(mb[1], mb[3]), max(mb[0], mb[2]), max(mb[1], mb[3])
        self.x0, self.y0, self.W, self.H = x0, y0, x1 - x0, y1 - y0
        self.rot = int(page.obj.get("/Rotate", 0) or 0) % 360
        self.vw, self.vh = (self.H, self.W) if self.rot in (90, 270) else (self.W, self.H)
        W, H = self.W, self.H
        self.matrix: tuple[float, ...] = {
            0: (1, 0, 0, 1, x0, y0),
            90: (0, 1, -1, 0, x0 + W, y0),
            180: (-1, 0, 0, -1, x0 + W, y0 + H),
            270: (0, -1, 1, 0, x0, y0 + H),
        }[self.rot]

    # top-left visual → drawing space
    def d(self, x: float, top: float) -> Pt:
        return (x, self.vh - top)

    def drect(self, r: Rect) -> tuple[float, float, float, float]:
        """(x, y, w, h) of a Rect in drawing space."""
        return (r.x0, self.vh - r.bottom, r.width, r.height)

    # drawing space → user space
    def user(self, u: float, v: float) -> Pt:
        a, b, c, d, e, f = self.matrix
        return (a * u + c * v + e, b * u + d * v + f)

    def user_pt(self, x: float, top: float) -> Pt:
        return self.user(*self.d(x, top))

    def user_bbox(self, pts: Iterable[Pt], pad: float = 0.0) -> tuple[float, float, float, float]:
        """Bounding box in user space of drawing-space points, padded."""
        us = [self.user(u, v) for u, v in pts]
        xs, ys = [p[0] for p in us], [p[1] for p in us]
        return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

    # user space → top-left visual
    def visual(self, x: float, y: float) -> Pt:
        a, b, c, d, e, f = self.matrix
        xx, yy = x - e, y - f
        det = a * d - b * c
        u, v = (d * xx - c * yy) / det, (-b * xx + a * yy) / det
        return (u, self.vh - v)

    def rect_from_user(self, llx: float, lly: float, urx: float, ury: float) -> Rect:
        pts = [self.visual(llx, lly), self.visual(urx, ury), self.visual(llx, ury), self.visual(urx, lly)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return Rect(min(xs), min(ys), max(xs), max(ys))

    def cm(self) -> str:
        return " ".join(_f(v) for v in self.matrix) + " cm"


# ============================================================== appearance builder
class _AP:
    """Collects content-stream operators in drawing space and builds an appearance Form XObject."""

    def __init__(self, pdf: pikepdf.Pdf, geom: PageGeom):
        self.pdf, self.g = pdf, geom
        self.ops: list[str] = [geom.cm()]
        self.gs = Dictionary()
        self.xo = Dictionary()

    def add(self, *ops: str) -> "_AP":
        self.ops.extend(ops)
        return self

    def gstate(self, opacity: float = 1.0, blend: str | None = None) -> None:
        if opacity >= 0.999 and not blend:
            return
        name = f"/G{len(self.gs)}"
        d = Dictionary(Type=Name.ExtGState, CA=float(opacity), ca=float(opacity))
        if blend:
            d.BM = Name("/" + blend)
        self.gs[name] = d
        self.ops.append(f"{name} gs")

    def form(self, form: pikepdf.Object, x: float, y: float) -> None:
        name = f"/T{len(self.xo)}"
        self.xo[name] = form
        self.ops.append(f"q 1 0 0 1 {_f(x)} {_f(y)} cm {name} Do Q")

    def build(self, bbox: tuple[float, float, float, float]) -> pikepdf.Stream:
        res = Dictionary(ProcSet=Array([Name.PDF, Name.Text]))
        if len(self.gs):
            res.ExtGState = self.gs
        if len(self.xo):
            res.XObject = self.xo
        data = ("\n".join(self.ops) + "\n").encode("latin-1")
        return self.pdf.make_stream(
            data, Type=Name.XObject, Subtype=Name.Form, FormType=1,
            BBox=Array([float(v) for v in bbox]), Matrix=Array([1, 0, 0, 1, 0, 0]), Resources=res,
        )


def _materialize(obj: Any, seen: set) -> None:
    """Pull copied foreign stream data into memory so the source PDF can be closed."""
    if isinstance(obj, pikepdf.Stream):
        if obj.objgen in seen and obj.objgen != (0, 0):
            return
        seen.add(obj.objgen)
        raw = obj.read_raw_bytes()
        obj.write(raw, filter=obj.get("/Filter"), decode_parms=obj.get("/DecodeParms"), type_check=False)
        for _k, v in obj.stream_dict.items():
            _materialize(v, seen)
    elif isinstance(obj, pikepdf.Dictionary):
        if obj.is_indirect:
            if obj.objgen in seen:
                return
            seen.add(obj.objgen)
        for k, v in obj.items():
            if k not in ("/Parent", "/P"):
                _materialize(v, seen)
    elif isinstance(obj, pikepdf.Array):
        for v in obj:
            _materialize(v, seen)


def rl_form(pdf: pikepdf.Pdf, w: float, h: float, draw: Callable[[Any], None]) -> pikepdf.Object:
    """Draw with ReportLab (house fonts registered, embedded + subset) on a w×h canvas; return a Form XObject in *pdf*."""
    from reportlab.pdfgen import canvas

    from pdfcore.fonts import register_reportlab

    register_reportlab()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(max(w, 1), max(h, 1)), pageCompression=1, initialFontName="Inter")
    draw(c)
    c.showPage()
    c.save()
    with pikepdf.open(io.BytesIO(buf.getvalue())) as src:
        form = pdf.copy_foreign(src.pages[0].as_form_xobject())
        _materialize(form, set())
    return form


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    from reportlab.lib.utils import simpleSplit

    out: list[str] = []
    for para in text.split("\n"):
        out.extend(simpleSplit(para, font, size, width) if para.strip() else [""])
    return out


def _text_form(pdf: pikepdf.Pdf, text: str, w: float, h: float, *, size: float, color: RGB, align: str = "left",
               font: str = "Inter", valign: str = "top", min_size: float = 5.0) -> tuple[pikepdf.Object, float]:
    """Wrapped text block as a form XObject (shrinks the font until it fits). Returns (form, used_size)."""
    from pdfcore.fonts import register_reportlab

    register_reportlab()
    lead = 1.22
    while True:
        lines = _wrap(text, font, size, w)
        if len(lines) * size * lead <= h + 0.01 or size <= min_size:
            break
        size = max(min_size, size - 0.5)

    def draw(c):
        c.setFillColorRGB(*color)
        c.setFont(font, size)
        total = len(lines) * size * lead
        y = h - size * 0.95 if valign == "top" else (h + total) / 2 - size * 0.95
        for ln in lines:
            if align == "center":
                c.drawCentredString(w / 2, y, ln)
            elif align == "right":
                c.drawRightString(w, y, ln)
            else:
                c.drawString(0, y, ln)
            y -= size * lead

    return rl_form(pdf, w, h, draw), size


# ============================================================== annotation dictionary
def _common(pdf: pikepdf.Pdf, page: pikepdf.Page, subtype: str, rect: tuple[float, float, float, float], *,
            color: RGB | None, opacity: float, author: str | None, subject: str | None, contents: str | None,
            created: datetime | str | None, modified: datetime | str | None, flags: int = FLAG_PRINT) -> Dictionary:
    d = Dictionary(
        Type=Name.Annot, Subtype=Name("/" + subtype), Rect=Array([float(v) for v in rect]), F=flags,
        NM=String(f"pdfstudio-{uuid.uuid4()}"), P=page.obj,
    )
    if color is not None:
        d.C = Array([float(v) for v in color])
    if opacity < 0.999:
        d.CA = float(opacity)
    if author:
        d.T = String(author)
    if subject:
        d.Subj = String(subject)
    if contents:
        d.Contents = String(contents)
    cdate = pdf_date(created)
    d.CreationDate = String(cdate)
    d.M = String(pdf_date(modified) if modified else cdate)
    return d


def _attach(pdf: pikepdf.Pdf, page: pikepdf.Page, annot: Dictionary, ap: pikepdf.Stream) -> Dictionary:
    annot.AP = Dictionary(N=ap)
    ind = pdf.make_indirect(annot)
    annots = page.obj.get("/Annots")
    if annots is None:
        page.obj.Annots = pdf.make_indirect(Array())
        annots = page.obj.Annots
    annots.append(ind)
    return ind


def _page(pdf: pikepdf.Pdf, page: int | pikepdf.Page) -> pikepdf.Page:
    return pdf.pages[page] if isinstance(page, int) else page


def _bs(d: Dictionary, width: float, dash: Sequence[float] | None = None) -> None:
    d.BS = Dictionary(Type=Name.Border, W=float(width), S=Name.D if dash else Name.S)
    if dash:
        d.BS.D = Array([float(v) for v in dash])


# ============================================================== text markup
def add_text_markup(pdf: pikepdf.Pdf, page: int | pikepdf.Page, rects: Sequence[Rect | Sequence[float]],
                    kind: str = "highlight", *, color: Any = None, opacity: float | None = None,
                    width: float | None = None, author: str | None = None, subject: str | None = None,
                    contents: str | None = None, created: Any = None, modified: Any = None,
                    vertical: bool | None = None) -> Dictionary:
    """Highlight / underline / strikeout / squiggly over one rect per text line.

    *vertical*: the text runs top↔bottom as displayed (auto: rect at least twice as tall as wide). For vertical
    text the glyph baseline side is taken as the visual left on pages rotated 90/180 and the right otherwise,
    which is right for horizontal text on a rotated page and for bottom-to-top CAD text on unrotated pages.
    """
    kind = kind.lower()
    if kind not in ("highlight", "underline", "strikeout", "squiggly"):
        raise ValueError(f"not a text-markup kind: {kind}")
    pg = _page(pdf, page)
    g = PageGeom(pg)
    rs = [_as_rect(r) for r in rects]
    if not rs:
        raise ValueError("text markup needs at least one rectangle")
    col = parse_color(color) or DEFAULT_COLOR[kind]
    op = (0.45 if kind == "highlight" else 1.0) if opacity is None else float(opacity)
    base_left = g.rot in (90, 180)
    quads: list[float] = []
    pts: list[Pt] = []
    verts: list[bool] = []
    for r in rs:
        vert = vertical if vertical is not None else (r.height > 2 * r.width)
        verts.append(vert)
        if not vert:
            corners = ((r.x0, r.top), (r.x1, r.top), (r.x0, r.bottom), (r.x1, r.bottom))  # UL UR LL LR
        elif base_left:  # reads top→bottom, glyph tops face visual right
            corners = ((r.x1, r.top), (r.x1, r.bottom), (r.x0, r.top), (r.x0, r.bottom))
        else:  # reads bottom→top, glyph tops face visual left
            corners = ((r.x0, r.bottom), (r.x0, r.top), (r.x1, r.bottom), (r.x1, r.top))
        for x, t in corners:
            quads.extend(g.user_pt(x, t))
            pts.append(g.d(x, t))
    pad = 2.0 if kind == "squiggly" else 1.0
    bbox = g.user_bbox(pts, pad)
    ap = _AP(pdf, g)
    ap.gstate(op, "Multiply" if kind == "highlight" else None)
    if kind == "highlight":
        ap.add(_rg(col))
        for r in rs:
            x, y, w, h = g.drect(r)
            ap.add(f"{_f(x)} {_f(y)} {_f(w)} {_f(h)} re")
        ap.add("f")
    else:
        ap.add(_rg(col, True), "1 J" if kind != "squiggly" else "1 j")
        for r, vert in zip(rs, verts):
            x, y, w, h = g.drect(r)
            # work in a local frame: along = text direction length, across = line height
            if not vert:
                along, across = w, h
                def P(s, t, x=x, y=y):  # s along the text from its start, t up from the glyph bottom
                    return (x + s, y + t)
            elif base_left:
                along, across = h, w
                def P(s, t, x=x, y=y, h=h):
                    return (x + t, y + h - s)
            else:
                along, across = h, w
                def P(s, t, x=x, y=y, w=w):
                    return (x + w - t, y + s)
            lw = width or max(0.6, across / 14)
            if kind in ("underline", "strikeout"):
                t = lw / 2 + across * 0.04 if kind == "underline" else across * 0.45
                (ax, ay), (bx, by) = P(0, t), P(along, t)
                ap.add(f"{_f(lw)} w {_f(ax)} {_f(ay)} m {_f(bx)} {_f(by)} l S")
            else:
                amp, per = max(0.8, across / 12), max(2.0, across / 5)
                t0 = amp + 0.3
                n = max(2, int(along / (per / 2)))
                step = along / n
                ax, ay = P(0, t0)
                seg = [f"{_f(ax)} {_f(ay)} m"]
                for k in range(1, n + 1):
                    qx, qy = P(k * step, t0 + (amp if k % 2 else -amp))
                    seg.append(f"{_f(qx)} {_f(qy)} l")
                ap.add(f"{_f(lw * 0.8)} w " + " ".join(seg) + " S")
    d = _common(pdf, pg, KINDS[kind], bbox, color=col, opacity=op, author=author, subject=subject, contents=contents,
                created=created, modified=modified)
    d.QuadPoints = Array([float(v) for v in quads])
    return _attach(pdf, pg, d, ap.build(bbox))


# ============================================================== sticky note
NOTE_ICONS = ["Comment", "Note", "Help", "Insert", "Key", "NewParagraph", "Paragraph"]


def add_note(pdf: pikepdf.Pdf, page: int | pikepdf.Page, at: Sequence[float], contents: str = "", *,
             icon: str = "Comment", color: Any = None, opacity: float = 1.0, author: str | None = None,
             subject: str | None = None, created: Any = None, modified: Any = None, size: float = 20.0,
             popup: bool = True, width: float | None = None) -> Dictionary:
    """Sticky note (/Text) whose icon's top-left corner is at *at*."""
    pg = _page(pdf, page)
    g = PageGeom(pg)
    x, t = _as_pt(at)
    col = parse_color(color) or DEFAULT_COLOR["note"]
    s = float(size)
    u, v = g.d(x, t + s)  # bottom-left of icon in drawing space
    bbox = g.user_bbox([(u, v), (u + s, v + s)])
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    k = 0.15 * s
    dark = tuple(c * 0.45 for c in col)
    # speech bubble with tail
    bx0, by0, bx1, by1 = u + 1, v + 0.28 * s, u + s - 1, v + s - 1
    path = (f"{_f(bx0 + k)} {_f(by1)} m {_f(bx1 - k)} {_f(by1)} l {_f(bx1)} {_f(by1)} {_f(bx1)} {_f(by1)} {_f(bx1)} {_f(by1 - k)} c "
            f"{_f(bx1)} {_f(by0 + k)} l {_f(bx1)} {_f(by0)} {_f(bx1)} {_f(by0)} {_f(bx1 - k)} {_f(by0)} c "
            f"{_f(u + 0.45 * s)} {_f(by0)} l {_f(u + 0.22 * s)} {_f(v + 1)} l {_f(u + 0.3 * s)} {_f(by0)} l "
            f"{_f(bx0 + k)} {_f(by0)} l {_f(bx0)} {_f(by0)} {_f(bx0)} {_f(by0)} {_f(bx0)} {_f(by0 + k)} c "
            f"{_f(bx0)} {_f(by1 - k)} l {_f(bx0)} {_f(by1)} {_f(bx0)} {_f(by1)} {_f(bx0 + k)} {_f(by1)} c h")
    ap.add(_rg(col), _rg(dark, True), f"{_f(max(0.6, s / 24))} w 1 j", path, "B")
    ap.add(_rg(dark, True), f"{_f(max(0.6, s / 22))} w 1 J")
    for i in range(3):
        yy = by1 - (0.22 + 0.17 * i) * s
        ap.add(f"{_f(bx0 + 0.18 * s)} {_f(yy)} m {_f(bx1 - (0.18 + 0.12 * (i == 2)) * s)} {_f(yy)} l S")
    d = _common(pdf, pg, "Text", bbox, color=col, opacity=opacity, author=author, subject=subject, contents=contents,
                created=created, modified=modified)
    d.Name = Name("/" + (icon if icon in NOTE_ICONS else "Comment"))
    d.Open = False
    ind = _attach(pdf, pg, d, ap.build(bbox))
    if popup:
        pw, ph = 180.0, 100.0
        pu = min(u + s + 4, max(0.0, g.vw - pw))
        pv = max(0.0, v + s - ph)
        pb = g.user_bbox([(pu, pv), (pu + pw, pv + ph)])
        pop = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Popup, Rect=Array([float(c) for c in pb]),
                                           Parent=ind, Open=False, F=FLAG_PRINT | 24, P=pg.obj))
        ind.Popup = pop
        pg.obj.Annots.append(pop)
    return ind


# ============================================================== free text + callout
def _box_ops(ap: _AP, x: float, y: float, w: float, h: float, *, fill: RGB | None, border: RGB | None, bw: float) -> None:
    if fill is not None:
        ap.add(_rg(fill), f"{_f(x)} {_f(y)} {_f(w)} {_f(h)} re f")
    if border is not None and bw > 0:
        ap.add(_rg(border, True), f"{_f(bw)} w {_f(x + bw / 2)} {_f(y + bw / 2)} {_f(w - bw)} {_f(h - bw)} re S")


def _da(text_color: RGB, size: float, border: RGB | None) -> str:
    da = f"{_f(text_color[0])} {_f(text_color[1])} {_f(text_color[2])} rg /Helv {_f(size)} Tf"
    if border is not None:
        da = _rg(border, True) + " " + da
    return da


def add_free_text(pdf: pikepdf.Pdf, page: int | pikepdf.Page, rect: Rect | Sequence[float], text: str, *,
                  font_size: float = 11.0, text_color: Any = "black", fill: Any = None, color: Any = "black",
                  width: float = 1.0, align: str = "left", opacity: float = 1.0, author: str | None = None,
                  subject: str | None = None, contents: str | None = None, created: Any = None,
                  modified: Any = None, padding: float = 4.0, bold: bool = False) -> Dictionary:
    """Text box (/FreeText). *color* is the border colour (None = no border); *fill* the background."""
    pg = _page(pdf, page)
    g = PageGeom(pg)
    r = _as_rect(rect)
    tc = parse_color(text_color) or (0.0, 0.0, 0.0)
    border = parse_color(color) if width > 0 else None
    fl = parse_color(fill)
    x, y, w, h = g.drect(r)
    bbox = g.user_bbox([(x, y), (x + w, y + h)])
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    _box_ops(ap, x, y, w, h, fill=fl, border=border, bw=width)
    pad = padding + (width if border else 0)
    font = "Inter-Bold" if bold else "Inter"
    form, used = _text_form(pdf, text, max(1.0, w - 2 * pad), max(1.0, h - 2 * pad), size=font_size, color=tc,
                            align=align, font=font)
    ap.form(form, x + pad, y + pad)
    d = _common(pdf, pg, "FreeText", bbox, color=fl, opacity=opacity, author=author, subject=subject,
                contents=contents if contents is not None else text, created=created, modified=modified)
    if fl is None and "/C" in d:
        del d["/C"]
    d.DA = String(_da(tc, used, border))
    d.DS = String(f"font: Inter {_f(used)}pt; text-align:{align}; color:{color_hex(tc)}")
    d.Q = {"left": 0, "center": 1, "right": 2}.get(align, 0)
    _bs(d, width if border else 0)
    return _attach(pdf, pg, d, ap.build(bbox))


def _head(style: str, p: Pt, direction: Pt, size: float, color: RGB, w: float) -> list[str]:
    """Line-ending ops at point *p*; *direction* is a unit vector pointing out of the line at p."""
    style = style or "None"
    if style == "None":
        return []
    dx, dy = direction
    if style.startswith("R"):
        dx, dy = -dx, -dy
        style = style[1:]
    px, py = p
    nx, ny = -dy, dx
    out: list[str] = []
    if style in ("OpenArrow", "ClosedArrow"):
        a = math.radians(28)
        ca, sa = math.cos(a), math.sin(a)
        l1 = (px - size * (dx * ca - dy * sa), py - size * (dy * ca + dx * sa))
        l2 = (px - size * (dx * ca + dy * sa), py - size * (dy * ca - dx * sa))
        if style == "OpenArrow":
            out.append(f"{_f(l1[0])} {_f(l1[1])} m {_f(px)} {_f(py)} l {_f(l2[0])} {_f(l2[1])} l S")
        else:
            out.append(_rg(color) + f" {_f(l1[0])} {_f(l1[1])} m {_f(px)} {_f(py)} l {_f(l2[0])} {_f(l2[1])} l h B")
    elif style == "Circle":
        out.append(_rg(color) + " " + _ellipse(px - size / 2, py - size / 2, size, size) + " B")
    elif style == "Square":
        s2 = size / 2
        out.append(_rg(color) + f" {_f(px - s2)} {_f(py - s2)} {_f(size)} {_f(size)} re B")
    elif style == "Diamond":
        s2 = size / 2
        out.append(_rg(color) + f" {_f(px + dx * s2)} {_f(py + dy * s2)} m {_f(px + nx * s2)} {_f(py + ny * s2)} l "
                   f"{_f(px - dx * s2)} {_f(py - dy * s2)} l {_f(px - nx * s2)} {_f(py - ny * s2)} l h B")
    elif style == "Butt":
        s2 = size / 2
        out.append(f"{_f(px + nx * s2)} {_f(py + ny * s2)} m {_f(px - nx * s2)} {_f(py - ny * s2)} l S")
    elif style == "Slash":
        s2 = size / 2
        a = math.radians(60)
        sx, sy = dx * math.cos(a) - dy * math.sin(a), dy * math.cos(a) + dx * math.sin(a)
        out.append(f"{_f(px + sx * s2)} {_f(py + sy * s2)} m {_f(px - sx * s2)} {_f(py - sy * s2)} l S")
    return out


def _unit(a: Pt, b: Pt) -> Pt:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1.0
    return (dx / n, dy / n)


def add_callout(pdf: pikepdf.Pdf, page: int | pikepdf.Page, rect: Rect | Sequence[float], text: str,
                target: Sequence[float], *, knee: Sequence[float] | None = None, head: str = "OpenArrow",
                font_size: float = 11.0, text_color: Any = "black", fill: Any = "white", color: Any = "red",
                width: float = 1.0, align: str = "left", opacity: float = 1.0, author: str | None = None,
                subject: str | None = None, contents: str | None = None, created: Any = None,
                modified: Any = None, padding: float = 4.0) -> Dictionary:
    """Callout: a text box (*rect*) with a leader line and arrow pointing at *target*."""
    pg = _page(pdf, page)
    g = PageGeom(pg)
    r = _as_rect(rect)
    col = parse_color(color) or NAMED_COLORS["red"]
    tc = parse_color(text_color) or (0.0, 0.0, 0.0)
    fl = parse_color(fill)
    x, y, w, h = g.drect(r)
    tgt = g.d(*_as_pt(target))
    # attach point: middle of the box edge facing the target
    cx, cy = x + w / 2, y + h / 2
    if tgt[0] < x:
        attach = (x, cy)
    elif tgt[0] > x + w:
        attach = (x + w, cy)
    elif tgt[1] < y:
        attach = (cx, y)
    else:
        attach = (cx, y + h)
    if knee is not None:
        kn = g.d(*_as_pt(knee))
    elif attach[0] in (x, x + w):
        kn = (attach[0] + (-14 if attach[0] == x else 14), attach[1])
    else:
        kn = (attach[0], attach[1] + (-14 if attach[1] == y else 14))
    size = max(7.0, 5 * width)
    pts = [(x, y), (x + w, y + h), tgt, kn]
    bbox = g.user_bbox(pts, size + width + 1)
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    ap.add("1 j 1 J", _rg(col, True), f"{_f(width)} w")
    ap.add(f"{_f(tgt[0])} {_f(tgt[1])} m {_f(kn[0])} {_f(kn[1])} l {_f(attach[0])} {_f(attach[1])} l S")
    ap.add(*_head(head, tgt, _unit(kn, tgt), size, col, width))
    _box_ops(ap, x, y, w, h, fill=fl, border=col, bw=width)
    pad = padding + width
    form, used = _text_form(pdf, text, max(1.0, w - 2 * pad), max(1.0, h - 2 * pad), size=font_size, color=tc, align=align)
    ap.form(form, x + pad, y + pad)
    d = _common(pdf, pg, "FreeText", bbox, color=fl, opacity=opacity, author=author, subject=subject,
                contents=contents if contents is not None else text, created=created, modified=modified)
    if fl is None and "/C" in d:
        del d["/C"]
    d.IT = Name.FreeTextCallout
    cl: list[float] = []
    for p in (tgt, kn, attach):
        cl.extend(g.user(*p))
    d.CL = Array([float(v) for v in cl])
    d.LE = Name("/" + head)
    d.DA = String(_da(tc, used, col))
    d.DS = String(f"font: Inter {_f(used)}pt; text-align:{align}; color:{color_hex(tc)}")
    d.Q = {"left": 0, "center": 1, "right": 2}.get(align, 0)
    _bs(d, width)
    box_u = g.user_bbox([(x, y), (x + w, y + h)])
    d.RD = Array([float(box_u[0] - bbox[0]), float(box_u[1] - bbox[1]), float(bbox[2] - box_u[2]), float(bbox[3] - box_u[3])])
    return _attach(pdf, pg, d, ap.build(bbox))


# ============================================================== shapes
def _ellipse(x: float, y: float, w: float, h: float) -> str:
    k = 0.5523
    rx, ry = w / 2, h / 2
    cx, cy = x + rx, y + ry
    return (f"{_f(cx + rx)} {_f(cy)} m "
            f"{_f(cx + rx)} {_f(cy + k * ry)} {_f(cx + k * rx)} {_f(cy + ry)} {_f(cx)} {_f(cy + ry)} c "
            f"{_f(cx - k * rx)} {_f(cy + ry)} {_f(cx - rx)} {_f(cy + k * ry)} {_f(cx - rx)} {_f(cy)} c "
            f"{_f(cx - rx)} {_f(cy - k * ry)} {_f(cx - k * rx)} {_f(cy - ry)} {_f(cx)} {_f(cy - ry)} c "
            f"{_f(cx + k * rx)} {_f(cy - ry)} {_f(cx + rx)} {_f(cy - k * ry)} {_f(cx + rx)} {_f(cy)} c h")


def _paint(fill: RGB | None) -> str:
    return "B" if fill is not None else "S"


def _shape(pdf, page, rect, subtype: str, *, color, fill, width, opacity, author, subject, contents, created,
           modified, dash) -> Dictionary:
    pg = _page(pdf, page)
    g = PageGeom(pg)
    r = _as_rect(rect)
    col = parse_color(color) or NAMED_COLORS["red"]
    fl = parse_color(fill)
    x, y, w, h = g.drect(r)
    pad = width / 2 + 1
    bbox = g.user_bbox([(x, y), (x + w, y + h)], pad)
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    ap.add(_rg(col, True), f"{_f(width)} w 1 j")
    if dash:
        ap.add("[" + " ".join(_f(v) for v in dash) + "] 0 d")
    if fl is not None:
        ap.add(_rg(fl))
    geom = f"{_f(x)} {_f(y)} {_f(w)} {_f(h)} re" if subtype == "Square" else _ellipse(x, y, w, h)
    ap.add(geom, _paint(fl))
    d = _common(pdf, pg, subtype, bbox, color=col, opacity=opacity, author=author, subject=subject, contents=contents,
                created=created, modified=modified)
    if fl is not None:
        d.IC = Array([float(v) for v in fl])
    _bs(d, width, dash)
    d.RD = Array([float(pad)] * 4)
    return _attach(pdf, pg, d, ap.build(bbox))


def add_square(pdf: pikepdf.Pdf, page: int | pikepdf.Page, rect: Rect | Sequence[float], *, color: Any = "red",
               fill: Any = None, width: float = 1.5, opacity: float = 1.0, author: str | None = None,
               subject: str | None = None, contents: str | None = None, created: Any = None, modified: Any = None,
               dash: Sequence[float] | None = None) -> Dictionary:
    """Rectangle (/Square); *rect* is the border's centre line."""
    return _shape(pdf, page, rect, "Square", color=color, fill=fill, width=width, opacity=opacity, author=author,
                  subject=subject, contents=contents, created=created, modified=modified, dash=dash)


def add_circle(pdf: pikepdf.Pdf, page: int | pikepdf.Page, rect: Rect | Sequence[float], *, color: Any = "red",
               fill: Any = None, width: float = 1.5, opacity: float = 1.0, author: str | None = None,
               subject: str | None = None, contents: str | None = None, created: Any = None, modified: Any = None,
               dash: Sequence[float] | None = None) -> Dictionary:
    """Ellipse (/Circle) inscribed in *rect*."""
    return _shape(pdf, page, rect, "Circle", color=color, fill=fill, width=width, opacity=opacity, author=author,
                  subject=subject, contents=contents, created=created, modified=modified, dash=dash)


def add_line(pdf: pikepdf.Pdf, page: int | pikepdf.Page, start: Sequence[float], end: Sequence[float], *,
             heads: Sequence[str] = ("None", "OpenArrow"), color: Any = "red", fill: Any = None, width: float = 1.5,
             opacity: float = 1.0, author: str | None = None, subject: str | None = None,
             contents: str | None = None, created: Any = None, modified: Any = None) -> Dictionary:
    """Line with line endings (/LE). *heads* = (start style, end style), e.g. ("None", "ClosedArrow")."""
    pg = _page(pdf, page)
    g = PageGeom(pg)
    col = parse_color(color) or NAMED_COLORS["red"]
    fl = parse_color(fill) or col
    a, b = g.d(*_as_pt(start)), g.d(*_as_pt(end))
    hs = [h if h in HEADS else "None" for h in (list(heads) + ["None", "None"])[:2]]
    size = max(7.0, 5 * width)
    bbox = g.user_bbox([a, b], size + width + 1)
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    ap.add(_rg(col, True), f"{_f(width)} w 1 j 0 J")
    ap.add(f"{_f(a[0])} {_f(a[1])} m {_f(b[0])} {_f(b[1])} l S")
    ap.add(*_head(hs[0], a, _unit(b, a), size, fl, width))
    ap.add(*_head(hs[1], b, _unit(a, b), size, fl, width))
    d = _common(pdf, pg, "Line", bbox, color=col, opacity=opacity, author=author, subject=subject, contents=contents,
                created=created, modified=modified)
    d.L = Array([float(v) for v in (*g.user(*a), *g.user(*b))])
    d.LE = Array([Name("/" + h) for h in hs])
    if any(h in ("ClosedArrow", "RClosedArrow", "Circle", "Square", "Diamond") for h in hs):
        d.IC = Array([float(v) for v in fl])
    _bs(d, width)
    return _attach(pdf, pg, d, ap.build(bbox))


def _signed_area(pts: Sequence[Pt]) -> float:
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))


def cloud_path(pts: Sequence[Pt], diameter: float) -> str:
    """Scalloped closed path (y-up drawing space) around polygon *pts*: semicircular bumps pointing outward."""
    if len(pts) < 3:
        raise ValueError("cloud needs at least 3 points")
    ccw = _signed_area(pts) > 0
    k = 0.5523
    out = [f"{_f(pts[0][0])} {_f(pts[0][1])} m"]
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < 1e-6:
            continue
        n_arcs = max(1, round(L / diameter))
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        nx, ny = (uy, -ux) if ccw else (-uy, ux)
        r = L / n_arcs / 2
        for j in range(n_arcs):
            ax, ay = a[0] + ux * 2 * r * j, a[1] + uy * 2 * r * j
            mx, my = ax + ux * r, ay + uy * r
            px, py = mx + nx * r, my + ny * r
            bx, by = ax + ux * 2 * r, ay + uy * 2 * r
            out.append(f"{_f(ax + nx * k * r)} {_f(ay + ny * k * r)} {_f(px - ux * k * r)} {_f(py - uy * k * r)} {_f(px)} {_f(py)} c")
            out.append(f"{_f(px + ux * k * r)} {_f(py + uy * k * r)} {_f(bx + nx * k * r)} {_f(by + ny * k * r)} {_f(bx)} {_f(by)} c")
    out.append("h")
    return " ".join(out)


def _poly(pdf, page, points, subtype: str, *, color, fill, width, opacity, author, subject, contents, created,
          modified, heads=None, cloud: float | None = None, scallop: float | None = None) -> Dictionary:
    pg = _page(pdf, page)
    g = PageGeom(pg)
    pts = [g.d(*_as_pt(p)) for p in points]
    if len(pts) < 2 or (subtype == "Polygon" and len(pts) < 3):
        raise ValueError(f"{subtype} needs more points")
    col = parse_color(color) or NAMED_COLORS["red"]
    fl = parse_color(fill)
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    ap.add(_rg(col, True), f"{_f(width)} w 1 j 1 J")
    if fl is not None:
        ap.add(_rg(fl))
    pad = width + 1
    if cloud is not None:
        diam = scallop or max(6.0, 10.0 * max(cloud, 0.3)) + 2 * width
        ap.add(cloud_path(pts, diam), _paint(fl))
        pad += diam / 2 + 1
    else:
        path = f"{_f(pts[0][0])} {_f(pts[0][1])} m " + " ".join(f"{_f(u)} {_f(v)} l" for u, v in pts[1:])
        if subtype == "Polygon":
            ap.add(path + " h", _paint(fl))
        else:
            ap.add(path, "S")
            hs = [h if h in HEADS else "None" for h in (list(heads or []) + ["None", "None"])[:2]]
            size = max(7.0, 5 * width)
            ap.add(*_head(hs[0], pts[0], _unit(pts[1], pts[0]), size, fl or col, width))
            ap.add(*_head(hs[1], pts[-1], _unit(pts[-2], pts[-1]), size, fl or col, width))
            pad += size
    bbox = g.user_bbox(pts, pad)
    d = _common(pdf, pg, subtype, bbox, color=col, opacity=opacity, author=author, subject=subject, contents=contents,
                created=created, modified=modified)
    verts: list[float] = []
    for p in pts:
        verts.extend(g.user(*p))
    d.Vertices = Array([float(v) for v in verts])
    if fl is not None:
        d.IC = Array([float(v) for v in fl])
    _bs(d, width)
    if cloud is not None:
        d.BE = Dictionary(S=Name.C, I=float(cloud))
        d.IT = Name.PolygonCloud
    if subtype == "PolyLine":
        hs = [h if h in HEADS else "None" for h in (list(heads or []) + ["None", "None"])[:2]]
        d.LE = Array([Name("/" + h) for h in hs])
    return _attach(pdf, pg, d, ap.build(bbox))


def add_polygon(pdf: pikepdf.Pdf, page: int | pikepdf.Page, points: Sequence[Sequence[float]], *, color: Any = "red",
                fill: Any = None, width: float = 1.5, opacity: float = 1.0, author: str | None = None,
                subject: str | None = None, contents: str | None = None, created: Any = None,
                modified: Any = None) -> Dictionary:
    return _poly(pdf, page, points, "Polygon", color=color, fill=fill, width=width, opacity=opacity, author=author,
                 subject=subject, contents=contents, created=created, modified=modified)


def add_polyline(pdf: pikepdf.Pdf, page: int | pikepdf.Page, points: Sequence[Sequence[float]], *,
                 heads: Sequence[str] = ("None", "None"), color: Any = "red", fill: Any = None, width: float = 1.5,
                 opacity: float = 1.0, author: str | None = None, subject: str | None = None,
                 contents: str | None = None, created: Any = None, modified: Any = None) -> Dictionary:
    return _poly(pdf, page, points, "PolyLine", color=color, fill=fill, width=width, opacity=opacity, author=author,
                 subject=subject, contents=contents, created=created, modified=modified, heads=heads)


def add_cloud(pdf: pikepdf.Pdf, page: int | pikepdf.Page, shape: Rect | Sequence[Any], *, intensity: float = 1.0,
              scallop: float | None = None, color: Any = None, fill: Any = None, width: float = 1.5,
              opacity: float = 1.0, author: str | None = None, subject: str | None = None,
              contents: str | None = None, created: Any = None, modified: Any = None) -> Dictionary:
    """Revision cloud: /Polygon with /BE <</S /C /I intensity>> and a scalloped appearance.

    *shape* is a Rect / (x0, top, x1, bottom) or a list of (x, y) vertices. *scallop* = bump diameter in points
    (default from intensity: 10 pt × intensity + 2 × width).
    """
    if isinstance(shape, Rect) or (len(shape) == 4 and all(isinstance(v, (int, float)) for v in shape)):
        r = _as_rect(shape)  # type: ignore[arg-type]
        pts = [(r.x0, r.top), (r.x1, r.top), (r.x1, r.bottom), (r.x0, r.bottom)]
    else:
        pts = [_as_pt(p) for p in shape]
    return _poly(pdf, page, pts, "Polygon", color=parse_color(color) or DEFAULT_COLOR["cloud"], fill=fill, width=width,
                 opacity=opacity, author=author, subject=subject, contents=contents, created=created,
                 modified=modified, cloud=float(intensity), scallop=scallop)


def _smooth(pts: Sequence[Pt]) -> str:
    """Catmull-Rom → cubic Bézier through all points."""
    if len(pts) == 1:
        u, v = pts[0]
        return f"{_f(u)} {_f(v)} m {_f(u + 0.01)} {_f(v)} l"
    out = [f"{_f(pts[0][0])} {_f(pts[0][1])} m"]
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        out.append(f"{_f(c1[0])} {_f(c1[1])} {_f(c2[0])} {_f(c2[1])} {_f(p2[0])} {_f(p2[1])} c")
    return " ".join(out)


def add_ink(pdf: pikepdf.Pdf, page: int | pikepdf.Page, strokes: Sequence[Sequence[Sequence[float]]], *,
            color: Any = "blue", width: float = 2.0, opacity: float = 1.0, author: str | None = None,
            subject: str | None = None, contents: str | None = None, created: Any = None,
            modified: Any = None, smooth: bool = True) -> Dictionary:
    """Freehand ink: one or more strokes, each a list of (x, y) points."""
    pg = _page(pdf, page)
    g = PageGeom(pg)
    col = parse_color(color) or NAMED_COLORS["blue"]
    sts = [[g.d(*_as_pt(p)) for p in s] for s in strokes if len(s)]
    if not sts:
        raise ValueError("ink needs at least one stroke")
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    ap.add(_rg(col, True), f"{_f(width)} w 1 J 1 j")
    for s in sts:
        ap.add((_smooth(s) if smooth else f"{_f(s[0][0])} {_f(s[0][1])} m " + " ".join(f"{_f(u)} {_f(v)} l" for u, v in s[1:])) + " S")
    bbox = g.user_bbox([p for s in sts for p in s], width + 2)
    d = _common(pdf, pg, "Ink", bbox, color=col, opacity=opacity, author=author, subject=subject, contents=contents,
                created=created, modified=modified)
    d.InkList = Array([Array([float(v) for p in s for v in g.user(*p)]) for s in sts])
    _bs(d, width)
    return _attach(pdf, pg, d, ap.build(bbox))


def _stamp_name(label: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]", "", label.title())
    for s in STD_STAMPS:
        if s.lower() == key.lower():
            return s
    return re.sub(r"[^A-Za-z0-9_]", "_", label.strip()) or "Stamp"


def add_stamp(pdf: pikepdf.Pdf, page: int | pikepdf.Page, label: str = "APPROVED", *,
              at: Sequence[float] | None = None, rect: Rect | Sequence[float] | None = None,
              sublabel: str | None = None, font_size: float = 20.0, color: Any = None, width: float = 2.5,
              opacity: float = 0.85, author: str | None = None, subject: str | None = None,
              contents: str | None = None, created: Any = None, modified: Any = None) -> Dictionary:
    """Rubber stamp: boxed label (APPROVED, REJECTED, …) with an optional small second line.

    Give *rect*, or *at* (top-left corner; size from the text). Default sub-line: "author · date".
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    from pdfcore.fonts import register_reportlab

    register_reportlab()
    pg = _page(pdf, page)
    g = PageGeom(pg)
    label = label.strip() or "APPROVED"
    col = parse_color(color) or _STAMP_COLORS.get(label.upper(), (0.1, 0.3, 0.7))
    if sublabel is None:
        when = (parse_pdf_date(pdf_date(created)) or datetime.now()).strftime("%Y-%m-%d")
        sublabel = f"{author} · {when}" if author else when
    sub_size = font_size * 0.42
    if rect is None:
        x0, t0 = _as_pt(at) if at is not None else (36.0, 36.0)
        tw = max(stringWidth(label, "Inter-Bold", font_size), stringWidth(sublabel, "Inter", sub_size) if sublabel else 0)
        hh = font_size * 1.25 + (sub_size * 1.5 if sublabel else 0) + 2 * width + 8
        r = Rect(x0, t0, x0 + tw + 2 * width + 20, t0 + hh)
    else:
        r = _as_rect(rect)
    x, y, w, h = g.drect(r)
    bbox = g.user_bbox([(x, y), (x + w, y + h)])
    ap = _AP(pdf, g)
    ap.gstate(opacity)
    rad = min(6.0, h / 4)
    ap.add(_rg(col, True), f"{_f(width)} w 1 j")
    ap.add(_rounded(x + width / 2, y + width / 2, w - width, h - width, rad), "S")
    inner = width + 2.5
    ap.add(f"{_f(max(0.5, width / 3))} w", _rounded(x + inner, y + inner, w - 2 * inner, h - 2 * inner, max(0.0, rad - 2.5)), "S")

    def draw(c):
        c.setFillColorRGB(*col)
        fs = font_size
        while stringWidth(label, "Inter-Bold", fs) > w - 2 * inner - 6 and fs > 6:
            fs -= 0.5
        total = fs * 0.72 + ((sub_size * 1.45) if sublabel else 0)
        base = (h - total) / 2 + ((sub_size * 1.45) if sublabel else 0)
        c.setFont("Inter-Bold", fs)
        c.drawCentredString(w / 2, base, label)
        if sublabel:
            ss = sub_size
            while stringWidth(sublabel, "Inter", ss) > w - 2 * inner - 6 and ss > 4:
                ss -= 0.25
            c.setFont("Inter", ss)
            c.drawCentredString(w / 2, base - sub_size * 1.45, sublabel)

    ap.form(rl_form(pdf, w, h, draw), x, y)
    d = _common(pdf, pg, "Stamp", bbox, color=col, opacity=opacity, author=author,
                subject=subject if subject is not None else label, contents=contents, created=created, modified=modified)
    d.Name = Name("/" + _stamp_name(label))
    return _attach(pdf, pg, d, ap.build(bbox))


def _rounded(x: float, y: float, w: float, h: float, r: float) -> str:
    if r <= 0.01:
        return f"{_f(x)} {_f(y)} {_f(w)} {_f(h)} re"
    k = 0.5523 * r
    return (f"{_f(x + r)} {_f(y)} m {_f(x + w - r)} {_f(y)} l {_f(x + w - r + k)} {_f(y)} {_f(x + w)} {_f(y + r - k)} {_f(x + w)} {_f(y + r)} c "
            f"{_f(x + w)} {_f(y + h - r)} l {_f(x + w)} {_f(y + h - r + k)} {_f(x + w - r + k)} {_f(y + h)} {_f(x + w - r)} {_f(y + h)} c "
            f"{_f(x + r)} {_f(y + h)} l {_f(x + r - k)} {_f(y + h)} {_f(x)} {_f(y + h - r + k)} {_f(x)} {_f(y + h - r)} c "
            f"{_f(x)} {_f(y + r)} l {_f(x)} {_f(y + r - k)} {_f(x + r - k)} {_f(y)} {_f(x + r)} {_f(y)} c h")


# ============================================================== spec dispatcher
_COMMON_KEYS = ("color", "opacity", "width", "author", "subject", "contents", "created", "modified")


def _pick(spec: dict, *keys: str) -> dict:
    return {k: spec[k] for k in keys if k in spec and spec[k] is not None}


def add_annotation(pdf: pikepdf.Pdf, spec: dict) -> list[Dictionary]:
    """Create annotation(s) from a spec dict (see module docstring). Returns the new annotation dicts.

    A text-markup spec with ``query`` (and no rects) creates one annotation per search hit; that needs the
    PDF on disk, so pass ``spec["_path"]`` (``annotate`` does this for you).
    """
    kind = str(spec.get("type", "")).lower()
    if kind not in KINDS:
        raise ValueError(f"unknown annotation type {spec.get('type')!r}; choose from {sorted(KINDS)}")
    page = int(spec.get("page", 0))
    if not 0 <= page < len(pdf.pages):
        raise ValueError(f"page index {page} out of range (document has {len(pdf.pages)} pages; JSON pages are 0-based)")
    c = _pick(spec, *_COMMON_KEYS)
    if kind in ("highlight", "underline", "strikeout", "squiggly"):
        if spec.get("rects"):
            return [add_text_markup(pdf, page, spec["rects"], kind, **c)]
        if spec.get("rect"):
            return [add_text_markup(pdf, page, [spec["rect"]], kind, **c)]
        raise ValueError("text markup needs 'rects' or 'query'")
    if kind == "note":
        c.pop("width", None)
        return [add_note(pdf, page, spec.get("at") or spec["rect"][:2], spec.get("contents") or spec.get("text", ""),
                         **{k: v for k, v in c.items() if k != "contents"}, **_pick(spec, "icon", "size"))]
    if kind == "freetext":
        return [add_free_text(pdf, page, spec["rect"], spec.get("text") or spec.get("contents", ""), **c,
                              **_pick(spec, "font_size", "text_color", "fill", "align", "bold"))]
    if kind == "callout":
        return [add_callout(pdf, page, spec["rect"], spec.get("text") or spec.get("contents", ""), spec["target"], **c,
                            **_pick(spec, "knee", "head", "font_size", "text_color", "fill", "align"))]
    if kind in ("square", "circle"):
        fn = add_square if kind == "square" else add_circle
        return [fn(pdf, page, spec["rect"], **c, **_pick(spec, "fill", "dash"))]
    if kind == "line":
        p = spec["points"]
        return [add_line(pdf, page, p[0], p[-1], **c, **_pick(spec, "heads", "fill"))]
    if kind == "polygon":
        return [add_polygon(pdf, page, spec["points"], **c, **_pick(spec, "fill"))]
    if kind == "polyline":
        return [add_polyline(pdf, page, spec["points"], **c, **_pick(spec, "heads", "fill"))]
    if kind == "cloud":
        return [add_cloud(pdf, page, spec.get("points") or spec["rect"], **c, **_pick(spec, "intensity", "scallop", "fill"))]
    if kind == "ink":
        return [add_ink(pdf, page, spec["strokes"], **c)]
    if kind == "stamp":
        return [add_stamp(pdf, page, spec.get("label") or spec.get("text") or "APPROVED", **c,
                          **_pick(spec, "at", "rect", "sublabel", "font_size"))]
    raise AssertionError(kind)


def _merge_columns(rects: Sequence[Rect]) -> list[Rect]:
    """Merge per-character rects of vertical text (search returns one rect per char there) into columns."""
    out: list[Rect] = []
    for r in sorted(rects, key=lambda r: (round(r.x0), r.top)):
        if out:
            last = out[-1]
            char_like = r.width <= 6 * max(r.height, 0.1)
            same_col = abs(r.x0 - last.x0) < 2 and abs(r.x1 - last.x1) < 2
            gap = r.top - last.bottom
            if char_like and same_col and -0.5 * r.height <= gap <= max(r.height, 2):
                out[-1] = Rect(last.x0, last.top, max(last.x1, r.x1), max(last.bottom, r.bottom))
                continue
        out.append(r)
    return out if len(out) < len(rects) else list(rects)


def _expand_queries(src: Path, specs: Sequence[dict], password: str | None) -> list[dict]:
    """Turn {"type": "highlight", "query": ...} into one spec per hit."""
    from pdfcore import text

    out: list[dict] = []
    for s in specs:
        if s.get("query") and not s.get("rects") and str(s.get("type", "highlight")).lower() in (
                "highlight", "underline", "strikeout", "squiggly"):
            pages = s.get("pages")
            if pages is None and "page" in s:
                pages = [int(s["page"])]
            hits = text.search(src, s["query"], regex=bool(s.get("regex")), case=bool(s.get("case")),
                               whole_word=bool(s.get("whole_word")), pages=pages, password=password)
            for h in hits:
                ns = {k: v for k, v in s.items() if k not in ("query", "regex", "case", "whole_word", "pages")}
                rs = _merge_columns(h.rects)
                ns.update(type=s.get("type", "highlight"), page=h.page, rects=[[r.x0, r.top, r.x1, r.bottom] for r in rs])
                ns.setdefault("contents", h.text)
                out.append(ns)
        else:
            out.append(dict(s))
    return out


def _open(src: Path, password: str | None) -> pikepdf.Pdf:
    return pikepdf.open(str(src), password=password or "")


def annotate(src: str | Path, specs: Sequence[dict] | dict, *, out: str | Path | None = None,
             password: str | None = None, options=None, quiet: bool = False, tag: str = "annotated") -> OpResult:
    """Add annotations described by *specs* to a copy of *src*. data: added, by_type, pages, skipped."""
    src = Path(src)
    specs = [specs] if isinstance(specs, dict) else list(specs)
    expanded = _expand_queries(src, specs, password)
    dst = output_path(src, tag, out=out)
    by_type: dict[str, int] = {}
    pages: set[int] = set()
    skipped: list[str] = []
    with _open(src, password) as pdf:
        for s in expanded:
            try:
                made = add_annotation(pdf, s)
            except (KeyError, IndexError) as exc:
                skipped.append(f"{s.get('type')}: missing {exc}")
                continue
            t = str(s["type"]).lower()
            by_type[t] = by_type.get(t, 0) + len(made)
            pages.add(int(s.get("page", 0)))
        pdf.save(str(dst))
    n = sum(by_type.values())
    return finalize(dst, baseline=src, options=options, quiet=quiet,
                    data={"added": n, "by_type": by_type, "pages": sorted(pages), "skipped": skipped})


def highlight(src: str | Path, query: str | None = None, *, rects: Sequence[Rect | Sequence[float]] | None = None,
              page: int = 0, kind: str = "highlight", regex: bool = False, case: bool = False,
              whole_word: bool = False, pages: Sequence[int] | None = None, out: str | Path | None = None,
              password: str | None = None, quiet: bool = False, **common) -> OpResult:
    """Highlight (or underline/strikeout/squiggly) every match of *query*, or the given *rects* on *page*."""
    if query:
        spec = {"type": kind, "query": query, "regex": regex, "case": case, "whole_word": whole_word, **common}
        if pages is not None:
            spec["pages"] = list(pages)
    elif rects:
        spec = {"type": kind, "page": page, "rects": [list(_astuple(r)) for r in rects], **common}
    else:
        raise ValueError("give a query or rects")
    return annotate(src, [spec], out=out, password=password, quiet=quiet, tag=kind)


def _astuple(r) -> tuple[float, float, float, float]:
    r = _as_rect(r)
    return (r.x0, r.top, r.x1, r.bottom)


# ============================================================== reading annotations
def _num_list(a) -> list[float]:
    try:
        return [float(v) for v in a]
    except Exception:
        return []


def _str(v) -> str:
    if v is None:
        return ""
    try:
        return str(v)
    except Exception:
        return ""


def _iso(v) -> str | None:
    dt = parse_pdf_date(_str(v)) if v is not None else None
    return dt.isoformat() if dt else None


def _width_of(a) -> float | None:
    bs = a.get("/BS")
    if isinstance(bs, Dictionary) and "/W" in bs:
        return float(bs.W)
    b = a.get("/Border")
    if isinstance(b, Array) and len(b) >= 3:
        return float(b[2])
    return None


def _da_colors(da: str) -> tuple[RGB | None, RGB | None, float | None]:
    """(text rg colour, border RG colour, font size) from a /DA string."""
    def grab(op):
        m = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+" + op + r"\b", da)
        if m:
            return tuple(float(v) for v in m.groups())
        m = re.search(r"([\d.]+)\s+" + op.lower().replace("rg", "g") + r"\b", da)  # gray
        return (float(m.group(1)),) * 3 if m else None

    size = re.search(r"([\d.]+)\s+Tf", da)
    return grab("rg"), grab("RG"), float(size.group(1)) if size else None  # type: ignore[return-value]


def _info(a: Dictionary, g: PageGeom, pi: int, idx: int) -> dict:
    sub = _str(a.get("/Subtype")).lstrip("/")
    kind = {v: k for k, v in KINDS.items() if k not in ("callout", "cloud")}.get(sub, sub.lower())
    rect_u = _num_list(a.get("/Rect", [0, 0, 0, 0]))
    r = g.rect_from_user(*rect_u) if len(rect_u) == 4 else Rect(0, 0, 0, 0)
    col = parse_color(_num_list(a.get("/C"))) if a.get("/C") is not None and len(_num_list(a.get("/C"))) else None
    info: dict[str, Any] = {
        "type": kind, "subtype": sub, "page": pi, "index": idx, "rect": [round(v, 2) for v in _astuple(r)],
        "color": color_hex(col), "opacity": float(a.get("/CA", 1.0)), "author": _str(a.get("/T")),
        "subject": _str(a.get("/Subj")), "contents": _str(a.get("/Contents")),
        "created": _iso(a.get("/CreationDate")), "modified": _iso(a.get("/M")), "name": _str(a.get("/NM")),
    }
    w = _width_of(a)
    if w is not None:
        info["width"] = w
    if a.get("/IC") is not None and _num_list(a.IC):
        info["fill"] = color_hex(parse_color(_num_list(a.IC)))

    def vis(pts: list[float]) -> list[list[float]]:
        return [[round(c, 2) for c in g.visual(pts[i], pts[i + 1])] for i in range(0, len(pts) - 1, 2)]

    if sub in ("Highlight", "Underline", "StrikeOut", "Squiggly"):
        q = _num_list(a.get("/QuadPoints", []))
        rects = []
        for i in range(0, len(q) - 7, 8):
            p = vis(q[i:i + 8])
            xs, ys = [c[0] for c in p], [c[1] for c in p]
            rects.append([min(xs), min(ys), max(xs), max(ys)])
        info["rects"] = rects or [info["rect"]]
    elif sub == "Text":
        info["at"] = [info["rect"][0], info["rect"][1]]
        info["icon"] = _str(a.get("/Name")).lstrip("/") or "Note"
    elif sub == "FreeText":
        tc, border, size = _da_colors(_str(a.get("/DA")))
        info["text"] = info["contents"]
        info["fill"] = info.pop("color")
        info["color"] = color_hex(border) if border else None
        if tc:
            info["text_color"] = color_hex(tc)
        if size:
            info["font_size"] = size
        info["align"] = {0: "left", 1: "center", 2: "right"}.get(int(a.get("/Q", 0)), "left")
        rd = _num_list(a.get("/RD", []))
        if _str(a.get("/IT")) == "/FreeTextCallout" or a.get("/CL") is not None:
            info["type"] = "callout"
            cl = vis(_num_list(a.get("/CL", [])))
            if cl:
                info["target"] = cl[0]
                if len(cl) == 3:
                    info["knee"] = cl[1]
            le = a.get("/LE")
            info["head"] = _str(le[0] if isinstance(le, Array) and len(le) else le).lstrip("/") or "None"
        if len(rd) == 4 and len(rect_u) == 4:
            box = g.rect_from_user(rect_u[0] + rd[0], rect_u[1] + rd[1], rect_u[2] - rd[2], rect_u[3] - rd[3])
            info["rect"] = [round(v, 2) for v in _astuple(box)]
    elif sub in ("Square", "Circle"):
        rd = _num_list(a.get("/RD", []))
        if len(rd) == 4:
            box = g.rect_from_user(rect_u[0] + rd[0], rect_u[1] + rd[1], rect_u[2] - rd[2], rect_u[3] - rd[3])
            info["rect"] = [round(v, 2) for v in _astuple(box)]
        be = a.get("/BE")
        if isinstance(be, Dictionary) and _str(be.get("/S")) == "/C":
            info["type"] = "cloud"
            info["intensity"] = float(be.get("/I", 1))
    elif sub == "Line":
        info["points"] = vis(_num_list(a.get("/L", [])))
        info["heads"] = [_str(h).lstrip("/") for h in (a.get("/LE") or [])] or ["None", "None"]
    elif sub in ("Polygon", "PolyLine"):
        info["points"] = vis(_num_list(a.get("/Vertices", [])))
        be = a.get("/BE")
        if isinstance(be, Dictionary) and _str(be.get("/S")) == "/C":
            info["type"] = "cloud"
            info["intensity"] = float(be.get("/I", 1))
        if sub == "PolyLine":
            info["heads"] = [_str(h).lstrip("/") for h in (a.get("/LE") or [])] or ["None", "None"]
    elif sub == "Ink":
        info["strokes"] = [vis(_num_list(s)) for s in (a.get("/InkList") or [])]
    elif sub == "Stamp":
        name = _str(a.get("/Name")).lstrip("/")
        std = {s: re.sub(r"(?<!^)([A-Z])", r" \1", s).upper() for s in STD_STAMPS}
        info["label"] = info["subject"] or std.get(name, name.replace("_", " "))
    return info


def list_annotations(src: str | Path, *, pages: Sequence[int] | None = None, types: Iterable[str] | None = None,
                     include_all: bool = False, password: str | None = None) -> list[dict]:
    """Every comment annotation as a spec-compatible dict (page 0-based, rect in our top-left convention).

    Links, form widgets and popups are skipped unless *include_all*. *types* filters by our kind names
    (highlight, note, cloud, …) or PDF subtypes (Highlight, Text, …), case-insensitive.
    """
    want = {t.lower() for t in types} if types else None
    out: list[dict] = []
    with _open(Path(src), password) as pdf:
        idx = range(len(pdf.pages)) if pages is None else pages
        for pi in idx:
            pg = pdf.pages[pi]
            g = PageGeom(pg)
            for k, a in enumerate(pg.obj.get("/Annots") or []):
                if not isinstance(a, Dictionary):
                    continue
                sub = _str(a.get("/Subtype")).lstrip("/")
                if not include_all and sub not in COMMENT_SUBTYPES:
                    continue
                info = _info(a, g, pi, k)
                if want and info["type"] not in want and sub.lower() not in want:
                    continue
                out.append(info)
    return out


def _matches(info: dict, *, types, authors, contains, names) -> bool:
    if types and info["type"] not in types and info["subtype"].lower() not in types:
        return False
    if authors and info["author"].lower() not in authors:
        return False
    if contains and contains.lower() not in (info["contents"] + " " + info["subject"]).lower():
        return False
    if names and info["name"] not in names:
        return False
    return True


def delete_annotations(src: str | Path, *, types: Iterable[str] | None = None, authors: Iterable[str] | None = None,
                       pages: Sequence[int] | None = None, contains: str | None = None,
                       names: Iterable[str] | None = None, out: str | Path | None = None,
                       password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Delete comment annotations matching every given filter (no filter = all comments). Popups go with them."""
    src = Path(src)
    tset = {t.lower() for t in types} if types else None
    aset = {a.lower() for a in authors} if authors else None
    nset = set(names) if names else None
    dst = output_path(src, "annots-deleted", out=out)
    removed: dict[str, int] = {}
    with _open(src, password) as pdf:
        idx = range(len(pdf.pages)) if pages is None else pages
        for pi in idx:
            pg = pdf.pages[pi]
            g = PageGeom(pg)
            annots = pg.obj.get("/Annots")
            if not annots:
                continue
            kill: set[tuple[int, int]] = set()
            kill_idx: set[int] = set()
            for k, a in enumerate(annots):
                if not isinstance(a, Dictionary):
                    continue
                sub = _str(a.get("/Subtype")).lstrip("/")
                if sub not in COMMENT_SUBTYPES:
                    continue
                info = _info(a, g, pi, k)
                if _matches(info, types=tset, authors=aset, contains=contains, names=nset):
                    kill_idx.add(k)
                    if a.is_indirect:
                        kill.add(a.objgen)
                    removed[info["type"]] = removed.get(info["type"], 0) + 1
            keep = Array()
            for k, a in enumerate(annots):
                if k in kill_idx:
                    continue
                if isinstance(a, Dictionary) and _str(a.get("/Subtype")) == "/Popup":
                    par = a.get("/Parent")
                    if par is not None and par.is_indirect and par.objgen in kill:
                        continue
                keep.append(a)
            if len(keep):
                pg.obj.Annots = pdf.make_indirect(keep)
            else:
                del pg.obj["/Annots"]
        pdf.save(str(dst))
    return finalize(dst, baseline=src, options=options, quiet=quiet,
                    data={"removed": sum(removed.values()), "by_type": removed})


# ============================================================== flatten
def _apply(m: Sequence[float], x: float, y: float) -> Pt:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def flatten_matrix(rect: Sequence[float], bbox: Sequence[float], matrix: Sequence[float]) -> tuple[float, ...]:
    """Matrix A (ISO 32000-2 §12.5.5) mapping the form's transformed BBox onto the annotation Rect."""
    pts = [_apply(matrix, x, y) for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3]))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
    rx0, ry0, rx1, ry1 = min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3])
    sx = (rx1 - rx0) / (bx1 - bx0) if bx1 - bx0 > 1e-9 else 1.0
    sy = (ry1 - ry0) / (by1 - by0) if by1 - by0 > 1e-9 else 1.0
    return (sx, 0.0, 0.0, sy, rx0 - bx0 * sx, ry0 - by0 * sy)


def _normal_ap(a: Dictionary) -> pikepdf.Stream | None:
    ap = a.get("/AP")
    if not isinstance(ap, Dictionary):
        return None
    n = ap.get("/N")
    if isinstance(n, pikepdf.Stream):
        return n
    if isinstance(n, Dictionary):
        st = a.get("/AS")
        if st is not None and st in n and isinstance(n[st], pikepdf.Stream):
            return n[st]
    return None


def flatten_annotations(src: str | Path, *, types: Iterable[str] | None = None, pages: Sequence[int] | None = None,
                        out: str | Path | None = None, password: str | None = None, options=None,
                        quiet: bool = False) -> OpResult:
    """Merge comment annotations' appearance streams into page content and remove the annotations.

    Hidden / no-view annotations and annotations without an appearance are left as they are
    (counted in data['kept']). Links and form widgets are never touched (use the forms op for widgets).
    """
    src = Path(src)
    tset = {t.lower() for t in types} if types else None
    dst = output_path(src, "flattened", out=out)
    done, kept = 0, 0
    with _open(src, password) as pdf:
        idx = range(len(pdf.pages)) if pages is None else pages
        for pi in idx:
            pg = pdf.pages[pi]
            g = PageGeom(pg)
            annots = pg.obj.get("/Annots")
            if not annots:
                continue
            ops: list[str] = []
            gone: set[int] = set()
            removed_objs: set[tuple[int, int]] = set()
            res = pg.obj.get("/Resources")
            if res is None:
                pg.obj.Resources = Dictionary()
                res = pg.obj.Resources
            if "/XObject" not in res:
                res.XObject = Dictionary()
            if "/ExtGState" not in res:
                res.ExtGState = Dictionary()
            for k, a in enumerate(annots):
                if not isinstance(a, Dictionary):
                    continue
                sub = _str(a.get("/Subtype")).lstrip("/")
                if sub not in COMMENT_SUBTYPES:
                    continue
                if tset:
                    info = _info(a, g, pi, k)
                    if info["type"] not in tset and sub.lower() not in tset:
                        continue
                flags = int(a.get("/F", 0))
                form = _normal_ap(a)
                if flags & (FLAG_HIDDEN | FLAG_NOVIEW) or form is None:
                    kept += 1
                    continue
                if "/Subtype" not in form:
                    form.Subtype = Name.Form
                form.Type = Name.XObject
                bbox = _num_list(form.get("/BBox", [0, 0, 0, 0]))
                mtx = _num_list(form.get("/Matrix", [1, 0, 0, 1, 0, 0])) or [1, 0, 0, 1, 0, 0]
                A = flatten_matrix(_num_list(a.Rect), bbox, mtx)
                n = 0
                while Name(f"/PdfStudioAnnot{n}") in res.XObject:
                    n += 1
                nm = f"/PdfStudioAnnot{n}"
                res.XObject[Name(nm)] = form
                gs = ""
                ca = float(a.get("/CA", 1.0))
                fres = form.get("/Resources")
                if ca < 0.999 and not (isinstance(fres, Dictionary) and "/ExtGState" in fres):
                    gname = f"/PdfStudioGS{n}"
                    res.ExtGState[Name(gname)] = Dictionary(Type=Name.ExtGState, CA=ca, ca=ca)
                    gs = f"{gname} gs "
                ops.append(f"q {gs}{' '.join(_f(v) for v in A)} cm {nm} Do Q")
                gone.add(k)
                if a.is_indirect:
                    removed_objs.add(a.objgen)
                done += 1
            if ops:
                pg.contents_coalesce()
                data = pg.obj.Contents.read_bytes()
                pg.obj.Contents = pdf.make_stream(b"q\n" + data + b"\nQ\n" + ("\n".join(ops) + "\n").encode())
                keep = Array()
                for k, a in enumerate(annots):
                    if k in gone:
                        continue
                    if isinstance(a, Dictionary) and _str(a.get("/Subtype")) == "/Popup":
                        par = a.get("/Parent")
                        if par is not None and par.is_indirect and par.objgen in removed_objs:
                            continue
                    keep.append(a)
                if len(keep):
                    pg.obj.Annots = pdf.make_indirect(keep)
                else:
                    del pg.obj["/Annots"]
        pdf.save(str(dst))
    return finalize(dst, baseline=src, options=options, quiet=quiet, data={"flattened": done, "kept": kept})


# ============================================================== import / export
_EXPORT_DROP = ("subtype", "index", "name")


def export_annotations(src: str | Path, *, out: str | Path | None = None, password: str | None = None,
                       quiet: bool = False) -> OpResult:
    """Write every comment as JSON ({"source", "annotations": [spec, …]}). The JSON can be re-imported."""
    src = Path(src)
    items = list_annotations(src, password=password)
    dst = output_path(src, "annots", ext=".json", out=out)
    dst.write_text(json.dumps({"source": src.name, "pages_are": "0-based", "annotations": items}, indent=2,
                              ensure_ascii=False), encoding="utf-8")
    if not quiet:
        print(f"{dst}  {len(items)} annotation(s)")
    return OpResult(dst, None, [], {"count": len(items)})


def import_annotations(src: str | Path, data: str | Path | Sequence[dict] | dict, *, out: str | Path | None = None,
                       password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Recreate annotations from an export (or any list of specs) onto a copy of *src*, with fresh appearances."""
    if isinstance(data, (str, Path)):
        data = json.loads(Path(data).read_text(encoding="utf-8"))
    items = data["annotations"] if isinstance(data, dict) and "annotations" in data else data
    specs = []
    for it in items:  # type: ignore[union-attr]
        if str(it.get("type", "")).lower() not in KINDS:
            continue
        s = {k: v for k, v in it.items() if k not in _EXPORT_DROP}
        if s.get("type") in ("highlight", "underline", "strikeout", "squiggly"):
            s.pop("width", None)
        if s.get("type") == "note":
            s.pop("rect", None)
        specs.append(s)
    res = annotate(src, specs, out=out, password=password, options=options, quiet=quiet, tag="imported")
    res.data["ignored"] = len(items) - len(specs)  # type: ignore[arg-type]
    return res


# ============================================================== comment summary
def comment_summary(src: str | Path, *, out: str | Path | None = None, csv_out: str | Path | None = None,
                    password: str | None = None, dpi: float = 110, include_csv: bool = True, options=None,
                    quiet: bool = False) -> OpResult:
    """PDF report of every comment, grouped by page, with a thumbnail of each marked-up area; plus CSV."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    from pdfcore import engines
    from pdfcore.fonts import register_reportlab

    register_reportlab()
    src = Path(src)
    items = list_annotations(src, password=password)
    dst = output_path(src, "comments", out=out)
    extra: list[Path] = []
    if include_csv:
        cpath = Path(csv_out) if csv_out is not None else dst.with_suffix(".csv")
        with cpath.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["page", "type", "author", "subject", "created", "modified", "contents", "color",
                        "x0", "top", "x1", "bottom"])
            for it in items:
                w.writerow([it["page"] + 1, it["type"], it["author"], it["subject"], it["created"] or "",
                            it["modified"] or "", it["contents"], it["color"] or it.get("fill") or "", *it["rect"]])
        extra.append(cpath)

    base = ParagraphStyle("b", fontName="Inter", fontSize=9.5, leading=12.5, alignment=TA_LEFT)
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=base, fontName="Inter-Bold", fontSize=18, leading=22, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=base, fontName="Inter-Bold", fontSize=12.5, leading=16, spaceBefore=10,
                        spaceAfter=4, keepWithNext=1)
    story: list[Any] = [Paragraph("Comment summary", h1),
                        Paragraph(escape(f"{src.name} · {len(items)} comment(s) · generated "
                                         f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"), small), Spacer(1, 6)]
    counts: dict[str, int] = {}
    for it in items:
        counts[it["type"]] = counts.get(it["type"], 0) + 1
    if counts:
        story.append(Paragraph(escape(", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))), base))
    else:
        story.append(Paragraph("This document has no comments.", base))
    render = engines.get("render")
    thumb_w, thumb_h = 2.1 * inch, 1.5 * inch
    by_page: dict[int, list[dict]] = {}
    for it in items:
        by_page.setdefault(it["page"], []).append(it)
    for pi in sorted(by_page):
        img = render.render(src, pi, dpi=dpi, password=password)
        sc = dpi / 72.0
        story.append(Paragraph(f"Page {pi + 1}", h2))
        for it in sorted(by_page[pi], key=lambda d: (d["rect"][1], d["rect"][0])):
            x0, t, x1, b = it["rect"]
            m = 18
            box = (max(0, int((x0 - m) * sc)), max(0, int((t - m) * sc)), min(img.width, int((x1 + m) * sc) + 1),
                   min(img.height, int((b + m) * sc) + 1))
            cell: Any = Paragraph("", small)
            if box[2] > box[0] + 2 and box[3] > box[1] + 2:
                crop = img.crop(box)
                f = min(thumb_w / crop.width, thumb_h / crop.height, 72.0 / dpi * 1.6)
                buf = io.BytesIO()
                crop.save(buf, "PNG")
                buf.seek(0)
                cell = Image(buf, width=crop.width * f, height=crop.height * f)
            when = (it["modified"] or it["created"] or "")[:16].replace("T", " ")
            head = f"<font name='Inter-Bold'>{escape(it['type'].title())}</font>"
            if it["author"]:
                head += f" · {escape(it['author'])}"
            if when:
                head += f" · <font color='#666666'>{escape(when)}</font>"
            body = [Paragraph(head, base)]
            if it["subject"] and it["subject"].lower() != it["type"]:
                body.append(Paragraph(escape(it["subject"]), small))
            if it["contents"]:
                body.append(Paragraph(escape(it["contents"]).replace("\n", "<br/>"), base))
            t_ = Table([[cell, body]], colWidths=[thumb_w + 8, 7.0 * inch - thumb_w - 8])
            t_.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
            ]))
            story.append(KeepTogether([t_]))

    def footer(c, doc):
        c.saveState()
        c.setFont("Inter", 8)
        c.setFillColor(colors.HexColor("#666666"))
        c.drawString(0.75 * inch, 0.45 * inch, f"Comment summary — {src.name}")
        c.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"Page {doc.page}")
        c.restoreState()

    doc = SimpleDocTemplate(str(dst), pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.75 * inch, bottomMargin=0.75 * inch, title=f"Comment summary — {src.name}",
                            author="PDF Studio", subject="Annotation report", creator="pdfcore markup", lang="en-US")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    from pdfcore.gate import GateOptions

    opts = options or GateOptions(profile="typeset", margins_in=0.5)
    return finalize(dst, baseline=None, options=opts, extra=extra, quiet=quiet,
                    data={"count": len(items), "by_type": counts, "pages": sorted(p + 1 for p in by_page)})


# ============================================================== CLI
def _floats(s: str) -> list[float]:
    return [float(v) for v in re.split(r"[,\s]+", s.strip()) if v]


def _pairs(s: str) -> list[list[float]]:
    v = _floats(s)
    if len(v) % 2:
        raise ValueError(f"odd number of coordinates in {s!r}")
    return [[v[i], v[i + 1]] for i in range(0, len(v), 2)]


def _common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--color", help="name (red, yellow…) or #rrggbb")
    p.add_argument("--opacity", type=float)
    p.add_argument("--width", type=float, help="border/line width in points")
    p.add_argument("--author")
    p.add_argument("--subject")
    p.add_argument("--contents", help="comment text (pop-up note)")


def _common_from(ns) -> dict:
    return {k: getattr(ns, k) for k in _COMMON_KEYS if getattr(ns, k, None) is not None}


@command("annotate", "Add an annotation (highlight, note, freetext, callout, shape, cloud, ink, stamp)", group="markup")
def _cli_annotate(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--kind", default="highlight", choices=sorted(set(KINDS) | {"arrow"}))
    p.add_argument("--page", type=int, default=1, help="1-based page (default 1)")
    p.add_argument("--search", help="text to find; marks every match (text-markup kinds)")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--case", action="store_true")
    p.add_argument("--whole-word", action="store_true")
    p.add_argument("--pages", help="page range for --search, e.g. 1-3")
    p.add_argument("--rect", action="append", help="x0,top,x1,bottom in points from the top-left (repeatable)")
    p.add_argument("--at", help="x,top for notes/stamps")
    p.add_argument("--points", action="append", help="x,y,x,y,… (line/polygon/polyline/cloud; repeat for ink strokes)")
    p.add_argument("--target", help="x,top the callout arrow points at")
    p.add_argument("--text", help="text for freetext/callout, label for stamp")
    p.add_argument("--fill", help="interior/background color")
    p.add_argument("--font-size", type=float)
    p.add_argument("--heads", help="line endings start,end e.g. None,ClosedArrow")
    p.add_argument("--spec", help="JSON file with a list of annotation specs (pages 0-based)")
    _common_args(p)
    add_common(p)

    def run(ns):
        if ns.spec:
            specs = json.loads(Path(ns.spec).read_text(encoding="utf-8"))
            specs = specs["annotations"] if isinstance(specs, dict) else specs
            return annotate(ns.pdf, specs, out=ns.out, password=ns.password, options=gate_options_from(ns))
        kind = "line" if ns.kind == "arrow" else ns.kind
        s: dict[str, Any] = {"type": kind, "page": ns.page - 1, **_common_from(ns)}
        if ns.search:
            s["query"] = ns.search
            s.update(regex=ns.regex, case=ns.case, whole_word=ns.whole_word)
            if ns.pages:
                with _open(Path(ns.pdf), ns.password) as pdf:
                    s["pages"] = parse_pages(ns.pages, len(pdf.pages))
            else:
                s.pop("page")
        if ns.rect:
            rs = [_floats(r) for r in ns.rect]
            s["rects"] = rs
            s["rect"] = rs[0]
        if ns.at:
            s["at"] = _floats(ns.at)[:2]
        if ns.points:
            pts = [_pairs(x) for x in ns.points]
            s["points"] = pts[0]
            s["strokes"] = pts
        if ns.target:
            s["target"] = _floats(ns.target)[:2]
        if ns.text is not None:
            s["text"] = ns.text
            s["label"] = ns.text
        if ns.fill:
            s["fill"] = ns.fill
        if ns.font_size:
            s["font_size"] = ns.font_size
        if ns.heads:
            s["heads"] = [h.strip() for h in ns.heads.split(",")]
        elif ns.kind == "arrow":
            s["heads"] = ["None", "ClosedArrow"]
        if kind not in ("highlight", "underline", "strikeout", "squiggly"):
            s.pop("rects", None)
        res = annotate(ns.pdf, [s], out=ns.out, password=ns.password, options=gate_options_from(ns))
        if res.data.get("added", 0) == 0:
            print("warning: nothing was added (no search matches?)")
        return res

    return run


@command("annots", "List annotations (type, page, author, date, contents)", group="markup")
def _cli_list(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--pages")
    p.add_argument("--type", action="append", help="filter by type (repeatable)")
    p.add_argument("--all", action="store_true", help="include links, widgets and popups")
    p.add_argument("--json", action="store_true")
    p.add_argument("--password")

    def run(ns):
        pages = None
        if ns.pages:
            with _open(Path(ns.pdf), ns.password) as pdf:
                pages = parse_pages(ns.pages, len(pdf.pages))
        items = list_annotations(ns.pdf, pages=pages, types=ns.type, include_all=ns.all, password=ns.password)
        if ns.json:
            print(json.dumps(items, indent=2, ensure_ascii=False))
        else:
            for it in items:
                when = (it["modified"] or it["created"] or "")[:10]
                txt = (it["contents"] or "").replace("\n", " ")[:60]
                print(f"p.{it['page'] + 1:<3} {it['type']:<10} {it['author'][:16]:<16} {when:<10} "
                      f"{it['color'] or '':<8} {txt}")
            print(f"{len(items)} annotation(s)")
        return OpResult(None, None, [], {"annotations": items})

    return run


@command("annots-delete", "Delete annotations by type / author / page / text", group="markup")
def _cli_delete(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--type", action="append")
    p.add_argument("--author", action="append")
    p.add_argument("--pages")
    p.add_argument("--contains")
    add_common(p)

    def run(ns):
        pages = None
        if ns.pages:
            with _open(Path(ns.pdf), ns.password) as pdf:
                pages = parse_pages(ns.pages, len(pdf.pages))
        return delete_annotations(ns.pdf, types=ns.type, authors=ns.author, pages=pages, contains=ns.contains,
                                  out=ns.out, password=ns.password, options=gate_options_from(ns))

    return run


@command("annots-flatten", "Flatten annotations into page content", group="markup")
def _cli_flatten(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--type", action="append")
    p.add_argument("--pages")
    add_common(p)

    def run(ns):
        pages = None
        if ns.pages:
            with _open(Path(ns.pdf), ns.password) as pdf:
                pages = parse_pages(ns.pages, len(pdf.pages))
        return flatten_annotations(ns.pdf, types=ns.type, pages=pages, out=ns.out, password=ns.password,
                                   options=gate_options_from(ns))

    return run


@command("annots-export", "Export annotations to JSON", group="markup")
def _cli_export(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("-o", "--out")
    p.add_argument("--password")
    return lambda ns: export_annotations(ns.pdf, out=ns.out, password=ns.password)


@command("annots-import", "Import annotations from JSON (appearances regenerated)", group="markup")
def _cli_import(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("json")
    add_common(p)
    return lambda ns: import_annotations(ns.pdf, ns.json, out=ns.out, password=ns.password, options=gate_options_from(ns))


@command("annots-summary", "Comment summary report (PDF with thumbnails + CSV)", group="markup")
def _cli_summary(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--csv", help="CSV path (default next to the input)")
    p.add_argument("--no-csv", action="store_true")
    p.add_argument("--dpi", type=float, default=110)
    add_common(p)
    return lambda ns: comment_summary(ns.pdf, out=ns.out, csv_out=ns.csv, include_csv=not ns.no_csv, dpi=ns.dpi,
                                      password=ns.password)
