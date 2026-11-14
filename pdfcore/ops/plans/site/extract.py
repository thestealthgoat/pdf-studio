"""Classify site imagery and vectorize it (classical computer vision, numpy + scikit-image).

Pipeline (``method="classic"``)
--------------------------------
1. Per-pixel cues at a working resolution of ≥ 0.5 ft/px: excess-green index (vegetation), brightness /
   saturation (pavement, roofs), local texture (tree canopy vs lawn, cars and striping), darkness (shadows).
2. Vegetation = ExG above an Otsu threshold → trees (textured canopy) vs grass (smooth).
3. Water = blue or dark, low-texture, compact and wide (shadows are thin).
4. Sun direction from tree shadows (the offset at which shadow most often follows canopy).
5. Everything else is over-segmented (Felzenszwalb) and merged by colour (RAG); each region is scored as a
   roof: rectangular fill, straight edges, a shadow on the away-from-sun side, no shadow on the sun side,
   roof colour, low texture, not road-shaped. Gable halves and roof equipment are merged in.
6. Pavement = remaining low-saturation surface. Roads = pavement skeleton branches with a steady width of
   14–60 ft and length ≥ 60 ft; road surface = union of discs along those branches; the rest is parking/paved.
7. Vectorize: contours → Douglas–Peucker → buildings squared to 90° when near-rectilinear; road
   centrelines traced from the skeleton and smoothed.

``method="osm"`` uses OpenStreetMap only; ``"both"`` prefers OSM geometry where it exists and fills gaps from
the imagery (OSM roads also keep the classifier from calling road surface a roof). Every feature records
``source`` (osm | imagery) and ``confidence`` (0–1).

``method="sam"`` (optional): if ``segment_anything`` + ``torch`` are importable and ``PDFCORE_SAM_CHECKPOINT``
points to a model checkpoint, SAM masks replace the Felzenszwalb regions in step 5. Not a dependency.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from pdfcore.ops.plans.site import geom
from pdfcore.ops.plans.site.geo import Georef, SitePackage, feature

METHODS = ("classic", "osm", "both", "sam")


def _need_skimage():
    try:
        import scipy  # noqa: F401
        import skimage  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError("site-extract needs scikit-image: pip install 'pdf-studio[site]'") from exc


# ----------------------------------------------------------------------------- per-pixel cues
@dataclass
class Cues:
    img: np.ndarray  # float RGB 0..1 at working resolution
    s: float  # ft per working pixel
    L: np.ndarray
    S: np.ndarray
    exg: np.ndarray
    tex: np.ndarray

    def px(self, ft: float) -> float:
        return ft / self.s

    def px2(self, sqft: float) -> float:
        return sqft / (self.s * self.s)


def cues(rgb: np.ndarray, ft_per_px: float, work_ft: float = 0.5) -> tuple[Cues, float]:
    """Working-resolution cues; returns (cues, factor) with factor = working px per input px."""
    from scipy import ndimage as ndi
    from skimage.transform import rescale

    img = rgb.astype(np.float32) / 255.0 if rgb.dtype == np.uint8 else rgb.astype(np.float32)
    k = 1.0
    if ft_per_px < work_ft:
        k = ft_per_px / work_ft
        img = rescale(img, k, channel_axis=2, anti_aliasing=True, preserve_range=True).astype(np.float32)
    s = ft_per_px / k
    # global linear stretch on luminance (same gain for all channels keeps chroma): hazy imagery → full range
    L0 = 0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]
    lo, hi = np.percentile(L0, 0.5), np.percentile(L0, 99.5)
    if hi - lo > 0.05:
        img = np.clip((img - lo) / (hi - lo), 0, 1).astype(np.float32)
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    L = 0.299 * r + 0.587 * g + 0.114 * b
    mx, mn = img.max(2), img.min(2)
    S = (mx - mn) / (mx + 1e-4)
    exg = (2 * g - r - b) / (r + g + b + 1e-4)
    w = max(3, int(round(5.0 / s)) | 1)
    m1 = ndi.uniform_filter(L, w)
    m2 = ndi.uniform_filter(L * L, w)
    tex = np.sqrt(np.maximum(m2 - m1 * m1, 0))
    return Cues(img, s, L, S, exg, tex), k


def _otsu(v: np.ndarray, lo: float, hi: float, default: float) -> float:
    from skimage.filters import threshold_otsu

    v = v[np.isfinite(v)]
    if v.size < 100 or v.std() < 1e-3:
        return default
    try:
        return float(np.clip(threshold_otsu(v), lo, hi))
    except Exception:
        return default


def _disk(r: float):
    from skimage.morphology import disk

    return disk(max(1, int(round(r))))


def _remove_small(m: np.ndarray, min_px: float) -> np.ndarray:
    from scipy import ndimage as ndi

    lab, n = ndi.label(m)
    if n == 0:
        return m
    sizes = np.bincount(lab.ravel())
    keep = sizes >= min_px
    keep[0] = False
    return keep[lab]


# ----------------------------------------------------------------------------- classification
@dataclass
class Classified:
    c: Cues
    veg: np.ndarray
    trees: np.ndarray
    grass: np.ndarray
    water: np.ndarray
    shadow: np.ndarray
    building: np.ndarray
    building_conf: dict[int, float]
    building_labels: np.ndarray
    pavement: np.ndarray
    road: np.ndarray
    road_skel: np.ndarray
    sun_dir: tuple[float, float] | None
    stats: dict = field(default_factory=dict)
    regions: np.ndarray | None = None  # merged segmentation labels (debug / QA)
    region_info: dict = field(default_factory=dict)


def _sun_direction(c: Cues, canopy: np.ndarray, shadow: np.ndarray) -> tuple[tuple[float, float] | None, float]:
    """Unit vector (drow, dcol) from objects to their shadows: the offset (8–16 ft) at which shadow most
    often follows lit tree canopy."""
    from scipy import ndimage as ndi

    H, W = canopy.shape
    if canopy.sum() < c.px2(1500):
        return None, 0.0
    canopy_d = ndi.binary_dilation(canopy, iterations=1)
    scores = np.zeros(16)
    for ft in (8.0, 12.0, 16.0):
        k = max(2, int(round(c.px(ft))))
        for i in range(16):
            th = 2 * math.pi * i / 16
            oy, ox = int(round(math.sin(th) * k)), int(round(math.cos(th) * k))
            a = canopy[max(0, -oy):H - max(0, oy), max(0, -ox):W - max(0, ox)]
            bt = canopy_d[max(0, oy):H - max(0, -oy), max(0, ox):W - max(0, -ox)]
            bs = shadow[max(0, oy):H - max(0, -oy), max(0, ox):W - max(0, -ox)]
            sel = a & ~bt
            scores[i] += float(bs[sel].mean()) / 3 if sel.sum() > 50 else 0.0
    # smooth over neighbouring directions, pick the peak
    sm = (np.roll(scores, 1) + 2 * scores + np.roll(scores, -1)) / 4
    i = int(np.argmax(sm))
    strength = float(sm[i] - np.median(sm))
    if strength < 0.05:
        return None, strength
    # refine: centroid of the peak
    th = 2 * math.pi * i / 16
    return (math.sin(th), math.cos(th)), strength


def _sun_from_regions(c: Cues, labels: np.ndarray, shadow: np.ndarray) -> tuple[tuple[float, float] | None, float]:
    """Shadow direction from compact, rectangular, lit regions (roofs and pavement alike): roofs cast their
    shadow on one side, so the summed directional shadow adjacency peaks there."""
    from scipy import ndimage as ndi

    H, W = labels.shape
    sizes = np.bincount(labels.ravel())
    k = max(2, int(round(c.px(5.0))))
    scores = np.zeros(16)
    used = 0
    slices = ndi.find_objects(labels)
    order = np.argsort(-sizes)
    for i in order[1:400]:
        if sizes[i] < c.px2(250) or sizes[i] > c.px2(60000) or slices[i - 1] is None:
            continue
        sl = slices[i - 1]
        y0, y1 = max(0, sl[0].start - k - 1), min(H, sl[0].stop + k + 1)
        x0, x1 = max(0, sl[1].start - k - 1), min(W, sl[1].stop + k + 1)
        m = labels[y0:y1, x0:x1] == i
        fill, rectil, _l, _s = _region_shapes(m)
        if fill < 0.8:
            continue
        shd = shadow[y0:y1, x0:x1]
        row = np.zeros(16)
        for d in range(16):
            th = 2 * math.pi * d / 16
            oy, ox = int(round(math.sin(th) * k)), int(round(math.cos(th) * k))
            mm = np.roll(np.roll(m, oy, 0), ox, 1) & ~m
            row[d] = shd[mm].mean() if mm.sum() else 0
        if row.max() > 0.15:
            scores += row * math.sqrt(sizes[i])
            used += 1
    if used < 3:
        return None, 0.0
    scores /= scores.sum() + 1e-9
    sm = (np.roll(scores, 1) + 2 * scores + np.roll(scores, -1)) / 4
    d = int(np.argmax(sm))
    strength = float(sm[d] / (np.median(sm) + 1e-9))
    if strength < 1.6:
        return None, strength
    th = 2 * math.pi * d / 16
    return (math.sin(th), math.cos(th)), strength


def _region_shapes(mask: np.ndarray) -> tuple[float, float, float, float]:
    """(fill of min rectangle, rectilinearity, long side px, short side px) of a region mask."""
    from skimage import measure

    cs = measure.find_contours(np.pad(mask, 1).astype(float), 0.5)
    if not cs:
        return 0.0, 0.0, 0.0, 0.0
    ring = [(p[1], p[0]) for p in max(cs, key=len)]
    ring = geom.simplify(ring, 1.0, closed=True)
    if len(ring) < 3:
        return 0.0, 0.0, 0.0, 0.0
    a, lng, sht = geom.min_rect(ring)
    fill = float(mask.sum()) / max(a, 1.0)
    return min(fill, 1.0), geom.rectilinearity(ring), lng, sht


def classify(rgb: np.ndarray, ft_per_px: float, *, road_hint: np.ndarray | None = None,
             building_hint: np.ndarray | None = None, regions: np.ndarray | None = None,
             work_ft: float = 0.5) -> tuple[Classified, float]:
    """Classify an RGB image. Hints are bool masks in *input* pixels. Returns (Classified, factor)."""
    _need_skimage()
    from scipy import ndimage as ndi
    from skimage import graph as skgraph
    from skimage.morphology import skeletonize
    from skimage.segmentation import felzenszwalb
    from skimage.transform import resize

    c, k = cues(rgb, ft_per_px, work_ft)
    H, W = c.L.shape

    def hint(m):
        if m is None:
            return None
        if m.shape != (H, W):
            m = resize(m.astype(float), (H, W), order=0) > 0.5
        return m

    road_hint, building_hint = hint(road_hint), hint(building_hint)
    if regions is not None:
        regions = resize(regions, (H, W), order=0, preserve_range=True, anti_aliasing=False).astype(np.int64)
    st: dict[str, Any] = {"work_ft_per_px": c.s, "size_px": [W, H]}

    # --- vegetation
    t_veg = _otsu(c.exg[(c.exg > -0.15) & (c.exg < 0.6)], 0.035, 0.12, 0.06)
    st["t_veg"] = t_veg
    veg0 = (c.exg > t_veg) & (c.L < 0.8)
    veg = ndi.binary_closing(veg0, _disk(c.px(2.0)))
    veg = ndi.binary_opening(veg, _disk(c.px(1.0)))
    veg = _remove_small(veg, c.px2(60))
    t_tex = _otsu(c.tex[veg], 0.03, 0.08, 0.05) if veg.sum() > 200 else 0.05
    dark_green = c.L < (np.median(c.L[veg]) - 0.1 if veg.sum() > 200 else 0.2)
    tree_px = veg & ((c.tex > t_tex) | dark_green)
    tree_frac = ndi.gaussian_filter(tree_px.astype(np.float32), c.px(4.0))
    trees = veg & (tree_frac > 0.5)
    trees = ndi.binary_closing(trees, _disk(c.px(3.0))) & ndi.binary_dilation(veg, _disk(c.px(1.0)))
    trees = _remove_small(trees, c.px2(150))
    grass = veg & ~trees
    grass = _remove_small(grass, c.px2(200))
    st["t_tex"] = t_tex

    # --- shadows and water
    p2, p50 = np.percentile(c.L, 2), np.percentile(c.L, 50)
    t_sh = float(np.clip(p2 + 0.3 * (p50 - p2), 0.06, 0.3))
    st["t_shadow"] = t_sh
    shadow = c.L < t_sh
    r, g, b = c.img[..., 0], c.img[..., 1], c.img[..., 2]
    blue = (b > r + 0.03) & (b >= g - 0.02) & (c.S > 0.12) & (c.L < 0.6)
    dark_smooth = (c.L < t_sh * 1.15) & (c.tex < 0.035)
    wc = ndi.binary_opening((blue | dark_smooth) & ~(veg & ~shadow), _disk(c.px(1.5)))
    wc = ndi.binary_fill_holes(wc)
    water = np.zeros_like(wc)
    lab, n = ndi.label(wc)
    tree_ring_src = ndi.binary_dilation(trees & ~shadow, _disk(c.px(2)))
    if n:
        dt = ndi.distance_transform_edt(wc) * c.s
        for i, sl in enumerate(ndi.find_objects(lab), 1):
            m = lab[sl] == i
            area_ft = m.sum() * c.s * c.s
            if area_ft < 1500 or dt[sl][m].max() < 14:
                continue
            if c.tex[sl][m].mean() > 0.05:
                continue
            fill, rectil, _l, _s = _region_shapes(m)
            from skimage.measure import regionprops

            sol = regionprops(m.astype(np.uint8))[0].solidity
            if sol < 0.85:
                continue  # shadows under canopy and along buildings are ragged
            if fill > 0.85 and rectil > 0.8 and not blue[sl][m].mean() > 0.5:
                continue  # dark rectilinear and not blue: leave it to the roof test
            y0, y1 = max(0, sl[0].start - 6), min(H, sl[0].stop + 6)
            x0, x1 = max(0, sl[1].start - 6), min(W, sl[1].stop + 6)
            mm = lab[y0:y1, x0:x1] == i
            ring = ndi.binary_dilation(mm, _disk(c.px(4))) & ~mm
            if ring.sum() and tree_ring_src[y0:y1, x0:x1][ring].mean() > 0.6:
                continue  # dark hole in woodland: canopy shadow
            water[sl] |= m
    water = ndi.binary_closing(water, _disk(c.px(2.0)))
    shadow &= ~water
    veg &= ~water
    trees &= ~water
    grass &= ~water

    shadow = (c.L < t_sh) & ~water
    sun, sun_strength = _sun_direction(c, trees & ~shadow, shadow)
    if sun is not None and water.any():
        # dark roofs cast shadows; ponds and basins do not
        k_sh = max(2, int(round(c.px(5.0))))
        wl, wn = ndi.label(water)
        for i, sl in enumerate(ndi.find_objects(wl), 1):
            y0, y1 = max(0, sl[0].start - k_sh - 1), min(H, sl[0].stop + k_sh + 1)
            x0, x1 = max(0, sl[1].start - k_sh - 1), min(W, sl[1].stop + k_sh + 1)
            m = wl[y0:y1, x0:x1] == i
            oy, ox = int(round(sun[0] * k_sh)), int(round(sun[1] * k_sh))
            mm = np.roll(np.roll(m, oy, 0), ox, 1) & ~m
            if mm.sum() and shadow[y0:y1, x0:x1][mm].mean() > 0.35:
                water[y0:y1, x0:x1][m] = False
    st["sun_dir"] = None if sun is None else [round(sun[0], 3), round(sun[1], 3)]
    st["sun_strength"] = round(sun_strength, 3)

    # --- segmentation of the built-up surface
    built = ~(veg & ~shadow) & ~water & ~shadow
    built = ndi.binary_opening(built, _disk(c.px(0.75)))
    if regions is None:
        sm = ndi.gaussian_filter(c.img, (0.6, 0.6, 0))
        seg = felzenszwalb(sm, scale=150, sigma=0.5, min_size=max(8, int(c.px2(25))))
        try:
            rag = skgraph.rag_mean_color(sm * 255.0, seg)
            seg = skgraph.cut_threshold(seg, rag, 14)
        except Exception:
            pass
    else:
        seg = regions
    from skimage.measure import label as cc_label

    labels = cc_label(np.where(built, seg + 1, 0), background=0, connectivity=1)
    n_reg = int(labels.max())
    st["regions"] = n_reg
    if sun is None:
        sun2, strength2 = _sun_from_regions(c, labels, shadow)
        st["sun_from_regions"] = round(strength2, 3)
        if sun2 is not None:
            sun = sun2
            st["sun_dir"] = [round(sun[0], 3), round(sun[1], 3)]

    # --- roof scoring
    k_sh = max(2, int(round(c.px(5.0))))
    sizes = np.bincount(labels.ravel(), minlength=n_reg + 1)
    slices = ndi.find_objects(labels)
    roof = np.zeros(n_reg + 1, float)
    info: dict[int, dict] = {}
    min_b, max_b = c.px2(120), c.px2(400_000)
    T_ROOF = 2.3
    pad = 2 * k_sh + 3
    for i, sl in enumerate(slices, 1):
        if sl is None or sizes[i] < min_b or sizes[i] > max_b:
            continue
        y0, y1 = max(0, sl[0].start - pad), min(H, sl[0].stop + pad)
        x0, x1 = max(0, sl[1].start - pad), min(W, sl[1].stop + pad)
        m = labels[y0:y1, x0:x1] == i
        fill, rectil, lng, sht = _region_shapes(m)
        Lm, Sm = float(c.L[y0:y1, x0:x1][m].mean()), float(c.S[y0:y1, x0:x1][m].mean())
        texm = float(c.tex[y0:y1, x0:x1][m].mean())
        shd = shadow[y0:y1, x0:x1]

        def side(dr, dc):
            # band swept 2–10 ft from the region on that side (eaves and edge pixels sit between roof and shadow)
            mm = np.zeros_like(m)
            for t in np.arange(max(1, k_sh // 3), 2 * k_sh + 1, max(1, k_sh // 3)):
                mm |= np.roll(np.roll(m, int(round(dr * t)), 0), int(round(dc * t)), 1)
            mm &= ~ndi.binary_dilation(m, iterations=1)
            return float(shd[mm].mean()) if mm.sum() else 0.0

        if sun is not None:
            sh_away, sh_sun = side(*sun), side(-sun[0], -sun[1])
        else:
            ring = ndi.binary_dilation(m, _disk(k_sh)) & ~m
            sh_away, sh_sun = (float(shd[ring].mean()) if ring.sum() else 0.0), 0.0
        width_ft = 2 * sizes[i] / max(1.0, (lng + sht)) * c.s
        road_like = (lng > 4.5 * max(sht, 1)) and width_ft < 70
        hint_road = float(road_hint[y0:y1, x0:x1][m].mean()) if road_hint is not None else 0.0
        hint_bldg = float(building_hint[y0:y1, x0:x1][m].mean()) if building_hint is not None else 0.0
        score = (1.6 * np.clip((fill - 0.62) / 0.28, 0, 1)
                 + 1.0 * np.clip((rectil - 0.55) / 0.35, 0, 1)
                 + 2.2 * np.clip(sh_away / 0.3, 0, 1)
                 - 1.6 * np.clip(sh_sun / 0.25, 0, 1)
                 + 0.6 * float(Sm > 0.14)
                 + 0.4 * float(Lm > 0.8 and Sm < 0.12)
                 - 1.2 * np.clip((texm - 0.06) / 0.05, 0, 1)
                 - 2.0 * float(road_like)
                 - 3.0 * hint_road
                 + 3.0 * hint_bldg
                 - 0.8 * float(sizes[i] * c.s * c.s > 120_000))
        if sun is not None and sh_away < 0.08 and hint_bldg < 0.5:
            score = min(score, T_ROOF - 0.5)  # no shadow where a roof would cast one: not a building
        roof[i] = score
        info[i] = {"fill": fill, "rectil": rectil, "sh_away": sh_away, "sh_sun": sh_sun, "L": Lm, "S": Sm, "tex": texm,
                   "road_like": road_like, "score": float(score), "box": (y0, y1, x0, x1)}
    is_b = roof >= T_ROOF
    is_b[0] = False
    # absorb neighbours that complete a rectangle (gable halves, roof edges, equipment)
    adj = _adjacency(labels)
    for i in [j for j in range(1, n_reg + 1) if is_b[j]]:
        for j in adj.get(i, ()):
            if is_b[j] or j not in info or sizes[j] > 2.5 * sizes[i]:
                continue
            y0, y1, x0, x1 = (min(info[i]["box"][0], info[j]["box"][0]), max(info[i]["box"][1], info[j]["box"][1]),
                              min(info[i]["box"][2], info[j]["box"][2]), max(info[i]["box"][3], info[j]["box"][3]))
            sub = labels[y0:y1, x0:x1]
            fill, rectil, _l, _s = _region_shapes((sub == i) | (sub == j))
            if fill >= max(0.86, info[i]["fill"] + 0.03) and not info[j]["road_like"] and info[j]["tex"] < 0.09:
                is_b[j] = True
                roof[j] = max(roof[j], roof[i] - 0.3)
    bmask = is_b[labels]
    bmask = ndi.binary_fill_holes(ndi.binary_closing(bmask, _disk(c.px(1.0))))
    bmask = ndi.binary_opening(bmask, _disk(c.px(1.0)))
    bmask = _remove_small(bmask, min_b)
    if water.any() and bmask.any():
        bring = ndi.binary_dilation(bmask, _disk(c.px(6)))
        wl, wn = ndi.label(water)
        for i, sl in enumerate(ndi.find_objects(wl), 1):
            m = wl[sl] == i
            edge = m & ~ndi.binary_erosion(m, iterations=2)
            if edge.sum() and bring[sl][edge].mean() > 0.15:
                water[sl][m] = False  # a dark patch hugging a roof is its shadow
    blab, nb = ndi.label(bmask)
    bconf: dict[int, float] = {}
    for j, sl in enumerate(ndi.find_objects(blab), 1):
        ids = labels[sl][blab[sl] == j]
        ids = ids[ids > 0]
        sc = float(np.average(roof[ids])) if ids.size else T_ROOF
        bconf[j] = float(1 / (1 + math.exp(-(sc - T_ROOF) * 1.5)) * 0.5 + 0.45)
    st["buildings"] = nb

    # --- pavement and roads
    pave = built & ~bmask & (c.S < 0.22) & (c.L > 0.18)
    if road_hint is not None:
        pave |= road_hint & ~bmask & ~water
    pave = ndi.binary_closing(pave | (shadow & ~bmask & ndi.binary_dilation(pave, _disk(c.px(3)))), _disk(c.px(2.5)))
    holes = ndi.binary_fill_holes(pave) & ~pave
    pave |= holes & ~_remove_small(holes, c.px2(400)) & ~bmask
    pave = ndi.binary_opening(pave, _disk(c.px(1.5)))
    pave = _remove_small(pave, c.px2(300))
    road, skel = roads_from_pavement(pave, c.s)
    st["road_px"] = int(road.sum())
    return Classified(c, veg, trees, grass, water, shadow, blab > 0, bconf, blab, pave & ~road, road, skel, sun, st,
                      labels, info), k


def _adjacency(labels: np.ndarray) -> dict[int, set[int]]:
    adj: dict[int, set[int]] = {}
    for a, b in ((labels[:, :-1], labels[:, 1:]), (labels[:-1, :], labels[1:, :])):
        sel = (a != b) & (a > 0) & (b > 0)
        pairs = np.unique(np.stack([a[sel], b[sel]], 1), axis=0) if sel.any() else np.zeros((0, 2), int)
        for x, y in pairs:
            adj.setdefault(int(x), set()).add(int(y))
            adj.setdefault(int(y), set()).add(int(x))
    return adj


# ----------------------------------------------------------------------------- roads
def roads_from_pavement(pave: np.ndarray, s: float, min_w: float = 14.0, max_w: float = 60.0,
                        min_len: float = 60.0) -> tuple[np.ndarray, np.ndarray]:
    """Road surface + kept skeleton from a pavement mask (*s* ft/px)."""
    from scipy import ndimage as ndi
    from skimage.morphology import skeletonize

    if not pave.any():
        return np.zeros_like(pave), np.zeros_like(pave)
    dt = ndi.distance_transform_edt(pave) * s
    sk = skeletonize(pave)
    w = 2 * dt
    sk &= (w >= min_w) & (w <= max_w)
    # branches: drop junction pixels, label segments, judge each by length and width steadiness
    nb = ndi.convolve(sk.astype(np.uint8), np.ones((3, 3), np.uint8), mode="constant") - 1
    junction = sk & (nb >= 3)
    seg = sk & ~ndi.binary_dilation(junction, np.ones((3, 3), bool))
    lab, n = ndi.label(seg, np.ones((3, 3), bool))
    keep = np.zeros(n + 1, bool)
    for i, sl in enumerate(ndi.find_objects(lab), 1):
        m = lab[sl] == i
        ln = m.sum() * s * 1.1
        ww = w[sl][m]
        if ln >= min_len and ww.std() <= 0.35 * ww.mean():
            keep[i] = True
    kept = keep[lab]
    # restore junctions that touch kept branches
    jl, jn = ndi.label(ndi.binary_dilation(junction, np.ones((3, 3), bool)) & sk, np.ones((3, 3), bool))
    for i, sl in enumerate(ndi.find_objects(jl), 1):
        m = jl[sl] == i
        if (ndi.binary_dilation(m, np.ones((3, 3), bool)) & kept[sl]).any():
            kept[sl] |= m
    if not kept.any():
        return np.zeros_like(pave), kept
    # each branch keeps its own typical width (local maxima at junctions/driveways would bulge the edges)
    bl, bn = ndi.label(kept, np.ones((3, 3), bool))
    med = np.zeros(bn + 1)
    for i in range(1, bn + 1):
        med[i] = np.median(dt[bl == i])
    d, (ri, ci) = ndi.distance_transform_edt(~kept, return_indices=True)
    rad = np.minimum(dt[ri, ci], 1.15 * med[bl[ri, ci]]) / s
    road = pave & (d <= rad + 1.0)
    road = ndi.binary_opening(road, _disk(min_w / s / 4))
    return road, kept


def trace_skeleton(sk: np.ndarray) -> list[list[tuple[float, float]]]:
    """Skeleton pixels → polylines (col, row), split at junctions and ends."""
    from scipy import ndimage as ndi

    sk = sk.copy()
    H, W = sk.shape
    nb = ndi.convolve(sk.astype(np.uint8), np.ones((3, 3), np.uint8), mode="constant") - 1
    nodes = sk & (nb != 2)
    visited = np.zeros_like(sk)
    nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    lines = []

    def walk(r, c, pr, pc):
        path = [(pr, pc), (r, c)]
        while True:
            visited[r, c] = True
            if nodes[r, c]:
                return path
            nxt = None
            for dr, dc in nbrs:
                rr, cc = r + dr, c + dc
                if 0 <= rr < H and 0 <= cc < W and sk[rr, cc] and (rr, cc) != (path[-2]) and not (visited[rr, cc] and not nodes[rr, cc]):
                    nxt = (rr, cc)
                    if nodes[rr, cc]:
                        break
            if nxt is None:
                return path
            r, c = nxt
            path.append((r, c))

    for r, c in zip(*np.nonzero(nodes)):
        for dr, dc in nbrs:
            rr, cc = r + dr, c + dc
            if 0 <= rr < H and 0 <= cc < W and sk[rr, cc] and not visited[rr, cc]:
                p = walk(rr, cc, r, c)
                if len(p) >= 2:
                    lines.append(p)
        visited[r, c] = True
    # loops without nodes
    rest = sk & ~visited
    while rest.any():
        r, c = map(int, np.argwhere(rest)[0])
        visited[r, c] = True
        p = [(r, c)]
        while True:
            nxt = None
            for dr, dc in nbrs:
                rr, cc = p[-1][0] + dr, p[-1][1] + dc
                if 0 <= rr < H and 0 <= cc < W and sk[rr, cc] and not visited[rr, cc]:
                    nxt = (rr, cc)
                    break
            if nxt is None:
                break
            visited[nxt] = True
            p.append(nxt)
        if len(p) > 2:
            lines.append(p + [p[0]])
        rest = sk & ~visited
    return [[(float(c) + 0.5, float(r) + 0.5) for r, c in ln] for ln in lines]


def centerlines(skel: np.ndarray, s: float, to_xy, *, min_len_ft: float = 40.0, tol_ft: float = 2.0,
                trim_ft: float = 20.0, max_gap_ft: float = 200.0) -> list[list[tuple[float, float]]]:
    """Skeleton → smoothed centreline polylines (CRS ft). Dangling ends away from the image edge (where a road
    meets a wide paved area) are trimmed and re-bridged straight across when two ends line up."""
    H, W = skel.shape
    px_lines = []
    for ln in trace_skeleton(skel):
        if geom.length(ln) * s < min_len_ft * 0.5:
            continue
        p = geom.simplify(ln, tol_ft / s)
        p = geom.chaikin(p, 2)
        px_lines.append(geom.simplify(p, 0.25 / s))
    px_lines = _join_lines(px_lines, tol=3.0 + 2.0 / s)
    edge = 6.0 + 40.0 / s  # skeletons stop about a half-width short of the image edge

    def at_edge(q):
        return q[0] < edge or q[1] < edge or q[0] > W - edge or q[1] > H - edge

    def dangling(q, me):
        return not at_edge(q) and not any(math.dist(q, e) < 4 + 3 / s for j, o in enumerate(px_lines) if j != me
                                           for e in (o[0], o[-1]))

    trimmed = []
    for i, ln in enumerate(px_lines):
        a, b = dangling(ln[0], i), dangling(ln[-1], i)
        t = trim_ft / s
        L = geom.length(ln)
        if L - (a + b) * t < min_len_ft / s:
            trimmed.append(ln)
            continue
        if a:
            ln = _cut(ln, t)
        if b:
            ln = _cut(ln[::-1], t)[::-1]
        trimmed.append(ln)
    px_lines = _join_lines(trimmed, tol=3.0 + 2.0 / s, max_gap=max_gap_ft / s)
    # run ends near the image edge out to the edge
    for i, ln in enumerate(px_lines):
        for from_end in (False, True):
            q = ln[-1] if from_end else ln[0]
            if not at_edge(q) or len(ln) < 2:
                continue
            d = _end_dir(ln, from_end=from_end)
            if not from_end:
                d += math.pi
            ux, uy = math.cos(d), math.sin(d)
            ts = [t for t in ((-q[0] / ux) if ux < -1e-6 else None, ((W - q[0]) / ux) if ux > 1e-6 else None,
                              (-q[1] / uy) if uy < -1e-6 else None, ((H - q[1]) / uy) if uy > 1e-6 else None) if t is not None]
            t = min(ts) if ts else 0
            if 0 < t < edge * 1.5:
                e = (q[0] + ux * t, q[1] + uy * t)
                ln = ln + [e] if from_end else [e] + ln
        px_lines[i] = ln
    out = [[to_xy(x, y) for x, y in ln] for ln in px_lines]
    return [ln for ln in out if geom.length(ln) >= min_len_ft]


def _cut(ln, d):
    """Drop the first *d* units of a polyline."""
    p = np.asarray(ln, float)
    seg = np.hypot(*np.diff(p, axis=0).T)
    acc = 0.0
    for i, sl in enumerate(seg):
        if acc + sl > d:
            t = (d - acc) / sl
            q = p[i] + t * (p[i + 1] - p[i])
            return [tuple(q)] + [tuple(x) for x in p[i + 1:]]
        acc += sl
    return [tuple(x) for x in p[-2:]]


def _join_lines(lines: list, tol: float, max_gap: float = 0.0) -> list:
    """Join polylines end to end: touching ends turning < 35°, or (``max_gap``) ends that line up within 12°
    across a gap (bridged straight)."""
    lines = [list(ln) for ln in lines if len(ln) >= 2]

    def ang(a, b):
        return abs((a - b + math.pi) % (2 * math.pi) - math.pi)

    changed = True
    while changed:
        changed = False
        best = None
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                a, b = lines[i], lines[j]
                for ra, rb in ((False, False), (False, True), (True, False), (True, True)):
                    A = a[::-1] if ra else a
                    B = b[::-1] if rb else b
                    gap = math.dist(A[-1], B[0])
                    da = _end_dir(A, from_end=True)
                    db = _end_dir(B, from_end=False)
                    if gap <= tol and ang(da, db) < math.radians(35):
                        cand = (gap, i, j, A + B[1:])
                    elif max_gap and gap <= max_gap:
                        dc = math.atan2(B[0][1] - A[-1][1], B[0][0] - A[-1][0])
                        if ang(da, dc) < math.radians(12) and ang(dc, db) < math.radians(12):
                            cand = (gap, i, j, A + B)
                        else:
                            continue
                    else:
                        continue
                    if best is None or cand[0] < best[0]:
                        best = cand
        if best is not None:
            _g, i, j, merged = best
            lines[i] = merged
            lines.pop(j)
            changed = True
    return lines


def _end_dir(ln, from_end: bool, span: float = 12.0) -> float:
    """Direction (rad) of a polyline's end, measured over ~*span* units, pointing outward."""
    p = ln[::-1] if from_end else ln
    acc, k = 0.0, 1
    while k < len(p) - 1 and acc + math.dist(p[k - 1], p[k]) < span:
        acc += math.dist(p[k - 1], p[k])
        k += 1
    a, b = p[k], p[0]  # inner point → end point
    d = math.atan2(b[1] - a[1], b[0] - a[0])
    return d if from_end else d + math.pi


# ----------------------------------------------------------------------------- vectorize
def _poly_features(mask: np.ndarray, kind: str, to_xy, s: float, *, tol_ft: float, min_ft2: float, conf: float,
                   smooth: bool = True, extra: dict | None = None) -> list[dict]:
    feats = []
    for rings in geom.mask_to_polygons(mask, to_xy, tol_px=tol_ft / s, min_area_px=min_ft2 / (s * s), smooth=smooth):
        a = geom.polygon_area(rings)
        if a < min_ft2:
            continue
        feats.append(feature(kind, "Polygon", [geom.close_ring(r) for r in rings], source="imagery",
                             confidence=round(conf, 2), area_sf=round(a, 1), **(extra or {})))
    return feats


def building_polygons(cl: Classified, to_xy, *, min_ft2: float = 120.0) -> list[dict]:
    from scipy import ndimage as ndi
    from skimage import measure

    s = cl.c.s
    out = []
    for j, sl in enumerate(ndi.find_objects(cl.building_labels), 1):
        if sl is None:
            continue
        m = np.pad(cl.building_labels[sl] == j, 1)
        cs = measure.find_contours(m.astype(float), 0.5)
        if not cs:
            continue
        r0, c0 = sl[0].start - 1, sl[1].start - 1
        ring_px = [(p[1] + c0 + 0.5, p[0] + r0 + 0.5) for p in max(cs, key=len)]
        ring = [to_xy(x, y) for x, y in ring_px]
        ring = geom.simplify(ring, max(1.2, 1.4 * s), closed=True)
        if len(ring) < 3 or geom.area(ring) < min_ft2:
            continue
        sq = None
        if geom.rectilinearity(ring) >= 0.7:
            sq = geom.orthogonalize(ring, max_dev_deg=20, min_edge=4.0)
        if sq is None:
            # nearly a rectangle? use the minimum-area rectangle
            a, lng, sht = geom.min_rect(ring)
            if geom.area(ring) / max(a, 1e-9) > 0.9:
                sq = _min_rect_ring(ring)
        final = sq or ring
        out.append(feature("building", "Polygon", [geom.close_ring(final)], source="imagery",
                           confidence=round(cl.building_conf.get(j, 0.6), 2), area_sf=round(geom.area(final), 1),
                           squared=bool(sq)))
    return out


def _min_rect_ring(ring) -> list[tuple[float, float]]:
    p = np.asarray(geom.open_ring(ring))
    hull = geom.convex_hull(p)
    best = None
    for i in range(len(hull)):
        e = hull[(i + 1) % len(hull)] - hull[i]
        le = math.hypot(*e)
        if le == 0:
            continue
        u = e / le
        v = np.array([-u[1], u[0]])
        pu, pv = hull @ u, hull @ v
        a = (pu.max() - pu.min()) * (pv.max() - pv.min())
        if best is None or a < best[0]:
            best = (a, u, v, pu.min(), pu.max(), pv.min(), pv.max())
    _a, u, v, u0, u1, v0, v1 = best
    return [tuple(map(float, u * a + v * b)) for a, b in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]


def classified_features(cl: Classified, to_xy) -> list[dict]:
    """All imagery features (CRS ft) from a classification; *to_xy(col, row)* in working pixels."""
    from scipy import ndimage as ndi

    s = cl.c.s
    feats = building_polygons(cl, to_xy)
    feats += _poly_features(cl.trees, "trees", to_xy, s, tol_ft=2.5, min_ft2=300, conf=0.8)
    feats += _poly_features(cl.grass, "grass", to_xy, s, tol_ft=3.0, min_ft2=800, conf=0.7)
    feats += _poly_features(cl.water, "water", to_xy, s, tol_ft=1.5, min_ft2=1500, conf=0.65)
    feats += _poly_features(cl.pavement, "pavement", to_xy, s, tol_ft=2.0, min_ft2=1000, conf=0.6)
    feats += _poly_features(cl.road, "road_surface", to_xy, s, tol_ft=1.0, min_ft2=500, conf=0.65)
    for ln in centerlines(cl.road_skel, s, to_xy):
        feats.append(feature("road", "LineString", [list(p) for p in ln], source="imagery", confidence=0.6,
                             length_ft=round(geom.length(ln), 1)))
    return feats


# ----------------------------------------------------------------------------- OSM merge
def _osm_masks(osm: list[dict], g: Georef, shape: tuple[int, int], factor: float) -> tuple[np.ndarray, np.ndarray]:
    H, W = shape

    def to_px(pts):
        out = []
        for p in pts:
            cc, rr = g.xy_to_px(p[0], p[1])
            out.append((cc * factor, rr * factor))
        return out

    roads = [(f["geometry"]["coordinates"], float(f["properties"].get("width_ft", 20))) for f in osm
             if f["properties"]["kind"] == "road" and f["geometry"]["type"] == "LineString"]
    px_per_ft = factor / g.ft_per_px
    rmask = geom.rasterize_lines(roads, (H, W), to_px, px_per_ft) if roads else np.zeros((H, W), bool)
    park = [f["geometry"]["coordinates"] for f in osm if f["properties"]["kind"] == "parking" and f["geometry"]["type"] == "Polygon"]
    if park:
        rmask |= geom.rasterize(park, (H, W), to_px)
    blds = [f["geometry"]["coordinates"] for f in osm if f["properties"]["kind"] in ("building", "tank")
            and f["geometry"]["type"] == "Polygon"]
    bmask = geom.rasterize(blds, (H, W), to_px) if blds else np.zeros((H, W), bool)
    return rmask, bmask


def osm_road_edges(osm: list[dict], extent: Sequence[float]) -> list[dict]:
    """Road surface polygons from OSM centrelines buffered to width (raster union, 0.5 ft)."""
    lines = [(f["geometry"]["coordinates"], float(f["properties"].get("width_ft", 20))) for f in osm
             if f["properties"]["kind"] == "road" and f["geometry"]["type"] == "LineString"]
    if not lines:
        return []
    x0, y0, x1, y1 = extent
    pad = 60
    res = max(0.5, math.sqrt((x1 - x0 + 2 * pad) * (y1 - y0 + 2 * pad) / 16e6))
    out = []
    for rings in geom.buffer_lines(lines, (x0 - pad, y0 - pad, x1 + pad, y1 + pad), res=res, tol=0.5):
        out.append(feature("road_surface", "Polygon", [geom.close_ring(r) for r in rings], source="osm", confidence=0.75,
                           note="OSM centreline buffered to tagged/typical width"))
    return out


def merge(osm: list[dict], img: list[dict], g: Georef) -> list[dict]:
    """Prefer OSM where present; imagery fills gaps."""
    x0, y0, x1, y1 = g.extent()
    grid = geom.Grid((x0, y0, x1, y1), max(1.0, g.ft_per_px))
    shape = (grid.H, grid.W)
    out = [f for f in osm if f["properties"]["kind"] not in ("trees", "grass")]
    out += osm_road_edges(osm, (x0, y0, x1, y1))

    def mask_of(feats, kinds):
        polys = [f["geometry"]["coordinates"] for f in feats if f["properties"]["kind"] in kinds and f["geometry"]["type"] == "Polygon"]
        return geom.rasterize(polys, shape, grid.to_px) if polys else np.zeros(shape, bool)

    osm_b = mask_of(osm, ("building", "tank"))
    osm_r = mask_of(out, ("road_surface", "parking"))
    osm_water = mask_of(osm, ("water",))
    osm_cl = [f["geometry"]["coordinates"] for f in osm if f["properties"]["kind"] == "road"]
    n_drop = 0
    for f in img:
        k = f["properties"]["kind"]
        gt = f["geometry"]["type"]
        if gt == "Polygon":
            m = geom.rasterize([f["geometry"]["coordinates"]], shape, grid.to_px)
            a = m.sum()
            if a == 0:
                continue
            if k == "building" and (m & osm_b).sum() > 0.3 * a:
                n_drop += 1
                continue
            if k in ("road_surface", "pavement") and (m & (osm_r | osm_b)).sum() > 0.6 * a:
                continue
            if k == "water" and ((m & osm_water).sum() > 0.3 * a or (m & (osm_b | osm_r)).sum() > 0.2 * a):
                continue
        elif k == "road" and osm_cl:
            if geom.length(f["geometry"]["coordinates"]) < 120:
                continue  # short imagery pieces next to mapped roads are driveways/aisles
            pts = geom.resample(f["geometry"]["coordinates"], 10.0)
            d = geom.distance_to_polylines(pts, osm_cl)
            if (d < 25).mean() > 0.5:
                continue
        out.append(f)
    # imagery road surfaces only where an imagery centreline survived
    cl_img = [f["geometry"]["coordinates"] for f in out if f["properties"]["kind"] == "road" and f["properties"].get("source") == "imagery"]
    keep = []
    for f in out:
        p = f["properties"]
        if p["kind"] == "road_surface" and p.get("source") == "imagery":
            ring = f["geometry"]["coordinates"][0]
            if not cl_img or not any(geom.point_in_ring(q, ring) for ln in cl_img for q in geom.resample(ln, 15.0)):
                continue  # road-shaped pavement with no surviving centreline: drop (OSM has the road)
        keep.append(f)
    return keep


# ----------------------------------------------------------------------------- package op
def _sam_regions(rgb: np.ndarray) -> np.ndarray:
    try:
        import torch  # noqa: F401
        from segment_anything import SamAutomaticMaskGenerator, sam_model_registry
    except ImportError as exc:
        raise ImportError("--method sam needs segment_anything + torch (optional; Apache-2.0/BSD) — "
                          "or use --method classic") from exc
    ck = os.environ.get("PDFCORE_SAM_CHECKPOINT")
    if not ck or not Path(ck).exists():
        raise FileNotFoundError("set PDFCORE_SAM_CHECKPOINT to a SAM checkpoint (.pth); model type from "
                                "PDFCORE_SAM_MODEL (default vit_b)")
    sam = sam_model_registry[os.environ.get("PDFCORE_SAM_MODEL", "vit_b")](checkpoint=ck)
    masks = SamAutomaticMaskGenerator(sam).generate(rgb)
    lab = np.zeros(rgb.shape[:2], np.int64)
    for i, m in enumerate(sorted(masks, key=lambda d: -d["area"]), 1):
        lab[m["segmentation"]] = i
    return lab


def preview_png(cl: Classified, rgb: np.ndarray, path: Path) -> Path:
    """Colour overlay of the classes over the image (QA)."""
    from PIL import Image

    base = (cl.c.img * 255).astype(np.uint8)
    over = base.copy().astype(float)
    cols = [(cl.grass, (150, 220, 120)), (cl.trees, (20, 120, 40)), (cl.water, (40, 110, 230)),
            (cl.pavement, (200, 200, 200)), (cl.road, (255, 170, 0)), (cl.building, (230, 30, 30))]
    for m, col in cols:
        over[m] = 0.45 * over[m] + 0.55 * np.array(col, float)
    over[cl.road_skel] = (0, 0, 0)
    Image.fromarray(over.astype(np.uint8)).save(path)
    return path


def site_extract(package: str | Path | SitePackage, *, method: str = "both", out: str | None = None,
                 preview: bool = True, quiet: bool = False) -> dict:
    """Classify + vectorize a package's imagery (and/or take OSM) → ``extracted.geojson``."""
    from PIL import Image

    pkg = package if isinstance(package, SitePackage) else SitePackage.open(package)
    if method not in METHODS:
        raise ValueError(f"--method must be one of {', '.join(METHODS)}")
    g = pkg.georef()
    osm = pkg.features("osm.geojson")
    if method == "osm" and not osm:
        raise ValueError("no osm.geojson in this package (site-from-image packages have no OSM; use --method classic)")
    feats_img: list[dict] = []
    stats: dict[str, Any] = {}
    if method in ("classic", "both", "sam"):
        rgb = np.asarray(Image.open(pkg.image_path).convert("RGB"))
        rh = bh = None
        k0 = 1.0
        if method == "both" and osm and not g.crs.is_local:
            rh, bh = _osm_masks(osm, g, rgb.shape[:2], 1.0)
        regions = _sam_regions(rgb) if method == "sam" else None
        cl, k = classify(rgb, g.ft_per_px, road_hint=rh, building_hint=bh, regions=regions)

        def to_xy(col, row, k=k):
            return g.px_to_xy(col / k, row / k)

        feats_img = classified_features(cl, to_xy)
        stats = cl.stats
        if preview:
            pv = preview_png(cl, rgb, pkg.root / "extract-preview.png")
            pkg.manifest.setdefault("files", {})["extract_preview"] = pv.name
    if method == "osm":
        feats = list(osm) + osm_road_edges(osm, g.extent())
    elif method == "both" and osm:
        feats = merge(osm, feats_img, g)
    else:
        feats = feats_img
    counts: dict[str, dict[str, int]] = {}
    for f in feats:
        p = f["properties"]
        counts.setdefault(p["kind"], {}).setdefault(p.get("source", "?"), 0)
        counts[p["kind"]][p.get("source", "?")] += 1
    name = out or "extracted.geojson"
    pkg.write_features(name, feats, g.crs, meta={"method": method, "stats": stats})
    pkg.log("site-extract", method=method, counts=counts)
    pkg.save()
    if not quiet:
        for k_, v in sorted(counts.items()):
            print(f"  {k_:<14} " + ", ".join(f"{n} from {src}" for src, n in sorted(v.items())))
    return {"path": str(pkg.root / name), "counts": counts, "stats": stats, "features": len(feats)}
