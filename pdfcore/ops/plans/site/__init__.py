"""Site plans from imagery. CLI: ``pdfcore plans site-fetch | site-from-image | site-extract | site-plan | site-add``.

Modules
-------
geo        CRS choice (State Plane ft via pyproj, else UTM in US ft), georeferencing, the package folder format
net        polite HTTP: User-Agent, timeouts, retries with backoff, on-disk cache (~/.cache/pdf-studio/site)
sources    US Census / Nominatim geocoding, USGS National Map imagery (NAIP + basemap), OpenStreetMap (Overpass)
ingest     site-fetch and site-from-image (calibration, control points, scale-bar OCR)
extract    classical CV classification + vectorization, OSM merge (site-extract)
geom       numpy geometry: simplify, orthogonalize, clip, rasterize, mask → polygons
plan       site-plan sheet (ReportLab drawing on the drawing-sheet title block) + layered DXF
job        site-add: proposed infrastructure from YAML, quantities → takeoff XLSX
synthetic  synthetic aerial image with known geometry (tests)

Data rules: imagery from USGS The National Map (public domain) only; OpenStreetMap is ODbL — every drawing
carries "© OpenStreetMap contributors". Google/Bing/Apple imagery is never fetched; a user's screenshot is
accepted as input with a notice that Google's terms restrict tracing it.

Heavy dependencies (pyproj, scikit-image) are imported lazily, so ``pdfcore plans`` works without the
``site`` extra installed.
"""

from pdfcore.ops.plans.site import cli  # noqa: F401  (registers the subcommands)
