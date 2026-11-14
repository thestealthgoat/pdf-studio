"""Stamping: headers/footers, page numbers, Bates numbering, watermarks and approval stamps.

Everything is drawn through :func:`pdfcore.overlay.stamp_pages`, so positions are in *visual*
coordinates (what the reader sees) and land correctly on rotated pages and offset CropBoxes.
Text uses the embedded house fonts (Inter / Source Serif 4).

Library::

    from pdfcore.ops import stamp
    stamp.page_numbers("report.pdf")                                  # "Page X of Y", footer centre
    stamp.header_footer("a.pdf", {"header-right": "{title}", "footer-left": "{file} · {date}"})
    res = stamp.bates(["a.pdf", "b.pdf"], prefix="ABC", start=1, digits=6)   # res.data["next"]
    stamp.watermark("a.pdf", "DRAFT", opacity=0.15)
    stamp.approval_stamp("sheet.pdf", "APPROVED", name="J. Smith", date="today")
"""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any, Mapping, Sequence

import pikepdf

from pdfcore.gate import GateOptions, GateReport
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.overlay import stamp_pages
from pdfcore.result import OpResult

__all__ = [
    "TextStyle", "BatesSpec", "WatermarkSpec", "ApprovalSpec", "POSITIONS",
    "stamp", "header_footer", "page_numbers", "bates", "watermark", "approval_stamp",
    "format_tokens", "parse_length",
]

POSITIONS = ("header-left", "header-center", "header-right", "footer-left", "footer-center", "footer-right")
_POS_ALIASES = {
    "tl": "header-left", "tc": "header-center", "tr": "header-right",
    "bl": "footer-left", "bc": "footer-center", "br": "footer-right",
    "top-left": "header-left", "top-center": "header-center", "top-right": "header-right",
    "bottom-left": "footer-left", "bottom-center": "footer-center", "bottom-right": "footer-right",
    "header": "header-center", "footer": "footer-center",
}
_FONT_ROLES = ("sans", "sans-bold", "sans-semibold", "sans-italic", "serif", "serif-bold", "serif-italic")

APPROVAL_COLORS = {
    "APPROVED": "#1B7F3B",
    "REVIEWED": "#1F5FA8",
    "REJECTED": "#C62828",
    "ISSUED FOR CONSTRUCTION": "#5B2C83",
    "VOID": "#C62828",
    "DRAFT": "#6D6D6D",
}
STAMP_POSITIONS = ("top-left", "top-center", "top-right", "center", "bottom-left", "bottom-center", "bottom-right")


# ------------------------------------------------------------------ specs
@dataclass
class TextStyle:
    """Look of header/footer/Bates text. Lengths in points."""

    font: str = "sans"  # house-font role: sans, sans-bold, serif, …
    size: float = 9.0
    color: str = "#222222"
    margin_y: float = 27.0  # page edge → outer edge of the text line (top for headers, bottom for footers)
    margin_x: float = 36.0  # page edge → left/right text


@dataclass
class BatesSpec:
    prefix: str = ""
    start: int = 1
    digits: int = 6
    suffix: str = ""
    position: str = "footer-right"
    pattern: str | None = None  # e.g. "BAR-{n:06d}"; overrides prefix/digits/suffix
    style: TextStyle | None = None  # default: bold-ish 9 pt black

    def label(self, n: int) -> str:
        if self.pattern:
            return format_tokens(self.pattern, {"n": n, "bates": n})
        return f"{self.prefix}{n:0{max(1, self.digits)}d}{self.suffix}"


@dataclass
class WatermarkSpec:
    text: str | None = None
    image: str | Path | None = None
    angle: str | float = "diagonal"  # "diagonal" (page diagonal, bottom-left → top-right), "horizontal" or degrees
    opacity: float = 0.18
    color: str = "#7F7F7F"
    size: float | None = None  # pt; None = fit to the page diagonal
    fill: float = 0.8  # fraction of the diagonal (or width) the auto-fitted text spans
    font: str = "sans-bold"
    behind: bool = False  # draw underneath page content
    scale: float = 0.5  # image width as a fraction of page width


@dataclass
class ApprovalSpec:
    label: str = "APPROVED"
    name: str | None = None
    date: str | None = None  # "today" → ISO date; any other string is printed as given
    color: str | None = None  # default by label (APPROVED green, REJECTED red, …)
    rotation: float = 0.0  # degrees, counter-clockwise
    position: str = "top-right"
    at: tuple[float, float] | None = None  # lower-left of the (unrotated) box, visual points
    scale: float | None = None  # None = auto (1.0 on letter/A4, larger on big sheets)
    opacity: float = 0.9
    margin: float = 36.0
    date_format: str = "%Y-%m-%d"


# ------------------------------------------------------------------ helpers
_TOKEN = re.compile(r"\{(\w+)(?::([^{}]*))?\}")


def format_tokens(text: str, ctx: Mapping[str, Any]) -> str:
    """Replace ``{key}`` / ``{key:spec}`` for keys in *ctx*; leave unknown braces untouched."""

    def sub(m: re.Match) -> str:
        key, spec = m.group(1), m.group(2)
        if key not in ctx:
            return m.group(0)
        v = ctx[key]
        try:
            return format(v, spec) if spec else str(v)
        except (ValueError, TypeError):
            return str(v)

    return _TOKEN.sub(sub, text)


_LEN = re.compile(r"^\s*(-?[\d.]+)\s*(pt|in|mm|cm)?\s*$", re.I)


def parse_length(v: str | float | int) -> float:
    """'0.5in', '12mm', '1cm', '36pt' or a bare number (points) → points."""
    if isinstance(v, (int, float)):
        return float(v)
    m = _LEN.match(str(v))
    if not m:
        raise ValueError(f"bad length {v!r} (use e.g. 36, 0.5in, 12mm)")
    x = float(m.group(1))
    unit = (m.group(2) or "pt").lower()
    return x * {"pt": 1.0, "in": 72.0, "mm": 72 / 25.4, "cm": 72 / 2.54}[unit]


def _norm_pos(p: str) -> str:
    q = p.strip().lower().replace("_", "-").replace("centre", "center")
    q = _POS_ALIASES.get(q, q)
    if q not in POSITIONS:
        raise ValueError(f"unknown position {p!r}; use one of {', '.join(POSITIONS)}")
    return q


def _font(role: str) -> str:
    from pdfcore.fonts import register_reportlab

    names = register_reportlab()
    if role in names:
        return names[role]
    if role in names.values():
        return role
    raise ValueError(f"unknown font {role!r}; use one of {', '.join(_FONT_ROLES)}")


def _color(spec: str):
    from reportlab.lib import colors

    s = spec.strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", s):
        return colors.HexColor(s if s.startswith("#") else "#" + s)
    if re.fullmatch(r"[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+", s):
        r, g, b = (float(t) for t in s.split(","))
        if max(r, g, b) > 1:
            r, g, b = r / 255, g / 255, b / 255
        return colors.Color(r, g, b)
    try:
        return colors.toColor(s)
    except Exception as exc:
        raise ValueError(f"bad color {spec!r} (use #RRGGBB, r,g,b or a color name)") from exc


def _title(pdf: pikepdf.Pdf) -> str:
    try:
        t = str(pdf.docinfo.get("/Title", "") or "")
        if t:
            return t
        with pdf.open_metadata(set_pikepdf_as_editor=False) as m:
            return str(m.get("dc:title", "") or "")
    except Exception:
        return ""


def _label(pdf: pikepdf.Pdf, i: int) -> str:
    try:
        return str(pdf.pages[i].label)
    except Exception:
        return str(i + 1)


def _open(src: Path, password: str | None) -> pikepdf.Pdf:
    return pikepdf.open(str(src), password=password or "")


def _save(pdf: pikepdf.Pdf, out: Path) -> None:
    # Keep the input's encryption (if any) rather than silently dropping it.
    pdf.save(str(out), encryption=True if pdf.is_encrypted else None, object_stream_mode=pikepdf.ObjectStreamMode.generate)


def _string_width(text: str, font: str, size: float) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    return stringWidth(text, font, size)


def _ascent_descent(font: str, size: float) -> tuple[float, float]:
    from reportlab.pdfbase.pdfmetrics import getAscentDescent

    a, d = getAscentDescent(font, size)
    return a, abs(d)


# ------------------------------------------------------------------ drawing
def _draw_band(c, texts: dict[str, str], band: str, w: float, h: float, st: TextStyle) -> None:
    """Draw left/center/right texts of one band ('header' or 'footer'), shrinking to fit the width."""
    font = _font(st.font)
    slots = {k.split("-", 1)[1]: v for k, v in texts.items() if k.startswith(band) and v}
    if not slots:
        return
    avail = max(10.0, w - 2 * st.margin_x)
    gap = max(6.0, st.size)
    widths = {k: _string_width(v, font, st.size) for k, v in slots.items()}
    # Left/right must clear a centred text: need 2*max(side) + centre + gaps ≤ avail.
    side = max(widths.get("left", 0), widths.get("right", 0))
    if "center" in widths and side:
        need = widths["center"] + 2 * side + 2 * gap
    else:
        need = sum(widths.values()) + gap * (len(widths) - 1)
    size = st.size * min(1.0, avail / need) if need > 0 else st.size
    a, d = _ascent_descent(font, size)
    y = h - st.margin_y - a if band == "header" else st.margin_y + d
    c.saveState()
    c.setFillColor(_color(st.color))
    c.setFont(font, size)
    if "left" in slots:
        c.drawString(st.margin_x, y, slots["left"])
    if "center" in slots:
        c.drawCentredString(w / 2, y, slots["center"])
    if "right" in slots:
        c.drawRightString(w - st.margin_x, y, slots["right"])
    c.restoreState()


def _rot_extent(bw: float, bh: float, deg: float) -> tuple[float, float]:
    t = math.radians(deg)
    return abs(bw * math.cos(t)) + abs(bh * math.sin(t)), abs(bw * math.sin(t)) + abs(bh * math.cos(t))


def _wm_angle(spec: str | float, w: float, h: float) -> float:
    if isinstance(spec, (int, float)):
        return float(spec)
    s = str(spec).strip().lower()
    if s in ("diagonal", "diag", "d"):
        return math.degrees(math.atan2(h, w))
    if s in ("horizontal", "h", "flat", "0"):
        return 0.0
    if s in ("vertical", "v"):
        return 90.0
    return float(s)


def _draw_watermark(c, w: float, h: float, wm: WatermarkSpec) -> None:
    ang = _wm_angle(wm.angle, w, h)
    c.saveState()
    c.setFillColor(_color(wm.color))  # set colours first: ReportLab resets alpha when a colour is set
    c.setFillAlpha(max(0.0, min(1.0, wm.opacity)))
    c.setStrokeAlpha(max(0.0, min(1.0, wm.opacity)))
    if wm.text:
        font = _font(wm.font)
        k = _string_width(wm.text, font, 1.0)
        a, d = _ascent_descent(font, 1.0)
        th = a + d
        if wm.size:
            size = wm.size
        else:
            t = math.radians(ang)
            ct, sn = abs(math.cos(t)), abs(math.sin(t))
            # Text box (k*s by th*s) rotated by ang must fit inside fill*page in both directions.
            size = min(
                wm.fill * w / max(1e-6, k * ct + th * sn),
                wm.fill * h / max(1e-6, k * sn + th * ct),
            )
        # Never let an explicit size push text off the page.
        ex, ey = _rot_extent(k * size, th * size, ang)
        if ex > 0.98 * w or ey > 0.98 * h:
            size *= min(0.98 * w / ex, 0.98 * h / ey)
        c.setFont(font, size)
        c.translate(w / 2, h / 2)
        c.rotate(ang)
        # Centre the glyph box vertically on the pivot.
        c.drawCentredString(0, -(a - d) * size / 2, wm.text)
    elif wm.image:
        from reportlab.lib.utils import ImageReader

        img = ImageReader(str(wm.image))
        iw, ih = img.getSize()
        dw = max(1.0, wm.scale * w)
        dh = dw * ih / iw
        ex, ey = _rot_extent(dw, dh, ang)
        f = min(1.0, 0.98 * w / ex, 0.98 * h / ey)
        dw, dh = dw * f, dh * f
        c.translate(w / 2, h / 2)
        c.rotate(ang)
        c.drawImage(img, -dw / 2, -dh / 2, dw, dh, mask="auto")
    c.restoreState()


def _approval_box(ap: ApprovalSpec) -> tuple[float, float, list[tuple[str, str, float, float]]]:
    """Box (bw, bh) and lines [(text, font, size, baseline-from-bottom)] for an approval stamp."""
    s = max(0.2, ap.scale or 1.0)
    bold, reg = _font("sans-bold"), _font("sans-semibold")
    label_size = 20 * s
    small = 8.5 * s
    pad_x, pad_y = 12 * s, 9 * s
    lines: list[tuple[str, str, float]] = []
    extra: list[str] = []
    if ap.name:
        extra.append(f"BY: {ap.name}")
    if ap.date:
        d = _date.today().strftime(ap.date_format) if ap.date.strip().lower() == "today" else ap.date
        extra.append(f"DATE: {d}")
    lw = _string_width(ap.label, bold, label_size)
    ew = max([_string_width(t, reg, small) for t in extra] or [0])
    bw = max(lw, ew) + 2 * pad_x
    la, ld = _ascent_descent(bold, label_size)
    sa, sd = _ascent_descent(reg, small)
    rule_gap = 5 * s if extra else 0
    bh = pad_y * 2 + la + ld * 0.4 + rule_gap * 2 * (1 if extra else 0) + len(extra) * (sa + sd + 2 * s)
    out: list[tuple[str, str, float, float]] = []
    y = bh - pad_y - la
    out.append((ap.label, bold, label_size, y))
    y -= ld * 0.4 + rule_gap
    rule_y = y
    y -= rule_gap
    for t in extra:
        y -= sa
        out.append((t, reg, small, y))
        y -= sd + 2 * s
    out.append(("__rule__", "", rule_y if extra else -1, 0))
    return bw, bh, out


def _approval_origin(ap: ApprovalSpec, w: float, h: float, bw: float, bh: float) -> tuple[float, float]:
    """Centre of the stamp in visual coordinates, clamped so the rotated box stays on the page."""
    ex, ey = _rot_extent(bw, bh, ap.rotation)
    m = ap.margin
    if ap.at is not None:
        cx, cy = ap.at[0] + bw / 2, ap.at[1] + bh / 2
    else:
        pos = ap.position.strip().lower().replace("_", "-").replace("centre", "center")
        pos = {"tl": "top-left", "tr": "top-right", "bl": "bottom-left", "br": "bottom-right", "c": "center",
               "tc": "top-center", "bc": "bottom-center", "middle": "center"}.get(pos, pos)
        if pos not in STAMP_POSITIONS:
            raise ValueError(f"unknown stamp position {ap.position!r}; use {', '.join(STAMP_POSITIONS)} or x,y")
        cx = {"left": m + ex / 2, "center": w / 2, "right": w - m - ex / 2}[
            "left" if pos.endswith("left") else "right" if pos.endswith("right") else "center"]
        cy = {"top": h - m - ey / 2, "center": h / 2, "bottom": m + ey / 2}[
            "top" if pos.startswith("top") else "bottom" if pos.startswith("bottom") else "center"]
    cx = min(max(cx, ex / 2 + 2), w - ex / 2 - 2)
    cy = min(max(cy, ey / 2 + 2), h - ey / 2 - 2)
    return cx, cy


def _draw_approval(c, w: float, h: float, ap: ApprovalSpec) -> None:
    color = _color(ap.color or APPROVAL_COLORS.get(ap.label.strip().upper(), "#1F3A93"))
    if not ap.scale:
        auto = min(3.0, max(1.0, min(w, h) / 612.0))
        ap = ApprovalSpec(**{**ap.__dict__, "scale": auto, "margin": ap.margin * auto})
    s = max(0.2, ap.scale)
    bw, bh, lines = _approval_box(ap)
    if bw + 4 > w or bh + 4 > h:  # very small page: shrink to fit
        f = min((w - 8) / bw, (h - 8) / bh)
        ap = ApprovalSpec(**{**ap.__dict__, "scale": s * f})
        s = ap.scale
        bw, bh, lines = _approval_box(ap)
    cx, cy = _approval_origin(ap, w, h, bw, bh)
    c.saveState()
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setFillAlpha(ap.opacity)  # after the colours: ReportLab resets alpha when a colour is set
    c.setStrokeAlpha(ap.opacity)
    c.translate(cx, cy)
    c.rotate(ap.rotation)
    c.translate(-bw / 2, -bh / 2)
    r = 6 * s
    c.setLineWidth(2.6 * s)
    c.roundRect(0, 0, bw, bh, r, stroke=1, fill=0)
    ins = 3.6 * s
    c.setLineWidth(0.8 * s)
    c.roundRect(ins, ins, bw - 2 * ins, bh - 2 * ins, max(1.0, r - ins / 2), stroke=1, fill=0)
    for text, font, size, y in lines:
        if text == "__rule__":
            if size >= 0:
                c.setLineWidth(0.7 * s)
                c.line(ins + 6 * s, size, bw - ins - 6 * s, size)
            continue
        c.setFont(font, size)
        if font == _font("sans-bold"):
            c.drawCentredString(bw / 2, y, text)
        else:
            c.drawString(12 * s, y, text)
    c.restoreState()


# ------------------------------------------------------------------ core apply
def _apply(
    pdf: pikepdf.Pdf,
    src: Path,
    idx: list[int],
    *,
    texts: Mapping[str, str] | None,
    style: TextStyle,
    first_number: int,
    date_format: str,
    title: str | None,
    bates: BatesSpec | None,
    bates_next: int,
    wm: WatermarkSpec | None,
    approval: ApprovalSpec | None,
    approval_pages: list[int] | None,
) -> dict[str, Any]:
    """Apply all requested stamps to *pdf* in place. Returns info (bates range etc.)."""
    n = len(pdf.pages)
    info: dict[str, Any] = {"pages_stamped": len(idx)}
    doc_title = title if title is not None else (_title(pdf) or src.stem)
    today = _date.today().strftime(date_format)
    bates_of: dict[int, str] = {}
    if bates is not None:
        k = bates_next
        for i in idx:
            bates_of[i] = bates.label(k)
            k += 1
        info["bates_first"] = bates_of[idx[0]] if idx else None
        info["bates_last"] = bates_of[idx[-1]] if idx else None
        info["next"] = k

    if wm is not None and (wm.text or wm.image):
        if wm.image and not Path(wm.image).exists():
            raise FileNotFoundError(f"watermark image not found: {wm.image}")
        stamp_pages(pdf, lambda c, i, w, h: _draw_watermark(c, w, h, wm), idx, under=wm.behind, tag="PdfStudioWm")

    norm = {_norm_pos(k): v for k, v in (texts or {}).items() if v}
    if norm or bates is not None:
        bst = bates.style if bates is not None and bates.style else TextStyle(font="sans-semibold", size=style.size, color="#000000",
                                                                               margin_y=style.margin_y, margin_x=style.margin_x)

        def draw(c, i, w, h):
            ctx = {"page": i + first_number, "pages": n + first_number - 1, "date": today, "title": doc_title,
                   "file": src.name, "label": _label(pdf, i)}
            if i in bates_of:
                ctx["bates"] = bates_of[i]
            filled = {k: format_tokens(v, ctx) for k, v in norm.items()}
            bpos = _norm_pos(bates.position) if bates is not None else None
            if bpos and i in bates_of:
                if bpos in filled:  # share the slot: Bates number after the text
                    filled[bpos] = f"{filled[bpos]}   {bates_of[i]}"
                    for band in ("header", "footer"):
                        _draw_band(c, filled, band, w, h, style)
                    return
                for band in ("header", "footer"):
                    _draw_band(c, filled, band, w, h, style)
                _draw_band(c, {bpos: bates_of[i]}, bpos.split("-")[0], w, h, bst)
                return
            for band in ("header", "footer"):
                _draw_band(c, filled, band, w, h, style)

        stamp_pages(pdf, draw, idx, tag="PdfStudioHf")

    if approval is not None:
        ap_idx = approval_pages if approval_pages is not None else idx[:1]
        stamp_pages(pdf, lambda c, i, w, h: _draw_approval(c, w, h, approval), ap_idx, tag="PdfStudioAp")
        info["approval_pages"] = [i + 1 for i in ap_idx]

    if bates is not None and idx:
        _write_bates_metadata(pdf, info["bates_first"], info["bates_last"])
    return info


def _write_bates_metadata(pdf: pikepdf.Pdf, first: str, last: str) -> None:
    tag = f"Bates {first}-{last}"
    try:
        kw = str(pdf.docinfo.get("/Keywords", "") or "")
    except Exception:
        kw = ""
    kw = re.sub(r"(;\s*)?Bates \S+-\S+", "", kw).strip("; ").strip()
    kw = f"{kw}; {tag}" if kw else tag
    pdf.docinfo["/Keywords"] = kw
    pdf.docinfo["/BatesFirst"] = first
    pdf.docinfo["/BatesLast"] = last
    if "/Metadata" in pdf.Root:
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as m:
                m["pdf:Keywords"] = kw
        except Exception:
            pass


# ------------------------------------------------------------------ public API
def stamp(
    src: str | Path,
    *,
    texts: Mapping[str, str] | None = None,
    style: TextStyle | None = None,
    pages: str | Sequence[int] | None = None,
    first_number: int = 1,
    date_format: str = "%Y-%m-%d",
    title: str | None = None,
    bates: BatesSpec | None = None,
    watermark: WatermarkSpec | None = None,
    approval: ApprovalSpec | None = None,
    approval_pages: str | Sequence[int] | None = None,
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Apply any combination of header/footer texts, Bates numbers, a watermark and an approval stamp.

    *texts* maps positions (``header-left`` … ``footer-right``, or ``tl``/``bc``…) to text with tokens
    ``{page} {pages} {date} {title} {file} {label}`` (and ``{bates}`` when Bates is on); format specs
    such as ``{page:03d}`` work. *pages* is a page spec (``"1-3,5"``) or 0-based indexes.
    *approval_pages* defaults to the first selected page.
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    style = style or TextStyle()
    tag = "stamped" if not (bates and not texts and not watermark and not approval) else "bates"
    dst = output_path(src, tag, out=out)
    with _open(src, password) as pdf:
        n = len(pdf.pages)
        idx = parse_pages(pages, n) if (pages is None or isinstance(pages, str)) else list(pages)
        ap_idx = None
        if approval_pages is not None:
            ap_idx = parse_pages(approval_pages, n) if isinstance(approval_pages, str) else list(approval_pages)
        info = _apply(pdf, src, idx, texts=texts, style=style, first_number=first_number, date_format=date_format,
                      title=title, bates=bates, bates_next=bates.start if bates else 1, wm=watermark,
                      approval=approval, approval_pages=ap_idx)
        _save(pdf, dst)
    return finalize(dst, baseline=src, options=options, password=password, data=info, quiet=quiet)


def header_footer(
    src: str | Path,
    texts: Mapping[str, str],
    *,
    style: TextStyle | None = None,
    pages: str | Sequence[int] | None = None,
    first_number: int = 1,
    date_format: str = "%Y-%m-%d",
    title: str | None = None,
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Headers/footers at up to six positions. See :func:`stamp` for tokens."""
    return stamp(src, texts=texts, style=style, pages=pages, first_number=first_number, date_format=date_format,
                 title=title, out=out, password=password, options=options, quiet=quiet)


def page_numbers(
    src: str | Path,
    fmt: str = "Page {page} of {pages}",
    *,
    position: str = "footer-center",
    style: TextStyle | None = None,
    pages: str | Sequence[int] | None = None,
    first_number: int = 1,
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """"Page X of Y" (or any format) at one position."""
    return stamp(src, texts={position: fmt}, style=style, pages=pages, first_number=first_number, out=out,
                 password=password, options=options, quiet=quiet)


def bates(
    srcs: str | Path | Sequence[str | Path],
    *,
    prefix: str = "",
    start: int = 1,
    digits: int = 6,
    suffix: str = "",
    position: str = "footer-right",
    pattern: str | None = None,
    style: TextStyle | None = None,
    outs: Sequence[str | Path] | None = None,
    out_dir: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Bates-number one or more PDFs, continuing the sequence across files (one output per input).

    Each output's metadata gets ``Keywords: … Bates FIRST-LAST`` plus ``/BatesFirst`` / ``/BatesLast``.
    Returns ``data["next"]`` (the number to start the next batch with) and ``data["ranges"]``.
    """
    paths = [Path(srcs)] if isinstance(srcs, (str, Path)) else [Path(s) for s in srcs]
    if not paths:
        raise ValueError("no input files")
    if outs is not None and len(outs) != len(paths):
        raise ValueError("outs must have one entry per input")
    st = style or TextStyle(font="sans-semibold", color="#000000")
    nxt = start
    results: list[OpResult] = []
    ranges = []
    for k, src in enumerate(paths):
        if not src.exists():
            raise FileNotFoundError(src)
        spec = BatesSpec(prefix=prefix, start=nxt, digits=digits, suffix=suffix, position=position, pattern=pattern, style=st)
        dst = output_path(src, "bates", out=outs[k] if outs is not None else out_dir)
        with _open(src, password) as pdf:
            idx = list(range(len(pdf.pages)))
            info = _apply(pdf, src, idx, texts=None, style=st, first_number=1, date_format="%Y-%m-%d", title=None,
                          bates=spec, bates_next=nxt, wm=None, approval=None, approval_pages=None)
            _save(pdf, dst)
        nxt = info["next"]
        ranges.append({"file": str(src), "output": str(dst), "first": info["bates_first"], "last": info["bates_last"],
                       "pages": len(idx)})
        results.append(finalize(dst, baseline=src, options=options, password=password, data=info, quiet=quiet))
    merged = GateReport(results[0].output)
    for r in results:
        if r.gate is None:
            continue
        for f in r.gate.failures:
            f.message = f"{r.output.name}: {f.message}" if len(results) > 1 else f.message
            merged.failures.append(f)
        merged.warnings.extend(r.gate.warnings)
    return OpResult(results[0].output, merged, [r.output for r in results[1:]],
                    {"next": nxt, "ranges": ranges, "gates": [r.line() for r in results]})


def watermark(
    src: str | Path,
    text: str | None = None,
    *,
    image: str | Path | None = None,
    angle: str | float = "diagonal",
    opacity: float = 0.18,
    color: str = "#7F7F7F",
    size: float | None = None,
    font: str = "sans-bold",
    behind: bool = False,
    scale: float = 0.5,
    pages: str | Sequence[int] | None = None,
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Text or image watermark with opacity (ExtGState), auto-fitted to the page diagonal."""
    if not text and not image:
        raise ValueError("give watermark text or an image")
    wm = WatermarkSpec(text=text, image=image, angle=angle, opacity=opacity, color=color, size=size, font=font,
                       behind=behind, scale=scale)
    return stamp(src, watermark=wm, pages=pages, out=out or None, password=password, options=options, quiet=quiet)


def approval_stamp(
    src: str | Path,
    label: str = "APPROVED",
    *,
    name: str | None = None,
    date: str | None = None,
    color: str | None = None,
    rotation: float = 0.0,
    position: str = "top-right",
    at: tuple[float, float] | None = None,
    scale: float | None = None,
    opacity: float = 0.9,
    pages: str | Sequence[int] | None = "1",
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Bordered rubber-stamp box (APPROVED / REVIEWED / REJECTED / ISSUED FOR CONSTRUCTION / custom)."""
    ap = ApprovalSpec(label=label, name=name, date=date, color=color, rotation=rotation, position=position, at=at,
                      scale=scale, opacity=opacity)
    return stamp(src, approval=ap, approval_pages=pages if pages is not None else "all", out=out, password=password,
                 options=options, quiet=quiet)


# ------------------------------------------------------------------ CLI
def _style_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--font", default="sans", choices=_FONT_ROLES, help="house font role (default sans = Inter)")
    p.add_argument("--size", type=float, default=9.0, help="text size in pt (default 9)")
    p.add_argument("--color", default="#222222", help="#RRGGBB, r,g,b or a color name")
    p.add_argument("--margin", default="27", help="distance from top/bottom edge (pt, or 0.5in / 12mm)")
    p.add_argument("--side-margin", default="36", help="distance from left/right edge")


def _style_from(ns) -> TextStyle:
    return TextStyle(font=ns.font, size=ns.size, color=ns.color, margin_y=parse_length(ns.margin),
                     margin_x=parse_length(ns.side_margin))


def _xy(s: str | None) -> tuple[float, float] | None:
    if not s:
        return None
    a, b = s.split(",")
    return parse_length(a), parse_length(b)


@command("stamp", "Headers, footers, page numbers, Bates, watermarks, approval stamps", group="stamp")
def _cli(p):
    p.add_argument("pdf")
    for pos in POSITIONS:
        p.add_argument(f"--{pos}", metavar="TEXT", help=f"text at {pos} (tokens {{page}} {{pages}} {{date}} {{title}} {{file}} {{label}})")
    p.add_argument("--header", metavar="TEXT", help="same as --header-center")
    p.add_argument("--footer", metavar="TEXT", help="same as --footer-center")
    p.add_argument("--page-numbers", nargs="?", const="Page {page} of {pages}", metavar="FMT",
                   help="add page numbers at footer-center (default 'Page {page} of {pages}')")
    p.add_argument("--pages", help="pages to stamp (e.g. 1-3,5; default all)")
    p.add_argument("--first-number", type=int, default=1, help="number shown on the first page")
    p.add_argument("--title", help="value for {title} (default: document title)")
    p.add_argument("--date-format", default="%Y-%m-%d")
    _style_args(p)
    g = p.add_argument_group("Bates")
    g.add_argument("--bates", metavar="PATTERN", help="Bates pattern ('BAR-{n:06d}') or a prefix ('BAR-')")
    g.add_argument("--bates-start", type=int, default=1)
    g.add_argument("--bates-digits", type=int, default=6)
    g.add_argument("--bates-suffix", default="")
    g.add_argument("--bates-position", default="footer-right", choices=POSITIONS)
    w = p.add_argument_group("watermark")
    w.add_argument("--watermark", metavar="TEXT")
    w.add_argument("--watermark-image", metavar="PNG/JPG")
    w.add_argument("--wm-angle", default="diagonal", help="diagonal, horizontal or degrees")
    w.add_argument("--wm-opacity", type=float, default=0.18)
    w.add_argument("--wm-color", default="#7F7F7F")
    w.add_argument("--wm-size", type=float, help="pt (default: fit the page)")
    w.add_argument("--wm-scale", type=float, default=0.5, help="image width as a fraction of page width")
    w.add_argument("--wm-behind", action="store_true", help="draw underneath page content")
    a = p.add_argument_group("approval stamp")
    a.add_argument("--approve", metavar="LABEL", help="APPROVED, REVIEWED, REJECTED, 'ISSUED FOR CONSTRUCTION' or custom")
    a.add_argument("--approve-name")
    a.add_argument("--approve-date", nargs="?", const="today", help="date line (no value = today)")
    a.add_argument("--approve-color")
    a.add_argument("--approve-rotate", type=float, default=0.0, help="degrees counter-clockwise")
    a.add_argument("--approve-pos", default="top-right", choices=STAMP_POSITIONS)
    a.add_argument("--approve-at", metavar="X,Y", help="lower-left of the box in pt from the visual bottom-left")
    a.add_argument("--approve-scale", type=float, help="size factor (default: auto by page size)")
    a.add_argument("--approve-pages", default="1", help="pages for the stamp (default 1)")
    add_common(p)

    def run(ns):
        texts = {pos: getattr(ns, pos.replace("-", "_")) for pos in POSITIONS if getattr(ns, pos.replace("-", "_"))}
        if ns.header:
            texts["header-center"] = ns.header
        if ns.footer:
            texts["footer-center"] = ns.footer
        if ns.page_numbers:
            texts["footer-center"] = ns.page_numbers
        bs = None
        if ns.bates is not None:
            pat = ns.bates if "{" in ns.bates else None
            bs = BatesSpec(prefix="" if pat else ns.bates, start=ns.bates_start, digits=ns.bates_digits,
                           suffix=ns.bates_suffix, position=ns.bates_position, pattern=pat)
        wm = None
        if ns.watermark or ns.watermark_image:
            ang: str | float = ns.wm_angle
            wm = WatermarkSpec(text=ns.watermark, image=ns.watermark_image, angle=ang, opacity=ns.wm_opacity,
                               color=ns.wm_color, size=ns.wm_size, behind=ns.wm_behind, scale=ns.wm_scale)
        ap = None
        if ns.approve:
            ap = ApprovalSpec(label=ns.approve, name=ns.approve_name, date=ns.approve_date, color=ns.approve_color,
                              rotation=ns.approve_rotate, position=ns.approve_pos, at=_xy(ns.approve_at),
                              scale=ns.approve_scale)
        if not (texts or bs or wm or ap):
            raise ValueError("nothing to stamp: give --header/--footer/--page-numbers/--bates/--watermark/--approve")
        res = stamp(ns.pdf, texts=texts, style=_style_from(ns), pages=ns.pages, first_number=ns.first_number,
                    date_format=ns.date_format, title=ns.title, bates=bs, watermark=wm, approval=ap,
                    approval_pages=ns.approve_pages, out=ns.out, password=ns.password, options=gate_options_from(ns))
        if bs is not None:
            print(f"Bates {res.data.get('bates_first')}–{res.data.get('bates_last')}; next number {res.data.get('next')}")
        return res

    return run


@command("bates", "Bates-number one or more PDFs, continuing across files", group="stamp")
def _cli_bates(p):
    p.add_argument("pdfs", nargs="+")
    p.add_argument("--prefix", default="")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--digits", type=int, default=6)
    p.add_argument("--suffix", default="")
    p.add_argument("--pattern", help="e.g. 'BAR-{n:06d}' (overrides prefix/digits/suffix)")
    p.add_argument("--position", default="footer-right", choices=POSITIONS)
    p.add_argument("--out-dir", help="write outputs here (default next to each input)")
    _style_args(p)
    add_common(p, out=False)

    def run(ns):
        st = _style_from(ns)
        if ns.font == "sans":
            st.font = "sans-semibold"
        res = bates(ns.pdfs, prefix=ns.prefix, start=ns.start, digits=ns.digits, suffix=ns.suffix, position=ns.position,
                    pattern=ns.pattern, style=st, out_dir=ns.out_dir, password=ns.password, options=gate_options_from(ns))
        for r in res.data["ranges"]:
            print(f"  {Path(r['output']).name}: {r['first']} – {r['last']} ({r['pages']} pages)")
        print(f"next Bates number: {res.data['next']}")
        return res

    return run


@command("watermark", "Text or image watermark (diagonal/horizontal, opacity, behind or above)", group="stamp")
def _cli_wm(p):
    p.add_argument("pdf")
    p.add_argument("text", nargs="?")
    p.add_argument("--image", help="PNG/JPG watermark instead of text")
    p.add_argument("--angle", default="diagonal", help="diagonal, horizontal or degrees")
    p.add_argument("--opacity", type=float, default=0.18)
    p.add_argument("--color", default="#7F7F7F")
    p.add_argument("--size", type=float, help="pt (default: fit the page diagonal)")
    p.add_argument("--font", default="sans-bold", choices=_FONT_ROLES)
    p.add_argument("--scale", type=float, default=0.5, help="image width as a fraction of page width")
    p.add_argument("--behind", action="store_true", help="draw underneath page content")
    p.add_argument("--pages")
    add_common(p)
    return lambda ns: watermark(ns.pdf, ns.text, image=ns.image, angle=ns.angle, opacity=ns.opacity, color=ns.color,
                                size=ns.size, font=ns.font, behind=ns.behind, scale=ns.scale, pages=ns.pages,
                                out=ns.out, password=ns.password, options=gate_options_from(ns))


@command("approve", "Approval stamp box (APPROVED / REVIEWED / REJECTED / ISSUED FOR CONSTRUCTION / custom)", group="stamp")
def _cli_ap(p):
    p.add_argument("pdf")
    p.add_argument("label", nargs="?", default="APPROVED")
    p.add_argument("--name")
    p.add_argument("--date", nargs="?", const="today", help="date line (no value = today)")
    p.add_argument("--color")
    p.add_argument("--rotate", type=float, default=0.0, help="degrees counter-clockwise")
    p.add_argument("--position", default="top-right", choices=STAMP_POSITIONS)
    p.add_argument("--at", metavar="X,Y", help="lower-left of the box in pt from the visual bottom-left")
    p.add_argument("--scale", type=float, help="size factor (default: auto by page size)")
    p.add_argument("--opacity", type=float, default=0.9)
    p.add_argument("--pages", default="1")
    add_common(p)
    return lambda ns: approval_stamp(ns.pdf, ns.label, name=ns.name, date=ns.date, color=ns.color, rotation=ns.rotate,
                                     position=ns.position, at=_xy(ns.at), scale=ns.scale, opacity=ns.opacity,
                                     pages=ns.pages, out=ns.out, password=ns.password, options=gate_options_from(ns))
