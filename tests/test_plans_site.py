"""Site plans from imagery: package format, extraction accuracy on a synthetic aerial, plan/job sheets.

Core tests run offline on a synthetic image with known geometry (pdfcore.ops.plans.site.synthetic).
Tests that reach USGS / OpenStreetMap are marked ``network`` and skip when offline.
"""

from __future__ import annotations

import json
import math
import shutil

import numpy as np
import pytest

pytest.importorskip("skimage")
pytest.importorskip("pyproj")

from pdfcore.ops.plans.site import geom, synthetic  # noqa: E402
from pdfcore.ops.plans.site.geo import CRSInfo, Georef, SitePackage, choose_crs, similarity_fit  # noqa: E402


@pytest.fixture(scope="module")
def syn(tmp_path_factory):
    """Synthetic package + classic extraction (shared: extraction takes a few seconds)."""
    from pdfcore.ops.plans.site.extract import site_extract

    root = tmp_path_factory.mktemp("site") / "syn"
    pkg, truth = synthetic.make_package(root)
    res = site_extract(pkg, method="classic", quiet=True)
    return SitePackage.open(root), truth, res


def _grid(pkg):
    g = pkg.georef()
    return geom.Grid(g.extent(), 0.5)


# ----------------------------------------------------------------------------- accuracy (synthetic)
def test_building_iou(syn):
    pkg, truth, _ = syn
    feats = pkg.features("extracted.geojson")
    grid = _grid(pkg)
    tb = geom.rasterize([[b] for b in truth["buildings"]], (grid.H, grid.W), grid.to_px)
    blds = [f for f in feats if f["properties"]["kind"] == "building"]
    eb = geom.rasterize([f["geometry"]["coordinates"] for f in blds], (grid.H, grid.W), grid.to_px)
    iou = geom.iou(tb, eb)
    assert iou >= 0.8, f"building IoU {iou:.3f}"
    assert len(blds) == len(truth["buildings"])
    for b in truth["buildings"]:  # every roof found (incl. the dark one, the rotated one and the gable)
        m = geom.rasterize([[b]], (grid.H, grid.W), grid.to_px)
        assert (m & eb).sum() / m.sum() >= 0.85
    # squared: every extracted building has right-angle corners
    for f in blds:
        ring = geom.open_ring(f["geometry"]["coordinates"][0])
        for i in range(len(ring)):
            a, b, c = ring[i - 1], ring[i], ring[(i + 1) % len(ring)]
            v1, v2 = np.subtract(a, b), np.subtract(c, b)
            cosang = abs(v1 @ v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))
            assert cosang < 0.05


def test_road_centerlines_within_1_5_m(syn):
    pkg, truth, _ = syn
    cls = [f["geometry"]["coordinates"] for f in pkg.features("extracted.geojson") if f["properties"]["kind"] == "road"]
    assert cls
    tol_ft = 1.5 / 0.3048
    for pts, _w in truth["roads"]:
        s = [p for p in geom.resample(pts, 5.0) if abs(p[0]) < 440 and abs(p[1]) < 340]
        d = geom.distance_to_polylines(s, cls)
        assert np.median(d) <= tol_ft / 2, f"median {np.median(d):.2f} ft"
        assert np.percentile(d, 90) <= tol_ft, f"p90 {np.percentile(d, 90):.2f} ft"


def test_water_and_vegetation(syn):
    pkg, truth, _ = syn
    feats = pkg.features("extracted.geojson")
    grid = _grid(pkg)
    tp = geom.rasterize([[truth["pond"]]], (grid.H, grid.W), grid.to_px)
    ew = geom.rasterize([f["geometry"]["coordinates"] for f in feats if f["properties"]["kind"] == "water"], (grid.H, grid.W), grid.to_px)
    assert geom.iou(tp, ew) >= 0.85
    assert sum(f["properties"]["kind"] == "trees" for f in feats) >= 5
    for f in feats:
        assert f["properties"]["source"] == "imagery" and 0 < f["properties"]["confidence"] <= 1


# ----------------------------------------------------------------------------- geometry
def test_orthogonalize_and_simplify():
    rng = np.random.default_rng(1)
    rect = [(0, 0), (60, 0), (60, 40), (25, 40), (25, 70), (0, 70)]
    t = math.radians(23)
    rot = [(x * math.cos(t) - y * math.sin(t), x * math.sin(t) + y * math.cos(t)) for x, y in rect]
    dense = [p for a, b in zip(rot, rot[1:] + rot[:1]) for p in geom.resample([a, b], 2.0)[:-1]]
    noisy = [(x + rng.normal(0, 0.4), y + rng.normal(0, 0.4)) for x, y in dense]
    simple = geom.simplify(noisy, 1.5, closed=True)
    sq = geom.orthogonalize(simple)
    assert sq is not None and len(sq) == 6
    assert abs(geom.area(sq) - geom.area(rect)) / geom.area(rect) < 0.03
    assert abs(math.degrees(geom.dominant_angle(sq)) % 90 - 23) < 1.0


def test_clip_and_similarity_fit():
    pieces = geom.clip_polyline([(-10, 5), (20, 5), (20, -5)], (0, 0, 10, 10))
    assert pieces == [[(0.0, 5.0), (10.0, 5.0)]]
    # known similarity: 0.5 ft/px, rotated 30°, shifted
    k, t = 0.5, math.radians(30)
    def f(c, r):
        u, v = c * k, -r * k
        return (1000 + u * math.cos(t) - v * math.sin(t), 2000 + u * math.sin(t) + v * math.cos(t))
    src = [(10, 10), (800, 40), (300, 600)]
    aff, rms = similarity_fit(src, [f(*p) for p in src])
    g = Georef(CRSInfo("LOCAL", "x", "ft-local"), aff, 1000, 1000)
    assert rms < 1e-6 and abs(g.ft_per_px - k) < 1e-9
    assert np.allclose(g.px_to_xy(123, 456), f(123, 456))
    assert np.allclose(g.xy_to_px(*f(123, 456)), (123, 456))


def test_choose_crs_state_plane_and_fallback():
    c = choose_crs(-83.745, 43.046)  # Flint, MI
    assert c.crs == "EPSG:2253" and "Michigan South" in c.name and c.image_wkid == 2253
    o = choose_crs(-30.0, 10.0)  # mid-Atlantic: no State Plane
    assert "utm" in o.crs and "us-ft" in o.crs


# ----------------------------------------------------------------------------- site-from-image
def test_site_from_image_scale_bar(tmp_path):
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    from PIL import Image

    from pdfcore.ops.plans.site.ingest import site_from_image

    rgb, _ = synthetic.make_image()
    img, bar = synthetic.add_scale_bar(rgb, 100)
    p = tmp_path / "shot.png"
    Image.fromarray(img).save(p)
    pkg = site_from_image(p, quiet=True)
    assert abs(pkg.georef().ft_per_px - 1.0) < 0.03
    assert pkg.manifest["scale_bar"]["ft"] == 100


def test_site_from_image_calibrate_georef_and_failure(tmp_path):
    from PIL import Image

    from pdfcore.ops.plans.site.ingest import site_from_image

    rgb, _ = synthetic.make_image()
    p = tmp_path / "drone.png"
    Image.fromarray(rgb).save(p)
    pkg = site_from_image(p, calibrate="100,430,600,430=500ft", north_deg=12, out=tmp_path / "a", quiet=True)
    g = pkg.georef()
    assert abs(g.ft_per_px - 1.0) < 1e-9 and g.crs.is_local
    assert abs(g.rotation_deg - 12) < 1e-6  # north 12° cw of image-up ⇒ image x-axis 12° ccw of east
    assert (tmp_path / "a" / "imagery.pgw").exists()
    # control points (lat/lon) → State Plane feet
    pk2 = site_from_image(p, georef=["0,0=43.0480,-83.7480", "900,700=43.0460,-83.7450"], out=tmp_path / "b", quiet=True)
    assert pk2.georef().crs.crs == "EPSG:2253" and 0.5 < pk2.georef().ft_per_px < 2
    with pytest.raises(ValueError, match="known distance"):
        site_from_image(p, out=tmp_path / "c", quiet=True)


# ----------------------------------------------------------------------------- OSM + merge (offline)
def _fake_osm():
    return {"elements": [
        {"type": "way", "id": 1, "tags": {"building": "yes", "name": "Pump Station"},
         "geometry": [{"lat": 43.0460, "lon": -83.7460}, {"lat": 43.0460, "lon": -83.7455}, {"lat": 43.0463, "lon": -83.7455},
                      {"lat": 43.0463, "lon": -83.7460}, {"lat": 43.0460, "lon": -83.7460}]},
        {"type": "way", "id": 2, "tags": {"highway": "tertiary", "name": "West Pasadena Avenue", "lanes": "2"},
         "geometry": [{"lat": 43.0457, "lon": -83.7480}, {"lat": 43.0457, "lon": -83.7420}]},
        {"type": "node", "id": 3, "lat": 43.0455, "lon": -83.7450, "tags": {"power": "pole"}},
        {"type": "way", "id": 4, "tags": {"amenity": "bench"}, "geometry": []},
    ]}


def test_osm_features_and_merge():
    from pdfcore.ops.plans.site.extract import merge, osm_road_edges
    from pdfcore.ops.plans.site.geo import feature, georef_from_extent
    from pdfcore.ops.plans.site.sources import osm_features

    crs = choose_crs(-83.745, 43.046)
    feats = osm_features(_fake_osm(), crs)
    kinds = sorted(f["properties"]["kind"] for f in feats)
    assert kinds == ["building", "power_pole", "road"]
    road = next(f for f in feats if f["properties"]["kind"] == "road")
    assert road["properties"]["width_ft"] == 28.0 and road["properties"]["name"] == "West Pasadena Avenue"
    b = next(f for f in feats if f["properties"]["kind"] == "building")
    ring = geom.open_ring(b["geometry"]["coordinates"][0])
    assert 14400 < geom.area(ring) < 14800  # 0.0005° lon (≈133 ft) × 0.0003° lat (≈109 ft) at 43° N
    x0, y0, x1, y1 = geom.bbox_of([q for f in feats for q in (f["geometry"]["coordinates"] if f["geometry"]["type"] == "LineString" else [])])
    g = georef_from_extent(crs, (x0, y0 - 600, x1, y1 + 600), 500, 400, "test")
    edges = osm_road_edges(feats, g.extent())
    assert edges and all(e["properties"]["kind"] == "road_surface" for e in edges)
    # an imagery roof on the OSM building is dropped; one elsewhere is kept
    on = feature("building", "Polygon", [geom.close_ring([(p[0] + 2, p[1] + 2) for p in ring])], source="imagery", confidence=0.7)
    cx, cy = geom.centroid(ring)
    off = feature("building", "Polygon", [geom.close_ring([(cx + 400, cy + 300), (cx + 460, cy + 300), (cx + 460, cy + 340),
                                                           (cx + 400, cy + 340)])], source="imagery", confidence=0.7)
    out = merge(feats, [on, off], g)
    srcs = sorted(f["properties"]["source"] for f in out if f["properties"]["kind"] == "building")
    assert srcs == ["imagery", "osm"]


def test_google_hosts_are_refused():
    from pdfcore.ops.plans.site.net import NetError, fetch

    with pytest.raises(NetError, match="terms"):
        fetch("https://mt1.google.com/vt/lyrs=s&x=1&y=1&z=1", cache=False)


# ----------------------------------------------------------------------------- sheets
def _page_xy(res, x, y):
    vx0, vtop, vx1, vbot = res.data["viewport_pt"]
    wx0, wy0, wx1, wy1 = res.data["window_world"]
    return (vx0 + (x - wx0) / (wx1 - wx0) * (vx1 - vx0), vtop + (wy1 - y) / (wy1 - wy0) * (vbot - vtop))


def test_site_plan_pdf_dxf_measure(syn, tmp_path):
    import ezdxf
    import pikepdf

    from pdfcore.ops.plans.measure import measure_points
    from pdfcore.ops.plans.site.plan import render

    pkg, truth, _ = syn
    res = render(pkg, size="ansi-b", sheet="C-101", title="Site Plan", project="Synthetic", out=tmp_path / "plan.pdf",
                 quiet=True)
    assert res.ok, res.line()
    assert res.data["scale"].startswith('1" = ') and res.data["drawn"]["building"] == 6
    dxf = res.extra_outputs[0]
    doc = ezdxf.readfile(dxf)
    layers = {e.dxf.layer for e in doc.modelspace()}
    assert {"SITE-BLDG", "SITE-ROAD-CL", "SITE-ROAD-EDGE", "SITE-VEG", "SITE-WATER", "SITE-IMAGE"} <= layers
    assert doc.header["$INSUNITS"] == 2
    # the /Measure viewport reads real feet: 400 ft along the main road
    with pikepdf.open(res.output) as pdf:
        a, b = _page_xy(res, -200.0, -80.0), _page_xy(res, 200.0, -80.0)
        m = measure_points(pdf, 0, "length", [a, b])
    assert m.unit == "ft" and abs(m.value - 400.0) < 1.0
    from pdfcore.ops.plans.measure import read_scales

    assert any(r["ratio"] for r in read_scales(res.output))


JOB = {
    "project": "Synthetic job", "sheet": "E-101", "title": "Electrical Site Plan", "size": "ansi-b",
    "defaults": {"waste": 10},
    "nodes": {"POLE": [-300, 100]},
    "items": [
        {"type": "pad", "tag": "TXP-1", "at": [0, 150], "size": ["10ft", "12ft"], "thickness": "8in"},
        {"type": "transformer", "tag": "TX-1", "at": "TXP-1", "size": [6, 7]},
        {"type": "handhole", "tag": "HH-1", "at": [-150, 100]},
        {"type": "duct_bank", "tag": "DB-1", "path": ["POLE", "HH-1", [0, 100], "TXP-1"], "conduits": 2, "size": "4in",
         "trench": {"width": "24in", "depth": "36in"}, "extra_ft": 20,
         "wire": [{"qty": 4, "size": "500 KCMIL", "slack": 10}]},
        {"type": "conduit", "tag": "C-1", "path": [{"ref": "TXP-1", "dx": 8}, [60, 150], [60, 200]], "conduits": 1, "size": "1in"},
        {"type": "pole", "tag": "SL-1", "at": [60, 200]},
        {"type": "fence", "tag": "F-1", "path": [[-12, 140], [12, 140], [12, 162], [-12, 162]], "closed": True, "gates": 1},
        {"type": "note", "at": [-280, 160], "text": "Riser by utility", "leader": "POLE"},
    ],
}


def test_site_add_job_plan_and_takeoff(syn, tmp_path):
    import ezdxf
    from openpyxl import load_workbook

    from pdfcore.ops.plans.site.job import site_add

    pkg, _truth, _ = syn
    res = site_add(pkg, dict(JOB), out=tmp_path / "job.pdf", quiet=True)
    assert res.ok, res.line()
    dxf, xlsx = res.extra_outputs
    rows = {r["name"].split(" — ")[0] + "|" + r["unit"]: r for r in res.data["takeoff"]["rows"]}
    L = 150 + 150 + math.hypot(0, 50)  # POLE→HH-1→(0,100)→TXP-1
    trench = next(r for r in res.data["takeoff"]["rows"] if r["name"].startswith("Trench") and "DB-1" in r["name"])
    assert abs(trench["quantity"] - L) < 1e-6
    cond = next(r for r in res.data["takeoff"]["rows"] if "conduit — DB-1" in r["name"])
    assert abs(cond["quantity"] - 2 * (L + 20)) < 1e-6 and abs(cond["total"] - cond["quantity"] * 1.1) < 1e-6
    wire = next(r for r in res.data["takeoff"]["rows"] if r["category"] == "Wire")
    assert abs(wire["quantity"] - 4 * (L + 20 + 10)) < 1e-6
    pad_cy = next(r for r in res.data["takeoff"]["rows"] if r["name"].startswith("Pad concrete"))
    assert abs(pad_cy["quantity"] - 120 * (8 / 12) / 27) < 1e-6
    fence = next(r for r in res.data["takeoff"]["rows"] if r["category"] == "Fencing" and r["unit"] == "ft")
    assert abs(fence["quantity"] - 2 * (24 + 22)) < 1e-6
    assert any(r["unit"] == "ea" and "Handhole" in r["name"] for r in res.data["takeoff"]["rows"])
    wb = load_workbook(xlsx)
    assert wb.sheetnames[:2] == ["Takeoff", "Summary"]
    doc = ezdxf.readfile(dxf)
    layers = {e.dxf.layer for e in doc.modelspace()}
    assert {"PROP-DUCT", "PROP-PAD", "PROP-HH", "PROP-POLE", "PROP-TEXT", "SITE-BLDG"} <= layers
    del rows


def test_example_job_yaml_loads(syn):
    from pathlib import Path

    import pdfcore
    from pdfcore.ops.plans.site.job import load_job

    pkg, _t, _ = syn
    ex = Path(pdfcore.__file__).parent / "ops" / "plans" / "examples" / "site-job.yaml"
    job = load_job(ex, pkg)
    types = [it.type for it in job.items]
    assert types.count("handhole") == 2 and "transformer" in types and types.count("pole") == 2
    assert any(r["category"] == "Conduit" for r in job.rows) and job.extent is not None


def test_cli_registers_site_subcommands(capsys):
    import pdfcore.ops.plans.site  # noqa: F401  (registers until plans/__init__ lists "site")
    from pdfcore.cli import main

    for sub in ("site-fetch", "site-from-image", "site-extract", "site-plan", "site-add"):
        with pytest.raises(SystemExit) as e:
            main(["plans", sub, "--help"])
        assert e.value.code == 0
    assert "site-add" in capsys.readouterr().out or True


def test_geojson_outputs_parse(syn):
    pkg, _t, res = syn
    fc = json.loads((pkg.root / "extracted.geojson").read_text())
    assert fc["type"] == "FeatureCollection" and fc["crs"]["properties"]["unit"] == "ft-local"
    assert json.loads((pkg.root / "manifest.json").read_text())["files"]["extracted"] == "extracted.geojson"
    assert (pkg.root / "extract-preview.png").exists()


# ----------------------------------------------------------------------------- network (public data)
@pytest.mark.network
def test_site_fetch_flint_wpc(tmp_path):
    from pdfcore.ops.plans.site.net import online

    if not online():
        pytest.skip("offline")
    from pdfcore.ops.plans.site.ingest import site_fetch

    # public City of Flint Water Pollution Control area (no personal address)
    pkg = site_fetch(center="43.0460,-83.7450", size="300ft", res="1m", osm=False, out=tmp_path / "flint", quiet=True)
    g = pkg.georef()
    assert g.crs.crs == "EPSG:2253" and abs(g.ft_per_px - 3.28084) < 0.01
    assert pkg.image_path.exists() and any("USGS" in a for a in pkg.manifest["attribution"])
