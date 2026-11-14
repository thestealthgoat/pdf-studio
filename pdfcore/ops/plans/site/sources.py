"""Public data sources: US Census geocoder (+ Nominatim fallback), USGS National Map orthoimagery,
OpenStreetMap via Overpass.

Licenses / terms
----------------
* USGS The National Map imagery — public domain (credit "Imagery: USGS").
* OpenStreetMap — ODbL 1.0: every drawing carries "© OpenStreetMap contributors".
* US Census Geocoder — public domain. Nominatim — usage policy: ≤ 1 request/s, identifying User-Agent.
"""

from __future__ import annotations

import io
import json
import math
import time
import urllib.parse
from typing import Any, Sequence

from pdfcore.ops.plans.site.geo import CRSInfo, Georef, feature, georef_from_extent, many_to_crs
from pdfcore.ops.plans.site.net import NetError, fetch, fetch_json

USGS_EXPORT = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/export"
NAIP_EXPORT = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer/exportImage"
IMAGERY = {  # name: (endpoint, credit) — both public domain, both USGS The National Map
    "naip": (NAIP_EXPORT, "Imagery: USDA NAIP via USGS The National Map (public domain)"),
    "usgs": (USGS_EXPORT, "Imagery: USGS The National Map (public domain)"),
}
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
MAX_PX = 4000  # per export request (server limit ≈ 4096)


# ----------------------------------------------------------------------------- geocoding
def geocode(address: str, *, quiet: bool = False) -> dict:
    """Address → {lat, lon, matched, source}. Census first (US, public domain), then Nominatim."""
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    try:
        d = fetch_json(f"{CENSUS}?{q}", timeout=30, retries=2)
        m = (d.get("result") or {}).get("addressMatches") or []
        if m:
            c = m[0]["coordinates"]
            return {"lat": float(c["y"]), "lon": float(c["x"]), "matched": m[0].get("matchedAddress", address),
                    "source": "US Census Geocoder"}
    except NetError as exc:
        if not quiet:
            print(f"  Census geocoder: {exc} — trying Nominatim")
    time.sleep(1.0)  # Nominatim policy: at most 1 request per second
    q = urllib.parse.urlencode({"q": address, "format": "json", "limit": 1})
    d = fetch_json(f"{NOMINATIM}?{q}", timeout=30, retries=2)
    if isinstance(d, list) and d:
        return {"lat": float(d[0]["lat"]), "lon": float(d[0]["lon"]), "matched": d[0].get("display_name", address),
                "source": "Nominatim (© OpenStreetMap contributors)"}
    raise ValueError(f"address not found: {address!r} — try --center lat,lon")


# ----------------------------------------------------------------------------- imagery
def usgs_imagery(crs: CRSInfo, extent_ft: Sequence[float], ft_per_px: float, *, service: str = "naip",
                 quiet: bool = False):
    """North-up orthoimage of *extent_ft* (CRS ft) at *ft_per_px* → (PIL RGB image, Georef).

    *service*: ``naip`` (USDA NAIP ≈0.6 m, the sharpest public-domain layer in most of the US) or ``usgs``
    (USGSImageryOnly basemap). Requested in the site CRS itself (ArcGIS reprojects), tiled when larger than
    the per-request limit.
    """
    endpoint = IMAGERY[service][0]
    from PIL import Image

    if crs.image_wkid is None:
        raise ValueError("no ArcGIS spatial reference for this CRS")
    x0, y0, x1, y1 = extent_ft
    W = max(1, int(round((x1 - x0) / ft_per_px)))
    H = max(1, int(round((y1 - y0) / ft_per_px)))
    x1, y0 = x0 + W * ft_per_px, y1 - H * ft_per_px  # exact pixel grid, anchored top-left
    k = 1.0 / crs.image_to_ft  # our ft → image-SR units
    mosaic = Image.new("RGB", (W, H), (255, 255, 255))
    nx, ny = math.ceil(W / MAX_PX), math.ceil(H / MAX_PX)
    for j in range(ny):
        for i in range(nx):
            c0, r0 = i * MAX_PX, j * MAX_PX
            w, h = min(MAX_PX, W - c0), min(MAX_PX, H - r0)
            tx0, ty1 = x0 + c0 * ft_per_px, y1 - r0 * ft_per_px
            tx1, ty0 = tx0 + w * ft_per_px, ty1 - h * ft_per_px
            bbox = ",".join(f"{v * k:.4f}" for v in (tx0, ty0, tx1, ty1))
            q = urllib.parse.urlencode({"bbox": bbox, "bboxSR": crs.image_wkid, "imageSR": crs.image_wkid,
                                        "size": f"{w},{h}", "format": "png", "transparent": "false", "f": "json"})
            meta = fetch_json(f"{endpoint}?{q}", timeout=90, retries=3, cache=False, quiet=quiet)
            if "href" not in meta:
                raise NetError(f"USGS export: {json.dumps(meta)[:200]}")
            img = Image.open(io.BytesIO(fetch(meta["href"], timeout=120, retries=3, cache=False))).convert("RGB")
            if img.size != (w, h):
                img = img.resize((w, h), Image.LANCZOS)
            mosaic.paste(img, (c0, r0))
            if not quiet and nx * ny > 1:
                print(f"  imagery tile {j * nx + i + 1}/{nx * ny}")
    g = georef_from_extent(crs, (x0, y0, x1, y1), W, H, method=f"{service} export (north-up, site CRS)")
    return mosaic, g


# ----------------------------------------------------------------------------- OpenStreetMap
def overpass_query(s: float, w: float, n: float, e: float, timeout: int = 180) -> str:
    b = f"({s:.7f},{w:.7f},{n:.7f},{e:.7f})"
    parts = [
        f'way["building"]{b}', f'relation["building"]{b}', f'way["highway"]{b}', f'way["amenity"="parking"]{b}',
        f'relation["amenity"="parking"]{b}', f'way["natural"~"^(water|wood|scrub|wetland|tree_row)$"]{b}',
        f'relation["natural"="water"]{b}', f'way["landuse"~"^(grass|forest|meadow|reservoir|basin)$"]{b}',
        f'way["leisure"="park"]{b}', f'way["waterway"]{b}', f'way["barrier"~"^(fence|wall)$"]{b}',
        f'way["man_made"~"^(wastewater_plant|storage_tank|water_tower|silo|works)$"]{b}',
        f'relation["man_made"="wastewater_plant"]{b}', f'way["power"~"^(line|minor_line)$"]{b}',
        f'node["power"~"^(pole|tower|transformer)$"]{b}', f'node["natural"="tree"]{b}',
    ]
    return f"[out:json][timeout:{timeout}];(" + ";".join(parts) + ";);out geom;"


def fetch_osm(bbox_lonlat: Sequence[float], *, quiet: bool = False, timeout: int = 180) -> dict:
    """Overpass JSON for (w, s, e, n). Tries each endpoint with backoff; results cached on disk."""
    w, s, e, n = bbox_lonlat
    q = overpass_query(s, w, n, e, timeout)
    data = urllib.parse.urlencode({"data": q}).encode()
    errors = []
    for ep in OVERPASS:
        try:
            if not quiet:
                print(f"  OpenStreetMap: querying {urllib.parse.urlparse(ep).netloc} (can take minutes)…")
            body = fetch(ep, data=data, timeout=timeout + 60, retries=2, backoff=10, quiet=quiet, accept="application/json")
            d = json.loads(body)
            if "elements" not in d:
                raise NetError(f"no elements: {str(d)[:120]}")
            if d.get("remark") and "runtime error" in str(d["remark"]).lower():
                raise NetError(str(d["remark"])[:160])
            return d
        except (NetError, json.JSONDecodeError) as exc:
            from pdfcore.ops.plans.site.net import _key, cache_dir

            (cache_dir() / _key(ep, data)).unlink(missing_ok=True)
            errors.append(f"{urllib.parse.urlparse(ep).netloc}: {exc}")
    raise NetError("OpenStreetMap unavailable — " + "; ".join(errors))


ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service",
                "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link", "living_street", "road",
                "track"}
PATH_CLASSES = {"footway", "path", "cycleway", "pedestrian", "steps", "bridleway", "sidewalk"}
# default paved width (ft) by class when OSM has no width/lanes
ROAD_WIDTH_FT = {"motorway": 48, "trunk": 44, "primary": 40, "secondary": 36, "tertiary": 30, "unclassified": 24,
                 "residential": 28, "service": 20, "living_street": 20, "road": 24, "track": 12, "_link": 20}


def road_width_ft(tags: dict) -> float:
    def num(v):
        try:
            return float(str(v).split(";")[0].replace("m", "").strip())
        except ValueError:
            return None

    if tags.get("width") and num(tags["width"]):
        return num(tags["width"]) / 0.3048
    hw = tags.get("highway", "")
    if tags.get("lanes") and num(tags["lanes"]):
        return max(12.0, num(tags["lanes"]) * 12.0 + (4 if hw in ("primary", "secondary", "tertiary") else 0))
    if hw.endswith("_link"):
        return ROAD_WIDTH_FT["_link"]
    if hw == "service" and tags.get("service") in ("parking_aisle", "driveway", "alley"):
        return 18.0 if tags.get("service") != "parking_aisle" else 22.0
    return float(ROAD_WIDTH_FT.get(hw, 6 if hw in PATH_CLASSES else 20))


def _rings_from_members(members: list[dict], role: str) -> list[list[tuple[float, float]]]:
    segs = [[(p["lon"], p["lat"]) for p in m.get("geometry") or []] for m in members
            if m.get("type") == "way" and m.get("geometry")
            and (m.get("role") == role or (role == "outer" and not m.get("role")))]
    rings: list[list[tuple[float, float]]] = []
    while segs:
        cur = segs.pop(0)
        changed = True
        while cur[0] != cur[-1] and changed:
            changed = False
            for k, sg in enumerate(segs):
                if sg[0] == cur[-1]:
                    cur += sg[1:]
                elif sg[-1] == cur[-1]:
                    cur += sg[::-1][1:]
                elif sg[-1] == cur[0]:
                    cur = sg[:-1] + cur
                elif sg[0] == cur[0]:
                    cur = sg[::-1][:-1] + cur
                else:
                    continue
                segs.pop(k)
                changed = True
                break
        if len(cur) >= 4 and cur[0] == cur[-1]:
            rings.append(cur)
    return rings


def _classify(tags: dict) -> str | None:
    if tags.get("building") and tags.get("building") != "no":
        return "building"
    hw = tags.get("highway")
    if hw:
        if tags.get("area") == "yes":
            return None
        return "road" if hw in ROAD_CLASSES else ("path" if hw in PATH_CLASSES else None)
    if tags.get("amenity") == "parking":
        return "parking"
    mm = tags.get("man_made")
    if mm in ("storage_tank", "water_tower", "silo"):
        return "tank"
    if mm in ("wastewater_plant", "works"):
        return "site"
    nat, lu = tags.get("natural"), tags.get("landuse")
    if nat == "water" or tags.get("water") or lu in ("reservoir", "basin"):
        return "water"
    if tags.get("waterway"):
        return "waterway"
    if nat in ("wood", "scrub", "tree_row") or lu == "forest":
        return "trees"
    if nat == "wetland":
        return "wetland"
    if lu in ("grass", "meadow") or tags.get("leisure") == "park":
        return "grass"
    if tags.get("barrier") in ("fence", "wall"):
        return "fence"
    if tags.get("power") in ("line", "minor_line"):
        return "power_line"
    if tags.get("power") in ("pole", "tower"):
        return "power_pole"
    if tags.get("power") == "transformer":
        return "transformer"
    if nat == "tree":
        return "tree"
    return None


AREA_KINDS = {"building", "parking", "water", "trees", "grass", "wetland", "tank", "site"}


def osm_features(osm: dict, crs: CRSInfo) -> list[dict]:
    """Overpass JSON → GeoJSON features in the site CRS (ft)."""
    out: list[dict] = []
    for el in osm.get("elements", []):
        tags = el.get("tags") or {}
        kind = _classify(tags)
        if kind is None:
            continue
        props = {"source": "osm", "confidence": 0.9, "osm_id": f"{el['type']}/{el['id']}"}
        for k in ("name", "highway", "service", "lanes", "width", "building", "surface", "oneway", "ref", "voltage",
                  "man_made", "amenity", "operator"):
            if tags.get(k):
                props[k] = tags[k]
        if kind in ("road", "path"):
            props["width_ft"] = round(road_width_ft(tags), 1)
        if el["type"] == "node":
            (x, y), = many_to_crs(crs, [(el["lon"], el["lat"])])
            out.append(feature(kind, "Point", [x, y], **props))
            continue
        if el["type"] == "way":
            pts = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if p]
            if len(pts) < 2:
                continue
            xy = [list(p) for p in many_to_crs(crs, pts)]
            closed = pts[0] == pts[-1] and len(pts) >= 4
            if kind in AREA_KINDS and closed:
                out.append(feature(kind, "Polygon", [xy], **props))
            elif kind in AREA_KINDS:
                continue  # open way tagged as an area: skip
            else:
                out.append(feature(kind, "LineString", xy, **props))
        elif el["type"] == "relation" and kind in AREA_KINDS:
            outers = _rings_from_members(el.get("members") or [], "outer")
            inners = _rings_from_members(el.get("members") or [], "inner")
            for ring in outers:
                rings = [[list(p) for p in many_to_crs(crs, ring)]]
                rings += [[list(p) for p in many_to_crs(crs, h)] for h in inners]
                out.append(feature(kind, "Polygon", rings, **props))
    return out
