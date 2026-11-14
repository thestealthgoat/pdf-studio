"""A synthetic aerial image with known geometry (tests, demos, tuning) — no network, no third-party imagery.

1.0 ft/px, 900 × 700 px, NAIP-like colours: grass, tree clusters with shadows, three roads (one diagonal),
a striped parking lot with cars, six roofs (light, red, tan L-shape, dark, white shed, two-tone gable; one
rotated 25°) casting shadows to the south-east, and a pond. Ground truth is returned in site feet
(x east, y north; origin at the image centre, like a LOCAL calibrated package).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

W, H = 900, 700
FT_PER_PX = 1.0
SUN = (0.6, 0.8)  # (drow, dcol): shadows fall down-right (south-east), ft of shadow per ft of height × 0.5


def _px_to_ft(c: float, r: float) -> tuple[float, float]:
    return ((c - W / 2) * FT_PER_PX, (H / 2 - r) * FT_PER_PX)


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _rot(pts, cx, cy, deg):
    t = math.radians(deg)
    return [(cx + (x - cx) * math.cos(t) - (y - cy) * math.sin(t), cy + (x - cx) * math.sin(t) + (y - cy) * math.cos(t))
            for x, y in pts]


BUILDINGS = [  # (pixel ring, roof rgb (or two for gable), height ft)
    (_rect(120, 120, 240, 200), (0.80, 0.80, 0.79), 20),
    (_rot(_rect(400, 150, 460, 190), 430, 170, 25), (0.56, 0.31, 0.26), 15),
    ([(700, 100), (820, 100), (820, 160), (750, 160), (750, 260), (700, 260)], (0.74, 0.63, 0.47), 18),
    (_rect(700, 500, 770, 550), (0.34, 0.35, 0.37), 14),
    (_rect(300, 520, 340, 550), (0.88, 0.88, 0.87), 10),
    (_rect(480, 520, 530, 554), ((0.63, 0.55, 0.50), (0.46, 0.40, 0.37)), 16),
]
ROADS = [  # (pixel centreline, width ft)
    ([(0, 430), (900, 430)], 28),
    ([(640, 0), (640, 430)], 24),
    ([(100, 700), (262, 444)], 22),
]
POND = (390, 620, 55, 38)  # cx, cy, rx, ry (px)
PARKING = _rect(380, 300, 560, 416)
TREES = [(330, 80, 18), (355, 95, 14), (300, 100, 12), (560, 90, 16), (585, 60, 20), (60, 300, 22), (90, 330, 16),
         (840, 330, 18), (860, 360, 14), (250, 600, 15), (600, 640, 18), (820, 640, 16), (40, 60, 15)]


def make_image(seed: int = 7) -> tuple[np.ndarray, dict]:
    """(RGB uint8 image, ground truth in site ft)."""
    from PIL import Image, ImageDraw

    from pdfcore.ops.plans.site.raster import gaussian_filter

    rng = np.random.default_rng(seed)

    def noise(sig, smooth):
        n = rng.normal(0, 1, (H, W)).astype(np.float32)
        if smooth:
            n = gaussian_filter(n, smooth)
            n /= n.std() + 1e-9
        return n * sig

    img = np.zeros((H, W, 3), np.float32)
    grass = np.array([0.42, 0.55, 0.30], np.float32)
    img[:] = grass
    img += (noise(0.012, 4) + noise(0.012, 0))[..., None]

    def mask_poly(pts):
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).polygon([tuple(p) for p in pts], fill=255)
        return np.asarray(m) > 0

    def mask_line(pts, width):
        m = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(m)
        d.line([tuple(p) for p in pts], fill=255, width=int(width))
        return np.asarray(m) > 0

    # pond
    cx, cy, rx, ry = POND
    yy, xx = np.mgrid[0:H, 0:W]
    pond = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1
    img[pond] = np.array([0.13, 0.22, 0.27]) + noise(0.008, 2)[pond][:, None]
    # roads and parking
    asphalt = np.array([0.37, 0.37, 0.38], np.float32)
    for pts, w in ROADS:
        m = mask_line(pts, w)
        img[m] = asphalt + noise(0.02, 1)[m][:, None]
    pk = mask_poly(PARKING)
    img[pk] = np.array([0.60, 0.60, 0.58]) + noise(0.02, 1)[pk][:, None]
    # centre dashes
    for x in range(10, 900, 40):
        img[429:431, x:x + 20] = (0.85, 0.75, 0.25)
    # stalls + cars
    x0, y0, x1, y1 = PARKING[0][0], PARKING[0][1], PARKING[2][0], PARKING[2][1]
    for row_y in (y0 + 2, y1 - 20):
        for sx in np.arange(x0 + 2, x1 - 2, 9):
            img[int(row_y):int(row_y) + 18, int(sx):int(sx) + 1] = (0.92, 0.92, 0.9)
            if rng.random() < 0.45:
                col = rng.choice([(0.1, 0.1, 0.12), (0.8, 0.1, 0.1), (0.9, 0.9, 0.9), (0.2, 0.3, 0.6), (0.5, 0.5, 0.5)])
                img[int(row_y) + 2:int(row_y) + 16, int(sx) + 2:int(sx) + 8] = col
    # shadows: buildings and trees (drawn on the ground before roofs/canopies)
    shadow = np.zeros((H, W), bool)
    dr, dc = SUN
    for ring, _col, h in BUILDINGS:
        off = 0.5 * h / FT_PER_PX
        sw = [(x + dc * off, y + dr * off) for x, y in ring]
        for a, b, a2, b2 in zip(ring, ring[1:] + ring[:1], sw, sw[1:] + sw[:1]):
            shadow |= mask_poly([a, b, b2, a2])
        shadow |= mask_poly(sw)
    for tx, ty, r in TREES:
        off = 0.5 * 30 / FT_PER_PX
        shadow |= ((xx - tx - dc * off) / r) ** 2 + ((yy - ty - dr * off) / r) ** 2 <= 1
    shadow &= ~pond
    img[shadow] *= 0.32
    # roofs
    truth_b = []
    for ring, col, h in BUILDINGS:
        m = mask_poly(ring)
        if isinstance(col[0], tuple):  # gable: two halves along the long axis
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            mid = (min(ys) + max(ys)) / 2
            top = m & (yy < mid)
            img[top] = np.array(col[0]) + noise(0.012, 1)[top][:, None]
            bot = m & (yy >= mid)
            img[bot] = np.array(col[1]) + noise(0.012, 1)[bot][:, None]
        else:
            img[m] = np.array(col) + noise(0.012, 1)[m][:, None]
        # roof equipment on the big flat roof
        if h == 20:
            for ux, uy in ((150, 140), (190, 150), (210, 175)):
                img[uy:uy + 6, ux:ux + 8] = (0.55, 0.55, 0.56)
        truth_b.append([_px_to_ft(x, y) for x, y in ring])
    # tree canopies (textured, darker on the shadow side)
    tex = noise(0.07, 0.8)
    for tx, ty, r in TREES:
        m = ((xx - tx) / r) ** 2 + ((yy - ty) / r) ** 2 <= 1
        shade = 1.0 - 0.25 * np.clip(((xx - tx) * dc + (yy - ty) * dr) / r, -1, 1)
        base = np.array([0.25, 0.40, 0.19])
        img[m] = (base[None, :] * shade[m][:, None]) + tex[m][:, None] * 0.6
    img = np.clip(img, 0, 1)
    truth = {
        "buildings": truth_b,
        "roads": [([_px_to_ft(x, y) for x, y in pts], w) for pts, w in ROADS],
        "pond": [_px_to_ft(cx + rx * math.cos(t), cy + ry * math.sin(t)) for t in np.linspace(0, 2 * math.pi, 73)[:-1]],
        "ft_per_px": FT_PER_PX,
        "size_px": (W, H),
    }
    return (img * 255).round().astype(np.uint8), truth


def add_scale_bar(rgb: np.ndarray, ft: float = 100, at: tuple[int, int] = (40, 660)) -> tuple[np.ndarray, dict]:
    """Draw a white scale bar with a '100 ft' label (Inter) like a map screenshot; returns (image, bar info)."""
    from PIL import Image, ImageDraw, ImageFont

    from pdfcore.fonts import font_path

    im = Image.fromarray(rgb)
    d = ImageDraw.Draw(im)
    x, y = at
    L = int(round(ft / FT_PER_PX))
    d.rectangle((x - 6, y - 34, x + L + 6, y + 10), fill=(30, 30, 30))
    d.rectangle((x, y, x + L, y + 3), fill=(255, 255, 255))
    for xx in (x, x + L):
        d.rectangle((xx, y - 6, xx + 2, y + 3), fill=(255, 255, 255))
    f = ImageFont.truetype(str(font_path("Inter-SemiBold.ttf")), 18)
    label = f"{ft:g} ft"
    tw = d.textlength(label, font=f)
    d.text((x + L / 2 - tw / 2, y - 30), label, fill=(255, 255, 255), font=f)
    return np.asarray(im), {"p0": (x, y + 1.5), "p1": (x + L, y + 1.5), "ft": ft}


def make_package(root: str | Path, *, seed: int = 7):
    """Write the synthetic image as a LOCAL-grid site package; returns (SitePackage, truth)."""
    from PIL import Image

    from pdfcore.ops.plans.site.geo import SitePackage
    from pdfcore.ops.plans.site.ingest import local_georef

    rgb, truth = make_image(seed)
    pkg = SitePackage.create(root, source="synthetic", imagery_source="synthetic test image")
    Image.fromarray(rgb).save(pkg.root / "imagery.png")
    pkg.manifest["files"] = {"imagery": "imagery.png", "georef": "georef.json", "world_file": "imagery.pgw"}
    pkg.save_georef(local_georef(W, H, FT_PER_PX, 0.0, "synthetic (1 ft/px)"))
    pkg.add_attribution("Synthetic test image")
    pkg.save()
    return pkg, truth
