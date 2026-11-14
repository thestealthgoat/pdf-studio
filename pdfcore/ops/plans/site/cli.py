"""CLI: ``pdfcore plans site-fetch | site-from-image | site-extract | site-plan | site-add``."""

from __future__ import annotations

import argparse
from pathlib import Path

from pdfcore.ops.plans import sub
from pdfcore.result import OpResult


def _pct(v: str | None) -> float | None:
    if v is None:
        return None
    t = str(v).strip().lower()
    if t in ("", "0", "0%", "none", "off", "no"):
        return None
    x = float(t.rstrip("%"))
    return x / 100.0 if x > 1 else x


def _floats(v: str | None, n: int) -> list[float] | None:
    if not v:
        return None
    xs = [float(t) for t in v.replace(";", ",").split(",") if t.strip()]
    if len(xs) != n:
        raise SystemExit(f"expected {n} numbers, got {v!r}")
    return xs


def _sheet_args(p: argparse.ArgumentParser, sheet: str, title: str) -> None:
    p.add_argument("--scale", default="auto", help="engineering scale, e.g. 1\"=40' (default auto: largest that fits)")
    p.add_argument("--size", default="ansi-d", help="sheet size: ansi-b … ansi-e, arch-b … arch-e (default ansi-d)")
    p.add_argument("--brand", default="default", help="title-block brand (private brands live in ~/.config/pdf-studio/brands)")
    p.add_argument("--underlay", default="25%", help="faded imagery under the linework, e.g. 25%% (0/none = off)")
    p.add_argument("--underlay-color", action="store_true", help="keep the underlay in colour (default greyscale)")
    p.add_argument("--sheet", default=sheet)
    p.add_argument("--title", default=title)
    p.add_argument("--project")
    p.add_argument("--client", default="")
    p.add_argument("--drawn", default="")
    p.add_argument("--checked", default="")
    p.add_argument("--date")
    p.add_argument("--rev", default="0")
    p.add_argument("--extent", help="crop: x0,y0,x1,y1 in feet from the site origin (image centre)")
    p.add_argument("--center", help="centre the view at x,y feet from the site origin")
    p.add_argument("-o", "--out", help="output PDF (default: <package>/<sheet>-site-plan.pdf, never overwriting)")
    p.add_argument("--png-dir", help="save a rendered PNG of the sheet here (visual check)")


@sub("site-fetch", "Site package from public data: USGS/NAIP orthoimagery + OpenStreetMap, in State Plane feet")
def _cli_fetch(p: argparse.ArgumentParser):
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--address", help="street address (US Census geocoder, Nominatim fallback)")
    g.add_argument("--center", help="lat,lon")
    g.add_argument("--bbox", help="w,s,e,n (lon/lat)")
    p.add_argument("--size", default="600ft", help="area around --address/--center: 600ft, 800x500ft, 200m")
    p.add_argument("--res", default="0.3m", help="pixel size (default 0.3m ≈ 1 ft; NAIP is ~0.6 m native)")
    p.add_argument("--imagery", default="auto", choices=["auto", "naip", "usgs"], help="auto = NAIP, else USGS basemap")
    p.add_argument("--osm", dest="osm", action="store_true", default=True, help="also fetch OpenStreetMap vectors (default)")
    p.add_argument("--no-osm", dest="osm", action="store_false")
    p.add_argument("-o", "--out", help="package folder (default ./site-<address or lat_lon>)")

    def run(ns):
        from pdfcore.ops.plans.site.ingest import site_fetch

        pkg = site_fetch(address=ns.address, center=ns.center, size=ns.size, bbox=ns.bbox, res=ns.res, osm=ns.osm,
                         imagery=ns.imagery, out=ns.out)
        print(f"{pkg.root}  site package ({pkg.manifest['size_ft'][0]:.0f} × {pkg.manifest['size_ft'][1]:.0f} ft, "
              f"{pkg.manifest['crs']['name']})")
        return OpResult(None, data={"package": str(pkg.root)})

    return run


@sub("site-from-image", "Site package from any image (screenshot, drone photo, scan): calibrate, georeference or read its scale bar")
def _cli_from_image(p: argparse.ArgumentParser):
    p.add_argument("image")
    p.add_argument("--calibrate", help="x0,y0,x1,y1=250ft — two image pixels and the real distance between them")
    p.add_argument("--georef", action="append", default=[], help="'px,py=lat,lon' control point (repeat; ≥ 2)")
    p.add_argument("--north-up", action="store_true", help="north is image-up (default)")
    p.add_argument("--north-deg", type=float, default=0.0, help="north is this many degrees clockwise from image-up")
    p.add_argument("-o", "--out", help="package folder (default <image>-site next to the image)")

    def run(ns):
        from pdfcore.ops.plans.site.ingest import site_from_image

        pkg = site_from_image(ns.image, calibrate=ns.calibrate, north_deg=0.0 if ns.north_up else ns.north_deg,
                              georef=ns.georef, out=ns.out)
        print(f"{pkg.root}  site package ({pkg.manifest['ft_per_px']:.3f} ft/px)")
        return OpResult(None, data={"package": str(pkg.root)})

    return run


@sub("site-extract", "Classify + vectorize site imagery: buildings, roads, pavement, trees, water (classic CV / OSM / both)")
def _cli_extract(p: argparse.ArgumentParser):
    p.add_argument("package")
    p.add_argument("--method", default="both", choices=["classic", "osm", "both", "sam"],
                   help="both (default): OSM where mapped, imagery fills gaps")
    p.add_argument("--no-preview", action="store_true", help="skip extract-preview.png (class overlay)")

    def run(ns):
        from pdfcore.ops.plans.site.extract import site_extract

        r = site_extract(ns.package, method=ns.method, preview=not ns.no_preview)
        print(f"{r['path']}  {r['features']} features")
        return OpResult(None, data=r)

    return run


@sub("site-plan", "Scaled site plan: layered DXF + PDF on the title block (north arrow, scale bar, legend, /Measure)")
def _cli_plan(p: argparse.ArgumentParser):
    p.add_argument("package")
    _sheet_args(p, "C-101", "Site Plan")
    p.add_argument("--features", help="GeoJSON in the package to draw (default extracted.geojson, else osm.geojson)")

    def run(ns):
        from pdfcore.ops.plans.site.plan import render

        res = render(ns.package, scale=ns.scale, size=ns.size, brand=ns.brand, underlay=_pct(ns.underlay),
                     underlay_color=ns.underlay_color, sheet=ns.sheet, title=ns.title, project=ns.project,
                     client=ns.client, drawn=ns.drawn, checked=ns.checked, date=ns.date, rev=ns.rev,
                     extent=_floats(ns.extent, 4), center=_floats(ns.center, 2), features=ns.features, out=ns.out,
                     png_dir=ns.png_dir)
        print(f"{res.extra_outputs[0]}  DXF ({res.data['scale']})")
        return res

    return run


@sub("site-add", "Add proposed work from a job YAML (pads, duct banks, conduit, trenches, poles…) → job plan PDF + DXF + takeoff XLSX")
def _cli_add(p: argparse.ArgumentParser):
    p.add_argument("package")
    p.add_argument("job", help="job YAML (example: pdfcore/ops/plans/examples/site-job.yaml)")
    p.add_argument("--scale")
    p.add_argument("--size")
    p.add_argument("--brand")
    p.add_argument("--underlay")
    p.add_argument("-o", "--out", help="output PDF (default <package>/<sheet>-<job name>.pdf)")
    p.add_argument("--png-dir", help="save a rendered PNG of the sheet here (visual check)")

    def run(ns):
        from pdfcore.ops.plans.site.job import site_add

        res = site_add(ns.package, ns.job, scale=ns.scale, size=ns.size, brand=ns.brand,
                       underlay=ns.underlay, out=ns.out, png_dir=ns.png_dir)
        print(f"{res.extra_outputs[0]}  DXF ({res.data['scale']})")
        return res

    return run
