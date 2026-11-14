"""Coordinate systems, georeferencing and the site-package folder format.

Working coordinates are **feet in a local projected CRS**: the State Plane zone that contains the site
(``NAD83 / <zone> (ftUS)`` or ``(ft)`` from the EPSG database via pyproj), else UTM in US survey feet.
Images calibrated only by a known distance get a ``LOCAL`` engineering CRS (feet, origin at the image's
lower-left, +y = north).

Georef (``georef.json``): pixel corner coordinates (col, row; (0, 0) = top-left corner of the top-left
pixel) map to CRS feet with an affine ``x = a·col + b·row + c``, ``y = d·col + e·row + f``. A standard
ESRI world file (``.pgw``, pixel-centre convention) is written next to the image for GIS/CAD tools.

Package folder::

    manifest.json      kind, source, CRS, extent, attribution, file list, history
    imagery.png/.pgw   the image (USGS orthoimagery or the user's image)
    georef.json        affine + CRS + feet per pixel + site origin
    osm.geojson        OpenStreetMap features in the CRS (site-fetch --osm)
    extracted.geojson  classified + vectorized features (site-extract)
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

US_FT = 1200 / 3937  # metres per US survey foot
INT_FT = 0.3048
PACKAGE_VERSION = 1

Pt = tuple[float, float]


# ----------------------------------------------------------------------------- CRS
@dataclass
class CRSInfo:
    crs: str  # "EPSG:2253", a PROJ string, or "LOCAL"
    name: str
    unit: str  # "ft" (international) | "us-ft" | "ft-local"
    image_wkid: int | None = None  # what to ask an ArcGIS server for
    image_to_ft: float = 1.0  # image-SR units → our feet (UTM metres → us-ft)

    def to_dict(self) -> dict:
        return {"crs": self.crs, "name": self.name, "unit": self.unit, "image_wkid": self.image_wkid,
                "image_to_ft": self.image_to_ft}

    @classmethod
    def from_dict(cls, d: dict) -> "CRSInfo":
        return cls(d["crs"], d.get("name", d["crs"]), d.get("unit", "ft"), d.get("image_wkid"), float(d.get("image_to_ft", 1.0)))

    @property
    def is_local(self) -> bool:
        return self.crs.upper() == "LOCAL"


LOCAL_CRS = CRSInfo("LOCAL", "Local site grid (feet, from image calibration)", "ft-local")


def choose_crs(lon: float, lat: float) -> CRSInfo:
    """State Plane zone (feet) containing (lon, lat), else UTM in US survey feet."""
    from pyproj.aoi import AreaOfInterest
    from pyproj.database import query_crs_info
    from pyproj.enums import PJType

    cands = []
    try:
        infos = query_crs_info(auth_name="EPSG", pj_types=[PJType.PROJECTED_CRS],
                               area_of_interest=AreaOfInterest(lon, lat, lon, lat), contains=True)
    except Exception:
        infos = []
    for c in infos:
        m = re.fullmatch(r"NAD83 / (?!BLM|UTM|Conus|USA)(.+) \((ftUS|ft)\)", c.name)
        if not m or c.deprecated:
            continue
        b = c.area_of_use.bounds if c.area_of_use else (-180, -90, 180, 90)
        area = (b[2] - b[0]) * (b[3] - b[1])
        cands.append((0 if m.group(2) == "ftUS" else 1, area, c))
    if cands:
        cands.sort(key=lambda t: (t[1], t[0]))
        c = cands[0][2]
        return CRSInfo(f"EPSG:{c.code}", c.name, "us-ft" if c.name.endswith("(ftUS)") else "ft", int(c.code), 1.0)
    zone = int((lon + 180) // 6) + 1
    north = lat >= 0
    wkid = (26900 + zone) if north and -172 <= lon <= -48 else ((32600 if north else 32700) + zone)
    datum = "+datum=NAD83" if 26900 < wkid < 27000 else "+datum=WGS84"
    proj = f"+proj=utm +zone={zone} {'+south ' if not north else ''}{datum} +units=us-ft +no_defs"
    return CRSInfo(proj, f"UTM zone {zone}{'N' if north else 'S'} (US survey ft)", "us-ft", wkid, 1.0 / US_FT)


def _pyproj_crs(info: CRSInfo):
    from pyproj import CRS

    return CRS.from_user_input(info.crs)


def to_crs(info: CRSInfo, lon: float, lat: float) -> Pt:
    if info.is_local:
        raise ValueError("a LOCAL (calibrated-only) site grid has no lat/lon; add --georef control points")
    from pyproj import Transformer

    t = Transformer.from_crs("EPSG:4326", _pyproj_crs(info), always_xy=True)
    x, y = t.transform(lon, lat)
    return float(x), float(y)


def many_to_crs(info: CRSInfo, lonlat: Iterable[Sequence[float]]) -> list[Pt]:
    from pyproj import Transformer

    t = Transformer.from_crs("EPSG:4326", _pyproj_crs(info), always_xy=True)
    pts = list(lonlat)
    if not pts:
        return []
    xs, ys = t.transform([p[0] for p in pts], [p[1] for p in pts])
    return [(float(a), float(b)) for a, b in zip(xs, ys)]


def to_lonlat(info: CRSInfo, x: float, y: float) -> Pt:
    from pyproj import Transformer

    t = Transformer.from_crs(_pyproj_crs(info), "EPSG:4326", always_xy=True)
    lon, lat = t.transform(x, y)
    return float(lon), float(lat)


def convergence_deg(info: CRSInfo, lon: float, lat: float) -> float:
    """Grid-north vs true-north (meridian convergence), degrees; 0 for LOCAL grids."""
    if info.is_local:
        return 0.0
    try:
        from pyproj import Proj

        f = Proj(_pyproj_crs(info)).get_factors(lon, lat)
        return float(f.meridian_convergence)
    except Exception:
        return 0.0


# ----------------------------------------------------------------------------- georef
@dataclass
class Georef:
    crs: CRSInfo
    transform: tuple[float, float, float, float, float, float]  # a, b, c, d, e, f (pixel corners)
    width: int
    height: int
    origin: Pt | None = None  # site origin for job coordinates (CRS ft); default image centre
    north_deg: float = 0.0  # LOCAL grids: rotation of true north, clockwise from image-up (info only)
    method: str = ""
    residual_ft: float | None = None

    # pixel <-> CRS
    def px_to_xy(self, col: float, row: float) -> Pt:
        a, b, c, d, e, f = self.transform
        return (a * col + b * row + c, d * col + e * row + f)

    def xy_to_px(self, x: float, y: float) -> Pt:
        a, b, c, d, e, f = self.transform
        det = a * e - b * d
        x0, y0 = x - c, y - f
        return ((e * x0 - b * y0) / det, (-d * x0 + a * y0) / det)

    @property
    def ft_per_px(self) -> float:
        a, b, c, d, e, f = self.transform
        return math.sqrt(abs(a * e - b * d))

    @property
    def rotation_deg(self) -> float:
        """Angle of the image's column axis from grid east (0 for north-up imagery)."""
        a, _b, _c, d, _e, _f = self.transform
        return math.degrees(math.atan2(d, a))

    def corners(self) -> list[Pt]:
        return [self.px_to_xy(0, 0), self.px_to_xy(self.width, 0), self.px_to_xy(self.width, self.height),
                self.px_to_xy(0, self.height)]

    def extent(self) -> tuple[float, float, float, float]:
        cs = self.corners()
        xs, ys = [p[0] for p in cs], [p[1] for p in cs]
        return (min(xs), min(ys), max(xs), max(ys))

    def site_origin(self) -> Pt:
        if self.origin is not None:
            return self.origin
        return self.px_to_xy(self.width / 2, self.height / 2)

    def to_dict(self) -> dict:
        x0, y0, x1, y1 = self.extent()
        return {"crs": self.crs.to_dict(), "transform": list(self.transform), "width": self.width, "height": self.height,
                "ft_per_px": self.ft_per_px, "origin": list(self.site_origin()), "north_deg": self.north_deg,
                "rotation_deg": self.rotation_deg, "extent_ft": [x0, y0, x1, y1], "method": self.method,
                "residual_ft": self.residual_ft,
                "convention": "x = a*col + b*row + c, y = d*col + e*row + f; (col,row) = pixel corners, (0,0) top-left"}

    @classmethod
    def from_dict(cls, d: dict) -> "Georef":
        return cls(CRSInfo.from_dict(d["crs"]), tuple(float(v) for v in d["transform"]), int(d["width"]), int(d["height"]),
                   tuple(d["origin"]) if d.get("origin") else None, float(d.get("north_deg", 0.0)), d.get("method", ""),
                   d.get("residual_ft"))

    def world_file(self) -> str:
        """ESRI world file lines (A, D, B, E, C, F) — pixel-centre convention."""
        a, b, c, d, e, f = self.transform
        cx, cy = a * 0.5 + b * 0.5 + c, d * 0.5 + e * 0.5 + f
        return "\n".join(f"{v:.10f}" for v in (a, d, b, e, cx, cy)) + "\n"


def georef_from_extent(crs: CRSInfo, extent: Sequence[float], width: int, height: int, method: str) -> Georef:
    """North-up image covering *extent* (xmin, ymin, xmax, ymax, CRS ft)."""
    x0, y0, x1, y1 = extent
    sx = (x1 - x0) / width
    sy = (y1 - y0) / height
    return Georef(crs, (sx, 0.0, x0, 0.0, -sy, y1), width, height, method=method)


def similarity_fit(src: Sequence[Pt], dst: Sequence[Pt]) -> tuple[tuple[float, float, float, float, float, float], float]:
    """Least-squares similarity (scale, rotation, shift; no shear) from image pixels *src* to *dst* (x, y).

    Image rows grow downward, so the fit is done on (col, -row) and mapped back. Returns (affine, rms residual).
    """
    import numpy as np

    if len(src) < 2:
        raise ValueError("need at least two control points")
    P = np.array([(c, -r) for c, r in src], float)
    Q = np.array(dst, float)
    pm, qm = P.mean(0), Q.mean(0)
    p, q = P - pm, Q - qm
    # complex-number similarity: q ≈ z · p
    pc = p[:, 0] + 1j * p[:, 1]
    qc = q[:, 0] + 1j * q[:, 1]
    z = (np.conj(pc) @ qc) / (np.conj(pc) @ pc)
    if abs(z) == 0:
        raise ValueError("control points coincide")
    A, B = z.real, z.imag  # x = A·u − B·v, y = B·u + A·v  with (u, v) = (col, −row) − pm
    tx = qm[0] - (A * pm[0] - B * pm[1])
    ty = qm[1] - (B * pm[0] + A * pm[1])
    # in (col, row): v = −row
    aff = (float(A), float(B), float(tx), float(B), float(-A), float(ty))
    pred = np.array([(aff[0] * c + aff[1] * r + aff[2], aff[3] * c + aff[4] * r + aff[5]) for c, r in src])
    rms = float(np.sqrt(((pred - Q) ** 2).sum(1).mean()))
    return aff, rms


# ----------------------------------------------------------------------------- package I/O
ATTR_OSM = "© OpenStreetMap contributors (ODbL 1.0, openstreetmap.org/copyright)"
ATTR_USGS = "Imagery: USGS The National Map (public domain)"


@dataclass
class SitePackage:
    root: Path
    manifest: dict = field(default_factory=dict)

    # paths
    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def image_path(self) -> Path:
        return self.root / self.manifest.get("files", {}).get("imagery", "imagery.png")

    @property
    def georef_path(self) -> Path:
        return self.root / "georef.json"

    def georef(self) -> Georef:
        return Georef.from_dict(json.loads(self.georef_path.read_text()))

    def save_georef(self, g: Georef) -> None:
        self.georef_path.write_text(json.dumps(g.to_dict(), indent=2))
        self.image_path.with_suffix(".pgw").write_text(g.world_file())
        x0, y0, x1, y1 = g.extent()
        self.manifest["crs"] = g.crs.to_dict()
        self.manifest["extent_ft"] = [float(v) for v in (x0, y0, x1, y1)]
        self.manifest["size_ft"] = [float(x1 - x0), float(y1 - y0)]
        self.manifest["ft_per_px"] = float(g.ft_per_px)

    def features(self, name: str) -> list[dict]:
        p = self.root / name
        if not p.exists():
            return []
        return json.loads(p.read_text()).get("features", [])

    def write_features(self, name: str, feats: list[dict], crs: CRSInfo, meta: dict | None = None) -> Path:
        p = self.root / name
        fc = {"type": "FeatureCollection", "name": Path(name).stem,
              "crs": {"type": "name", "properties": {"name": crs.crs, "title": crs.name, "unit": crs.unit}},
              "features": feats}
        if meta:
            fc["meta"] = meta
        p.write_text(json.dumps(fc, separators=(",", ":")))
        json.loads(p.read_text())  # verify it parses
        self.manifest.setdefault("files", {})[Path(name).stem] = name
        return p

    def log(self, step: str, **info: Any) -> None:
        self.manifest.setdefault("history", []).append(
            {"step": step, "at": dt.datetime.now().isoformat(timespec="seconds"), **info})

    def add_attribution(self, *lines: str) -> None:
        cur = self.manifest.setdefault("attribution", [])
        for ln in lines:
            if ln and ln not in cur:
                cur.append(ln)

    def save(self) -> None:
        self.manifest.setdefault("kind", "pdf-studio site package")
        self.manifest["version"] = PACKAGE_VERSION
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2))

    @classmethod
    def create(cls, root: str | Path, **manifest: Any) -> "SitePackage":
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"{root} exists and is not empty — pick a new folder (packages are never overwritten)")
        root.mkdir(parents=True, exist_ok=True)
        pkg = cls(root, {"kind": "pdf-studio site package", "version": PACKAGE_VERSION,
                         "created": dt.datetime.now().isoformat(timespec="seconds"), **manifest})
        return pkg

    @classmethod
    def open(cls, root: str | Path) -> "SitePackage":
        root = Path(root)
        if root.is_file() and root.name == "manifest.json":
            root = root.parent
        mp = root / "manifest.json"
        if not mp.exists():
            raise FileNotFoundError(f"{root} is not a site package (no manifest.json) — make one with "
                                    "`pdfcore plans site-fetch` or `site-from-image`")
        return cls(root, json.loads(mp.read_text()))


def new_package_dir(base: str | Path) -> Path:
    """*base* if free, else base-2, base-3 …"""
    base = Path(base)
    cand, n = base, 2
    while cand.exists() and any(cand.iterdir()):
        cand = base.with_name(f"{base.name}-{n}")
        n += 1
    return cand


def feature(kind: str, geom_type: str, coords: Any, **props: Any) -> dict:
    return {"type": "Feature", "geometry": {"type": geom_type, "coordinates": coords}, "properties": {"kind": kind, **props}}
