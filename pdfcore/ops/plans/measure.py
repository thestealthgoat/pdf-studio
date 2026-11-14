"""Scale, measurement, takeoff, sheet sizes, print-to-scale, tiling and layers for drawing sets.

Built from ISO 32000-2 §12.9 (measurement properties: ``/VP`` viewport arrays, ``/Measure`` rectilinear
dictionaries with ``/R`` ``/X`` ``/D`` ``/A`` number-format arrays) and §8.11 (optional content), with
pikepdf / pdfplumber / ReportLab / openpyxl only.

Coordinates
-----------
Same convention as :mod:`pdfcore.ops.markup`: points, **top-left origin of the page as displayed**
(relative to the MediaBox, ``/Rotate`` applied) — what pdfplumber reports and what :func:`pdfcore.text.search`
returns. Pages are 0-based in Python and YAML/JSON specs; CLI flags and printed output are 1-based.

Scales
------
:func:`parse_scale` reads ``1/4"=1'-0"``, ``1/8" = 1'``, ``3/16"=1'-0"``, ``1"=20'``, ``1:100``, ``1:50 (mm)``,
``1 in = 4 ft``, ``FULL SIZE``; ``NTS`` / ``NONE`` / ``NOT TO SCALE`` / ``AS NOTED`` return ``None``.
A :class:`Scale` knows its *ratio* (real length / paper length, both in the same unit: 1/4"=1'-0" → 48),
its real unit (ft, m, mm…) and how lengths are displayed (``ft-in`` with 1/16" fractions, decimal ``ft``, ``m``…).

Viewports written by :func:`add_viewport` / :func:`set_scale`::

    /VP [ << /Type /Viewport /BBox [llx lly urx ury] /Name (1/4" = 1'-0")
           /Measure << /Type /Measure /Subtype /RL /R (1 in = 4 ft)
                       /X [<< /Type /NumberFormat /U (ft) /C 0.0555556 /F /D /D 100 >>]
                       /D [<< /U (ft) /C 1 /F /T >> << /U (in) /C 12 /F /F /D 16 >>]
                       /A [<< /U (sq ft) /C 1 /F /D /D 100 >>] >> >> ]

``/X[0]/C`` converts default user-space units (points × ``/UserUnit``) to the real unit, so Acrobat,
Bluebeam and PDF-XChange measure with the same scale. When several viewports contain a point the last one
in the array wins (§12.9.2), so detail viewports are appended after the whole-sheet one.

Main functions (one line each)
------------------------------
parse_scale / parse_length / format_length / format_area / scale_from_ratio — units and scale text
find_scale_labels / find_scale_bar / calibrate — scale from sheet text, a drawn scale bar, or two points
add_viewport / set_scale / read_viewports / read_scales — write and read ``/VP`` ``/Measure``
measure_points / measure / annotate_measurement — length, area (minus holes), perimeter, count (+ annotations)
takeoff — YAML spec or harvested measurement annotations → XLSX with SUMIFS/SUBTOTAL formulas
detect_size / sheet_sizes / resize / tile — sheet sizes, print to scale (keeps /Measure right), tiled prints
list_layers / set_layers — optional content on/off by name, flatten (bake visibility, drop /OCProperties)
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.ops.plans import sub
from pdfcore.result import OpResult
from pdfcore.types import Rect

__all__ = [
    "Scale", "Viewport", "Measurement", "IN_PER", "SHEET_SIZES",
    "parse_length", "parse_scale", "format_ft_in", "format_length", "format_area", "scale_from_ratio",
    "convert_length", "find_sheet_page", "find_scale_labels", "find_scale_bar", "calibrate",
    "make_measure", "add_viewport", "clear_viewports", "read_viewports", "read_scales", "set_scale",
    "measure_points", "annotate_measurement", "measure", "takeoff", "harvest_measurements",
    "detect_size", "parse_size", "sheet_sizes", "resize", "tile", "list_layers", "set_layers",
]

Pt = tuple[float, float]
Matrix = tuple[float, float, float, float, float, float]
PT_PER_IN = 72.0

# ====================================================================== units
IN_PER: dict[str, float] = {
    "in": 1.0, "ft": 12.0, "yd": 36.0, "mi": 63360.0,
    "mm": 1 / 25.4, "cm": 10 / 25.4, "m": 1000 / 25.4, "km": 1e6 / 25.4, "pt": 1 / 72,
}
METRIC = {"mm", "cm", "m", "km"}
_UNIT_WORDS: dict[str, str] = {
    '"': "in", "in": "in", "inch": "in", "inches": "in",
    "'": "ft", "ft": "ft", "feet": "ft", "foot": "ft", "lf": "ft",
    "yd": "yd", "yds": "yd", "yard": "yd", "yards": "yd", "mi": "mi", "mile": "mi", "miles": "mi",
    "mm": "mm", "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "km": "km", "kilometer": "km", "kilometers": "km", "kilometre": "km", "kilometres": "km",
    "pt": "pt", "points": "pt",
}
_NUM = r"(?:\d+(?:\.\d*)?|\.\d+)"


def _norm(s: str) -> str:
    """Normalize typographic quotes, primes and dashes; ``''`` (two apostrophes) means inches."""
    t = str(s)
    for a, b in (("″", '"'), ("“", '"'), ("”", '"'), ("′", "'"), ("‘", "'"), ("’", "'"), ("–", "-"), ("—", "-"),
                 ("−", "-"), (" ", " ")):
        t = t.replace(a, b)
    return t.replace("''", '"')


def unit_word(w: str) -> str | None:
    """'feet' → 'ft', 'Meters' → 'm', '"' → 'in'; None if unknown."""
    return _UNIT_WORDS.get(str(w).strip().lower().rstrip("."))


def parse_length(s: str, default_unit: str | None = None) -> tuple[float, str]:
    """Parse a length → (inches, primary unit).

    ``16ft`` ``16'`` ``16 FT`` ``12'-6 1/2"`` ``1'-0"`` ``1/4"`` ``1-1/2"`` ``3"`` ``0.25 in`` ``4.88m`` ``1200 mm``.
    A bare number needs *default_unit*.
    """
    t = _norm(s).lower().replace(",", "").strip()
    if not t:
        raise ValueError("empty length")
    m = re.fullmatch(rf"({_NUM})\s*([a-z]+)\.?", t)
    if m:
        u = unit_word(m.group(2))
        if u is None:
            raise ValueError(f"unknown unit {m.group(2)!r} in {s!r}")
        return float(m.group(1)) * IN_PER[u], u
    if re.fullmatch(_NUM, t):
        if not default_unit:
            raise ValueError(f"no unit in {s!r} (write e.g. 16ft, 16', 4.88m)")
        u = unit_word(default_unit) or default_unit
        return float(t) * IN_PER[u], u
    feet, has_ft = 0.0, False
    m = re.match(rf"({_NUM})\s*(?:'|ft\b\.?|feet\b|foot\b)", t)
    if m:
        feet, has_ft = float(m.group(1)), True
        t = t[m.end():].strip().lstrip("-").strip()
    inches, has_in = 0.0, False
    if t:
        m = re.fullmatch(rf"(?:({_NUM})(?:\s+|\s*-\s*)?)?(?:(\d+)\s*/\s*(\d+))?\s*(\"|in\b\.?|inch(?:es)?)?", t)
        if not m or not (m.group(1) or m.group(2)):
            raise ValueError(f"cannot read length {s!r}")
        if not m.group(4) and not has_ft:
            raise ValueError(f"no unit in {s!r}")
        if m.group(2) and int(m.group(3)) == 0:
            raise ValueError(f"bad fraction in {s!r}")
        inches = float(m.group(1) or 0) + (int(m.group(2)) / int(m.group(3)) if m.group(2) else 0.0)
        has_in = True
    if not (has_ft or has_in):
        raise ValueError(f"cannot read length {s!r}")
    return feet * 12 + inches, ("ft" if has_ft else "in")


def convert_length(v: float, frm: str, to: str) -> float:
    return v * IN_PER[unit_word(frm) or frm] / IN_PER[unit_word(to) or to]


def area_unit(u: str) -> str | None:
    """'sq ft' / 'sf' / 'ft2' / 'ft²' / 'm2' → base length unit ('ft', 'm'); None if not an area unit."""
    s = str(u).strip().lower().replace("²", "2")
    if s in ("sf", "sqft"):
        return "ft"
    if s in ("sy", "sqyd"):
        return "yd"
    if s in ("sm", "sqm"):
        return "m"
    m = re.fullmatch(r"(?:sq\.?\s*|square\s+)([a-z]+)|([a-z]+)\s*2", s)
    if m:
        return unit_word(m.group(1) or m.group(2))
    return None


def _frac_text(x: float, denom: int = 64) -> str:
    """0.25 → '1/4', 1.5 → '1 1/2', 3.0 → '3'."""
    f = Fraction(x).limit_denominator(denom)
    whole, rem = divmod(f.numerator, f.denominator)
    if rem == 0:
        return str(whole)
    fr = f"{rem}/{f.denominator}"
    return f"{whole} {fr}" if whole else fr


def format_ft_in(ft: float, denom: int = 16) -> str:
    """12.541667 → 12'-6 1/2" (fractions to 1/denom inch)."""
    sign = "-" if ft < 0 else ""
    n = round(abs(ft) * 12 * denom)
    whole_in, num = divmod(n, denom)
    feet, inch = divmod(whole_in, 12)
    frac = ""
    if num:
        f = Fraction(num, denom)
        frac = f" {f.numerator}/{f.denominator}"
    return f"{sign}{feet}'-{inch}{frac}\""


def _num(v: float, places: int = 2) -> str:
    return f"{v:,.{places}f}"


def format_length(value: float, unit: str, display: str | None = None, *, denom: int = 16) -> str:
    """Format a length given in *unit*: display 'ft-in' → 12'-6 1/2"; else decimal in *display* unit."""
    display = display or unit
    if display == "ft-in":
        return format_ft_in(convert_length(value, unit, "ft"), denom)
    v = convert_length(value, unit, display)
    return f"{_num(v, 0 if display == 'mm' else 2)} {display}"


def format_area(value: float, unit: str) -> str:
    """*value* in square *unit* (length unit) → '4,928.00 sq ft'."""
    return f"{_num(value, 0 if unit == 'mm' else 2)} sq {unit}"


# ====================================================================== scales
# Standard scales: architectural (paper inches per foot), engineering (feet per inch), metric 1:N.
ARCH_IN_PER_FT = [1 / 32, 1 / 16, 3 / 32, 1 / 8, 3 / 16, 1 / 4, 3 / 8, 1 / 2, 3 / 4, 1, 1.5, 3, 6, 12]
ENG_FT_PER_IN = [10, 20, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250, 300, 400, 500, 600, 1000, 2000]
METRIC_N = [1, 2, 5, 10, 20, 25, 50, 75, 100, 125, 200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000]


def _g(v: float) -> str:
    s = f"{v:.6g}"
    return s


@dataclass(frozen=True)
class Scale:
    """A drawing scale. *ratio* = real length / paper length (same units); 1/4"=1'-0" → 48."""

    ratio: float
    real_unit: str = "ft"  # unit of /X (ft, in, m, mm…)
    display: str = "ft-in"  # 'ft-in' or a unit name for decimal display
    label: str = ""
    family: str = "custom"  # arch | eng | metric | custom
    paper_unit: str = "in"  # unit used on the paper side of /R

    def factor(self, user_unit: float = 1.0) -> float:
        """Real units per default user-space unit (1 pt × UserUnit)."""
        return self.ratio * user_unit / PT_PER_IN / IN_PER[self.real_unit]

    def r_string(self) -> str:
        """The /R text, e.g. '1 in = 4 ft', '1 mm = 0.1 m'."""
        v = self.ratio * IN_PER[self.paper_unit] / IN_PER[self.real_unit]
        return f"1 {self.paper_unit} = {_g(v)} {self.real_unit}"

    def scaled(self, k: float) -> "Scale":
        """The scale after printing at *k* × size (k=0.5: 1/4"=1'-0" → 1/8"=1'-0")."""
        hint = "m" if self.real_unit in METRIC else "ft"
        s = scale_from_ratio(self.ratio / k, hint, tol=0.001)
        return Scale(s.ratio, self.real_unit, self.display, s.label, s.family, self.paper_unit)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["r"] = self.r_string()
        return d


def _arch_label(in_per_ft: float) -> str:
    return f'{_frac_text(in_per_ft)}" = 1\'-0"'


def scale_from_ratio(ratio: float, unit_hint: str = "ft", *, tol: float = 0.005, snap: bool = True) -> Scale:
    """Scale for a measured *ratio*, snapped to the nearest standard scale within *tol* (relative)."""
    if ratio <= 0 or not math.isfinite(ratio):
        raise ValueError(f"bad scale ratio {ratio}")
    u = unit_word(unit_hint) or unit_hint
    if u in METRIC:
        if snap:
            best = min(METRIC_N, key=lambda n: abs(n - ratio) / n)
            if abs(best - ratio) / best <= tol:
                return Scale(float(best), u, u, f"1:{best}", "metric", "mm")
        return Scale(ratio, u, u, f"1:{_g(round(ratio, 2))}", "custom", "mm")
    if snap:
        cands: list[tuple[float, Scale]] = []
        for x in ARCH_IN_PER_FT:
            r = 12 / x
            cands.append((abs(r - ratio) / r, Scale(r, "ft", "ft-in", _arch_label(x), "arch")))
        for n in ENG_FT_PER_IN:
            r = 12.0 * n
            cands.append((abs(r - ratio) / r, Scale(r, "ft", "ft", f"1\" = {n}'", "eng")))
        err, best = min(cands, key=lambda c: c[0])
        if err <= tol:
            return best
    ft_per_in = ratio / 12
    return Scale(ratio, "ft", "ft-in", f"1\" = {_g(round(ft_per_in, 4))}'", "custom")


_NO_SCALE = re.compile(r"(?:n\.?\s*t\.?\s*s\.?|none|not\s+to\s+scale|no\s+scale|as\s+(?:noted|shown|indicated))", re.I)


def parse_scale(text: str) -> Scale | None:
    """Parse scale text; ``None`` for NTS/NONE. Raises ValueError if it is not a scale."""
    t = _norm(text).strip()
    t = re.sub(r"^\s*scale\s*[:=\-]?\s*", "", t, flags=re.I).strip().rstrip(".;,").strip()
    if not t:
        raise ValueError("empty scale")
    low = t.lower()
    if _NO_SCALE.fullmatch(low):
        return None
    if low in ("full", "full size", "full scale"):
        return Scale(1.0, "in", "in", "FULL SIZE", "arch")
    if low in ("half size", "half scale"):
        return Scale(2.0, "in", "in", "HALF SIZE", "custom")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*:\s*(\d[\d,]*(?:\.\d+)?)\s*(?:\(?\s*([a-z]+)\s*\)?)?", low)
    if m:
        a, b = float(m.group(1)), float(m.group(2).replace(",", ""))
        if a <= 0 or b <= 0:
            raise ValueError(f"bad scale {text!r}")
        u = unit_word(m.group(3)) if m.group(3) else "m"
        if u is None:
            raise ValueError(f"unknown unit in {text!r}")
        ratio = b / a
        lab = f"1:{_g(ratio)}" + (f" ({u})" if m.group(3) else "")
        if u in METRIC:
            fam = "metric" if any(abs(n - ratio) < 1e-9 for n in METRIC_N) else "custom"
            return Scale(ratio, u, u, lab, fam, "mm")
        return Scale(ratio, u, "ft-in" if u == "ft" else u, lab, "custom")
    if "=" not in t:
        raise ValueError(f"not a scale: {text!r}")
    left, right = (p.strip() for p in t.split("=", 1))
    paper_in, pu = parse_length(left)
    real_in, ru = parse_length(right, default_unit="ft" if pu == "in" else pu)
    if paper_in <= 0 or real_in <= 0:
        raise ValueError(f"bad scale {text!r}")
    ratio = real_in / paper_in
    label = re.sub(r"\s*=\s*", " = ", t)
    if ru in METRIC:
        return Scale(ratio, ru, ru, label, "metric", pu if pu in METRIC else "in")
    if ru == "ft" and pu == "in":
        if abs(real_in - 12) < 1e-9:
            return Scale(ratio, "ft", "ft-in", label, "arch")
        ft_per_in = real_in / 12
        if abs(paper_in - 1) < 1e-9 and abs(ft_per_in - round(ft_per_in)) < 1e-9 and round(ft_per_in) % 10 == 0:
            return Scale(ratio, "ft", "ft", label, "eng")
        return Scale(ratio, "ft", "ft-in", label, "custom")
    return Scale(ratio, ru, "ft-in" if ru == "ft" else ru, label, "custom", pu if pu in METRIC else "in")


def _as_scale(s: Scale | str | None) -> Scale | None:
    if s is None or isinstance(s, Scale):
        return s
    return parse_scale(s)


# ====================================================================== matrices
def _mul(A: Sequence[float], B: Sequence[float]) -> Matrix:
    """A then B (PDF row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = A
    a2, b2, c2, d2, e2, f2 = B
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _inv(M: Sequence[float]) -> Matrix:
    a, b, c, d, e, f = M
    det = a * d - b * c
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_))


def _apply(M: Sequence[float], x: float, y: float) -> Pt:
    a, b, c, d, e, f = M
    return (a * x + c * y + e, b * x + d * y + f)


def _fm(M: Sequence[float]) -> str:
    return " ".join(f"{v:.6f}".rstrip("0").rstrip(".") if abs(v) > 1e-12 else "0" for v in M)


# ====================================================================== page helpers
def _open(src: str | Path, password: str | None) -> pikepdf.Pdf:
    return pikepdf.open(str(src), password=password or "")


def _geom(page: pikepdf.Page):
    from pdfcore.ops.markup import PageGeom

    return PageGeom(page)


def _user_unit(page: pikepdf.Page) -> float:
    try:
        return float(page.obj.get("/UserUnit", 1.0))
    except Exception:
        return 1.0


def _pts(v: Any) -> list[Pt]:
    """'x,y;x,y' / 'x0,y0,x1,y1' / [[x,y],…] / [x,y,x,y] → [(x,y),…]."""
    if isinstance(v, str):
        nums = [float(t) for t in re.split(r"[,;\s]+", v.strip()) if t]
    else:
        seq = list(v)
        if seq and isinstance(seq[0], (int, float)):
            nums = [float(t) for t in seq]
        else:
            return [(float(p[0]), float(p[1])) for p in seq]
    if len(nums) % 2:
        raise ValueError(f"odd number of coordinates in {v!r}")
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]


def _rect_of(v: Any) -> Rect:
    if isinstance(v, Rect):
        return v
    nums = [float(t) for t in re.split(r"[,;\s]+", v.strip()) if t] if isinstance(v, str) else [float(t) for t in v]
    if len(nums) != 4:
        raise ValueError(f"a rectangle needs x0,top,x1,bottom: {v!r}")
    x0, t, x1, b = nums
    return Rect(min(x0, x1), min(t, b), max(x0, x1), max(t, b))


def _rect_poly(r: Rect) -> list[Pt]:
    return [(r.x0, r.top), (r.x1, r.top), (r.x1, r.bottom), (r.x0, r.bottom)]


def _norm_sheet(s: str) -> str:
    return re.sub(r"[\s\-‐–—_.]", "", _norm(s)).upper()


def find_sheet_page(src: str | Path, sheet: str, *, password: str | None = None) -> int:
    """0-based page whose title block carries sheet number *sheet* (largest matching text wins)."""
    from pdfcore import text as T

    want = _norm_sheet(sheet)
    n = _page_count(src, password)
    best: tuple[float, int] | None = None
    for pi in range(n):
        try:
            ws = T.words(src, pi, password=password)
        except Exception:
            continue
        for w in ws:
            if _norm_sheet(w.text.strip(".,;:()")) == want:
                score = (w.size or w.rect.height) + (5.0 if w.rect.x0 > 0.6 * max(1.0, max(x.rect.x1 for x in ws)) else 0)
                if best is None or score > best[0]:
                    best = (score, pi)
    if best is None:
        raise ValueError(f"sheet {sheet!r} not found in {Path(src).name}")
    return best[1]


def _page_count(src: str | Path, password: str | None) -> int:
    with _open(src, password) as pdf:
        return len(pdf.pages)


def _resolve_pages(src: str | Path, *, page: int | Sequence[int] | None = None, sheet: str | None = None,
                   password: str | None = None) -> list[int]:
    if sheet:
        return [find_sheet_page(src, sheet, password=password)]
    if page is None:
        return list(range(_page_count(src, password)))
    return [int(page)] if isinstance(page, int) else [int(p) for p in page]


# ====================================================================== /Measure + /VP
def _real(v: float) -> Decimal:
    """A PDF real with 12 significant digits (pikepdf writes Python floats with only 6 decimals)."""
    return Decimal(f"{float(v):.12g}")


def _nf(u: str, c: float, f: str = "D", d: int = 100) -> Dictionary:
    return Dictionary(Type=Name.NumberFormat, U=String(u), C=_real(c), F=Name("/" + f), D=int(d))


def make_measure(pdf: pikepdf.Pdf, scale: Scale, user_unit: float = 1.0) -> pikepdf.Object:
    """Rectilinear /Measure dictionary (indirect) for *scale*."""
    base = scale.real_unit
    xs = [_nf(base, scale.factor(user_unit))]
    if scale.display == "ft-in" and base == "ft":
        ds = [_nf("ft", 1, "T", 1), _nf("in", 12, "F", 16)]
    elif scale.display == "ft-in":
        ds = [_nf("ft", IN_PER[base] / 12, "T", 1), _nf("in", 12, "F", 16)]
    else:
        disp = scale.display if scale.display in IN_PER else base
        ds = [_nf(disp, IN_PER[base] / IN_PER[disp], "D", 1 if disp == "mm" else 100)]
    a_unit = "ft" if scale.display == "ft-in" else (scale.display if scale.display in IN_PER else base)
    as_ = [_nf(f"sq {a_unit}", (IN_PER[base] / IN_PER[a_unit]) ** 2, "D", 100)]
    md = Dictionary(Type=Name.Measure, Subtype=Name.RL, R=String(scale.r_string()), X=Array(xs), D=Array(ds),
                    A=Array(as_))
    return pdf.make_indirect(md)


@dataclass
class Viewport:
    """One /VP entry, decoded."""

    page: int
    index: int
    name: str
    bbox_user: tuple[float, float, float, float]
    bbox: Rect  # top-left visual
    unit: str  # /X unit, normalized (ft, m…) or raw /U text
    factor: float  # real units per user-space unit (/X[0]/C)
    ratio: float | None
    display: str
    r: str
    scale: Scale | None
    subtype: str = "RL"

    def contains_user(self, x: float, y: float, tol: float = 0.5) -> bool:
        x0, y0, x1, y1 = self.bbox_user
        return min(x0, x1) - tol <= x <= max(x0, x1) + tol and min(y0, y1) - tol <= y <= max(y0, y1) + tol

    def to_dict(self) -> dict:
        return {
            "page": self.page, "index": self.index, "name": self.name,
            "bbox": [round(v, 2) for v in (self.bbox.x0, self.bbox.top, self.bbox.x1, self.bbox.bottom)],
            "unit": self.unit, "factor": self.factor, "ratio": self.ratio, "display": self.display, "r": self.r,
            "label": self.scale.label if self.scale else None, "subtype": self.subtype,
        }


def _decode_measure(md: Any, uu: float = 1.0) -> tuple[str, float, float | None, str, str, Scale | None, str]:
    """→ (unit, factor, ratio, display, R, Scale, subtype) from a /Measure dict."""
    subtype = str(md.get("/Subtype", "/RL")).lstrip("/")
    r = str(md.get("/R", ""))
    if subtype != "RL":
        return ("", 0.0, None, "", r, None, subtype)
    X = md.get("/X")
    if X is None or len(X) == 0:
        raise ValueError("measure dictionary without /X")
    x0 = X[0]
    raw_u = str(x0.get("/U", ""))
    factor = float(x0.get("/C", 1.0))
    u = unit_word(raw_u) or raw_u
    ratio = factor * IN_PER[u] * PT_PER_IN / uu if u in IN_PER else None
    D = md.get("/D")
    display = u
    if D is not None and len(D) >= 2:
        units = [unit_word(str(d.get("/U", ""))) for d in D]
        if units[:2] == ["ft", "in"]:
            display = "ft-in"
    elif D is not None and len(D) == 1:
        du = unit_word(str(D[0].get("/U", "")))
        if du:
            display = du
    sc = None
    if ratio:
        hint = "m" if u in METRIC else "ft"
        base = scale_from_ratio(ratio, hint, tol=0.001)
        sc = Scale(ratio, u, display, base.label, base.family, "mm" if u in METRIC else "in")
    return (u, factor, ratio, display, r, sc, subtype)


def read_viewports(pdf: pikepdf.Pdf, page_index: int) -> list[Viewport]:
    """Decode the /VP array of a page (open pikepdf document)."""
    page = pdf.pages[page_index]
    vps = page.obj.get("/VP")
    if vps is None:
        return []
    if isinstance(vps, Dictionary):
        vps = [vps]
    g = _geom(page)
    uu = _user_unit(page)
    out: list[Viewport] = []
    for k, vp in enumerate(vps):
        try:
            bb = [float(v) for v in vp.BBox]
            md = vp.get("/Measure")
            if md is None:
                continue
            u, factor, ratio, display, r, sc, st = _decode_measure(md, uu)
            name = str(vp.get("/Name", "")) or (sc.label if sc else "")
            if sc is not None:
                try:
                    parsed = parse_scale(name) if name else None
                    if parsed is not None and abs(parsed.ratio - sc.ratio) / sc.ratio < 1e-3:
                        sc = Scale(sc.ratio, sc.real_unit, sc.display, parsed.label, parsed.family, sc.paper_unit)
                except ValueError:
                    pass
            out.append(Viewport(page_index, k, name, (bb[0], bb[1], bb[2], bb[3]), g.rect_from_user(*bb), u, factor,
                                ratio, display, r, sc, st))
        except Exception:
            continue
    return out


def read_scales(src: str | Path, *, password: str | None = None) -> list[dict]:
    """Every /VP /Measure viewport in any PDF, as dicts (page 0-based)."""
    with _open(src, password) as pdf:
        out: list[dict] = []
        for pi in range(len(pdf.pages)):
            out.extend(v.to_dict() for v in read_viewports(pdf, pi))
        return out


def clear_viewports(pdf: pikepdf.Pdf, page_index: int) -> int:
    page = pdf.pages[page_index]
    vps = page.obj.get("/VP")
    if vps is None:
        return 0
    n = len(vps) if isinstance(vps, Array) else 1
    del page.obj["/VP"]
    return n


def add_viewport(pdf: pikepdf.Pdf, page_index: int, scale: Scale | str, bbox: Rect | Sequence[float] | None = None,
                 *, name: str | None = None) -> Viewport:
    """Append a /Viewport with a /Measure for *scale* (bbox: top-left visual; None = whole page).

    A viewport with the same BBox is replaced in place.
    """
    sc = _as_scale(scale)
    if sc is None:
        raise ValueError("NTS / no scale: nothing to write")
    page = pdf.pages[page_index]
    g = _geom(page)
    if bbox is None:
        ub = (g.x0, g.y0, g.x0 + g.W, g.y0 + g.H)
    else:
        r = _rect_of(bbox)
        ub = g.user_bbox([g.d(r.x0, r.top), g.d(r.x1, r.bottom)])
    md = make_measure(pdf, sc, _user_unit(page))
    vp = Dictionary(Type=Name.Viewport, BBox=Array([round(float(v), 4) for v in ub]), Name=String(name or sc.label),
                    Measure=md)
    vps = page.obj.get("/VP")
    if vps is None or not isinstance(vps, Array):
        page.obj.VP = Array([])
        vps = page.obj.VP
    idx = None
    for k, old in enumerate(vps):
        try:
            if all(abs(float(a) - b) < 0.5 for a, b in zip(old.BBox, ub)):
                idx = k
        except Exception:
            continue
    if idx is None:
        vps.append(vp)
        idx = len(vps) - 1
    else:
        vps[idx] = vp
    return read_viewports(pdf, page_index)[idx]


# ====================================================================== reading scales from the sheet
def _page_words(src: str | Path, page: int, password: str | None):
    from pdfcore import text as T

    return T.words(src, page, password=password)


def find_scale_labels(src: str | Path, page: int, *, password: str | None = None) -> list[dict]:
    """Scale notes on a sheet ("SCALE: 1/4" = 1'-0"", "1:100", "SCALE: NONE") → [{text, scale, rect}].

    *scale* is a :class:`Scale` or None (NTS). *rect* is the label's box (top-left visual).
    """
    from pdfcore import text as T

    ws = _page_words(src, page, password)
    out: list[dict] = []
    for ln in T.group_lines(ws):
        words = ln.words
        for i, w in enumerate(words):
            if not re.match(r"^scale\b", _norm(w.text), re.I):
                continue
            rest = re.sub(r"^scale\s*[:=\-]?", "", _norm(w.text), flags=re.I)
            cand_words = ([w] if rest else []) + words[i + 1: i + 9]
            best = None
            for k in range(1, len(cand_words) + 1):
                seg = cand_words[:k]
                if k > 1 and seg[-1].rect.x0 - seg[-2].rect.x1 > 3 * max(seg[-1].size, 6):
                    break
                txt = " ".join(x.text for x in seg)
                if seg[0] is w:
                    txt = rest + txt[len(w.text):]
                try:
                    sc = parse_scale(txt)
                except ValueError:
                    continue
                best = (txt.strip(), sc, Rect.union([w.rect] + [x.rect for x in seg]))
            if best:
                out.append({"text": best[0], "scale": best[1], "rect": best[2]})
    return out


def _segments(src: str | Path, page: int, password: str | None) -> tuple[list[tuple[float, float, float, float]], list[Rect]]:
    """(line segments [x0, y0, x1, y1] top-left, rectangles) from the page's vector paths."""
    from pdfcore import engines

    segs: list[tuple[float, float, float, float]] = []
    rects: list[Rect] = []
    for p in engines.get("text").paths(Path(src), page, password=password):
        if p.kind == "line" and len(p.points) >= 2:
            for a, b in zip(p.points, p.points[1:]):
                segs.append((a[0], a[1], b[0], b[1]))
        elif p.kind == "rect":
            rects.append(p.rect)
    return segs, rects


def find_scale_bar(src: str | Path, page: int, *, password: str | None = None) -> dict | None:
    """Detect a graphic scale bar: a horizontal line with end ticks (or a chain of equal boxes) and a
    length label near it ("16 FT", "5 m", tick numbers "0 4 8 16 FEET").

    Returns {scale, ratio, paper_pt, real_in, real_unit, label, rect} or None.
    """
    from pdfcore import text as T

    segs, rects = _segments(src, page, password)
    horiz = [s for s in segs if abs(s[1] - s[3]) < 0.6 and abs(s[2] - s[0]) >= 36]
    vert = [s for s in segs if abs(s[0] - s[2]) < 0.6 and 2 <= abs(s[3] - s[1]) <= 36]
    bars: list[tuple[float, float, float, int]] = []  # x0, x1, y, ticks
    for x0, y0, x1, _y1 in horiz:
        xa, xb = sorted((x0, x1))
        y = y0
        ticks = [v for v in vert if xa - 1.5 <= v[0] <= xb + 1.5 and min(v[1], v[3]) - 1 <= y <= max(v[1], v[3]) + 1]
        tx = [v[0] for v in ticks]
        if any(abs(t - xa) < 1.5 for t in tx) and any(abs(t - xb) < 1.5 for t in tx) and len(ticks) >= 2:
            bars.append((xa, xb, y, len(ticks)))
    # alternating-box bars
    small = sorted((r for r in rects if 1 <= r.height <= 30 and r.width >= 4), key=lambda r: (round(r.top), r.x0))
    i = 0
    while i < len(small):
        chain = [small[i]]
        j = i + 1
        while j < len(small) and abs(small[j].top - chain[-1].top) < 0.6 and abs(small[j].bottom - chain[-1].bottom) < 0.6 \
                and abs(small[j].x0 - chain[-1].x1) < 1.0:
            chain.append(small[j])
            j += 1
        if len(chain) >= 2 and chain[-1].x1 - chain[0].x0 >= 36:
            bars.append((chain[0].x0, chain[-1].x1, (chain[0].top + chain[0].bottom) / 2, len(chain) + 1))
        i = j if j > i + 1 else i + 1
    if not bars:
        return None
    ws = _page_words(src, page, password)
    lines = T.group_lines(ws)
    results: list[tuple[float, dict]] = []
    for xa, xb, y, nt in bars:
        band = Rect(xa - 72, y - 54, xb + 216, y + 54)
        cands: list[tuple[float, float, str, Rect]] = []  # distance, real_in, text, rect
        for ln in lines:
            if not band.intersects(ln.rect):
                continue
            lt = _norm(ln.text)
            if "=" in lt or re.search(r"\bscale\b", lt, re.I) or re.search(r"\d\s*:\s*\d", lt):
                continue
            toks = [w for w in ln.words if band.intersects(w.rect)]
            unit = None
            for w in toks:
                m = re.fullmatch(rf"{_NUM}?\s*([a-z'\"]+)\.?", _norm(w.text).lower())
                if m and unit_word(m.group(1)):
                    unit = unit_word(m.group(1))
            for k, w in enumerate(toks):
                txt = _norm(w.text)
                val = None
                shown = txt
                try:
                    val = parse_length(txt)[0]
                except ValueError:
                    if re.fullmatch(_NUM, txt) and k + 1 < len(toks):
                        try:
                            shown = txt + " " + _norm(toks[k + 1].text)
                            val = parse_length(shown)[0]
                        except ValueError:
                            val, shown = None, txt
                    if val is None and re.fullmatch(_NUM, txt) and unit:
                        val, shown = float(txt) * IN_PER[unit], f"{txt} {unit}"
                if val and val > 0:
                    cx = (w.rect.x0 + w.rect.x1) / 2
                    cy = (w.rect.top + w.rect.bottom) / 2
                    d = math.hypot(max(0.0, cx - xb), cy - y) if cx > xb else math.hypot(cx - xb, cy - y)
                    cands.append((d, val, shown, w.rect))
        if not cands:
            continue
        # prefer the largest value among labels near the right end of the bar
        near = [c for c in cands if c[0] <= min(c2[0] for c2 in cands) + 36]
        d, real_in, label, lrect = max(near, key=lambda c: c[1])
        paper_pt = xb - xa
        results.append((d - nt, {"paper_pt": paper_pt, "real_in": real_in, "label": label,
                                 "rect": Rect(xa, y - 4, xb, y + 4).expand(0), "label_rect": lrect}))
    if not results:
        return None
    best = min(results, key=lambda r: r[0])[1]
    with _open(src, password) as pdf:
        uu = _user_unit(pdf.pages[page])
    ratio = best["real_in"] / (best["paper_pt"] * uu / PT_PER_IN)
    try:
        unit = parse_length(_norm(best["label"]))[1]
    except ValueError:
        unit = "ft"
    sc = scale_from_ratio(ratio, unit)
    best.update(scale=sc, ratio=ratio, real_unit=unit)
    return best


def _real_distance(d: str | float, unit_hint: str = "ft") -> tuple[float, str]:
    if isinstance(d, (int, float)):
        return float(d) * IN_PER[unit_hint], unit_hint
    return parse_length(str(d), default_unit=unit_hint)


def calibrate(p0: Sequence[float], p1: Sequence[float], distance: str | float, *, user_unit: float = 1.0,
              snap: bool = True, tol: float = 0.005) -> tuple[Scale, float]:
    """Scale from two points (top-left, points) a known real *distance* apart ("16ft", "4.88m", "12'-6\"").

    Returns (scale, measured_ratio); the scale is snapped to a standard one within *tol* when *snap*.
    """
    paper_pt = math.hypot(float(p1[0]) - float(p0[0]), float(p1[1]) - float(p0[1]))
    if paper_pt < 1:
        raise ValueError("calibration points are the same")
    real_in, unit = _real_distance(distance)
    ratio = real_in / (paper_pt * user_unit / PT_PER_IN)
    return scale_from_ratio(ratio, unit, tol=tol, snap=snap), ratio


def _parse_calibration(spec: str) -> tuple[Pt, Pt, str]:
    if "=" not in spec:
        raise ValueError("--calibrate wants x0,y0,x1,y1=DISTANCE (e.g. 288,1512,576,1512=16ft) or 'bar'")
    pts, dist = spec.split("=", 1)
    p = _pts(pts)
    if len(p) != 2:
        raise ValueError("--calibrate needs exactly two points")
    return p[0], p[1], dist.strip()


def _view_box_for_label(label: Rect, rects: list[Rect], page_w: float, page_h: float) -> Rect | None:
    """The drawing frame a scale label belongs to: a box just above it or containing it."""
    best: tuple[float, Rect] | None = None
    for r in rects:
        if r.width < 72 or r.height < 72 or r.width * r.height > 0.6 * page_w * page_h:
            continue
        overlap = min(r.x1, label.x1) - max(r.x0, label.x0)
        if overlap <= 0:
            continue
        if r.contains_point((label.x0 + label.x1) / 2, (label.top + label.bottom) / 2):
            gap = 0.0
        elif -6 <= label.top - r.bottom <= 108:
            gap = label.top - r.bottom
        else:
            continue
        if best is None or gap < best[0] or (gap == best[0] and r.area < best[1].area):
            best = (gap, r)
    if best is None:
        return None
    return Rect.union([best[1], label]).expand(4)


# ====================================================================== set_scale
def set_scale(src: str | Path, *, page: int | Sequence[int] | None = None, sheet: str | None = None,
              scale: Scale | str | None = None, calibrate_with: str | tuple | None = None, scale_bar: bool = False,
              bbox: Rect | Sequence[float] | str | None = None, name: str | None = None, replace: bool = False,
              snap: bool = True, out: str | Path | None = None, password: str | None = None, gate=None,
              quiet: bool = False) -> OpResult:
    """Write /VP /Measure viewports.

    Source of the scale, first that applies: *scale* text → *calibrate_with* (``"x0,y0,x1,y1=16ft"`` or
    ``((x0, y0), (x1, y1), "16ft")``) → *scale_bar* → automatic (scale notes on the sheet — one viewport per
    detail when several — else a detected scale bar). With no *page*/*sheet* every page is processed.
    """
    src = Path(src)
    pages = _resolve_pages(src, page=page, sheet=sheet, password=password)
    explicit = scale is not None or calibrate_with is not None or scale_bar
    plan: list[tuple[int, Scale, Rect | None, str, str, dict]] = []  # page, scale, bbox, name, source, extra
    notes: list[str] = []
    with _open(src, password) as pdf0:
        uus = {pi: _user_unit(pdf0.pages[pi]) for pi in pages}
        sizes = {pi: (_geom(pdf0.pages[pi]).vw, _geom(pdf0.pages[pi]).vh) for pi in pages}
    box = _rect_of(bbox) if bbox is not None else None
    for pi in pages:
        if scale is not None:
            sc = _as_scale(scale)
            if sc is None:
                raise ValueError(f"{scale!r} is not to scale (NTS) — no viewport written")
            plan.append((pi, sc, box, name or sc.label, "given", {}))
        elif calibrate_with is not None:
            if isinstance(calibrate_with, str) and calibrate_with.strip().lower() in ("bar", "scale-bar", "scalebar"):
                bar = find_scale_bar(src, pi, password=password)
                if bar is None:
                    raise ValueError(f"p.{pi + 1}: no scale bar found")
                plan.append((pi, bar["scale"], box, name or bar["scale"].label, "scale bar",
                             {"measured_ratio": bar["ratio"], "bar_label": bar["label"]}))
                continue
            p0, p1, dist = _parse_calibration(calibrate_with) if isinstance(calibrate_with, str) else calibrate_with
            sc, measured = calibrate(p0, p1, dist, user_unit=uus[pi], snap=snap)
            plan.append((pi, sc, box, name or sc.label, "calibration", {"measured_ratio": measured}))
        elif scale_bar:
            bar = find_scale_bar(src, pi, password=password)
            if bar is None:
                raise ValueError(f"p.{pi + 1}: no scale bar found")
            plan.append((pi, bar["scale"], box, name or bar["scale"].label, "scale bar",
                         {"measured_ratio": bar["ratio"], "bar_label": bar["label"]}))
        else:
            labels = [lb for lb in find_scale_labels(src, pi, password=password) if lb["scale"] is not None]
            bar = find_scale_bar(src, pi, password=password)
            if len(labels) > 1 and box is None:
                _segs, rects = _segments(src, pi, password)
                W, H = sizes[pi]
                framed = [(lb, _view_box_for_label(lb["rect"], rects, W, H)) for lb in labels]
                # unframed labels (title block) first → whole page; framed details after so they win
                for lb, fr in sorted(framed, key=lambda t: t[1] is not None):
                    plan.append((pi, lb["scale"], fr, lb["scale"].label, f"sheet note '{lb['text']}'", {}))
            elif labels:
                lb = labels[0]
                sc = lb["scale"]
                src_note = f"sheet note '{lb['text']}'"
                extra: dict = {}
                if bar is not None:
                    extra = {"bar_ratio": bar["ratio"]}
                    if abs(bar["scale"].ratio - sc.ratio) / sc.ratio > 0.01:
                        notes.append(f"p.{pi + 1}: scale note says {sc.label} but the scale bar measures "
                                     f"{bar['scale'].label} — using the scale bar (the sheet may be printed at another size)")
                        sc, src_note = bar["scale"], "scale bar"
                plan.append((pi, sc, box, name or sc.label, src_note, extra))
            elif bar is not None:
                plan.append((pi, bar["scale"], box, name or bar["scale"].label, "scale bar",
                             {"measured_ratio": bar["ratio"]}))
            elif explicit or len(pages) == 1:
                if find_scale_labels(src, pi, password=password):
                    raise ValueError(f"p.{pi + 1}: the sheet is marked not to scale (SCALE: NONE / NTS) — "
                                     "pass --scale or --calibrate to override")
                raise ValueError(f"p.{pi + 1}: no scale found on the sheet — pass --scale or --calibrate")
    if not plan:
        raise ValueError("no scale found on any selected sheet — pass --scale or --calibrate")
    o = output_path(src, "scaled", out=out)
    written: list[dict] = []
    with _open(src, password) as pdf:
        if replace:
            for pi in sorted({p[0] for p in plan}):
                clear_viewports(pdf, pi)
        for pi, sc, bb, nm, source, extra in plan:
            vp = add_viewport(pdf, pi, sc, bb, name=nm)
            row = {**vp.to_dict(), "source": source, **extra}
            written.append(row)
            if not quiet:
                print(f"p.{pi + 1}: {sc.label} (ratio {sc.ratio:g}, {sc.r_string()}) from {source}")
        pdf.save(o)
    for n in notes:
        if not quiet:
            print(n)
    return finalize(o, src, options=gate, password=password, data={"viewports": written, "notes": notes}, quiet=quiet)


# ====================================================================== measurement
@dataclass
class Measurement:
    kind: str  # length | area | perimeter | count
    value: float  # in *unit* (ft, sq ft as unit 'ft' squared, ea)
    unit: str  # 'ft', 'sq ft', 'ea' …
    text: str
    page: int
    points: list[Pt]
    holes: list[list[Pt]] = field(default_factory=list)
    scale_label: str = ""
    ratio: float | None = None
    source: str = ""  # 'viewport <name>' | 'given scale'
    paper: float = 0.0  # points (length) or points² (area)
    base_unit: str = ""  # length unit of the scale
    display: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["points"] = [list(p) for p in self.points]
        d["holes"] = [[list(p) for p in h] for h in self.holes]
        return d


def _polylen(pts: Sequence[Pt], closed: bool = False) -> float:
    seq = list(pts) + ([pts[0]] if closed and len(pts) > 2 else [])
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(seq, seq[1:]))


def _shoelace(pts: Sequence[Pt]) -> float:
    n = len(pts)
    return abs(0.5 * sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n)))


def _centroid(pts: Sequence[Pt]) -> Pt:
    n = len(pts)
    a = 0.0
    cx = cy = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cr = x0 * y1 - x1 * y0
        a += cr
        cx += (x0 + x1) * cr
        cy += (y0 + y1) * cr
    if abs(a) < 1e-9:
        return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
    return (cx / (3 * a), cy / (3 * a))


def _viewport_for(pdf: pikepdf.Pdf, page_index: int, pts: Sequence[Pt]) -> Viewport | None:
    vps = read_viewports(pdf, page_index)
    if not vps:
        return None
    g = _geom(pdf.pages[page_index])
    us = [g.user_pt(*p) for p in pts]
    for vp in reversed(vps):  # §12.9.2: last matching viewport wins
        if vp.subtype == "RL" and all(vp.contains_user(*u) for u in us):
            return vp
    for vp in reversed(vps):
        if vp.subtype == "RL" and vp.contains_user(*us[0]):
            return vp
    return None


def measure_points(pdf: pikepdf.Pdf, page_index: int, kind: str, points: Any, *, holes: Any = None,
                   scale: Scale | str | None = None, units: str | None = None) -> Measurement:
    """Measure on an open document. *kind*: length | area | perimeter | count. Points: top-left visual.

    The scale comes from the /VP viewport containing the points unless *scale* is given. *units* converts
    the result (``ft``, ``in``, ``m``, ``ft-in`` for length; ``sq ft``, ``sq m``, ``sq yd`` for area).
    """
    kind = {"linear": "length", "polyline": "length", "polygon": "area", "line": "length"}.get(kind, kind)
    if kind not in ("length", "area", "perimeter", "count"):
        raise ValueError(f"unknown measurement kind {kind!r}")
    pts = _pts(points)
    hs = [_pts(h) for h in (holes or [])]
    need = {"length": 2, "area": 3, "perimeter": 3, "count": 1}[kind]
    if len(pts) < need:
        raise ValueError(f"{kind} needs at least {need} points")
    page = pdf.pages[page_index]
    sc = _as_scale(scale) if scale is not None else None
    if kind == "count":
        return Measurement("count", float(len(pts)), "ea", f"{len(pts)} ea", page_index, pts, [],
                           sc.label if sc else "", sc.ratio if sc else None, "count")
    if sc is not None:
        factor, base, display = sc.factor(_user_unit(page)), sc.real_unit, sc.display
        label, ratio, source = sc.label, sc.ratio, "given scale"
    else:
        vp = _viewport_for(pdf, page_index, pts)
        if vp is None:
            raise ValueError(f"p.{page_index + 1}: no /Measure viewport contains these points — set the scale first "
                             "(pdfcore plans scale …) or pass a scale")
        factor, base, display = vp.factor, vp.unit, vp.display
        label = vp.scale.label if vp.scale else vp.r
        ratio, source = vp.ratio, f"viewport '{vp.name}'"
        if base not in IN_PER:
            display = base
    if kind in ("length", "perimeter"):
        paper = _polylen(pts, closed=(kind == "perimeter"))
        val = paper * factor
        disp = display
        if units:
            if units == "ft-in":
                disp = "ft-in"
            else:
                u = unit_word(units) or units
                val, base, disp = convert_length(val, base, u), u, u
        text = format_length(val, base, disp) if base in IN_PER else f"{_num(val)} {base}"
        if disp == "ft-in" and base in IN_PER:
            val, base = convert_length(val, base, "ft"), "ft"
        return Measurement(kind, val, base, text, page_index, pts, [], label, ratio, source, paper, base, disp)
    paper = _shoelace(pts) - sum(_shoelace(h) for h in hs)
    val = paper * factor * factor
    au = "ft" if display == "ft-in" and base in IN_PER else base
    if base in IN_PER and au != base:
        val *= (IN_PER[base] / IN_PER[au]) ** 2
    if units:
        u = area_unit(units) or unit_word(units)
        if u is None:
            raise ValueError(f"unknown area unit {units!r}")
        val *= (IN_PER[au] / IN_PER[u]) ** 2
        au = u
    return Measurement("area", val, f"sq {au}", format_area(val, au), page_index, pts, hs, label, ratio, source,
                       paper, au, display)


# ---------------------------------------------------------------------- annotations
def _caption_form(pdf: pikepdf.Pdf, text: str, size: float, color: tuple[float, float, float]):
    from reportlab.pdfbase.pdfmetrics import stringWidth

    from pdfcore.fonts import register_reportlab
    from pdfcore.ops.markup import rl_form

    names = register_reportlab()
    font = names["sans-semibold"]
    pad = size * 0.35
    w = stringWidth(text, font, size) + 2 * pad
    h = size * 1.45

    def draw(c):
        c.setFillColorRGB(1, 1, 1)
        c.setStrokeColorRGB(*color)
        c.setLineWidth(0.6)
        c.roundRect(0.3, 0.3, w - 0.6, h - 0.6, min(3, h / 4), fill=1, stroke=1)
        c.setFillColorRGB(*color)
        c.setFont(font, size)
        c.drawCentredString(w / 2, h * 0.5 - size * 0.36, text)

    return rl_form(pdf, w, h, draw), w, h


def _place_caption(ap, form, w: float, h: float, at: Pt, angle: float, offset: float) -> list[Pt]:
    """Draw *form* centred at *at* (drawing space), rotated *angle*, shifted *offset* along the normal.

    Returns the caption's corner points (drawing space) for the bbox.
    """
    ca, sa = math.cos(angle), math.sin(angle)
    name = f"/T{len(ap.xo)}"
    ap.xo[name] = form
    M = _mul((1, 0, 0, 1, -w / 2, offset - (h / 2 if offset == 0 else 0)), (ca, sa, -sa, ca, at[0], at[1]))
    ap.add(f"q {_fm(M)} cm {name} Do Q")
    return [_apply(M, x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h))]


def _readable(angle: float) -> float:
    while angle > math.pi / 2 + 1e-9:
        angle -= math.pi
    while angle <= -math.pi / 2 - 1e-9:
        angle += math.pi
    return angle


def annotate_measurement(pdf: pikepdf.Pdf, m: Measurement, *, color: Any = "red", width: float = 1.5,
                         author: str | None = None, name: str | None = None, category: str | None = None,
                         scale: Scale | str | None = None, caption_size: float | None = None) -> list[Dictionary]:
    """Add measurement annotations for *m*: /Line (/IT /LineDimension), /PolyLine (/PolyLineDimension),
    /Polygon (/PolygonDimension) with /Measure and a caption, or one /Circle per point for counts."""
    from pdfcore.ops.markup import _AP, NAMED_COLORS, _attach, _bs, _common, _f, _head, _rg, _unit, parse_color

    pg = pdf.pages[m.page]
    g = _geom(pg)
    col = parse_color(color) or NAMED_COLORS["red"]
    dark = tuple(v * 0.8 for v in col)
    size = caption_size or max(8.0, min(14.0, math.hypot(g.vw, g.vh) / 220))
    sc = _as_scale(scale) if scale is not None else None
    if m.kind != "count":
        if sc is not None:
            md = make_measure(pdf, sc, _user_unit(pg))
        else:
            vp = _viewport_for(pdf, m.page, m.points)
            md = pg.obj.VP[vp.index].Measure if vp is not None else None
    else:
        md = None
    pts = [g.d(*p) for p in m.points]
    subj = name or {"length": "Length Measurement", "perimeter": "Perimeter Measurement",
                    "area": "Area Measurement", "count": "Count"}[m.kind]
    made: list[Dictionary] = []

    def finish(d: Dictionary, ap, bbox: tuple[float, float, float, float], it: str | None) -> None:
        if it:
            d.IT = Name("/" + it)
        if md is not None:
            d.Measure = md
        d.PdfStudioKind = Name("/" + m.kind)
        d.PdfStudioValue = float(m.value)
        d.PdfStudioUnit = String(m.unit)
        if name:
            d.PdfStudioName = String(name)
        if category:
            d.PdfStudioCategory = String(category)
        _bs(d, width)
        made.append(_attach(pdf, pg, d, ap.build(bbox)))

    if m.kind == "count":
        r = max(4.0, size * 0.75)
        n = len(pts)
        for i, (u, v) in enumerate(pts):
            ap = _AP(pdf, g)
            k = 0.5523 * r
            ap.add(_rg(col, True), _rg(col), f"{_f(width)} w")
            ap.add(f"{_f(u + r)} {_f(v)} m {_f(u + r)} {_f(v + k)} {_f(u + k)} {_f(v + r)} {_f(u)} {_f(v + r)} c "
                   f"{_f(u - k)} {_f(v + r)} {_f(u - r)} {_f(v + k)} {_f(u - r)} {_f(v)} c "
                   f"{_f(u - r)} {_f(v - k)} {_f(u - k)} {_f(v - r)} {_f(u)} {_f(v - r)} c "
                   f"{_f(u + k)} {_f(v - r)} {_f(u + r)} {_f(v - k)} {_f(u + r)} {_f(v)} c h S")
            form, w, h = _caption_form(pdf, str(i + 1), size * 0.8, dark)
            corners = _place_caption(ap, form, w, h, (u + r * 0.7 + w / 2, v + r * 0.7 + h / 2), 0.0, 0.0)
            bbox = g.user_bbox([(u - r, v - r), (u + r, v + r)] + corners, width + 1)
            d = _common(pdf, pg, "Circle", bbox,
                        color=col, opacity=1.0, author=author, subject=subj, contents=f"{subj} {i + 1} of {n}",
                        created=None, modified=None)
            d.RD = Array([0, 0, 0, 0])
            finish(d, ap, bbox, None)
        return made

    ap = _AP(pdf, g)
    ap.add(_rg(col, True), f"{_f(width)} w 1 j 0 J")
    tick = max(6.0, 4 * width)
    corners: list[Pt] = []
    if m.kind == "length" and len(pts) == 2:
        a, b = pts
        ap.add(f"{_f(a[0])} {_f(a[1])} m {_f(b[0])} {_f(b[1])} l S")
        ap.add(*_head("Butt", a, _unit(b, a), tick * 2, col, width))
        ap.add(*_head("Butt", b, _unit(a, b), tick * 2, col, width))
        ang = _readable(math.atan2(b[1] - a[1], b[0] - a[0]))
        form, w, h = _caption_form(pdf, m.text, size, dark)
        corners = _place_caption(ap, form, w, h, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), ang, width + 2)
        bbox = g.user_bbox(pts + corners, tick + width + 1)
        d = _common(pdf, pg, "Line", bbox, color=col, opacity=1.0, author=author, subject=subj, contents=m.text,
                    created=None, modified=None)
        d.L = Array([float(v) for v in (*g.user(*a), *g.user(*b))])
        d.LE = Array([Name.Butt, Name.Butt])
        d.Cap = True
        d.CP = Name.Top
        finish(d, ap, bbox, "LineDimension")
        return made
    closed = m.kind in ("area", "perimeter")
    path = f"{_f(pts[0][0])} {_f(pts[0][1])} m " + " ".join(f"{_f(u)} {_f(v)} l" for u, v in pts[1:])
    if m.kind == "area":
        gs = Dictionary(Type=Name.ExtGState, ca=0.18, CA=1.0)
        ap.gs["/GF"] = gs
        sub_paths = [path + " h"]
        for hole in m.holes:
            hp = [g.d(*p) for p in hole]
            sub_paths.append(f"{_f(hp[0][0])} {_f(hp[0][1])} m " + " ".join(f"{_f(u)} {_f(v)} l" for u, v in hp[1:]) + " h")
        ap.add("q /GF gs", _rg(col), " ".join(sub_paths) + " f* Q")
        ap.add(" ".join(sub_paths) + " S")
        c = _centroid(pts)
        form, w, h = _caption_form(pdf, m.text, size, dark)
        corners = _place_caption(ap, form, w, h, c, 0.0, 0.0)
        bbox = g.user_bbox(pts + corners, width + 1)
        d = _common(pdf, pg, "Polygon", bbox, color=col, opacity=1.0, author=author, subject=subj, contents=m.text,
                    created=None, modified=None)
        d.IC = Array([float(v) for v in col])
        verts: list[float] = []
        for p in pts:
            verts.extend(g.user(*p))
        d.Vertices = Array([float(v) for v in verts])
        finish(d, ap, bbox, "PolygonDimension")
        return made
    # polyline length / closed perimeter
    seq = pts + ([pts[0]] if closed else [])
    ap.add(f"{_f(seq[0][0])} {_f(seq[0][1])} m " + " ".join(f"{_f(u)} {_f(v)} l" for u, v in seq[1:]) + " S")
    if not closed:
        ap.add(*_head("Butt", seq[0], _unit(seq[1], seq[0]), tick * 2, col, width))
        ap.add(*_head("Butt", seq[-1], _unit(seq[-2], seq[-1]), tick * 2, col, width))
    k = max(range(len(seq) - 1), key=lambda i: math.hypot(seq[i + 1][0] - seq[i][0], seq[i + 1][1] - seq[i][1]))
    a, b = seq[k], seq[k + 1]
    ang = _readable(math.atan2(b[1] - a[1], b[0] - a[0]))
    text = m.text if not closed else f"P {m.text}"
    form, w, h = _caption_form(pdf, text, size, dark)
    corners = _place_caption(ap, form, w, h, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), ang, width + 2)
    bbox = g.user_bbox(seq + corners, tick + width + 1)
    d = _common(pdf, pg, "PolyLine", bbox, color=col, opacity=1.0, author=author, subject=subj, contents=text,
                created=None, modified=None)
    verts = []
    for p in seq:
        verts.extend(g.user(*p))
    d.Vertices = Array([float(v) for v in verts])
    d.LE = Array([Name.Butt, Name.Butt] if not closed else [Name("/None"), Name("/None")])
    finish(d, ap, bbox, "PolyLineDimension")
    return made


def _as_shapes(v: Any) -> list[list[Pt]]:
    """One shape or several: 'x,y;x,y' | [[x,y],…] | [[[x,y],…], …]."""
    if v is None:
        return []
    if isinstance(v, str):
        return [_pts(v)]
    seq = list(v)
    if not seq:
        return []
    if isinstance(seq[0], str):
        return [_pts(s) for s in seq]
    if isinstance(seq[0], (int, float)):
        return [_pts(seq)]
    if seq and isinstance(seq[0], (list, tuple)) and seq[0] and isinstance(seq[0][0], (list, tuple)):
        return [_pts(s) for s in seq]
    return [_pts(seq)]


def measure(src: str | Path, *, page: int | None = None, sheet: str | None = None, length: Any = None,
            area: Any = None, perimeter: Any = None, count: Any = None, holes: Any = None,
            scale: Scale | str | None = None, units: str | None = None, annotate: bool = False,
            color: Any = "red", name: str | None = None, category: str | None = None, author: str | None = None,
            out: str | Path | None = None, password: str | None = None, gate=None, quiet: bool = False) -> OpResult:
    """Measure lengths / areas / perimeters / counts on one sheet; with *annotate*, write a new PDF with
    measurement annotations other viewers read. Each of *length*/*area*/*perimeter*/*count* takes one
    point list or a list of them. *holes* (point lists) are subtracted from every area."""
    src = Path(src)
    pi = _resolve_pages(src, page=page if page is not None else (None if sheet else 0), sheet=sheet,
                        password=password)[0]
    hs = _as_shapes(holes) if holes is not None else []
    jobs = [(k, s) for k, v in (("length", length), ("area", area), ("perimeter", perimeter), ("count", count))
            for s in _as_shapes(v)]
    if not jobs:
        raise ValueError("nothing to measure: give --length, --area, --perimeter or --count points")
    results: list[Measurement] = []
    with _open(src, password) as pdf:
        for k, s in jobs:
            m = measure_points(pdf, pi, k, s, holes=hs if k == "area" else None, scale=scale, units=units)
            results.append(m)
            if not quiet:
                extra = f" ({m.value:,.3f} {m.unit})" if m.kind != "count" else ""
                print(f"p.{pi + 1} {m.kind}: {m.text}{extra} · scale {m.scale_label or '—'} ({m.source})")
        data = {"page": pi, "measurements": [m.to_dict() for m in results]}
        if not annotate:
            return OpResult(None, None, [], data)
        for m in results:
            annotate_measurement(pdf, m, color=color, name=name, category=category, author=author, scale=scale)
        o = output_path(src, "measured", out=out)
        pdf.save(o)
    return finalize(o, src, options=gate, password=password, data=data, quiet=quiet)


# ====================================================================== takeoff
_KIND_TITLE = {"length": "Length", "perimeter": "Length", "area": "Area", "count": "Count"}


def _qty_header(unit: str) -> str:
    if unit in ("ea", "each", "count"):
        return "Count (ea)"
    if area_unit(unit):
        return f"Area ({unit})"
    if unit_word(unit):
        return f"Length ({unit})"
    return f"Quantity ({unit})"


def harvest_measurements(src: str | Path, *, password: str | None = None) -> list[dict]:
    """Takeoff rows from measurement annotations already in a PDF (ours, Acrobat's, Bluebeam's…)."""
    rows: list[dict] = []
    counts: dict[tuple[int, str, str], dict] = {}
    with _open(src, password) as pdf:
        for pi, page in enumerate(pdf.pages):
            vps = read_viewports(pdf, pi)
            uu = _user_unit(page)
            for a in page.obj.get("/Annots", []) or []:
                try:
                    st = str(a.get("/Subtype", ""))
                    it = str(a.get("/IT", ""))
                    kind = str(a.get("/PdfStudioKind", "")).lstrip("/")
                    if not (kind or "/Measure" in a or it.endswith("Dimension")):
                        continue
                    subj = str(a.get("/Subj", "")) or ""
                    nm = str(a.get("/PdfStudioName", "")) or subj
                    cat = str(a.get("/PdfStudioCategory", "")) or subj or "Measurements"
                    if kind == "count" or (st == "/Circle" and not it):
                        key = (pi, nm or "Count", cat)
                        c = counts.setdefault(key, {"type": "count", "name": nm or "Count", "category": cat,
                                                    "page": pi, "quantity": 0.0, "unit": "ea", "source": "annotation"})
                        c["quantity"] += 1
                        continue
                    if st == "/Line":
                        L = [float(v) for v in a.L]
                        upts = [(L[0], L[1]), (L[2], L[3])]
                    elif st in ("/PolyLine", "/Polygon"):
                        V = [float(v) for v in a.Vertices]
                        upts = [(V[i], V[i + 1]) for i in range(0, len(V) - 1, 2)]
                    else:
                        continue
                    md = a.get("/Measure")
                    if md is not None:
                        unit, factor, ratio, display, r, sc, sub_t = _decode_measure(md, uu)
                    else:
                        vp = next((v for v in reversed(vps) if v.contains_user(*upts[0])), None)
                        if vp is None:
                            continue
                        unit, factor, ratio, display = vp.unit, vp.factor, vp.ratio, vp.display
                        sc = vp.scale
                    label = sc.label if sc else ""
                    if st == "/Polygon":
                        typ = "area"
                        au = "ft" if display == "ft-in" else unit
                        q = _shoelace(upts) * factor * factor
                        if unit in IN_PER and au != unit:
                            q *= (IN_PER[unit] / IN_PER[au]) ** 2
                        u = f"sq {au}"
                    else:
                        closed = st == "/PolyLine" and len(upts) > 2 and math.hypot(
                            upts[0][0] - upts[-1][0], upts[0][1] - upts[-1][1]) < 0.01
                        typ = "perimeter" if closed or kind == "perimeter" else "length"
                        q = _polylen(upts) * factor
                        u = unit
                        if display == "ft-in" and unit in IN_PER:
                            q, u = convert_length(q, unit, "ft"), "ft"
                    su = str(a.get("/PdfStudioUnit", ""))
                    if su and su != u:  # report in the unit the measurement was made in
                        if typ == "area" and area_unit(su) and area_unit(u) in IN_PER and area_unit(su) in IN_PER:
                            q *= (IN_PER[area_unit(u)] / IN_PER[area_unit(su)]) ** 2
                            u = su
                        elif typ != "area" and unit_word(su) and unit_word(u):
                            q, u = convert_length(q, u, su), su
                    if "/PdfStudioValue" in a and su == u and str(a.get("/M", "")) == str(a.get("/CreationDate", "")):
                        q = float(a.PdfStudioValue)  # unedited since we wrote it: exact (areas with holes)
                    rows.append({"type": typ, "name": nm or typ.title(), "category": cat, "page": pi,
                                 "quantity": q, "unit": u, "scale": label, "source": "annotation"})
                except Exception:
                    continue
    rows.extend(counts.values())
    return rows


def _load_spec(spec: str | Path | dict | list) -> dict:
    if isinstance(spec, (str, Path)) and Path(spec).exists():
        import yaml

        data = yaml.safe_load(Path(spec).read_text(encoding="utf-8"))
    elif isinstance(spec, str):
        import yaml

        data = yaml.safe_load(spec)
    else:
        data = spec
    if isinstance(data, list):
        data = {"items": data}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("takeoff spec needs an 'items:' list")
    return data


def _write_takeoff_xlsx(rows: list[dict], path: Path, title: str, currency: str = "$") -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Takeoff"
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="DDE3EA")
    sub_fill = PatternFill("solid", fgColor="F1F3F5")
    thin = Side(style="thin", color="9AA5B1")
    money = f'"{currency}"#,##0.00'
    qty_fmt = "#,##0.00"
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Generated {date.today().isoformat()} by PDF Studio · quantities incl. waste = quantity × (1 + waste %)"
    heads = ["Category", "Type", "Item", "Sheet", "Page", "Scale", "Quantity", "Unit", "Waste %",
             "Qty incl. waste", f"Unit cost ({currency}/unit)", f"Extended cost ({currency})", "Source"]
    HR = 4
    for c, h in enumerate(heads, 1):
        cell = ws.cell(HR, c, h)
        cell.font = bold
        cell.fill = head_fill
        cell.border = Border(bottom=thin)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    order: list[str] = []
    for r in rows:
        if r["category"] not in order:
            order.append(r["category"])
    r_i = HR + 1
    first = r_i
    for cat in order:
        grp = [r for r in rows if r["category"] == cat]
        a = r_i
        for it in grp:
            ws.cell(r_i, 1, cat)
            ws.cell(r_i, 2, it["type"])
            ws.cell(r_i, 3, it["name"])
            ws.cell(r_i, 4, it.get("sheet") or "")
            ws.cell(r_i, 5, (it["page"] + 1) if it.get("page") is not None else None)
            ws.cell(r_i, 6, it.get("scale") or "")
            ws.cell(r_i, 7, round(float(it["quantity"]), 6)).number_format = qty_fmt
            ws.cell(r_i, 8, it["unit"])
            ws.cell(r_i, 9, float(it.get("waste", 0) or 0) / 100.0).number_format = "0.0%"
            ws.cell(r_i, 10, f"=G{r_i}*(1+I{r_i})").number_format = qty_fmt
            if it.get("unit_cost") is not None:
                ws.cell(r_i, 11, float(it["unit_cost"])).number_format = money
                ws.cell(r_i, 12, f"=J{r_i}*K{r_i}").number_format = money
            ws.cell(r_i, 13, it.get("source", ""))
            r_i += 1
        b = r_i - 1
        units = {it["unit"] for it in grp}
        ws.cell(r_i, 3, f"Subtotal — {cat}").font = bold
        if len(units) == 1:
            ws.cell(r_i, 8, next(iter(units))).font = bold
            c = ws.cell(r_i, 10, f"=SUBTOTAL(9,J{a}:J{b})")
            c.number_format, c.font = qty_fmt, bold
        c = ws.cell(r_i, 12, f"=SUBTOTAL(9,L{a}:L{b})")
        c.number_format, c.font = money, bold
        for col in range(1, len(heads) + 1):
            ws.cell(r_i, col).fill = sub_fill
        r_i += 1
    last = r_i - 1
    ws.cell(r_i, 3, "Grand total").font = Font(bold=True, size=12)
    c = ws.cell(r_i, 12, f"=SUBTOTAL(9,L{first}:L{last})")
    c.number_format, c.font = money, Font(bold=True, size=12)
    for col in range(1, len(heads) + 1):
        ws.cell(r_i, col).border = Border(top=thin)
    total_row = r_i
    widths = [18, 11, 30, 9, 6, 16, 13, 8, 9, 15, 15, 17, 12]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(HR + 1, 1)
    ws.auto_filter.ref = f"A{HR}:M{last}"

    # ---- summary
    sm = wb.create_sheet("Summary")
    sm["A1"] = f"Takeoff summary — {title}"
    sm["A1"].font = Font(bold=True, size=14)
    unit_order: list[str] = []
    for key in ("length", "area", "count", "other"):
        for r in rows:
            u = r["unit"]
            k = "count" if u in ("ea", "each") else ("area" if area_unit(u) else ("length" if unit_word(u) else "other"))
            if k == key and u not in unit_order:
                unit_order.append(u)
    rng = lambda col: f"Takeoff!${col}${first}:${col}${last}"  # noqa: E731
    python_totals: dict[str, dict] = {"by_category": {}, "by_type": {}}
    row = 3
    for section, key_col, field_name in (("By category", "A", "category"), ("By type", "B", "type")):
        sm.cell(row, 1, section).font = Font(bold=True, size=12)
        row += 1
        hdr = [section.split()[-1].title()] + [_qty_header(u) for u in unit_order] + [f"Cost ({currency})"]
        for c, h in enumerate(hdr, 1):
            cell = sm.cell(row, c, h)
            cell.font, cell.fill, cell.border = bold, head_fill, Border(bottom=thin)
        row += 1
        keys: list[str] = []
        for r in rows:
            if r[field_name] not in keys:
                keys.append(r[field_name])
        a = row
        bucket = python_totals["by_category" if field_name == "category" else "by_type"]
        for k in keys:
            sm.cell(row, 1, k)
            for j, u in enumerate(unit_order, 2):
                f = f'=SUMIFS({rng("J")},{rng(key_col)},$A{row},{rng("H")},"{u}")'
                sm.cell(row, j, f).number_format = qty_fmt
            cc = sm.cell(row, len(unit_order) + 2, f"=SUMIF({rng(key_col)},$A{row},{rng('L')})")
            cc.number_format = money
            tot = {u: sum(r["total"] for r in rows if r[field_name] == k and r["unit"] == u) for u in unit_order}
            tot["cost"] = sum(r["cost"] or 0 for r in rows if r[field_name] == k)
            bucket[k] = tot
            row += 1
        b = row - 1
        sm.cell(row, 1, "Total").font = bold
        for j in range(2, len(unit_order) + 3):
            col = get_column_letter(j)
            c = sm.cell(row, j, f"=SUM({col}{a}:{col}{b})")
            c.font = bold
            c.number_format = money if j == len(unit_order) + 2 else qty_fmt
            c.border = Border(top=thin)
        row += 3
    sm.column_dimensions["A"].width = 22
    for j in range(2, len(unit_order) + 3):
        sm.column_dimensions[get_column_letter(j)].width = 16
    python_totals["grand_cost"] = sum(r["cost"] or 0 for r in rows)
    python_totals["by_unit"] = {u: sum(r["total"] for r in rows if r["unit"] == u) for u in unit_order}
    wb.save(path)

    # verify what we wrote
    from openpyxl import load_workbook

    chk = load_workbook(path)
    if chk.sheetnames[:2] != ["Takeoff", "Summary"]:
        raise RuntimeError("takeoff workbook is missing its sheets")
    formulas = sum(1 for wsx in chk for rr in wsx.iter_rows() for c in rr if isinstance(c.value, str) and c.value.startswith("="))
    n_items = sum(1 for rr in chk["Takeoff"].iter_rows(min_row=first, max_row=last) if rr[0].value)
    if n_items != len(rows) or formulas < len(rows):
        raise RuntimeError("takeoff workbook verification failed")
    python_totals["total_row"] = total_row
    python_totals["formulas"] = formulas
    return python_totals


def takeoff(src: str | Path, spec: str | Path | dict | list | None = None, *, out: str | Path | None = None,
            xlsx: str | Path | None = None, annotate: bool | None = None, password: str | None = None, gate=None,
            quiet: bool = False) -> OpResult:
    """Quantity takeoff → XLSX (Takeoff sheet with subtotals, Summary with SUMIFS by category and type).

    *spec* (YAML path/text, dict or list)::

        annotate: true            # also write a PDF with measurement annotations
        defaults: {category: Electrical, waste: 5}
        items:
          - {type: length, name: Feeder F1, category: Conduit, sheet: E-301, points: "216,432;1800,432", waste: 10, unit_cost: 12.5}
          - {type: area, name: Slab, unit: sq ft, page: 3, rects: [[216, 432, 1800, 1440]], holes: [...]}
          - {type: count, name: Receptacles, sheet: E-301, points: [[360, 1296], [648, 1296]]}

    ``page`` is 0-based (or use ``sheet``); ``points`` / ``rects`` in points, top-left. ``unit`` converts
    (ft, m, sq ft, sq yd…), ``multiplier`` repeats an item, ``scale`` overrides the sheet's viewport.
    Without *spec*, measurement annotations already in the PDF are harvested.
    """
    src = Path(src)
    rows: list[dict] = []
    pdf_out: Path | None = None
    if spec is None:
        rows = harvest_measurements(src, password=password)
        if not rows:
            raise ValueError("no measurement annotations in the PDF — pass a takeoff spec (YAML)")
        for r in rows:
            r.setdefault("waste", 0)
            r.setdefault("unit_cost", None)
        do_annot = False
    else:
        data = _load_spec(spec)
        defaults = data.get("defaults") or {}
        do_annot = bool(data.get("annotate", False)) if annotate is None else annotate
        sheet_cache: dict[str, int] = {}
        with _open(src, password) as pdf:
            for n, raw in enumerate(data["items"]):
                it = {**defaults, **raw}
                typ = str(it.get("type", "length")).lower()
                typ = {"linear": "length", "polyline": "length", "polygon": "area", "counts": "count"}.get(typ, typ)
                if it.get("sheet"):
                    s = str(it["sheet"])
                    if s not in sheet_cache:
                        sheet_cache[s] = find_sheet_page(src, s, password=password)
                    pi = sheet_cache[s]
                else:
                    pi = int(it.get("page", 0))
                shapes = _as_shapes(it.get("points"))
                for rc in it.get("rects") or []:
                    r = _rect_of(rc)
                    shapes.append([((r.x0 + r.x1) / 2, (r.top + r.bottom) / 2)] if typ == "count" else _rect_poly(r))
                if not shapes:
                    raise ValueError(f"item {n + 1} ({it.get('name', typ)}): no points or rects")
                if typ == "count" and it.get("points") is not None and it.get("rects"):
                    pass
                unit = it.get("unit")
                kind = "perimeter" if typ == "length" and it.get("rects") and not it.get("points") else typ
                qty = 0.0
                ms: list[Measurement] = []
                if typ == "count":
                    pts = [p for s in shapes for p in s]
                    m = measure_points(pdf, pi, "count", pts)
                    ms.append(m)
                    qty = m.value
                    unit = "ea"
                else:
                    for s in shapes:
                        k = "perimeter" if kind == "perimeter" else kind
                        m = measure_points(pdf, pi, k, s, holes=it.get("holes") if typ == "area" else None,
                                           scale=it.get("scale"), units=(None if unit in (None, "ft-in") else unit))
                        ms.append(m)
                        qty += m.value
                    unit = ms[0].unit if unit in (None, "ft-in") else (
                        f"sq {area_unit(unit)}" if typ == "area" and area_unit(unit) else (unit_word(unit) or unit))
                qty *= float(it.get("multiplier", 1) or 1)
                rows.append({"type": typ, "name": str(it.get("name") or f"Item {n + 1}"),
                             "category": str(it.get("category") or "Uncategorized"), "page": pi,
                             "sheet": str(it.get("sheet") or ""), "quantity": qty, "unit": unit,
                             "waste": float(it.get("waste", 0) or 0), "unit_cost": it.get("unit_cost"),
                             "scale": ms[0].scale_label if ms else "", "source": "spec"})
                if do_annot:
                    for m in ms:
                        annotate_measurement(pdf, m, color=it.get("color", "red"), name=rows[-1]["name"],
                                             category=rows[-1]["category"], scale=it.get("scale"))
            if do_annot:
                pdf_out = output_path(src, "takeoff", out=out if out and not str(out).endswith(".xlsx") else None)
                pdf.save(pdf_out)
    for r in rows:
        r["total"] = float(r["quantity"]) * (1 + float(r.get("waste", 0) or 0) / 100)
        r["cost"] = r["total"] * float(r["unit_cost"]) if r.get("unit_cost") is not None else None
    xp = Path(xlsx) if xlsx else (Path(out) if out and str(out).endswith(".xlsx") else output_path(src, "takeoff", ".xlsx"))
    totals = _write_takeoff_xlsx(rows, xp, f"Quantity takeoff — {src.name}")
    if not quiet:
        for r in rows:
            print(f"{r['category']:<18} {r['type']:<9} {r['name']:<28} {r['total']:>12,.2f} {r['unit']}")
        print(f"{xp}  ({len(rows)} items, {totals['formulas']} formulas)")
    data_out = {"items": rows, "totals": totals, "xlsx": str(xp)}
    if pdf_out is not None:
        return finalize(pdf_out, src, options=gate, password=password, extra=[xp], data=data_out, quiet=quiet)
    return OpResult(None, None, [xp], data_out)


# ====================================================================== sheet sizes
SHEET_SIZES: dict[str, tuple[float, float]] = {  # inches, portrait
    "letter": (8.5, 11), "legal": (8.5, 14), "ansi-a": (8.5, 11), "ansi-b": (11, 17), "tabloid": (11, 17),
    "ansi-c": (17, 22), "ansi-d": (22, 34), "ansi-e": (34, 44),
    "arch-a": (9, 12), "arch-b": (12, 18), "arch-c": (18, 24), "arch-d": (24, 36), "arch-e1": (30, 42),
    "arch-e": (36, 48),
    "iso-a4": (210 / 25.4, 297 / 25.4), "iso-a3": (297 / 25.4, 420 / 25.4), "iso-a2": (420 / 25.4, 594 / 25.4),
    "iso-a1": (594 / 25.4, 841 / 25.4), "iso-a0": (841 / 25.4, 1189 / 25.4),
}
SIZE_NAMES = {
    "letter": "Letter", "legal": "Legal", "tabloid": "Tabloid", "ansi-a": "ANSI A", "ansi-b": "ANSI B",
    "ansi-c": "ANSI C", "ansi-d": "ANSI D", "ansi-e": "ANSI E", "arch-a": "ARCH A", "arch-b": "ARCH B",
    "arch-c": "ARCH C", "arch-d": "ARCH D", "arch-e1": "ARCH E1", "arch-e": "ARCH E", "iso-a4": "A4",
    "iso-a3": "A3", "iso-a2": "A2", "iso-a1": "A1", "iso-a0": "A0",
}


def _size_key(name: str) -> str | None:
    s = re.sub(r"[\s_]+", "", str(name).strip().lower()).replace("-", "")
    for k in SHEET_SIZES:
        if s == k.replace("-", ""):
            return k
    m = re.fullmatch(r"(?:iso)?a([0-4])", s)
    if m:
        return f"iso-a{m.group(1)}"
    m = re.fullmatch(r"(ansi|arch)([a-e]1?)", s)
    if m and f"{m.group(1)}-{m.group(2)}" in SHEET_SIZES:
        return f"{m.group(1)}-{m.group(2)}"
    if s in ("ledger",):
        return "tabloid"
    return None


def parse_size(spec: str) -> tuple[float, float, str]:
    """'arch-d' | 'ANSI B' | 'A3' | '17x11in' | '420x297mm' → (width_in, height_in, name). Named sizes portrait."""
    k = _size_key(spec)
    if k:
        w, h = SHEET_SIZES[k]
        return w, h, SIZE_NAMES[k]
    m = re.fullmatch(r"\s*([\d.]+)\s*[x×]\s*([\d.]+)\s*(in|mm|cm|pt)?\s*", str(spec).lower())
    if m:
        u = m.group(3) or "in"
        f = IN_PER[u]
        return float(m.group(1)) * f, float(m.group(2)) * f, f"{m.group(1)}×{m.group(2)} {u}"
    raise ValueError(f"unknown sheet size {spec!r} (try arch-d, ansi-b, letter, tabloid, a3, 17x11in)")


def detect_size(width_pt: float, height_pt: float, *, tol: float = 0.01) -> dict:
    """Name a sheet size (tolerance: max(3 pt, tol × side)). Returns name, key, aliases, orientation, inches."""
    w_in, h_in = width_pt / PT_PER_IN, height_pt / PT_PER_IN
    a, b = sorted((w_in, h_in))
    order = ["letter", "legal", "ansi-a", "ansi-b", "tabloid", "ansi-c", "ansi-d", "ansi-e", "arch-a", "arch-b",
             "arch-c", "arch-d", "arch-e1", "arch-e", "iso-a4", "iso-a3", "iso-a2", "iso-a1", "iso-a0"]
    hits = []
    for k in order:
        sw, sh = SHEET_SIZES[k]
        if abs(a - sw) <= max(3 / 72, tol * sw) and abs(b - sh) <= max(3 / 72, tol * sh):
            hits.append(k)
    orient = "landscape" if w_in > h_in + 1e-6 else ("portrait" if h_in > w_in + 1e-6 else "square")
    return {"name": SIZE_NAMES[hits[0]] if hits else None, "key": hits[0] if hits else None,
            "aliases": [SIZE_NAMES[k] for k in hits[1:]], "orientation": orient,
            "width_in": round(w_in, 3), "height_in": round(h_in, 3)}


def sheet_sizes(src: str | Path, *, password: str | None = None) -> list[dict]:
    """Displayed size (CropBox, /Rotate applied) and size name of every page."""
    from pdfcore.overlay import visual_size

    out = []
    with _open(src, password) as pdf:
        for i, p in enumerate(pdf.pages):
            w, h = visual_size(p)
            uu = _user_unit(p)
            out.append({"page": i, **detect_size(w * uu, h * uu)})
    return out


# ====================================================================== resize / print to scale
NICE_FACTORS = [1 / 8, 1 / 4, 1 / 3, 1 / 2, 2 / 3, 3 / 4, 1.0, 1.5, 2.0, 3.0, 4.0]


def _scale_annot(a: Dictionary, M: Matrix, s: float, seen: set) -> None:
    def tx_pairs(arr) -> Array:
        v = [float(x) for x in arr]
        out: list[float] = []
        for i in range(0, len(v) - 1, 2):
            out.extend(_apply(M, v[i], v[i + 1]))
        return Array(out)

    if "/Rect" in a:
        r = [float(x) for x in a.Rect]
        p0, p1 = _apply(M, r[0], r[1]), _apply(M, r[2], r[3])
        a.Rect = Array([min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])])
    for k in ("/QuadPoints", "/Vertices", "/L", "/CL"):
        if k in a:
            a[k] = tx_pairs(a[k])
    if "/InkList" in a:
        a.InkList = Array([tx_pairs(st) for st in a.InkList])
    if "/RD" in a:
        a.RD = Array([float(x) * s for x in a.RD])
    md = a.get("/Measure")
    if md is not None:
        _scale_measure(md, s, seen)


def _scale_measure(md: Any, s: float, seen: set) -> None:
    key = md.objgen if md.is_indirect else id(md)
    if key in seen:
        return
    seen.add(key)
    if str(md.get("/Subtype", "/RL")) != "/RL" or "/X" not in md:
        return
    for arr_key in ("/X", "/Y"):
        if arr_key in md and len(md[arr_key]):
            md[arr_key][0].C = _real(float(md[arr_key][0].C) / s)
    try:
        u, factor, ratio, display, r, sc, st = _decode_measure(md)
        if sc is not None:
            md.R = String(sc.r_string())
    except Exception:
        pass


def _content_extent(src: str | Path, page: int, password: str | None) -> Rect | None:
    """Union of everything drawn on a page (chars, lines, rects, curves, images), top-left visual."""
    from pdfcore.text import open_plumber

    with open_plumber(src, password) as pl:
        p = pl.pages[page]
        boxes = [Rect(o["x0"], o["top"], o["x1"], o["bottom"]) for kind in ("chars", "lines", "rects", "curves", "images")
                 for o in getattr(p, kind) if not (kind == "chars" and not str(o.get("text", "")).strip())]
        W, H = float(p.width), float(p.height)
    boxes = [b for b in boxes if b.x1 > 0 and b.bottom > 0 and b.x0 < W and b.top < H]
    if not boxes:
        return None
    u = Rect.union(boxes)
    return Rect(max(0.0, u.x0), max(0.0, u.top), min(W, u.x1), min(H, u.bottom))


def _fits(ext: Rect, k: float, vw: float, vh: float, tvw: float, tvh: float, tol: float = 1.0) -> bool:
    dx, dy = (tvw - k * vw) / 2, (tvh - k * vh) / 2
    return (k * ext.x0 + dx >= -tol and k * ext.x1 + dx <= tvw + tol and
            k * ext.top + dy >= -tol and k * ext.bottom + dy <= tvh + tol)


def resize(src: str | Path, to: str, *, pages: Sequence[int] | None = None, factor: float | None = None,
           fit: bool = False, out: str | Path | None = None, password: str | None = None, gate=None,
           quiet: bool = False) -> OpResult:
    """Print to scale onto another sheet size, content centred.

    The factor is *factor* if given; with *fit* the exact fit; otherwise the nearest standard print factor
    (50 %, 25 %, 200 % …) within 10 % of the fit, used only when no drawn content would be trimmed, else the
    exact fit. ANSI D → ANSI B and ARCH D → ARCH B are exact 50 % half-size sets; ARCH D → ANSI B is 45.8 %
    unless ``factor=0.5`` is forced (which trims 0.5 in from every edge — the gate reports cut text). Annotations, links and /VP viewports move with the content and every
    /Measure ratio is divided by the factor, so measurements stay right (1/4"=1'-0" becomes 1/8"=1'-0").
    """
    src = Path(src)
    tw_in, th_in, tname = parse_size(to)
    o = output_path(src, re.sub(r"[^a-z0-9]+", "-", tname.lower()).strip("-"), out=out)
    info: list[dict] = []
    with _open(src, password) as pdf:
        idx = list(range(len(pdf.pages))) if pages is None else list(pages)
        seen: set = set()
        for i in idx:
            page = pdf.pages[i]
            g = _geom(page)
            uu = _user_unit(page)
            landscape = g.vw > g.vh
            tvw, tvh = sorted((tw_in, th_in), reverse=landscape)
            tvw, tvh = tvw * PT_PER_IN / uu, tvh * PT_PER_IN / uu  # target, visual, user units
            fit_k = min(tvw / g.vw, tvh / g.vh)
            if factor is not None:
                k = float(factor)
            elif fit:
                k = fit_k
            else:
                k = min(NICE_FACTORS, key=lambda f: abs(f - fit_k) / fit_k)
                if abs(k - fit_k) / fit_k > 0.10:
                    k = fit_k
                elif k > fit_k + 1e-9:
                    # a standard factor that trims the sheet is only used if no drawn content is lost
                    ext = _content_extent(src, i, password)
                    if ext is None or not _fits(ext, k, g.vw, g.vh, tvw, tvh):
                        k = fit_k
            TW, TH = (tvh, tvw) if g.rot in (90, 270) else (tvw, tvh)
            M: Matrix = (k, 0, 0, k, (TW - k * g.W) / 2 - k * g.x0, (TH - k * g.H) / 2 - k * g.y0)
            page.contents_coalesce()
            data = page.obj.Contents.read_bytes() if "/Contents" in page.obj else b""
            page.obj.Contents = pdf.make_stream(f"q {_fm(M)} cm\n".encode() + data + b"\nQ\n")
            page.obj.MediaBox = Array([0, 0, round(TW, 4), round(TH, 4)])
            for bk in ("/CropBox", "/BleedBox", "/TrimBox", "/ArtBox"):
                if bk in page.obj:
                    del page.obj[bk]
            for a in page.obj.get("/Annots", []) or []:
                _scale_annot(a, M, k, seen)
            vps = page.obj.get("/VP")
            for vp in (vps if isinstance(vps, Array) else ([vps] if vps is not None else [])):
                bb = [float(v) for v in vp.BBox]
                p0, p1 = _apply(M, bb[0], bb[1]), _apply(M, bb[2], bb[3])
                vp.BBox = Array([min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])])
                if "/Measure" in vp:
                    _scale_measure(vp.Measure, k, seen)
                    try:
                        sc = _decode_measure(vp.Measure, uu)[5]
                        if sc is not None:
                            vp.Name = String(sc.label)
                    except Exception:
                        pass
            dx, dy = (tvw - k * g.vw) / 2, (tvh - k * g.vh) / 2
            crop = (max(0.0, -dx) * 2 * uu / PT_PER_IN, max(0.0, -dy) * 2 * uu / PT_PER_IN)
            info.append({"page": i, "factor": k, "fit_factor": fit_k, "dx": dx, "dy": dy,
                         "trimmed_in": [round(crop[0], 3), round(crop[1], 3)],
                         "size": detect_size(tvw * uu, tvh * uu)})
            if not quiet:
                trim = f", trims {crop[0] / 2:.2f} in per side × {crop[1] / 2:.2f} in top/bottom" if crop[0] > 0.01 or crop[1] > 0.01 else ""
                scl = "; ".join(v.scale.label for v in read_viewports(pdf, i) if v.scale) if "/VP" in page.obj else ""
                print(f"p.{i + 1}: {k:.1%} onto {tname}{trim}" + (f" · scale now {scl}" if scl else ""))
        pdf.save(o)
    return finalize(o, src, options=gate, password=password,
                    data={"pages": info, "to": tname,
                          "transform": "x' = factor·x + dx, top' = factor·top + dy (points, top-left)"}, quiet=quiet)


# ====================================================================== tiling
def _row_label(r: int) -> str:
    s = ""
    r += 1
    while r:
        r, rem = divmod(r - 1, 26)
        s = chr(65 + rem) + s
    return s


def _grid(content: float, window: float, overlap: float) -> int:
    if content <= window + 1e-6:
        return 1
    return int(math.ceil((content - window) / (window - overlap) - 1e-9)) + 1


def tile(src: str | Path, *, paper: str = "letter", pages: Sequence[int] | None = None, overlap: float = 36.0,
         margin: float = 36.0, scale: float = 1.0, orientation: str = "auto", marks: bool = True,
         labels: bool = True, out: str | Path | None = None, password: str | None = None, gate=None,
         quiet: bool = False) -> OpResult:
    """Tile big sheets onto small paper for tiled printing (*overlap*, *margin* in points; *scale* 1 = full size).

    Each tile carries crop marks at the printable window, overlap ticks, a label (A1 = row A, column 1,
    top-left first) and a key map of the grid. Tile pages show the window through their CropBox; the
    rest of the sheet stays in the MediaBox, clipped.
    """
    from reportlab.pdfgen import canvas

    from pdfcore.fonts import register_reportlab
    from pdfcore.overlay import visual_box, visual_to_user_matrix

    src = Path(src)
    pw_in, ph_in, pname = parse_size(paper)
    o = output_path(src, f"tiled-{re.sub(r'[^a-z0-9]+', '-', pname.lower()).strip('-')}", out=out)
    register_reportlab()
    tiles: list[dict] = []
    grids: dict[int, list[int]] = {}
    with _open(src, password) as pdf:
        idx = list(range(len(pdf.pages))) if pages is None else list(pages)
        res = pikepdf.new()
        try:
            res.docinfo = res.copy_foreign(pdf.docinfo)
        except Exception:
            pass
        plan = []
        for i in idx:
            page = pdf.pages[i]
            x0, y0, W, H, rot = visual_box(page)
            vw, vh = (H, W) if rot in (90, 270) else (W, H)
            cw, ch = vw * scale, vh * scale
            best = None
            for orient in (("portrait", "landscape") if orientation == "auto" else (orientation,)):
                pw, ph = sorted((pw_in * PT_PER_IN, ph_in * PT_PER_IN), reverse=(orient == "landscape"))
                ww, wh = pw - 2 * margin, ph - 2 * margin
                if ww <= overlap or wh <= overlap:
                    raise ValueError("margin/overlap too large for the paper")
                cols, rows = _grid(cw, ww, overlap), _grid(ch, wh, overlap)
                cand = (cols * rows, orient, pw, ph, ww, wh, cols, rows)
                if best is None or cand[0] < best[0]:
                    best = cand
            plan.append((i, page, (x0, y0, W, H, rot), vw, vh, cw, ch, best))
        # overlays (labels, marks) in one ReportLab document
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pageCompression=1)
        for i, page, box, vw, vh, cw, ch, (_n, orient, pw, ph, ww, wh, cols, rows) in plan:
            grids[i] = [rows, cols]
            for r in range(rows):
                for col in range(cols):
                    c.setPageSize((pw, ph))
                    _draw_tile_overlay(c, pw, ph, margin, ww, wh, overlap, r, col, rows, cols, i, scale, marks,
                                       labels, pname)
                    c.showPage()
        c.save()
        ov = pikepdf.open(io.BytesIO(buf.getvalue()))
        k = 0
        for i, page, (x0, y0, W, H, rot), vw, vh, cw, ch, (_n, orient, pw, ph, ww, wh, cols, rows) in plan:
            form = page.as_form_xobject(False)
            form.BBox = Array([x0, y0, x0 + W, y0 + H])
            form = res.copy_foreign(form)
            Minv = _inv(visual_to_user_matrix(page))
            stepx, stepy = ww - overlap, wh - overlap
            gx, gy = (cols - 1) * stepx + ww, (rows - 1) * stepy + wh
            ox, oy = (gx - cw) / 2, (gy - ch) / 2
            for r in range(rows):
                for col in range(cols):
                    wx, wy = col * stepx - ox, r * stepy - oy  # window top-left in scaled visual coords
                    T: Matrix = (1, 0, 0, 1, margin - wx, ph - ch + wy - margin)
                    M = _mul(_mul(Minv, (scale, 0, 0, scale, 0, 0)), T)
                    corners = [_apply(M, px, py) for px, py in ((x0, y0), (x0 + W, y0), (x0, y0 + H), (x0 + W, y0 + H))]
                    mb = [min(0.0, min(p[0] for p in corners)), min(0.0, min(p[1] for p in corners)),
                          max(pw, max(p[0] for p in corners)), max(ph, max(p[1] for p in corners))]
                    # MediaBox at origin (0, 0) holding the whole placed sheet; the CropBox is the paper.
                    sx, sy = -mb[0], -mb[1]
                    M = _mul(M, (1, 0, 0, 1, sx, sy))
                    np_ = res.add_blank_page(page_size=(pw, ph))
                    ovf = res.copy_foreign(ov.pages[k].as_form_xobject())
                    np_.obj.MediaBox = Array([0, 0, round(mb[2] - mb[0], 3), round(mb[3] - mb[1], 3)])
                    np_.obj.CropBox = Array([round(sx, 3), round(sy, 3), round(sx + pw, 3), round(sy + ph, 3)])
                    np_.obj.TrimBox = np_.obj.CropBox
                    np_.obj.Resources = Dictionary(XObject=Dictionary(Src=form, Ov=ovf))
                    cs = (f"q {sx + margin:.3f} {sy + margin:.3f} {ww:.3f} {wh:.3f} re W n {_fm(M)} cm /Src Do Q\n"
                          f"q 1 0 0 1 {sx:.3f} {sy:.3f} cm /Ov Do Q\n")
                    np_.obj.Contents = res.make_stream(cs.encode())
                    label = f"{_row_label(r)}{col + 1}"
                    tiles.append({"page": len(res.pages) - 1, "label": label, "row": r, "col": col, "src_page": i,
                                  "paper": pname, "orientation": orient})
                    k += 1
            if not quiet:
                print(f"p.{i + 1}: {rows} rows × {cols} cols = {rows * cols} {pname} {orient} tiles at {scale:.0%}")
        res.save(o)
        ov.close()
        res.close()
    from pdfcore.gate import GateOptions

    g = gate or GateOptions()
    g.allow_clipped = True  # each tile page carries the whole sheet; only its tile is meant to be visible
    return finalize(o, src, options=g, password=password,
                    data={"tiles": tiles, "grid": grids, "paper": pname, "overlap": overlap, "margin": margin},
                    quiet=quiet)


def _draw_tile_overlay(c, pw: float, ph: float, m: float, ww: float, wh: float, overlap: float, r: int, col: int,
                       rows: int, cols: int, src_page: int, scale: float, marks: bool, labels: bool,
                       pname: str) -> None:
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    x0, y0, x1, y1 = m, m, m + ww, m + wh
    L, gap = min(18.0, m * 0.6), 3.0
    if marks:
        for x, y, sx, sy in ((x0, y0, -1, -1), (x1, y0, 1, -1), (x0, y1, -1, 1), (x1, y1, 1, 1)):
            c.line(x + sx * gap, y, x + sx * (gap + L), y)
            c.line(x, y + sy * gap, x, y + sy * (gap + L))
        # overlap ticks: where the neighbouring tile's window starts/ends
        c.setStrokeColorRGB(0.1, 0.45, 0.85)
        c.setDash(2, 2)
        t = min(10.0, m * 0.35)
        if col < cols - 1:
            x = x1 - overlap
            c.line(x, y1 + gap, x, y1 + gap + t)
            c.line(x, y0 - gap, x, y0 - gap - t)
        if col > 0:
            x = x0 + overlap
            c.line(x, y1 + gap, x, y1 + gap + t)
            c.line(x, y0 - gap, x, y0 - gap - t)
        if r < rows - 1:
            y = y0 + overlap
            c.line(x0 - gap, y, x0 - gap - t, y)
            c.line(x1 + gap, y, x1 + gap + t, y)
        if r > 0:
            y = y1 - overlap
            c.line(x0 - gap, y, x0 - gap - t, y)
            c.line(x1 + gap, y, x1 + gap + t, y)
        c.setDash()
    if labels:
        lab = f"{_row_label(r)}{col + 1}"
        base = max(4.0, m * 0.25)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Inter-Bold", min(14.0, m * 0.4))
        c.drawString(x0 + 6, base, lab)
        c.setFont("Inter", min(7.0, m * 0.2))
        c.drawString(x0 + 6 + min(14.0, m * 0.4) * 2.2, base,
                     f"page {src_page + 1} · row {_row_label(r)} of {_row_label(rows - 1)} · column {col + 1} of {cols}"
                     f" · {scale:.0%} · overlap {overlap / 72:g} in · {pname}")
        # key map
        cell = min(5.0, (m * 0.6) / max(rows, 1), 60.0 / max(cols, 1))
        kx = x1 - cols * cell
        ky = base
        c.setLineWidth(0.3)
        for rr in range(rows):
            for cc in range(cols):
                yy = ky + (rows - 1 - rr) * cell
                if rr == r and cc == col:
                    c.setFillColorRGB(0.1, 0.45, 0.85)
                    c.rect(kx + cc * cell, yy, cell, cell, fill=1, stroke=1)
                else:
                    c.rect(kx + cc * cell, yy, cell, cell, fill=0, stroke=1)


# ====================================================================== layers (optional content)
def _oc_state(pdf: pikepdf.Pdf) -> tuple[dict, list]:
    ocp = pdf.Root.get("/OCProperties")
    if ocp is None:
        return {}, []
    ocgs = list(ocp.get("/OCGs", []) or [])
    D = ocp.get("/D") or Dictionary()
    base = str(D.get("/BaseState", "/ON"))
    state = {g.objgen: base != "/OFF" for g in ocgs}
    for g in D.get("/ON", []) or []:
        state[g.objgen] = True
    for g in D.get("/OFF", []) or []:
        state[g.objgen] = False
    return state, ocgs


def _eval_ve(e: Any, state: dict) -> bool:
    if isinstance(e, Array) and len(e) and isinstance(e[0], Name):
        op, args = str(e[0]), list(e)[1:]
        vals = [_eval_ve(a, state) for a in args]
        if op == "/And":
            return all(vals)
        if op == "/Or":
            return any(vals)
        if op == "/Not":
            return not vals[0] if vals else True
        return True
    try:
        return state.get(e.objgen, True)
    except Exception:
        return True


def _oc_visible(oc: Any, state: dict) -> bool:
    if oc is None:
        return True
    try:
        if not isinstance(oc, Dictionary):
            return True
        if str(oc.get("/Type", "")) == "/OCMD" or "/OCGs" in oc or "/VE" in oc:
            if "/VE" in oc:
                return _eval_ve(oc.VE, state)
            ocgs = oc.get("/OCGs")
            if ocgs is None:
                return True
            lst = list(ocgs) if isinstance(ocgs, Array) else [ocgs]
            vals = [state.get(g.objgen, True) for g in lst if g is not None]
            if not vals:
                return True
            p = str(oc.get("/P", "/AnyOn"))
            return {"/AllOn": all(vals), "/AnyOff": not all(vals), "/AllOff": not any(vals)}.get(p, any(vals))
        return state.get(oc.objgen, True)
    except Exception:
        return True


def _filter_ops(instrs: list, props: Any, xobjs: Any, state: dict, stats: dict) -> list:
    out: list = []
    stack: list[str] = []
    skip = 0
    for ins in instrs:
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            if not skip:
                out.append(ins)
            continue
        op = str(ins.operator)
        if skip:
            if op in ("BDC", "BMC"):
                skip += 1
            elif op == "EMC":
                skip -= 1
            continue
        if op == "BDC" and len(ins.operands) == 2 and ins.operands[0] == Name.OC:
            ref = ins.operands[1]
            oc = props.get(ref) if isinstance(ref, Name) and props is not None else ref
            if not _oc_visible(oc, state):
                skip = 1
                stats["sequences"] += 1
                continue
            stack.append("drop")
            continue
        if op in ("BDC", "BMC"):
            stack.append("keep")
            out.append(ins)
            continue
        if op == "EMC":
            if stack and stack.pop() == "drop":
                continue
            out.append(ins)
            continue
        if op == "Do" and xobjs is not None and ins.operands:
            xo = xobjs.get(ins.operands[0])
            if xo is not None and "/OC" in xo and not _oc_visible(xo.OC, state):
                stats["xobjects"] += 1
                continue
        out.append(ins)
    return out


def _flatten_stream(pdf: pikepdf.Pdf, stream: pikepdf.Object, res: Any, state: dict, stats: dict, seen: set,
                    holders: list) -> None:
    key = stream.objgen
    if key in seen and key != (0, 0):
        return
    seen.add(key)
    res = stream.get("/Resources") or res
    instrs = list(pikepdf.parse_content_stream(stream))
    new = _filter_ops(instrs, res.get("/Properties") if res is not None else None,
                      res.get("/XObject") if res is not None else None, state, stats)
    stream.write(pikepdf.unparse_content_stream(new))
    _flatten_resources(pdf, res, state, stats, seen, holders)


def _flatten_resources(pdf: pikepdf.Pdf, res: Any, state: dict, stats: dict, seen: set, holders: list) -> None:
    if res is None:
        return
    xobjs = res.get("/XObject")
    if xobjs is not None:
        for _k, xo in list(xobjs.items()):
            if not isinstance(xo, pikepdf.Stream):
                continue
            if "/OC" in xo:
                holders.append(xo)
            if xo.get("/Subtype") == Name.Form:
                _flatten_stream(pdf, xo, res, state, stats, seen, holders)
    holders.append(("props", res))


def list_layers(src: str | Path, *, password: str | None = None) -> list[dict]:
    """Optional content groups: name, on/off in the default configuration, locked, intent."""
    with _open(src, password) as pdf:
        state, ocgs = _oc_state(pdf)
        D = (pdf.Root.get("/OCProperties") or Dictionary()).get("/D") or Dictionary()
        locked = {g.objgen for g in (D.get("/Locked", []) or [])}
        out = []
        for g in ocgs:
            it = g.get("/Intent", Name.View)
            out.append({"name": str(g.get("/Name", "")), "on": state.get(g.objgen, True),
                        "locked": g.objgen in locked,
                        "intent": [str(x).lstrip("/") for x in it] if isinstance(it, Array) else str(it).lstrip("/")})
        return out


def set_layers(src: str | Path, *, off: Iterable[str] = (), on: Iterable[str] = (), flatten: bool = False,
               out: str | Path | None = None, password: str | None = None, gate=None, quiet: bool = False) -> OpResult:
    """Turn layers on/off by name (case-insensitive) in the default configuration (/D /ON /OFF);
    with *flatten*, bake the resulting visibility into the content — remove marked content, form/image
    XObjects (/OC) and annotations (/OC) of OFF layers — and drop /OCProperties."""
    src = Path(src)
    off, on = [s.lower() for s in off], [s.lower() for s in on]
    with _open(src, password) as pdf:
        ocp = pdf.Root.get("/OCProperties")
        if ocp is None:
            if off or on:
                raise ValueError("this PDF has no layers (no /OCProperties)")
            if not flatten:
                raise ValueError("nothing to do: this PDF has no layers")
        state, ocgs = _oc_state(pdf)
        names = {str(g.get("/Name", "")).lower(): g for g in ocgs}
        unknown = [n for n in off + on if n not in names]
        if unknown:
            raise ValueError(f"unknown layer(s) {unknown}; layers: {[str(g.get('/Name', '')) for g in ocgs]}")
        if ocp is not None:
            D = ocp.get("/D")
            if D is None:
                ocp.D = Dictionary()
                D = ocp.D
            on_set = {g.objgen: g for g in (D.get("/ON", []) or [])}
            off_set = {g.objgen: g for g in (D.get("/OFF", []) or [])}
            for g in ocgs:
                nm = str(g.get("/Name", "")).lower()
                if nm in off:
                    on_set.pop(g.objgen, None)
                    off_set[g.objgen] = g
                if nm in on:
                    off_set.pop(g.objgen, None)
                    on_set[g.objgen] = g
            D.ON = Array(list(on_set.values()))
            D.OFF = Array(list(off_set.values()))
        state, ocgs = _oc_state(pdf)
        stats = {"sequences": 0, "xobjects": 0, "annotations": 0}
        if flatten:
            seen: set = set()
            holders: list = []
            for page in pdf.pages:
                res = page.obj.get("/Resources")
                if "/Contents" in page.obj:
                    instrs = list(pikepdf.parse_content_stream(page))
                    new = _filter_ops(instrs, res.get("/Properties") if res is not None else None,
                                      res.get("/XObject") if res is not None else None, state, stats)
                    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new))
                _flatten_resources(pdf, res, state, stats, seen, holders)
                annots = page.obj.get("/Annots")
                if annots is not None:
                    keep = Array()
                    for a in annots:
                        ap_n = (a.get("/AP") or Dictionary()).get("/N")
                        aps = [ap_n] if isinstance(ap_n, pikepdf.Stream) else (
                            [v for _k, v in ap_n.items() if isinstance(v, pikepdf.Stream)] if isinstance(ap_n, Dictionary) else [])
                        hidden = not _oc_visible(a.get("/OC"), state) or any(
                            "/OC" in s and not _oc_visible(s.OC, state) for s in aps)
                        if hidden:
                            stats["annotations"] += 1
                            continue
                        if "/OC" in a:
                            del a["/OC"]
                        for s in aps:
                            if "/OC" in s:
                                del s["/OC"]
                            _flatten_stream(pdf, s, s.get("/Resources"), state, stats, seen, holders)
                        keep.append(a)
                    page.obj.Annots = keep
            for h in holders:
                if isinstance(h, tuple):
                    props = h[1].get("/Properties")
                    if props is not None:
                        for k in list(props.keys()):
                            v = props[k]
                            if isinstance(v, Dictionary) and str(v.get("/Type", "")) in ("/OCG", "/OCMD"):
                                del props[k]
                elif "/OC" in h:
                    del h["/OC"]
            if "/OCProperties" in pdf.Root:
                del pdf.Root["/OCProperties"]
        tag = "flattened" if flatten else "layers"
        o = output_path(src, tag, out=out)
        pdf.save(o)
    if not quiet:
        if off or on:
            print(f"off: {', '.join(off) or '—'} · on: {', '.join(on) or '—'}")
        if flatten:
            print(f"flattened: removed {stats['sequences']} hidden sequences, {stats['xobjects']} XObjects, "
                  f"{stats['annotations']} annotations; /OCProperties dropped")
    return finalize(o, src, options=gate, password=password, data={"stats": stats}, quiet=quiet)


# ====================================================================== CLI
def _gate(ns: argparse.Namespace):
    return gate_options_from(ns)


def _page_arg(ns: argparse.Namespace) -> int | None:
    return ns.page - 1 if getattr(ns, "page", None) else None


def _pages_arg(ns: argparse.Namespace, src: str) -> list[int] | None:
    if getattr(ns, "sheet", None):
        return [find_sheet_page(src, ns.sheet, password=ns.password)]
    if getattr(ns, "page", None):
        return [ns.page - 1]
    if getattr(ns, "pages", None):
        return parse_pages(ns.pages, _page_count(src, ns.password))
    return None


def _sel(p: argparse.ArgumentParser, multi: bool = True) -> None:
    p.add_argument("--sheet", help="sheet number from the title block, e.g. E-301")
    p.add_argument("--page", type=int, help="1-based page")
    if multi:
        p.add_argument("--pages", help="page range, e.g. 1-3,5")


@sub("scale", "Set drawing scale: write /VP /Measure viewports (scale text, calibration, scale bar, or auto)")
def _cli_scale(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    _sel(p)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--scale", help="""e.g. '1/4"=1'-0"', '1"=20'"'"'', '1:100', '1:50 (mm)'""")
    g.add_argument("--calibrate", metavar="x0,y0,x1,y1=DIST", help="two points a known distance apart (16ft, 4.88m), or 'bar'")
    g.add_argument("--scale-bar", action="store_true", help="calibrate from the scale bar drawn on the sheet")
    g.add_argument("--list", action="store_true", help="print the viewports already in the PDF")
    p.add_argument("--bbox", help="viewport box x0,top,x1,bottom in points (default: whole sheet)")
    p.add_argument("--name", help="viewport name (default: the scale label)")
    p.add_argument("--replace", action="store_true", help="remove the page's existing viewports first")
    p.add_argument("--no-snap", action="store_true", help="keep the calibrated ratio exactly (no standard-scale snap)")
    add_common(p)

    def run(ns):
        if ns.list:
            rows = read_scales(ns.pdf, password=ns.password)
            for r in rows:
                print(f"p.{r['page'] + 1} [{r['index']}] {r['name']!r}: {r['label'] or '—'} · R '{r['r']}' · "
                      f"ratio {r['ratio']:g} · {r['unit']} · bbox {r['bbox']}" if r["ratio"] else
                      f"p.{r['page'] + 1} [{r['index']}] {r['name']!r}: {r['subtype']} R '{r['r']}'")
            if not rows:
                print("no /Measure viewports")
            return OpResult(None, data={"viewports": rows})
        return set_scale(ns.pdf, page=_pages_arg(ns, ns.pdf), scale=ns.scale,
                         calibrate_with=ns.calibrate, scale_bar=ns.scale_bar, bbox=ns.bbox, name=ns.name,
                         replace=ns.replace, snap=not ns.no_snap, out=ns.out, password=ns.password, gate=_gate(ns))

    return run


@sub("measure", "Measure length / area / perimeter / count in real units (optionally as measurement annotations)")
def _cli_measure(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    _sel(p, multi=False)
    p.add_argument("--length", action="append", metavar="x,y;x,y…", help="polyline (repeatable)")
    p.add_argument("--area", action="append", metavar="x,y;x,y;x,y…", help="polygon (repeatable)")
    p.add_argument("--perimeter", action="append", metavar="x,y;x,y;x,y…", help="closed polygon (repeatable)")
    p.add_argument("--count", action="append", metavar="x,y;x,y…", help="points to count (repeatable)")
    p.add_argument("--hole", action="append", metavar="x,y;x,y;x,y…", help="polygon subtracted from areas")
    p.add_argument("--rect", action="append", metavar="x0,top,x1,bottom", help="rectangle → area")
    p.add_argument("--scale", help="override the sheet's viewport scale")
    p.add_argument("--units", help="ft, in, m, mm, ft-in, sq ft, sq m, sq yd")
    p.add_argument("--annotate", action="store_true", help="write measurement annotations to a new PDF")
    p.add_argument("--name", help="annotation subject (e.g. 'Feeder F1')")
    p.add_argument("--category", help="takeoff category stored on the annotations")
    p.add_argument("--color", default="red")
    p.add_argument("--author")
    p.add_argument("--json", action="store_true", help="print measurements as JSON")
    add_common(p)

    def run(ns):
        areas = list(ns.area or []) + [_rect_poly(_rect_of(r)) for r in (ns.rect or [])]
        res = measure(ns.pdf, page=_page_arg(ns), sheet=ns.sheet, length=ns.length, area=areas or None,
                      perimeter=ns.perimeter, count=ns.count, holes=ns.hole, scale=ns.scale, units=ns.units,
                      annotate=ns.annotate, color=ns.color, name=ns.name, category=ns.category, author=ns.author,
                      out=ns.out, password=ns.password, gate=_gate(ns), quiet=ns.json)
        if ns.json:
            print(json.dumps(res.data, indent=2))
            if res.output:
                print(f"{res.output}  {res.line()}")
        return res

    return run


@sub("takeoff", "Quantity takeoff → XLSX from a YAML spec or the PDF's measurement annotations")
def _cli_takeoff(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("spec", nargs="?", help="takeoff.yaml (omit to harvest measurement annotations)")
    p.add_argument("--xlsx", help="workbook path (default: next to the PDF)")
    p.add_argument("--annotate", action="store_true", default=None, help="also write annotated PDF")
    add_common(p)

    def run(ns):
        return takeoff(ns.pdf, ns.spec, out=ns.out, xlsx=ns.xlsx, annotate=ns.annotate, password=ns.password,
                       gate=_gate(ns))

    return run


@sub("sizes", "Sheet sizes by name (ANSI, ARCH, ISO, Letter/Legal/Tabloid)")
def _cli_sizes(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--password")
    p.add_argument("--json", action="store_true")

    def run(ns):
        rows = sheet_sizes(ns.pdf, password=ns.password)
        if ns.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                alias = f" (= {', '.join(r['aliases'])})" if r["aliases"] else ""
                print(f"p.{r['page'] + 1}: {r['width_in']:g} × {r['height_in']:g} in  {r['name'] or 'non-standard'}"
                      f"{alias} {r['orientation']}")
        return OpResult(None, data={"pages": rows})

    return run


@sub("resize", "Print to scale on another sheet size (keeps /Measure scales right); --tile for tiled prints")
def _cli_resize(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    _sel(p)
    p.add_argument("--to", help="arch-d, ansi-b, tabloid, letter, a3, 17x11in …")
    p.add_argument("--factor", help="explicit factor: 0.5 or 50%%")
    p.add_argument("--fit", action="store_true", help="exact fit instead of the nearest standard factor")
    p.add_argument("--tile", metavar="PAPER", help="tile onto this paper instead/after (letter, tabloid)")
    p.add_argument("--overlap", type=float, default=0.5, help="tile overlap, inches (default 0.5)")
    add_common(p)

    def run(ns):
        if not ns.to and not ns.tile:
            raise ValueError("give --to SIZE and/or --tile PAPER")
        f = None
        if ns.factor:
            s = ns.factor.strip()
            f = float(s[:-1]) / 100 if s.endswith("%") else float(s)
        pages = _pages_arg(ns, ns.pdf)
        res = None
        src = ns.pdf
        if ns.to:
            res = resize(src, ns.to, pages=pages, factor=f, fit=ns.fit, out=None if ns.tile else ns.out,
                         password=ns.password, gate=_gate(ns))
            src = res.output
        if ns.tile:
            t = tile(src, paper=ns.tile, pages=pages, overlap=ns.overlap * 72, out=ns.out, password=ns.password,
                     gate=_gate(ns))
            if res is not None:
                t.extra_outputs.append(res.output)
            return t
        return res

    return run


@sub("tile", "Tile a large sheet onto Letter/Tabloid pages with overlap, crop marks and tile labels")
def _cli_tile(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    _sel(p)
    p.add_argument("--paper", default="letter", help="letter (default), tabloid, legal, a4, a3 …")
    p.add_argument("--overlap", type=float, default=0.5, help="inches (default 0.5)")
    p.add_argument("--margin", type=float, default=0.5, help="inches (default 0.5)")
    p.add_argument("--scale", default="100%", help="print scale, e.g. 100%% or 0.5")
    p.add_argument("--orientation", choices=["auto", "portrait", "landscape"], default="auto")
    p.add_argument("--no-marks", action="store_true")
    add_common(p)

    def run(ns):
        s = ns.scale.strip()
        sc = float(s[:-1]) / 100 if s.endswith("%") else float(s)
        return tile(ns.pdf, paper=ns.paper, pages=_pages_arg(ns, ns.pdf), overlap=ns.overlap * 72,
                    margin=ns.margin * 72, scale=sc, orientation=ns.orientation, marks=not ns.no_marks, out=ns.out,
                    password=ns.password, gate=_gate(ns))

    return run


@sub("layers", "Layers (optional content): list, turn on/off by name, flatten")
def _cli_layers(p: argparse.ArgumentParser):
    p.add_argument("pdf")
    p.add_argument("--list", action="store_true", help="list layers with on/off state (default when nothing else)")
    p.add_argument("--off", nargs="+", default=[], metavar="NAME")
    p.add_argument("--on", nargs="+", default=[], metavar="NAME")
    p.add_argument("--flatten", action="store_true", help="bake visibility: remove OFF layers, drop /OCProperties")
    add_common(p)

    def run(ns):
        if ns.list or not (ns.off or ns.on or ns.flatten):
            rows = list_layers(ns.pdf, password=ns.password)
            for r in rows:
                print(f"{'ON ' if r['on'] else 'OFF'}  {r['name']}" + ("  (locked)" if r["locked"] else ""))
            if not rows:
                print("no layers")
            if not (ns.off or ns.on or ns.flatten):
                return OpResult(None, data={"layers": rows})
        return set_layers(ns.pdf, off=ns.off, on=ns.on, flatten=ns.flatten, out=ns.out, password=ns.password,
                          gate=_gate(ns))

    return run
