"""Drawing sets by sheet number: title blocks, sheet index, set reading, compare, batch links, slip sheets.

Reading functions are importable without the CLI (UL 508A skills reuse them)::

    from pdfcore.ops.plans.sheets import read_sheets, read_set
    for s in read_sheets("set.pdf"):
        print(s.page, s.number, s.title, s.rev, s.date, s.size)

Coordinates: :class:`~pdfcore.types.Rect`, points, top-left origin of the page **as displayed** (``/Rotate``
applied, relative to the MediaBox — the space pdfplumber, the renderer and :mod:`pdfcore.ops.markup` use).
Pages are 0-based in Python data, 1-based in reports, spreadsheets, JSON and the CLI.

Text layer (:class:`PageText`)
    Bid sets are big (ARCH D/E, 10⁵ vector paths per sheet), so text and path bounds come from pdfium
    (pypdfium2, Apache/BSD) — milliseconds per sheet instead of seconds. CAD exports often set title-block text
    vertically, on rotated pages, or mirrored, which left-to-right word grouping returns reversed
    ("LACIRTCELE"). Each char's direction comes from its text matrix (corrected for ``/Rotate``); chars are
    mapped into a per-direction *reading frame* (u along the baseline, v across it), grouped into lines and
    words there (word gaps judged against the line's own letter spacing), and mapped back to page coordinates.
    Every reader here uses it; pdfplumber is only used for table extraction (:func:`pdfcore.ops.extract.find_tables`)
    on the few pages that look like schedules.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path
from pdfcore.ops.plans import sub
from pdfcore.result import OpResult
from pdfcore.types import Rect

__all__ = [
    "Sheet", "TChar", "TLine", "page_lines", "read_sheets", "sheet_outline", "index", "read_set", "compare_sets",
    "link_set", "slipsheet", "norm_number", "discipline_of", "size_name", "DISCIPLINES", "DISCIPLINE_ORDER",
    "SHEET_RX",
]

IN = 72.0

# ====================================================================== vocabulary

#: A sheet number: E-101, A1.01, M-2, FP-101, E101, A-101A.
SHEET_RX = re.compile(r"^[A-Z]{1,2}-?\d{1,3}(?:\.\d{1,3})?[A-Z]?$")

DISCIPLINES: dict[str, str] = {
    "G": "General", "H": "Hazardous Materials", "V": "Survey / Mapping", "B": "Geotechnical", "C": "Civil",
    "L": "Landscape", "A": "Architectural", "I": "Interiors", "Q": "Equipment", "S": "Structural",
    "M": "Mechanical", "P": "Plumbing", "FP": "Fire Protection", "F": "Fire Protection", "FA": "Fire Alarm",
    "D": "Process", "E": "Electrical", "W": "Distributed Energy", "T": "Telecommunications", "R": "Resource",
    "X": "Other Disciplines", "Z": "Contractor / Shop Drawings", "O": "Operations", "N": "Instrumentation",
    "Y": "Controls", "K": "Food Service", "U": "Utilities",
}
#: Conventional set order (US NCS-like): G, C, L, A, S, M, P, FP, E, T … then the rest.
DISCIPLINE_ORDER = ["G", "H", "V", "B", "C", "L", "A", "I", "Q", "S", "M", "P", "FP", "F", "FA", "D", "E", "N",
                    "Y", "W", "T", "R", "U", "K", "X", "Z", "O"]

_LABELS: dict[str, tuple[str, ...]] = {
    "number": ("SHEET NO", "SHEET NUMBER", "SHEET", "DRAWING NO", "DRAWING NUMBER", "DWG NO", "DWG NUMBER", "DWG",
               "SHT NO", "SHT", "SHEET #", "DRAWING #", "DWG #", "SHEET ID"),
    "title": ("SHEET TITLE", "DRAWING TITLE", "TITLE", "SHEET NAME", "DRAWING NAME", "DWG TITLE", "SHEET DESCRIPTION"),
    "rev": ("REV", "REVISION", "REV NO", "REVISION NO", "REV #", "REVISION NUMBER"),
    "date": ("DATE", "ISSUE DATE", "DATE ISSUED", "PLOT DATE", "ISSUED"),
    "project": ("PROJECT", "PROJECT NAME", "PROJECT TITLE", "JOB NAME"),
    "project_no": ("PROJECT NO", "PROJECT NUMBER", "JOB NO", "JOB NUMBER", "PROJ NO", "PROJECT #", "JOB #"),
    "scale": ("SCALE",),
}
_LABEL_OF = {lab: k for k, labs in _LABELS.items() for lab in labs}

DATE_RX = re.compile(
    r"\b(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}\.\d{1,2}\.\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4}|"
    r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]*\.?\s+\d{1,2},?\s+\d{4})\b", re.I)
SCALE_RX = re.compile(
    r"SCALE\s*:?\s*((?:\d+(?:\s*\d+)?(?:/\d+)?\s*(?:\"|”|''|IN\.?|INCH)?\s*=\s*\d+\s*(?:'|’|FT\.?)\s*-?\s*\d*\s*(?:\"|”)?)"
    r"|(?:1\s*:\s*\d+)|NONE|NTS|N\.T\.S\.?|AS NOTED|AS SHOWN|NOT TO SCALE)", re.I)

# Paper sizes in inches (short side, long side).
_SIZES: list[tuple[str, float, float]] = [
    ("ANSI A", 8.5, 11), ("ANSI B", 11, 17), ("ANSI C", 17, 22), ("ANSI D", 22, 34), ("ANSI E", 34, 44),
    ("ARCH A", 9, 12), ("ARCH B", 12, 18), ("ARCH C", 18, 24), ("ARCH D", 24, 36), ("ARCH E", 36, 48),
    ("ARCH E1", 30, 42), ("ARCH E2", 26, 38), ("ARCH E3", 27, 39), ("ISO A4", 8.27, 11.69), ("ISO A3", 11.69, 16.54),
    ("ISO A2", 16.54, 23.39), ("ISO A1", 23.39, 33.11), ("ISO A0", 33.11, 46.81), ("LEGAL", 8.5, 14),
]


def size_name(width_pt: float, height_pt: float, tol_in: float = 0.2) -> str:
    """'ARCH D', 'ANSI D', … for a page size in points (either orientation); else '36 x 24 in'."""
    a, b = sorted((width_pt / IN, height_pt / IN))
    for name, s, l in _SIZES:
        if abs(a - s) <= tol_in and abs(b - l) <= tol_in:
            return name
    return f"{width_pt / IN:g} x {height_pt / IN:g} in".replace(".0 ", " ")


def norm_number(s: str) -> str:
    """Matching key for sheet numbers: upper case, no spaces or hyphens ('E-101' ≡ 'E101')."""
    return re.sub(r"[\s\-_]", "", s.upper())


def discipline_of(number: str) -> str:
    """Discipline designator of a sheet number: 'E-101' → 'E', 'FP-101' → 'FP', 'EP-2' → 'E'."""
    m = re.match(r"[A-Z]+", number.upper())
    if not m:
        return ""
    p = m.group(0)
    if len(p) >= 2 and p[:2] in DISCIPLINES:
        return p[:2]
    return p[0]


def _disc_rank(d: str) -> tuple[int, str]:
    return (DISCIPLINE_ORDER.index(d) if d in DISCIPLINE_ORDER else len(DISCIPLINE_ORDER), d)


def _norm_label(s: str) -> str:
    s = re.sub(r"[:.]+", " ", s.upper())
    return re.sub(r"\s+", " ", s).strip()


# ====================================================================== text layer


@dataclass(slots=True)
class TChar:
    text: str
    rect: Rect  # page coordinates (displayed, top-left)
    u0: float  # reading frame: u along the baseline, v across it (v grows "down" in reading terms)
    u1: float
    v0: float
    v1: float
    size: float
    dir: int  # displayed text direction: 0 → left-to-right, 90 → bottom-to-top, 180 → right-to-left, 270 → top-to-bottom
    font: str = ""
    gap: bool = False  # a space precedes this char in the content stream


@dataclass
class TLine:
    """A line of text in its own reading direction. ``spans[k]`` = index into ``chars`` of ``text[k]`` (-1 = space)."""

    chars: list[TChar]
    text: str
    spans: list[int]
    dir: int
    size: float

    @property
    def rect(self) -> Rect:
        return Rect.union(c.rect for c in self.chars)

    @property
    def frame(self) -> Rect:
        """Box in the reading frame (x = u, top = v)."""
        return Rect(min(c.u0 for c in self.chars), min(c.v0 for c in self.chars),
                    max(c.u1 for c in self.chars), max(c.v1 for c in self.chars))

    @property
    def bold(self) -> bool:
        return sum(bool(re.search(r"bold|black|heavy|semibold|demi", c.font, re.I)) for c in self.chars) > len(self.chars) / 2

    def sub_rect(self, i: int, j: int) -> Rect:
        idx = [self.spans[k] for k in range(i, j) if self.spans[k] >= 0]
        return Rect.union(self.chars[k].rect for k in idx) if idx else self.rect

    def sub_frame(self, i: int, j: int) -> Rect:
        idx = [self.spans[k] for k in range(i, j) if self.spans[k] >= 0]
        cs = [self.chars[k] for k in idx] or self.chars
        return Rect(min(c.u0 for c in cs), min(c.v0 for c in cs), max(c.u1 for c in cs), max(c.v1 for c in cs))

    def words(self) -> list[tuple[str, int, int]]:
        return [(m.group(0), m.start(), m.end()) for m in re.finditer(r"\S+", self.text)]


def _dir_from(a: float, b: float) -> int:
    """Baseline direction (counter-clockwise degrees, y up) from the first matrix column."""
    return (0 if a >= 0 else 180) if abs(a) >= abs(b) else (90 if b > 0 else 270)


def _to_frame(r: Rect, d: int, W: float, H: float) -> tuple[float, float, float, float]:
    if d == 0:
        return r.x0, r.x1, r.top, r.bottom
    if d == 90:  # reads bottom → top; the next line is to the right
        return H - r.bottom, H - r.top, r.x0, r.x1
    if d == 270:  # reads top → bottom; the next line is to the left
        return r.top, r.bottom, W - r.x1, W - r.x0
    return W - r.x1, W - r.x0, H - r.bottom, H - r.top


def frame_rect(r: Rect, d: int, W: float, H: float) -> Rect:
    """A displayed box in the reading frame of direction *d* (x = u, top = v)."""
    u0, u1, v0, v1 = _to_frame(r, d, W, H)
    return Rect(u0, v0, u1, v1)


def frame_to_page(f: Rect, d: int, W: float, H: float) -> Rect:
    """Inverse of :func:`frame_rect`."""
    if d == 0:
        return f
    if d == 90:
        return Rect(f.top, H - f.x1, f.bottom, H - f.x0)
    if d == 270:
        return Rect(W - f.bottom, f.x0, W - f.top, f.x1)
    return Rect(W - f.x1, H - f.bottom, W - f.x0, H - f.top)


def build_lines(chars: Iterable[TChar]) -> list[TLine]:
    """Group :class:`TChar` into direction-aware lines (see module docstring)."""
    by_dir: dict[int, list[TChar]] = {}
    for c in chars:
        by_dir.setdefault(c.dir, []).append(c)
    out: list[TLine] = []
    for d, cs in by_dir.items():
        out += _lines_one_dir(list(cs), d)
    out.sort(key=lambda l: (l.rect.top, l.rect.x0))
    return out


@dataclass(slots=True)
class PathInfo:
    rect: Rect  # displayed bounds
    stroke: float  # stroke width in points (0 = not stroked)
    filled: bool


class PageText:
    """Fast text + vector access for one page through pdfium (pypdfium2): chars, lines, path bounds.

    Coordinates are displayed points relative to the MediaBox (``/Rotate`` applied), like the rest of pdfcore.
    """

    def __init__(self, page) -> None:
        self.page = page
        l, b, r, t = (float(v) for v in page.get_mediabox())
        self.mx0, self.my0, self.mW, self.mH = min(l, r), min(b, t), abs(r - l), abs(t - b)
        self.rot = int(page.get_rotation() or 0) % 360
        self.W, self.H = (self.mH, self.mW) if self.rot in (90, 270) else (self.mW, self.mH)
        self._chars: list[TChar] | None = None
        self._paths: list[PathInfo] | None = None
        self._paths_min = 0.0

    # user space → displayed top-left
    def disp(self, x: float, y: float) -> tuple[float, float]:
        X, Y = x - self.mx0, self.mH - (y - self.my0)
        if self.rot == 90:
            return self.mH - Y, X
        if self.rot == 180:
            return self.mW - X, self.mH - Y
        if self.rot == 270:
            return Y, self.mW - X
        return X, Y

    def disp_rect(self, l: float, b: float, r: float, t: float) -> Rect:
        p, q = self.disp(l, b), self.disp(r, t)
        return Rect(min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))

    def chars(self) -> list[TChar]:
        if self._chars is not None:
            return self._chars
        import ctypes

        import pypdfium2.raw as raw

        out: list[TChar] = []
        tp = self.page.get_textpage()
        try:
            h = tp.raw
            n = raw.FPDFText_CountChars(h)
            rc, m = raw.FS_RECTF(), raw.FS_MATRIX()
            l, r, b, t = (ctypes.c_double() for _ in range(4))
            gap = False
            for k in range(n):
                u = raw.FPDFText_GetUnicode(h, k)
                if u <= 32 or u in (0xA0, 0x2002, 0x2003, 0x2009, 0xFEFF, 0xFFFE, 0xFFFF):
                    gap = True
                    continue
                if raw.FPDFText_GetLooseCharBox(h, k, rc):
                    box = (rc.left, rc.bottom, rc.right, rc.top)
                else:
                    raw.FPDFText_GetCharBox(h, k, l, r, b, t)
                    box = (l.value, b.value, r.value, t.value)
                if raw.FPDFText_GetMatrix(h, k, m):
                    du = _dir_from(m.a, m.b)
                else:
                    du = 0
                d = (du - self.rot) % 360
                rect = self.disp_rect(*box)
                u0, u1, v0, v1 = _to_frame(rect, d, self.W, self.H)
                size = v1 - v0
                if size <= 0.3:
                    continue
                try:
                    ch = chr(u)
                except ValueError:
                    continue
                wt = raw.FPDFText_GetFontWeight(h, k)
                out.append(TChar(ch, rect, u0, u1, v0, v1, size, d, "bold" if wt >= 600 else "", gap))
                gap = False
        finally:
            tp.close()
        self._chars = out
        return out

    def lines(self, bbox: Rect | None = None, *, where=None) -> list[TLine]:
        cs = self.chars()
        if bbox is not None:
            cs = [c for c in cs if _center_in(c.rect, bbox)]
        if where is not None:
            cs = [c for c in cs if where(c)]
        return build_lines(cs)

    def paths(self, min_extent: float = 0.0) -> list[PathInfo]:
        """Bounds and stroke width of path objects (Form XObjects included) whose longer side is ≥ *min_extent*.

        Cheap per object (type + bounds); dense CAD pages have 10⁵ paths, so callers filter by size.
        """
        key = round(min_extent, 1)
        if self._paths is not None and self._paths_min <= key:
            return [p for p in self._paths if max(p.rect.width, p.rect.height) >= min_extent]
        import ctypes

        import pypdfium2.raw as raw

        out: list[PathInfo] = []
        l, b, r, t = (ctypes.c_float() for _ in range(4))
        w = ctypes.c_float()
        fm, st = ctypes.c_int(), ctypes.c_int()
        m = raw.FS_MATRIX()
        PATH, FORM = raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_FORM
        get_type, get_bounds = raw.FPDFPageObj_GetType, raw.FPDFPageObj_GetBounds
        ident = (1, 0, 0, 1, 0, 0)

        def walk(get, n, mat, depth):
            sc = 1.0 if mat == ident else math.sqrt(abs(mat[0] * mat[3] - mat[1] * mat[2])) or 1.0
            lim = min_extent / sc
            for k in range(n):
                o = get(k)
                typ = get_type(o)
                if typ == PATH:
                    if not get_bounds(o, l, b, r, t):
                        continue
                    L, B, R, T = l.value, b.value, r.value, t.value
                    if R - L < lim and T - B < lim:
                        continue
                    if mat != ident:
                        pts = [_apply(mat, x, y) for x, y in ((L, B), (R, T), (L, T), (R, B))]
                        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                        L, B, R, T = min(xs), min(ys), max(xs), max(ys)
                    sw = 0.0
                    raw.FPDFPath_GetDrawMode(o, fm, st)
                    if st.value and raw.FPDFPageObj_GetStrokeWidth(o, w):
                        om = (m.a, m.b, m.c, m.d, m.e, m.f) if raw.FPDFPageObj_GetMatrix(o, m) else ident
                        cm = _mul(om, mat)
                        sw = float(w.value) * math.sqrt(abs(cm[0] * cm[3] - cm[1] * cm[2]))
                    out.append(PathInfo(self.disp_rect(L, B, R, T), sw, bool(fm.value)))
                elif typ == FORM and depth < 12:
                    sub_m = _mul((m.a, m.b, m.c, m.d, m.e, m.f), mat) if raw.FPDFPageObj_GetMatrix(o, m) else mat
                    walk(lambda i, o=o: raw.FPDFFormObj_GetObject(o, i), raw.FPDFFormObj_CountObjects(o), sub_m, depth + 1)

        pg = self.page.raw
        walk(lambda i: raw.FPDFPage_GetObject(pg, i), raw.FPDFPage_CountObjects(pg), ident, 0)
        self._paths, self._paths_min = out, key
        return out


def _mul(m1: Sequence[float], m2: Sequence[float]) -> tuple[float, ...]:
    """m1 then m2 (PDF row-vector convention)."""
    a, b, c, d, e, f = m1
    A, B, C, D, E, F = m2
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _apply(m: Sequence[float], x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _center_in(r: Rect, box: Rect) -> bool:
    return box.contains_point((r.x0 + r.x1) / 2, (r.top + r.bottom) / 2)


class open_set:
    """Context manager: a pdfium document plus cached :class:`PageText` per page (``with open_set(p) as d: d[i]``)."""

    def __init__(self, path: str | Path, password: str | None = None) -> None:
        import pypdfium2 as pdfium

        self.doc = pdfium.PdfDocument(str(path), password=password)
        self._cache: dict[int, PageText] = {}

    def __enter__(self) -> "open_set":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self.doc)

    def __getitem__(self, i: int) -> PageText:
        if i not in self._cache:
            self._cache[i] = PageText(self.doc[i])
        return self._cache[i]

    def release(self, i: int) -> None:
        pt = self._cache.pop(i, None)
        if pt is not None:
            try:
                pt.page.close()
            except Exception:
                pass

    def close(self) -> None:
        for i in list(self._cache):
            self.release(i)
        self.doc.close()


def _lines_one_dir(cs: list[TChar], d: int) -> list[TLine]:
    cs.sort(key=lambda c: ((c.v0 + c.v1) / 2, c.u0))
    rows: list[list[TChar]] = []
    centers: list[float] = []
    sizes: list[float] = []
    for c in cs:
        vc = (c.v0 + c.v1) / 2
        placed = False
        for k in range(len(rows) - 1, max(-1, len(rows) - 8), -1):
            if abs(vc - centers[k]) <= 0.33 * max(sizes[k], c.size) and 0.6 <= c.size / sizes[k] <= 1.65:
                rows[k].append(c)
                placed = True
                break
            if vc - centers[k] > 2 * max(sizes[k], c.size):
                break
        if not placed:
            rows.append([c])
            centers.append(vc)
            sizes.append(c.size)
    out: list[TLine] = []
    for row in rows:
        row.sort(key=lambda c: c.u0)
        # drop overprinted duplicates (CAD "bold" by double drawing)
        ded: list[TChar] = []
        for c in row:
            if ded and c.text == ded[-1].text and abs(c.u0 - ded[-1].u0) < 0.2 * c.size:
                continue
            ded.append(c)
        seg: list[TChar] = []
        for c in ded:
            if seg and c.u0 - max(s.u1 for s in seg[-3:]) > 2.2 * max(c.size, seg[-1].size):
                out.append(_mk_line(seg, d))
                seg = []
            seg.append(c)
        if seg:
            out.append(_mk_line(seg, d))
    return [ln for ln in out if ln.text.strip()]


def _mk_line(seg: list[TChar], d: int) -> TLine:
    text: list[str] = []
    spans: list[int] = []
    prev: TChar | None = None
    med = statistics.median(c.size for c in seg)
    gaps = [b.u0 - a.u1 for a, b in zip(seg, seg[1:])]
    # typical letter gap of this line (loose boxes overlap or touch); a word gap stands out from it
    base = min(0.0, statistics.median(gaps)) if gaps else 0.0
    for k, c in enumerate(seg):
        if c.text.isspace():
            if text and text[-1] != " ":
                text.append(" ")
                spans.append(-1)
            prev = None
            continue
        g = c.u0 - prev.u1 if prev is not None else 0.0
        if prev is not None and (g - base > 0.22 * med or (c.gap and g - base > 0.05 * med)) and text and text[-1] != " ":
            text.append(" ")
            spans.append(-1)
        for ch in c.text:
            text.append(ch)
            spans.append(k)
        prev = c
    s = "".join(text)
    lead = len(s) - len(s.lstrip())
    s2 = s.strip()
    spans = spans[lead: lead + len(s2)]
    chars_used = [c for c in seg if not c.text.isspace()] or seg
    # re-index spans onto chars_used
    remap = {id(c): i for i, c in enumerate(chars_used)}
    spans = [remap.get(id(seg[k]), -1) if k >= 0 else -1 for k in spans]
    return TLine(chars_used, s2, spans, d, med)


# ====================================================================== sheets (title blocks)


@dataclass
class Sheet:
    """One drawing sheet read from its title block. ``page`` is 0-based; rects are displayed points."""

    page: int
    number: str
    title: str = ""
    rev: str = ""
    date: str = ""
    discipline: str = ""
    discipline_name: str = ""
    size: str = ""
    width_in: float = 0.0
    height_in: float = 0.0
    scale: str = ""
    project: str = ""
    project_no: str = ""
    confidence: float = 0.0
    source: str = "title-block"  # title-block | page-label | fallback
    title_block: Rect | None = None
    number_rect: Rect | None = None
    text_dir: int = 0  # reading direction of most title-block text (0 = horizontal)
    revisions: list[dict] = field(default_factory=list)
    title_alternatives: list[str] = field(default_factory=list, repr=False)

    @property
    def key(self) -> str:
        return norm_number(self.number)

    def to_dict(self) -> dict[str, Any]:
        def r(x: Rect | None):
            return None if x is None else [round(v, 2) for v in (x.x0, x.top, x.x1, x.bottom)]

        return {"page": self.page + 1, "number": self.number, "title": self.title, "rev": self.rev, "date": self.date,
                "discipline": self.discipline, "discipline_name": self.discipline_name, "size": self.size,
                "width_in": self.width_in, "height_in": self.height_in, "scale": self.scale, "project": self.project,
                "project_no": self.project_no, "confidence": round(self.confidence, 2), "source": self.source,
                "title_block": r(self.title_block), "number_rect": r(self.number_rect), "text_dir": self.text_dir,
                "revisions": self.revisions}


def _dist(a: Rect, b: Rect) -> float:
    dx = max(0.0, max(a.x0, b.x0) - min(a.x1, b.x1))
    dy = max(0.0, max(a.top, b.top) - min(a.bottom, b.bottom))
    return math.hypot(dx, dy)


def _is_label(ln: TLine) -> str | None:
    return _LABEL_OF.get(_norm_label(ln.text))


def _inline_label(ln: TLine) -> tuple[str, str] | None:
    m = re.match(r"^([A-Za-z][A-Za-z .#]{1,22}?)\s*[:#]\s*(\S.*)$", ln.text)
    if not m:
        return None
    k = _LABEL_OF.get(_norm_label(m.group(1)))
    if k is None and _norm_label(m.group(1)) in ("PROJ", "JOB", "PROJ NO", "PROJECT ID"):
        k = "project_no"
    return (k, m.group(2).strip()) if k else None


@dataclass
class _Cand:
    text: str
    rect: Rect
    size: float
    line: TLine
    score: float = 0.0


def _title_block_rect(pt: PageText, num: Rect, lines: list[TLine], dom: int = 0) -> tuple[Rect, str]:
    """Title-block box (displayed points) around the sheet number and how it was found.

    A full-height rule left of the number (title block on the right) or a full-width rule above it (title block
    along the bottom) bounds it; otherwise a text cluster grown from the number (links ≤ 0.6 in).
    """
    PW, PH = pt.W, pt.H
    W, H = (PW, PH) if dom in (0, 180) else (PH, PW)  # frame extents
    fnum = frame_rect(num, dom, PW, PH)
    thin = [frame_rect(p.rect, dom, PW, PH) for p in pt.paths(0.5 * min(PW, PH))
            if min(p.rect.width, p.rect.height) <= 4.0]
    num_page, num = num, fnum
    cands: list[tuple[float, Rect, str]] = []
    if num.x0 > 0.5 * W:
        vs = [r for r in thin if r.width <= 4 and r.height >= 0.55 * H and r.top < 0.2 * H and r.bottom > 0.8 * H]
        left = [r for r in vs if 0.4 * W < r.x1 < num.x0 - 1 and r.top <= num.top and r.bottom >= num.bottom]
        if left:
            sep = max(left, key=lambda r: r.x1)
            right = [r.x0 for r in vs if r.x0 > num.x1 + 1]
            rr = Rect(sep.x0, sep.top, min(right) if right else W, sep.bottom)
            cands.append((rr.area, rr, "rule-right"))
    if num.top > 0.5 * H:
        hs = [r for r in thin if r.height <= 4 and r.width >= 0.55 * W and r.x0 < 0.2 * W and r.x1 > 0.8 * W]
        above = [r for r in hs if 0.4 * H < r.bottom < num.top - 1 and r.x0 <= num.x0 and r.x1 >= num.x1]
        if above:
            sep = max(above, key=lambda r: r.bottom)
            below = [r.top for r in hs if r.top > num.bottom + 1]
            rr = Rect(sep.x0, sep.top, sep.x1, min(below) if below else H)
            cands.append((rr.area, rr, "rule-bottom"))
    if cands:
        cands.sort(key=lambda c: c[0])
        return frame_to_page(cands[0][1], dom, PW, PH), cands[0][2]
    W, H = PW, PH
    box = num_page
    pool = list(lines)
    grown = True
    while grown:
        grown = False
        rest = []
        for ln in pool:
            if _dist(ln.rect, box) <= 0.6 * IN:
                box = Rect.union([box, ln.rect])
                grown = True
            else:
                rest.append(ln)
        pool = rest
    pad = 0.12 * IN
    return Rect(max(0, box.x0 - pad), max(0, box.top - pad), min(W, box.x1 + pad), min(H, box.bottom + pad)), "text-cluster"


def _value_for(lab: TLine, lines: list[TLine], key: str) -> list[TLine]:
    """Value line(s) for a label line: to its right on the same baseline, else the nearest below (reading frame)."""
    f = lab.frame
    same = [l for l in lines if l is not lab and l.dir == lab.dir and not _is_label(l)]
    right = [l for l in same if abs(l.frame.top - f.top) < 0.6 * max(l.size, lab.size)
             and 0 < l.frame.x0 - f.x1 < 1.6 * IN]
    if right:
        return [min(right, key=lambda l: l.frame.x0)]
    below = [l for l in same if l.frame.top >= f.top + 0.5 * lab.size
             and l.frame.top - f.bottom <= max(4 * lab.size, 0.6 * IN)
             and f.x0 - 0.5 * IN <= l.frame.x0 <= f.x1 + 0.4 * IN]
    if not below:
        return []
    first = min(below, key=lambda l: (round(l.frame.top - f.bottom, 0), abs(l.frame.x0 - f.x0)))
    out = [first]
    if key in ("title", "project"):
        cur = first
        while True:
            nxt = [l for l in same if l not in out and 0 <= l.frame.top - cur.frame.bottom <= 0.9 * cur.size
                   and abs(l.size - cur.size) <= 0.2 * cur.size and abs(l.frame.x0 - cur.frame.x0) < 0.6 * IN]
            if not nxt:
                break
            cur = min(nxt, key=lambda l: l.frame.top)
            out.append(cur)
    return out


def _parse_date(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%m.%d.%Y", "%m.%d.%y", "%m-%d-%Y", "%m-%d-%y", "%b %d, %Y",
                "%B %d, %Y", "%b %d %Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(s.strip().title() if s[:1].isalpha() else s.strip(), fmt)
        except ValueError:
            continue
    return None


_DESC_RX = re.compile(r"\b(DESCRIPTION|DESC|REVISION|REVISIONS|ISSUE|ISSUED FOR|REMARKS)\b")


def _revisions(lines: list[TLine]) -> list[dict]:
    """Rows of a revision table: a DATE header (with DESCRIPTION-like words on its row), rows with a date nearby."""
    out: list[dict] = []
    for hdr in lines:
        t = _norm_label(hdr.text)
        if "DATE" not in t.split():
            continue
        f = hdr.frame
        row = [l for l in lines if l.dir == hdr.dir and abs(l.frame.top - f.top) < 0.6 * hdr.size
               and abs(l.frame.x0 - f.x0) < 6 * IN]
        if not any(_DESC_RX.search(_norm_label(l.text)) for l in row):
            continue
        u0, u1 = min(l.frame.x0 for l in row) - 0.5 * IN, max(l.frame.x1 for l in row) + 0.5 * IN
        near = [l for l in lines if l.dir == hdr.dir and l not in row and DATE_RX.search(l.text)
                and abs(l.frame.top - f.top) <= 10 * IN and l.frame.x1 > u0 and l.frame.x0 < u1]
        rows: list[TLine] = []
        for side in (1, -1):  # contiguous rows below, then above, the header
            cand = sorted((l for l in near if (l.frame.top - f.top) * side > 0), key=lambda l: abs(l.frame.top - f.top))
            edge = f.bottom if side > 0 else f.top
            for l in cand:
                gap = (l.frame.top - edge) if side > 0 else (edge - l.frame.bottom)
                if gap > 2.5 * max(l.size, hdr.size):
                    break
                rows.append(l)
                edge = l.frame.bottom if side > 0 else l.frame.top
        for r in sorted(rows, key=lambda l: l.frame.top):
            m = DATE_RX.search(r.text)
            assert m is not None
            before, after = r.text[: m.start()].strip(), r.text[m.end():].strip()
            mark = before.split()[0] if before.split() and len(before.split()[0]) <= 3 else ""
            if not mark:  # a separate REV/MARK column on the same row, to the left
                left = [l for l in lines if l.dir == r.dir and l is not r and abs(l.frame.top - r.frame.top) < 0.5 * r.size
                        and 0 < r.frame.x0 - l.frame.x1 < 1.5 * IN and len(l.text) <= 3]
                if left:
                    mark = max(left, key=lambda l: l.frame.x1).text
            out.append({"rev": mark, "date": m.group(0), "description": after})
        if out:
            break
    return out


def _latest(revs: list[dict]) -> dict | None:
    if not revs:
        return None
    dated = [(d, r) for r in revs if (d := _parse_date(r["date"])) is not None]
    if dated:
        return max(dated, key=lambda x: x[0])[1]
    return revs[-1]


def _number_candidates(lines: list[TLine], W: float, H: float, dom: int = 0) -> list[_Cand]:
    if not lines:
        return []
    smax = max(l.size for l in lines)
    diag = math.hypot(W, H)
    labels = [l for l in lines if _is_label(l) == "number" or _norm_label(l.text) in ("SHEET NO", "SHEET NUMBER")]
    out: list[_Cand] = []
    for ln in lines:
        ws = ln.words()
        for w, s, e in ws:
            tok = w.strip(".,;:()[]")
            if not SHEET_RX.match(tok) or not re.search(r"\d", tok):
                continue
            off = w.find(tok)
            sz = statistics.median(ln.chars[ln.spans[k]].size for k in range(s + off, s + off + len(tok)) if ln.spans[k] >= 0)
            rect = ln.sub_rect(s + off, s + off + len(tok))
            sc = 0.5 * sz / smax
            others = [x for x, _, _ in ws if x is not w]
            if not others:
                sc += 0.15
            elif all(_LABEL_OF.get(_norm_label(x)) == "number" or _norm_label(x) in ("NO", "SHEET", "SHT", "DWG") for x in others):
                sc += 0.1
            else:
                sc -= 0.1 * min(3, len(others))
            if any(_dist(l.rect, rect) < max(3 * sz, 0.8 * IN) for l in labels):
                sc += 0.2
            fr = frame_rect(rect, dom, W, H)
            FW, FH = (W, H) if dom in (0, 180) else (H, W)
            cx, cy = (fr.x0 + fr.x1) / 2, (fr.top + fr.bottom) / 2
            sc += 0.2 * (1 - min(1.0, math.hypot(FW - cx, FH - cy) / (0.45 * diag)))
            out.append(_Cand(tok, rect, sz, ln, sc))
    out.sort(key=lambda c: -c.score)
    return out


def _dominant_dir(pt: PageText) -> int:
    """Reading direction of most of the page's text (the sheet's 'up'), weighted by char count."""
    cnt: dict[int, int] = {}
    for c in pt.chars():
        cnt[c.dir] = cnt.get(c.dir, 0) + 1
    if not cnt:
        return 0
    best = max(cnt, key=lambda d: cnt[d])
    return best if cnt[best] > 1.5 * cnt.get(0, 0) or best == 0 else 0


def _read_page(pt: PageText, i: int, *, scan_scale: bool, label: str | None) -> Sheet:
    W, H = pt.W, pt.H
    dom = _dominant_dir(pt)
    FW, FH = (W, H) if dom in (0, 180) else (H, W)

    def in_band(c: TChar) -> bool:
        f = frame_rect(c.rect, dom, W, H)
        return (f.x0 + f.x1) / 2 >= 0.68 * FW or (f.top + f.bottom) / 2 >= 0.72 * FH

    lines = pt.lines(where=in_band)
    sh = Sheet(i, "", size=size_name(W, H), width_in=round(W / IN, 2), height_in=round(H / IN, 2))
    cands = _number_candidates(lines, W, H, dom)
    best = cands[0] if cands and cands[0].score >= 0.4 else None
    if best is None:
        if label and not label.isdigit():
            sh.number, sh.source, sh.confidence = label, "page-label", 0.3
        else:
            sh.number, sh.source, sh.confidence = f"Sheet {i + 1}", "fallback", 0.0
    else:
        sh.number, sh.number_rect = best.text, best.rect
        tb, how = _title_block_rect(pt, best.rect, lines, dom)
        sh.title_block = tb
        tl = [l for l in lines if _center_in(l.rect, tb)]
        _fields(sh, tl, best)
        sh.confidence = max(0.0, min(1.0, best.score + (0.1 if how != "text-cluster" else 0.0)
                                     + (0.1 if sh.title else -0.1)))
        dirs = [l.dir for l in tl for _ in l.text]
        sh.text_dir = max(set(dirs), key=dirs.count) if dirs else 0
    sh.discipline = discipline_of(sh.number) if sh.source != "fallback" else ""
    sh.discipline_name = DISCIPLINES.get(sh.discipline, "")
    if scan_scale and not sh.scale:
        found: list[str] = []
        for ln in pt.lines():
            for m in SCALE_RX.finditer(ln.text):
                v = re.sub(r"\s+", " ", m.group(1)).strip()
                if v not in found:
                    found.append(v)
        sh.scale = "; ".join(found[:4])
    return sh


def _fields(sh: Sheet, tl: list[TLine], best: _Cand) -> None:
    vals: dict[str, str] = {}
    used: set[int] = set()
    for ln in tl:
        k = _is_label(ln)
        if k is not None and k not in vals:
            v = _value_for(ln, tl, k)
            if v:
                vals[k] = " ".join(x.text for x in v)
                used.update(id(x) for x in v)
        il = _inline_label(ln)
        if il and il[0] not in vals:
            vals[il[0]] = il[1]
    # sheet number from its label when that agrees with the pattern
    if "number" in vals:
        tok = vals["number"].split()[0].strip(".,;:") if vals["number"].split() else ""
        if SHEET_RX.match(tok) and re.search(r"\d", tok) and tok != sh.number:
            if tok in {w for l in tl for w, _, _ in l.words()}:
                sh.number = tok
    title = vals.get("title", "")
    if not title or title == sh.number:
        alts = _title_fallback([l for l in tl if id(l) not in used], best)
        sh.title_alternatives = alts
        title = alts[0] if alts else ""
    else:
        sh.title_alternatives = [title]
    sh.title = re.sub(r"\s+", " ", title).strip()
    proj = vals.get("project", "")
    sh.project = proj if proj != sh.title else ""
    sh.project_no = vals.get("project_no", "")
    sh.revisions = _revisions(tl)
    rv = vals.get("rev", "")
    if rv and (len(rv) > 6 or DATE_RX.search(rv)):
        rv = ""
    latest = _latest(sh.revisions)
    sh.rev = rv or (latest["rev"] if latest else "")
    dv = vals.get("date", "")
    m = DATE_RX.search(dv) if dv else None
    if m:
        sh.date = m.group(0)
    elif latest:
        sh.date = latest["date"]
    else:
        for ln in tl:
            m = DATE_RX.search(ln.text)
            if m:
                sh.date = m.group(0)
                break
    sc = vals.get("scale", "")
    if not sc:
        for ln in tl:
            m = SCALE_RX.search(ln.text)
            if m:
                sc = m.group(1)
                break
    sh.scale = re.sub(r"\s+", " ", sc).strip()


_NOT_TITLE = re.compile(r"@|www\.|https?:|PHONE|FAX|TEL\b|COPYRIGHT|©|\bSUITE\b|\bOF \d+\b|^\d+$|"
                        r"SEAL|LICENSE|REGISTERED|PROFESSIONAL ENGINEER|NOT FOR CONSTRUCTION", re.I)


def _title_fallback(tl: list[TLine], best: _Cand) -> list[str]:
    """No TITLE label: groups of adjacent large lines near the sheet number, best first (see read_sheets for the
    set-level pass that skips a group repeated on most sheets — the project name)."""
    pool = [l for l in tl if l is not best.line and not _is_label(l) and not _inline_label(l)
            and not _NOT_TITLE.search(l.text) and not DATE_RX.search(l.text) and re.search(r"[A-Za-z]{3}", l.text)
            and not SHEET_RX.match(l.text.strip())]
    if not pool:
        return []
    # adjacent same-size lines → groups
    groups: list[list[TLine]] = []
    for l in sorted(pool, key=lambda l: (l.dir, round(l.size, 0), l.frame.top)):
        for g in groups:
            last = g[-1]
            if (last.dir == l.dir and abs(last.size - l.size) <= 0.12 * last.size
                    and 0 <= l.frame.top - last.frame.bottom <= 1.2 * l.size
                    and min(last.frame.x1, l.frame.x1) - max(last.frame.x0, l.frame.x0) > -0.3 * IN):
                g.append(l)
                break
        else:
            groups.append([l])
    reach = max(6 * IN, 0.5 * max(best.rect.bottom, 1))

    def score(g: list[TLine]) -> float:
        d = min(_dist(l.rect, best.rect) for l in g)
        return g[0].size * (1.0 if d < reach else 0.35) - 0.002 * d

    ranked = sorted(groups, key=score, reverse=True)
    top = ranked[0][0].size
    outs: list[str] = []
    for grp in ranked[:6]:
        if grp[0].size < 0.6 * top:
            break
        g = sorted(grp, key=lambda l: l.frame.top)
        # a discipline header line is often followed by a smaller sheet-name line: extend downward (reading frame)
        big = max(l.size for l in g)
        while len(g) < 6:
            last = g[-1]
            nxt = [l for l in pool if l not in g and l.dir == last.dir and 0 <= l.frame.top - last.frame.bottom <= 1.2 * big
                   and 0.6 * big <= l.size <= 1.05 * last.size
                   and min(last.frame.x1, l.frame.x1) - max(last.frame.x0, l.frame.x0) > 0]
            if not nxt:
                break
            g.append(min(nxt, key=lambda l: l.frame.top))
        t = re.sub(r"\s+", " ", " ".join(l.text for l in g)).strip()
        if t and t not in outs:
            outs.append(t)
    return outs


def _page_labels(path: str | Path, password: str | None) -> list[str | None]:
    import pikepdf

    try:
        with pikepdf.open(str(path), password=password or "") as pk:
            if "/PageLabels" not in pk.Root:
                return [None] * len(pk.pages)
            return [pg.label for pg in pk.pages]
    except Exception:
        return []


def _tokens(t: str) -> frozenset[str]:
    return frozenset(re.findall(r"[A-Z0-9]+", t.upper()))


def _similar(a: frozenset[str], b: frozenset[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= 0.6


def _drop_repeated_titles(sheets: list[Sheet]) -> None:
    """A fallback title (nearly) shared by most sheets is the project name, not a sheet title: use the next
    candidate that is not."""
    found = [s for s in sheets if s.source == "title-block" and s.title_alternatives]
    if len(found) < 4:
        return
    toks = [[_tokens(t) for t in s.title_alternatives] for s in found]

    def common(t: frozenset[str]) -> bool:
        n = sum(any(_similar(t, a) for a in alts) for alts in toks)
        return n >= max(3, 0.5 * len(found))

    for s, alts in zip(found, toks):
        if len(alts) > 1 and common(alts[0]):
            for t, tk in zip(s.title_alternatives[1:], alts[1:]):
                if not common(tk):
                    if not s.project:
                        s.project = s.title
                    s.title = t
                    break


def read_sheets(path: str | Path, *, password: str | None = None, pages: Iterable[int] | None = None,
                scan_scale: bool = True, _doc: "open_set | None" = None) -> list[Sheet]:
    """Read every page's title block → :class:`Sheet` list (page order).

    Detection is generic: sheet-number tokens (``SHEET_RX``) in the right / bottom bands are scored by text size
    relative to the band, standing alone on their line, proximity to a SHEET NO. label and to the bottom-right
    corner. The title block is the region behind the nearest full-height rule left of the number (or full-width
    rule above it), else a text cluster grown from the number. Fields come from labels (SHEET TITLE, REV, DATE,
    PROJECT, SCALE…) with values to the right or below in the label's reading direction (vertical title-block
    text included), plus the revision table; without a TITLE label the largest text group near the number is
    the title. Pages without a readable number fall back to their page label, then "Sheet N".
    *scan_scale*: also look for "SCALE: …" anywhere on the sheet when the title block has none.
    """
    labels = _page_labels(path, password)
    out: list[Sheet] = []
    doc = _doc or open_set(path, password)
    try:
        idx = range(len(doc)) if pages is None else pages
        for i in idx:
            out.append(_read_page(doc[i], i, scan_scale=scan_scale, label=labels[i] if i < len(labels) else None))
            if _doc is None:
                doc.release(i)
    finally:
        if _doc is None:
            doc.close()
    _drop_repeated_titles(out)
    seen: dict[str, int] = {}
    for s in out:
        seen[s.key] = seen.get(s.key, 0) + 1
    for s in out:
        if seen[s.key] > 1 and s.source == "title-block":
            s.confidence = min(s.confidence, 0.5)
    return out


# ====================================================================== shared helpers


def sheet_outline(sheets: Sequence[Sheet], *, page_of: dict[int, int] | None = None) -> list:
    """Bookmarks: one per sheet ("E-101 — TITLE"), grouped by discipline in conventional set order."""
    from pdfcore.ops.navigate import Bookmark

    groups: dict[str, list[Sheet]] = {}
    for s in sheets:
        groups.setdefault(s.discipline or "?", []).append(s)
    out = []
    for d in sorted(groups, key=_disc_rank):
        kids = [Bookmark(f"{s.number} — {s.title}" if s.title else s.number,
                         page_of.get(s.page, s.page) if page_of else s.page) for s in groups[d]]
        name = DISCIPLINES.get(d, "Other" if d == "?" else d)
        title = f"{name} ({d})" if d != "?" else "Unnumbered sheets"
        out.append(Bookmark(title, kids[0].page, kids))
    return out


def _label_ranges(sheets: Sequence[Sheet]) -> list:
    from pdfcore.ops.organize import LabelRange

    return [LabelRange(i, None, s.number, 1) for i, s in enumerate(sheets)]


def _gate_opts(options, n_pages: int):
    """Caller's gate options, else defaults that sample big sets (the gate re-reads every page twice)."""
    if options is not None:
        return options
    from pdfcore.gate import GateOptions

    return GateOptions(max_pages=12) if n_pages > 12 else None


def _verify_files(paths: Iterable[Path]) -> None:
    from pdfcore.ops.extract import _verify

    _verify(paths)


def _xlsx_table(wb, title: str, header: Sequence[str], rows: Iterable[Sequence[Any]], widths: Sequence[int] | None = None) -> Any:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(title=_sheet_title(wb, title))
    ws.append(list(header))
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
        c.alignment = Alignment(vertical="center")
    n = 0
    w = [len(str(h)) for h in header]
    for r in rows:
        ws.append(list(r))
        n += 1
        for k, v in enumerate(r):
            if k < len(w):
                w[k] = max(w[k], min(70, len(str(v)) if v is not None else 0))
    for k, wd in enumerate(widths or w):
        ws.column_dimensions[get_column_letter(k + 1)].width = max(8, min(72, wd + 2))
    ws.freeze_panes = "A2"
    if n:
        ws.auto_filter.ref = ws.dimensions
    return ws


def _sheet_title(wb, title: str) -> str:
    t = re.sub(r"[\[\]:*?/\\]", "-", title)[:31] or "Sheet"
    base, k = t, 2
    while t in wb.sheetnames:
        suf = f" ({k})"
        t = base[: 31 - len(suf)] + suf
        k += 1
    return t


def _pdf_rect_for(page, r: Rect) -> list[float]:
    """Displayed Rect → PDF user-space [llx, lly, urx, ury] on a pikepdf page (handles /Rotate)."""
    from pdfcore.ops.markup import PageGeom

    g = PageGeom(page)
    return list(g.user_bbox([g.d(r.x0, r.top), g.d(r.x1, r.bottom)]))


def _disp_rect_of(page, rect: Sequence[float]) -> Rect:
    from pdfcore.ops.markup import PageGeom

    return PageGeom(page).rect_from_user(*(float(v) for v in rect))


# ====================================================================== index


def index(path: str | Path, *, bookmark: bool = True, xlsx: bool = True, out: str | Path | None = None,
          xlsx_out: str | Path | None = None, password: str | None = None, options=None,
          quiet: bool = False) -> OpResult:
    """Sheet index / drawing log from title blocks.

    *xlsx*: ``<name>.sheet-index.xlsx`` (Sheet No., Title, Rev, Date, Page, Discipline, Size, Scale, …).
    *bookmark*: ``<name>.indexed.pdf`` — one bookmark per sheet grouped by discipline (G, C, L, A, S, M, P, FP, E,
    T …) and page labels set to the sheet numbers. data: ``sheets`` (list of dicts), ``low_confidence`` pages.
    """
    import pikepdf

    from pdfcore.ops.navigate import save_pdf, write_outline
    from pdfcore.ops.organize import write_label_ranges

    src = Path(path)
    if not (bookmark or xlsx):
        raise ValueError("nothing to do: enable bookmark and/or xlsx")
    sheets = read_sheets(src, password=password)
    data = {"sheets": [s.to_dict() for s in sheets],
            "low_confidence": [s.page + 1 for s in sheets if s.confidence < 0.6]}
    extra: list[Path] = []
    if xlsx:
        xp = Path(xlsx_out) if xlsx_out else output_path(src, "sheet-index", ".xlsx")
        _index_xlsx(sheets, xp, src)
        _verify_files([xp])
        extra.append(xp)
    if not bookmark:
        if not quiet:
            print(f"{extra[0]}  PASS")
        return OpResult(extra[0], None, extra, data)
    dst = output_path(src, "indexed", out=out)
    with pikepdf.open(str(src), password=password or "") as pdf:
        write_outline(pdf, sheet_outline(sheets))
        write_label_ranges(pdf, _label_ranges(sheets))
        save_pdf(pdf, dst)
    return finalize(dst, src, options=_gate_opts(options, len(sheets)), password=password, extra=extra, data=data,
                    quiet=quiet)


def _index_xlsx(sheets: Sequence[Sheet], path: Path, src: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    order = sorted(sheets, key=lambda s: (_disc_rank(s.discipline), s.page))
    _xlsx_table(wb, "Drawing Log", ["Sheet No.", "Title", "Rev", "Date", "Page", "Discipline", "Size", "Scale",
                                    "Project", "Confidence", "Source"],
                ([s.number, s.title, s.rev, s.date, s.page + 1, f"{s.discipline} {s.discipline_name}".strip(), s.size,
                  s.scale, s.project, round(s.confidence, 2), s.source] for s in order))
    _xlsx_table(wb, "Revisions", ["Sheet No.", "Page", "Rev", "Date", "Description"],
                ([s.number, s.page + 1, r["rev"], r["date"], r["description"]] for s in sheets for r in s.revisions))
    wb.properties.title = f"Drawing log — {src.name}"
    wb.properties.creator = "PDF Studio"
    wb.save(path)


# ====================================================================== read a set (one-lines, schedules, notes)

_PANEL_COLS = {"CKT", "CIRCUIT", "CKT NO", "CKT#", "NO"}
_PANEL_HINT = {"BKR", "BREAKER", "TRIP", "VA", "KVA", "POLE", "POLES", "P", "LOAD", "AMP", "AMPS", "A", "B", "C", "WIRE",
               "PH", "PHASE"}
_EQUIP_HINT = {"HP", "FLA", "VOLTS", "VOLT", "V", "KW", "KVA", "AMPS", "MCA", "MOCP", "PHASE", "PH", "LRA", "RLA", "HZ"}
_ONELINE_TITLE = re.compile(r"\b(ONE[- ]?LINE|SINGLE[- ]LINE|RISER DIAGRAM|POWER DISTRIBUTION DIAGRAM)\b", re.I)
_ONELINE_EQUIP = re.compile(r"\b(\d+(?:\.\d+)?\s*KVA|XFMR|TRANSFORMER|MDP|SWBD|SWITCHBOARD|SWITCHGEAR|MCC|ATS|"
                            r"GEN(?:ERATOR)?|UTILITY|\d+(?:\.\d+)?\s*KV\b|MAIN BREAKER|\d{3,4}\s*A(?:MP)?\b|VFD)\b", re.I)
_NOTES_HEAD = re.compile(r"^(?:[A-Z][A-Z &/-]{0,30}\s)?NOTES?\s*:?$")
_NOTE_ITEM = re.compile(r"^\(?(\d{1,2}|[A-Z])[.)]\s+(\S.*)$")


def _cell(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().upper().rstrip(".:")


def _header_index(rows: list[list[str]], need: set[str], hint: set[str], min_hint: int = 1) -> int | None:
    for k, row in enumerate(rows[:3]):
        cells = {_cell(c) for c in row}
        if cells & need and len(cells & hint) >= min_hint:
            return k
    return None


def _num(v: str) -> float | None:
    from pdfcore.ops.extract import to_number

    n = to_number(v.replace(",", "")) if isinstance(v, str) else None
    return float(n) if n is not None else None


def _lines_near_above(lines: list[TLine], box: Rect, reach: float = 1.6 * IN) -> list[TLine]:
    return sorted([l for l in lines if l.dir == 0 and 0 <= box.top - l.rect.bottom <= reach
                   and l.rect.x1 > box.x0 - 0.5 * IN and l.rect.x0 < box.x1], key=lambda l: box.top - l.rect.bottom)


def _panel(table, lines: list[TLine], sheet: Sheet) -> dict | None:
    rows = [[str(c or "") for c in r] for r in table.rows]
    hi = _header_index(rows, _PANEL_COLS | {"CKT"}, _PANEL_HINT)
    if hi is None or not ({_cell(c) for c in rows[hi]} & {"CKT", "CIRCUIT", "CKT NO", "CKT#"}):
        return None
    header = [_cell(c) for c in rows[hi]]
    body = [r for r in rows[hi + 1:] if any(c.strip() for c in r)]
    halves = [(0, len(header))]
    ck = [k for k, h in enumerate(header) if h in ("CKT", "CIRCUIT", "CKT NO", "CKT#")]
    if len(ck) == 2 and len(header) % 2 == 0:  # two-column panel schedule: odd | even circuits
        halves = [(0, len(header) // 2), (len(header) // 2, len(header))]
    circuits: list[dict] = []
    for a, b in halves:
        cols = header[a:b]
        for r in body:
            d = {cols[k]: r[a + k].strip() for k in range(len(cols)) if a + k < len(r)}
            if any(d.values()):
                circuits.append(d)
    circuits.sort(key=lambda d: (_num(next((d[c] for c in ("CKT", "CIRCUIT", "CKT NO", "CKT#") if c in d), "")) or 1e9))
    va_cols = [h for h in header if h in ("VA", "LOAD", "LOAD VA", "KVA")]
    total = sum((_num(d.get(c, "")) or 0) for d in circuits for c in va_cols[:1])
    info: dict[str, Any] = {"kind": "panel_schedule", "sheet": sheet.number, "page": table.page + 1,
                            "bbox": [round(v, 1) for v in (table.bbox.x0, table.bbox.top, table.bbox.x1, table.bbox.bottom)],
                            "columns": header, "circuits": circuits, "n_circuits": len(circuits)}
    if va_cols:
        info["total_" + va_cols[0].lower().replace(" ", "_")] = round(total, 2)
    head = " ".join(l.text for l in _lines_near_above(lines, table.bbox)[:2])
    m = re.search(r"\bPANEL(?:BOARD)?\s+([A-Z0-9][A-Z0-9\-.]*)", head.upper())
    info["panel"] = m.group(1) if m else ""
    for key, rx in (("voltage", r"\b(\d{3}(?:Y?/\d{3})?\s*V)\b"), ("phase", r"\b([13])\s*(?:PH|Ø|PHASE)\b"),
                    ("wires", r"\b(\d)\s*W(?:IRE)?\b"), ("amps", r"\b(\d{2,4})\s*A\b"), ("main", r"\b(MLO|MCB|MAIN LUGS?|MAIN BREAKER)\b")):
        mm = re.search(rx, head.upper())
        info[key] = mm.group(1).replace(" ", "") if mm else ""
    info["header_text"] = head
    return info


def _equipment(table, lines: list[TLine], sheet: Sheet) -> dict | None:
    rows = [[str(c or "") for c in r] for r in table.rows]
    hi = _header_index(rows, {"TAG", "MARK", "EQUIP", "EQUIPMENT", "ITEM", "TAG NO"}, _EQUIP_HINT)
    if hi is None:
        return None
    header = [_cell(c) for c in rows[hi]]
    items = [{header[k]: r[k].strip() for k in range(min(len(header), len(r)))} for r in rows[hi + 1:] if any(c.strip() for c in r)]
    near = _lines_near_above(lines, table.bbox)
    title = next((l.text for l in near if re.search(r"LIST|SCHEDULE|EQUIPMENT", l.text, re.I)), near[0].text if near else "")
    return {"kind": "equipment_list", "sheet": sheet.number, "page": table.page + 1, "title": title,
            "bbox": [round(v, 1) for v in (table.bbox.x0, table.bbox.top, table.bbox.x1, table.bbox.bottom)],
            "columns": header, "items": items, "n_items": len(items)}


def _general_notes(lines: list[TLine], sheet: Sheet, tb: Rect | None) -> list[dict]:
    body = [l for l in lines if not (tb and _center_in(l.rect, tb))]
    if not body:
        return []
    med = statistics.median(l.size for l in body)
    out: list[dict] = []
    for h in body:
        t = h.text.strip().upper()
        if not _NOTES_HEAD.match(t) or not (h.size >= 1.12 * med or h.bold):
            continue
        f = h.frame
        col = sorted([l for l in body if l.dir == h.dir and l is not h and l.frame.top > f.bottom - 1
                      and f.x0 - 0.6 * IN <= l.frame.x0 <= f.x0 + 3 * IN and l.frame.top - f.bottom < 40 * IN],
                     key=lambda l: (l.frame.top, l.frame.x0))
        notes: list[dict] = []
        last_bottom, base_x = f.bottom, None
        for l in col:
            if l.frame.top - last_bottom > 3.2 * max(l.size, med):
                break
            if _NOTES_HEAD.match(l.text.strip().upper()) and l.size >= 1.12 * med:
                break
            m = _NOTE_ITEM.match(l.text.strip())
            if m and (base_x is None or abs(l.frame.x0 - base_x) < 0.4 * IN):
                base_x = l.frame.x0 if base_x is None else base_x
                notes.append({"n": m.group(1), "text": m.group(2).strip()})
            elif notes and base_x is not None and l.frame.x0 >= base_x - 2:
                notes[-1]["text"] += " " + l.text.strip()
            elif not notes:
                continue
            else:
                break
            last_bottom = l.frame.bottom
        if len(notes) >= 2:
            out.append({"kind": "general_notes", "sheet": sheet.number, "page": sheet.page + 1, "heading": h.text.strip(),
                        "notes": notes, "n_notes": len(notes),
                        "refs": sorted({m.group(0) for n in notes for m in re.finditer(r"\b[A-Z]{1,2}-?\d{1,3}(?:\.\d{1,3})?\b", n["text"])
                                        if SHEET_RX.match(m.group(0))})})
    return out


def _one_line(pt: PageText, lines: list[TLine], sheet: Sheet) -> dict | None:
    tb = sheet.title_block
    body = [l for l in lines if not (tb and _center_in(l.rect, tb))]
    med = statistics.median(l.size for l in body) if body else 0.0
    # a ONE-LINE heading (short, larger or bold) or the sheet title — not a "see one-line on E-101" note
    title_hit = bool(_ONELINE_TITLE.search(sheet.title)) or any(
        _ONELINE_TITLE.search(l.text) and len(l.text.split()) <= 7 and (l.size >= 1.15 * med or l.bold) for l in body)
    equip = [l for l in body if _ONELINE_EQUIP.search(l.text)]
    kinds = {m.group(1).upper().split()[-1] for l in equip for m in _ONELINE_EQUIP.finditer(l.text)}
    W, H = pt.W, pt.H
    heavy = [p for p in pt.paths(1.0 * IN) if p.stroke >= 1.75 and not (tb and _center_in(p.rect, tb))
             and p.rect.width < 0.9 * W and p.rect.height < 0.9 * H]
    score = (3 if title_hit else 0) + min(4, len(kinds)) + (2 if len(heavy) >= 3 else 0)
    if not title_hit or score < 5:
        return None
    items = []
    for l in equip:
        d: dict[str, Any] = {"text": l.text, "bbox": [round(v, 1) for v in (l.rect.x0, l.rect.top, l.rect.x1, l.rect.bottom)]}
        u = l.text.upper()
        for key, rx in (("kva", r"(\d+(?:\.\d+)?)\s*KVA"), ("amps", r"\b(\d{2,4})\s*A(?:MP)?\b"), ("hp", r"(\d+(?:\.\d+)?)\s*HP"),
                        ("voltage", r"(\d+(?:\.\d+)?\s*KV-\d{3}Y?/\d{3}V|\d+(?:\.\d+)?\s*KV\b|\d{3}Y?/\d{3}V|\d{3}V\b)")):
            mm = re.search(rx, u)
            if mm:
                d[key] = mm.group(1)
        d["type"] = next((t for t, rx in (("transformer", r"XFMR|TRANSFORMER|KVA"), ("switchboard", r"MDP|SWBD|SWITCHBOARD|SWITCHGEAR"),
                                            ("mcc", r"\bMCC\b"), ("vfd", r"\bVFD\b"), ("panel", r"\bPANEL\b"), ("generator", r"\bGEN"),
                                            ("ats", r"\bATS\b"), ("utility", r"UTILITY")) if re.search(rx, u)), "other")
        items.append(d)
    return {"kind": "one_line", "sheet": sheet.number, "page": sheet.page + 1, "title": sheet.title,
            "score": score, "heavy_lines": len(heavy), "equipment": items, "n_equipment": len(items)}


def read_set(path: str | Path, *, out: str | Path | None = None, json_out: str | Path | None = None,
             password: str | None = None, quiet: bool = False) -> OpResult:
    """Find and classify one-line diagrams, panel schedules, general notes and equipment lists in a drawing set.

    Writes an XLSX (one worksheet per item found, plus a Summary) and a JSON with the structured data.
    data: ``items`` [{kind, sheet, page, …}], ``counts`` by kind, ``sheets``.
    """
    from pdfcore.ops.extract import find_tables

    src = Path(path)
    items: list[dict] = []
    with open_set(src, password) as doc:
        sheets = read_sheets(src, password=password, scan_scale=False, _doc=doc)
        cand_pages: list[int] = []
        page_lines: dict[int, list[TLine]] = {}
        for s in sheets:
            pt = doc[s.page]
            lines = pt.lines()
            page_lines[s.page] = lines
            ol = _one_line(pt, lines, s)
            if ol:
                items.append(ol)
            items += _general_notes(lines, s, s.title_block)
            words = {w.upper().strip(".:") for l in lines for w in l.text.split()}
            if words & {"CKT", "CIRCUIT", "BKR", "TAG", "FLA", "HP"}:
                cand_pages.append(s.page)
            doc.release(s.page)
    by_page = {s.page: s for s in sheets}
    if cand_pages:
        for t in find_tables(src, pages=cand_pages, password=password):
            s = by_page[t.page]
            if s.title_block and _center_in(t.bbox, s.title_block):
                continue
            got = _panel(t, page_lines.get(t.page, []), s) or _equipment(t, page_lines.get(t.page, []), s)
            if got:
                items.append(got)
    items.sort(key=lambda d: (d["page"], d["kind"]))
    counts: dict[str, int] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    xp = Path(out) if out else output_path(src, "set", ".xlsx")
    jp = Path(json_out) if json_out else xp.with_suffix(".json")
    data = {"source": str(src), "items": items, "counts": counts, "sheets": [s.to_dict() for s in sheets],
            "pages_are": "1-based"}
    jp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    _set_xlsx(items, sheets, xp)
    _verify_files([xp, jp])
    if not quiet:
        print(f"{xp}  PASS")
    return OpResult(xp, None, [xp, jp], data)


def _set_xlsx(items: list[dict], sheets: Sequence[Sheet], path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    names = {"one_line": "One-line", "panel_schedule": "Panel", "general_notes": "Notes", "equipment_list": "Equipment"}
    _xlsx_table(wb, "Summary", ["Kind", "Sheet No.", "Page", "Name", "Count", "Worksheet"], [])
    summary = wb["Summary"]
    for it in items:
        kind = it["kind"]
        label = it.get("panel") or it.get("heading") or it.get("title") or ""
        ws_title = f"{it['sheet']} {names[kind]}" + (f" {it['panel']}" if it.get("panel") else "")
        if kind == "panel_schedule":
            cols = it["columns"] if len(it["columns"]) == len(set(it["columns"])) else list(dict.fromkeys(it["columns"]))
            rows = [[_numeric(c.get(k, "")) for k in cols] for c in it["circuits"]]
            ws = _xlsx_table(wb, ws_title, cols, rows)
            count = it["n_circuits"]
        elif kind == "equipment_list":
            rows = [[_numeric(c.get(k, "")) for k in it["columns"]] for c in it["items"]]
            ws = _xlsx_table(wb, ws_title, it["columns"], rows)
            count = it["n_items"]
        elif kind == "general_notes":
            ws = _xlsx_table(wb, ws_title, ["No.", "Note", "Sheet references"],
                             [[_numeric(n["n"]), n["text"], ", ".join(sorted({m.group(0) for m in re.finditer(r"\b[A-Z]{1,2}-?\d{1,3}(?:\.\d{1,3})?\b", n["text"])}))] for n in it["notes"]],
                             widths=[6, 100, 20])
            count = it["n_notes"]
        else:
            ws = _xlsx_table(wb, ws_title, ["Type", "Label", "kVA", "Amps", "HP", "Voltage"],
                             [[e.get("type", ""), e["text"], _numeric(e.get("kva", "")), _numeric(e.get("amps", "")),
                               _numeric(e.get("hp", "")), e.get("voltage", "")] for e in it["equipment"]])
            count = it["n_equipment"]
        summary.append([names[kind], it["sheet"], it["page"], label, count, ws.title])
    if not items:
        summary.append(["(nothing found)", "", "", "", 0, ""])
    wb.save(path)


def _numeric(v: Any) -> Any:
    if isinstance(v, str):
        n = _num(v)
        if n is not None and re.fullmatch(r"[\d,]+(\.\d+)?", v.strip()):
            return int(n) if n.is_integer() else n
    return v


# ====================================================================== compare sets by sheet number


def _render(pt: PageText, dpi: float):
    return pt.page.render(scale=dpi / 72.0, draw_annots=True).to_pil().convert("RGB")


def _page_tokens(pt: PageText, page: int, ignore: Rect | None) -> list:
    from pdfcore.ops.compare import Token, _norm

    toks = []
    for ln in sorted(pt.lines(), key=lambda l: (l.dir, round(l.frame.top / 4), l.frame.x0)):
        if ignore is not None and _center_in(ln.rect, ignore):
            continue
        for w, a, b in ln.words():
            toks.append(Token(w, _norm(w), page, ln.sub_rect(a, b)))
    return toks


def _words_in(pt: PageText, box: Rect) -> str:
    ls = [l for l in pt.lines(box.expand(2))]
    return " ".join(l.text for l in sorted(ls, key=lambda l: (round(l.rect.top / 6), l.rect.x0)))[:300]


def _field_changes(a: Sheet, b: Sheet) -> list[str]:
    out = []
    for f, lab in (("rev", "REV"), ("date", "DATE"), ("title", "TITLE")):
        va, vb = getattr(a, f), getattr(b, f)
        if va != vb:
            out.append(f"{lab} {va or '—'} → {vb or '—'}")
    return out


def compare_sets(old: str | Path, new: str | Path, *, out: str | Path | None = None, dpi: float = 72.0,
                 max_px: int = 3600, threshold: float = 48.0, merge_pt: float = 14.0, password: str | None = None,
                 new_password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Compare two issues of a drawing set sheet by sheet (matched by sheet number, not page index).

    For every matched sheet the pages are rendered (low *dpi*, capped at *max_px* on the long side), composited
    (old only red, new only blue, both gray) and the differences outside the title block become revision clouds
    with numbered delta tags. Differences inside the title block (rev/date/issue changes) are reported once per
    sheet as "title block updated", not clouded. Added sheets get a page (all blue); removed sheets are reported.

    Outputs: overlay PDF (``output``; one page per changed or added sheet, bookmarked and labelled by sheet
    number) + delta report PDF + XLSX (``extra_outputs``). data: ``sheets`` (per-sheet status), ``added``,
    ``removed``, ``changed``, ``unchanged``, ``title_block_only``.
    """
    import pikepdf
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    from pdfcore.fonts import register_reportlab
    from pdfcore.ops import compare as cmp
    from pdfcore.ops import markup
    from pdfcore.ops.navigate import Bookmark, write_outline
    from pdfcore.ops.organize import LabelRange, write_label_ranges
    from PIL import Image

    register_reportlab()
    old, new = Path(old), Path(new)
    dst = output_path(new, "compare", out=out)
    rep_pdf = dst.with_name(dst.stem + ".report.pdf") if out else output_path(new, "delta-report", ".pdf")
    rep_xlsx = rep_pdf.with_suffix(".xlsx")
    rows: list[dict] = []
    pages_meta: list[dict] = []
    with open_set(old, password) as da, open_set(new, new_password or password) as db:
        sa = read_sheets(old, password=password, scan_scale=False, _doc=da)
        sb = read_sheets(new, password=new_password or password, scan_scale=False, _doc=db)
        old_by = {}
        for s in sa:
            old_by.setdefault(s.key, s)
        new_keys = {s.key for s in sb}
        removed = [s for s in sa if s.key not in new_keys]
        c = canvas.Canvas(str(dst), pageCompression=1, initialFontName="Inter")
        c.setTitle(f"Sheet compare — {old.name} vs {new.name}")
        c.setAuthor("PDF Studio")
        c.setSubject("Drawing set comparison overlay by sheet number")
        c.setCreator("pdfcore plans compare")
        seen: set[str] = set()
        for b in sb:
            if b.key in seen:
                continue
            seen.add(b.key)
            a = old_by.get(b.key)
            pb = db[b.page]
            d = min(dpi, max_px / (max(pb.W, pb.H) / 72.0))
            s_ = d / 72.0
            ib = _render(pb, d)
            row: dict[str, Any] = {"sheet": b.number, "title": b.title, "new_page": b.page + 1,
                                   "old_page": a.page + 1 if a else None, "status": "", "changes": 0, "boxes": [],
                                   "title_block_updated": False, "title_block_notes": [], "text_changes": []}
            if a is None:
                ia = Image.new("RGB", ib.size, "white")
                boxes = cmp._ink_box(ib, d)
                row["status"] = "added"
            else:
                pa = da[a.page]
                ia = _render(pa, d)
                ign = [r for r in (a.title_block, b.title_block) if r is not None]
                boxes = cmp.find_changes(ia, ib, dpi=d, threshold=threshold, merge_pt=merge_pt, ignore=ign)
                # title block differences: compare only inside the title block
                if ign:
                    tb = Rect.union(ign)
                    box = (int(tb.x0 * s_), int(tb.top * s_), int(math.ceil(tb.x1 * s_)), int(math.ceil(tb.bottom * s_)))
                    if cmp.find_changes(ia.crop(box), ib.crop(box), dpi=d, threshold=threshold):
                        row["title_block_updated"] = True
                        row["title_block_notes"] = _field_changes(a, b) or ["title block content changed"]
                if boxes:
                    tc = cmp._diff(_page_tokens(pa, a.page, Rect.union(ign) if ign else None),
                                   _page_tokens(pb, b.page, Rect.union(ign) if ign else None))
                    row["text_changes"] = [{"kind": t["kind"], "old": t["old_text"], "new": t["new_text"]} for t in tc][:200]
                row["status"] = "changed" if boxes else ("title block only" if row["title_block_updated"] else "unchanged")
            row["changes"] = len(boxes)
            if boxes:
                details = []
                for k, r in enumerate(boxes):
                    det = {"n": k + 1, "box": [round(v, 1) for v in (r.x0, r.top, r.x1, r.bottom)],
                           "where_in": [round(r.x0 / IN, 1), round(r.top / IN, 1)],
                           "new_text": _words_in(pb, r)}
                    if a is not None:
                        det["old_text"] = _words_in(da[a.page], r)
                    details.append(det)
                row["boxes"] = details
                comp = cmp.composite(ia, ib)
                W, H = max(ia.width, ib.width), max(ia.height, ib.height)
                pw, ph = W / s_, H / s_
                c.setPageSize((pw, ph))
                buf = io.BytesIO()
                comp.save(buf, "PNG")
                buf.seek(0)
                c.drawImage(ImageReader(buf), 0, 0, pw, ph)
                tag = max(12.0, min(26.0, 0.009 * max(pw, ph)))
                for k, r in enumerate(boxes):
                    cr, diam = cmp._cloud_geom(r, pw, ph)
                    cmp._delta_tag(c, k + 1, cr.expand(diam / 2 + 1), pw, ph, tag)
                if a is not None and (a.title_block or b.title_block):
                    _tb_marker(c, Rect.union([r for r in (a.title_block, b.title_block) if r]), ph, row["title_block_updated"])
                _sheet_legend(c, old.name, new.name, pw, ph, b.number, len(boxes), row["status"])
                c.showPage()
                row["overlay_page"] = len(pages_meta) + 1
                pages_meta.append({"sheet": b, "size": (pw, ph), "boxes": boxes})
            rows.append(row)
            da.release(a.page) if a is not None else None
            db.release(b.page)
        if not pages_meta:
            c.setPageSize((792, 612))
            c.setFont("Inter-Bold", 18)
            c.drawString(54, 540, "No drawing changes")
            c.setFont("Inter", 11)
            c.drawString(54, 515, f"{old.name} → {new.name}: every matched sheet is unchanged outside its title block.")
            c.showPage()
        c.save()
    # clouds, bookmarks, labels
    with pikepdf.open(str(dst), allow_overwriting_input=True) as pdf:
        outline = []
        for k, pm in enumerate(pages_meta):
            pw, ph = pm["size"]
            for j, r in enumerate(pm["boxes"]):
                cr, diam = cmp._cloud_geom(r, pw, ph)
                markup.add_cloud(pdf, k, cr, color=cmp.CLOUD_RGB, width=1.6, scallop=diam, author="pdfcore plans compare",
                                 subject="Revision cloud", contents=f"{pm['sheet'].number}: change {j + 1}")
            sh = pm["sheet"]
            outline.append(Bookmark(f"{sh.number} — {sh.title} ({len(pm['boxes'])} change{'s' if len(pm['boxes']) != 1 else ''})"
                                    if sh.title else sh.number, k))
        if outline:
            write_outline(pdf, outline)
            write_label_ranges(pdf, [LabelRange(k, None, pm["sheet"].number, 1) for k, pm in enumerate(pages_meta)])
        pdf.save(str(dst))
    summary = {
        "changed": [r["sheet"] for r in rows if r["status"] == "changed"],
        "added": [r["sheet"] for r in rows if r["status"] == "added"],
        "removed": [s.number for s in removed],
        "unchanged": [r["sheet"] for r in rows if r["status"] in ("unchanged", "title block only")],
        "title_block_only": [r["sheet"] for r in rows if r["status"] == "title block only"],
        "title_block_updated": [r["sheet"] for r in rows if r["title_block_updated"]],
    }
    data = {"old": str(old), "new": str(new), "sheets": rows, **summary,
            "removed_detail": [{"sheet": s.number, "title": s.title, "old_page": s.page + 1} for s in removed]}
    _delta_report_pdf(rep_pdf, old, new, rows, summary, removed)
    _delta_report_xlsx(rep_xlsx, rows, removed)
    from pdfcore.gate import GateOptions, run_gate

    _verify_files([rep_xlsx])
    rg = run_gate(rep_pdf, options=GateOptions(profile="typeset", margins_in=0.5))
    # The overlay is regenerated from renders + house fonts, so nothing in it is inherited from the inputs:
    # gate it on its own (a baseline would re-read both full sets).
    res = finalize(dst, baseline=None, options=options, extra=[rep_pdf, rep_xlsx], data=data, quiet=quiet)
    if not quiet:
        print(f"{rep_pdf}  {rg.line()}")
        print(f"{rep_xlsx}  PASS")
    if res.ok and not rg.passed:
        res.gate = rg
    return res


def _tb_marker(c, r: Rect, ph: float, updated: bool) -> None:
    c.saveState()
    c.setStrokeColorRGB(0.45, 0.45, 0.45)
    c.setDash(6, 4)
    c.setLineWidth(0.8)
    c.rect(r.x0 + 2, ph - r.bottom + 2, r.width - 4, r.height - 4, stroke=1, fill=0)
    c.setDash()
    fs = 7.5
    c.setFont("Inter", fs)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    label = "Title block updated (not clouded)" if updated else "Title block (not compared)"
    from reportlab.pdfbase.pdfmetrics import stringWidth

    tw = stringWidth(label, "Inter", fs)
    x = max(4.0, r.x1 - tw - 6)
    c.setFillColorRGB(1, 1, 1)
    c.rect(x - 2, ph - r.top - fs - 7, tw + 4, fs + 3, stroke=0, fill=1)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawString(x, ph - r.top - fs - 5, label)
    c.restoreState()


def _sheet_legend(c, old_name: str, new_name: str, pw: float, ph: float, sheet: str, n: int, status: str) -> None:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    from pdfcore.ops import compare as cmp

    fs = max(7.0, min(10.0, 0.0045 * max(pw, ph)))
    lines = [(cmp.OLD_RGB, f"Only in old: {old_name}"), (cmp.NEW_RGB, f"Only in new: {new_name}"),
             (cmp.SAME_RGB, "Unchanged"),
             (tuple(int(v * 255) for v in cmp.CLOUD_RGB), f"Sheet {sheet}: {n} change region(s)" + (" — new sheet" if status == "added" else ""))]
    tw = max(stringWidth(t, "Inter", fs) for _, t in lines)
    bw, bh = tw + fs * 3.2, fs * 1.45 * len(lines) + fs
    x, y = fs + 30, fs + 30
    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.setFillAlpha(0.9)
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


def _delta_report_pdf(dst: Path, old: Path, new: Path, rows: list[dict], summary: dict, removed: list[Sheet]) -> None:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from pdfcore.fonts import register_reportlab

    register_reportlab()
    base = ParagraphStyle("b", fontName="Inter", fontSize=9, leading=12)
    small = ParagraphStyle("s", parent=base, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#555555"))
    cellb = ParagraphStyle("cb", parent=base, fontName="Inter-Bold")
    h1 = ParagraphStyle("h1", parent=base, fontName="Inter-Bold", fontSize=18, leading=22, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=base, fontName="Inter-Bold", fontSize=12.5, leading=16, spaceBefore=12, spaceAfter=4,
                        keepWithNext=1)
    red, blue = "#c81e1e", "#1a4bd6"

    def P(t: str, st=base) -> Paragraph:
        return Paragraph(escape(t), st)

    story: list[Any] = [Paragraph("Drawing set comparison — delta report", h1),
                        P(f"Old: {old.name}    New: {new.name}    Generated {datetime.now():%Y-%m-%d %H:%M}", small),
                        Spacer(1, 8)]
    facts = [["Sheets compared", str(len([r for r in rows if r['status'] != 'added']))],
             ["Changed", ", ".join(summary["changed"]) or "none"],
             ["Unchanged (outside the title block)", ", ".join(summary["unchanged"]) or "none"],
             ["Added sheets", ", ".join(summary["added"]) or "none"],
             ["Removed sheets", ", ".join(summary["removed"]) or "none"]]
    t = Table([[P(k, cellb), P(v)] for k, v in facts], colWidths=[2.3 * inch, 4.7 * inch])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("FONTNAME", (0, 0), (-1, -1), "Inter")]))
    story.append(t)
    # title-block updates, summarized once
    tbu = [r for r in rows if r["title_block_updated"]]
    if tbu:
        story.append(Paragraph("Title block updates", h2))
        notes: dict[str, list[str]] = {}
        for r in tbu:
            notes.setdefault("; ".join(r["title_block_notes"]), []).append(r["sheet"])
        for n, shs in notes.items():
            who = "all matched sheets" if len(shs) == len([r for r in rows if r["status"] != "added"]) and len(shs) > 1 else ", ".join(shs)
            story.append(P(f"{n} — {who}"))
        story.append(P("Title-block differences are not clouded on the overlay.", small))
    story.append(Paragraph("Sheets", h2))
    hdr = [P(x, cellb) for x in ("Sheet", "Title", "Status", "Changes", "Overlay p.", "Old p.", "New p.")]
    body = [[P(r["sheet"]), P(r["title"] or ""), P(r["status"] + (" · title block updated" if r["title_block_updated"] and r["status"] != "title block only" else "")),
             P(str(r["changes"])), P(str(r.get("overlay_page", "—"))), P(str(r["old_page"] or "—")), P(str(r["new_page"]))]
            for r in rows]
    body += [[P(s.number), P(s.title), P("removed"), P("—"), P("—"), P(str(s.page + 1)), P("—")] for s in removed]
    tt = Table([hdr] + body, colWidths=[0.75 * inch, 2.3 * inch, 1.45 * inch, 0.75 * inch, 0.65 * inch, 0.5 * inch, 0.6 * inch],
               repeatRows=1)
    tt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf3")),
                            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTNAME", (0, 0), (-1, -1), "Inter")]))
    story.append(tt)
    for r in rows:
        if not r["boxes"]:
            continue
        heading = Paragraph(escape(f"{r['sheet']} — {r['title']}") + f" <font color='#666666' size='9'>· overlay page {r['overlay_page']}</font>", h2)
        for i, b in enumerate(r["boxes"]):
            x, y = b["where_in"]
            mid = ""
            if b.get("old_text"):
                mid += f"<br/><font color='{red}'>old: {escape(b['old_text'][:220])}</font>"
            if b.get("new_text"):
                mid += f"<br/><font color='{blue}'>new: {escape(b['new_text'][:220])}</font>"
            block = [Paragraph(f"<font name='Inter-Bold'>Δ{b['n']}</font> at {x}″ from left, {y}″ from top{mid}", base), Spacer(1, 4)]
            # keep the sheet heading with its first change (no heading stranded at a page foot)
            story.append(KeepTogether(([heading] if i == 0 else []) + block))
        if r["text_changes"]:
            items = []
            for tc in r["text_changes"][:40]:
                s = ""
                if tc["old"]:
                    s += f"<font color='{red}'><strike>{escape(tc['old'][:200])}</strike></font> "
                if tc["new"]:
                    s += f"<font color='{blue}'><u>{escape(tc['new'][:200])}</u></font>"
                items.append(Paragraph(f"{tc['kind']}: {s}", small))
            h3 = Paragraph("Text changes on this sheet", ParagraphStyle("h3", parent=base, fontName="Inter-Bold", spaceBefore=4))
            story.append(KeepTogether([h3, items[0]]))
            story += items[1:]

    def footer(cv, doc):
        cv.saveState()
        cv.setFont("Inter", 8)
        cv.setFillColor(colors.HexColor("#666666"))
        cv.drawString(0.75 * inch, 0.45 * inch, f"Delta report — {old.name} vs {new.name}")
        cv.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"Page {doc.page}")
        cv.restoreState()

    doc = SimpleDocTemplate(str(dst), pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.75 * inch,
                            bottomMargin=0.75 * inch, title=f"Delta report — {old.name} vs {new.name}", author="PDF Studio",
                            subject="Drawing set delta report by sheet", creator="pdfcore plans compare", lang="en-US",
                            initialFontName="Inter")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def _delta_report_xlsx(path: Path, rows: list[dict], removed: list[Sheet]) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _xlsx_table(wb, "Summary", ["Sheet No.", "Title", "Status", "Changes", "Title block updated", "Title block notes",
                                "Overlay page", "Old page", "New page"],
                [[r["sheet"], r["title"], r["status"], r["changes"], "yes" if r["title_block_updated"] else "",
                  "; ".join(r["title_block_notes"]), r.get("overlay_page"), r["old_page"], r["new_page"]] for r in rows]
                + [[s.number, s.title, "removed", None, "", "", None, s.page + 1, None] for s in removed])
    _xlsx_table(wb, "Changes", ["Sheet No.", "Delta", "x (in)", "y (in)", "Box (pt)", "Old text", "New text"],
                [[r["sheet"], b["n"], b["where_in"][0], b["where_in"][1], ", ".join(str(v) for v in b["box"]),
                  b.get("old_text", ""), b.get("new_text", "")] for r in rows for b in r["boxes"]])
    _xlsx_table(wb, "Text changes", ["Sheet No.", "Kind", "Old", "New"],
                [[r["sheet"], t["kind"], t["old"], t["new"]] for r in rows for t in r["text_changes"]])
    wb.save(path)


# ====================================================================== batch link


def _sheet_alt(numbers: Iterable[str]) -> str:
    pats = []
    for n in sorted(set(numbers), key=len, reverse=True):
        m = re.match(r"([A-Z]+)-?(.*)$", n.upper())
        pats.append(re.escape(m.group(1)) + "-?" + re.escape(m.group(2)) if m else re.escape(n))
    return "|".join(pats)


_REF_BEFORE = re.compile(r"(?:\bSEE|\bREFER\s+TO|\bREF\.?|\bON|\bPER|\bSHEET|\bSHT\.?|\bDWG\.?|\bDETAIL|\bDETAILS)\s*:?\s*$", re.I)


def detail_bubbles(pt: PageText) -> dict[str, list[dict]]:
    """Detail bubbles on a page: short labels (1–3 chars) centered in a roughly round/square path 0.25–1.5 in
    across. → {label: [{"bubble": Rect, "title": str, "region": Rect | None}]} (displayed points)."""
    shapes = [p.rect for p in pt.paths(0.22 * IN) if 0.22 * IN <= p.rect.width <= 1.6 * IN
              and 0.75 <= p.rect.width / max(p.rect.height, 0.1) <= 1.33]
    if not shapes:
        return {}
    frames = [p.rect for p in pt.paths(2 * IN) if p.rect.width >= 2 * IN and p.rect.height >= 2 * IN
              and p.rect.width < 0.8 * pt.W]
    lines = pt.lines()
    out: dict[str, list[dict]] = {}
    for ln in lines:
        t = ln.text.strip()
        if not re.fullmatch(r"[A-Z0-9]{1,3}", t):
            continue
        r = ln.rect
        cx, cy = (r.x0 + r.x1) / 2, (r.top + r.bottom) / 2
        hits = [s for s in shapes if s.contains_point(cx, cy) and abs((s.x0 + s.x1) / 2 - cx) < 0.25 * s.width
                and abs((s.top + s.bottom) / 2 - cy) < 0.3 * s.height and ln.size >= 0.2 * s.height]
        if not hits:
            continue
        b = min(hits, key=lambda s: s.area)
        title = [l for l in lines if l is not ln and l.dir == ln.dir and abs((l.rect.top + l.rect.bottom) / 2 - cy) < 0.7 * b.height
                 and 0 <= l.rect.x0 - b.x1 < 1.2 * IN and len(l.text) >= 4]
        region = None
        near = [f for f in frames if f.x0 - 0.5 * IN <= b.x1 and f.x1 + 0.5 * IN >= b.x0
                and (-0.2 * IN <= b.top - f.bottom <= 1.5 * IN or f.contains_point(cx, cy))]
        if near:
            region = Rect.union([min(near, key=lambda f: f.area), b] + [l.rect for l in title[:1]])
        out.setdefault(t, []).append({"bubble": b, "title": title[0].text if title else "", "region": region})
    return out


def _best_bubble(cands: list[dict]) -> dict | None:
    titled = [c for c in cands if c["title"]]
    pool = titled or (cands if len(cands) == 1 else [])
    if not pool:
        return None
    return max(pool, key=lambda c: c["bubble"].area)


def link_set(path: str | Path, *, out: str | Path | None = None, index_entries: bool = True, details: bool = True,
             password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Batch link: hyperlink sheet references across a set.

    Links "SEE E-201", "REFER TO E-101", standalone sheet numbers that exist in the set, detail callouts
    "3/E-501" (to the detail bubble "3" on E-501 when one is found — zoomed to the detail with /FitR — else to the
    sheet), and sheet-index rows (number + title). Never links a sheet to itself and never over an existing link.
    data: ``links`` [{page, sheet, text, kind, to_sheet, to_page, dest}], ``per_sheet`` {sheet: {kind: n}}, ``total``.
    """
    import pikepdf
    from pikepdf import Array, Dictionary, Name, String

    from pdfcore.ops.navigate import _annots, save_pdf

    src = Path(path)
    dst = output_path(src, "linked", out=out)
    found: list[dict] = []
    with open_set(src, password) as doc:
        sheets = read_sheets(src, password=password, scan_scale=False, _doc=doc)
        by_key: dict[str, Sheet] = {}
        for s in sheets:
            if s.source == "title-block":
                by_key.setdefault(s.key, s)
        if not by_key:
            raise ValueError("no sheet numbers found in the title blocks; nothing to link")
        alt = _sheet_alt(s.number for s in by_key.values())
        rx = re.compile(rf"(?<![A-Za-z0-9/.\-])(?:(?P<det>[A-Z0-9]{{1,3}})\s?/\s?)?(?P<sh>{alt})(?![A-Za-z0-9]|[.\-]\d)")
        bubbles: dict[int, dict[str, list[dict]]] = {}
        for s in sheets:
            pt = doc[s.page]
            lines = pt.lines()
            for ln in lines:
                for m in rx.finditer(ln.text):
                    tgt = by_key.get(norm_number(m.group("sh")))
                    if tgt is None or tgt.page == s.page:
                        continue
                    a, b = (m.start("det") if m.group("det") else m.start("sh")), m.end("sh")
                    kind = "detail" if m.group("det") else ("reference" if _REF_BEFORE.search(ln.text[:a]) else "token")
                    rect = ln.sub_rect(a, b)
                    if kind == "token" and index_entries and tgt.title:
                        row = _index_row(ln, lines, m.end("sh"), tgt.title)
                        if row is not None:
                            kind, rect = "index", Rect.union([rect, row])
                    found.append({"page": s.page, "sheet": s.number, "text": ln.text[a:b], "kind": kind,
                                  "to_sheet": tgt.number, "to_page": tgt.page, "detail": m.group("det"), "rect": rect})
            doc.release(s.page)
        if details:
            for f in found:
                if f["kind"] == "detail" and f["to_page"] not in bubbles:
                    bubbles[f["to_page"]] = detail_bubbles(doc[f["to_page"]])
                    doc.release(f["to_page"])
    links: list[dict] = []
    per_sheet: dict[str, dict[str, int]] = {}
    with pikepdf.open(str(src), password=password or "") as pdf:
        existing: dict[int, list[Rect]] = {}
        for f in found:
            pg = pdf.pages[f["page"]]
            if f["page"] not in existing:
                existing[f["page"]] = [_disp_rect_of(pg, a.Rect) for a in (pg.obj.get("/Annots") or [])
                                       if a.get("/Subtype") == Name.Link and "/Rect" in a]
            r = f["rect"].expand(1.0)
            if any(e.overlap_fraction(r) > 0.5 or r.overlap_fraction(e) > 0.5 for e in existing[f["page"]]):
                continue
            tgt_pg = pdf.pages[f["to_page"]]
            dest: Any = Array([tgt_pg.obj, Name.Fit])
            dest_desc = "sheet"
            if f["kind"] == "detail" and details:
                bb = _best_bubble(bubbles.get(f["to_page"], {}).get(f["detail"], []))
                if bb is not None:
                    if bb["region"] is not None:
                        l, b_, r_, t = _pdf_rect_for(tgt_pg, bb["region"].expand(0.25 * IN))
                        dest = Array([tgt_pg.obj, Name.FitR, l, b_, r_, t])
                        dest_desc = "detail (zoomed)"
                    else:
                        from pdfcore.ops.markup import PageGeom

                        g = PageGeom(tgt_pg)
                        ux, uy = g.user_pt(max(0, bb["bubble"].x0 - IN), max(0, bb["bubble"].top - 4 * IN))
                        dest = Array([tgt_pg.obj, Name.XYZ, ux, uy, None])
                        dest_desc = "detail"
            annot = Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=Array(_pdf_rect_for(pg, r)), Border=Array([0, 0, 0]),
                               P=pg.obj, Dest=dest, H=Name.I, Contents=String(f"Go to {f['to_sheet']}"),
                               NM=String(f"pdfcore-link-{f['page']}-{len(links)}"))
            _annots(pdf, pg).append(pdf.make_indirect(annot))
            existing[f["page"]].append(r)
            row = {k: v for k, v in f.items() if k != "rect"}
            row.update({"page": f["page"] + 1, "to_page": f["to_page"] + 1, "dest": dest_desc,
                        "rect": [round(v, 1) for v in (r.x0, r.top, r.x1, r.bottom)]})
            links.append(row)
            ps = per_sheet.setdefault(f["sheet"], {"links": 0})
            ps["links"] += 1
            ps[f["kind"]] = ps.get(f["kind"], 0) + 1
        save_pdf(pdf, dst)
    for s in sheets:
        per_sheet.setdefault(s.number, {"links": 0})
    data = {"links": links, "per_sheet": per_sheet, "total": len(links),
            "skipped_existing": len(found) - len(links)}
    return finalize(dst, src, options=_gate_opts(options, len(sheets)), password=password, data=data, quiet=quiet)


def _index_row(ln: TLine, lines: list[TLine], end: int, title: str) -> Rect | None:
    """If the sheet number is followed (same line or same row) by the target sheet's title, the row's rect."""
    want = _tokens(title)
    if not want:
        return None
    rest = ln.text[end:]
    if rest.strip() and len(_tokens(rest) & want) >= max(1, 0.6 * len(want)):
        return ln.sub_rect(end, len(ln.text))
    f = ln.frame
    row = [l for l in lines if l is not ln and l.dir == ln.dir and abs(l.frame.top - f.top) < 0.5 * ln.size
           and 0 < l.frame.x0 - f.x1 < 8 * IN]
    if row:
        nxt = min(row, key=lambda l: l.frame.x0)
        if len(_tokens(nxt.text) & want) >= max(1, 0.6 * len(want)):
            return nxt.rect
    return None


# ====================================================================== slip sheets

_MARKUP_SKIP = {"/Link", "/Widget", "/Popup"}


def slipsheet(set_path: str | Path, revised: str | Path, *, out: str | Path | None = None, carry_markups: bool = True,
              note: bool = True, password: str | None = None, revised_password: str | None = None, options=None,
              quiet: bool = False) -> OpResult:
    """Replace sheets in a set with revised sheets, matched by sheet number.

    Page order is kept; revised sheets whose numbers are not in the set are appended in their order. Markup
    annotations on a replaced sheet (clouds, notes, text, shapes, stamps… — not links or form widgets) are carried
    onto the new page at the same position (anchored top-left if the page size changed). Links elsewhere that
    pointed at a replaced page are retargeted. Bookmarks and page labels are rebuilt from the sheet numbers, and a
    note listing the slipped sheets goes on the first page (and on each replaced page).
    data: ``replaced`` [{sheet, page, markups}], ``added`` [sheet], ``unchanged`` count.
    """
    import pikepdf

    from pdfcore.ops import markup
    from pdfcore.ops.navigate import save_pdf, write_outline
    from pdfcore.ops.organize import write_label_ranges

    set_path, revised = Path(set_path), Path(revised)
    dst = output_path(set_path, "slipsheeted", out=out)
    s_old = read_sheets(set_path, password=password, scan_scale=False)
    s_rev = read_sheets(revised, password=revised_password or password, scan_scale=False)
    rev_by: dict[str, Sheet] = {}
    for s in s_rev:
        if s.source == "title-block":
            rev_by.setdefault(s.key, s)
    old_keys = {s.key for s in s_old if s.source == "title-block"}
    replaced: list[dict] = []
    added: list[str] = []
    final: list[Sheet] = []
    today = datetime.now().strftime("%Y-%m-%d")
    with pikepdf.open(str(set_path), password=password or "") as pdf, \
            pikepdf.open(str(revised), password=revised_password or password or "") as rpdf:
        remap: dict[tuple[int, int], Any] = {}
        used: set[str] = set()
        for s in s_old:
            r = rev_by.get(s.key) if s.source == "title-block" and s.key not in used else None
            if r is None:
                final.append(s)
                continue
            used.add(s.key)
            i = s.page
            old_page = pdf.pages[i]
            old_obj = old_page.obj
            keep = []
            if carry_markups:
                keep = [a for a in (old_obj.get("/Annots") or []) if str(a.get("/Subtype", "")) not in _MARKUP_SKIP]
            pdf.pages.insert(i, rpdf.pages[r.page])
            new_page = pdf.pages[i]
            del pdf.pages[i + 1]
            remap[old_obj.objgen] = new_page.obj
            moved = _carry(pdf, keep, old_page, new_page) if keep else 0
            replaced.append({"sheet": s.number, "page": i + 1, "markups": moved,
                             "old_rev": s.rev, "new_rev": r.rev})
            final.append(Sheet(**{**r.__dict__, "page": i}))
        for r in s_rev:
            if r.source == "title-block" and r.key not in old_keys and r.key not in used:
                used.add(r.key)
                pdf.pages.append(rpdf.pages[r.page])
                final.append(Sheet(**{**r.__dict__, "page": len(pdf.pages) - 1}))
                added.append(r.number)
        if remap:
            _retarget(pdf, remap)
        from pdfcore.ops.organize import _drop_dangling_links

        _drop_dangling_links(pdf)  # links in revised pages to sheets that were not brought over
        write_outline(pdf, sheet_outline(final))
        write_label_ranges(pdf, _label_ranges(final))
        if note and (replaced or added):
            text = (f"Slip sheets {today} from {revised.name}: "
                    + (f"replaced {', '.join(x['sheet'] for x in replaced)}" if replaced else "")
                    + ("; " if replaced and added else "")
                    + (f"added {', '.join(added)}" if added else "") + ".")
            markup.add_note(pdf, 0, (18, 18), text, author="pdfcore plans slipsheet", subject="Slip sheets")
            for x in replaced:
                if x["page"] != 1:
                    markup.add_note(pdf, x["page"] - 1, (18, 18),
                                    f"Slip-sheeted {today} from {revised.name} (rev {x['old_rev'] or '?'} → {x['new_rev'] or '?'}); "
                                    f"{x['markups']} markup(s) carried over.", author="pdfcore plans slipsheet",
                                    subject="Slip sheet")
        save_pdf(pdf, dst)
    data = {"replaced": replaced, "added": added, "unchanged": len(final) - len(replaced) - len(added),
            "sheets": [s.number for s in final]}
    return finalize(dst, [set_path, revised], options=_gate_opts(options, len(final)), password=password, data=data,
                    quiet=quiet)


def _carry(pdf, annots: list, old_page, new_page) -> int:
    """Move markup annotations onto *new_page*, translating if the MediaBox top-left moved."""
    from pikepdf import Array, Name

    from pdfcore.ops.navigate import _annots

    mo = [float(v) for v in old_page.mediabox]
    mn = [float(v) for v in new_page.mediabox]
    dx = min(mn[0], mn[2]) - min(mo[0], mo[2])
    dy = max(mn[1], mn[3]) - max(mo[1], mo[3])
    arr = _annots(pdf, new_page)
    n = 0
    for a in annots:
        if dx or dy:
            for key, step in (("/Rect", 2), ("/QuadPoints", 2), ("/Vertices", 2), ("/L", 2), ("/CL", 2)):
                if key in a:
                    vals = [float(v) for v in a[key]]
                    a[key] = Array([v + (dx if k % 2 == 0 else dy) for k, v in enumerate(vals)])
            if "/InkList" in a:
                a.InkList = Array([Array([float(v) + (dx if k % 2 == 0 else dy) for k, v in enumerate(s)]) for s in a.InkList])
        if "/P" in a:
            a.P = new_page.obj
        pop = a.get("/Popup")
        if pop is not None:
            pop.P = new_page.obj
            if "/Rect" in pop and (dx or dy):
                pop.Rect = Array([float(v) + (dx if k % 2 == 0 else dy) for k, v in enumerate(pop.Rect)])
            arr.append(pop)
        arr.append(a)
        if a.get("/Subtype") != Name.Popup:
            n += 1
    return n


def _retarget(pdf, remap: dict) -> None:
    """Point destinations at replaced pages to their replacements (links, GoTo actions, named dests)."""
    from pikepdf import Array, Dictionary

    def fix(dest) -> None:
        if isinstance(dest, Array) and len(dest) > 0:
            try:
                tgt = remap.get(dest[0].objgen)
            except Exception:
                tgt = None
            if tgt is not None:
                dest[0] = tgt

    for page in pdf.pages:
        for a in page.obj.get("/Annots") or []:
            if "/Dest" in a:
                fix(a.Dest)
            act = a.get("/A")
            if isinstance(act, Dictionary) and "/D" in act:
                fix(act.D)
    from pdfcore.ops.navigate import named_dests

    for v in named_dests(pdf).values():
        fix(v.get("/D") if isinstance(v, Dictionary) else v)


# ====================================================================== CLI


def _print_sheets(sheets: Sequence[dict]) -> None:
    w = max([len(s["number"]) for s in sheets] + [5])
    for s in sheets:
        flag = "" if s["confidence"] >= 0.6 else "  (?)"
        print(f"  p.{s['page']:<4} {s['number']:<{w}}  {s['title'][:60]:<60}  rev {s['rev'] or '-':<3} {s['date'] or '':<10} "
              f"{s['size']}{flag}")


@sub("index", "Sheet index / drawing log from title blocks → XLSX + PDF bookmarked by discipline, page labels = sheet nos.")
def _cli_index(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--bookmark", action=argparse.BooleanOptionalAction, default=True,
                   help="write <name>.indexed.pdf with bookmarks + page labels (default on)")
    p.add_argument("--xlsx", action=argparse.BooleanOptionalAction, default=True, help="write the drawing log XLSX (default on)")
    p.add_argument("--json", action="store_true", help="print the sheets as JSON")
    add_common(p)

    def run(ns):
        res = index(ns.pdf, bookmark=ns.bookmark, xlsx=ns.xlsx, out=ns.out, password=ns.password,
                    options=gate_options_from(ns))
        if ns.json:
            print(json.dumps(res.data["sheets"], indent=1))
        else:
            _print_sheets(res.data["sheets"])
        for x in res.extra_outputs:
            print(f"  → {x}")
        return res

    return run


@sub("read", "Read a set: one-line diagrams, panel schedules, general notes, equipment lists → XLSX + JSON")
def _cli_read(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("-o", "--out", help="XLSX path (JSON goes next to it)")
    p.add_argument("--password")

    def run(ns):
        res = read_set(ns.pdf, out=ns.out, password=ns.password)
        for it in res.data["items"]:
            name = it.get("panel") or it.get("heading") or it.get("title") or ""
            n = it.get("n_circuits") or it.get("n_items") or it.get("n_notes") or it.get("n_equipment") or 0
            print(f"  p.{it['page']:<4} {it['sheet']:<8} {it['kind']:<15} {name[:50]}  ({n})")
        print(f"  → {res.extra_outputs[1]}")
        return res

    return run


@sub("compare", "Compare two issues of a set by sheet number: overlay (old red / new blue, clouds, delta tags) + delta report")
def _cli_compare(p: argparse.ArgumentParser):
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--dpi", type=float, default=72.0, help="overlay render resolution (default 72, capped for huge sheets)")
    p.add_argument("--new-password")
    add_common(p)

    def run(ns):
        res = compare_sets(ns.old, ns.new, out=ns.out, dpi=ns.dpi, password=ns.password, new_password=ns.new_password,
                           options=gate_options_from(ns))
        d = res.data
        for r in d["sheets"]:
            tb = "  · title block updated" if r["title_block_updated"] else ""
            print(f"  {r['sheet']:<8} {r['status']:<17} {r['changes']:>3} change(s){tb}")
        for s in d["removed"]:
            print(f"  {s:<8} removed")
        return res

    return run


@sub("link", "Batch link: 'SEE E-201', '3/E-501' detail callouts, sheet-index rows → live links")
def _cli_link(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--no-index", action="store_true", help="do not treat sheet-index rows specially")
    p.add_argument("--no-details", action="store_true", help="link detail callouts to the sheet, not the detail")
    add_common(p)

    def run(ns):
        res = link_set(ns.pdf, out=ns.out, index_entries=not ns.no_index, details=not ns.no_details,
                       password=ns.password, options=gate_options_from(ns))
        for sh, c in res.data["per_sheet"].items():
            kinds = ", ".join(f"{k} {v}" for k, v in c.items() if k != "links")
            print(f"  {sh:<8} {c['links']:>3} link(s){'  (' + kinds + ')' if kinds else ''}")
        return res

    return run


@sub("slipsheet", "Replace sheets by sheet number with revised ones, carry markups over, rebuild bookmarks")
def _cli_slip(p: argparse.ArgumentParser):
    p.add_argument("set")
    p.add_argument("revised")
    p.add_argument("--no-markups", action="store_true", help="do not carry markups from replaced sheets")
    p.add_argument("--no-note", action="store_true", help="do not add the slip-sheet note")
    p.add_argument("--revised-password")
    add_common(p)

    def run(ns):
        res = slipsheet(ns.set, ns.revised, out=ns.out, carry_markups=not ns.no_markups, note=not ns.no_note,
                        password=ns.password, revised_password=ns.revised_password, options=gate_options_from(ns))
        for x in res.data["replaced"]:
            print(f"  replaced {x['sheet']:<8} p.{x['page']:<4} rev {x['old_rev'] or '?'} → {x['new_rev'] or '?'}  "
                  f"({x['markups']} markup(s) carried)")
        for a in res.data["added"]:
            print(f"  added    {a}")
        return res

    return run
