"""Proposed infrastructure on a site package (``site-add``): pads, equipment, duct banks, conduit, trenches,
handholes, poles, generators, fences and notes from a job YAML, in real-world feet (or lat/lon) — drawn on
the plan (PROP-* layers), written to the DXF, and totalled in a takeoff XLSX (measure.takeoff conventions).

Job YAML (see ``pdfcore/ops/plans/examples/site-job.yaml``)::

    project: …            sheet: E-101        title: Electrical Site Plan
    scale: auto | 1" = 40'   size: ansi-d   brand: default   underlay: 30%
    coords: local          # local: feet east/north of the package origin (image centre) | crs | latlon
    view: {center: [0, 0], extent: [-250, -200, 250, 200]}    # optional (local ft)
    defaults: {waste: 5}
    nodes: {SVC-POLE: [120, -40]}                              # named points
    items:
      - {type: pad, tag: TX-1, at: [10, 20], size: [10ft, 12ft], thickness: 8in}
      - {type: transformer, tag: T-1, at: TX-1, size: [6ft, 6ft], label: 500 KVA PAD-MOUNT XFMR}
      - {type: duct_bank, tag: DB-1, path: [TX-1, [60, 20], MCC], conduits: 2, size: 4in, material: PVC,
         trench: {width: 24in, depth: 36in}, extra_ft: 20}
      - {type: handhole, tag: HH-1, at: [60, 20]}
      - {type: pole, tag: SL-1, at: [30, 60], label: 25' POLE, LED AREA LIGHT}

Points: ``[x, y]``, a node name or item tag, ``{ref: TAG, dx: 5, dy: -3}``, or ``{lat: …, lon: …}``.
Lengths take units (``10ft``, ``10'``, ``30in``, ``3m``); bare numbers are feet.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from pdfcore.ops.plans.site import geom

POINT_TYPES = {"pad", "transformer", "panel", "switchboard", "mcc", "equipment", "generator", "handhole", "pullbox",
               "vault", "pole", "light_pole", "utility_pole", "meter", "note", "disconnect"}
PATH_TYPES = {"duct_bank", "conduit", "trench", "fence"}
ALIASES = {"ductbank": "duct_bank", "db": "duct_bank", "pull_box": "pullbox", "light": "pole", "lightpole": "light_pole",
           "gen": "generator", "xfmr": "transformer", "switchgear": "switchboard"}
DEFAULT_SIZE = {"pad": (10, 10), "transformer": (6, 6), "panel": (4, 2), "switchboard": (8, 3), "mcc": (12, 3),
                "equipment": (4, 3), "generator": (12, 5), "handhole": (2.5, 4), "pullbox": (2, 3), "vault": (6, 10),
                "meter": (1.5, 1.5), "disconnect": (2, 1.5)}
CATEGORY = {"transformer": "Equipment", "panel": "Equipment", "switchboard": "Equipment", "mcc": "Equipment",
            "equipment": "Equipment", "generator": "Equipment", "meter": "Equipment", "disconnect": "Equipment",
            "handhole": "Structures", "pullbox": "Structures", "vault": "Structures", "pole": "Lighting",
            "light_pole": "Lighting", "utility_pole": "Utility poles"}
NAMES = {"pad": "Concrete equipment pad", "transformer": "Transformer", "panel": "Panelboard", "switchboard": "Switchboard",
         "mcc": "Motor control center", "equipment": "Equipment", "generator": "Generator", "handhole": "Handhole",
         "pullbox": "Pull box", "vault": "Vault", "pole": "Light pole", "light_pole": "Light pole",
         "utility_pole": "Utility pole", "meter": "Meter", "disconnect": "Disconnect switch", "duct_bank": "Duct bank",
         "conduit": "Conduit", "trench": "Trench", "fence": "Fence"}


def ft(v: Any, default_unit: str = "ft") -> float:
    from pdfcore.ops.plans.site.ingest import length_ft

    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) if default_unit == "ft" else length_ft(f"{v}{default_unit}")
    return length_ft(str(v), default_unit)


def inch_text(v: Any) -> str:
    """'4in' / 4 / '4"' → '4"' ; '1-1/2in' → '1-1/2"'."""
    t = str(v).strip().lower().replace("in", "").replace('"', "").strip()
    return f'{t}"'


@dataclass
class Item:
    type: str
    tag: str
    raw: dict
    at: tuple[float, float] | None = None
    ring: list[tuple[float, float]] | None = None  # footprint (CRS ft)
    path: list[tuple[float, float]] | None = None
    label: str = ""
    length_ft: float = 0.0


@dataclass
class Job:
    meta: dict
    items: list[Item]
    notes: list[str] = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    extent: tuple[float, float, float, float] | None = None
    rows: list[dict] = field(default_factory=list)


def _rect_ring(cx, cy, w, h, rot_deg):
    t = math.radians(rot_deg)
    ct, st = math.cos(t), math.sin(t)
    out = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        out.append((cx + dx * ct - dy * st, cy + dx * st + dy * ct))
    return out


def load_job(spec: str | Path | dict, pkg) -> Job:
    """Parse a job YAML into items with CRS-feet geometry; computes lengths/areas and takeoff rows."""
    import yaml

    from pdfcore.ops.plans.site.geo import to_crs

    data = spec if isinstance(spec, dict) else yaml.safe_load(Path(spec).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("job YAML needs an 'items:' list")
    g = pkg.georef()
    ox, oy = g.site_origin()
    mode = str(data.get("coords", "local")).lower()
    defaults = data.get("defaults") or {}
    nodes_raw = data.get("nodes") or {}
    resolved: dict[str, tuple[float, float]] = {}

    def conv(p) -> tuple[float, float]:
        if isinstance(p, dict) and "lat" in p:
            return to_crs(g.crs, float(p["lon"]), float(p["lat"]))
        if isinstance(p, dict) and "x" in p:
            p = [p["x"], p["y"]]
        if isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float, str)) for v in p):
            a, b = ft(p[0]), ft(p[1])
            if mode == "latlon":
                return to_crs(g.crs, b, a)
            if mode == "crs":
                return (a, b)
            return (ox + a, oy + b)
        raise ValueError(f"cannot read point {p!r}")

    def point(p, ctx: str):
        if isinstance(p, str):
            key = p.strip()
            if key in resolved:
                return resolved[key]
            raise KeyError(key)
        if isinstance(p, dict) and "ref" in p:
            bx, by = point(p["ref"], ctx)
            return (bx + ft(p.get("dx", 0)), by + ft(p.get("dy", 0)))
        return conv(p)

    for k, v in nodes_raw.items():
        resolved[str(k)] = conv(v)
    raw_items = []
    for n, it in enumerate(data["items"]):
        it = {**{k: v for k, v in defaults.items() if k not in ("waste", "unit_cost")}, **it}
        t = str(it.get("type", "")).lower().replace("-", "_").replace(" ", "_")
        t = ALIASES.get(t, t)
        if t not in POINT_TYPES | PATH_TYPES:
            raise ValueError(f"item {n + 1}: unknown type {it.get('type')!r}; known: {', '.join(sorted(POINT_TYPES | PATH_TYPES))}")
        raw_items.append((t, it, n))
    items: list[Item] = []
    pending = list(raw_items)
    for _round in range(len(pending) + 1):  # resolve references in dependency order
        left = []
        for t, it, n in pending:
            tag = str(it.get("tag") or f"{t.upper()}-{n + 1}")
            try:
                if t in PATH_TYPES:
                    pts = it.get("path")
                    if pts is None and "from" in it and "to" in it:
                        pts = [it["from"], *(it.get("via") or []), it["to"]]
                    if not pts or len(pts) < 2:
                        raise ValueError(f"{tag}: a {t} needs path: [p1, p2, …] (or from/to)")
                    path = [point(p, tag) for p in pts]
                    item = Item(t, tag, it, path=path, length_ft=geom.length(path, closed=bool(it.get("closed"))))
                else:
                    at = point(it.get("at"), tag) if it.get("at") is not None else None
                    if at is None:
                        raise ValueError(f"{tag}: needs at: [x, y]")
                    item = Item(t, tag, it, at=at)
                    if t == "note" and it.get("leader") is not None:
                        it["_leader_xy"] = point(it["leader"], tag)
                    if t in DEFAULT_SIZE or it.get("size"):
                        sz = it.get("size") or DEFAULT_SIZE.get(t, (3, 3))
                        w, h = (ft(sz[0]), ft(sz[1])) if isinstance(sz, (list, tuple)) else (ft(sz), ft(sz))
                        item.ring = _rect_ring(at[0], at[1], w, h, float(it.get("rotate", 0) or 0))
                    resolved.setdefault(tag, at)
            except KeyError:
                left.append((t, it, n))
                continue
            items.append(item)
        if not left:
            break
        if len(left) == len(pending):
            names = ", ".join(sorted({str(x) for _t, it, _n in left for x in _refs(it) if x not in resolved}))
            raise ValueError(f"unknown point name(s): {names} — define them under nodes: or as item tags")
        pending = left
    order = {id(it): n for _t, it, n in raw_items}
    items.sort(key=lambda x: order.get(id(x.raw), 0))
    for it in items:
        it.label = item_label(it)
    job = Job(meta={k: v for k, v in data.items() if k not in ("items", "nodes", "notes", "defaults")}, items=items,
              notes=list(data.get("notes") or []), defaults=defaults)
    view = data.get("view") or {}
    if view.get("extent"):
        x0, y0, x1, y1 = (ft(v) for v in view["extent"])
        job.extent = (ox + x0, oy + y0, ox + x1, oy + y1) if mode == "local" else (x0, y0, x1, y1)
    else:
        pts = [q for it in items for q in (it.path or it.ring or ([it.at] if it.at else []))]
        if pts:
            x0, y0, x1, y1 = geom.bbox_of(pts)
            pad = max(90.0, 0.4 * max(x1 - x0, y1 - y0))
            job.extent = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    job.rows = quantities(job)
    return job


def _refs(it: dict) -> list:
    out = []
    for k in ("at", "from", "to"):
        if isinstance(it.get(k), str):
            out.append(it[k])
    for p in (it.get("path") or []) + (it.get("via") or []):
        if isinstance(p, str):
            out.append(p)
    return out


def item_label(it: Item) -> str:
    r = it.raw
    if r.get("label"):
        return str(r["label"])
    t = it.type
    if t in ("duct_bank", "conduit"):
        n = int(r.get("conduits", r.get("count", 1)) or 1)
        size = inch_text(r.get("size", "4in" if t == "duct_bank" else "2in"))
        mat = str(r.get("material", "PVC")).upper()
        kind = " DUCT BANK" if t == "duct_bank" else (" CONDUIT" if n == 1 else " CONDUITS")
        return f"{n}-{size} {mat}{kind}"
    if t == "trench":
        return f'TRENCH {ft(r.get("width", "18in")) * 12:g}"W x {ft(r.get("depth", "30in")) * 12:g}"D'
    if t == "fence":
        return f"{ft(r.get('height', 6)):g}' {str(r.get('material', 'chain link')).upper()} FENCE"
    if t == "pad":
        w, h = (r.get("size") or DEFAULT_SIZE["pad"])
        return f"CONC. PAD {ft(w):g}' x {ft(h):g}' x {ft(r.get('thickness', '6in')) * 12:g}\""
    return NAMES.get(t, t).upper()


# ----------------------------------------------------------------------------- quantities
def quantities(job: Job) -> list[dict]:
    waste = float(job.defaults.get("waste", 0) or 0)
    costs = job.defaults.get("unit_cost") or {}
    rows: list[dict] = []

    def row(cat, typ, name, qty, unit, it: Item, key: str | None = None):
        uc = it.raw.get("unit_cost")
        if isinstance(uc, dict):
            uc = uc.get(key or unit)
        if uc is None and isinstance(costs, dict):
            uc = costs.get(key or "") if key else None
        rows.append({"type": typ, "name": name, "category": cat, "page": None, "sheet": job.meta.get("sheet", ""),
                     "quantity": float(qty), "unit": unit, "waste": float(it.raw.get("waste", waste) or 0),
                     "unit_cost": uc, "scale": "", "source": f"site-add {it.tag}"})

    counts: dict[tuple[str, str], list[Item]] = {}
    for it in job.items:
        r = it.raw
        t = it.type
        if t in ("duct_bank", "conduit"):
            n = int(r.get("conduits", r.get("count", 1)) or 1)
            L = it.length_ft + ft(r.get("extra_ft", 0))
            size = inch_text(r.get("size", "4in" if t == "duct_bank" else "2in"))
            mat = str(r.get("material", "PVC")).upper()
            tr = r.get("trench") if t == "duct_bank" else r.get("trench")
            if tr:
                tw, td = ft(tr.get("width", "24in")), ft(tr.get("depth", "36in"))
                row("Trenching", "length", f"Trench {tw * 12:g}\"W x {td * 12:g}\"D — {it.tag}", it.length_ft, "ft", it, "trench")
                if tr.get("backfill", True):
                    row("Trenching", "volume", f"Excavation / backfill — {it.tag}", it.length_ft * tw * td / 27.0, "cu yd", it, "excavation")
            row("Conduit", "length", f"{size} {mat} conduit — {it.tag} ({n} × {L:,.0f} ft)", L * n, "ft", it, f"conduit {size}")
            enc = r.get("encasement")
            if enc:
                ew, eh = ft(enc.get("width", "16in")), ft(enc.get("height", "12in"))
                row("Concrete", "volume", f"Duct bank encasement {ew * 12:g}\"x{eh * 12:g}\" — {it.tag}", it.length_ft * ew * eh / 27.0,
                    "cu yd", it, "encasement")
            for wre in r.get("wire") or []:
                q = int(wre.get("qty", 1))
                sets = int(wre.get("sets", 1))
                row("Wire", "length", f"{wre.get('size', '')} {wre.get('type', 'THWN-2')} — {it.tag} ({q * sets} × {L + ft(wre.get('slack', 10)):,.0f} ft)",
                    q * sets * (L + ft(wre.get("slack", 10))), "ft", it, f"wire {wre.get('size', '')}")
        elif t == "trench":
            tw, td = ft(r.get("width", "18in")), ft(r.get("depth", "30in"))
            row("Trenching", "length", f"Trench {tw * 12:g}\"W x {td * 12:g}\"D — {it.tag}", it.length_ft, "ft", it, "trench")
            row("Trenching", "volume", f"Excavation / backfill — {it.tag}", it.length_ft * tw * td / 27.0, "cu yd", it, "excavation")
        elif t == "fence":
            row("Fencing", "length", f"{item_label(it).title()} — {it.tag}", it.length_ft, "ft", it, "fence")
            if r.get("gates"):
                row("Fencing", "count", f"Gate — {it.tag}", int(r["gates"]), "ea", it, "gate")
        elif t == "pad":
            a = geom.area(it.ring)
            th = ft(r.get("thickness", "6in"))
            row("Concrete", "area", f"Equipment pad {it.tag} ({th * 12:g}\" thick)", a, "sq ft", it, "pad sf")
            row("Concrete", "volume", f"Pad concrete {it.tag}", a * th / 27.0, "cu yd", it, "concrete")
        elif t != "note":
            desc = str(r.get("description") or r.get("label") or NAMES.get(t, t))
            counts.setdefault((CATEGORY.get(t, "Equipment"), f"{NAMES.get(t, t)} — {desc}" if desc.lower() != NAMES.get(t, t).lower() else NAMES.get(t, t)), []).append(it)
    for (cat, name), its in counts.items():
        tags = ", ".join(i.tag for i in its)
        row(cat, "count", f"{name} ({tags})", len(its), "ea", its[0], "each")
    order = ["Trenching", "Conduit", "Wire", "Concrete", "Structures", "Equipment", "Lighting", "Utility poles", "Fencing"]
    rows.sort(key=lambda r: order.index(r["category"]) if r["category"] in order else len(order))
    for r in rows:
        r["total"] = r["quantity"] * (1 + r["waste"] / 100.0)
        r["cost"] = r["total"] * float(r["unit_cost"]) if r.get("unit_cost") is not None else None
    return rows


def write_takeoff(job: Job, path: Path, title: str) -> dict:
    from pdfcore.ops.plans.measure import _write_takeoff_xlsx

    return _write_takeoff_xlsx(job.rows, path, title)


# ----------------------------------------------------------------------------- drawing
def _prop_color(pen) -> str:
    return pen.sh.accent


def draw_job(pen, job: Job) -> dict[str, int]:
    from pdfcore.ops.plans.site.plan import _lw, fence_line

    c, v = pen.c, pen.v
    col = _prop_color(pen)
    drawn: dict[str, int] = {}
    fs = pen.sh.fs
    # runs first (under equipment)
    for it in job.items:
        if it.path is None:
            continue
        r = it.raw
        pts = it.path + ([it.path[0]] if r.get("closed") else [])
        if it.type in ("duct_bank", "trench") or (it.type == "conduit" and r.get("trench")):
            tr = r.get("trench") if it.type != "trench" else r
            tw = ft((tr or {}).get("width", "18in")) if tr else 0
            if tw * v.k >= 2.5:
                for side in (-1, 1):
                    off = _offset(pts, side * tw / 2)
                    pen.style(col, _lw(0.18), dash=[4, 2.5])
                    pen.line(off)
        if it.type == "duct_bank":
            pen.style(col, _lw(0.7))
            pen.line(pts)
        elif it.type == "conduit":
            pen.style(col, _lw(0.5), dash=[7, 3])
            pen.line(pts)
        elif it.type == "trench":
            pen.style(col, _lw(0.25), dash=[2, 2])
            pen.line(pts)
        elif it.type == "fence":
            fence_line(pen, pts, col, _lw(0.35))
        drawn[it.type] = drawn.get(it.type, 0) + 1
    # footprints and symbols
    for it in job.items:
        if it.path is not None or it.type == "note":
            continue
        r = it.raw
        X, Y = v.p(*it.at)
        if it.type == "pad":
            pen.style(col, _lw(0.5), "#FFFFFF")
            pen.poly([it.ring], fill=True)
            pen.hatch([it.ring], 3.2, angle=45, color=col, width=0.25)
            pen.style(col, _lw(0.5))
            pen.poly([it.ring])
        elif it.ring is not None:
            pen.style(INK_P, _lw(0.5), "#FFFFFF")
            pen.poly([it.ring], fill=True)
            pen.style(col, _lw(0.5))
            pen.poly([it.ring])
            P = v.pts(it.ring)
            if it.type in ("handhole", "pullbox", "vault"):
                c.line(*P[0], *P[2])
                c.line(*P[1], *P[3])
            else:
                mono = {"transformer": "T", "generator": "G", "panel": "P", "switchboard": "SB", "mcc": "MCC",
                        "meter": "M", "disconnect": "D"}.get(it.type, "")
                wmin = min(math.dist(P[0], P[1]), math.dist(P[1], P[2]))
                if mono and wmin > fs(7) * 1.1:
                    pen.text(X, Y - fs(7) * 0.35, mono, fs(7), font="Inter-Bold", color=col, anchor="center", reserve=False,
                             force=True)
                if it.type == "generator":
                    rr = min(wmin * 0.35, 6)
                    c.circle(X, Y, rr, stroke=1, fill=0)
        elif it.type in ("pole", "light_pole"):
            rr = max(2.6, 3.4 * pen.sh.s)
            pen.style(col, _lw(0.35), "#FFFFFF")
            c.circle(X, Y, rr, stroke=1, fill=1)
            arm = math.radians(float(r.get("arm_deg", 90)))
            L = rr * 3.2
            ex, ey = X + math.cos(arm) * L, Y + math.sin(arm) * L
            c.line(X + math.cos(arm) * rr, Y + math.sin(arm) * rr, ex, ey)
            c.saveState()
            c.translate(ex, ey)
            c.rotate(math.degrees(arm))
            c.setFillColor(col)
            c.rect(0, -rr * 0.8, rr * 1.8, rr * 1.6, stroke=1, fill=1)
            c.restoreState()
        elif it.type == "utility_pole":
            rr = max(2.6, 3.4 * pen.sh.s)
            pen.style(col, _lw(0.35))
            c.circle(X, Y, rr, stroke=1, fill=0)
            c.line(X - rr * 0.7, Y - rr * 0.7, X + rr * 0.7, Y + rr * 0.7)
            c.line(X - rr * 0.7, Y + rr * 0.7, X + rr * 0.7, Y - rr * 0.7)
        pen.labels.take(_sym_box(v, it))
        drawn[it.type] = drawn.get(it.type, 0) + 1
    for it in job.items:
        if it.path is not None:
            pen.labels.avoid_line(v.pts(it.path + ([it.path[0]] if it.raw.get("closed") else [])))
    # labels: symbol callouts first (they need to sit near their symbol), then runs along their paths
    for it in job.items:
        if it.path is not None:
            continue
        if it.type == "note":
            _note(pen, it)
            continue
        lines = [it.tag, it.label] if it.label and it.label.upper() != it.tag.upper() else [it.tag]
        _callout(pen, it, lines)
    for it in job.items:
        if it.path is not None and it.type != "fence":
            txt = f"{it.tag}: {it.label} ({it.length_ft:,.0f} LF)"
            pts = it.path
            if pen.text_along(pts, txt, fs(8), color=INK_P, font="Inter-SemiBold", min_frac=0.9, above=True):
                continue
            # too tight along the run: tag on the line, full description as a callout with a leader
            pen.text_along(pts, it.tag, fs(8), color=INK_P, font="Inter-SemiBold", min_frac=0.3, above=True)
            (mx, my), _a = geom.point_at(pts, 0.5)
            _callout(pen, Item("note", it.tag, {}, at=(mx, my)), [it.tag, f"{it.label} ({it.length_ft:,.0f} LF)"])
        elif it.type == "fence":
            pen.text_along(it.path, it.label, fs(7.5), color=INK_P, min_frac=0.6, above=True)
    return drawn


INK_P = "#111111"


def _offset(pts, d):
    out = []
    n = len(pts)
    for i in range(n):
        a = pts[max(0, i - 1)]
        b = pts[min(n - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1
        out.append((pts[i][0] - dy / L * d, pts[i][1] + dx / L * d))
    return out


def _sym_box(v, it: Item):
    if it.ring:
        P = v.pts(it.ring)
    else:
        X, Y = v.p(*it.at)
        P = [(X - 6, Y - 6), (X + 16, Y + 16)]
    xs, ys = [p[0] for p in P], [p[1] for p in P]
    return (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)


def _callout(pen, it: Item, lines: list[str]) -> None:
    v = pen.v
    fs = pen.sh.fs
    size = fs(8)
    lh = size * 1.22
    w = max(pen.text_w(t, size, "Inter-SemiBold") for t in lines) + 2
    h = lh * len(lines)
    x0, y0, x1, y1 = _sym_box(v, it)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    gapd = 0.18 * 72
    cands = []
    for d in (1.0, 1.8, 2.8, 4.0, 5.5):
        g_ = gapd * d
        cands += [(x1 + g_, cy + h / 2), (x0 - g_ - w, cy + h / 2), (cx - w / 2, y1 + g_ + h), (cx - w / 2, y0 - g_),
                  (x1 + g_, y1 + g_ + h), (x0 - g_ - w, y1 + g_ + h), (x1 + g_, y0 - g_), (x0 - g_ - w, y0 - g_)]
    for tx, ty in cands:
        box = (tx - 2, ty - h - 2, tx + w + 2, ty + 2)
        if not v.inside(box[0], box[1]) or not v.inside(box[2], box[3]):
            continue
        if pen.labels.free(box, soft=True):
            break
    else:
        tx, ty = cands[0]
        box = (tx - 2, ty - h - 2, tx + w + 2, ty + 2)
    # leader from the label's nearest edge to the symbol
    lx = tx if tx > cx else tx + w
    ly = ty - h / 2
    if not (x0 - 2 <= lx <= x1 + 2 and y0 - 2 <= ly <= y1 + 2):
        sx = min(max(lx, x0), x1)
        sy = min(max(ly, y0), y1)
        if math.dist((lx, ly), (sx, sy)) > 4:
            pen.style(INK_P, 0.5)
            pen.c.line(lx + (-2 if tx > cx else 2), ly, sx, sy)
            pen.c.circle(sx, sy, 0.9, stroke=1, fill=1)
    for i, t in enumerate(lines):
        pen.text(tx, ty - size - i * lh, t, size if i == 0 else fs(7.5), font="Inter-Bold" if i == 0 else "Inter",
                 color=INK_P, halo=True, reserve=False, force=True)
    pen.labels.take(box)


def _note(pen, it: Item) -> None:
    v = pen.v
    fs = pen.sh.fs
    size = fs(8)
    text = str(it.raw.get("text", it.raw.get("label", ""))).upper()
    from pdfcore.ops.plans.site.plan import wrap

    lines = wrap(pen, text, size, float(it.raw.get("width_in", 2.4)) * 72)
    X, Y = v.p(*it.at)
    for i, ln in enumerate(lines):
        pen.text(X, Y - size - i * size * 1.22, ln, size, color=INK_P, halo=True, force=True)
    lead = it.raw.get("leader")
    if lead is not None:
        # leader target in the same coordinate convention as 'at' (already resolved at load time)
        tx, ty = v.p(*it.raw["_leader_xy"]) if "_leader_xy" in it.raw else (None, None)
        if tx is not None:
            pen.style(INK_P, 0.5)
            pen.c.line(X - 2, Y - size * 0.5, tx, ty)
            _arrow(pen, (X - 2, Y - size * 0.5), (tx, ty))


def _arrow(pen, a, b, L: float = 6.0):
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    p = pen.c.beginPath()
    p.moveTo(*b)
    p.lineTo(b[0] - L * math.cos(ang - 0.35), b[1] - L * math.sin(ang - 0.35))
    p.lineTo(b[0] - L * math.cos(ang + 0.35), b[1] - L * math.sin(ang + 0.35))
    p.close()
    pen.c.setFillColor(INK_P)
    pen.c.drawPath(p, stroke=0, fill=1)


def legend_items(pen, job: Job, drawn: dict) -> list:
    from pdfcore.ops.plans.site.plan import _lw

    c = pen.c
    col = _prop_color(pen)
    out = []

    def line_sw(width, dash=None):
        def f(x, y, w, h):
            pen.style(col, width, dash=dash)
            c.line(x, y + h / 2, x + w, y + h / 2)
        return f

    def box_sw(kind):
        def f(x, y, w, h):
            pen.style(col, _lw(0.5), "#FFFFFF")
            c.rect(x + w * 0.2, y, w * 0.6, h, stroke=1, fill=1)
            if kind == "hh":
                c.line(x + w * 0.2, y, x + w * 0.8, y + h)
                c.line(x + w * 0.2, y + h, x + w * 0.8, y)
            if kind == "pad":
                pen.c.saveState()
                p = c.beginPath()
                p.rect(x + w * 0.2, y, w * 0.6, h)
                c.clipPath(p, stroke=0, fill=0)
                pen.style(col, 0.25)
                for i in range(-3, 6):
                    c.line(x + w * 0.2 + i * 3.2, y, x + w * 0.2 + i * 3.2 + h, y + h)
                pen.c.restoreState()
        return f

    def pole_sw(x, y, w, h):
        rr = 3.0
        pen.style(col, _lw(0.35), "#FFFFFF")
        c.circle(x + w * 0.3, y + h / 2, rr, stroke=1, fill=1)
        c.line(x + w * 0.3 + rr, y + h / 2, x + w * 0.62, y + h / 2)
        c.setFillColor(col)
        c.rect(x + w * 0.62, y + h / 2 - 2.4, 5.4, 4.8, stroke=1, fill=1)

    from pdfcore.ops.plans.site.plan import fence_line

    table = [("duct_bank", "PROPOSED DUCT BANK (HEAVY)", line_sw(_lw(0.7))),
             ("conduit", "PROPOSED CONDUIT", line_sw(_lw(0.5), [7, 3])),
             ("trench", "PROPOSED TRENCH", line_sw(_lw(0.25), [2, 2])),
             ("pad", "PROPOSED CONCRETE PAD", box_sw("pad")),
             ("transformer", "PROPOSED EQUIPMENT (TAG PER CALLOUT)", box_sw("eq")),
             ("handhole", "PROPOSED HANDHOLE / PULL BOX", box_sw("hh")),
             ("pole", "PROPOSED LIGHT POLE + FIXTURE", pole_sw)]
    seen_eq = False
    for key, lbl, fn in table:
        keys = {key}
        if key == "transformer":
            keys = {"transformer", "panel", "switchboard", "mcc", "equipment", "generator", "meter", "disconnect"}
        if key == "handhole":
            keys = {"handhole", "pullbox", "vault"}
        if key == "pole":
            keys = {"pole", "light_pole"}
        if any(drawn.get(k) for k in keys):
            if key == "transformer" and seen_eq:
                continue
            seen_eq = seen_eq or key == "transformer"
            out.append((fn, lbl))
    if drawn.get("fence"):
        out.append((lambda x, y, w, h: fence_line_sw(pen, x, y, w, h, col), "PROPOSED FENCE"))
    return out


def fence_line_sw(pen, x, y, w, h, col):
    from pdfcore.ops.plans.site.plan import _lw

    pen.style(col, _lw(0.35))
    mid = y + h / 2
    pen.c.line(x, mid, x + w, mid)
    for xx in (x + w * 0.3, x + w * 0.7):
        pen.c.line(xx - 2.2, mid - 2.2, xx + 2.2, mid + 2.2)
        pen.c.line(xx - 2.2, mid + 2.2, xx + 2.2, mid - 2.2)


def quantities_table(pen, job: Job, x: float, y: float, w: float) -> float:
    from pdfcore.ops.plans.site.plan import heading, wrap

    y = heading(pen, x, y, w, "Quantities (this sheet)")
    size = pen.sh.fs(7.5)
    lh = size * 1.25
    qw, uw = pen.text_w("00,000.0", size) + 4, pen.text_w("CU YD", size) + 8
    dw = w - qw - uw
    pen.text(x, y - size, "DESCRIPTION", size, font="Inter-SemiBold", reserve=False, force=True)
    pen.text(x + dw + qw - 2, y - size, "QTY", size, font="Inter-SemiBold", anchor="right", reserve=False, force=True)
    pen.text(x + dw + qw + 4, y - size, "UNIT", size, font="Inter-SemiBold", reserve=False, force=True)
    y -= lh + 2
    pen.c.setLineWidth(0.4)
    pen.c.setStrokeColor("#1E1E1E")
    pen.c.setDash([])
    pen.c.line(x, y + lh * 0.55, x + w, y + lh * 0.55)
    cat = None
    for r in job.rows:
        if r["category"] != cat:
            cat = r["category"]
            pen.text(x, y - size, cat.upper(), size, font="Inter-SemiBold", color="#333333", reserve=False, force=True)
            y -= lh
        lines = wrap(pen, r["name"], size, dw - 10)
        q = r["quantity"]
        qs = f"{q:,.0f}" if r["unit"] in ("ea",) or q >= 100 else f"{q:,.1f}"
        for j, ln in enumerate(lines):
            pen.text(x + 6, y - size, ln, size, reserve=False, force=True)
            if j == 0:
                pen.text(x + dw + qw - 2, y - size, qs, size, anchor="right", reserve=False, force=True)
                pen.text(x + dw + qw + 4, y - size, r["unit"].upper().replace("FT", "LF") if r["type"] == "length" else r["unit"].upper(),
                         size, reserve=False, force=True)
            y -= lh
    pen.text(x, y - size, "QUANTITIES ARE PLAN LENGTHS FROM THIS DRAWING + STATED EXTRAS; WASTE IN XLSX.", pen.sh.fs(7),
             color="#555555", reserve=False, force=True)
    return y - lh - 2


# ----------------------------------------------------------------------------- DXF
def job_dxf(msp, job: Job, *, text_ft: float = 4.0) -> None:
    import ezdxf

    layer = {"pad": "PROP-PAD", "duct_bank": "PROP-DUCT", "conduit": "PROP-COND", "trench": "PROP-TRENCH",
             "fence": "PROP-FENCE", "handhole": "PROP-HH", "pullbox": "PROP-HH", "vault": "PROP-HH", "pole": "PROP-POLE",
             "light_pole": "PROP-POLE", "utility_pole": "PROP-POLE", "generator": "PROP-GEN", "note": "PROP-TEXT"}
    for it in job.items:
        ly = layer.get(it.type, "PROP-EQPT")
        if it.path is not None:
            msp.add_lwpolyline(it.path, close=bool(it.raw.get("closed")), dxfattribs={"layer": ly})
            (mx, my), ang = geom.point_at(it.path, 0.5)
            if ang > 90:
                ang -= 180
            if ang < -90:
                ang += 180
            txt = f"{it.tag}: {it.label} ({it.length_ft:,.0f} LF)" if it.type != "fence" else it.label
            msp.add_text(txt, height=text_ft * 0.8, rotation=ang, dxfattribs={"layer": "PROP-TEXT"}).set_placement(
                (mx, my), align=ezdxf.enums.TextEntityAlignment.BOTTOM_CENTER)
            continue
        if it.ring:
            msp.add_lwpolyline(it.ring, close=True, dxfattribs={"layer": ly})
        elif it.type in ("pole", "light_pole", "utility_pole"):
            msp.add_circle(it.at, radius=1.0, dxfattribs={"layer": ly})
        if it.type == "note":
            msp.add_text(str(it.raw.get("text", "")).upper(), height=text_ft * 0.8, dxfattribs={"layer": "PROP-TEXT"}).set_placement(it.at)
        else:
            msp.add_text(it.tag, height=text_ft * 0.8, dxfattribs={"layer": "PROP-TEXT"}).set_placement(
                (it.at[0] + 3, it.at[1] + 3))


# ----------------------------------------------------------------------------- op
def site_add(package, job_spec, *, scale: str | None = None, size: str | None = None, brand: str | None = None,
             underlay: str | float | None = None, out: str | Path | None = None, png_dir: str | Path | None = None,
             quiet: bool = False):
    """Job plan: site plan + proposed work (PDF + DXF) and a takeoff XLSX. Returns the OpResult of the PDF."""
    from pdfcore.ops.plans.site.cli import _pct
    from pdfcore.ops.plans.site.geo import SitePackage
    from pdfcore.ops.plans.site.plan import _unique, render

    pkg = package if isinstance(package, SitePackage) else SitePackage.open(package)
    job = load_job(job_spec, pkg)
    m = job.meta
    ul = underlay if underlay is not None else m.get("underlay", "25%")
    ul = _pct(ul) if not isinstance(ul, float) else ul
    view = m.get("view") if isinstance(m.get("view"), dict) else {}
    center = None
    if view.get("center") and str(m.get("coords", "local")).lower() == "local":
        center = [ft(view["center"][0]), ft(view["center"][1])]
    sheet = str(m.get("sheet", "E-101"))
    stem = Path(job_spec).stem if not isinstance(job_spec, dict) else "job"
    pdf_out = Path(out) if out else _unique(pkg.root / f"{sheet}-{stem}.pdf")
    res = render(pkg, scale=scale or str(m.get("scale", "auto")), size=size or str(m.get("size", "ansi-d")),
                 brand=brand or str(m.get("brand", "default")), underlay=ul, underlay_color=bool(m.get("underlay_color")),
                 sheet=sheet, title=str(m.get("title", "Electrical Site Plan")), project=m.get("project"),
                 client=str(m.get("client", "")), drawn=str(m.get("drawn", "")), checked=str(m.get("checked", "")),
                 date=str(m["date"]) if m.get("date") else None, rev=str(m.get("rev", "0")), center=center,
                 job=job, out=pdf_out, view_title_text=m.get("view_title"), png_dir=png_dir, quiet=quiet)
    xp = _unique(pdf_out.with_suffix(".takeoff.xlsx"))
    totals = write_takeoff(job, xp, f"Quantity takeoff — {m.get('project') or pkg.root.name} — {sheet}")
    res.extra_outputs.append(xp)
    res.data["takeoff"] = {"xlsx": str(xp), "rows": job.rows, "totals": totals}
    if not quiet:
        for r in job.rows:
            print(f"  {r['category']:<12} {r['name'][:58]:<58} {r['quantity']:>10,.1f} {r['unit']}")
        print(f"{xp}  ({len(job.rows)} takeoff rows)")
    pkg.log("site-add takeoff", xlsx=xp.name, rows=len(job.rows))
    pkg.save()
    return res
