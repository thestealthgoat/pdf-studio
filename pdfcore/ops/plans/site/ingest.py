"""Make a site package: from public data (``site-fetch``) or from any image (``site-from-image``)."""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from pdfcore.ops.plans.site.geo import (ATTR_OSM, ATTR_USGS, LOCAL_CRS, CRSInfo, Georef, SitePackage, choose_crs,
                                        convergence_deg, many_to_crs, new_package_dir, similarity_fit, to_crs, to_lonlat)

GOOGLE_NOTICE = ("note: Google's terms restrict tracing Google Earth/Maps imagery — use it for reference only; "
                 "for client deliverables run `pdfcore plans site-fetch` (USGS public-domain imagery + OpenStreetMap).")


# ----------------------------------------------------------------------------- units
def length_ft(s: str | float, default_unit: str = "ft") -> float:
    from pdfcore.ops.plans.measure import parse_length

    if isinstance(s, (int, float)):
        return float(s)
    return parse_length(str(s), default_unit)[0] / 12.0


def parse_size(s: str) -> tuple[float, float]:
    """'600ft' → (600, 600); '800x500ft' / "800' x 500'" / '200m' → feet."""
    t = str(s).strip().lower().replace("×", "x")
    m = re.fullmatch(r"([\d.]+)\s*(ft|'|m|feet|meters?)?\s*x\s*([\d.]+)\s*(ft|'|m|feet|meters?)?", t)
    if m:
        u = m.group(4) or m.group(2) or "ft"
        return length_ft(m.group(1) + u.replace("'", "ft")), length_ft(m.group(3) + u.replace("'", "ft"))
    v = length_ft(t)
    return v, v


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "site"


# ----------------------------------------------------------------------------- site-fetch
def site_fetch(*, address: str | None = None, center: str | Sequence[float] | None = None, size: str = "600ft",
               bbox: str | Sequence[float] | None = None, res: str = "0.3m", osm: bool = True,
               imagery: str = "auto", out: str | Path | None = None, quiet: bool = False) -> SitePackage:
    """Public-domain USGS orthoimagery + OpenStreetMap vectors for a site → package folder."""
    from pdfcore.ops.plans.site import sources

    geo_info: dict[str, Any] = {}
    if address:
        if not quiet:
            print(f"  geocoding {address!r}…")
        g = sources.geocode(address, quiet=quiet)
        lat, lon = g["lat"], g["lon"]
        geo_info = {"address": address, "matched": g["matched"], "geocoder": g["source"]}
    elif center is not None:
        lat, lon = (float(v) for v in (re.split(r"[,\s]+", center.strip()) if isinstance(center, str) else center))
    elif bbox is not None:
        w, s, e, n = (float(v) for v in (re.split(r"[,\s]+", bbox.strip()) if isinstance(bbox, str) else bbox))
        if w > e or s > n:
            raise ValueError("--bbox is w,s,e,n (lon/lat, west < east, south < north)")
        lat, lon = (s + n) / 2, (w + e) / 2
    else:
        raise ValueError("give --address, --center lat,lon (+ --size) or --bbox w,s,e,n")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"bad coordinates {lat}, {lon} (lat, lon)")
    crs = choose_crs(lon, lat)
    if bbox is not None and not address and center is None:
        edge = [(w + (e - w) * t, s) for t in np.linspace(0, 1, 9)] + [(e, s + (n - s) * t) for t in np.linspace(0, 1, 9)] + \
               [(w + (e - w) * t, n) for t in np.linspace(0, 1, 9)] + [(w, s + (n - s) * t) for t in np.linspace(0, 1, 9)]
        xy = np.array(many_to_crs(crs, edge))
        extent = (float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max()))
    else:
        sw, sh = parse_size(size)
        cx, cy = to_crs(crs, lon, lat)
        extent = (cx - sw / 2, cy - sh / 2, cx + sw / 2, cy + sh / 2)
    ft_px = length_ft(res, "m")
    if ft_px <= 0:
        raise ValueError("--res must be positive")
    wpx, hpx = (extent[2] - extent[0]) / ft_px, (extent[3] - extent[1]) / ft_px
    if wpx * hpx > 120e6:
        raise ValueError(f"{wpx:.0f}×{hpx:.0f} px is too large — use a coarser --res or a smaller area")
    root = Path(out) if out else new_package_dir(Path.cwd() / f"site-{_slug(address) if address else f'{lat:.5f}_{lon:.5f}'}")
    pkg = SitePackage.create(root, source="site-fetch", center_lonlat=[lon, lat], request={
        "address": address, "center": [lat, lon] if center is not None else None, "size": size, "bbox": bbox, "res": res,
        "osm": osm}, **geo_info)
    if not quiet:
        print(f"  CRS: {crs.name} ({crs.crs}); area {extent[2] - extent[0]:.0f} × {extent[3] - extent[1]:.0f} ft "
              f"at {ft_px:.2f} ft/px")
        print("  imagery: USGS The National Map (public domain)…")
    services = ["naip", "usgs"] if imagery == "auto" else [imagery]
    err = None
    for svc in services:
        try:
            img, g = sources.usgs_imagery(crs, extent, ft_px, service=svc, quiet=quiet)
            if np.asarray(img.convert("L")).std() < 2:  # blank tile (no coverage)
                raise ValueError(f"{svc}: no imagery coverage here")
            break
        except Exception as exc:
            err = exc
            if not quiet and svc != services[-1]:
                print(f"  {svc} imagery unavailable ({exc}) — trying {services[services.index(svc) + 1]}")
    else:
        raise RuntimeError(f"imagery download failed: {err}")
    img.save(pkg.root / "imagery.png", optimize=True)
    pkg.manifest["files"] = {"imagery": "imagery.png", "georef": "georef.json", "world_file": "imagery.pgw"}
    pkg.manifest["imagery_source"] = sources.IMAGERY[svc][1]
    pkg.manifest["convergence_deg"] = float(convergence_deg(crs, lon, lat))
    pkg.save_georef(g)
    pkg.add_attribution(sources.IMAGERY[svc][1])
    ll = [to_lonlat(crs, x, y) for x, y in g.corners()]
    bb = (min(p[0] for p in ll), min(p[1] for p in ll), max(p[0] for p in ll), max(p[1] for p in ll))
    pkg.manifest["bbox_lonlat"] = [float(v) for v in bb]
    pkg.log("site-fetch imagery", px=[g.width, g.height], ft_per_px=g.ft_per_px)
    if osm:
        try:
            data = sources.fetch_osm(bb, quiet=quiet)
            feats = sources.osm_features(data, crs)
            pkg.write_features("osm.geojson", feats, crs, meta={"source": "OpenStreetMap via Overpass",
                                                               "license": "ODbL 1.0", "attribution": ATTR_OSM})
            pkg.add_attribution(ATTR_OSM)
            kinds: dict[str, int] = {}
            for f in feats:
                kinds[f["properties"]["kind"]] = kinds.get(f["properties"]["kind"], 0) + 1
            pkg.log("site-fetch osm", features=len(feats), kinds=kinds)
            if not quiet:
                print("  OpenStreetMap: " + (", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) or "no features"))
        except Exception as exc:  # imagery is still useful without OSM
            pkg.log("site-fetch osm failed", error=str(exc))
            if not quiet:
                print(f"  warning: OpenStreetMap skipped ({exc}); run site-extract --method classic, or retry later")
    pkg.save()
    return pkg


# ----------------------------------------------------------------------------- scale bar
_DIST = re.compile(r"^(\d+(?:[.,]\d+)?)(ft|feet|foot|m|meters?|metres?|km|mi|miles?|yd|yards?|')?$", re.I)


def _ocr_words(img, scale: int = 2) -> list[dict]:
    import pytesseract
    from PIL import ImageOps

    out = []
    g = img.convert("L")
    for inv in (False, True):
        im = ImageOps.invert(g) if inv else g
        im = im.resize((im.width * scale, im.height * scale))
        try:
            d = pytesseract.image_to_data(im, config="--psm 11", output_type=pytesseract.Output.DICT)
        except Exception:
            continue
        for i, t in enumerate(d["text"]):
            t = (t or "").strip()
            if not t:
                continue
            out.append({"text": t, "conf": float(d["conf"][i]), "x0": d["left"][i] / scale, "y0": d["top"][i] / scale,
                        "x1": (d["left"][i] + d["width"][i]) / scale, "y1": (d["top"][i] + d["height"][i]) / scale,
                        "line": (d["block_num"][i], d["par_num"][i], d["line_num"][i], inv)})
    return out


def _labels(words: list[dict]) -> list[dict]:
    """Distance labels: '100 ft', '50m', "200'" (number + unit in one word or the next on the line)."""
    labs = []
    for i, w in enumerate(words):
        t = w["text"].replace(",", "").strip(".:;")
        m = _DIST.match(t)
        if not m:
            continue
        unit = m.group(2)
        x1 = w["x1"]
        if not unit and i + 1 < len(words):
            nxt = words[i + 1]
            if nxt["line"] == w["line"] and re.fullmatch(r"(ft|feet|foot|m|meters?|metres?|km|mi|miles?|yd|yards?)\.?", nxt["text"], re.I):
                unit = nxt["text"].strip(".")
                x1 = nxt["x1"]
        if not unit:
            continue
        unit = {"'": "ft"}.get(unit, unit)
        try:
            ft = length_ft(f"{float(m.group(1))}{unit.lower()}")
        except ValueError:
            continue
        if ft <= 0:
            continue
        labs.append({**w, "x1": x1, "ft": ft, "label": f"{m.group(1)} {unit}"})
    return labs


def _runs(row: np.ndarray) -> list[tuple[int, int]]:
    d = np.diff(np.r_[0, row.astype(np.int8), 0])
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0]))


def find_scale_bar(img) -> dict | None:
    """A drawn scale bar + its OCR'd label → {p0, p1, ft, label}; None if not found."""
    words = _ocr_words(img)
    labs = _labels(words)
    if not labs:
        return None
    a = np.asarray(img.convert("L"), float) / 255.0
    H, W = a.shape
    best = None
    for lab in labs:
        h = max(6.0, lab["y1"] - lab["y0"])
        ys = (int(max(0, lab["y0"] - 4 * h)), int(min(H, lab["y1"] + 4 * h)))
        xs = (int(max(0, lab["x0"] - 40 * h)), int(min(W, lab["x1"] + 40 * h)))
        win = a[ys[0]:ys[1], xs[0]:xs[1]]
        for mask in (win > 0.82, win < 0.2):
            for r in range(mask.shape[0]):
                for c0, c1 in _runs(mask[r]):
                    L = c1 - c0
                    if L < 2.0 * h or L > 0.6 * W:
                        continue
                    # a bar is thin: rows a few px above/below are mostly clear over its length
                    cl = []
                    for dy in (-6, -5, 5, 6):
                        rr = r + dy
                        if 0 <= rr < mask.shape[0]:
                            cl.append(mask[rr, c0:c1].mean())
                    if not cl or max(cl) > 0.5:
                        continue
                    y = ys[0] + r
                    bx0, bx1 = xs[0] + c0, xs[0] + c1
                    lx, ly = (lab["x0"] + lab["x1"]) / 2, (lab["y0"] + lab["y1"]) / 2
                    dy_ = abs(ly - y)
                    # label beside an end, or above/below the bar
                    near_end = min(abs(lab["x0"] - bx1), abs(lab["x1"] - bx0)) < 3 * h and dy_ < 2.5 * h
                    over = bx0 - 2 * h <= lx <= bx1 + 2 * h and dy_ < 3.5 * h
                    if not (near_end or over):
                        continue
                    score = L - 2 * dy_
                    if best is None or score > best[0]:
                        best = (score, {"p0": (float(bx0), float(y + 0.5)), "p1": (float(bx1), float(y + 0.5)),
                                        "ft": lab["ft"], "label": lab["label"], "px": float(L)})
    if best is None:
        return None
    # several labels on the bar ("0 50 100 ft"): the bar spans the largest
    res = best[1]
    same = [lb for lb in labs if abs((lb["y0"] + lb["y1"]) / 2 - (labs[0]["y0"] + labs[0]["y1"]) / 2) < 3]
    if len(same) > 1:
        res["ft"] = max(res["ft"], max(lb["ft"] for lb in same))
    return res


def image_mentions_google(img, name: str = "") -> bool:
    if re.search(r"google|earth", name, re.I):
        return True
    try:
        return any(re.search(r"google", w["text"], re.I) for w in _ocr_words(img, scale=1))
    except Exception:
        return False


# ----------------------------------------------------------------------------- site-from-image
def _calib(spec: str) -> tuple[tuple[float, float], tuple[float, float], float]:
    m = re.fullmatch(r"\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*=\s*(.+)", spec)
    if not m:
        raise ValueError("--calibrate is x0,y0,x1,y1=DIST (image pixels, e.g. 120,880,620,880=250ft)")
    p0, p1 = (float(m.group(1)), float(m.group(2))), (float(m.group(3)), float(m.group(4)))
    d = length_ft(m.group(5))
    if d <= 0 or p0 == p1:
        raise ValueError("calibration needs two different points and a positive distance")
    return p0, p1, d


def _gcp(spec: str) -> tuple[tuple[float, float], tuple[float, float]]:
    m = re.fullmatch(r"\s*([-\d.]+)\s*,\s*([-\d.]+)\s*=\s*([-\d.]+)\s*,\s*([-\d.]+)\s*", spec)
    if not m:
        raise ValueError("--georef is 'px,py=lat,lon' (repeat it: at least two points)")
    return (float(m.group(1)), float(m.group(2))), (float(m.group(3)), float(m.group(4)))


def local_georef(width: int, height: int, ft_per_px: float, north_deg: float, method: str) -> Georef:
    """LOCAL grid: image centre = (0, 0); +y = true north (north_deg clockwise from image-up)."""
    t = math.radians(north_deg)
    c, s = math.cos(t), math.sin(t)
    # world = R_ccw(t) · (col·k, −row·k) + shift
    a, b = c * ft_per_px, s * ft_per_px
    d, e = s * ft_per_px, -c * ft_per_px
    cx, cy = width / 2, height / 2
    tx = -(a * cx + b * cy)
    ty = -(d * cx + e * cy)
    return Georef(LOCAL_CRS, (a, b, tx, d, e, ty), width, height, origin=(0.0, 0.0), north_deg=north_deg, method=method)


def site_from_image(image: str | Path, *, calibrate: str | None = None, north_deg: float = 0.0,
                    georef: Sequence[str] = (), out: str | Path | None = None, quiet: bool = False) -> SitePackage:
    """Any image (screenshot, drone photo, scan) → site package, scaled by control points, a known distance,
    or the image's own scale bar."""
    from PIL import Image

    image = Path(image)
    img = Image.open(image)
    img.load()
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    img = img.convert("RGB")
    W, H = img.size
    if not quiet:
        print(GOOGLE_NOTICE)
    google = image_mentions_google(img, image.name)
    info: dict[str, Any] = {}
    if georef:
        pts = [_gcp(s) for s in georef]
        if len(pts) < 2:
            raise ValueError("--georef needs at least two control points")
        lat = sum(p[1][0] for p in pts) / len(pts)
        lon = sum(p[1][1] for p in pts) / len(pts)
        crs = choose_crs(lon, lat)
        dst = many_to_crs(crs, [(p[1][1], p[1][0]) for p in pts])
        aff, rms = similarity_fit([p[0] for p in pts], dst)
        g = Georef(crs, aff, W, H, method=f"{len(pts)} control points (similarity fit)", residual_ft=rms if len(pts) > 2 else None)
        info = {"control_points": [{"px": list(p[0]), "latlon": list(p[1])} for p in pts], "center_lonlat": [lon, lat],
                "convergence_deg": convergence_deg(crs, lon, lat)}
        if not quiet:
            print(f"  georeferenced in {crs.name}: {g.ft_per_px:.3f} ft/px, image rotated {g.rotation_deg:.1f}° from grid"
                  + (f", RMS residual {rms:.2f} ft" if len(pts) > 2 else ""))
    else:
        if calibrate:
            p0, p1, d = _calib(calibrate)
            method = f"calibrated: {d:g} ft between ({p0[0]:g},{p0[1]:g}) and ({p1[0]:g},{p1[1]:g})"
        else:
            sb = find_scale_bar(img)
            if sb is None:
                raise ValueError("no scale found — no readable scale bar in the image. Give a known distance: "
                                 "--calibrate x0,y0,x1,y1=250ft (pixel coordinates of two points), or "
                                 "--georef 'px,py=lat,lon' twice")
            p0, p1, d = sb["p0"], sb["p1"], sb["ft"]
            method = f"scale bar '{sb['label']}' ({sb['px']:.0f} px)"
            info["scale_bar"] = sb
            if not quiet:
                print(f"  scale bar found: {sb['label']} = {sb['px']:.0f} px at y={p0[1]:.0f}")
        k = d / math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        g = local_georef(W, H, k, north_deg, method)
        if not quiet:
            print(f"  {k:.3f} ft/px ({method}); north {north_deg:g}° clockwise from image-up; local grid, origin = image centre")
    root = Path(out) if out else new_package_dir(image.with_name(f"{image.stem}-site"))
    pkg = SitePackage.create(root, source="site-from-image", input=str(image), **info)
    img.save(pkg.root / "imagery.png", optimize=True)
    pkg.manifest["files"] = {"imagery": "imagery.png", "georef": "georef.json", "world_file": "imagery.pgw"}
    pkg.manifest["imagery_source"] = f"user-supplied image ({image.name})"
    pkg.manifest["restricted_imagery"] = bool(google)
    pkg.save_georef(g)
    pkg.add_attribution("Base image: user-supplied" + (" (Google imagery — reference only, not for tracing or "
                                                       "redistribution)" if google else ""))
    pkg.log("site-from-image", method=g.method, ft_per_px=g.ft_per_px)
    pkg.save()
    return pkg
