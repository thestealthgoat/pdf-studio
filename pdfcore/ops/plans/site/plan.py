"""Site plan sheets: layered DXF (real-world feet) + a vector PDF on the drawing-sheet title block.

The plan is drawn with ReportLab into a PDF the exact size of the drawing-sheet template's drawing area
(:func:`pdfcore.ops.plans.cad.drawing_area`), so Typst places it 1:1 and the scale is exact. A /Measure
viewport over the plan window makes ``pdfcore plans measure`` (and Bluebeam/Acrobat) read real feet.

Sheet layout (drawing area): plan window on the left (graphic scale + view title under it), a side panel
on the right with north arrow, legend, quantities (job plans) and general notes / data attribution.
Existing features are screened (greys, thin); proposed work (``site-add``) is heavy, in the brand accent.
"""

from __future__ import annotations

import datetime as dt
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from pdfcore.ops.plans.site import geom
from pdfcore.ops.plans.site.geo import Georef, SitePackage

PT = 72.0
MM = 72.0 / 25.4
ENG_SCALES = [10, 20, 30, 40, 50, 60, 80, 100, 150, 200, 300, 400, 500, 600, 1000, 2000]

# DXF layers: name → (ACI colour, lineweight 1/100 mm, linetype)
LAYERS = {
    "SITE-BLDG": (8, 50, "CONTINUOUS"), "SITE-ROAD-EDGE": (8, 35, "CONTINUOUS"), "SITE-ROAD-CL": (9, 18, "CENTER"),
    "SITE-PAVE": (9, 25, "CONTINUOUS"), "SITE-VEG": (94, 18, "CONTINUOUS"), "SITE-WATER": (150, 25, "CONTINUOUS"),
    "SITE-UTIL": (8, 25, "DASHED"), "SITE-FENCE": (8, 18, "CONTINUOUS"), "SITE-TEXT": (8, 18, "CONTINUOUS"),
    "SITE-IMAGE": (8, 13, "CONTINUOUS"),
    "PROP-PAD": (1, 50, "CONTINUOUS"), "PROP-EQPT": (1, 50, "CONTINUOUS"), "PROP-DUCT": (1, 70, "CONTINUOUS"),
    "PROP-COND": (1, 50, "DASHED"), "PROP-TRENCH": (1, 35, "DASHED"), "PROP-POLE": (1, 35, "CONTINUOUS"),
    "PROP-HH": (1, 35, "CONTINUOUS"), "PROP-FENCE": (1, 35, "CONTINUOUS"), "PROP-GEN": (1, 50, "CONTINUOUS"),
    "PROP-TEXT": (7, 25, "CONTINUOUS"),
}

def _lw(mm: float) -> float:
    """Line weight in points from millimetres."""
    return mm * MM


# existing-feature styles (ReportLab): stroke colour, width (pt), fill, dash
INK = "#1E1E1E"
EX = {
    "building": ("#3C3C3C", _lw(0.35), "#D9D9D9", None),
    "road_edge": ("#555555", _lw(0.35), None, None),
    "road_cl": ("#7A7A7A", _lw(0.18), None, [14, 3, 2.5, 3]),
    "pavement": ("#8C8C8C", _lw(0.18), "#F0F0F0", None),
    "trees": ("#5F7F5F", _lw(0.18), None, None),
    "water": ("#3C78B4", _lw(0.25), "#D9E8F6", None),
    "fence": ("#666666", _lw(0.18), None, None),
    "power_line": ("#6A6A6A", _lw(0.25), None, [8, 3]),
}


# ----------------------------------------------------------------------------- scale + view
def eng_scale(ft_per_in: float) -> str:
    return f'1" = {ft_per_in:g}\''


def choose_scale(extent_w: float, extent_h: float, win_w_in: float, win_h_in: float) -> float:
    need = max(extent_w / win_w_in, extent_h / win_h_in)
    for s in ENG_SCALES:
        if s >= need * 0.999:
            return float(s)
    return float(math.ceil(need / 1000) * 1000)


@dataclass
class View:
    """World (CRS ft) ↔ drawing-PDF points. Window (x, y, w, h) in points, origin bottom-left."""

    cx: float
    cy: float
    ft_per_in: float
    win: tuple[float, float, float, float]

    @property
    def k(self) -> float:  # points per foot
        return PT / self.ft_per_in

    def p(self, x: float, y: float) -> tuple[float, float]:
        wx, wy, ww, wh = self.win
        return (wx + ww / 2 + (x - self.cx) * self.k, wy + wh / 2 + (y - self.cy) * self.k)

    def pts(self, ring: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
        return [self.p(q[0], q[1]) for q in ring]

    def world_box(self, pad_ft: float = 0.0) -> tuple[float, float, float, float]:
        wx, wy, ww, wh = self.win
        hw, hh = ww / 2 / self.k + pad_ft, wh / 2 / self.k + pad_ft
        return (self.cx - hw, self.cy - hh, self.cx + hw, self.cy + hh)

    def inside(self, X: float, Y: float, m: float = 0.0) -> bool:
        wx, wy, ww, wh = self.win
        return wx + m <= X <= wx + ww - m and wy + m <= Y <= wy + wh - m


# ----------------------------------------------------------------------------- label placement
class Labels:
    """Greedy label placement: keeps text boxes from overlapping each other and reserved areas."""

    def __init__(self):
        self.boxes: list[tuple[float, float, float, float]] = []
        self.soft: list[tuple[float, float, float, float]] = []  # linework callouts should not sit on

    def free(self, b, soft: bool = False) -> bool:
        x0, y0, x1, y1 = b
        return all(x1 < a0 or x0 > a1 or y1 < b0 or y0 > b1
                   for a0, b0, a1, b1 in (self.boxes + self.soft if soft else self.boxes))

    def avoid_line(self, pts, r: float = 3.0, step: float = 5.0) -> None:
        for (x, y) in geom.resample(pts, step) if len(pts) >= 2 else pts:
            self.soft.append((x - r, y - r, x + r, y + r))

    def take(self, b) -> None:
        self.boxes.append(b)


# ----------------------------------------------------------------------------- drawing context
@dataclass
class Sheet:
    W: float  # drawing-area PDF size (pt)
    H: float
    s: float  # sheet text scale (1.0 ≈ ARCH/ANSI D)
    fs: Callable[[float], float]
    accent: str
    primary: str


class Pen:
    """Thin wrapper over a ReportLab canvas with plan-drawing helpers."""

    def __init__(self, c, view: View, sheet: Sheet):
        self.c = c
        self.v = view
        self.sh = sheet
        self.labels = Labels()
        from pdfcore.fonts import register_reportlab

        register_reportlab()

    # --- primitives
    def style(self, stroke: str | None, width: float = 0.5, fill: str | None = None, dash=None, alpha: float = 1.0):
        c = self.c
        if stroke:
            c.setStrokeColor(stroke)
        c.setLineWidth(width)
        if fill:
            c.setFillColor(fill)
        c.setDash(dash or [])
        c.setLineJoin(1)
        c.setLineCap(1)
        try:
            c.setFillAlpha(alpha)
        except Exception:
            pass

    def path(self, pts, closed: bool = False):
        p = self.c.beginPath()
        if not pts:
            return p
        p.moveTo(*pts[0])
        for q in pts[1:]:
            p.lineTo(*q)
        if closed:
            p.close()
        return p

    def poly(self, rings_world, stroke=True, fill=False):
        p = self.c.beginPath()
        for r in rings_world:
            pts = self.v.pts(geom.open_ring(r))
            if len(pts) < 2:
                continue
            p.moveTo(*pts[0])
            for q in pts[1:]:
                p.lineTo(*q)
            p.close()
        self.c.drawPath(p, stroke=1 if stroke else 0, fill=1 if fill else 0, fillMode=0)

    def line(self, pts_world, *, closed: bool = False):
        pts = self.v.pts(pts_world)
        if len(pts) >= 2:
            self.c.drawPath(self.path(pts, closed), stroke=1, fill=0)

    def hatch(self, rings_world, spacing_pt: float, angle: float = 45.0, color: str = "#9A9A9A", width: float = 0.35):
        c = self.c
        pts = [self.v.pts(geom.open_ring(r)) for r in rings_world]
        if not pts or len(pts[0]) < 3:
            return
        xs = [q[0] for r in pts for q in r]
        ys = [q[1] for r in pts for q in r]
        c.saveState()
        p = c.beginPath()
        for r in pts:
            p.moveTo(*r[0])
            for q in r[1:]:
                p.lineTo(*q)
            p.close()
        c.clipPath(p, stroke=0, fill=0, fillMode=0)
        self.style(color, width)
        t = math.radians(angle)
        dx, dy = math.cos(t), math.sin(t)
        nx, ny = -dy, dx
        cxp, cyp = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        R = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 2 + 2
        n = int(R / spacing_pt) + 1
        for i in range(-n, n + 1):
            ox, oy = cxp + nx * i * spacing_pt, cyp + ny * i * spacing_pt
            c.line(ox - dx * R, oy - dy * R, ox + dx * R, oy + dy * R)
        c.restoreState()

    def scallops(self, ring_world, step_pt: float):
        """Tree-line ('cloud') outline: outward arcs along the ring."""
        pts = self.v.pts(geom.open_ring(ring_world))
        if len(pts) < 3:
            return
        if geom.signed_area(pts) < 0:
            pts = pts[::-1]  # CCW in y-up page space: outside is to the right
        per = geom.length(pts, closed=True)
        n = max(6, int(per / step_pt))
        q = geom.resample(pts + [pts[0]], per / n)
        p = self.c.beginPath()
        p.moveTo(*q[0])
        for a, b in zip(q[:-1], q[1:]):
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            dx, dy = b[0] - a[0], b[1] - a[1]
            nx, ny = dy, -dx  # right-hand normal = outward
            h = 0.55
            c1 = (a[0] + dx * 0.15 + nx * h, a[1] + dy * 0.15 + ny * h)
            c2 = (b[0] - dx * 0.15 + nx * h, b[1] - dy * 0.15 + ny * h)
            p.curveTo(c1[0], c1[1], c2[0], c2[1], b[0], b[1])
        self.c.drawPath(p, stroke=1, fill=0)

    # --- text
    def text_w(self, s: str, size: float, font: str = "Inter") -> float:
        from reportlab.pdfbase.pdfmetrics import stringWidth

        return stringWidth(s, font, size)

    def text(self, x, y, s, size, *, font="Inter", color=INK, anchor="left", angle=0.0, halo: bool = False,
             reserve: bool = True, force: bool = False) -> bool:
        """Text at (x, y) points; anchor left|center|right; returns False if it collides (unless force)."""
        w = self.text_w(s, size, font)
        off = {"left": 0.0, "center": -w / 2, "right": -w}[anchor]
        a = math.radians(angle)
        corners = [(x + (off + u) * math.cos(a) - v * math.sin(a), y + (off + u) * math.sin(a) + v * math.cos(a))
                   for u in (0, w) for v in (-0.25 * size, 0.85 * size)]
        box = (min(p[0] for p in corners) - 1, min(p[1] for p in corners) - 1, max(p[0] for p in corners) + 1,
               max(p[1] for p in corners) + 1)
        if not force and not self.labels.free(box):
            return False
        c = self.c
        # Rotated text: PDF text extractors measure a glyph's size from its rotated bounding box, so steep labels
        # would read as tiny type. Those are drawn as glyph outlines instead (same look, no text layer).
        a_ = math.radians(angle)
        if size * (0.25 * abs(math.sin(a_)) + 0.95 * abs(math.cos(a_))) < 7.4:
            self._outline_text(x, y, s, size, font, color, off, angle, halo)
            if reserve:
                self.labels.take(box)
            return True
        c.saveState()
        c.translate(x, y)
        c.rotate(angle)
        if halo:
            t = c.beginText()
            t.setTextOrigin(off, 0)
            t.setFont(font, size)
            t.setTextRenderMode(1)
            c.setStrokeColor("#FFFFFF")
            c.setLineWidth(max(1.6, size * 0.28))
            c.setLineJoin(1)
            t.textOut(s)
            t.setTextRenderMode(0)  # text render mode persists in the graphics state
            c.drawText(t)
        c.setFillColor(color)
        c.setFont(font, size)
        c.drawString(off, 0, s)
        c.restoreState()
        if reserve:
            self.labels.take(box)
        return True

    _glyphs: dict = {}

    def _outline_text(self, x, y, s, size, font, color, off, angle, halo):
        from fontTools.pens.basePen import BasePen
        from fontTools.ttLib import TTFont

        from pdfcore.fonts import font_path

        fn = {"Inter": "Inter-Regular.ttf", "Inter-Bold": "Inter-Bold.ttf", "Inter-SemiBold": "Inter-SemiBold.ttf"}.get(font, "Inter-Regular.ttf")
        if fn not in Pen._glyphs:
            tt = TTFont(str(font_path(fn)))
            Pen._glyphs[fn] = (tt, tt.getGlyphSet(), tt.getBestCmap(), tt["head"].unitsPerEm, tt["hmtx"])
        tt, gs, cmap, upm, hmtx = Pen._glyphs[fn]
        k = size / upm
        path = self.c.beginPath()

        class P(BasePen):
            def __init__(self, glyphset, dx):
                super().__init__(glyphset)
                self.dx = dx

            def _moveTo(self, pt):
                path.moveTo(self.dx + pt[0] * k, pt[1] * k)

            def _lineTo(self, pt):
                path.lineTo(self.dx + pt[0] * k, pt[1] * k)

            def _curveToOne(self, p1, p2, p3):
                path.curveTo(self.dx + p1[0] * k, p1[1] * k, self.dx + p2[0] * k, p2[1] * k, self.dx + p3[0] * k, p3[1] * k)

            def _closePath(self):
                path.close()

        dx = off
        for ch in s:
            gname = cmap.get(ord(ch))
            if gname is None:
                gname = cmap.get(ord("?"))
            gs[gname].draw(P(gs, dx))
            dx += hmtx[gname][0] * k
        c = self.c
        c.saveState()
        c.translate(x, y)
        c.rotate(angle)
        if halo:
            c.setStrokeColor("#FFFFFF")
            c.setLineWidth(max(1.6, size * 0.28))
            c.setLineJoin(1)
            c.drawPath(path, stroke=1, fill=0)
        c.setFillColor(color)
        c.drawPath(path, stroke=0, fill=1, fillMode=1)
        c.restoreState()

    def text_along(self, pts_world, s, size, *, color=INK, font="Inter-SemiBold", halo=True, min_frac=0.6,
                   above: bool = False) -> bool:
        """Label a polyline on its longest nearly straight stretch inside the window (readable direction)."""
        pts = self.v.pts(pts_world)
        w = self.text_w(s, size, font)
        best = None
        for a, b in zip(pts[:-1], pts[1:]):
            L = math.dist(a, b)
            if L < w * min_frac + 4:
                continue
            for t in (0.5, 0.3, 0.7, 0.2, 0.8):
                mx, my = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                if not self.v.inside(mx, my, m=w / 2 + 6):
                    continue
                ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
                if ang > 90:
                    ang -= 180
                if ang < -90:
                    ang += 180
                cand = (L, mx, my, ang)
                if best is None or cand[0] > best[0]:
                    best = cand
                break
        if best is None:
            return False
        _L, mx, my, ang = best
        nx, ny = -math.sin(math.radians(ang)), math.cos(math.radians(ang))
        y_off = 0.45 * size if above else -0.35 * size
        return self.text(mx + nx * y_off, my + ny * y_off, s, size, font=font, color=color, anchor="center",
                         angle=ang, halo=halo)


# ----------------------------------------------------------------------------- existing features
def draw_existing(pen: Pen, feats: list[dict], *, underlay: bool, labels: bool = True) -> dict[str, int]:
    """Draw site features (SITE-*); returns what was drawn (for the legend)."""
    c, v = pen.c, pen.v
    box = v.world_box(pad_ft=20)
    drawn: dict[str, int] = {}

    def vis(f) -> bool:
        g = f["geometry"]
        co = g["coordinates"]
        if g["type"] == "Point":
            pts = [co]
        elif g["type"] == "LineString":
            pts = co
        else:
            pts = co[0]
        x0, y0, x1, y1 = geom.bbox_of(pts)
        return not (x1 < box[0] or x0 > box[2] or y1 < box[1] or y0 > box[3])

    feats = [f for f in feats if vis(f)]
    by = lambda *ks: [f for f in feats if f["properties"]["kind"] in ks]  # noqa: E731

    # vegetation (tree lines)
    st, w, fill, dash = EX["trees"]
    pen.style(st, w)
    min_tree = max(400.0, (0.35 * v.ft_per_in) ** 2)  # skip specks smaller than ~0.35 in on paper
    for f in by("trees"):
        a = geom.polygon_area(f["geometry"]["coordinates"])
        if a < min_tree:
            continue
        ring = geom.open_ring(f["geometry"]["coordinates"][0])
        ring = geom.chaikin(geom.simplify(ring, 0.06 * v.ft_per_in, closed=True), 2, closed=True)
        pen.scallops(ring, step_pt=max(6.0, 10 * pen.sh.s))
        drawn["trees"] = drawn.get("trees", 0) + 1
    # water
    st, w, fill, dash = EX["water"]
    for f in by("water"):
        pen.style(st, w, fill, alpha=0.55 if underlay else 1.0)
        pen.poly(f["geometry"]["coordinates"], fill=True)
        pen.style(st, w)
        pen.hatch(f["geometry"]["coordinates"], 7 * pen.sh.s, angle=0, color="#7AA6D2", width=0.35)
        pen.poly(f["geometry"]["coordinates"])
        drawn["water"] = drawn.get("water", 0) + 1
    # pavement / parking
    st, w, fill, dash = EX["pavement"]
    min_pave = max(1500.0, (0.4 * v.ft_per_in) ** 2)
    for f in by("pavement", "parking"):
        if f["geometry"]["type"] != "Polygon":
            continue
        if f["properties"].get("source") != "osm" and geom.polygon_area(f["geometry"]["coordinates"]) < min_pave:
            continue
        rings = [geom.simplify(geom.open_ring(r), 0.03 * v.ft_per_in, closed=True) for r in f["geometry"]["coordinates"]]
        if underlay:
            pen.style("#A5A5A5", _lw(0.13), dash=[3, 2])
            pen.poly(rings)
        else:
            pen.style(st, w, fill)
            pen.poly(rings, fill=True)
        drawn["pavement"] = drawn.get("pavement", 0) + 1
    # road surfaces
    st, w, fill, dash = EX["road_edge"]
    for f in by("road_surface"):
        if not underlay:
            pen.style(None, w, "#E9E9E9")
            pen.poly(f["geometry"]["coordinates"], stroke=False, fill=True)
        pen.style(st, w)
        pen.poly(f["geometry"]["coordinates"])
        drawn["road_edge"] = drawn.get("road_edge", 0) + 1
    st, w, fill, dash = EX["road_cl"]
    pen.style(st, w, dash=dash)
    for f in by("road"):
        if f["geometry"]["type"] == "LineString":
            pen.line(f["geometry"]["coordinates"])
            drawn["road_cl"] = drawn.get("road_cl", 0) + 1
    st, w, fill, dash = EX["road_cl"]
    pen.style("#9A9A9A", _lw(0.13), dash=[3, 2])
    for f in by("path"):
        if f["geometry"]["type"] == "LineString":
            pen.line(f["geometry"]["coordinates"])
    # buildings and tanks
    st, w, fill, dash = EX["building"]
    for f in by("building", "tank"):
        if f["geometry"]["type"] != "Polygon":
            continue
        rings = f["geometry"]["coordinates"]
        if not underlay:
            pen.style(None, w, fill)
            pen.poly(rings, stroke=False, fill=True)
        pen.hatch(rings, max(4.5, 6.5 * pen.sh.s), angle=45, color="#8C8C8C" if not underlay else "#6E6E6E", width=0.3)
        pen.style(st, w)
        pen.poly(rings)
        drawn["building"] = drawn.get("building", 0) + 1
    # fences, power lines, poles
    st, w, fill, dash = EX["fence"]
    pen.style(st, w)
    for f in by("fence"):
        if f["geometry"]["type"] == "LineString":
            fence_line(pen, f["geometry"]["coordinates"], st, w)
            drawn["fence"] = drawn.get("fence", 0) + 1
    st, w, fill, dash = EX["power_line"]
    for f in by("power_line"):
        pen.style(st, w, dash=dash)
        pen.line(f["geometry"]["coordinates"])
        drawn["power_line"] = drawn.get("power_line", 0) + 1
    pen.style("#555555", _lw(0.25))
    for f in by("power_pole"):
        X, Y = v.p(*f["geometry"]["coordinates"])
        if v.inside(X, Y):
            r = max(2.2, 3.0 * pen.sh.s)
            c.circle(X, Y, r, stroke=1, fill=0)
            c.line(X - r * 0.7, Y - r * 0.7, X + r * 0.7, Y + r * 0.7)
            c.line(X - r * 0.7, Y + r * 0.7, X + r * 0.7, Y - r * 0.7)
            drawn["power_pole"] = drawn.get("power_pole", 0) + 1
    if labels:
        label_existing(pen, feats)
    return drawn


def fence_line(pen: Pen, pts_world, color: str, width: float, mark: str = "x"):
    pen.style(color, width)
    pen.line(pts_world)
    P = pen.v.pts(pts_world)
    step = max(18.0, 26 * pen.sh.s)
    per = geom.length(P)
    if per < step:
        return
    r = 2.2 * max(0.8, pen.sh.s)
    for (x, y) in geom.resample(P, step)[1:-1]:
        pen.c.line(x - r, y - r, x + r, y + r)
        pen.c.line(x - r, y + r, x + r, y - r)


def label_existing(pen: Pen, feats: list[dict]) -> None:
    fs = pen.sh.fs
    # road names: one label per name, on its longest piece
    roads: dict[str, list] = {}
    for f in feats:
        p = f["properties"]
        if p["kind"] == "road" and p.get("name") and f["geometry"]["type"] == "LineString":
            roads.setdefault(p["name"], []).append(f["geometry"]["coordinates"])
    for name, lines in roads.items():
        clipped = [pc for ln in lines for pc in geom.clip_polyline([tuple(q) for q in ln], pen.v.world_box())]
        clipped.sort(key=geom.length, reverse=True)
        for ln in clipped[:2]:
            if pen.text_along(ln, name.upper(), fs(10.5), color="#2E2E2E"):
                if geom.length(ln) < 900:
                    break
    # buildings: OSM name, else EXIST. BLDG with area for the larger ones
    for f in sorted(feats, key=lambda f: -f["properties"].get("area_sf", geom.polygon_area(f["geometry"]["coordinates"])
                                          if f["geometry"]["type"] == "Polygon" else 0)):
        p = f["properties"]
        if p["kind"] not in ("building", "tank") or f["geometry"]["type"] != "Polygon":
            continue
        a = geom.polygon_area(f["geometry"]["coordinates"])
        ring = geom.open_ring(f["geometry"]["coordinates"][0])
        X, Y = pen.v.p(*geom.interior_point(ring))
        if not pen.v.inside(X, Y, m=30):
            continue
        name = p.get("name")
        if not name and a < 2500:
            continue
        lines = [name.upper()] if name else ["EXIST. BLDG"]
        lines.append(f"±{a:,.0f} SF")
        size = pen.sh.fs(8)
        wmax = max(pen.text_w(t, size, "Inter-SemiBold") for t in lines)
        bw = math.sqrt(a) * pen.v.k
        if wmax > bw * 1.6 and not name:
            continue
        lh = size * 1.2
        y0 = Y + (len(lines) - 1) * lh / 2 - size * 0.35
        if all(pen.labels.free((X - wmax / 2, y0 - i * lh - 2, X + wmax / 2, y0 - i * lh + size)) for i in range(len(lines))):
            for i, t in enumerate(lines):
                pen.text(X, y0 - i * lh, t, size if i == 0 else pen.sh.fs(7.5), font="Inter-SemiBold" if i == 0 else "Inter",
                         color="#2E2E2E", anchor="center", halo=True)


# ----------------------------------------------------------------------------- panel pieces
def north_arrow(pen: Pen, x: float, y: float, size: float, note: str = "") -> None:
    c = pen.c
    c.saveState()
    c.setStrokeColor(INK)
    c.setFillColor(INK)
    c.setLineWidth(0.8)
    r = size / 2
    c.circle(x, y, r * 0.78, stroke=1, fill=0)
    p = c.beginPath()
    p.moveTo(x, y + r)
    p.lineTo(x + r * 0.32, y - r * 0.55)
    p.lineTo(x, y - r * 0.25)
    p.close()
    c.drawPath(p, stroke=1, fill=1)
    p = c.beginPath()
    p.moveTo(x, y + r)
    p.lineTo(x - r * 0.32, y - r * 0.55)
    p.lineTo(x, y - r * 0.25)
    p.close()
    c.setFillColor("#FFFFFF")
    c.drawPath(p, stroke=1, fill=1)
    c.restoreState()
    pen.text(x, y + r + 4, "N", pen.sh.fs(14), font="Inter-Bold", anchor="center", reserve=False, force=True)
    if note:
        pen.text(x, y - r - pen.sh.fs(9), note, pen.sh.fs(7.5), anchor="center", color="#444444", reserve=False, force=True)


def scale_bar(pen: Pen, x: float, y: float, ft_per_in: float, max_w: float) -> float:
    """Graphic scale (alternating blocks) with its left end at (x, y); returns its width."""
    c = pen.c
    seg = 0.5 * PT  # half-inch blocks
    nseg = int(min(8, max(4, max_w // seg)))
    nseg -= nseg % 2
    h = max(4.0, 5.0 * pen.sh.s)
    c.saveState()
    c.setLineWidth(0.6)
    c.setStrokeColor(INK)
    for i in range(nseg):
        c.setFillColor(INK if i % 2 == 0 else "#FFFFFF")
        c.rect(x + i * seg, y, seg, h, stroke=1, fill=1)
    c.restoreState()
    fs = pen.sh.fs(8)
    for i in range(0, nseg + 1, 2 if nseg > 4 else 1):
        ft = i * 0.5 * ft_per_in
        pen.text(x + i * seg, y + h + 3, f"{ft:g}", fs, anchor="center", reserve=False, force=True)
    last = f"{nseg * 0.5 * ft_per_in:g}"
    pen.text(x + nseg * seg + pen.text_w(last, fs) / 2 + 5, y + h + 3, "FEET", fs, reserve=False, force=True)
    return nseg * seg


def view_title(pen: Pen, x: float, y: float, title: str, scale_text: str, num: str = "1") -> None:
    c = pen.c
    r = max(9.0, 12 * pen.sh.s)
    c.saveState()
    c.setLineWidth(1.0)
    c.setStrokeColor(INK)
    c.circle(x + r, y, r, stroke=1, fill=0)
    c.line(x, y, x + 2 * r, y)
    c.restoreState()
    pen.text(x + r, y + r * 0.25, num, pen.sh.fs(10), font="Inter-SemiBold", anchor="center", reserve=False, force=True)
    pen.text(x + r, y - r * 0.75, "—", pen.sh.fs(8), anchor="center", reserve=False, force=True)
    tx = x + 2 * r + 8
    pen.text(tx, y + 2, title.upper(), pen.sh.fs(13), font="Inter-Bold", reserve=False, force=True)
    tw = pen.text_w(title.upper(), pen.sh.fs(13), "Inter-Bold")
    c.saveState()
    c.setLineWidth(1.4)
    c.line(tx, y - 3, tx + tw, y - 3)
    c.setLineWidth(0.5)
    c.line(tx, y - 5.5, tx + tw, y - 5.5)
    c.restoreState()
    pen.text(tx, y - 5.5 - pen.sh.fs(10), "SCALE: " + scale_text, pen.sh.fs(9), reserve=False, force=True)


def wrap(pen: Pen, text: str, size: float, width: float, font: str = "Inter") -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if pen.text_w(t, size, font) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def heading(pen: Pen, x: float, y: float, w: float, text: str) -> float:
    size = pen.sh.fs(10.5)
    pen.text(x, y - size, text.upper(), size, font="Inter-Bold", reserve=False, force=True)
    pen.c.saveState()
    pen.c.setLineWidth(0.8)
    pen.c.setStrokeColor(INK)
    pen.c.line(x, y - size - 4, x + w, y - size - 4)
    pen.c.restoreState()
    return y - size - 10


def numbered_notes(pen: Pen, x: float, y: float, w: float, notes: list[str], size: float) -> float:
    lh = size * 1.28
    for i, n in enumerate(notes, 1):
        num = f"{i}."
        lines = wrap(pen, n.upper(), size, w - 16)
        for j, ln in enumerate(lines):
            if j == 0:
                pen.text(x, y - size, num, size, reserve=False, force=True)
            pen.text(x + 16, y - size, ln, size, reserve=False, force=True)
            y -= lh
        y -= size * 0.35
    return y


LEGEND_ORDER = [
    ("building", "EXISTING BUILDING"), ("road_edge", "EXISTING EDGE OF PAVEMENT (ROAD)"),
    ("road_cl", "EXISTING ROAD CENTERLINE"), ("pavement", "EXISTING PAVED / PARKING AREA"),
    ("trees", "EXISTING TREE LINE"), ("water", "EXISTING WATER"), ("fence", "EXISTING FENCE"),
    ("power_line", "EXISTING OVERHEAD ELECTRIC (OSM)"), ("power_pole", "EXISTING UTILITY POLE (OSM)"),
]


def legend_swatch(pen: Pen, key: str, x: float, y: float, w: float, h: float) -> None:
    c = pen.c
    mid = y + h / 2
    c.saveState()
    if key in ("building",):
        st, lw, fill, _ = EX["building"]
        r = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        pen.style(None, lw, fill)
        c.rect(x, y, w, h, stroke=0, fill=1)
        c.saveState()
        p = c.beginPath()
        p.rect(x, y, w, h)
        c.clipPath(p, stroke=0, fill=0)
        pen.style("#8C8C8C", 0.3)
        for i in range(-3, 8):
            c.line(x + i * 5, y, x + i * 5 + h, y + h)
        c.restoreState()
        pen.style(st, lw)
        c.rect(x, y, w, h, stroke=1, fill=0)
        del r
    elif key in ("road_edge",):
        st, lw, _f, _d = EX["road_edge"]
        pen.style(st, lw)
        c.line(x, y + 1, x + w, y + 1)
        c.line(x, y + h - 1, x + w, y + h - 1)
    elif key in ("road_cl", "power_line"):
        st, lw, _f, d = EX[key]
        pen.style(st, lw, dash=d)
        c.line(x, mid, x + w, mid)
    elif key == "pavement":
        st, lw, fill, _ = EX["pavement"]
        pen.style(st, lw, fill)
        c.rect(x, y, w, h, stroke=1, fill=1)
    elif key == "water":
        st, lw, fill, _ = EX["water"]
        pen.style(st, lw, fill)
        c.rect(x, y, w, h, stroke=1, fill=1)
    elif key == "trees":
        st, lw, _f, _d = EX["trees"]
        pen.style(st, lw)
        n = 4
        p = c.beginPath()
        p.moveTo(x, mid - 2)
        for i in range(n):
            a = x + i * w / n
            b = a + w / n
            p.curveTo(a + w / n * 0.15, mid + 5, b - w / n * 0.15, mid + 5, b, mid - 2)
        c.drawPath(p, stroke=1, fill=0)
    elif key == "fence":
        st, lw, _f, _d = EX["fence"]
        pen.style(st, lw)
        c.line(x, mid, x + w, mid)
        for xx in (x + w * 0.3, x + w * 0.7):
            c.line(xx - 2.2, mid - 2.2, xx + 2.2, mid + 2.2)
            c.line(xx - 2.2, mid + 2.2, xx + 2.2, mid - 2.2)
    elif key == "power_pole":
        pen.style("#555555", _lw(0.25))
        r = 3.0
        c.circle(x + w / 2, mid, r, stroke=1, fill=0)
        c.line(x + w / 2 - 2, mid - 2, x + w / 2 + 2, mid + 2)
        c.line(x + w / 2 - 2, mid + 2, x + w / 2 + 2, mid - 2)
    c.restoreState()


# ----------------------------------------------------------------------------- underlay
def underlay_image(pkg: SitePackage, g: Georef, view: View, *, opacity: float, color: bool, dpi: float, out: Path) -> Path:
    """Faded imagery resampled into the plan window at *dpi* (JPEG)."""
    from PIL import Image

    from pdfcore.ops.plans.site.raster import sample_bilinear

    wx, wy, ww, wh = view.win
    Wp, Hp = int(math.ceil(ww / PT * dpi)), int(math.ceil(wh / PT * dpi))
    src = np.asarray(Image.open(pkg.image_path).convert("RGB" if color else "L"), np.float32) / 255.0
    x0, y0, x1, y1 = view.world_box()
    cols = np.linspace(0.5, Wp - 0.5, Wp) / Wp
    rows = np.linspace(0.5, Hp - 0.5, Hp) / Hp
    X = x0 + cols[None, :] * (x1 - x0)
    Y = y1 - rows[:, None] * (y1 - y0)
    a, b, c, d, e, f = g.transform
    det = a * e - b * d
    Xc, Yc = X - c, Y - f
    pc = (e * Xc - b * Yc) / det - 0.5
    pr = (-d * Xc + a * Yc) / det - 0.5
    pc = np.broadcast_to(pc, (Hp, Wp))
    pr = np.broadcast_to(pr, (Hp, Wp))
    inside = (pc >= -0.5) & (pr >= -0.5) & (pc <= g.width - 0.5) & (pr <= g.height - 0.5)
    if color:
        chans = [sample_bilinear(src[..., i], pr, pc) for i in range(3)]
        img = np.stack(chans, -1)
        img = np.where(inside[..., None], img, 1.0)
    else:
        img = sample_bilinear(src, pr, pc)
        lo, hi = np.percentile(img[inside], 1) if inside.any() else 0, np.percentile(img[inside], 99) if inside.any() else 1
        img = np.clip((img - lo) / max(hi - lo, 1e-3), 0, 1) ** 0.75  # lift shadows/woods so linework reads
        img = np.where(inside, img, 1.0)
    img = 1.0 - opacity * (1.0 - img)  # fade toward white
    im = Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))
    im.save(out, quality=82, optimize=True)
    return out


# ----------------------------------------------------------------------------- DXF
def write_dxf(feats: list[dict], path: Path, *, g: Georef, image: Path | None, job=None, text_ft: float = 8.0) -> Path:
    import ezdxf

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 2  # feet
    doc.header["$MEASUREMENT"] = 0
    doc.header["$LUNITS"] = 2
    for name, (col, lw, lt) in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=col, lineweight=lw, linetype=lt if lt in doc.linetypes else "CONTINUOUS")
    msp = doc.modelspace()
    if image is not None and image.exists():
        idef = doc.add_image_def(filename=image.name, size_in_pixel=(g.width, g.height))
        ll = g.px_to_xy(0, g.height)
        msp.add_image(idef, insert=ll, size_in_units=(g.width * g.ft_per_px, g.height * g.ft_per_px),
                      rotation=g.rotation_deg, dxfattribs={"layer": "SITE-IMAGE"})
    for f in feats:
        p = f["properties"]
        k = p["kind"]
        gt = f["geometry"]["type"]
        co = f["geometry"]["coordinates"]
        layer = {"building": "SITE-BLDG", "tank": "SITE-BLDG", "road_surface": "SITE-ROAD-EDGE", "road": "SITE-ROAD-CL",
                 "path": "SITE-ROAD-CL", "pavement": "SITE-PAVE", "parking": "SITE-PAVE", "trees": "SITE-VEG",
                 "grass": "SITE-VEG", "water": "SITE-WATER", "waterway": "SITE-WATER", "fence": "SITE-FENCE",
                 "power_line": "SITE-UTIL", "power_pole": "SITE-UTIL", "transformer": "SITE-UTIL", "site": "SITE-TEXT",
                 "wetland": "SITE-VEG", "tree": "SITE-VEG"}.get(k)
        if layer is None:
            continue
        xd = {"layer": layer}
        if gt == "Polygon":
            for r in co:
                msp.add_lwpolyline([tuple(q[:2]) for q in geom.open_ring(r)], close=True, dxfattribs=xd)
        elif gt == "LineString":
            msp.add_lwpolyline([tuple(q[:2]) for q in co], dxfattribs=xd)
        elif gt == "Point":
            msp.add_circle(tuple(co[:2]), radius=1.0, dxfattribs=xd)
        if k == "road" and p.get("name") and gt == "LineString" and len(co) >= 2:
            (mx, my), ang = geom.point_at(co, 0.5)
            if ang > 90:
                ang -= 180
            if ang < -90:
                ang += 180
            msp.add_text(p["name"].upper(), height=text_ft, rotation=ang,
                         dxfattribs={"layer": "SITE-TEXT"}).set_placement((mx, my), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)
        if k == "building" and gt == "Polygon" and (p.get("name") or geom.polygon_area(co) >= 2500):
            cx, cy = geom.interior_point(geom.open_ring(co[0]))
            msp.add_text((p.get("name") or "EXIST. BLDG").upper(), height=text_ft * 0.8,
                         dxfattribs={"layer": "SITE-TEXT"}).set_placement((cx, cy), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)
    if job is not None:
        from pdfcore.ops.plans.site.job import job_dxf

        job_dxf(msp, job, text_ft=text_ft)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    chk = ezdxf.readfile(path)  # verify
    if len(chk.modelspace()) == 0:
        raise RuntimeError("DXF verification failed: empty modelspace")
    return path


# ----------------------------------------------------------------------------- the sheet
GENERAL_NOTES = [
    "Existing site features are approximate: derived from aerial imagery by automated classification{osm}. "
    "This drawing is not a survey. Field verify all locations, dimensions and utilities before construction.",
    "Call 811 before you dig. Locate and protect all existing underground utilities.",
    "Coordinate system: {crs}. North arrow shows grid north{conv}.",
    "Data: {attr}.",
]


@dataclass
class PlanResult:
    pdf: Path
    dxf: Path
    data: dict = field(default_factory=dict)


def _unique(path: Path) -> Path:
    cand, n = path, 2
    while cand.exists():
        cand = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return cand


def load_features(pkg: SitePackage, source: str | None = None) -> tuple[list[dict], str]:
    for name in ([source] if source else ["extracted.geojson", "osm.geojson"]):
        if name and (pkg.root / name).exists():
            return pkg.features(name), name
    return [], ""


def render(pkg: SitePackage | str | Path, *, scale: str = "auto", size: str = "ansi-d", brand: str = "default",
           underlay: float | None = 0.25, underlay_color: bool = False, sheet: str = "C-101", title: str = "Site Plan",
           project: str | None = None, client: str = "", drawn: str = "", checked: str = "", date: str | None = None,
           rev: str = "0", extent: Sequence[float] | None = None, center: Sequence[float] | None = None,
           features: str | None = None, job=None, out: str | Path | None = None, dxf_out: str | Path | None = None,
           view_title_text: str | None = None, notes: Sequence[str] = (), png_dir: str | Path | None = None,
           quiet: bool = False):
    """Render a site plan (or job plan when *job* is given) → OpResult (PDF, gate) with the DXF as extra output."""
    import pikepdf
    from reportlab.pdfgen import canvas

    from pdfcore.gate import GateOptions
    from pdfcore.ops._common import finalize
    from pdfcore.ops.plans.cad import _norm_size, TEMPLATE_SIZES, drawing_area, paper_size
    from pdfcore.ops.plans.measure import add_viewport, parse_scale
    from pdfcore.ops.typeset import load_brand, make

    pkg = pkg if isinstance(pkg, SitePackage) else SitePackage.open(pkg)
    g = pkg.georef()
    feats, fsrc = load_features(pkg, features)
    if not feats and job is None and not quiet:
        print("note: no extracted.geojson / osm.geojson — run `pdfcore plans site-extract` first; drawing imagery only")
    size_key = _norm_size(size)
    if size_key not in TEMPLATE_SIZES:
        raise ValueError(f"--size must be one of {', '.join(TEMPLATE_SIZES)}")
    b = load_brand(brand)
    accent = b["colors"].get("accent_text") or b["colors"]["accent"]
    primary = b["colors"]["primary"]
    restricted = bool(pkg.manifest.get("restricted_imagery"))
    if restricted and underlay:
        if not quiet:
            print("note: imagery looks like Google's — underlay left off (their terms restrict reuse)")
        underlay = None

    Wsh, Hsh = paper_size(size_key)
    ax, ay, aw, ah = drawing_area(size_key)
    s = max(0.55, Wsh / 36.0)
    W, H = aw * PT, ah * PT
    fs = lambda x: max(7.0, x * max(s, 0.85))  # noqa: E731
    panel_w = min(max(3.3, 0.21 * aw), 6.4) * PT
    gap = 0.3 * PT
    bottom = (0.95 + 0.25 * s) * PT  # room for the view title and graphic scale
    avail = (0.0, bottom, W - panel_w - gap, H - bottom)

    # data extent (local ft)
    origin = g.site_origin()
    if extent is not None:
        ex0, ey0, ex1, ey1 = extent
        ext = (origin[0] + ex0, origin[1] + ey0, origin[0] + ex1, origin[1] + ey1)
    else:
        ext = g.extent()
        if job is not None and getattr(job, "extent", None):
            ext = job.extent
    if center is not None:
        cxw, cyw = origin[0] + center[0], origin[1] + center[1]
    else:
        cxw, cyw = (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2
    ew, eh = ext[2] - ext[0], ext[3] - ext[1]
    if scale in (None, "", "auto", "fit"):
        ft_per_in = choose_scale(ew, eh, avail[2] / PT * 0.97, avail[3] / PT * 0.97)
    else:
        sc = parse_scale(scale)
        if sc is None:
            raise ValueError(f"not a plotting scale: {scale!r}")
        ft_per_in = sc.ratio / 12.0  # ratio = real/paper in the same unit
    scale_text = eng_scale(ft_per_in) if abs(ft_per_in - round(ft_per_in)) < 1e-9 else f'1" = {ft_per_in:.4g}\''
    # window: as much of the imagery as fits the available area at this scale (never past the imagery),
    # centred on the requested extent but slid back inside the imagery
    ix0, iy0, ix1, iy1 = g.extent()
    win_w = min(avail[2], (ix1 - ix0) / ft_per_in * PT)
    win_h = min(avail[3], (iy1 - iy0) / ft_per_in * PT)
    win_w, win_h = max(win_w, 3 * PT), max(win_h, 3 * PT)
    hw, hh = win_w / PT * ft_per_in / 2, win_h / PT * ft_per_in / 2
    if ix1 - ix0 >= 2 * hw:
        cxw = min(max(cxw, ix0 + hw), ix1 - hw)
    if iy1 - iy0 >= 2 * hh:
        cyw = min(max(cyw, iy0 + hh), iy1 - hh)
    win = (avail[0] + (avail[2] - win_w) / 2, avail[1] + (avail[3] - win_h) * 0.55, win_w, win_h)
    view = View(cxw, cyw, ft_per_in, win)

    out_pdf = Path(out) if out else _unique(pkg.root / f"{sheet}-{'job-plan' if job is not None else 'site-plan'}.pdf")
    out_dxf = Path(dxf_out) if dxf_out else _unique(out_pdf.with_suffix(".dxf"))
    sheet_obj = Sheet(W, H, s, fs, accent, primary)

    with tempfile.TemporaryDirectory(prefix="pdfcore-site-") as tmp:
        tmp = Path(tmp)
        draw_pdf = tmp / "drawing.pdf"
        c = canvas.Canvas(str(draw_pdf), pagesize=(W, H), pageCompression=1)
        c.setTitle(f"{sheet} {title}")
        pen = Pen(c, view, sheet_obj)
        wx, wy, ww, wh = win
        # --- plan window (clipped)
        c.saveState()
        p = c.beginPath()
        p.rect(wx, wy, ww, wh)
        c.clipPath(p, stroke=0, fill=0)
        has_underlay = bool(underlay)
        if has_underlay:
            jpg = underlay_image(pkg, g, view, opacity=float(underlay), color=underlay_color, dpi=165, out=tmp / "underlay.jpg")
            c.drawImage(str(jpg), wx, wy, ww, wh)
        drawn_ex = draw_existing(pen, feats, underlay=has_underlay)
        job_drawn: dict = {}
        if job is not None:
            from pdfcore.ops.plans.site.job import draw_job

            job_drawn = draw_job(pen, job)
        c.restoreState()
        # frame
        c.setStrokeColor(INK)
        c.setLineWidth(_lw(0.5))
        c.setDash([])
        c.rect(wx, wy, ww, wh, stroke=1, fill=0)
        # attribution strip inside the window, bottom-right
        attr = " · ".join(pkg.manifest.get("attribution", []))
        if attr:
            asz = fs(7)
            tw = pen.text_w(attr, asz)
            if tw > ww - 12:
                attr = "; ".join(a.split(" (")[0] for a in pkg.manifest.get("attribution", []))
                tw = pen.text_w(attr, asz)
            c.setFillColor("#FFFFFF")
            c.setFillAlpha(0.85)
            c.rect(wx + ww - tw - 8, wy + 1, tw + 7, asz + 4, stroke=0, fill=1)
            c.setFillAlpha(1)
            pen.text(wx + ww - 4, wy + 3.2, attr, asz, anchor="right", color="#333333", reserve=False, force=True)
        # view title + graphic scale under the window
        ty = wy - (0.42 + 0.05 * s) * PT
        view_title(pen, wx, ty, view_title_text or title, scale_text)
        sb_w = scale_bar(pen, wx + ww - 3.2 * PT, ty - 2, ft_per_in, 3.0 * PT)
        pen.text(wx + ww - 3.2 * PT, ty - 2 - fs(9), "GRAPHIC SCALE", fs(7.5), color="#444444", reserve=False, force=True)
        del sb_w

        # --- side panel
        px = W - panel_w
        pw = panel_w
        y = H
        conv = pkg.manifest.get("convergence_deg")
        na_size = min(1.15 * PT, 0.95 * PT * max(s, 0.8))
        north_arrow(pen, px + pw / 2, y - na_size * 0.75 - fs(14), na_size,
                    note="GRID NORTH" if not g.crs.is_local else ("NORTH (FROM IMAGE)" if g.north_deg == 0 else "TRUE NORTH"))
        y -= na_size * 1.6 + fs(14) + fs(9)
        # legend
        y = heading(pen, px, y, pw, "Legend")
        lsz = fs(8)
        sw_w, sw_h = 0.55 * PT, max(7.0, 8 * s)
        items = [(k, lbl) for k, lbl in LEGEND_ORDER if drawn_ex.get(k)]
        if has_underlay:
            items.append(("_img", "AERIAL IMAGERY (FADED, REFERENCE ONLY)"))
        for k, lbl in items:
            if k == "_img":
                c.setFillColor("#CFCFCF")
                c.rect(px, y - sw_h - 1, sw_w, sw_h, stroke=0, fill=1)
            else:
                legend_swatch(pen, k, px, y - sw_h - 1, sw_w, sw_h)
            for j, ln in enumerate(wrap(pen, lbl, lsz, pw - sw_w - 8)):
                pen.text(px + sw_w + 8, y - sw_h + 0.5 - j * lsz * 1.2, ln, lsz, reserve=False, force=True)
            y -= max(sw_h + 5, lsz * 1.25 * len(wrap(pen, lbl, lsz, pw - sw_w - 8)) + 3)
        if job is not None:
            from pdfcore.ops.plans.site.job import legend_items, quantities_table

            for draw_sw, lbl in legend_items(pen, job, job_drawn):
                draw_sw(px, y - sw_h - 1, sw_w, sw_h)
                lines = wrap(pen, lbl, lsz, pw - sw_w - 8)
                for j, ln in enumerate(lines):
                    pen.text(px + sw_w + 8, y - sw_h + 0.5 - j * lsz * 1.2, ln, lsz, reserve=False, force=True)
                y -= max(sw_h + 5, lsz * 1.25 * len(lines) + 3)
            y -= fs(8)
            y = quantities_table(pen, job, px, y, pw)
        y -= fs(10)
        if job is None:
            # site data: where the base came from and what was found (traceability)
            y = heading(pen, px, y, pw, "Site Data")
            x0_, y0_, x1_, y1_ = g.extent()
            cnt: dict[str, dict[str, int]] = {}
            for f in feats:
                k_ = f["properties"]["kind"]
                cnt.setdefault(k_, {}).setdefault(f["properties"].get("source", "?"), 0)
                cnt[k_][f["properties"].get("source", "?")] += 1
            def _c(k_, label):
                d = cnt.get(k_, {})
                return f"{sum(d.values())} {label} (" + ", ".join(f"{v} {s_}" for s_, v in sorted(d.items())) + ")" if d else None
            lines = [f"Base area {x1_ - x0_:,.0f} x {y1_ - y0_:,.0f} ft ({(x1_ - x0_) * (y1_ - y0_) / 43560:,.1f} ac)",
                     f"{pkg.manifest.get('imagery_source', 'user image')} — {g.ft_per_px:.2f} ft/px",
                     _c("building", "buildings"), _c("road", "road centerlines"),
                     f"Features: {fsrc or 'none'}; drawn {dt.date.today().isoformat()} by pdfcore plans site-plan"]
            dsz = fs(7.5)
            for ln in [l_ for l_ in lines if l_]:
                for w_ in wrap(pen, ln.upper(), dsz, pw):
                    pen.text(px, y - dsz, w_, dsz, reserve=False, force=True)
                    y -= dsz * 1.28
            y -= fs(10)
        # notes
        y = heading(pen, px, y, pw, "General Notes")
        attr_txt = "; ".join(pkg.manifest.get("attribution", [])) or "user-supplied image"
        crs_txt = f"{g.crs.name}" + (f" ({g.crs.crs})" if g.crs.crs.startswith("EPSG") else "")
        if g.crs.is_local:
            crs_txt = "local site grid in feet from image calibration (" + (g.method or "calibrated") + ")"
        conv_txt = f" (convergence {conv:+.2f}° from true north)" if conv not in (None, 0) and not g.crs.is_local else ""
        osm_txt = " and OpenStreetMap" if any(f["properties"].get("source") == "osm" for f in feats) else ""
        gn = [n.format(osm=osm_txt, crs=crs_txt, conv=conv_txt, attr=attr_txt) for n in GENERAL_NOTES]
        if restricted:
            gn.append("Base image is user-supplied Google imagery: reference only — not traced for this deliverable; "
                      "replace with licensed imagery before issue.")
        gn += list(notes)
        if job is not None:
            gn += list(getattr(job, "notes", []) or [])
        y = numbered_notes(pen, px, y, pw, gn, fs(8))
        if y < 0.2 * PT and not quiet:
            print("warning: side panel overflows the sheet — use a larger --size or fewer notes")
        c.showPage()
        c.save()

        # --- title block sheet
        import yaml

        data = {"sheet": sheet, "title": title, "project": project or pkg.manifest.get("matched") or pkg.root.name,
                "size": size_key, "scale": scale_text, "date": date or dt.date.today().isoformat(), "drawn": drawn,
                "checked": checked, "rev": rev, "client": client, "image": "drawing.pdf",
                "revisions": [{"rev": rev, "date": date or dt.date.today().isoformat(),
                               "description": "Issued for review" if job is None else "Issued for pricing / review"}]}
        yml = tmp / "sheet.yaml"
        yml.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        sheet_pdf = tmp / "sheet.pdf"
        make("drawing-sheet", yml, brand=brand, out=sheet_pdf, quiet=True)
        # --- /Measure viewport over the plan window (page points, top-left)
        Hpage = Hsh * PT
        vx0 = ax * PT + wx
        vx1 = vx0 + ww
        vtop = Hpage - (ay * PT + wy + wh)
        vbot = vtop + wh
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        with pikepdf.open(sheet_pdf) as pdf:
            from pdfcore.ops.plans.measure import scale_from_ratio

            add_viewport(pdf, 0, scale_from_ratio(ft_per_in * 12.0, "ft", tol=1e-6), (vx0, vtop, vx1, vbot),
                         name=f"{title} — {scale_text}")
            pdf.docinfo["/Title"] = f"{sheet} {title}"
            pdf.docinfo["/Subject"] = "Site plan" if job is None else "Job plan"
            pdf.docinfo["/Creator"] = "pdfcore plans site-plan"
            pdf.save(out_pdf)
    write_dxf(feats, out_dxf, g=g, image=pkg.image_path, job=job, text_ft=0.1 * ft_per_in)
    opts = GateOptions(profile="drawing", min_text_pt=7.0, min_image_dpi=150, require_metadata=True,
                       png_dir=Path(png_dir) if png_dir else None, png_dpi=100 if png_dir else 60)
    res = finalize(out_pdf, None, options=opts, extra=[out_dxf], quiet=quiet,
                   data={"scale": scale_text, "ft_per_in": ft_per_in, "viewport_pt": [vx0, vtop, vx1, vbot],
                         "window_world": list(view.world_box()), "features": fsrc, "drawn": drawn_ex, "job": job_drawn,
                         "dxf": str(out_dxf), "underlay": underlay})
    pkg.log("site-plan" if job is None else "site-add", pdf=out_pdf.name, dxf=out_dxf.name, scale=scale_text, size=size_key)
    pkg.save()
    return res
