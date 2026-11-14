"""Raster primitives for site imagery (pdfcore.ops.plans.site.raster): small hand-computed cases."""

from __future__ import annotations

import numpy as np
import pytest

from pdfcore.ops.plans.site import raster as rs


def B(rows: str) -> np.ndarray:
    """Bool image from rows of '#' / '.'."""
    return np.array([[ch == "#" for ch in r] for r in rows.split()], bool)


# ----------------------------------------------------------------------------- structuring elements
def test_disk_and_cross():
    assert rs.disk(1).tolist() == rs.cross().tolist() == [[False, True, False], [True, True, True], [False, True, False]]
    d2 = rs.disk(2).astype(int)
    assert d2.tolist() == [[0, 0, 1, 0, 0], [0, 1, 1, 1, 0], [1, 1, 1, 1, 1], [0, 1, 1, 1, 0], [0, 0, 1, 0, 0]]
    assert rs.disk(3).sum() == 29  # lattice points with x² + y² ≤ 9


# ----------------------------------------------------------------------------- filters
def test_uniform_filter_symmetric_border():
    a = np.array([[1.0, 2.0, 3.0, 4.0]])
    out = rs.uniform_filter(a, (1, 3))
    # borders reflect the edge sample: [1] 1 2 3 4 [4]
    assert np.allclose(out, [[(1 + 1 + 2) / 3, 2.0, 3.0, (3 + 4 + 4) / 3]])
    img = np.arange(20, dtype=np.float32).reshape(4, 5)
    assert rs.uniform_filter(img, 3).dtype == np.float32
    assert np.isclose(rs.uniform_filter(img, 3)[1, 1], img[0:3, 0:3].mean())


def test_gaussian_filter_kernel_and_mass():
    k = rs.gaussian_kernel(1.0)
    assert len(k) == 9 and np.isclose(k.sum(), 1) and np.isclose(k[4] / k[5], np.exp(0.5))
    a = np.zeros((21, 21))
    a[10, 10] = 1
    g = rs.gaussian_filter(a, 1.5)
    assert np.isclose(g.sum(), 1) and np.isclose(g[10, 10], rs.gaussian_kernel(1.5)[6] ** 2)
    rgb = np.random.default_rng(0).random((8, 9, 3))
    g3 = rs.gaussian_filter(rgb, (0.6, 0.6, 0))  # σ = 0 leaves channels apart
    assert np.allclose(g3[..., 1], rs.gaussian_filter(rgb[..., 1], 0.6))


def test_otsu_splits_two_clusters():
    v = np.r_[np.full(100, 0.1), np.full(60, 0.9)]
    t = rs.threshold_otsu(v)
    assert 0.1 <= t < 0.9 and ((v > t) == (v == 0.9)).all()


# ----------------------------------------------------------------------------- morphology
def test_dilation_erosion_hand_cases():
    m = B("""
        .....
        .....
        ..#..
        .....
        .....""")
    assert rs.binary_dilation(m).tolist() == rs.binary_dilation(m, rs.disk(1)).tolist()
    assert rs.binary_dilation(m).sum() == 5
    assert rs.binary_dilation(m, iterations=2).sum() == 13  # diamond of radius 2
    assert rs.binary_dilation(m, rs.square(3)).sum() == 9
    sq = B("""
        .....
        .###.
        .###.
        .###.
        .....""")
    assert rs.binary_erosion(sq).tolist() == m.tolist()
    assert rs.binary_erosion(sq, rs.square(3)).tolist() == m.tolist()
    # outside the image is background: a full image erodes from its border
    full = np.ones((5, 5), bool)
    assert rs.binary_erosion(full).sum() == 9


def test_opening_and_closing():
    a = B("""
        ........
        .####...
        .####.#.
        .####...
        ........""")
    assert rs.binary_opening(a, rs.square(3)).sum() == 12  # the lone pixel goes, the block stays
    gap = B("""
        .......
        .##.##.
        .##.##.
        .......""")
    closed = rs.binary_closing(gap, rs.square(3))
    assert closed[1:3, 1:6].all() and closed.sum() == 10
    # a closing clears pixels whose element reaches past the image edge (outside counts as background)
    edge = np.ones((4, 4), bool)
    assert rs.binary_closing(edge, rs.square(3)).sum() == 4


def test_fill_holes():
    ring = B("""
        #####
        #...#
        #.#.#
        #...#
        ####.""")
    f = rs.fill_holes(ring)
    assert f[1:4, 1:4].all() and not f[4, 4]
    open_ring = B("""
        ###.#
        #...#
        #####""")
    assert rs.fill_holes(open_ring).tolist() == open_ring.tolist()  # the "hole" reaches the border


# ----------------------------------------------------------------------------- labeling
def test_label_connectivity_and_order():
    m = B("""
        #..#
        .#.#
        ...#
        ##..""")
    lab4, n4 = rs.label(m, 1)
    lab8, n8 = rs.label(m, 2)
    assert n4 == 4 and n8 == 3
    assert lab4[0, 0] == 1 and lab4[0, 3] == 2 and lab4[1, 1] == 3 and lab4[3, 0] == 4  # raster order
    assert lab8[0, 0] == lab8[1, 1] == 1
    u = B("""
        #.#
        #.#
        ###""")
    lab, n = rs.label(u)
    assert n == 1 and (lab[u] == 1).all()  # a U merges through its bottom


def test_label_values_and_find_objects():
    a = np.array([[1, 1, 2],
                  [0, 2, 2],
                  [3, 3, 0]])
    lab, n = rs.label_values(a, 1)
    assert n == 3 and lab.tolist() == [[1, 1, 2], [0, 2, 2], [3, 3, 0]]
    b = np.array([[1, 2],
                  [2, 1]])
    assert rs.label_values(b, 1)[1] == 4 and rs.label_values(b, 2)[1] == 2
    objs = rs.find_objects(lab)
    assert objs[0] == (slice(0, 1), slice(0, 2)) and objs[1] == (slice(0, 2), slice(1, 3))
    assert rs.find_objects(lab, 5)[3:] == [None, None]


def test_remove_small():
    m = B("""
        ##..#
        ##...
        .....""")
    assert rs.remove_small(m, 2).sum() == 4


# ----------------------------------------------------------------------------- distance transform
def test_distance_transform_exact():
    m = np.ones((5, 7), bool)
    m[2, 3] = False
    d = rs.distance_transform_edt(m)
    yy, xx = np.mgrid[0:5, 0:7]
    assert np.allclose(d, np.hypot(yy - 2, xx - 3))
    m2 = np.ones((4, 4), bool)
    m2[0, 0] = m2[3, 3] = False
    d2, (ri, ci) = rs.distance_transform_edt(m2, return_indices=True)
    assert d2[0, 3] == pytest.approx(3.0) and d2[1, 2] == pytest.approx(np.sqrt(5))
    assert (ri[3, 2], ci[3, 2]) == (3, 3) and (ri[0, 1], ci[0, 1]) == (0, 0)
    assert np.allclose(d2, np.hypot(ri - yy[:4, :4], ci - xx[:4, :4]))
    rng = np.random.default_rng(3)
    m3 = rng.random((30, 40)) > 0.05
    sites = np.argwhere(~m3)
    brute = np.sqrt(((np.argwhere(np.ones_like(m3))[:, None, :] - sites[None]) ** 2).sum(-1)).min(1).reshape(m3.shape)
    assert np.allclose(rs.distance_transform_edt(m3), brute)


# ----------------------------------------------------------------------------- skeleton
def test_skeleton_of_a_bar_and_a_cross():
    bar = np.zeros((9, 20), bool)
    bar[2:7, 2:18] = True
    sk = rs.skeletonize(bar)
    assert sk[4, 5:15].all() and not sk[[2, 3, 5, 6], 5:15].any()  # centre row
    nb = rs.neighbour_count(sk)
    assert ((nb[sk] == 1).sum()) == 2 and not (sk & (nb >= 3)).any()  # one line, two ends
    plus = np.zeros((21, 21), bool)
    plus[8:13, 1:20] = True
    plus[1:20, 8:13] = True
    sk = rs.skeletonize(plus)
    nb = rs.neighbour_count(sk)
    assert (sk & (nb == 1)).sum() == 4  # four arms
    assert rs.label(sk, 2)[1] == 1


def test_skeleton_diagonal_is_thin():
    d = np.zeros((30, 30), bool)
    for i in range(3, 27):
        d[i, max(0, i - 2):i + 2] = True
    sk = rs.skeletonize(d)
    nb = rs.neighbour_count(sk)
    assert rs.label(sk, 2)[1] == 1 and not (sk & (nb >= 3)).any()  # no staircase "junctions"


# ----------------------------------------------------------------------------- contours
def _shoelace(c):
    return 0.5 * abs(np.dot(c[:, 0], np.roll(c[:, 1], -1)) - np.dot(c[:, 1], np.roll(c[:, 0], -1)))


def test_find_contours_square_and_hole():
    a = np.zeros((6, 6))
    a[2:4, 2:4] = 1
    cs = rs.find_contours(a, 0.5)
    assert len(cs) == 1
    c = cs[0]
    assert (c[0] == c[-1]).all() and len(c) == 9  # closed: 8 edge midpoints + repeat
    assert _shoelace(c[:-1]) == pytest.approx(3.5)  # 2×2 square with its four corners cut: 4 − 4·⅛
    assert c[:, 0].min() == 1.5 and c[:, 0].max() == 3.5
    h = np.zeros((8, 8))
    h[1:7, 1:7] = 1
    h[3:5, 3:5] = 0
    cs = sorted(rs.find_contours(h, 0.5), key=len)
    assert len(cs) == 2 and _shoelace(cs[0][:-1]) == pytest.approx(3.5)  # the 2×2 hole
    assert _shoelace(cs[1][:-1]) == pytest.approx(35.5)  # 6×6 outline
    # linear interpolation along the edge
    g = np.array([[0.0, 1.0], [0.0, 1.0]])
    (c,) = rs.find_contours(g, 0.25)
    assert np.allclose(c[:, 1], 0.25) and c[0, 0] != c[-1, 0]  # open contour crossing the array


def test_find_contours_saddle_keeps_high_apart():
    a = np.array([[0, 0, 0, 0],
                  [0, 1, 0, 0],
                  [0, 0, 1, 0],
                  [0, 0, 0, 0]], float)
    assert len(rs.find_contours(a, 0.5)) == 2


def test_solidity():
    sq = np.zeros((10, 10), bool)
    sq[2:7, 2:7] = True
    assert rs.solidity(sq) == pytest.approx(1.0)
    ell = np.zeros((10, 10), bool)
    ell[2:8, 2:4] = True
    ell[6:8, 2:8] = True  # L: 20 px, hull covers 26 pixel centres
    assert rs.solidity(ell) == pytest.approx(20 / 26)


# ----------------------------------------------------------------------------- segmentation
def _reference_fh(img, scale, min_size):
    """Plain sequential Felzenszwalb–Huttenlocher (no smoothing) for comparison."""
    H, W, _ = img.shape
    idx = np.arange(H * W).reshape(H, W)
    S = slice(None)
    pr = [((S, slice(0, -1)), (S, slice(1, None))), ((slice(0, -1), S), (slice(1, None), S)),
          ((slice(0, -1), slice(0, -1)), (slice(1, None), slice(1, None))),
          ((slice(1, None), slice(0, -1)), (slice(0, -1), slice(1, None)))]
    u = np.concatenate([idx[a].ravel() for a, _ in pr])
    v = np.concatenate([idx[b].ravel() for _, b in pr])
    w = np.concatenate([np.sqrt(((img[a] - img[b]) ** 2).sum(-1)).astype(np.float32).ravel() for a, b in pr])
    o = np.argsort(w, kind="stable")
    p, sz, it, k = list(range(H * W)), [1] * (H * W), [0.0] * (H * W), scale / 255
    def f(a):
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a
    for a, b, x in zip(u[o].tolist(), v[o].tolist(), w[o].tolist()):
        ra, rb = f(a), f(b)
        if ra != rb and x <= min(it[ra] + k / sz[ra], it[rb] + k / sz[rb]):
            p[rb] = ra
            sz[ra] += sz[rb]
            it[ra] = x
    for a, b in zip(u[o].tolist(), v[o].tolist()):
        ra, rb = f(a), f(b)
        if ra != rb and (sz[ra] < min_size or sz[rb] < min_size):
            p[rb] = ra
            sz[ra] += sz[rb]
    return np.array([f(i) for i in range(H * W)]).reshape(H, W)


def _same_partition(a, b):
    joint = len(np.unique(a.astype(np.int64) * 10**6 + b))
    return joint == len(np.unique(a)) == len(np.unique(b))


def test_felzenszwalb_two_flat_regions():
    img = np.zeros((12, 16, 3))
    img[:, 8:] = (0.8, 0.2, 0.1)
    lab = rs.felzenszwalb(img, scale=50, sigma=0, min_size=5)
    assert lab.max() == 1 and _same_partition(lab, (np.arange(16) >= 8)[None].repeat(12, 0))


def test_felzenszwalb_matches_sequential_reference():
    rng = np.random.default_rng(5)
    img = np.zeros((30, 40, 3))
    img[:, :20] = (0.3, 0.5, 0.2)
    img[:, 20:] = (0.6, 0.6, 0.6)
    img[8:20, 25:35] = (0.2, 0.2, 0.25)
    img += rng.normal(0, 0.03, img.shape)
    ref = _reference_fh(img, 150, 10)
    lab = rs.felzenszwalb(img, scale=150, sigma=0, min_size=10, batch=64)
    assert _same_partition(lab, ref)


def test_merge_regions_by_mean_colour():
    img = np.zeros((4, 6))
    img[:, 2:4] = 10
    img[:, 4:] = 12
    labels = np.repeat(np.array([[0, 0, 1, 1, 2, 2]]), 4, 0)
    out = rs.merge_regions(img, labels, 5)
    assert out[0].tolist() == [0, 0, 1, 1, 1, 1]
    assert rs.merge_regions(img, labels, 1).max() == 2


# ----------------------------------------------------------------------------- resampling
def test_resampling():
    a = np.arange(4).reshape(2, 2)
    assert rs.resize_nearest(a, (4, 4)).tolist() == [[0, 0, 1, 1], [0, 0, 1, 1], [2, 2, 3, 3], [2, 2, 3, 3]]
    src = np.array([[0.0, 10.0], [20.0, 30.0]])
    v = rs.sample_bilinear(src, np.array([0.5, -3.0, 0.0]), np.array([0.5, 0.0, 5.0]))
    assert np.allclose(v, [15.0, 0.0, 10.0])  # centre; beyond the edge the edge pixel repeats
    img = np.ones((40, 60, 3), np.float32) * 0.5
    small = rs.rescale(img, 0.5)
    assert small.shape == (20, 30, 3) and np.allclose(small, 0.5, atol=1e-5)
