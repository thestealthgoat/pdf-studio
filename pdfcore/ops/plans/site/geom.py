"""Planar geometry for site work in numpy (no GEOS): rings, polylines, simplification, orthogonalization,
rasterizing, mask → polygons, raster buffers, clipping.

Rings are lists of (x, y) without the closing duplicate unless noted; GeoJSON rings (closed) are accepted
everywhere and normalized with :func:`open_ring`.
"""

from __future__ import annotations

import math
from typing import Callable, Iterable, Sequence

import numpy as np

Pt = tuple[float, float]


# ----------------------------------------------------------------------------- basics
def open_ring(r: Sequence[Sequence[float]]) -> list[Pt]:
    pts = [(float(p[0]), float(p[1])) for p in r]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def close_ring(r: Sequence[Pt]) -> list[list[float]]:
    pts = [[float(p[0]), float(p[1])] for p in r]
    if pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts


def signed_area(r: Sequence[Pt]) -> float:
    p = np.asarray(open_ring(r), float)
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return float(0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def area(r: Sequence[Pt]) -> float:
    return abs(signed_area(r))


def polygon_area(rings: Sequence[Sequence[Pt]]) -> float:
    if not rings:
        return 0.0
    return max(0.0, area(rings[0]) - sum(area(h) for h in rings[1:]))


def length(pts: Sequence[Pt], closed: bool = False) -> float:
    p = np.asarray([(float(a[0]), float(a[1])) for a in pts], float)
    if len(p) < 2:
        return 0.0
    if closed:
        p = np.vstack([p, p[:1]])
    return float(np.hypot(*np.diff(p, axis=0).T).sum())


def centroid(r: Sequence[Pt]) -> Pt:
    p = np.asarray(open_ring(r), float)
    a = signed_area(p.tolist())
    if abs(a) < 1e-9:
        return (float(p[:, 0].mean()), float(p[:, 1].mean()))
    x, y = p[:, 0], p[:, 1]
    xn, yn = np.roll(x, -1), np.roll(y, -1)
    cr = x * yn - xn * y
    return (float(((x + xn) * cr).sum() / (6 * a)), float(((y + yn) * cr).sum() / (6 * a)))


def point_in_ring(pt: Pt, r: Sequence[Pt]) -> bool:
    x, y = pt
    p = open_ring(r)
    inside = False
    j = len(p) - 1
    for i in range(len(p)):
        xi, yi = p[i]
        xj, yj = p[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def interior_point(r: Sequence[Pt]) -> Pt:
    """A point inside the ring (centroid when it is inside, else a scanline midpoint)."""
    c = centroid(r)
    if point_in_ring(c, r):
        return c
    p = np.asarray(open_ring(r))
    y = float(np.median(p[:, 1]))
    xs = []
    for (x1, y1), (x2, y2) in zip(p, np.roll(p, -1, axis=0)):
        if (y1 > y) != (y2 > y):
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    if len(xs) >= 2:
        k = max(range(0, len(xs) - 1, 2), key=lambda i: xs[i + 1] - xs[i])
        return ((xs[k] + xs[k + 1]) / 2, y)
    return c


def point_segment_distance(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Distance from points *p* (N,2) to segment a–b."""
    ab = b - a
    t = np.clip(((p - a) @ ab) / max(float(ab @ ab), 1e-12), 0, 1)
    q = a + t[:, None] * ab
    return np.hypot(*(p - q).T)


def distance_to_polylines(pts: Sequence[Pt], lines: Iterable[Sequence[Pt]]) -> np.ndarray:
    P = np.asarray(pts, float)
    best = np.full(len(P), np.inf)
    for ln in lines:
        L = np.asarray(ln, float)
        for a, b in zip(L[:-1], L[1:]):
            best = np.minimum(best, point_segment_distance(P, a, b))
    return best


def resample(pts: Sequence[Pt], step: float) -> list[Pt]:
    p = np.asarray(pts, float)
    if len(p) < 2:
        return [tuple(x) for x in p]
    d = np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
    n = max(2, int(d[-1] / step) + 1)
    s = np.linspace(0, d[-1], n)
    return list(zip(np.interp(s, d, p[:, 0]), np.interp(s, d, p[:, 1])))


def point_at(pts: Sequence[Pt], frac: float) -> tuple[Pt, float]:
    """Point at *frac* of the polyline length and the direction (deg) there."""
    p = np.asarray(pts, float)
    seg = np.hypot(*np.diff(p, axis=0).T)
    target = frac * seg.sum()
    acc = 0.0
    for i, sl in enumerate(seg):
        if acc + sl >= target or i == len(seg) - 1:
            t = 0 if sl == 0 else (target - acc) / sl
            a, b = p[i], p[i + 1]
            q = a + t * (b - a)
            return (float(q[0]), float(q[1])), math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        acc += sl
    return (float(p[0, 0]), float(p[0, 1])), 0.0


# ----------------------------------------------------------------------------- simplification
def simplify(pts: Sequence[Pt], tol: float, closed: bool = False) -> list[Pt]:
    """Douglas–Peucker. Closed rings are split at the two mutually farthest vertices."""
    P = np.asarray(open_ring(pts) if closed else [(float(a[0]), float(a[1])) for a in pts], float)
    if len(P) < 3 or tol <= 0:
        return [tuple(map(float, x)) for x in P]

    def dp(Q: np.ndarray) -> np.ndarray:
        keep = np.zeros(len(Q), bool)
        keep[0] = keep[-1] = True
        stack = [(0, len(Q) - 1)]
        while stack:
            i, j = stack.pop()
            if j <= i + 1:
                continue
            d = point_segment_distance(Q[i + 1:j], Q[i], Q[j])
            k = int(np.argmax(d))
            if d[k] > tol:
                m = i + 1 + k
                keep[m] = True
                stack += [(i, m), (m, j)]
        return Q[keep]

    if not closed:
        return [tuple(map(float, x)) for x in dp(P)]
    i0 = 0
    i1 = int(np.argmax(np.hypot(*(P - P[0]).T)))
    i0 = int(np.argmax(np.hypot(*(P - P[i1]).T)))
    a, b = sorted((i0, i1))
    A = dp(P[a:b + 1])
    B = dp(np.vstack([P[b:], P[:a + 1]]))
    out = np.vstack([A[:-1], B[:-1]])
    return [tuple(map(float, x)) for x in out]


def chaikin(pts: Sequence[Pt], n: int = 2, closed: bool = False) -> list[Pt]:
    P = np.asarray(pts, float)
    for _ in range(n):
        if len(P) < 3:
            break
        if closed:
            Q = np.roll(P, -1, axis=0)
            P = np.column_stack([0.75 * P + 0.25 * Q, 0.25 * P + 0.75 * Q]).reshape(-1, 2)
        else:
            Q = P[1:]
            R = P[:-1]
            mid = np.column_stack([0.75 * R + 0.25 * Q, 0.25 * R + 0.75 * Q]).reshape(-1, 2)
            P = np.vstack([P[:1], mid, P[-1:]])
    return [tuple(map(float, x)) for x in P]


def dominant_angle(r: Sequence[Pt]) -> float:
    """Dominant edge direction modulo 90° (radians), length-weighted."""
    p = np.asarray(open_ring(r), float)
    d = np.roll(p, -1, axis=0) - p
    L = np.hypot(d[:, 0], d[:, 1])
    th = np.arctan2(d[:, 1], d[:, 0])
    # 4θ trick: average of directions modulo 90°
    c = (L * np.cos(4 * th)).sum()
    s = (L * np.sin(4 * th)).sum()
    return float(math.atan2(s, c) / 4)


def rectilinearity(r: Sequence[Pt]) -> float:
    """Share of the perimeter within 12° of the dominant direction or its perpendicular (0–1)."""
    p = np.asarray(open_ring(r), float)
    if len(p) < 3:
        return 0.0
    a = dominant_angle(r)
    d = np.roll(p, -1, axis=0) - p
    L = np.hypot(d[:, 0], d[:, 1])
    th = np.arctan2(d[:, 1], d[:, 0]) - a
    dev = np.abs(((th + math.pi / 4) % (math.pi / 2)) - math.pi / 4)
    return float(L[dev < math.radians(12)].sum() / max(L.sum(), 1e-9))


def orthogonalize(r: Sequence[Pt], max_dev_deg: float = 18.0, min_edge: float = 0.0) -> list[Pt] | None:
    """Square a near-rectilinear ring: every edge snapped to the dominant axis or its perpendicular,
    vertices re-solved as intersections. ``None`` if any long edge is off-axis by more than *max_dev_deg*."""
    p = np.asarray(open_ring(r), float)
    if len(p) < 4:
        return None
    a = dominant_angle(r)
    ca, sa = math.cos(-a), math.sin(-a)
    R = np.array([[ca, -sa], [sa, ca]])
    q = p @ R.T  # rotate so the dominant direction is the x axis
    n = len(q)
    d = np.roll(q, -1, axis=0) - q
    L = np.hypot(d[:, 0], d[:, 1])
    th = np.degrees(np.arctan2(d[:, 1], d[:, 0]))
    horiz = np.abs(np.sin(np.radians(th))) < np.abs(np.cos(np.radians(th)))
    dev = np.where(horiz, np.degrees(np.arcsin(np.minimum(1, np.abs(np.sin(np.radians(th)))))),
                   np.degrees(np.arcsin(np.minimum(1, np.abs(np.cos(np.radians(th)))))))
    long = L >= max(min_edge, 0.08 * L.sum() / 4)
    if (dev[long] > max_dev_deg).any():
        return None
    # merge consecutive edges with the same orientation into runs; each run is one axis-aligned line
    start = int(np.argmax(horiz != np.roll(horiz, 1))) if (horiz != np.roll(horiz, 1)).any() else 0
    runs: list[tuple[bool, float]] = []
    idx = [(start + k) % n for k in range(n)]
    cur_h, acc_w, acc_v = horiz[idx[0]], 0.0, 0.0
    for k in idx:
        if horiz[k] != cur_h:
            runs.append((bool(cur_h), acc_v / max(acc_w, 1e-9)))
            cur_h, acc_w, acc_v = horiz[k], 0.0, 0.0
        mid = (q[k] + q[(k + 1) % n]) / 2
        w = L[k] + 1e-9
        acc_w += w
        acc_v += w * (mid[1] if horiz[k] else mid[0])
    runs.append((bool(cur_h), acc_v / max(acc_w, 1e-9)))
    if len(runs) > 1 and runs[0][0] == runs[-1][0]:
        h, v1 = runs.pop()
        runs[0] = (h, (runs[0][1] + v1) / 2)
    if len(runs) < 4 or len(runs) % 2:
        return None
    pts = []
    for k in range(len(runs)):
        h1, v1 = runs[k]
        h2, v2 = runs[(k + 1) % len(runs)]
        if h1 == h2:
            return None
        x, y = (v2, v1) if h1 else (v1, v2)
        pts.append((x, y))
    out = np.asarray(pts) @ R  # rotate back (R is orthonormal; inverse = transpose)
    res = [tuple(map(float, x)) for x in out]
    if area(res) < 0.5 * area(p.tolist()) or area(res) > 1.5 * area(p.tolist()):
        return None
    return res


def min_rect(r: Sequence[Pt]) -> tuple[float, float, float]:
    """Minimum-area bounding rectangle: (area, long side, short side)."""
    p = np.asarray(open_ring(r), float)
    hull = convex_hull(p)
    best = (math.inf, 0.0, 0.0)
    for i in range(len(hull)):
        e = hull[(i + 1) % len(hull)] - hull[i]
        le = math.hypot(*e)
        if le == 0:
            continue
        u = e / le
        v = np.array([-u[1], u[0]])
        pu, pv = hull @ u, hull @ v
        w, h = pu.max() - pu.min(), pv.max() - pv.min()
        if w * h < best[0]:
            best = (w * h, max(w, h), min(w, h))
    return best


def convex_hull(p: np.ndarray) -> np.ndarray:
    pts = sorted(set(map(tuple, np.asarray(p, float))))
    if len(pts) < 3:
        return np.asarray(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for q in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    for q in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return np.asarray(lower[:-1] + upper[:-1])


# ----------------------------------------------------------------------------- clipping
def clip_polyline(pts: Sequence[Pt], box: Sequence[float]) -> list[list[Pt]]:
    """Liang–Barsky clip of a polyline to (x0, y0, x1, y1) → list of pieces."""
    x0, y0, x1, y1 = box
    pieces: list[list[Pt]] = []
    cur: list[Pt] = []
    for a, b in zip(pts[:-1], pts[1:]):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        ok = True
        for pp, qq in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if pp == 0:
                if qq < 0:
                    ok = False
                    break
            else:
                t = qq / pp
                if pp < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if not ok or t0 > t1:
            if cur:
                pieces.append(cur)
                cur = []
            continue
        pa = (ax + t0 * dx, ay + t0 * dy)
        pb = (ax + t1 * dx, ay + t1 * dy)
        if not cur:
            cur = [pa]
        elif math.hypot(cur[-1][0] - pa[0], cur[-1][1] - pa[1]) > 1e-9:
            pieces.append(cur)
            cur = [pa]
        cur.append(pb)
        if t1 < 1.0:
            pieces.append(cur)
            cur = []
    if cur:
        pieces.append(cur)
    return [pc for pc in pieces if len(pc) >= 2 and length(pc) > 1e-6]


def clip_ring(r: Sequence[Pt], box: Sequence[float]) -> list[Pt]:
    """Sutherland–Hodgman clip of a ring to a rectangle."""
    x0, y0, x1, y1 = box
    out = open_ring(r)
    for inside, inter in (
        (lambda p: p[0] >= x0, lambda a, b: (x0, a[1] + (b[1] - a[1]) * (x0 - a[0]) / (b[0] - a[0]))),
        (lambda p: p[0] <= x1, lambda a, b: (x1, a[1] + (b[1] - a[1]) * (x1 - a[0]) / (b[0] - a[0]))),
        (lambda p: p[1] >= y0, lambda a, b: (a[0] + (b[0] - a[0]) * (y0 - a[1]) / (b[1] - a[1]), y0)),
        (lambda p: p[1] <= y1, lambda a, b: (a[0] + (b[0] - a[0]) * (y1 - a[1]) / (b[1] - a[1]), y1)),
    ):
        src, out = out, []
        if not src:
            break
        prev = src[-1]
        for cur in src:
            if inside(cur):
                if not inside(prev):
                    out.append(inter(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(inter(prev, cur))
            prev = cur
    return out


def bbox_of(pts: Iterable[Sequence[float]]) -> tuple[float, float, float, float]:
    a = np.asarray(list(pts), float)
    return (float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 0].max()), float(a[:, 1].max()))


# ----------------------------------------------------------------------------- raster <-> vector
class Grid:
    """A north-up raster over (x0, y0, x1, y1) at *res* ft/px for rasterizing vectors."""

    def __init__(self, extent: Sequence[float], res: float):
        self.x0, self.y0, self.x1, self.y1 = map(float, extent)
        self.res = float(res)
        self.W = max(1, int(math.ceil((self.x1 - self.x0) / res)))
        self.H = max(1, int(math.ceil((self.y1 - self.y0) / res)))

    def to_px(self, pts: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
        return [((p[0] - self.x0) / self.res, (self.y1 - p[1]) / self.res) for p in pts]

    def to_xy(self, col: float, row: float) -> Pt:
        return (self.x0 + col * self.res, self.y1 - row * self.res)


def rasterize(polys: Iterable[Sequence[Sequence[Pt]]], shape: tuple[int, int], to_px: Callable) -> np.ndarray:
    """Polygons (list of rings: outer, holes…) → bool mask of *shape* (H, W)."""
    from PIL import Image, ImageDraw

    H, W = shape
    im = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(im)
    for rings in polys:
        if not rings:
            continue
        outer = to_px(open_ring(rings[0]))
        if len(outer) >= 3:
            dr.polygon(outer, fill=255)
        for h in rings[1:]:
            hp = to_px(open_ring(h))
            if len(hp) >= 3:
                dr.polygon(hp, fill=0)
    return np.asarray(im) > 0


def rasterize_lines(lines: Iterable[tuple[Sequence[Pt], float]], shape: tuple[int, int], to_px: Callable,
                    px_per_unit: float) -> np.ndarray:
    """Polylines with widths (units) → bool mask, round joins/caps."""
    from PIL import Image, ImageDraw

    H, W = shape
    im = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(im)
    for pts, width in lines:
        pp = to_px(pts)
        w = max(1.0, width * px_per_unit)
        if len(pp) >= 2:
            dr.line(pp, fill=255, width=int(round(w)), joint="curve")
        rr = w / 2
        for x, y in (pp[0], pp[-1]):
            dr.ellipse((x - rr, y - rr, x + rr, y + rr), fill=255)
    return np.asarray(im) > 0


def mask_to_polygons(mask: np.ndarray, to_xy: Callable[[float, float], Pt], *, tol_px: float = 1.0,
                     min_area_px: float = 4.0, min_hole_px: float | None = None, smooth: bool = False) -> list[list[list[Pt]]]:
    """Bool mask → polygons [[outer, hole, …], …] in world coordinates (via *to_xy(col, row)*)."""
    from scipy import ndimage as ndi
    from skimage import measure

    min_hole_px = min_area_px if min_hole_px is None else min_hole_px
    lab, n = ndi.label(mask)
    out = []
    objs = ndi.find_objects(lab)
    for k, sl in enumerate(objs, 1):
        if sl is None:
            continue
        r0, c0 = sl[0].start - 1, sl[1].start - 1
        sub = np.pad(lab[sl] == k, 1)
        if sub.sum() < min_area_px:
            continue
        filled = ndi.binary_fill_holes(sub)
        src = filled.astype(float)
        if smooth:
            src = ndi.gaussian_filter(src, 0.7)
        cs = measure.find_contours(src, 0.5)
        if not cs:
            continue
        c = max(cs, key=len)
        ring = simplify([(p[1], p[0]) for p in c], tol_px, closed=True)
        if len(ring) < 3 or area(ring) < min_area_px:
            continue
        rings = [[to_xy(x + c0 + 0.5, y + r0 + 0.5) for x, y in ring]]
        holes = filled & ~sub
        if holes.any():
            hl, hn = ndi.label(holes)
            for j in range(1, hn + 1):
                hm = hl == j
                if hm.sum() < min_hole_px:
                    continue
                hc = measure.find_contours(np.pad(hm, 1).astype(float), 0.5)
                if hc:
                    hr = simplify([(p[1] - 1, p[0] - 1) for p in max(hc, key=len)], tol_px, closed=True)
                    if len(hr) >= 3:
                        rings.append([to_xy(x + c0 + 0.5, y + r0 + 0.5) for x, y in hr])
        out.append(rings)
    return out


def buffer_lines(lines: Sequence[tuple[Sequence[Pt], float]], extent: Sequence[float], res: float = 0.5,
                 tol: float = 0.4) -> list[list[list[Pt]]]:
    """Union of polylines buffered to their widths (ft) → polygons (raster method, *res* ft/px)."""
    g = Grid(extent, res)
    m = rasterize_lines(lines, (g.H, g.W), g.to_px, 1.0 / res)
    return mask_to_polygons(m, g.to_xy, tol_px=tol / res, min_area_px=4, smooth=True)


def iou(a: np.ndarray, b: np.ndarray) -> float:
    u = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / u) if u else 1.0
