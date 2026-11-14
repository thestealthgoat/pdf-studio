"""AcroForm fields: list, fill (with appearance streams in the embedded house font), create, auto-detect on
flat forms, bulk fill from CSV, flatten, and export (JSON / CSV / XFDF).

Coordinates follow pdfcore convention: :class:`~pdfcore.types.Rect` in points, top-left origin, relative to
the page's MediaBox (unrotated page space, as pdfplumber reports it). Page numbers are 0-based in the API
and in JSON.

Appearance streams are drawn with ReportLab using Inter (embedded, subset) so filled values render the
same in every viewer, including ones that ignore ``/NeedAppearances``. The field's ``/DA`` keeps naming a
standard font for viewers that regenerate appearances while the user types.
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from pdfcore import fonts
from pdfcore.gate import GateOptions
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.result import OpResult
from pdfcore.types import Rect

__all__ = [
    "FieldInfo", "FieldSpec", "list_fields", "fill", "fill_from_csv", "add_fields", "detect_fields",
    "flatten", "export_data", "read_values", "parse_xfdf",
]

AP_FONT = "Inter"
_ASCENT, _DESCENT, _CAP = 0.96875, 0.2421875, 0.727

# Field flag bits (PDF 32000-1 §12.7.3.1, 12.7.4.2–4), 1-based bit positions.
_FLAGS_COMMON = {1: "ReadOnly", 2: "Required", 3: "NoExport"}
_FLAGS = {
    "/Tx": {13: "Multiline", 14: "Password", 21: "FileSelect", 23: "DoNotSpellCheck", 24: "DoNotScroll", 25: "Comb", 26: "RichText"},
    "/Btn": {15: "NoToggleToOff", 16: "Radio", 17: "Pushbutton", 26: "RadiosInUnison"},
    "/Ch": {18: "Combo", 19: "Edit", 20: "Sort", 22: "MultiSelect", 23: "DoNotSpellCheck", 27: "CommitOnSelChange"},
}


def _bit(n: int) -> int:
    return 1 << (n - 1)


MULTILINE, PASSWORD, COMB = _bit(13), _bit(14), _bit(25)
RADIO, PUSHBUTTON, NOTOGGLEOFF = _bit(16), _bit(17), _bit(15)
COMBO, EDIT, MULTISELECT = _bit(18), _bit(19), _bit(22)
REQUIRED, READONLY = _bit(2), _bit(1)


# ====================================================================== model
@dataclass
class Widget:
    page: int
    rect: Rect
    on: str | None = None  # button on-state name (no slash)


@dataclass
class FieldInfo:
    name: str
    type: str  # text | checkbox | radio | choice | signature | button
    value: Any
    options: list[str] = field(default_factory=list)
    rect: Rect | None = None
    page: int | None = None
    flags: list[str] = field(default_factory=list)
    tooltip: str = ""
    widgets: list[Widget] = field(default_factory=list)
    max_len: int | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rect"] = _rect_list(self.rect) if self.rect else None
        d["widgets"] = [{"page": w.page, "rect": _rect_list(w.rect), **({"on": w.on} if w.on else {})} for w in self.widgets]
        return d


def _rect_list(r: Rect) -> list[float]:
    return [round(r.x0, 2), round(r.top, 2), round(r.x1, 2), round(r.bottom, 2)]


class _Field:
    """A terminal field node plus its widget annotations and inherited attributes."""

    def __init__(self, node: Dictionary, name: str, inh: dict[str, Any]):
        self.node = node
        self.name = name
        self.inh = inh
        kids = node.get("/Kids")
        if kids is not None and len(kids) and not any("/T" in k for k in kids):
            self.widgets = list(kids)
        else:
            self.widgets = [node]

    def get(self, key: str, default=None):
        if key in self.node:
            return self.node[key]
        return self.inh.get(key, default)

    @property
    def ft(self) -> str:
        return str(self.get("/FT", ""))

    @property
    def ff(self) -> int:
        try:
            return int(self.get("/Ff", 0) or 0)
        except Exception:
            return 0

    @property
    def kind(self) -> str:
        ft, ff = self.ft, self.ff
        if ft == "/Tx":
            return "text"
        if ft == "/Btn":
            if ff & PUSHBUTTON:
                return "button"
            return "radio" if ff & RADIO else "checkbox"
        if ft == "/Ch":
            return "choice"
        if ft == "/Sig":
            return "signature"
        return ft.lstrip("/").lower() or "unknown"

    def options(self) -> list[tuple[str, str]]:
        """(export value, display text) pairs for a choice field."""
        out = []
        for o in self.get("/Opt", []) or []:
            if isinstance(o, Array) and len(o) >= 2:
                out.append((_pystr(o[0]), _pystr(o[1])))
            else:
                out.append((_pystr(o), _pystr(o)))
        return out

    def on_states(self) -> list[str]:
        states: list[str] = []
        for w in self.widgets:
            s = _on_state(w)
            if s and s not in states:
                states.append(s)
        return states


def _pystr(o) -> str:
    if isinstance(o, Name):
        return str(o)[1:]
    try:
        return str(o)
    except Exception:
        return ""


def _on_state(widget) -> str | None:
    ap = widget.get("/AP")
    if ap is not None:
        for key in ("/N", "/D"):
            n = ap.get(key)
            if isinstance(n, Dictionary) and not isinstance(n, pikepdf.Stream):
                for k in n.keys():
                    if k != "/Off":
                        return k[1:]
    asv = widget.get("/AS")
    if asv is not None and str(asv) != "/Off":
        return str(asv)[1:]
    return None


_INHERIT = ("/FT", "/Ff", "/V", "/DV", "/DA", "/Q", "/Opt", "/MaxLen", "/TU")


def _walk_fields(pdf: pikepdf.Pdf) -> Iterator[_Field]:
    af = pdf.Root.get("/AcroForm")
    if af is None:
        return

    def rec(node, prefix: str, inh: dict, depth: int):
        if depth > 32:
            return
        inh = dict(inh)
        for k in _INHERIT:
            if k in node:
                inh[k] = node[k]
        t = node.get("/T")
        name = f"{prefix}.{_pystr(t)}" if prefix and t is not None else (_pystr(t) if t is not None else prefix)
        kids = node.get("/Kids")
        if kids is not None and any("/T" in k for k in kids):
            for k in kids:
                if "/T" in k:
                    yield from rec(k, name, inh, depth + 1)
            return
        yield _Field(node, name, {k: v for k, v in inh.items() if k not in node})

    for f in af.get("/Fields", []) or []:
        yield from rec(f, "", {}, 0)


def _page_index(pdf: pikepdf.Pdf) -> dict[tuple[int, int], int]:
    out = {}
    for i, p in enumerate(pdf.pages):
        for a in p.obj.get("/Annots", []) or []:
            try:
                out[a.objgen] = i
            except Exception:
                pass
    return out


def _widget_rect(pdf: pikepdf.Pdf, widget, page: int | None) -> Rect:
    llx, lly, urx, ury = [float(v) for v in widget.get("/Rect", [0, 0, 0, 0])]
    if page is None:
        return Rect(min(llx, urx), 0, max(llx, urx), 0)
    mb = [float(v) for v in pdf.pages[page].mediabox]
    x0, y0, y1 = min(mb[0], mb[2]), min(mb[1], mb[3]), max(mb[1], mb[3])
    return Rect.from_pdf(llx - x0, lly - y0, urx - x0, ury - y0, y1 - y0)


def _value(f: _Field) -> Any:
    v = f.get("/V")
    kind = f.kind
    if kind in ("checkbox", "radio"):
        if v is None:
            # Some writers only set /AS on the widget.
            for w in f.widgets:
                a = w.get("/AS")
                if a is not None and str(a) != "/Off":
                    return str(a)[1:]
            return "Off"
        return _pystr(v)
    if v is None:
        return None
    if isinstance(v, Array):
        return [_pystr(x) for x in v]
    if isinstance(v, pikepdf.Stream):
        return v.read_bytes().decode("utf-8", "replace")
    return _pystr(v)


def _flag_names(f: _Field) -> list[str]:
    ff = f.ff
    names = [n for b, n in _FLAGS_COMMON.items() if ff & _bit(b)]
    names += [n for b, n in _FLAGS.get(f.ft, {}).items() if ff & _bit(b)]
    return names


def _info(pdf: pikepdf.Pdf, f: _Field, pidx: dict) -> FieldInfo:
    widgets = []
    for w in f.widgets:
        try:
            pg = pidx.get(w.objgen)
        except Exception:
            pg = None
        if pg is None and "/P" in w:
            try:
                og = w.P.objgen
                pg = next((i for i, p in enumerate(pdf.pages) if p.obj.objgen == og), None)
            except Exception:
                pg = None
        widgets.append(Widget(pg if pg is not None else -1, _widget_rect(pdf, w, pg), _on_state(w) if f.ft == "/Btn" else None))
    kind = f.kind
    opts: list[str] = []
    if kind == "choice":
        opts = [e for e, _ in f.options()]
    elif kind in ("radio", "checkbox"):
        opts = f.on_states()
    ml = f.get("/MaxLen")
    return FieldInfo(
        name=f.name, type=kind, value=_value(f), options=opts,
        rect=widgets[0].rect if widgets else None, page=widgets[0].page if widgets else None,
        flags=_flag_names(f), tooltip=_pystr(f.get("/TU")) if f.get("/TU") is not None else "",
        widgets=widgets, max_len=int(ml) if ml is not None else None,
    )


# ====================================================================== list / read
def list_fields(src: str | Path, *, password: str | None = None) -> list[FieldInfo]:
    """Every terminal field with its type, value, options, first-widget rect/page, flags and all widgets."""
    with pikepdf.open(str(src), password=password or "") as pdf:
        pidx = _page_index(pdf)
        return [_info(pdf, f, pidx) for f in _walk_fields(pdf)]


def read_values(src: str | Path, *, password: str | None = None) -> dict[str, Any]:
    """{field name: value} for every field (buttons: on-state name or 'Off')."""
    return {f.name: f.value for f in list_fields(src, password=password) if f.type not in ("button",)}


# ====================================================================== appearance streams
def _parse_da(da: str) -> tuple[float, tuple[str, tuple[float, ...]]]:
    """Font size and fill colour from a /DA string ('/Helv 10 Tf 0 g')."""
    size = 0.0
    m = re.search(r"([\d.]+)\s+Tf", da or "")
    if m:
        size = float(m.group(1))
    color: tuple[str, tuple[float, ...]] = ("g", (0.0,))
    for pat, kind in ((r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+k\b", "k"), (r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg\b", "rg"), (r"([\d.]+)\s+g\b", "g")):
        m = re.search(pat, da or "")
        if m:
            color = (kind, tuple(float(x) for x in m.groups()))
            break
    return size, color


def _set_fill(c, color: tuple[str, tuple[float, ...]]) -> None:
    kind, v = color
    if kind == "g":
        c.setFillGray(v[0])
    elif kind == "rg":
        c.setFillColorRGB(*v)
    else:
        c.setFillColorCMYK(*v)


def _mk_color(arr) -> tuple[float, ...] | None:
    if arr is None:
        return None
    try:
        vals = [float(x) for x in arr]
    except Exception:
        return None
    return tuple(vals) if vals else None


def _apply_color(c, vals: tuple[float, ...], stroke: bool) -> None:
    if len(vals) == 1:
        (c.setStrokeGray if stroke else c.setFillGray)(vals[0])
    elif len(vals) == 3:
        (c.setStrokeColorRGB if stroke else c.setFillColorRGB)(*vals)
    elif len(vals) == 4:
        (c.setStrokeColorCMYK if stroke else c.setFillColorCMYK)(*vals)


@dataclass
class _Look:
    """Visual parameters of one widget."""

    w: float
    h: float
    rot: int = 0
    bg: tuple[float, ...] | None = None
    bc: tuple[float, ...] | None = None
    bw: float = 1.0
    style: str = "/S"
    size: float = 0.0
    color: tuple[str, tuple[float, ...]] = ("g", (0.0,))
    q: int = 0


def _look(widget, f: _Field | None = None, da: str | None = None) -> _Look:
    llx, lly, urx, ury = [float(v) for v in widget.get("/Rect", [0, 0, 0, 0])]
    w, h = abs(urx - llx), abs(ury - lly)
    mk = widget.get("/MK") or Dictionary()
    rot = int(mk.get("/R", 0) or 0) % 360
    if rot in (90, 270):
        w, h = h, w
    bs = widget.get("/BS") or Dictionary()
    bw = float(bs.get("/W", 1)) if bs is not None else 1.0
    if "/Border" in widget and "/BS" not in widget:
        try:
            bw = float(widget.Border[2])
        except Exception:
            pass
    da_s = da if da is not None else _pystr(widget.get("/DA") or (f.get("/DA") if f else None) or "")
    size, color = _parse_da(da_s)
    q = widget.get("/Q")
    if q is None and f is not None:
        q = f.get("/Q")
    return _Look(w, h, rot, _mk_color(mk.get("/BG")), _mk_color(mk.get("/BC")), bw,
                 str(bs.get("/S", "/S")) if bs is not None else "/S", size, color, int(q or 0))


def _frame(c, lk: _Look, circle: bool = False) -> None:
    """Background and border per /MK."""
    if lk.bg:
        _apply_color(c, lk.bg, stroke=False)
        if circle:
            r = min(lk.w, lk.h) / 2
            c.circle(lk.w / 2, lk.h / 2, r, stroke=0, fill=1)
        else:
            c.rect(0, 0, lk.w, lk.h, stroke=0, fill=1)
    if lk.bc and lk.bw > 0:
        _apply_color(c, lk.bc, stroke=True)
        c.setLineWidth(lk.bw)
        if lk.style == "/D":
            c.setDash(3, 3)
        if circle:
            r = min(lk.w, lk.h) / 2 - lk.bw / 2
            c.circle(lk.w / 2, lk.h / 2, r, stroke=1, fill=0)
        else:
            c.rect(lk.bw / 2, lk.bw / 2, lk.w - lk.bw, lk.h - lk.bw, stroke=1, fill=0)
        c.setDash()
        if lk.style == "/U" and not circle:
            pass


def _clip(c, x: float, y: float, w: float, h: float) -> None:
    p = c.beginPath()
    p.rect(x, y, max(0.0, w), max(0.0, h))
    c.clipPath(p, stroke=0, fill=0)


def _safe(text: str, cover: set[int]) -> str:
    if not cover:
        return text
    return "".join(ch if ord(ch) in cover or ch in "\n\r\t" else "?" for ch in text)


def _wrap(text: str, size: float, width: float) -> list[str]:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    out: list[str] = []
    for para in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        words = para.split(" ")
        cur = ""
        for wd in words:
            trial = wd if not cur else f"{cur} {wd}"
            if stringWidth(trial, AP_FONT, size) <= width or not cur:
                cur = trial
                # hard-break a single word longer than the box
                while stringWidth(cur, AP_FONT, size) > width and len(cur) > 1:
                    k = len(cur)
                    while k > 1 and stringWidth(cur[:k], AP_FONT, size) > width:
                        k -= 1
                    out.append(cur[:k])
                    cur = cur[k:]
            else:
                out.append(cur)
                cur = wd
        out.append(cur)
    return out


def _draw_text_ap(c, lk: _Look, value: str, *, multiline: bool, comb: int = 0, password: bool = False,
                  cover: set[int] = frozenset()) -> None:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    _frame(c, lk)
    pad = max(1.0, lk.bw) + 1.0
    iw, ih = lk.w - 2 * pad, lk.h - 2 * pad
    if not value or iw <= 0 or ih <= 0:
        return
    text = _safe("•" * len(value) if password else value, cover)
    c.saveState()
    _clip(c, pad, pad, iw, ih)
    _set_fill(c, lk.color)
    if comb and not multiline:
        size = lk.size or min(12.0, ih * 0.8)
        cell = lk.w / comb
        y = (lk.h - size * _CAP) / 2
        for k, ch in enumerate(text[:comb]):
            cw = stringWidth(ch, AP_FONT, size)
            c.setFont(AP_FONT, size)
            c.drawString(k * cell + (cell - cw) / 2, y, ch)
    elif multiline:
        size = lk.size or 10.0
        while True:
            lines = _wrap(text, size, iw)
            lead = size * 1.16
            if lk.size or len(lines) * lead <= ih or size <= 4:
                break
            size -= 0.5
        c.setFont(AP_FONT, size)
        y = lk.h - pad - size * _ASCENT + size * 0.1
        for ln in lines:
            if y < pad - size:
                break
            tw = stringWidth(ln, AP_FONT, size)
            x = pad + (iw - tw if lk.q == 2 else (iw - tw) / 2 if lk.q == 1 else 0)
            c.drawString(x, y, ln)
            y -= lead
    else:
        text = text.replace("\n", " ")
        size = lk.size
        if not size:
            size = min(12.0, ih * 0.78)
            tw = stringWidth(text, AP_FONT, size)
            if tw > iw:
                size = max(4.0, size * iw / tw)
        tw = stringWidth(text, AP_FONT, size)
        x = pad + (iw - tw if lk.q == 2 else (iw - tw) / 2 if lk.q == 1 else 1)
        y = (lk.h - size * _CAP) / 2
        c.setFont(AP_FONT, size)
        c.drawString(x, y, text)
    c.restoreState()


def _draw_list_ap(c, lk: _Look, opts: list[str], selected: set[int], top: int, cover: set[int]) -> None:
    _frame(c, lk)
    pad = max(1.0, lk.bw) + 1.0
    size = lk.size or 10.0
    lead = size * 1.2
    c.saveState()
    _clip(c, pad, pad, lk.w - 2 * pad, lk.h - 2 * pad)
    y = lk.h - pad
    for k in range(top, len(opts)):
        if y - lead < pad - lead:
            break
        if k in selected:
            c.setFillColorRGB(0.6, 0.75, 0.95)
            c.rect(pad, y - lead, lk.w - 2 * pad, lead, stroke=0, fill=1)
        _set_fill(c, lk.color)
        c.setFont(AP_FONT, size)
        c.drawString(pad + 1, y - lead + (lead - size * _CAP) / 2, _safe(opts[k], cover))
        y -= lead
    c.restoreState()


def _draw_button_ap(c, lk: _Look, on: bool, radio: bool) -> None:
    _frame(c, lk, circle=radio)
    if not on:
        return
    _set_fill(c, lk.color)
    kind, v = lk.color
    if kind == "g":
        c.setStrokeGray(v[0])
    elif kind == "rg":
        c.setStrokeColorRGB(*v)
    else:
        c.setStrokeColorCMYK(*v)
    w, h = lk.w, lk.h
    if radio:
        c.circle(w / 2, h / 2, min(w, h) * 0.25, stroke=0, fill=1)
    else:
        c.setLineWidth(max(0.8, min(w, h) * 0.12))
        c.setLineCap(1)
        c.setLineJoin(1)
        p = c.beginPath()
        p.moveTo(w * 0.22, h * 0.52)
        p.lineTo(w * 0.42, h * 0.27)
        p.lineTo(w * 0.80, h * 0.76)
        c.drawPath(p, stroke=1, fill=0)


class _APBatch:
    """Collect appearance drawings, render them in one ReportLab document, return Form XObjects."""

    def __init__(self) -> None:
        self.jobs: list[tuple[_Look, Callable]] = []

    def add(self, lk: _Look, draw: Callable) -> int:
        self.jobs.append((lk, draw))
        return len(self.jobs) - 1

    def build(self, pdf: pikepdf.Pdf) -> list[pikepdf.Stream]:
        if not self.jobs:
            return []
        from reportlab.pdfgen import canvas

        fonts.register_reportlab()
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pageCompression=1, initialFontName=AP_FONT, initialFontSize=10)
        for lk, draw in self.jobs:
            c.setPageSize((max(lk.w, 0.5), max(lk.h, 0.5)))
            draw(c)
            c.showPage()
        c.save()
        buf.seek(0)
        src = pikepdf.open(buf)
        out = []
        for (lk, _), page in zip(self.jobs, src.pages):
            fx = pdf.copy_foreign(page.as_form_xobject())
            fx.BBox = Array([0, 0, max(lk.w, 0.5), max(lk.h, 0.5)])
            if lk.rot:
                import math

                a = math.radians(lk.rot)
                ca, sa = round(math.cos(a), 6), round(math.sin(a), 6)
                fx.Matrix = Array([ca, sa, -sa, ca, 0, 0])
            out.append(fx)
        return out


def _font_cover() -> set[int]:
    from reportlab.pdfbase import pdfmetrics

    fonts.register_reportlab()
    try:
        return set(pdfmetrics.getFont(AP_FONT).face.charToGlyph.keys())
    except Exception:
        return set()


def _embedded_font(fd) -> bool:
    try:
        if str(fd.get("/Subtype", "")) == "/Type0":
            return all(_embedded_font(d) for d in fd.get("/DescendantFonts", []))
        desc = fd.get("/FontDescriptor")
        return desc is not None and any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
    except Exception:
        return False


def _draws_unembedded_text(stream) -> bool:
    """True if a Form XObject (appearance) shows text in a font that is not embedded."""
    if not isinstance(stream, pikepdf.Stream):
        return False
    try:
        res = stream.get("/Resources") or Dictionary()
        fdict = res.get("/Font") or Dictionary()
        cur = None
        for operands, op in pikepdf.parse_content_stream(stream):
            o = str(op)
            if o == "Tf" and operands:
                cur = str(operands[0])
            elif o in ("Tj", "TJ", "'", '"') and cur:
                arg = operands[-1] if operands else None
                nonempty = (len(bytes(arg)) > 0) if isinstance(arg, String) else (
                    any(isinstance(a, String) and len(bytes(a)) for a in arg) if isinstance(arg, Array) else True)
                if nonempty:
                    fd = fdict.get(cur)
                    if fd is None or not _embedded_font(fd):
                        return True
    except Exception:
        return True
    return False


def _needs_refresh(widget) -> bool:
    ap = widget.get("/AP")
    if ap is None or "/N" not in ap:
        return True
    n = ap.N
    if isinstance(n, pikepdf.Stream):
        return _draws_unembedded_text(n)
    return any(_draws_unembedded_text(v) for _, v in n.items())


def _hide_widget_ap(widget) -> bool:
    try:
        flags = int(widget.get("/F", 0) or 0)
    except Exception:
        flags = 0
    return bool(flags & 2 or flags & 32)  # Hidden, NoView


def _queue_appearances(f: _Field, batch: _APBatch, cover: set[int]) -> list[Callable[[list], None]]:
    """Queue AP drawings for every widget of *f*; return setters that attach the built XObjects."""
    setters: list[Callable[[list], None]] = []
    kind = f.kind
    val = _value(f)
    da = _pystr(f.get("/DA") or "")
    for w in f.widgets:
        lk = _look(w, f, _pystr(w.get("/DA")) if "/DA" in w else da)
        if kind == "text":
            ff = f.ff
            ml = f.get("/MaxLen")
            comb = int(ml) if (ff & COMB and ml is not None) else 0
            text = val if isinstance(val, str) else ("" if val is None else str(val))
            k = batch.add(lk, lambda c, lk=lk, text=text, ff=ff, comb=comb: _draw_text_ap(
                c, lk, text, multiline=bool(ff & MULTILINE), comb=comb, password=bool(ff & PASSWORD), cover=cover))
            setters.append(lambda xs, w=w, k=k: _set_ap(w, xs[k]))
        elif kind == "choice":
            opts = f.options()
            vals = val if isinstance(val, list) else ([val] if val else [])
            if f.ff & COMBO:
                disp = next((d for e, d in opts if e == (vals[0] if vals else None)), vals[0] if vals else "")
                k = batch.add(lk, lambda c, lk=lk, t=disp: _draw_text_ap(c, lk, t, multiline=False, cover=cover))
            else:
                sel = {i for i, (e, _) in enumerate(opts) if e in vals}
                top = int(f.get("/TI", 0) or 0)
                k = batch.add(lk, lambda c, lk=lk, o=[d for _, d in opts], s=sel, t=top: _draw_list_ap(c, lk, o, s, t, cover))
            setters.append(lambda xs, w=w, k=k: _set_ap(w, xs[k]))
        elif kind in ("checkbox", "radio"):
            on_name = _on_state(w) or "Yes"
            radio = kind == "radio"
            if not lk.bc and not lk.bg:
                lk.bc, lk.bw = (0.0,), 0.8
            k_on = batch.add(lk, lambda c, lk=lk, r=radio: _draw_button_ap(c, lk, True, r))
            k_off = batch.add(lk, lambda c, lk=lk, r=radio: _draw_button_ap(c, lk, False, r))

            def set_btn(xs, w=w, on_name=on_name, k_on=k_on, k_off=k_off):
                w.AP = Dictionary(N=Dictionary({f"/{on_name}": xs[k_on], "/Off": xs[k_off]}))
                cur = _value(f)
                w.AS = Name(f"/{on_name}") if cur == on_name else Name.Off

            setters.append(set_btn)
        elif kind == "signature":
            k = batch.add(lk, lambda c, lk=lk: _frame(c, lk))
            setters.append(lambda xs, w=w, k=k: _set_ap(w, xs[k]))
    return setters


def _set_ap(widget, xobj) -> None:
    widget.AP = Dictionary(N=xobj)


def _refresh(pdf: pikepdf.Pdf, fields: Iterable[_Field]) -> int:
    cover = _font_cover()
    batch = _APBatch()
    setters: list[Callable] = []
    for f in fields:
        setters += _queue_appearances(f, batch, cover)
    xs = batch.build(pdf)
    for s in setters:
        s(xs)
    af = pdf.Root.get("/AcroForm")
    if af is not None and "/NeedAppearances" in af:
        del af["/NeedAppearances"]
    return len(setters)


# ====================================================================== fill
_TRUE = {"yes", "on", "true", "1", "x", "checked", "check", "y"}
_FALSE = {"off", "no", "false", "0", "", "unchecked", "n", "none"}


def _match(fields: dict[str, _Field], key: str) -> _Field | None:
    if key in fields:
        return fields[key]
    tail = [f for n, f in fields.items() if n.split(".")[-1] == key]
    return tail[0] if len(tail) == 1 else None


def _set_value(f: _Field, value: Any) -> None:
    kind = f.kind
    node = f.node
    if kind == "text":
        s = "" if value is None else str(value)
        ml = f.get("/MaxLen")
        if ml is not None and int(ml) > 0:
            s = s[: int(ml)]
        node.V = String(s)
    elif kind == "checkbox":
        states = f.on_states() or ["Yes"]
        sv = str(value).strip() if not isinstance(value, bool) else ("Yes" if value else "Off")
        if sv in states:
            on = sv
        elif sv.lower() in _TRUE:
            on = states[0]
        elif sv.lower() in _FALSE:
            on = None
        else:
            raise ValueError(f"checkbox {f.name!r}: use yes/no or one of {states}, not {value!r}")
        node.V = Name(f"/{on}") if on else Name.Off
        for w in f.widgets:
            ws = _on_state(w)
            w.AS = Name(f"/{ws}") if on and ws == on else (Name(f"/{on}") if on and ws is None else Name.Off)
    elif kind == "radio":
        states = f.on_states()
        sv = "" if value is None else str(value).strip()
        if sv.lower() in _FALSE and sv not in states:
            if f.ff & NOTOGGLEOFF and sv:
                raise ValueError(f"radio {f.name!r} cannot be switched off")
            on = None
        elif sv in states:
            on = sv
        else:
            low = {s.lower(): s for s in states}
            if sv.lower() in low:
                on = low[sv.lower()]
            elif sv.isdigit() and int(sv) < len(states):
                on = states[int(sv)]
            else:
                raise ValueError(f"radio {f.name!r}: {value!r} is not one of {states}")
        node.V = Name(f"/{on}") if on else Name.Off
        for w in f.widgets:
            w.AS = Name(f"/{on}") if on and _on_state(w) == on else Name.Off
    elif kind == "choice":
        opts = f.options()
        exports = [e for e, _ in opts]
        disp = {d: e for e, d in opts}
        vals = list(value) if isinstance(value, (list, tuple)) else ([] if value in (None, "") else [str(value)])
        resolved = []
        for v in vals:
            v = str(v)
            if v in exports:
                resolved.append(v)
            elif v in disp:
                resolved.append(disp[v])
            elif f.ff & EDIT:
                resolved.append(v)
            else:
                raise ValueError(f"choice {f.name!r}: {v!r} is not one of {exports}")
        if len(resolved) > 1 and not f.ff & MULTISELECT:
            raise ValueError(f"choice {f.name!r} allows one value")
        if not resolved:
            node.V = String("")
            if "/I" in node:
                del node["/I"]
        else:
            node.V = String(resolved[0]) if len(resolved) == 1 else Array([String(v) for v in resolved])
            idx = sorted(exports.index(v) for v in resolved if v in exports)
            if idx:
                node.I = Array(idx)
            elif "/I" in node:
                del node["/I"]
    elif kind == "signature":
        raise ValueError(f"{f.name!r} is a signature field; sign it with pdfcore sign")
    else:
        raise ValueError(f"{f.name!r}: cannot fill a {kind} field")


def _fill_pdf(pdf: pikepdf.Pdf, values: Mapping[str, Any], *, strict: bool = False) -> dict[str, Any]:
    fields = {f.name: f for f in _walk_fields(pdf)}
    if not fields:
        raise ValueError("document has no form fields (try pdfcore form-detect)")
    filled, unknown = [], []
    touched: dict[str, _Field] = {}
    for key, val in values.items():
        f = _match(fields, key)
        if f is None:
            unknown.append(key)
            continue
        _set_value(f, val)
        filled.append(f.name)
        touched[f.name] = f
    if unknown and strict:
        raise KeyError(f"no such field(s): {', '.join(unknown)}")
    # Fields we filled, plus any whose existing appearance would show text in a non-embedded font.
    for n, f in fields.items():
        if n not in touched and f.kind in ("text", "choice", "checkbox", "radio") and any(_needs_refresh(w) for w in f.widgets):
            v = _value(f)
            if v not in (None, "", "Off") or f.kind in ("checkbox", "radio"):
                touched[n] = f
    _refresh(pdf, touched.values())
    return {"filled": filled, "unknown": unknown, "refreshed": sorted(touched)}


def parse_xfdf(path_or_text: str | Path) -> dict[str, Any]:
    """{name: value} from an XFDF file (nested <field> names joined with '.')."""
    p = Path(str(path_or_text))
    text = p.read_text(encoding="utf-8") if p.exists() else str(path_or_text)
    root = ET.fromstring(text)
    out: dict[str, Any] = {}

    def local(tag: str) -> str:
        return tag.split("}", 1)[-1]

    def rec(el, prefix: str):
        for ch in el:
            if local(ch.tag) != "field":
                continue
            name = f"{prefix}.{ch.get('name')}" if prefix else ch.get("name", "")
            vals = [v.text or "" for v in ch if local(v.tag) in ("value", "value-richtext")]
            if vals:
                out[name] = vals[0] if len(vals) == 1 else vals
            rec(ch, name)

    for fs in root.iter():
        if local(fs.tag) == "fields":
            rec(fs, "")
            break
    return out


def _load_values(data: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(data, Mapping):
        return dict(data)
    p = Path(str(data))
    if p.suffix.lower() == ".xfdf":
        return parse_xfdf(p)
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            raise ValueError(f"{p} has no data rows")
        return dict(rows[0])
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(str(data))


def fill(
    src: str | Path,
    data: Mapping[str, Any] | str | Path,
    *,
    out: str | Path | None = None,
    flatten_after: bool = False,
    strict: bool = False,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Fill fields from a dict, a JSON string/file, an XFDF file, or a CSV (first row).

    Text → string; checkbox → bool / 'Yes' / 'Off' / on-state name; radio → on-state (export) name or index;
    choice → export value or display text (list for multi-select). Appearances are regenerated in Inter.
    """
    src = Path(src)
    values = _load_values(data)
    dst = output_path(src, "filled", out=out)
    with pikepdf.open(str(src), password=password or "") as pdf:
        info = _fill_pdf(pdf, values, strict=strict)
        if flatten_after:
            info["flattened"] = _flatten_pdf(pdf)
        pdf.save(str(dst))
    if info["unknown"] and not quiet:
        print(f"warning: no such field(s): {', '.join(info['unknown'])}")
    return finalize(dst, baseline=src, options=options, password=password, data=info, quiet=quiet)


# ====================================================================== create
@dataclass
class FieldSpec:
    """A field to add. ``rect`` is top-left-origin points on ``page`` (0-based).

    type: text | multiline | password | checkbox | radio | choice (dropdown) | list | signature
    For ``radio``: ``widgets`` = [(export_value, Rect), …] (``rect`` unused); ``options`` ignored.
    For ``choice``/``list``: ``options`` = list of values (or (export, display) pairs).
    """

    name: str
    type: str
    page: int = 0
    rect: Rect | None = None
    value: Any = None
    options: list[Any] = field(default_factory=list)
    widgets: list[tuple[str, Rect]] = field(default_factory=list)
    tooltip: str = ""
    required: bool = False
    read_only: bool = False
    font_size: float = 0.0  # 0 = auto
    max_len: int | None = None
    editable: bool = False  # choice: allow typed values
    multi_select: bool = False  # list
    align: int = 0  # 0 left, 1 centre, 2 right
    border: tuple[float, ...] | None = (0.55,)  # border colour (gray / rgb); None = no border
    background: tuple[float, ...] | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FieldSpec":
        d = dict(d)
        if d.get("rect") is not None and not isinstance(d["rect"], Rect):
            d["rect"] = Rect(*[float(v) for v in d["rect"]])
        if d.get("widgets"):
            ws = []
            for w in d["widgets"]:
                if isinstance(w, Mapping):
                    ws.append((str(w["value"]), Rect(*[float(v) for v in w["rect"]])))
                else:
                    ws.append((str(w[0]), w[1] if isinstance(w[1], Rect) else Rect(*[float(v) for v in w[1]])))
            d["widgets"] = ws
        for k in ("border", "background"):
            if isinstance(d.get(k), list):
                d[k] = tuple(d[k])
        known = {f for f in cls.__dataclass_fields__}
        bad = set(d) - known
        if bad:
            raise ValueError(f"unknown field-spec key(s): {', '.join(sorted(bad))}")
        return cls(**d)


def _ensure_acroform(pdf: pikepdf.Pdf) -> Dictionary:
    af = pdf.Root.get("/AcroForm")
    if af is None:
        af = pdf.make_indirect(Dictionary(Fields=Array()))
        pdf.Root.AcroForm = af
    if "/Fields" not in af:
        af.Fields = Array()
    dr = af.get("/DR")
    if dr is None:
        af.DR = Dictionary()
        dr = af.DR
    if "/Font" not in dr:
        dr.Font = Dictionary()
    if "/Helv" not in dr.Font:
        dr.Font.Helv = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                                                    Encoding=Name.WinAnsiEncoding))
    if "/ZaDb" not in dr.Font:
        dr.Font.ZaDb = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.ZapfDingbats))
    if "/DA" not in af:
        af.DA = String("/Helv 0 Tf 0 g")
    return af


def _pdf_rect(pdf: pikepdf.Pdf, page: int, r: Rect) -> Array:
    mb = [float(v) for v in pdf.pages[page].mediabox]
    x0, y0, y1 = min(mb[0], mb[2]), min(mb[1], mb[3]), max(mb[1], mb[3])
    llx, lly, urx, ury = r.to_pdf(y1 - y0)
    return Array([round(llx + x0, 3), round(lly + y0, 3), round(urx + x0, 3), round(ury + y0, 3)])


def _mk(spec: FieldSpec, extra: dict | None = None) -> Dictionary:
    mk = Dictionary()
    if spec.border is not None:
        mk.BC = Array(list(spec.border))
    if spec.background is not None:
        mk.BG = Array(list(spec.background))
    for k, v in (extra or {}).items():
        mk[k] = v
    return mk


def _widget(pdf: pikepdf.Pdf, spec: FieldSpec, page: int, rect: Rect) -> Dictionary:
    w = Dictionary(Type=Name.Annot, Subtype=Name.Widget, Rect=_pdf_rect(pdf, page, rect), F=4,
                   P=pdf.pages[page].obj, BS=Dictionary(W=1 if spec.border is not None else 0, S=Name.S))
    return w


def _add_annot(pdf: pikepdf.Pdf, page: int, annot) -> None:
    p = pdf.pages[page].obj
    if "/Annots" not in p:
        p.Annots = Array()
    p.Annots.append(annot)


def _create(pdf: pikepdf.Pdf, spec: FieldSpec, existing: set[str]) -> pikepdf.Object:
    if not spec.name or spec.name in existing:
        raise ValueError(f"field name {spec.name!r} is empty or already used")
    if not (0 <= spec.page < len(pdf.pages)):
        raise ValueError(f"field {spec.name!r}: page {spec.page} out of range")
    t = spec.type.lower()
    ff = (REQUIRED if spec.required else 0) | (READONLY if spec.read_only else 0)
    da = String(f"/Helv {spec.font_size:g} Tf 0 g")
    if t == "radio":
        if not spec.widgets:
            raise ValueError(f"radio {spec.name!r} needs widgets=[(value, rect), …]")
        parent = pdf.make_indirect(Dictionary(FT=Name.Btn, Ff=ff | RADIO | NOTOGGLEOFF, T=String(spec.name), Kids=Array(),
                                              V=Name.Off, DA=String("/ZaDb 0 Tf 0 g")))
        for val, r in spec.widgets:
            w = _widget(pdf, spec, spec.page, r)
            w.MK = _mk(spec, {"/CA": String("l")})
            w.Parent = parent
            w.AS = Name.Off
            # placeholder /AP so the on-state name is known; real streams come from _refresh
            w.AP = Dictionary(N=Dictionary({f"/{val}": pdf.make_stream(b""), "/Off": pdf.make_stream(b"")}))
            w = pdf.make_indirect(w)
            parent.Kids.append(w)
            _add_annot(pdf, spec.page, w)
        if spec.tooltip:
            parent.TU = String(spec.tooltip)
        node = parent
    else:
        if spec.rect is None:
            raise ValueError(f"field {spec.name!r} needs a rect")
        w = _widget(pdf, spec, spec.page, spec.rect)
        w.T = String(spec.name)
        w.MK = _mk(spec)
        if t in ("text", "multiline", "password"):
            w.FT = Name.Tx
            w.Ff = ff | (MULTILINE if t == "multiline" else 0) | (PASSWORD if t == "password" else 0)
            w.DA = da
            w.V = String("")
            if spec.align:
                w.Q = spec.align
            if spec.max_len:
                w.MaxLen = spec.max_len
        elif t == "checkbox":
            w.FT = Name.Btn
            w.Ff = ff
            w.DA = String("/ZaDb 0 Tf 0 g")
            w.MK = _mk(spec, {"/CA": String("4")})
            w.V = Name.Off
            w.AS = Name.Off
            w.AP = Dictionary(N=Dictionary({"/Yes": pdf.make_stream(b""), "/Off": pdf.make_stream(b"")}))
        elif t in ("choice", "dropdown", "combo", "list", "listbox"):
            w.FT = Name.Ch
            combo = t not in ("list", "listbox")
            w.Ff = ff | (COMBO if combo else 0) | (EDIT if spec.editable and combo else 0) | (MULTISELECT if spec.multi_select and not combo else 0)
            w.DA = da
            opts = Array()
            for o in spec.options:
                if isinstance(o, (list, tuple)):
                    opts.append(Array([String(str(o[0])), String(str(o[1]))]))
                else:
                    opts.append(String(str(o)))
            w.Opt = opts
            w.V = String("")
        elif t == "signature":
            w.FT = Name.Sig
            w.Ff = ff
        else:
            raise ValueError(f"unknown field type {spec.type!r}")
        if spec.tooltip:
            w.TU = String(spec.tooltip)
        node = pdf.make_indirect(w)
        _add_annot(pdf, spec.page, node)
    pdf.Root.AcroForm.Fields.append(node)
    existing.add(spec.name)
    return node


def _add_specs(pdf: pikepdf.Pdf, specs: Sequence[FieldSpec]) -> list[str]:
    _ensure_acroform(pdf)
    existing = {f.name for f in _walk_fields(pdf)}
    names = []
    for s in specs:
        _create(pdf, s, existing)
        names.append(s.name)
    fields = {f.name: f for f in _walk_fields(pdf)}
    new = [fields[n] for n in names]
    vals = {s.name: s.value for s in specs if s.value not in (None, "")}
    for n, v in vals.items():
        _set_value(fields[n], v)
    _refresh(pdf, new)
    return names


def add_fields(
    src: str | Path,
    specs: Sequence[FieldSpec | Mapping[str, Any]] | str | Path,
    *,
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Add fields (text, multiline, password, checkbox, radio group, dropdown, list, signature) at Rects."""
    src = Path(src)
    if isinstance(specs, (str, Path)):
        raw = json.loads(Path(specs).read_text(encoding="utf-8")) if Path(str(specs)).exists() else json.loads(str(specs))
        specs = raw["fields"] if isinstance(raw, Mapping) else raw
    sp = [s if isinstance(s, FieldSpec) else FieldSpec.from_dict(s) for s in specs]
    dst = output_path(src, "fields", out=out)
    with pikepdf.open(str(src), password=password or "") as pdf:
        names = _add_specs(pdf, sp)
        pdf.save(str(dst))
    return finalize(dst, baseline=src, options=options, password=password, data={"added": names}, quiet=quiet)


# ====================================================================== auto-detect
def _slug(label: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", label.strip().rstrip(":").lower()).strip("_")
    return s[:48] or "field"


def _same_line(words: list[dict], y_mid: float, tol: float) -> list[dict]:
    return [w for w in words if abs((w["top"] + w["bottom"]) / 2 - y_mid) <= tol]


def _label_left(words: list[dict], x0: float, baseline: float, max_gap: float = 160) -> str:
    """Words ending just left of x0 whose bottom sits near *baseline* (top-left coords)."""
    cand = [w for w in words if abs(w["bottom"] - baseline) <= 7 and w["x1"] <= x0 + 2 and x0 - w["x1"] <= max_gap]
    if not cand:
        return ""
    cand.sort(key=lambda w: w["x0"])
    # keep the contiguous run that ends nearest the line
    run = [cand[-1]]
    for w in reversed(cand[:-1]):
        if run[0]["x0"] - w["x1"] <= max(6.0, (w["bottom"] - w["top"]) * 1.2):
            run.insert(0, w)
        else:
            break
    return " ".join(w["text"] for w in run)


def _label_above(words: list[dict], r: Rect, reach: float = 22) -> str:
    cand = [w for w in words if r.top - reach <= w["bottom"] <= r.top + 1 and w["x1"] > r.x0 - 4 and w["x0"] < r.x1]
    if not cand:
        return ""
    bottom = max(w["bottom"] for w in cand)
    ln = sorted([w for w in cand if abs(w["bottom"] - bottom) < 3], key=lambda w: w["x0"])
    return " ".join(w["text"] for w in ln)


def _label_right(words: list[dict], r: Rect, reach: float = 30) -> str:
    mid = (r.top + r.bottom) / 2
    cand = sorted([w for w in words if w["x0"] >= r.x1 - 1 and w["x0"] - r.x1 <= 400 and abs((w["top"] + w["bottom"]) / 2 - mid) <= max(4, r.height * 0.6)],
                  key=lambda w: w["x0"])
    if not cand or cand[0]["x0"] - r.x1 > reach:
        return ""
    run = [cand[0]]
    for w in cand[1:]:
        if w["x0"] - run[-1]["x1"] <= max(6.0, (w["bottom"] - w["top"]) * 1.2):
            run.append(w)
        else:
            break
    return " ".join(w["text"] for w in run)


def _chars_in(chars: list[dict], r: Rect) -> int:
    return sum(1 for c in chars if c["text"].strip() and c["text"] != "_" and r.contains_point((c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2))


def _detect_page(page, pi: int, taken: list[Rect]) -> list[FieldSpec]:
    W, H = float(page.width), float(page.height)
    words = page.extract_words(keep_blank_chars=False, extra_attrs=["size"])
    chars = page.chars
    plain = [w for w in words if not re.fullmatch(r"_+", w["text"])]
    for w in plain:  # strip trailing underscore runs glued to a label ("Name:_____")
        w["text"] = w["text"].rstrip("_")
    plain = [w for w in plain if w["text"]]
    specs: list[FieldSpec] = []
    used: set[str] = set()

    def name_for(label: str, fallback: str) -> str:
        base = _slug(label) if label else fallback
        n, k = base, 2
        while n in used:
            n = f"{base}_{k}"
            k += 1
        used.add(n)
        return n

    def free(r: Rect) -> bool:
        return not any(t.overlap_fraction(r) > 0.3 or r.overlap_fraction(t) > 0.3 for t in taken)

    # --- horizontal rules: vector lines, thin filled rects, underscore runs
    rules: list[tuple[float, float, float, float]] = []  # x0, x1, y, text size hint
    for ln in page.lines:
        if abs(float(ln["top"]) - float(ln["bottom"])) < 1.0 and float(ln["x1"]) - float(ln["x0"]) >= 36:
            rules.append((float(ln["x0"]), float(ln["x1"]), float(ln["bottom"]), 0.0))
    for r in page.rects:
        h = float(r["bottom"]) - float(r["top"])
        if h < 1.5 and float(r["x1"]) - float(r["x0"]) >= 36:
            rules.append((float(r["x0"]), float(r["x1"]), float(r["bottom"]), 0.0))
    us = sorted([c for c in chars if c["text"] == "_"], key=lambda c: (round(c["top"]), c["x0"]))
    run: list[dict] = []
    for c in us + [None]:
        if c is not None and run and abs(c["top"] - run[-1]["top"]) < 1 and c["x0"] - run[-1]["x1"] < 1.5:
            run.append(c)
            continue
        if len(run) >= 3 and run[-1]["x1"] - run[0]["x0"] >= 30:
            rules.append((run[0]["x0"], run[-1]["x1"], run[0]["bottom"] - 1.0, float(run[0].get("size", 0))))
        run = [c] if c is not None else []
    big_rects = [Rect(float(r["x0"]), float(r["top"]), float(r["x1"]), float(r["bottom"])) for r in page.rects]
    for x0, x1, y, sz in sorted(rules, key=lambda t: (t[2], t[0])):
        if x1 - x0 > 0.97 * W:
            continue
        # skip edges of drawn boxes
        if any(abs(y - b.top) < 1.5 or abs(y - b.bottom) < 1.5 for b in big_rects if b.height > 4 and b.x0 - 1 <= x0 and x1 <= b.x1 + 1):
            continue
        # must be blank: no text sitting on it
        probe = Rect(x0 + 1, y - 14, x1 - 1, y - 0.5)
        if _chars_in(chars, probe) > 0:
            continue
        label = _label_left(plain, x0, y)
        if not label:
            near = _same_line(plain, y - 5, 7)
            label = _label_left(near, x0, max((w["bottom"] for w in near), default=y)) if near else ""
        if not label:
            label = _label_above(plain, Rect(x0, y - 16, x1, y), reach=16)
        if not label:
            continue
        lab_words = [w for w in plain if abs(w["bottom"] - y) <= 7]
        size = max((float(w.get("size", 0)) for w in lab_words), default=sz or 10.0) or 10.0
        h = min(max(size * 1.6, 13.0), 22.0)
        r = Rect(x0, y - h, x1, y - 0.5)
        if not free(r):
            continue
        specs.append(FieldSpec(name=name_for(label, f"text_{pi + 1}_{len(specs) + 1}"), type="text", page=pi, rect=r,
                               tooltip=label.rstrip(":"), border=None, font_size=round(min(size, h * 0.7), 1)))
        taken.append(r)

    # --- boxes: small squares → checkboxes, large empty rectangles → multiline text
    for r in page.rects:
        b = Rect(float(r["x0"]), float(r["top"]), float(r["x1"]), float(r["bottom"]))
        if not r.get("stroke", True) and r.get("fill"):
            continue
        if 6 <= b.width <= 22 and 6 <= b.height <= 22 and abs(b.width - b.height) <= 2.5:
            if _chars_in(chars, b) or not free(b):
                continue
            label = _label_right(plain, b) or _label_left(plain, b.x0, b.bottom, max_gap=40)
            specs.append(FieldSpec(name=name_for(label, f"check_{pi + 1}_{len(specs) + 1}"), type="checkbox", page=pi,
                                   rect=b.expand(-0.5), tooltip=label, border=None))
            taken.append(b)
        elif b.width >= 72 and b.height >= 36 and b.area < 0.6 * W * H:
            inner = b.expand(-2)
            if _chars_in(chars, inner) or not free(inner):
                continue
            label = _label_above(plain, b) or _label_left(plain, b.x0, b.top + 12)
            specs.append(FieldSpec(name=name_for(label, f"notes_{pi + 1}_{len(specs) + 1}"), type="multiline", page=pi,
                                   rect=inner, tooltip=label.rstrip(":"), border=None, font_size=10))
            taken.append(inner)
    return specs


def detect_fields(
    src: str | Path,
    *,
    out: str | Path | None = None,
    pages: str | None = None,
    dry_run: bool = False,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Find blank underlines / underscore runs (→ text), small squares (→ checkbox), large empty boxes
    (→ multiline) on a flat form and write a fillable copy. ``data['fields']`` lists what was found."""
    import pdfplumber

    src = Path(src)
    with pikepdf.open(str(src), password=password or "") as pdf:
        pidx = _page_index(pdf)
        taken_by_page: dict[int, list[Rect]] = {}
        for f in _walk_fields(pdf):
            for w in _info(pdf, f, pidx).widgets:
                taken_by_page.setdefault(w.page, []).append(w.rect)
        n = len(pdf.pages)
    sel = parse_pages(pages, n)
    specs: list[FieldSpec] = []
    with pdfplumber.open(str(src), password=password or "") as doc:
        for pi in sel:
            specs += _detect_page(doc.pages[pi], pi, taken_by_page.setdefault(pi, []))
    # names must be unique across pages
    seen: dict[str, int] = {}
    for s in specs:
        if s.name in seen:
            seen[s.name] += 1
            s.name = f"{s.name}_{seen[s.name]}"
        else:
            seen[s.name] = 1
    found = [{"name": s.name, "type": s.type, "page": s.page, "rect": _rect_list(s.rect), "label": s.tooltip} for s in specs if s.rect]
    if dry_run or not specs:
        if not quiet:
            print(f"{len(found)} field(s) detected" + ("" if specs else " — nothing written"))
        return OpResult(None, None, data={"fields": found})
    dst = output_path(src, "fillable", out=out)
    with pikepdf.open(str(src), password=password or "") as pdf:
        _add_specs(pdf, specs)
        pdf.save(str(dst))
    return finalize(dst, baseline=src, options=options, password=password, data={"fields": found}, quiet=quiet)


# ====================================================================== flatten
def _ap_for(widget):
    ap = widget.get("/AP")
    if ap is None or "/N" not in ap:
        return None
    n = ap.N
    if isinstance(n, pikepdf.Stream):
        return n
    st = widget.get("/AS")
    if st is None or st not in n:
        return None
    return n[st]


def _bbox_matrix_fit(x, rect) -> tuple[float, float, float, float]:
    """(sx, sy, e, f) mapping the Matrix-transformed BBox of form *x* onto *rect* (spec §12.5.5)."""
    bx0, by0, bx1, by1 = [float(v) for v in x.get("/BBox", [0, 0, 1, 1])]
    m = [float(v) for v in x.get("/Matrix", [1, 0, 0, 1, 0, 0])]
    pts = [(bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1)]
    tx = [m[0] * px + m[2] * py + m[4] for px, py in pts]
    ty = [m[1] * px + m[3] * py + m[5] for px, py in pts]
    tx0, tx1, ty0, ty1 = min(tx), max(tx), min(ty), max(ty)
    rx0, ry0, rx1, ry1 = [float(v) for v in rect]
    rx0, rx1 = sorted((rx0, rx1))
    ry0, ry1 = sorted((ry0, ry1))
    sx = (rx1 - rx0) / (tx1 - tx0) if tx1 > tx0 else 1.0
    sy = (ry1 - ry0) / (ty1 - ty0) if ty1 > ty0 else 1.0
    return sx, sy, rx0 - tx0 * sx, ry0 - ty0 * sy


def _flatten_pdf(pdf: pikepdf.Pdf) -> int:
    """Bake every widget's current appearance into page content; remove widgets and the AcroForm."""
    fields = list(_walk_fields(pdf))
    stale = [f for f in fields if f.kind != "button" and any(_needs_refresh(w) for w in f.widgets)]
    if stale:
        _refresh(pdf, stale)
    count = 0
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not annots:
            continue
        keep = Array()
        ops: list[str] = []
        res = page.obj.get("/Resources")
        if res is None:
            page.obj.Resources = Dictionary()
            res = page.obj.Resources
        if "/XObject" not in res:
            res.XObject = Dictionary()
        for a in annots:
            if a.get("/Subtype") != "/Widget":
                keep.append(a)
                continue
            ap = _ap_for(a)
            if ap is None or _hide_widget_ap(a):
                continue
            if "/Subtype" not in ap:
                ap.Subtype = Name.Form
            if "/Type" not in ap:
                ap.Type = Name.XObject
            k = 0
            while Name(f"/Fld{k}") in res.XObject:
                k += 1
            nm = f"/Fld{k}"
            res.XObject[nm] = ap
            sx, sy, e, f = _bbox_matrix_fit(ap, a.Rect)
            ops.append(f"q {sx:.6g} 0 0 {sy:.6g} {e:.4f} {f:.4f} cm {nm} Do Q")
            count += 1
        if ops:
            page.contents_coalesce()
            data = page.obj.Contents.read_bytes() if "/Contents" in page.obj else b""
            page.obj.Contents = pdf.make_stream(b"q\n" + data + b"\nQ\n" + "\n".join(ops).encode() + b"\n")
        if len(keep):
            page.obj.Annots = keep
        else:
            del page.obj["/Annots"]
    if "/AcroForm" in pdf.Root:
        del pdf.Root["/AcroForm"]
    return count


def flatten(src: str | Path, *, out: str | Path | None = None, password: str | None = None, options: GateOptions | None = None,
            quiet: bool = False) -> OpResult:
    """Bake field appearances into the page and remove the form (values stay visible, no longer editable)."""
    src = Path(src)
    dst = output_path(src, "flat", out=out)
    with pikepdf.open(str(src), password=password or "") as pdf:
        n = _flatten_pdf(pdf)
        pdf.save(str(dst))
    return finalize(dst, baseline=src, options=options, password=password, data={"flattened": n}, quiet=quiet)


# ====================================================================== bulk fill
_BAD_FN = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _fname(template: str, row: Mapping[str, Any], n: int) -> str:
    safe = {k: _BAD_FN.sub("_", str(v)).strip() for k, v in row.items() if k is not None}
    try:
        name = template.format_map({**safe, "n": n, "row": n})
    except KeyError as exc:
        raise KeyError(f"name template uses {exc} which is not a CSV column") from exc
    name = name.strip() or f"row-{n}.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def fill_from_csv(
    src: str | Path,
    csv_path: str | Path,
    *,
    name: str = "{n:03d}.pdf",
    out_dir: str | Path | None = None,
    merged: str | Path | bool | None = None,
    flatten_each: bool = False,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """One filled PDF per CSV row (columns = field names). ``name`` is a format template over the row's
    columns plus ``{n}`` (1-based row number), e.g. "{legal_name}.pdf". ``merged`` also writes all rows,
    flattened, into one PDF (True → <src>.merged.pdf next to the outputs)."""
    src = Path(src)
    out_dir = Path(out_dir) if out_dir else src.parent / f"{src.stem}.filled"
    out_dir.mkdir(parents=True, exist_ok=True)
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{csv_path} has no data rows")
    outputs: list[Path] = []
    flats: list[Path] = []
    results = []
    failures = 0
    for n, row in enumerate(rows, 1):
        target = out_dir / _fname(name, row, n)
        if target.resolve() == src.resolve():
            raise ValueError("refusing to overwrite the input file")
        k = 2
        while target.exists() or target in outputs:
            target = target.with_name(f"{target.stem}-{k}.pdf")
            k += 1
        values = {c: v for c, v in row.items() if c is not None and c.strip()}
        r = fill(src, values, out=target, flatten_after=flatten_each, password=password, options=options, quiet=quiet)
        outputs.append(target)
        failures += 0 if r.ok else 1
        results.append({"row": n, "output": str(target), "gate": r.line(), "unknown": r.data.get("unknown", [])})
    extra = list(outputs)
    merged_path: Path | None = None
    if merged:
        merged_path = Path(merged) if not isinstance(merged, bool) else output_path(src, "merged", out=out_dir)
        with pikepdf.new() as mpdf:
            for p in outputs:
                with pikepdf.open(str(p)) as one:
                    if not flatten_each:
                        _flatten_pdf(one)
                    buf = io.BytesIO()
                    one.save(buf)
                buf.seek(0)
                part = pikepdf.open(buf)
                mpdf.pages.extend(part.pages)
            with pikepdf.open(str(src), password=password or "") as s:
                if s.docinfo is not None and "/Title" in s.docinfo:
                    mpdf.docinfo["/Title"] = s.docinfo["/Title"]
            mpdf.save(str(merged_path))
    data = {"rows": len(rows), "outputs": [str(p) for p in outputs], "results": results, "failed": failures}
    if merged_path:
        data["merged"] = str(merged_path)
        return finalize(merged_path, baseline=src, options=options, password=password, extra=extra, data=data, quiet=quiet)
    first = outputs[0]
    res = OpResult(first, None, extra[1:], data)
    # report the worst per-row gate as the op's gate
    from pdfcore.gate import run_gate

    res.gate = run_gate(first, baseline=src, options=options, password=password)
    if failures and res.gate.passed:
        bad = next(r for r in results if not r["gate"].startswith("PASS"))
        res.gate = run_gate(Path(bad["output"]), baseline=src, options=options, password=password)
    return res


# ====================================================================== export
def _xfdf(values: Mapping[str, Any], href: str) -> str:
    ns = "http://ns.adobe.com/xfdf/"
    root = ET.Element("xfdf", {"xmlns": ns, "xml:space": "preserve"})
    ET.SubElement(root, "f", {"href": href})
    fields_el = ET.SubElement(root, "fields")
    nodes: dict[str, ET.Element] = {}
    for name, val in values.items():
        parent, path = fields_el, ""
        parts = name.split(".")
        for k, part in enumerate(parts):
            path = f"{path}.{part}" if path else part
            el = nodes.get(path)
            if el is None:
                el = ET.SubElement(parent, "field", {"name": part})
                nodes[path] = el
            parent = el
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            ET.SubElement(parent, "value").text = "" if v is None else str(v)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def export_data(src: str | Path, *, fmt: str = "json", out: str | Path | None = None, password: str | None = None,
                quiet: bool = False) -> OpResult:
    """Write field values as JSON ({name: value}), CSV (header + one row) or XFDF."""
    src = Path(src)
    fmt = fmt.lower()
    if fmt not in ("json", "csv", "xfdf"):
        raise ValueError("format must be json, csv or xfdf")
    infos = [f for f in list_fields(src, password=password) if f.type not in ("button", "signature")]
    values = {f.name: f.value for f in infos}
    dst = output_path(src, "data", ext=f".{fmt}", out=out)
    if fmt == "json":
        dst.write_text(json.dumps(values, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elif fmt == "csv":
        with dst.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(list(values))
            w.writerow(["|".join(v) if isinstance(v, list) else ("" if v is None else v) for v in values.values()])
    else:
        dst.write_text(_xfdf(values, src.name), encoding="utf-8")
    if not quiet:
        print(f"{dst}  {len(values)} field(s)")
    return OpResult(dst, None, data={"values": values})


# ====================================================================== CLI
def _kv(items: Sequence[str] | None) -> dict[str, str]:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"--set expects name=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


@command("form-list", "List form fields (name, type, value, options, rect, page, flags)", group="forms")
def _cfg_list(p):
    p.add_argument("pdf")
    p.add_argument("--json", action="store_true")
    p.add_argument("--password")

    def run(ns):
        infos = list_fields(ns.pdf, password=ns.password)
        if ns.json:
            print(json.dumps([i.as_dict() for i in infos], indent=2, ensure_ascii=False))
        else:
            for i in infos:
                opts = f"  options={i.options}" if i.options else ""
                fl = f"  [{', '.join(i.flags)}]" if i.flags else ""
                print(f"{i.name:<28} {i.type:<9} p.{(i.page or 0) + 1}  value={i.value!r}{opts}{fl}")
            if not infos:
                print("no form fields")
        return None

    return run


@command("form-fill", "Fill form fields from JSON / XFDF / name=value (appearances in an embedded font)", group="forms")
def _cfg_fill(p):
    p.add_argument("pdf")
    p.add_argument("--data", help="JSON file or string, .xfdf, or .csv (first row)")
    p.add_argument("--set", action="append", metavar="NAME=VALUE", help="set one field (repeatable)")
    p.add_argument("--flatten", action="store_true", help="flatten after filling")
    p.add_argument("--strict", action="store_true", help="error on unknown field names")
    add_common(p)

    def run(ns):
        vals: dict[str, Any] = _load_values(ns.data) if ns.data else {}
        vals.update(_kv(ns.set))
        if not vals:
            raise ValueError("nothing to fill: pass --data or --set")
        return fill(ns.pdf, vals, out=ns.out, flatten_after=ns.flatten, strict=ns.strict, password=ns.password,
                    options=gate_options_from(ns))

    return run


@command("form-create", "Add form fields at given rectangles from a JSON spec", group="forms")
def _cfg_create(p):
    p.add_argument("pdf")
    p.add_argument("--spec", required=True, help='JSON file/string: [{"name","type","page","rect":[x0,top,x1,bottom],…}]')
    add_common(p)

    def run(ns):
        return add_fields(ns.pdf, ns.spec, out=ns.out, password=ns.password, options=gate_options_from(ns))

    return run


@command("form-detect", "Detect fields on a flat form (underlines, boxes) and make it fillable", group="forms")
def _cfg_detect(p):
    p.add_argument("pdf")
    p.add_argument("--pages")
    p.add_argument("--dry-run", action="store_true", help="only list what would be created")
    add_common(p)

    def run(ns):
        res = detect_fields(ns.pdf, out=ns.out, pages=ns.pages, dry_run=ns.dry_run, password=ns.password,
                            options=gate_options_from(ns))
        for f in res.data["fields"]:
            print(f"  {f['type']:<9} {f['name']:<28} p.{f['page'] + 1}  {f['rect']}")
        return res

    return run


@command("form-bulk", "Fill a form once per CSV row (column = field name)", group="forms")
def _cfg_bulk(p):
    p.add_argument("pdf")
    p.add_argument("csv")
    p.add_argument("--name", default="{n:03d}.pdf", help='output name template, e.g. "{legal_name}.pdf"')
    p.add_argument("--out-dir", help="directory for the outputs (default <name>.filled/ next to the input)")
    p.add_argument("--merged", nargs="?", const=True, default=None, help="also write one merged (flattened) PDF")
    p.add_argument("--flatten", action="store_true", help="flatten each output")
    add_common(p, out=False)

    def run(ns):
        res = fill_from_csv(ns.pdf, ns.csv, name=ns.name, out_dir=ns.out_dir, merged=ns.merged, flatten_each=ns.flatten,
                            password=ns.password, options=gate_options_from(ns))
        print(f"{res.data['rows']} file(s) in {Path(res.data['outputs'][0]).parent}" + (f"; merged → {res.data['merged']}" if res.data.get("merged") else ""))
        return res

    return run


@command("form-flatten", "Flatten form fields into page content", group="forms")
def _cfg_flatten(p):
    p.add_argument("pdf")
    add_common(p)

    def run(ns):
        return flatten(ns.pdf, out=ns.out, password=ns.password, options=gate_options_from(ns))

    return run


@command("form-export", "Export field values to JSON, CSV or XFDF", group="forms")
def _cfg_export(p):
    p.add_argument("pdf")
    p.add_argument("--format", "-f", default="json", choices=["json", "csv", "xfdf"])
    p.add_argument("-o", "--out")
    p.add_argument("--password")

    def run(ns):
        export_data(ns.pdf, fmt=ns.format, out=ns.out, password=ns.password)
        return None

    return run
