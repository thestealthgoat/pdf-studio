"""Extraction: page renders, text with positions, search, vector paths, embedded images, tables.

None of these ops writes a PDF. Each returns an :class:`OpResult` whose ``output`` is the main
file written (``gate`` is ``None``), lists every file in ``extra_outputs``, and prints
``<path>  PASS`` once the files are verified (PNG/JPEG open with Pillow, JSON parses, SVG is
well-formed XML, XLSX opens with openpyxl).

Coordinates are points with a top-left origin in unrotated page space (as :mod:`pdfcore.text`).
Page numbers in files and JSON are 1-based.
"""

from __future__ import annotations

import csv
import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from pdfcore import engines
from pdfcore import text as ptext
from pdfcore.ops import command
from pdfcore.ops._common import parse_pages
from pdfcore.result import OpResult, output_path
from pdfcore.types import Block, Rect

__all__ = [
    "render", "text", "search", "vectors", "images", "tables",
    "find_tables", "reading_order", "to_number", "Table",
]


# ---------------------------------------------------------------- helpers


def _page_count(path: Path, password: str | None) -> int:
    return engines.get("render").page_count(path, password=password)


def _select(path: Path, pages: str | Sequence[int] | None, password: str | None) -> list[int]:
    n = _page_count(path, password)
    if pages is None or isinstance(pages, str):
        return parse_pages(pages, n)
    out = list(pages)
    bad = [i + 1 for i in out if not 0 <= i < n]
    if bad:
        raise ValueError(f"page(s) {bad} out of range 1–{n}")
    return out


def _out_dir(src: Path, suffix: str, out: str | Path | None) -> Path:
    """Folder for multi-file output: ``<stem>_<suffix>/`` next to *src* (a fresh one if it is non-empty)."""
    if out is not None:
        d = Path(out)
        if d.exists() and not d.is_dir():
            raise ValueError(f"{d} exists and is not a folder")
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = src.with_name(f"{src.stem}_{suffix}")
    cand, n = base, 2
    while cand.exists() and (not cand.is_dir() or any(cand.iterdir())):
        cand = base.with_name(f"{base.name}-{n}")
        n += 1
    cand.mkdir(parents=True, exist_ok=True)
    return cand


def _unique(p: Path) -> Path:
    cand, n = p, 2
    while cand.exists():
        cand = p.with_name(f"{p.stem}-{n}{p.suffix}")
        n += 1
    return cand


def _file_out(src: Path, tag: str, ext: str, out: str | Path | None) -> Path:
    p = output_path(src, tag, ext, out=out)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _verify(paths: Iterable[Path]) -> None:
    """Raise if any output is missing, empty or does not parse as its type."""
    for p in paths:
        p = Path(p)
        if not p.is_file() or p.stat().st_size == 0:
            raise RuntimeError(f"output not written: {p}")
        ext = p.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".jp2", ".bmp", ".gif"):
            from PIL import Image

            with Image.open(p) as im:
                im.verify()
        elif ext == ".json":
            json.loads(p.read_text(encoding="utf-8"))
        elif ext == ".svg":
            import xml.etree.ElementTree as ET

            ET.parse(p)
        elif ext == ".xlsx":
            import openpyxl

            openpyxl.load_workbook(p, read_only=True).close()


def _done(main: Path | None, files: list[Path], data: dict, quiet: bool) -> OpResult:
    _verify(files)
    if not quiet and main is not None:
        print(f"{main}  PASS")
    return OpResult(main, None, files, data)


def _r(v: float) -> float:
    return round(float(v), 2)


def _bbox(r: Rect) -> list[float]:
    return [_r(r.x0), _r(r.top), _r(r.x1), _r(r.bottom)]


def _write_json(p: Path, obj: Any) -> Path:
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------- render


def render(
    src: str | Path,
    *,
    pages: str | Sequence[int] | None = None,
    dpi: float = 150,
    fmt: str = "png",
    quality: int = 90,
    grayscale: bool = False,
    out: str | Path | None = None,
    password: str | None = None,
    engine: str | None = None,
    quiet: bool = False,
) -> OpResult:
    """Rasterize pages to PNG/JPEG files in ``<stem>_pages/`` (or *out*). Files carry their DPI."""
    src = Path(src)
    fmt = fmt.lower().lstrip(".")
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("png", "jpeg", "tiff"):
        raise ValueError("fmt must be png, jpeg or tiff")
    if not 10 <= dpi <= 2400:
        raise ValueError("dpi must be between 10 and 2400")
    idx = _select(src, pages, password)
    folder = _out_dir(src, "pages", out)
    eng = engines.get("render", engine)
    digits = max(3, len(str(max(idx) + 1))) if idx else 3
    ext = {"png": ".png", "jpeg": ".jpg", "tiff": ".tif"}[fmt]
    files: list[Path] = []
    for i in idx:
        img = eng.render(src, i, dpi=dpi, password=password)
        if grayscale:
            img = img.convert("L")
        p = _unique(folder / f"{src.stem}_p{i + 1:0{digits}d}{ext}")
        kw: dict[str, Any] = {"dpi": (dpi, dpi)}
        if fmt == "jpeg":
            kw.update(quality=quality, optimize=True)
        elif fmt == "png":
            kw.update(optimize=False, compress_level=6)
        elif fmt == "tiff":
            kw.update(compression="tiff_deflate")
        img.save(p, fmt.upper(), **kw)
        files.append(p)
    return _done(files[0] if files else None, files, {"dir": str(folder), "dpi": dpi, "pages": [i + 1 for i in idx]}, quiet)


# ---------------------------------------------------------------- text


LEVELS = ("chars", "words", "lines", "blocks", "plain")


def reading_order(blocks: list[Block], min_gutter: float = 12.0) -> list[Block]:
    """Order text blocks for reading: recursive XY-cut. Column gutters are cut before horizontal gaps
    when both sides are substantial, so two-column pages read column by column."""
    if len(blocks) <= 1:
        return list(blocks)

    def cut(bs: list[Block]) -> list[Block]:
        if len(bs) <= 1:
            return bs
        rs = [b.rect for b in bs]
        top, bottom = min(r.top for r in rs), max(r.bottom for r in rs)
        height = bottom - top or 1.0
        # vertical cut (columns)
        order = sorted(range(len(bs)), key=lambda k: rs[k].x0)
        best = None
        reach = rs[order[0]].x1
        for a, b in zip(order, order[1:]):
            reach = max(reach, rs[a].x1)
            gap = rs[b].x0 - reach
            if gap >= min_gutter:
                left = [bs[k] for k in order if rs[k].x1 <= reach]
                right = [bs[k] for k in order if rs[k].x0 >= rs[b].x0]
                lh = sum(x.rect.height for x in left)
                rh = sum(x.rect.height for x in right)
                if lh >= 0.25 * height and rh >= 0.25 * height:
                    best = (left, right)
                    break
        if best:
            return cut(best[0]) + cut(best[1])
        # horizontal cuts (bands)
        order = sorted(range(len(bs)), key=lambda k: rs[k].top)
        bands: list[list[Block]] = [[bs[order[0]]]]
        reach = rs[order[0]].bottom
        for k in order[1:]:
            if rs[k].top > reach + 0.5:
                bands.append([bs[k]])
            else:
                bands[-1].append(bs[k])
            reach = max(reach, rs[k].bottom)
        if len(bands) > 1:
            return [b for band in bands for b in cut(band)]
        return sorted(bs, key=lambda b: (round(b.rect.top, 0), b.rect.x0))

    return cut(list(blocks))


def text(
    src: str | Path,
    *,
    level: str = "words",
    pages: str | Sequence[int] | None = None,
    layout: bool = False,
    out: str | Path | None = None,
    password: str | None = None,
    engine: str | None = None,
    quiet: bool = False,
) -> OpResult:
    """Export text. ``level`` chars|words|lines|blocks → JSON with boxes; ``plain`` → UTF-8 text in
    reading order (``layout=True`` keeps the page's spatial layout with spaces instead)."""
    src = Path(src)
    level = level.lower()
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    idx = _select(src, pages, password)
    te = engines.get("text", engine)

    if level == "plain":
        parts: list[str] = []
        if layout:
            import pdfplumber

            with pdfplumber.open(str(src), password=password or "") as pdf:
                for i in idx:
                    parts.append(pdf.pages[i].extract_text(layout=True) or "")
        else:
            for i in idx:
                bl = ptext.group_blocks(ptext.group_lines(te.words(src, i, password=password)))
                parts.append("\n\n".join(b.text for b in reading_order(bl)))
        p = _file_out(src, "text", ".txt", out)
        p.write_text("\f\n".join(parts) + "\n", encoding="utf-8")  # form feed between pages
        n_chars = sum(len(x) for x in parts)
        return _done(p, [p], {"pages": len(idx), "chars": n_chars}, quiet)

    doc: dict[str, Any] = {"source": src.name, "level": level, "units": "pt, top-left origin", "pages": []}
    total = 0
    for i in idx:
        w, h = te.page_size(src, i, password=password)
        items: list[dict[str, Any]] = []
        if level == "chars":
            for c in te.chars(src, i, password=password):
                items.append({"text": c.text, "bbox": _bbox(c.rect), "font": c.font, "size": _r(c.size)})
        else:
            ws = te.words(src, i, password=password)
            if level == "words":
                lns = ptext.group_lines(ws)
                ptext.group_blocks(lns)  # assigns line_no / block_no
                for wd in sorted(ws, key=lambda x: (x.block_no, x.line_no, x.rect.x0)):
                    items.append({"text": wd.text, "bbox": _bbox(wd.rect), "font": wd.font, "size": _r(wd.size),
                                  "line": wd.line_no, "block": wd.block_no})
            else:
                bl = reading_order(ptext.group_blocks(ptext.group_lines(ws)))
                if level == "lines":
                    for b_no, b in enumerate(bl):
                        for ln in b.lines:
                            items.append({"text": ln.text, "bbox": _bbox(ln.rect), "block": b_no,
                                          "size": _r(statistics.median(x.size for x in ln.words))})
                else:
                    for b in bl:
                        items.append({"text": b.text, "bbox": _bbox(b.rect),
                                      "lines": [{"text": ln.text, "bbox": _bbox(ln.rect)} for ln in b.lines]})
        total += len(items)
        doc["pages"].append({"page": i + 1, "width": _r(w), "height": _r(h), "items": items})
    p = _write_json(_file_out(src, level, ".json", out), doc)
    return _done(p, [p], {"items": total, "pages": len(idx)}, quiet)


# ---------------------------------------------------------------- search


def search(
    src: str | Path,
    query: str,
    *,
    regex: bool = False,
    case: bool = False,
    whole_word: bool = False,
    pages: str | Sequence[int] | None = None,
    out: str | Path | None = None,
    password: str | None = None,
    quiet: bool = False,
) -> OpResult:
    """Find text. ``data['hits']``: [{page, text, rects (top-left), rects_pdf (bottom-left)}].
    Writes JSON only when *out* is given (``output`` is then that file)."""
    src = Path(src)
    if regex:
        re.compile(query)  # surface a bad pattern as ValueError-ish re.error early
    idx = _select(src, pages, password)
    hits = ptext.search(src, query, regex=regex, case=case, whole_word=whole_word, pages=idx, password=password)
    te = engines.get("text")
    heights: dict[int, float] = {}
    rows = []
    for h in hits:
        if h.page not in heights:
            heights[h.page] = te.page_size(src, h.page, password=password)[1]
        ph = heights[h.page]
        rows.append({
            "page": h.page + 1,
            "text": h.text,
            "rects": [_bbox(r) for r in h.rects],
            "rects_pdf": [[_r(v) for v in r.to_pdf(ph)] for r in h.rects],
        })
    data = {"query": query, "regex": regex, "case": case, "whole_word": whole_word,
            "count": len(rows), "pages_with_hits": sorted({r["page"] for r in rows}), "hits": rows}
    if out is None:
        return OpResult(None, None, [], data)
    p = Path(out)
    if p.is_dir():
        p = output_path(src, "search", ".json", out=p)
    elif p.exists():
        p = _unique(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_json(p, {"source": src.name, **data})
    return _done(p, [p], data, quiet)


# ---------------------------------------------------------------- vector paths


def _rgb(c: Any) -> tuple[float, float, float] | None:
    if c is None or isinstance(c, str):
        return None
    if isinstance(c, (int, float)):
        c = (float(c),)
    try:
        v = [float(x) for x in c]
    except (TypeError, ValueError):
        return None
    if len(v) == 1:
        return (v[0], v[0], v[0])
    if len(v) == 3:
        return (v[0], v[1], v[2])
    if len(v) == 4:
        k = v[3]
        return tuple((1 - min(1.0, x + k)) for x in v[:3])  # type: ignore[return-value]
    return None


def _hex(c: Any) -> str | None:
    rgb = _rgb(c)
    if rgb is None:
        return None
    return "#" + "".join(f"{max(0, min(255, round(x * 255))):02x}" for x in rgb)


def _path_cmds(obj: dict) -> list[list]:
    """pdfplumber path → [["M",x,y],["L",x,y],["C",x1,y1,x2,y2,x,y],["Z"]] (top-left coords)."""
    cmds: list[list] = []
    cur = (0.0, 0.0)
    start = cur
    for seg in obj.get("path") or []:
        op, pts = seg[0], [tuple(map(float, p)) for p in seg[1:]]
        if op == "m":
            cur = start = pts[0]
            cmds.append(["M", *map(_r, cur)])
        elif op == "l":
            cur = pts[0]
            cmds.append(["L", *map(_r, cur)])
        elif op in ("c", "v", "y"):
            if op == "c":
                c1, c2, end = pts[0], pts[1], pts[2]
            elif op == "v":
                c1, c2, end = cur, pts[0], pts[1]
            else:
                c1, c2, end = pts[0], pts[1], pts[1]
            cmds.append(["C", *map(_r, c1), *map(_r, c2), *map(_r, end)])
            cur = end
        elif op == "h":
            cmds.append(["Z"])
            cur = start
    if not cmds:
        pts = [tuple(map(float, p)) for p in obj.get("pts") or []]
        for k, p in enumerate(pts):
            cmds.append(["M" if k == 0 else "L", _r(p[0]), _r(p[1])])
    return cmds


def _page_vectors(page, pno: int) -> list[dict[str, Any]]:
    out = []
    for kind, objs in (("line", page.lines), ("rect", page.rects), ("curve", page.curves)):
        for o in objs:
            stroke = o.get("stroke", True)
            fill = o.get("fill", False)
            dash = o.get("dash")
            out.append({
                "kind": kind,
                "page": pno,
                "bbox": [_r(o["x0"]), _r(o["top"]), _r(o["x1"]), _r(o["bottom"])],
                "path": _path_cmds(o),
                "stroke": _hex(o.get("stroking_color", 0)) if stroke else None,
                "fill": _hex(o.get("non_stroking_color", 0)) if fill else None,
                "width": _r(o.get("linewidth") or 0),
                "dash": [list(map(float, dash[0])), float(dash[1])] if dash and dash[0] else None,
                "evenodd": bool(o.get("evenodd")),
            })
    # painting order approximates the content-stream order
    out.sort(key=lambda d: (0 if d["fill"] and not d["stroke"] else 1))
    return out


def _svg_d(cmds: list[list]) -> str:
    return " ".join(f"{c[0]}" + ("" if len(c) == 1 else " " + " ".join(f"{v:g}" for v in c[1:])) for c in cmds)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _svg(items: list[dict], w: float, h: float, words: list | None, background: str | None) -> str:
    rows = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:g}pt" height="{h:g}pt" viewBox="0 0 {w:g} {h:g}">',
    ]
    if background:
        rows.append(f'<rect x="0" y="0" width="{w:g}" height="{h:g}" fill="{background}"/>')
    rows.append('<g stroke-linecap="butt" stroke-linejoin="miter">')
    for it in items:
        if not it["stroke"] and not it["fill"]:
            continue
        a = [f'class="{it["kind"]}"', f'd="{_svg_d(it["path"])}"']
        a.append(f'fill="{it["fill"]}"' if it["fill"] else 'fill="none"')
        if it["fill"] and it["evenodd"]:
            a.append('fill-rule="evenodd"')
        if it["stroke"]:
            a.append(f'stroke="{it["stroke"]}" stroke-width="{max(it["width"], 0.25):g}"')
            if it["dash"]:
                arr = " ".join(f"{v:g}" for v in it["dash"][0])
                a.append(f'stroke-dasharray="{arr}" stroke-dashoffset="{it["dash"][1]:g}"')
        rows.append(f'<path {" ".join(a)}/>')
    rows.append("</g>")
    if words:
        rows.append('<g font-family="Helvetica, Arial, sans-serif" fill="#000">')
        for wd in words:
            size = wd.size or wd.rect.height
            rows.append(f'<text x="{wd.rect.x0:.2f}" y="{wd.rect.bottom - 0.2 * size:.2f}" font-size="{size:.2f}">{_esc(wd.text)}</text>')
        rows.append("</g>")
    rows.append("</svg>")
    return "\n".join(rows) + "\n"


def vectors(
    src: str | Path,
    *,
    pages: str | Sequence[int] | None = None,
    fmt: str = "both",
    include_text: bool = False,
    background: str | None = "#ffffff",
    out: str | Path | None = None,
    password: str | None = None,
    quiet: bool = False,
) -> OpResult:
    """Vector linework (lines, rects, curves with stroke/fill colour, width, dash) → JSON and/or one SVG per page.

    JSON goes to ``<stem>.vectors.json``; SVGs to ``<stem>.pNNN.svg`` beside it (*out* may be a folder).
    """
    import pdfplumber

    src = Path(src)
    fmt = fmt.lower()
    if fmt not in ("both", "json", "svg"):
        raise ValueError("fmt must be both, json or svg")
    idx = _select(src, pages, password)
    out_dir: Path | None = None
    json_path: Path | None = None
    if out is not None:
        o = Path(out)
        if o.suffix.lower() in (".json", ".svg") and not o.is_dir():
            o.parent.mkdir(parents=True, exist_ok=True)
            out_dir = o.parent
            if o.suffix.lower() == ".json":
                json_path = _unique(o)
        else:
            o.mkdir(parents=True, exist_ok=True)
            out_dir = o
    files: list[Path] = []
    doc: dict[str, Any] = {"source": src.name, "units": "pt, top-left origin", "pages": []}
    counts: dict[str, int] = {"line": 0, "rect": 0, "curve": 0}
    te = engines.get("text")
    with pdfplumber.open(str(src), password=password or "") as pdf:
        for i in idx:
            page = pdf.pages[i]
            w, h = float(page.width), float(page.height)
            items = _page_vectors(page, i + 1)
            for it in items:
                counts[it["kind"]] += 1
            doc["pages"].append({"page": i + 1, "width": _r(w), "height": _r(h), "items": items})
            if fmt in ("both", "svg"):
                words = te.words(src, i, password=password) if include_text else None
                p = output_path(src, f"p{i + 1:03d}", ".svg", out=out_dir)
                p.write_text(_svg(items, w, h, words, background), encoding="utf-8")
                files.append(p)
    if fmt in ("both", "json"):
        jp = json_path or output_path(src, "vectors", ".json", out=out_dir)
        _write_json(jp, doc)
        files.insert(0, jp)
    return _done(files[0], files, {"counts": counts, "pages": [i + 1 for i in idx]}, quiet)


# ---------------------------------------------------------------- embedded images


def _inline_to_pil(stream):
    from PIL import Image

    attrs = stream.attrs
    w, h = int(attrs.get("W") or attrs.get("Width")), int(attrs.get("H") or attrs.get("Height"))
    bpc = int(attrs.get("BPC") or attrs.get("BitsPerComponent") or 8)
    cs = str(attrs.get("CS") or attrs.get("ColorSpace") or "")
    data = stream.get_data()
    mode = "RGB" if "RGB" in cs else "L" if ("G" in cs or "Gray" in cs) else None
    if mode is None or bpc not in (1, 8):
        return None
    if bpc == 1:
        return Image.frombytes("1", (w, h), data)
    return Image.frombytes(mode, (w, h), data)


def images(
    src: str | Path,
    *,
    pages: str | Sequence[int] | None = None,
    fmt: str = "native",
    min_px: int = 16,
    out: str | Path | None = None,
    password: str | None = None,
    quiet: bool = False,
) -> OpResult:
    """Extract embedded raster images to ``<stem>_images/`` with a manifest ``images.json``.

    ``fmt='native'`` keeps JPEG/JPEG 2000 streams byte-for-byte and writes the rest as PNG;
    ``fmt='png'`` converts everything to PNG. An image drawn several times is written once and
    lists every placement (page, bbox, size in points, effective DPI). Soft masks are not merged.
    """
    import pdfplumber
    import pikepdf
    from pikepdf import PdfImage

    src = Path(src)
    fmt = fmt.lower()
    if fmt not in ("native", "png"):
        raise ValueError("fmt must be native or png")
    idx = _select(src, pages, password)
    folder = _out_dir(src, "images", out)
    written: dict[int, dict[str, Any]] = {}
    manifest: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    files: list[Path] = []
    seq = 0
    with pikepdf.open(str(src), password=password or "") as pk, pdfplumber.open(str(src), password=password or "") as pl:
        for i in idx:
            for im in pl.pages[i].images:
                stream = im.get("stream")
                objid = getattr(stream, "objid", None)
                sw, sh = (im.get("srcsize") or (0, 0))
                place = {
                    "page": i + 1,
                    "bbox": [_r(im["x0"]), _r(im["top"]), _r(im["x1"]), _r(im["bottom"])],
                    "size_pt": [_r(im["x1"] - im["x0"]), _r(im["bottom"] - im["top"])],
                    "dpi": [
                        _r(sw * 72 / (im["x1"] - im["x0"])) if im["x1"] > im["x0"] else None,
                        _r(sh * 72 / (im["bottom"] - im["top"])) if im["bottom"] > im["top"] else None,
                    ],
                }
                if sw < min_px or sh < min_px:
                    skipped.append({**place, "reason": f"smaller than {min_px}px"})
                    continue
                if objid is not None and objid in written:
                    written[objid]["placements"].append(place)
                    continue
                seq += 1
                stem = folder / f"{src.stem}_p{i + 1:03d}_img{seq:03d}"
                path: Path | None = None
                try:
                    if objid is not None:
                        pim = PdfImage(pk.get_object((objid, 0)))
                        if fmt == "png":
                            path = _unique(stem.with_suffix(".png"))
                            pim.as_pil_image().save(path, "PNG")
                        else:
                            path = Path(pim.extract_to(fileprefix=str(stem)))
                    else:
                        pil = _inline_to_pil(stream)
                        if pil is not None:
                            path = _unique(stem.with_suffix(".png"))
                            pil.save(path, "PNG")
                except Exception as exc:  # unsupported filter / colour space
                    skipped.append({**place, "reason": f"cannot decode: {exc}"})
                    seq -= 1
                    continue
                if path is None:
                    skipped.append({**place, "reason": "inline image in an unsupported format"})
                    seq -= 1
                    continue
                cs = im.get("colorspace")
                entry = {
                    "file": path.name,
                    "width_px": int(sw),
                    "height_px": int(sh),
                    "bits": im.get("bits"),
                    "colorspace": str(cs[0]) if isinstance(cs, list) and cs else str(cs) if cs else None,
                    "name": im.get("name"),
                    "placements": [place],
                }
                manifest.append(entry)
                files.append(path)
                if objid is not None:
                    written[objid] = entry
    mp = _write_json(_unique(folder / "images.json"), {"source": src.name, "images": manifest, "skipped": skipped})
    return _done(mp, [mp, *files], {"dir": str(folder), "count": len(files), "skipped": len(skipped)}, quiet)


# ---------------------------------------------------------------- tables


@dataclass
class Table:
    page: int  # 0-based
    bbox: Rect
    rows: list[list[str]]
    strategy: str  # "lines" | "text" | "camelot-<flavor>"

    @property
    def header(self) -> list[str]:
        return self.rows[0] if self.rows else []

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page + 1, "bbox": _bbox(self.bbox), "strategy": self.strategy,
                "n_rows": len(self.rows), "n_cols": max((len(r) for r in self.rows), default=0), "rows": self.rows}


_NUM = re.compile(r"^\(?[-+−]?[$€£¥]?\s*\(?[-+−]?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?\)?%?$")


def to_number(s: str) -> int | float | None:
    """'1,234.50' → 1234.5, '$12' → 12, '(3.00)' → -3.0, '12%' → 0.12. Codes like '20/1', '007', 'E-101' → None."""
    t = s.strip().replace(" ", "").replace(" ", "")
    if not t or not any(ch.isdigit() for ch in t) or not _NUM.match(t):
        return None
    neg = t.startswith("(") and t.endswith(")") or "-" in t or "−" in t
    pct = t.endswith("%")
    core = re.sub(r"[()$€£¥%+\-−,]", "", t)
    if not core or core == ".":
        return None
    if len(core) > 1 and core[0] == "0" and core[1].isdigit():
        return None  # leading zero: an identifier, keep as text
    try:
        v: int | float = float(core) if "." in core else int(core)
    except ValueError:
        return None
    if pct:
        v = float(v) / 100
    return -v if neg else v


@dataclass
class _Cell:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass
class _Row:
    cells: list[_Cell] = field(default_factory=list)

    @property
    def top(self) -> float:
        return min(c.top for c in self.cells)

    @property
    def bottom(self) -> float:
        return max(c.bottom for c in self.cells)

    @property
    def height(self) -> float:
        return statistics.median(c.bottom - c.top for c in self.cells)


def _rows_from_words(words: list[dict], gap_em: float = 0.8) -> list[_Row]:
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        h = w["bottom"] - w["top"] or 1.0
        for r in reversed(rows[-3:]):
            rt, rb = min(x["top"] for x in r), max(x["bottom"] for x in r)
            ov = min(rb, w["bottom"]) - max(rt, w["top"])
            if ov >= 0.5 * min(h, rb - rt or 1.0):
                r.append(w)
                break
        else:
            rows.append([w])
    out = []
    for r in rows:
        r.sort(key=lambda w: w["x0"])
        cells: list[list[dict]] = [[r[0]]]
        for w in r[1:]:
            prev = cells[-1][-1]
            em = min(prev["bottom"] - prev["top"], w["bottom"] - w["top"]) or 1.0
            if w["x0"] - prev["x1"] <= gap_em * em:
                cells[-1].append(w)
            else:
                cells.append([w])
        out.append(_Row([_Cell(" ".join(x["text"] for x in c), c[0]["x0"], max(x["x1"] for x in c),
                               min(x["top"] for x in c), max(x["bottom"] for x in c)) for c in cells]))
    out.sort(key=lambda r: r.top)
    return out


def _fit(row: _Row, cols: list[list[float]], tol: float = 2.0) -> list[int] | None:
    """Map each cell to exactly one column (overlap); None if a cell misses or straddles columns."""
    m: list[int] = []
    for c in row.cells:
        hit = [j for j, (a, b) in enumerate(cols) if min(b, c.x1) - max(a, c.x0) > -tol]
        if len(hit) != 1:
            return None
        m.append(hit[0])
    if len(set(m)) != len(m) or m != sorted(m):
        return None
    return m


def _text_tables(rows: list[_Row], page: int, min_rows: int, min_cols: int) -> list[Table]:
    tables: list[Table] = []
    k = 0
    while k < len(rows):
        seed = rows[k]
        if len(seed.cells) < min_cols:
            k += 1
            continue
        cols = [[c.x0, c.x1] for c in seed.cells]
        run: list[tuple[_Row, list[int]]] = [(seed, list(range(len(cols))))]
        gaps: list[float] = []
        j = k + 1
        while j < len(rows):
            r = rows[j]
            prev = run[-1][0]
            gap = r.top - prev.bottom
            hmed = statistics.median(x[0].height for x in run)
            limit = 4.0 * hmed if len(run) == 1 else max(2.5 * hmed, 1.5 * statistics.median(gaps))
            if gap > limit or not (0.6 * hmed <= r.height <= 1.6 * hmed):
                break
            m = _fit(r, cols)
            if m is None:
                break
            if len(r.cells) < 2 and len(cols) > 2 and sum(1 for x in run[-2:] if len(x[0].cells) < 2) >= 2:
                break  # too many sparse rows in a row: probably left the table
            for c, jj in zip(r.cells, m):
                cols[jj] = [min(cols[jj][0], c.x0), max(cols[jj][1], c.x1)]
            gaps.append(max(gap, 0.0))
            run.append((r, m))
            j += 1
        while len(run) > 1 and len(run[-1][0].cells) < min_cols:
            run.pop()  # trailing sparse rows (headings, notes) are not part of the table
        if len(run) >= min_rows:
            grid = [[""] * len(cols) for _ in run]
            for ri, (r, m) in enumerate(run):
                for c, jj in zip(r.cells, m):
                    grid[ri][jj] = c.text
            ncell = [c for r, _ in run for c in r.cells]
            mean_words = sum(len(c.text.split()) for c in ncell) / max(1, len(ncell))
            filled = [sum(1 for ri in range(len(grid)) if grid[ri][cj]) for cj in range(len(cols))]
            if mean_words <= 4.5 and sum(1 for f in filled if f >= 2) >= min_cols:
                bb = Rect(min(c.x0 for c in ncell), min(c.top for c in ncell), max(c.x1 for c in ncell), max(c.bottom for c in ncell))
                tables.append(Table(page, bb, grid, "text"))
                k = k + len(run)
                continue
        k += 1
    return tables


def _clean(v: Any) -> str:
    return re.sub(r"\s*\n\s*", " ", str(v)).strip() if v is not None else ""


def _lines_tables(page, pno: int, min_rows: int, min_cols: int) -> list[Table]:
    out = []
    settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines", "snap_tolerance": 3,
                "join_tolerance": 3, "intersection_tolerance": 3}
    for t in page.find_tables(settings):
        raw = t.extract()
        rows = [[_clean(c) for c in r] for r in raw if any((c or "").strip() for c in r)]
        if len(rows) < min_rows or max((len(r) for r in rows), default=0) < min_cols:
            continue
        cells = [c for r in raw for c in r if c]
        if not cells:
            continue
        # a drawing frame or a text box is "one cell full of paragraphs", not a table
        if statistics.mean(str(c).count("\n") + 1 for c in cells) > 3.0:
            continue
        x0, top, x1, bottom = t.bbox
        out.append(Table(pno, Rect(x0, top, x1, bottom), rows, "lines"))
    return out


def find_tables(
    src: str | Path,
    *,
    pages: str | Sequence[int] | None = None,
    strategy: str = "auto",
    min_rows: int = 3,
    min_cols: int = 2,
    password: str | None = None,
) -> list[Table]:
    """Detect tables. ``auto``: ruled tables via pdfplumber's "lines" strategy, then whitespace-aligned
    tables ("text" strategy: rows of aligned cells, column extents grown row by row) among words
    outside those. ``lines`` / ``text`` run just one; ``camelot`` / ``camelot-stream`` use Camelot if installed."""
    src = Path(src)
    strategy = strategy.lower()
    idx = _select(src, pages, password)
    if strategy.startswith("camelot"):
        return _camelot_tables(src, idx, strategy, password)
    if strategy not in ("auto", "lines", "text"):
        raise ValueError("strategy must be auto, lines, text, camelot or camelot-stream")
    import pdfplumber

    found: list[Table] = []
    with pdfplumber.open(str(src), password=password or "") as pdf:
        for i in idx:
            page = pdf.pages[i]
            ruled = _lines_tables(page, i, min_rows, min_cols) if strategy in ("auto", "lines") else []
            found += ruled
            if strategy in ("auto", "text"):
                words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False, use_text_flow=False)
                if ruled:
                    words = [w for w in words if not any(
                        t.bbox.contains_point((w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2) for t in ruled)]
                found += _text_tables(_rows_from_words(words), i, min_rows, min_cols)
    found.sort(key=lambda t: (t.page, t.bbox.top, t.bbox.x0))
    return found


def _camelot_tables(src: Path, idx: list[int], strategy: str, password: str | None) -> list[Table]:
    try:
        import camelot  # optional extra; MIT
    except ImportError as exc:
        raise RuntimeError("Camelot is not installed: pip install 'pdf-studio[tables]' (or use strategy auto)") from exc
    flavor = strategy.split("-", 1)[1] if "-" in strategy else "lattice"
    kw: dict[str, Any] = {"pages": ",".join(str(i + 1) for i in idx), "flavor": flavor}
    if password:
        kw["password"] = password
    if flavor == "lattice":
        kw["backend"] = "pdfium"  # never the ghostscript backend
    try:
        res = camelot.read_pdf(str(src), **kw)
    except TypeError as exc:
        raise RuntimeError("this Camelot version has no pdfium backend; upgrade camelot-py (>=1.0) or use strategy auto") from exc
    te = engines.get("text")
    out = []
    for t in res:
        pno = int(t.page) - 1
        rows = [[_clean(v) for v in r] for r in t.df.values.tolist()]
        rows = [r for r in rows if any(r)]
        h = te.page_size(src, pno, password=password)[1]
        x0, y0, x1, y1 = getattr(t, "_bbox", (0, 0, 0, 0))
        out.append(Table(pno, Rect.from_pdf(x0, y0, x1, y1, h), rows, f"camelot-{flavor}"))
    return out


def _xlsx(tables: list[Table], path: Path, numbers: bool) -> None:
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    per_page: dict[int, int] = {}
    for t in tables:
        per_page[t.page] = per_page.get(t.page, 0) + 1
        ws = wb.create_sheet(title=f"p{t.page + 1} t{per_page[t.page]}"[:31])
        widths: dict[int, int] = {}
        for ri, row in enumerate(t.rows, start=1):
            for ci, val in enumerate(row, start=1):
                v: Any = val
                cell = ws.cell(row=ri, column=ci)
                if ri > 1 and numbers:
                    num = to_number(val)
                    if num is not None:
                        v = num
                        if val.strip().endswith("%"):
                            cell.number_format = "0.0%"
                        elif isinstance(num, float):
                            dec = len(val.split(".")[-1].rstrip("%)")) if "." in val else 2
                            cell.number_format = ("#,##0." if "," in val else "0.") + "0" * max(1, min(dec, 6))
                        elif "," in val:
                            cell.number_format = "#,##0"
                cell.value = v
                if ri == 1:
                    cell.font = Font(bold=True)
                widths[ci] = max(widths.get(ci, 0), len(val))
        for ci, wdt in widths.items():
            ws.column_dimensions[get_column_letter(ci)].width = min(60, max(8, wdt + 2))
        ws.freeze_panes = "A2"
    if not tables:
        ws = wb.create_sheet("no tables")
        ws["A1"] = "No tables found"
    wb.save(path)


def tables(
    src: str | Path,
    *,
    pages: str | Sequence[int] | None = None,
    fmt: str = "xlsx",
    strategy: str = "auto",
    numbers: bool = True,
    min_rows: int = 3,
    min_cols: int = 2,
    out: str | Path | None = None,
    password: str | None = None,
    quiet: bool = False,
) -> OpResult:
    """Tables → ``<stem>.tables.xlsx`` (one sheet per table, bold header, numeric cells as numbers),
    CSV files in ``<stem>_tables/`` (``fmt='csv'``), or ``<stem>.tables.json``. ``data['tables']`` has them all."""
    src = Path(src)
    fmt = fmt.lower()
    if fmt not in ("xlsx", "csv", "json"):
        raise ValueError("fmt must be xlsx, csv or json")
    found = find_tables(src, pages=pages, strategy=strategy, min_rows=min_rows, min_cols=min_cols, password=password)
    data = {"count": len(found), "tables": [t.to_dict() for t in found]}
    files: list[Path] = []
    if fmt == "xlsx":
        p = _file_out(src, "tables", ".xlsx", out)
        _xlsx(found, p, numbers)
        files = [p]
    elif fmt == "json":
        p = _write_json(_file_out(src, "tables", ".json", out), {"source": src.name, **data})
        files = [p]
    else:
        folder = _out_dir(src, "tables", out)
        per_page: dict[int, int] = {}
        for t in found:
            per_page[t.page] = per_page.get(t.page, 0) + 1
            p = _unique(folder / f"{src.stem}_p{t.page + 1:03d}_t{per_page[t.page]:02d}.csv")
            with p.open("w", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh).writerows(t.rows)
            files.append(p)
        data["dir"] = str(folder)
        if not files:
            if not quiet:
                print(f"{folder}  PASS (no tables found)")
            return OpResult(None, None, [], data)
    return _done(files[0], files, data, quiet)


# ---------------------------------------------------------------- CLI


def _pw(ns) -> str | None:
    return getattr(ns, "password", None)


def _base(p, *, pages: bool = True) -> None:
    p.add_argument("pdf")
    if pages:
        p.add_argument("--pages", help="page selection, e.g. 1-3,5 (default: all)")
    p.add_argument("-o", "--out", help="output file or folder (default: next to the input)")
    p.add_argument("--password", help="password to open the input")


@command("render", "Render pages to PNG/JPEG/TIFF images at any DPI", group="extract")
def _cli_render(p):
    _base(p)
    p.add_argument("--dpi", type=float, default=150)
    p.add_argument("--format", dest="fmt", default="png", choices=["png", "jpeg", "jpg", "tiff"])
    p.add_argument("--quality", type=int, default=90, help="JPEG quality")
    p.add_argument("--gray", action="store_true", help="grayscale images")
    return lambda ns: render(ns.pdf, pages=ns.pages, dpi=ns.dpi, fmt=ns.fmt, quality=ns.quality,
                             grayscale=ns.gray, out=ns.out, password=_pw(ns))


@command("text", "Export text: JSON with boxes (chars/words/lines/blocks) or plain text in reading order", group="extract")
def _cli_text(p):
    _base(p)
    p.add_argument("--level", default="plain", choices=list(LEVELS))
    p.add_argument("--layout", action="store_true", help="plain text keeping the page layout")
    return lambda ns: text(ns.pdf, level=ns.level, pages=ns.pages, layout=ns.layout, out=ns.out, password=_pw(ns))


@command("search", "Search text; print hits with page and rectangles as JSON", group="extract")
def _cli_search(p):
    _base(p)
    p.add_argument("query")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--case", action="store_true", help="case-sensitive")
    p.add_argument("--whole-word", action="store_true")

    def run(ns):
        res = search(ns.pdf, ns.query, regex=ns.regex, case=ns.case, whole_word=ns.whole_word,
                     pages=ns.pages, out=ns.out, password=_pw(ns), quiet=True)
        import sys

        if ns.out:
            print(f"{res.output}  PASS")
        else:
            print(json.dumps(res.data, indent=1, ensure_ascii=False))
        print(f"{res.data['count']} hit(s) on {len(res.data['pages_with_hits'])} page(s)", file=sys.stderr)
        return res

    return run


@command("vectors", "Export vector paths (lines, rects, curves) to JSON and SVG", group="extract")
def _cli_vectors(p):
    _base(p)
    p.add_argument("--format", dest="fmt", default="both", choices=["both", "json", "svg"])
    p.add_argument("--text", action="store_true", help="also draw text in the SVG")
    p.add_argument("--transparent", action="store_true", help="no white background in the SVG")
    return lambda ns: vectors(ns.pdf, pages=ns.pages, fmt=ns.fmt, include_text=ns.text,
                              background=None if ns.transparent else "#ffffff", out=ns.out, password=_pw(ns))


@command("extract-images", "Extract embedded raster images with page and placement info", group="extract")
def _cli_images(p):
    _base(p)
    p.add_argument("--format", dest="fmt", default="native", choices=["native", "png"])
    p.add_argument("--min-px", type=int, default=16, help="skip images smaller than this")
    return lambda ns: images(ns.pdf, pages=ns.pages, fmt=ns.fmt, min_px=ns.min_px, out=ns.out, password=_pw(ns))


@command("tables", "Extract tables to XLSX (one sheet per table), CSV or JSON", group="extract")
def _cli_tables(p):
    _base(p)
    p.add_argument("--format", dest="fmt", default="xlsx", choices=["xlsx", "csv", "json"])
    p.add_argument("--strategy", default="auto", choices=["auto", "lines", "text", "camelot", "camelot-stream"])
    p.add_argument("--no-numbers", action="store_true", help="keep numeric cells as text in XLSX")
    p.add_argument("--min-rows", type=int, default=3)
    return lambda ns: tables(ns.pdf, pages=ns.pages, fmt=ns.fmt, strategy=ns.strategy, numbers=not ns.no_numbers,
                             min_rows=ns.min_rows, out=ns.out, password=_pw(ns))
