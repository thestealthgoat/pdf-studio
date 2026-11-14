"""Raster primitives for site imagery in plain numpy (no scipy, no scikit-image).

Written from the textbook algorithms; every loop that runs over a full image is vectorized.

filters       :func:`uniform_filter` (box, cumulative sums), :func:`gaussian_filter` (separable, truncated at
              4 σ); borders are half-sample symmetric (``d c b a | a b c d``)
threshold     :func:`threshold_otsu` (maximum between-class variance on a 256-bin histogram)
morphology    :func:`binary_dilation`, :func:`binary_erosion`, :func:`binary_opening`, :func:`binary_closing`
              with flat structuring elements (:func:`disk`, :func:`square`, :func:`cross`), decomposed into
              row runs. Outside the image counts as background, so an erosion (and therefore a closing)
              clears pixels whose element reaches past the border.
              :func:`fill_holes` (background not 4-connected to the border)
labeling      :func:`label` / :func:`label_values` (row runs + union–find with hooking and pointer jumping;
              4- or 8-connectivity; labels numbered in raster order of their first pixel), :func:`find_objects`
distance      :func:`distance_transform_edt`: exact Euclidean distance to the nearest background pixel (column
              pass, then the lower envelope of parabolas along rows, solved for all rows at once)
skeleton      :func:`skeletonize`: Zhang–Suen parallel thinning, then removal of staircase pixels whose
              removal keeps topology (Yokoi 8-connectivity number 1), giving an 8-thin skeleton;
              :func:`neighbour_count`
contours      :func:`find_contours`: marching squares with linear interpolation; saddles keep the high corners
              apart (above-level pixels are 4-connected), contours are linked by pointer jumping
shape         :func:`solidity` (area / pixel area of the convex hull of the pixel squares)
segmentation  :func:`felzenszwalb`: graph-based segmentation (Felzenszwalb & Huttenlocher 2004) over the
              8-neighbour colour-distance graph, merging in batches of edges of increasing weight;
              :func:`merge_regions`: region adjacency graph on mean colour, cut at a threshold
resampling    :func:`resize_nearest`, :func:`rescale` (Pillow, anti-aliased), :func:`sample_bilinear`
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


# ----------------------------------------------------------------------------- structuring elements
def disk(r: float) -> np.ndarray:
    """Pixels within *r* of the centre (side 2r + 1)."""
    r = int(r)
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def square(n: int) -> np.ndarray:
    return np.ones((n, n), bool)


def cross() -> np.ndarray:
    """3×3 plus sign (4-connectivity)."""
    return np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool)


def _se(se) -> np.ndarray:
    if se is None:
        return cross()
    se = np.asarray(se, bool)
    if se.ndim != 2 or se.shape[0] % 2 == 0 or se.shape[1] % 2 == 0:
        raise ValueError("structuring element must be 2-D with odd sides")
    return se


def _runs1d(row: np.ndarray) -> list[tuple[int, int]]:
    """(first, last) index of each run of True."""
    d = np.diff(np.r_[0, row.astype(np.int8), 0])
    return list(zip(np.flatnonzero(d == 1).tolist(), (np.flatnonzero(d == -1) - 1).tolist()))


def _sweep(m: np.ndarray, se: np.ndarray, outside: bool) -> np.ndarray:
    """out[p] = OR over centre-relative offsets s in *se* of M[p − s], M = *m* extended by *outside*."""
    H, W = m.shape
    ry, rx = se.shape[0] // 2, se.shape[1] // 2
    P = np.pad(m, ((ry, ry), (rx, rx)), constant_values=outside)
    out = np.zeros((H, W), bool)
    if se.sum() <= 9:
        for i, j in zip(*np.nonzero(se)):
            dy, dx = int(i) - ry, int(j) - rx
            out |= P[ry - dy:ry - dy + H, rx - dx:rx - dx + W]
        return out
    cs = np.zeros((P.shape[0], P.shape[1] + 1), np.int32)
    np.cumsum(P, axis=1, out=cs[:, 1:])
    cache: dict[tuple[int, int], np.ndarray] = {}
    for i in range(se.shape[0]):
        dy = i - ry
        for a, b in _runs1d(se[i]):
            h = cache.get((a, b))
            if h is None:  # any(P[y, x + 2rx − b … x + 2rx − a]) for every padded row
                lo, hi = 2 * rx - b, 2 * rx - a + 1
                h = (cs[:, hi:hi + W] - cs[:, lo:lo + W]) > 0
                cache[(a, b)] = h
            out |= h[ry - dy:ry - dy + H]
    return out


def _se_power(se: np.ndarray, n: int) -> np.ndarray:
    """se ⊕ se ⊕ … (*n* times): one dilation with it equals *n* iterated dilations."""
    out = se
    ry, rx = se.shape[0] // 2, se.shape[1] // 2
    for _ in range(n - 1):
        out = _sweep(np.pad(out, ((ry, ry), (rx, rx))), se, False)
    return out


def binary_dilation(m: np.ndarray, se=None, iterations: int = 1) -> np.ndarray:
    m = np.asarray(m, bool)
    se = _se(se)
    if iterations > 1:
        se = _se_power(se, iterations)
    if not m.any():
        return np.zeros_like(m)
    return _sweep(m, se, False)


def binary_erosion(m: np.ndarray, se=None, iterations: int = 1) -> np.ndarray:
    """Pixels whose whole element (placed on them) is True; outside the image counts as False."""
    m = np.asarray(m, bool)
    se = _se(se)
    if iterations > 1:
        se = _se_power(se, iterations)
    if not m.any():
        return np.zeros_like(m)
    return ~_sweep(~m, se[::-1, ::-1], True)


def binary_opening(m: np.ndarray, se=None) -> np.ndarray:
    return binary_dilation(binary_erosion(m, se), se)


def binary_closing(m: np.ndarray, se=None) -> np.ndarray:
    return binary_erosion(binary_dilation(m, se), se)


# ----------------------------------------------------------------------------- filters
def _take(a: np.ndarray, start: int, n: int, axis: int) -> np.ndarray:
    sl = [slice(None)] * a.ndim
    sl[axis] = slice(start, start + n)
    return a[tuple(sl)]


def _pad_axis(a: np.ndarray, before: int, after: int, axis: int) -> np.ndarray:
    pw = [(0, 0)] * a.ndim
    pw[axis] = (before, after)
    return np.pad(a, pw, mode="symmetric")


def uniform_filter(a: np.ndarray, size: int | Sequence[int]) -> np.ndarray:
    """Mean over a *size* window (per axis) centred on each pixel; float input keeps its dtype."""
    a = np.asarray(a)
    dt = a.dtype if a.dtype.kind == "f" else np.float64
    sizes = [size] * a.ndim if np.isscalar(size) else list(size)
    out = a.astype(np.float64)
    for ax, w in enumerate(sizes):
        w = int(w)
        if w <= 1:
            continue
        left = w // 2
        n = out.shape[ax]
        p = _pad_axis(out, left, w - 1 - left, ax)
        c = np.cumsum(p, axis=ax)
        zero_shape = list(c.shape)
        zero_shape[ax] = 1
        c = np.concatenate([np.zeros(zero_shape), c], axis=ax)
        out = (_take(c, w, n, ax) - _take(c, 0, n, ax)) / w
    return out.astype(dt, copy=False)


def gaussian_kernel(sigma: float, truncate: float = 4.0) -> np.ndarray:
    r = int(truncate * sigma + 0.5)
    x = np.arange(-r, r + 1, dtype=np.float64)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    return k / k.sum()


def gaussian_filter(a: np.ndarray, sigma: float | Sequence[float], truncate: float = 4.0) -> np.ndarray:
    """Separable Gaussian blur (σ per axis; 0 skips an axis); float input keeps its dtype."""
    a = np.asarray(a)
    dt = a.dtype if a.dtype.kind == "f" else np.float64
    sig = [float(sigma)] * a.ndim if np.isscalar(sigma) else [float(s) for s in sigma]
    out = a.astype(np.float64)
    for ax, s in enumerate(sig):
        if s <= 0:
            continue
        k = gaussian_kernel(s, truncate)
        r = len(k) // 2
        n = out.shape[ax]
        p = _pad_axis(out, r, r, ax)
        acc = k[0] * _take(p, 0, n, ax)
        for j in range(1, len(k)):
            acc += k[j] * _take(p, j, n, ax)
        out = acc
    return out.astype(dt, copy=False)


# ----------------------------------------------------------------------------- threshold
def threshold_otsu(v: np.ndarray, nbins: int = 256) -> float:
    """Otsu's threshold: values above it are the upper class."""
    v = np.asarray(v, np.float64).ravel()
    v = v[np.isfinite(v)]
    if v.size == 0:
        raise ValueError("no finite values")
    lo, hi = float(v.min()), float(v.max())
    if lo == hi:
        return lo
    hist, edges = np.histogram(v, bins=nbins, range=(lo, hi))
    centres = (edges[:-1] + edges[1:]) / 2
    hist = hist.astype(np.float64)
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centres)
    with np.errstate(invalid="ignore", divide="ignore"):
        mu0 = m0 / w0
        mu1 = (m0[-1] - m0) / w1
        between = w0[:-1] * w1[:-1] * (mu0[:-1] - mu1[:-1]) ** 2
    between = np.nan_to_num(between, nan=-1.0)
    return float(centres[int(np.argmax(between))])


# ----------------------------------------------------------------------------- union–find (vectorized)
def _compress(parent: np.ndarray) -> np.ndarray:
    while True:
        pp = parent[parent]
        if np.array_equal(pp, parent):
            return parent
        parent = pp


def _union(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Connected components of n nodes under edges a–b → fully compressed parent (root = smallest index)."""
    parent = np.arange(n)
    a = np.asarray(a, np.int64)
    b = np.asarray(b, np.int64)
    while a.size:
        pa, pb = parent[a], parent[b]
        keep = pa != pb
        if not keep.any():
            break
        a, b, pa, pb = a[keep], b[keep], pa[keep], pb[keep]
        np.minimum.at(parent, np.maximum(pa, pb), np.minimum(pa, pb))  # hook root onto the smaller root
        parent = _compress(parent)
    return parent


# ----------------------------------------------------------------------------- labeling
def _label_runs(v: np.ndarray, connectivity: int) -> tuple[np.ndarray, int]:
    H, W = v.shape
    lab = np.zeros((H, W), np.int32)
    nz = v != 0
    if not nz.any():
        return lab, 0
    new = np.ones((H, W), bool)
    new[:, 1:] = v[:, 1:] != v[:, :-1]
    last = np.ones((H, W), bool)
    last[:, :-1] = v[:, :-1] != v[:, 1:]
    st = np.flatnonzero(nz & new)
    en = np.flatnonzero(nz & last)
    val = v.ravel()[st]
    r = st // W
    c0, c1 = st - r * W, en - r * W
    K = W + 4
    skey, ekey = r * K + c0 + 2, r * K + c1 + 2
    c = 1 if connectivity == 2 else 0
    j = np.flatnonzero(r > 0)
    lo = np.searchsorted(ekey, (r[j] - 1) * K + c0[j] + 2 - c, "left")  # runs above ending at/after c0 − c
    hi = np.searchsorted(skey, (r[j] - 1) * K + c1[j] + 2 + c, "right")  # … and starting at/before c1 + c
    cnt = np.maximum(hi - lo, 0)
    tot = int(cnt.sum())
    J = np.repeat(j, cnt)
    I = np.repeat(lo, cnt) + (np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt))
    same = val[I] == val[J]
    parent = _union(len(st), I[same], J[same])
    root = parent == np.arange(len(st))
    run_label = np.cumsum(root).astype(np.int32)[parent]
    acc = np.zeros(H * W + 1, np.int32)
    acc[st] += run_label
    acc[en + 1] -= run_label
    lab = np.cumsum(acc[:-1], dtype=np.int32).reshape(H, W)
    return lab, int(root.sum())


def label(m: np.ndarray, connectivity: int = 1) -> tuple[np.ndarray, int]:
    """Connected components of a bool mask (1 = 4-connected, 2 = 8-connected) → (labels 1…n, n)."""
    return _label_runs(np.asarray(m, bool), connectivity)


def label_values(a: np.ndarray, connectivity: int = 1) -> tuple[np.ndarray, int]:
    """Connected regions of equal non-zero value → (labels 1…n, n)."""
    return _label_runs(np.asarray(a), connectivity)


def find_objects(lab: np.ndarray, n: int | None = None) -> list[tuple[slice, slice] | None]:
    """Bounding-box slices of labels 1…n (``None`` for absent labels); index i − 1 is label i."""
    H, W = lab.shape
    flat = lab.ravel()
    nz = np.flatnonzero(flat)
    if n is None:
        n = int(flat[nz].max()) if nz.size else 0
    out: list[tuple[slice, slice] | None] = [None] * n
    if not nz.size or n == 0:
        return out
    lv = flat[nz]
    sel = lv <= n
    nz, lv = nz[sel], lv[sel]
    r = nz // W
    cc = nz - r * W
    rmin = np.full(n + 1, H)
    cmin = np.full(n + 1, W)
    rmax = np.full(n + 1, -1)
    cmax = np.full(n + 1, -1)
    np.minimum.at(rmin, lv, r)
    np.maximum.at(rmax, lv, r)
    np.minimum.at(cmin, lv, cc)
    np.maximum.at(cmax, lv, cc)
    for i in np.flatnonzero(rmax[1:] >= 0) + 1:
        out[i - 1] = (slice(int(rmin[i]), int(rmax[i]) + 1), slice(int(cmin[i]), int(cmax[i]) + 1))
    return out


def fill_holes(m: np.ndarray) -> np.ndarray:
    """Mask plus every background region not 4-connected to the image border."""
    m = np.asarray(m, bool)
    lab, n = label(~m, 1)
    if n == 0:
        return m.copy()
    outer = np.zeros(n + 1, bool)
    for edge in (lab[0], lab[-1], lab[:, 0], lab[:, -1]):
        outer[edge] = True
    outer[0] = True
    return m | ~outer[lab]


def remove_small(m: np.ndarray, min_px: float, connectivity: int = 1) -> np.ndarray:
    lab, n = label(m, connectivity)
    if n == 0:
        return np.asarray(m, bool).copy()
    keep = np.bincount(lab.ravel(), minlength=n + 1) >= min_px
    keep[0] = False
    return keep[lab]


# ----------------------------------------------------------------------------- distance transform
def _lower_envelope(f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per row: d[q] = min_p (q − p)² + f[p] and its argmin (f = inf where p is not a site)."""
    R, n = f.shape
    v = np.zeros((R, n), np.int64)
    z = np.full((R, n + 1), np.inf)
    k = np.full(R, -1, np.int64)
    fin = np.isfinite(f)
    with np.errstate(divide="ignore", invalid="ignore"):
        for q in range(n):
            act = np.flatnonzero(fin[:, q])
            if not act.size:
                continue
            fq = f[act, q] + q * q
            ka = k[act]
            s = np.full(act.size, -np.inf)
            cur = np.flatnonzero(ka >= 0)
            while cur.size:
                rr, kk = act[cur], ka[cur]
                vk = v[rr, kk]
                sc = (fq[cur] - (f[rr, vk] + vk * vk)) / (2.0 * (q - vk))  # intersection with the top parabola
                s[cur] = sc
                pop = sc <= z[rr, kk]
                cur = cur[pop]
                ka[cur] -= 1
                cur = cur[ka[cur] >= 0]
            ka += 1
            v[act, ka] = q
            z[act, ka] = np.where(ka == 0, -np.inf, s)
            z[act, ka + 1] = np.inf
            k[act] = ka
    rows = np.arange(R)
    k = np.zeros(R, np.int64)
    d = np.empty((R, n))
    arg = np.empty((R, n), np.int64)
    for q in range(n):
        while True:
            adv = z[rows, k + 1] < q
            if not adv.any():
                break
            k += adv
        vk = v[rows, k]
        arg[:, q] = vk
        d[:, q] = (q - vk) ** 2 + f[rows, vk]
    return d, arg


def distance_transform_edt(m: np.ndarray, return_indices: bool = False):
    """Exact Euclidean distance from each True pixel to the nearest False pixel (0 on False pixels).

    With *return_indices*, also ``(rows, cols)`` of that nearest False pixel."""
    m = np.asarray(m, bool)
    H, W = m.shape
    site = ~m
    if not site.any():
        d = np.full((H, W), np.inf)
        if return_indices:
            return d, (np.zeros((H, W), np.int64), np.zeros((H, W), np.int64))
        return d
    rows = np.arange(H)[:, None]
    big = 4 * (H + W) + 10
    above = np.maximum.accumulate(np.where(site, rows, -big), axis=0)
    below = np.minimum.accumulate(np.where(site, rows, big)[::-1], axis=0)[::-1]
    near = np.where(rows - above <= below - rows, above, below)
    g = np.abs(rows - near).astype(np.float64)
    f = g * g
    f[:, ~site.any(0)] = np.inf
    d2, arg = _lower_envelope(f)
    d = np.sqrt(d2)
    if return_indices:
        return d, (near[np.arange(H)[:, None], arg], arg)
    return d


# ----------------------------------------------------------------------------- skeleton
# 8 neighbours clockwise from north: N, NE, E, SE, S, SW, W, NW (bit i of the neighbourhood code)
_NB = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]


def _thinning_tables() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    code = np.arange(256)
    b = (code[:, None] >> np.arange(8)) & 1
    N, NE, E, SE, S, SW, W, NW = b.T
    B = b.sum(1)
    A = ((b == 0) & (np.roll(b, -1, axis=1) == 1)).sum(1)  # 0 → 1 transitions around the ring
    base = (B >= 2) & (B <= 6) & (A == 1)
    first = base & (N * E * S == 0) & (E * S * W == 0)
    second = base & (N * E * W == 0) & (N * S * W == 0)
    # Yokoi 8-connectivity number with x1…x8 = E, NE, N, NW, W, SW, S, SE
    x = [E, NE, N, NW, W, SW, S, SE]
    xb = [1 - t for t in x]
    yokoi = sum(xb[i] - xb[i] * xb[(i + 1) % 8] * xb[(i + 2) % 8] for i in (0, 2, 4, 6))
    stair = (yokoi == 1) & (((N & E) | (E & S) | (S & W) | (W & N)) == 1)
    return first.astype(bool), second.astype(bool), stair.astype(bool)


_ZS1, _ZS2, _STAIR = _thinning_tables()


def _codes(P: np.ndarray, idx: np.ndarray, offs: np.ndarray) -> np.ndarray:
    return np.packbits(P[idx[:, None] + offs], axis=1, bitorder="little")[:, 0]


def skeletonize(m: np.ndarray) -> np.ndarray:
    """One-pixel-wide (8-connected) skeleton of a bool mask."""
    m = np.asarray(m, bool)
    H, W = m.shape
    if not m.any():
        return np.zeros_like(m)
    Wp = W + 2
    P = np.pad(m, 1).ravel()
    offs = np.array([dr * Wp + dc for dr, dc in _NB])
    fg = np.flatnonzero(P)
    cand = fg[_codes(P, fg, offs) != 255]  # only pixels touching background can be removed
    while True:
        changed = False
        for table in (_ZS1, _ZS2):
            if not cand.size:
                break
            dele = cand[table[_codes(P, cand, offs)]]
            if not dele.size:
                continue
            P[dele] = False
            changed = True
            nb = (dele[:, None] + offs).ravel()
            cand = np.union1d(cand[P[cand]], nb[P[nb]])
        if not changed:
            break
    # staircase pixels: four parity subfields, so no two pixels removed together are neighbours
    for _ in range(4):
        changed = False
        for pr in (0, 1):
            for pc in (0, 1):
                idx = np.flatnonzero(P)
                rr, cc = idx // Wp, idx % Wp
                idx = idx[(rr % 2 == pr) & (cc % 2 == pc)]
                if not idx.size:
                    continue
                rem = idx[_STAIR[_codes(P, idx, offs)]]
                if rem.size:
                    P[rem] = False
                    changed = True
        if not changed:
            break
    return P.reshape(H + 2, Wp)[1:-1, 1:-1].copy()


def neighbour_count(m: np.ndarray) -> np.ndarray:
    """Number of True pixels among the 8 neighbours."""
    m = np.asarray(m, bool)
    H, W = m.shape
    p = np.pad(m, 1).astype(np.uint8)
    out = np.zeros((H, W), np.uint8)
    for dr, dc in _NB:
        out += p[1 + dr:1 + dr + H, 1 + dc:1 + dc + W]
    return out


# ----------------------------------------------------------------------------- contours
def _ms_table():
    """Per marching-squares case (bits tl=1, tr=2, br=4, bl=8 set where above level): directed segments
    (from edge, to edge) with edges T=0, R=1, B=2, L=3, oriented so the high side is always on the same side."""
    corner = {1: (0.0, 0.0), 2: (0.0, 1.0), 4: (1.0, 1.0), 8: (1.0, 0.0)}  # (row, col)
    edges = {0: (1, 2), 1: (2, 4), 2: (8, 4), 3: (1, 8)}
    mid = {0: (0.0, 0.5), 1: (0.5, 1.0), 2: (1.0, 0.5), 3: (0.5, 0.0)}
    table = np.full((16, 2, 2), -1, np.int64)
    for case in range(16):
        high = [bit for bit in (1, 2, 4, 8) if case & bit]
        cut = [e for e, (a, b) in edges.items() if bool(case & a) != bool(case & b)]
        if len(cut) == 2:
            segs = [((cut[0], cut[1]), high[0])]
        elif case == 5:  # tl + br high: keep them apart
            segs = [((0, 3), 1), ((1, 2), 4)]
        elif case == 10:  # tr + bl high
            segs = [((0, 1), 2), ((2, 3), 8)]
        else:
            continue
        for i, ((e0, e1), h) in enumerate(segs):
            (pr, pc), (qr, qc), (hr, hc) = mid[e0], mid[e1], corner[h]
            crs = (qc - pc) * (hr - pr) - (qr - pr) * (hc - pc)
            table[case, i] = (e0, e1) if crs > 0 else (e1, e0)
    return table


_MS = _ms_table()


def _link(s: np.ndarray, e: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Directed segments (start key, end key) → (order, chain head per ordered segment, closed per segment)."""
    n = len(s)
    idx = np.arange(n)
    order = np.argsort(s, kind="stable")
    ss = s[order]
    pos = np.minimum(np.searchsorted(ss, e), n - 1)
    ok = ss[pos] == e
    nxt = np.where(ok, order[pos], -1)
    pred = np.full(n, -1)
    pred[nxt[ok]] = idx[ok]
    steps = max(1, int(math.ceil(math.log2(max(n, 2))))) + 1
    succ = np.where(ok, nxt, idx)
    rep, p = idx.copy(), succ.copy()
    for _ in range(steps):  # min index reachable forward (= cycle minimum on cycles)
        rep = np.minimum(rep, rep[p])
        p = p[p]
    pr = np.where(pred >= 0, pred, idx)
    h = pr.copy()
    for _ in range(steps):
        h = h[h]
    is_open = pred[h] == -1
    cut = ~is_open & (rep == idx)
    pr[cut] = idx[cut]  # break each cycle at its smallest segment
    dist = (pr != idx).astype(np.int64)
    p = pr.copy()
    for _ in range(steps):  # list ranking: distance to the chain head
        dist = dist + dist[p]
        p = p[p]
    o = np.lexsort((dist, p))
    return o, p[o], ~is_open


def find_contours(a: np.ndarray, level: float) -> list[np.ndarray]:
    """Iso-contours of a 2-D array at *level* → list of (N, 2) arrays of (row, col); closed contours repeat
    their first point at the end."""
    a = np.asarray(a, np.float64)
    H, W = a.shape
    if H < 2 or W < 2:
        return []
    hi = a > level
    case = (hi[:-1, :-1] * 1 + hi[:-1, 1:] * 2 + hi[1:, 1:] * 4 + hi[1:, :-1] * 8).ravel()
    cells = np.flatnonzero((case != 0) & (case != 15))
    if not cells.size:
        return []
    cr, cc = cells // (W - 1), cells % (W - 1)
    cs = case[cells]
    segs = _MS[cs]  # (n, 2, 2)
    two = segs[:, 1, 0] >= 0
    cr = np.concatenate([cr, cr[two]])
    cc = np.concatenate([cc, cc[two]])
    e0 = np.concatenate([segs[:, 0, 0], segs[two, 1, 0]])
    e1 = np.concatenate([segs[:, 0, 1], segs[two, 1, 1]])
    HW = H * W

    def edge_key(e):  # horizontal edge (r, c)–(r, c+1): r·W + c; vertical (r, c)–(r+1, c): HW + r·W + c
        r = cr + (e == 2)
        c = cc + (e == 1)
        return np.where((e == 0) | (e == 2), r * W + c, HW + r * W + c)

    ks, ke = edge_key(e0), edge_key(e1)
    flat = a.ravel()

    def point(k):
        vert = k >= HW
        kk = np.where(vert, k - HW, k)
        r, c = kk // W, kk % W
        a0 = flat[kk]
        a1 = flat[np.where(vert, kk + W, kk + 1)]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.clip(np.nan_to_num((level - a0) / (a1 - a0), nan=0.5), 0, 1)
        return np.stack([np.where(vert, r + t, r), np.where(vert, c, c + t)], 1)

    o, head, closed = _link(ks, ke)
    ps, pe = point(ks[o]), point(ke[o])
    bounds = np.flatnonzero(np.r_[True, head[1:] != head[:-1], True])
    out = []
    for i0, i1 in zip(bounds[:-1], bounds[1:]):
        pts = ps[i0:i1]
        if closed[o[i0]]:
            j = int(np.lexsort((pts[:, 1], pts[:, 0]))[0])  # start at the top-most, then left-most point
            pts = np.vstack([pts[j:], pts[:j], pts[j:j + 1]])
        else:
            pts = np.vstack([pts, pe[i1 - 1:i1]])
        out.append(pts)
    return out


# ----------------------------------------------------------------------------- shape
def _hull(p: np.ndarray) -> np.ndarray:
    """Convex hull (monotone chain), counter-clockwise in (x, y)."""
    p = np.unique(np.asarray(p, np.float64), axis=0)
    if len(p) < 3:
        return p

    def half(pts):
        h: list = []
        for q in pts:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (q[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(q)
        return h

    lo, up = half(p), half(p[::-1])
    return np.asarray(lo[:-1] + up[:-1])


def solidity(mask: np.ndarray) -> float:
    """Pixel area / number of pixel centres inside the convex hull of the region (hull of the midpoints of
    the outer pixel edges)."""
    m = np.asarray(mask, bool)
    area = int(m.sum())
    if area == 0:
        return 0.0
    edge = m & ~binary_erosion(np.pad(m, 1), cross())[1:-1, 1:-1]
    r, c = np.nonzero(edge)
    mids = np.concatenate([np.stack([c + dx, r + dy], 1) for dx, dy in ((-0.5, 0), (0.5, 0), (0, -0.5), (0, 0.5))])
    hull = _hull(mids)
    if len(hull) < 3:
        return 1.0
    r0, r1, c0, c1 = r.min(), r.max(), c.min(), c.max()
    yy, xx = np.mgrid[r0:r1 + 1, c0:c1 + 1]
    P = np.stack([xx.ravel(), yy.ravel()], 1).astype(np.float64)
    inside = np.ones(len(P), bool)
    for a, b in zip(hull, np.roll(hull, -1, axis=0)):
        inside &= (b[0] - a[0]) * (P[:, 1] - a[1]) - (b[1] - a[1]) * (P[:, 0] - a[0]) >= -1e-9
    return area / max(int(inside.sum()), area)


# ----------------------------------------------------------------------------- segmentation
def _find(parent: np.ndarray, x: np.ndarray) -> np.ndarray:
    r = parent[x]
    while True:
        rr = parent[r]
        if np.array_equal(rr, r):
            break
        r = rr
    parent[x] = r
    return r


def _merge(parent, size, internal, a, b, w) -> None:
    """Union root sets joined by edges a–b (weights w): sizes add, internal difference = max weight."""
    nodes, inv = np.unique(np.concatenate([a, b]), return_inverse=True)
    la, lb = inv[:len(a)], inv[len(a):]
    lr = _union(len(nodes), la, lb)
    tot = np.zeros(len(nodes))
    np.add.at(tot, lr, size[nodes])
    mx = np.zeros(len(nodes))
    np.maximum.at(mx, lr, internal[nodes])
    np.maximum.at(mx, lr[la], w)
    roots = np.flatnonzero(lr == np.arange(len(nodes)))
    size[nodes[roots]] = tot[roots]
    internal[nodes[roots]] = mx[roots]
    parent[nodes] = nodes[lr]


def felzenszwalb(img: np.ndarray, scale: float = 1.0, sigma: float = 0.8, min_size: int = 20,
                 batch: int = 2048) -> np.ndarray:
    """Graph-based segmentation (Felzenszwalb & Huttenlocher 2004) → int labels 0…n−1.

    *img* is H×W or H×W×C with values 0–1; *scale* is the paper's k for 0–255 images (larger → larger
    regions); *sigma* pre-smooths. Edges join 8-neighbours with the colour distance as weight. Two regions
    merge across an edge of weight w when w ≤ min(Int(C) + k/|C|) over both, Int(C) being the largest edge
    already inside C. Edges are taken in increasing weight in batches of *batch* (each batch judged against
    the regions as they stood before it). Regions smaller than *min_size* are then merged into the neighbour
    across their lightest edge."""
    x = np.asarray(img, np.float64)
    if x.ndim == 2:
        x = x[..., None]
    if sigma > 0:
        x = gaussian_filter(x, (sigma, sigma, 0))
    H, W, _C = x.shape
    N = H * W
    idx = np.arange(N, dtype=np.int32).reshape(H, W)
    S = slice(None)
    pairs = [((S, slice(0, -1)), (S, slice(1, None))), ((slice(0, -1), S), (slice(1, None), S)),
             ((slice(0, -1), slice(0, -1)), (slice(1, None), slice(1, None))),
             ((slice(1, None), slice(0, -1)), (slice(0, -1), slice(1, None)))]
    u = np.concatenate([idx[pa].ravel() for pa, _ in pairs])
    v = np.concatenate([idx[pb].ravel() for _, pb in pairs])
    w = np.concatenate([np.sqrt(((x[pa] - x[pb]) ** 2).sum(-1)).astype(np.float32).ravel() for pa, pb in pairs])
    order = np.argsort(w, kind="stable")
    u, v, w = u[order], v[order], w[order]
    del order
    k = float(scale) / 255.0
    parent = np.arange(N)
    size = np.ones(N)
    internal = np.zeros(N)
    for i, s0 in enumerate(range(0, len(w), batch)):
        bu, bv = u[s0:s0 + batch], v[s0:s0 + batch]
        bw = w[s0:s0 + batch].astype(np.float64)
        while bu.size:  # merging raises Int(C), which can admit edges of the batch that failed before
            ru, rv = _find(parent, bu), _find(parent, bv)
            diff = ru != rv
            ru, rv = ru[diff], rv[diff]
            bu, bv, bw = bu[diff], bv[diff], bw[diff]
            ok = bw <= np.minimum(internal[ru] + k / size[ru], internal[rv] + k / size[rv])
            if not ok.any():
                break
            _merge(parent, size, internal, ru[ok], rv[ok], bw[ok])
            bu, bv, bw = bu[~ok], bv[~ok], bw[~ok]
        if i % 64 == 63:
            parent = _compress(parent)
    parent = _compress(parent)
    if min_size > 1:
        ru, rv = parent[u], parent[v]
        cand = (ru != rv) & ((size[ru] < min_size) | (size[rv] < min_size))
        cu, cv = u[cand], v[cand]
        for s0 in range(0, len(cu), batch):
            bu, bv = cu[s0:s0 + batch], cv[s0:s0 + batch]
            while True:
                ru, rv = _find(parent, bu), _find(parent, bv)
                su, sv = size[ru] < min_size, size[rv] < min_size
                ok = (ru != rv) & (su | sv)
                if not ok.any():
                    break
                # each small region takes only its lightest edge per round, so no small region can bridge
                # two large ones in one step
                e = np.flatnonzero(ok)
                first = np.full(N, len(bu))
                np.minimum.at(first, ru[e][su[e]], e[su[e]])
                np.minimum.at(first, rv[e][sv[e]], e[sv[e]])
                take = e[((first[ru[e]] == e) & su[e]) | ((first[rv[e]] == e) & sv[e])]
                _merge(parent, size, internal, ru[take], rv[take], np.zeros(len(take)))
        parent = _compress(parent)
    root = parent == np.arange(N)
    lab = (np.cumsum(root) - 1)[parent]
    return lab.reshape(H, W)


def merge_regions(img: np.ndarray, labels: np.ndarray, thresh: float, connectivity: int = 2) -> np.ndarray:
    """Merge adjacent regions whose mean colours are closer than *thresh* (Euclidean, image units): regions are
    graph nodes, neighbours are joined when their mean-colour distance < *thresh*, and each connected part of
    that graph becomes one region. → labels 0…m−1."""
    x = np.asarray(img, np.float64)
    if x.ndim == 2:
        x = x[..., None]
    lab = np.asarray(labels, np.int64)
    base = int(lab.min())
    lab = lab - base
    n = int(lab.max()) + 1
    flat = lab.ravel()
    cnt = np.bincount(flat, minlength=n).astype(np.float64)
    means = np.stack([np.bincount(flat, x[..., c].ravel(), minlength=n) for c in range(x.shape[2])], 1)
    means /= np.maximum(cnt, 1)[:, None]
    S = slice(None)
    shifts = [((S, slice(0, -1)), (S, slice(1, None))), ((slice(0, -1), S), (slice(1, None), S))]
    if connectivity == 2:
        shifts += [((slice(0, -1), slice(0, -1)), (slice(1, None), slice(1, None))),
                   ((slice(1, None), slice(0, -1)), (slice(0, -1), slice(1, None)))]
    keys = []
    for pa, pb in shifts:
        a, b = lab[pa].ravel(), lab[pb].ravel()
        d = a != b
        a, b = a[d], b[d]
        keys.append(np.minimum(a, b) * n + np.maximum(a, b))
    keys = np.unique(np.concatenate(keys)) if keys else np.zeros(0, np.int64)
    pa, pb = keys // n, keys % n
    wgt = np.sqrt(((means[pa] - means[pb]) ** 2).sum(1))
    keep = wgt < thresh
    parent = _union(n, pa[keep], pb[keep])
    present = cnt > 0
    root = (parent == np.arange(n)) & present
    newid = np.cumsum(root) - 1
    return newid[parent][lab]


# ----------------------------------------------------------------------------- resampling
def resize_nearest(a: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Nearest-neighbour resize of the first two axes to *shape* (pixel centres aligned)."""
    H, W = a.shape[:2]
    h, w = shape
    ri = np.minimum(((np.arange(h) + 0.5) * H / h).astype(np.int64), H - 1)
    ci = np.minimum(((np.arange(w) + 0.5) * W / w).astype(np.int64), W - 1)
    return a[ri[:, None], ci[None, :]]


def rescale(img: np.ndarray, k: float) -> np.ndarray:
    """Anti-aliased rescale of an H×W(×C) float image by factor *k* (Pillow triangle filter, float32)."""
    from PIL import Image

    x = np.asarray(img, np.float32)
    H, W = x.shape[:2]
    h, w = max(1, int(round(H * k))), max(1, int(round(W * k)))
    planes = x[..., None] if x.ndim == 2 else x
    out = np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(planes[..., i])).resize((w, h), Image.Resampling.BILINEAR))
                    for i in range(planes.shape[2])], -1)
    return out[..., 0] if x.ndim == 2 else out


def sample_bilinear(src: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    """Bilinear samples of a 2-D array at fractional (row, col); beyond the edge the edge pixel repeats."""
    H, W = src.shape
    r0f, c0f = np.floor(rows), np.floor(cols)
    fr, fc = rows - r0f, cols - c0f
    r0, c0 = r0f.astype(np.int64), c0f.astype(np.int64)
    ra, rb = np.clip(r0, 0, H - 1), np.clip(r0 + 1, 0, H - 1)
    ca, cb = np.clip(c0, 0, W - 1), np.clip(c0 + 1, 0, W - 1)
    top = src[ra, ca] * (1 - fc) + src[ra, cb] * fc
    bot = src[rb, ca] * (1 - fc) + src[rb, cb] * fc
    return top * (1 - fr) + bot * fr
