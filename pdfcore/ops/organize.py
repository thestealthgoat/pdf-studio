"""Organize pages: merge, split, insert, delete, reorder, extract, duplicate, rotate, crop, page labels.

Library functions take 0-based page indexes; the CLI takes 1-based page specs (``1-3,5``).
Bookmarks, internal links, form fields and page labels follow the pages they belong to:
entries for deleted pages are dropped, the rest are re-pointed at the new positions.
Encrypted inputs are opened with ``password`` and the output keeps the input's encryption.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.ops.navigate import (Bookmark, named_dests, open_pdf, page_index_map, read_outline, remap_outline,
                                  resolve_dest, save_pdf, write_outline)
from pdfcore.overlay import visual_size, visual_to_user_matrix
from pdfcore.result import OpResult

# ====================================================================== page labels


class LabelRange(NamedTuple):
    """A /PageLabels range: from page *start* (0-based) on, label = prefix + number in *style*."""

    start: int
    style: str | None = "D"  # D decimal · r/R roman · a/A letters · None prefix only
    prefix: str = ""
    first: int = 1


_STYLES = {"d": "D", "decimal": "D", "1": "D", "arabic": "D", "r": "r", "roman": "r", "i": "r",
           "upper-roman": "R", "roman-upper": "R", "a": "a", "alpha": "a", "letters": "a",
           "upper-alpha": "A", "alpha-upper": "A", "none": None, "-": None, "": None}


def norm_style(s: str | None) -> str | None:
    if s is None:
        return None
    if s in ("D", "r", "R", "a", "A"):
        return s
    if s in ("I", "ROMAN"):
        return "R"
    if s in ("ALPHA", "LETTERS"):
        return "A"
    if s.lower() not in _STYLES:
        raise ValueError(f"unknown page-label style {s!r} (use decimal, roman, ROMAN, alpha, ALPHA, none)")
    return _STYLES[s.lower()]


PageLabel = tuple[str | None, str, int]  # (style, prefix, number)


def read_page_labels(pdf: pikepdf.Pdf) -> list[PageLabel] | None:
    """Per-page (style, prefix, number) from /PageLabels, or None when the document has none."""
    if "/PageLabels" not in pdf.Root:
        return None
    ranges = sorted((int(k), v) for k, v in pikepdf.NumberTree(pdf.Root.PageLabels).items())
    if not ranges:
        return None
    out: list[PageLabel] = []
    for i in range(len(pdf.pages)):
        cur = [r for r in ranges if r[0] <= i]
        if not cur:
            out.append(("D", "", i + 1))
            continue
        start, d = cur[-1]
        style = str(d.S)[1:] if "/S" in d else None
        out.append((style, str(d.get("/P", "")), int(d.get("/St", 1)) + i - start))
    return out


def _label_dict(style: str | None, prefix: str, first: int) -> Dictionary:
    d = Dictionary(Type=Name.PageLabel)
    if style:
        d.S = Name("/" + style)
    if prefix:
        d.P = String(prefix)
    if first != 1:
        d.St = first
    return d


def write_label_ranges(pdf: pikepdf.Pdf, ranges: Sequence[LabelRange]) -> None:
    n = len(pdf.pages)
    rs = sorted(ranges, key=lambda r: r.start)
    for r in rs:
        if not 0 <= r.start < n:
            raise ValueError(f"page-label range starts at page {r.start + 1}, document has {n}")
    if not rs or rs[0].start != 0:  # ISO 32000: the tree must include page index 0
        rs.insert(0, LabelRange(0, "D", "", 1))
    nums = Array()
    for r in rs:
        nums.append(r.start)
        nums.append(_label_dict(r.style, r.prefix, r.first))
    pdf.Root.PageLabels = pdf.make_indirect(Dictionary(Nums=nums))


def write_page_labels(pdf: pikepdf.Pdf, labels: Sequence[PageLabel] | None) -> None:
    """Compress per-page labels back into ranges (a new range wherever numbering breaks)."""
    if labels is None:
        if "/PageLabels" in pdf.Root:
            del pdf.Root["/PageLabels"]
        return
    ranges: list[LabelRange] = []
    for i, (style, prefix, num) in enumerate(labels):
        if ranges:
            prev = labels[i - 1]
            if (style, prefix) == prev[:2] and (style is None or num == prev[2] + 1):
                continue
        ranges.append(LabelRange(i, style, prefix, num))
    write_label_ranges(pdf, ranges)


def _default_labels(n: int) -> list[PageLabel]:
    return [("D", "", i + 1) for i in range(n)]


# ====================================================================== page rearrangement core


_FIELD_KEYS = ("/FT", "/T", "/TU", "/TM", "/Ff", "/V", "/DV", "/Opt", "/TI", "/I", "/MaxLen")


def _register_widget_copy(pdf: pikepdf.Pdf, orig: Dictionary, copy: Dictionary) -> None:
    """Make a copied widget a second widget of the same form field (so it stays in /AcroForm)."""
    if "/Parent" in copy:
        parent = copy.Parent
        if not isinstance(parent.get("/Kids"), Array):
            parent.Kids = Array()
        parent.Kids.append(copy)
        return
    af = pdf.Root.get("/AcroForm")
    if "/T" not in orig or not isinstance(af, Dictionary) or not isinstance(af.get("/Fields"), Array):
        return
    # Merged field+widget: split into a parent field with two widget kids.
    field = pdf.make_indirect(Dictionary({k: orig[k] for k in _FIELD_KEYS + ("/DA", "/Q") if k in orig}))
    for w in (orig, copy):
        for k in _FIELD_KEYS:
            if k in w:
                del w[k]
        w.Parent = field
    field.Kids = Array([orig, copy])
    af.Fields = Array([field if f.objgen == orig.objgen else f for f in af.Fields])


def _copy_annots(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
    """Give a duplicated page its own annotation dicts (a shallow page copy shares them)."""
    annots = page.obj.get("/Annots")
    if not isinstance(annots, Array):
        return
    new = Array()
    for a in annots:
        c = pdf.make_indirect(Dictionary({k: v for k, v in a.items()}))
        if "/P" in c:
            c.P = page.obj
        if a.get("/Subtype") == Name.Widget:
            _register_widget_copy(pdf, a, c)
        new.append(c)
    page.obj.Annots = new


def _is_internal_link(a: Dictionary) -> bool:
    if a.get("/Subtype") != Name.Link:
        return False
    if "/Dest" in a:
        return True
    act = a.get("/A")
    return isinstance(act, Dictionary) and act.get("/S") == Name.GoTo


def _drop_dangling_links(pdf: pikepdf.Pdf) -> int:
    """Remove internal links whose target page is no longer in the document."""
    index, names, removed = page_index_map(pdf), named_dests(pdf), 0
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array):
            continue
        keep = Array()
        for a in annots:
            dest = a.get("/Dest") if "/Dest" in a else a.get("/A")
            if _is_internal_link(a) and resolve_dest(pdf, dest, index, names)[0] is None:
                removed += 1
            else:
                keep.append(a)
        if len(keep) != len(annots):
            page.obj.Annots = keep
    return removed


def _prune_acroform(pdf: pikepdf.Pdf) -> None:
    """Drop form fields whose widgets are no longer on any page."""
    af = pdf.Root.get("/AcroForm")
    if not isinstance(af, Dictionary) or not isinstance(af.get("/Fields"), Array):
        return
    live = {a.objgen for p in pdf.pages for a in (p.obj.get("/Annots") or []) if a.is_indirect}

    def alive(f, depth=0) -> bool:
        return f.objgen in live or (depth < 20 and any(alive(k, depth + 1) for k in f.get("/Kids", [])))

    af.Fields = Array([f for f in af.Fields if alive(f)])


def rearrange(pdf: pikepdf.Pdf, order: Sequence[int]) -> None:
    """Make the document's pages ``[old[i] for i in order]`` (repeats become copies), fixing references."""
    n = len(pdf.pages)
    bad = [i + 1 for i in order if not 0 <= i < n]
    if bad:
        raise ValueError(f"page(s) {bad} out of range 1–{n}")
    if not order:
        raise ValueError("the result would have no pages")
    outline = read_outline(pdf) if "/Outlines" in pdf.Root else None
    labels = read_page_labels(pdf)
    originals = list(pdf.pages)
    for _ in range(n):
        del pdf.pages[-1]
    mapping: dict[int, int] = {}
    for new_i, old_i in enumerate(order):
        pdf.pages.append(originals[old_i])  # appending a page already present makes a copy
        if old_i in mapping:
            _copy_annots(pdf, pdf.pages[new_i])
        mapping.setdefault(old_i, new_i)
    _drop_dangling_links(pdf)
    _prune_acroform(pdf)
    if outline is not None:
        write_outline(pdf, remap_outline(outline, mapping))
    if labels is not None:
        write_page_labels(pdf, [labels[i] for i in order])


def _rewrite(src: str | Path, order_fn, tag: str, out, *, password, options, quiet, data=None) -> OpResult:
    """Open *src*, compute the new page order from its page count, write it to a new file, gate it."""
    dst = output_path(src, tag, out=out)
    with open_pdf(src, password) as pdf:
        order = order_fn(len(pdf.pages))
        rearrange(pdf, order)
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password,
                    data={"pages": len(order), **(data or {})}, quiet=quiet)


# ====================================================================== merge


def merge(inputs: Sequence[str | Path], out: str | Path | None = None, *, file_bookmarks: bool = True,
          titles: Sequence[str] | None = None, password: str | None = None, options=None,
          quiet: bool = False) -> OpResult:
    """Concatenate PDFs. Each file's bookmarks are kept, nested under one top-level bookmark per file
    (``file_bookmarks=False``: kept at top level, no per-file entry). Metadata comes from the first file."""
    inputs = [Path(p) for p in inputs]
    if not inputs:
        raise ValueError("merge needs at least one input")
    dst = output_path(inputs[0], "merged", out=out)
    opened: list[pikepdf.Pdf] = []
    try:
        pdf = open_pdf(inputs[0], password)
        opened.append(pdf)
        outline: list[Bookmark] = []
        labels: list = []
        any_labels, offset = False, 0
        for k, path in enumerate(inputs):
            src = pdf if k == 0 else open_pdf(path, password)
            if k:
                opened.append(src)
            n = len(src.pages)
            items = remap_outline(read_outline(src), {i: i + offset for i in range(n)})
            labs = read_page_labels(src)
            any_labels |= labs is not None
            labels += labs or _default_labels(n)
            if k:  # carries form fields, layers and named destinations of the copied pages
                pdf.add_pages_from(src, outlines="ignore")
            if file_bookmarks:
                title = titles[k] if titles and k < len(titles) else path.stem
                outline.append(Bookmark(title, offset, items, None))
            else:
                outline += items
            offset += n
        write_outline(pdf, outline)
        write_page_labels(pdf, labels if any_labels else None)
        save_pdf(pdf, dst)
    finally:
        for p in reversed(opened):
            p.close()
    return finalize(dst, inputs, options=options, password=password,
                    data={"pages": offset, "files": len(inputs)}, quiet=quiet)


# ====================================================================== split


def _ranges_from_spec(spec: str, n: int) -> list[list[int]]:
    """'1-3,4-6,7-' → [[0,1,2],[3,4,5],[6..]]: each comma-separated piece is one output file."""
    parts = [s for s in re.split(r"\s*[,;]\s*", spec.strip()) if s]
    return [parse_pages(s, n) for s in parts]


def _bookmark_groups(pdf: pikepdf.Pdf) -> list[tuple[str, list[int]]]:
    n = len(pdf.pages)
    starts: dict[int, str] = {}
    for b in read_outline(pdf):
        if b.page is not None and b.page not in starts:
            starts[b.page] = b.title
    if not starts:
        raise ValueError("document has no top-level bookmarks to split on")
    cuts = sorted(starts)
    if cuts[0] != 0:  # pages before the first bookmark go with it
        starts[0] = starts.pop(cuts[0])
        cuts[0] = 0
    bounds = cuts + [n]
    return [(starts[a], list(range(a, b))) for a, b in zip(bounds, bounds[1:])]


def _slug(s: str, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:limit].strip("-").lower() or "part"


def _part_path(src: Path, tag: str, out: str | Path | None) -> Path:
    if out is None or Path(out).is_dir():
        return output_path(src, tag, out=out)
    return output_path(Path(out), tag)  # out names a file: parts go next to it as <out>.<tag>.pdf


def split(src: str | Path, out: str | Path | None = None, *, ranges: str | Sequence[Sequence[int]] | None = None,
          every: int | None = None, bookmarks: bool = False, password: str | None = None, options=None,
          quiet: bool = False) -> OpResult:
    """Split into several files: by *ranges* ("1-3,4-6" 1-based, or 0-based index lists), *every* N pages,
    or at top-level *bookmarks*. Returns the first part as ``output``, the rest in ``extra_outputs``."""
    src = Path(src)
    if sum(x is not None and x is not False for x in (ranges, every, bookmarks)) != 1:
        raise ValueError("split needs exactly one of: ranges, every, bookmarks")
    with open_pdf(src, password) as pdf:
        n = len(pdf.pages)
        if bookmarks:
            groups = [(f"part{k + 1:02d}-{_slug(t)}", g) for k, (t, g) in enumerate(_bookmark_groups(pdf))]
        else:
            if every is not None:
                if every < 1:
                    raise ValueError("every must be ≥ 1")
                idx = [list(range(a, min(a + every, n))) for a in range(0, n, every)]
            elif isinstance(ranges, str):
                idx = _ranges_from_spec(ranges, n)
            else:
                idx = [list(g) for g in ranges]
            groups = [(f"part{k + 1:02d}", g) for k, g in enumerate(idx)]
    results = []
    for tag, pages in groups:
        part = _part_path(src, tag, out)
        with open_pdf(src, password) as pdf:
            rearrange(pdf, pages)
            save_pdf(pdf, part)
        results.append(finalize(part, src, options=options, password=password, quiet=quiet))
    failed = next((r for r in results if not r.ok), results[0])
    return OpResult(results[0].output, failed.gate, [r.output for r in results[1:]],
                    {"parts": [{"path": str(r.output), "pages": [p + 1 for p in g], "ok": r.ok}
                               for r, (_, g) in zip(results, groups)]})


# ====================================================================== insert / delete / reorder / extract


def insert(target: str | Path, other: str | Path, out: str | Path | None = None, *, at: int | None = None,
           pages: Sequence[int] | None = None, password: str | None = None, other_password: str | None = None,
           options=None, quiet: bool = False) -> OpResult:
    """Insert pages of *other* (all, or 0-based *pages*) into *target* before index *at* (default: append)."""
    dst = output_path(target, "inserted", out=out)
    with open_pdf(target, password) as pdf, open_pdf(other, other_password or password) as b:
        n, nb = len(pdf.pages), len(b.pages)
        at = n if at is None else at
        if not 0 <= at <= n:
            raise ValueError(f"insert position {at} out of range 0–{n}")
        sel = list(range(nb)) if pages is None else list(pages)
        if any(not 0 <= i < nb for i in sel):
            raise ValueError(f"page(s) out of range 1–{nb} in the inserted file")
        a_items, b_items = read_outline(pdf), read_outline(b)
        a_labs, b_labs = read_page_labels(pdf), read_page_labels(b)
        pdf.add_pages_from(b, sel, outlines="ignore")  # appended, with their forms / layers / dests
        added = [pdf.pages[n + k] for k in range(len(sel))]
        for _ in sel:
            del pdf.pages[-1]
        for k, pg in enumerate(added):
            pdf.pages.insert(at + k, pg)  # a removed page re-inserted keeps its identity
        shift = {i: (i if i < at else i + len(sel)) for i in range(n)}
        placed = {old: at + k for k, old in enumerate(sel)}
        items = remap_outline(a_items, shift) + remap_outline(b_items, placed)
        if items:
            write_outline(pdf, sorted(items, key=lambda x: (x.page is None, x.page or 0)))
        if a_labs is not None or b_labs is not None:
            la, lb = a_labs or _default_labels(n), b_labs or _default_labels(nb)
            write_page_labels(pdf, la[:at] + [lb[i] for i in sel] + la[at:])
        _drop_dangling_links(pdf)
        save_pdf(pdf, dst)
    return finalize(dst, [Path(target), Path(other)], options=options, password=password,
                    data={"inserted": len(sel), "at": at}, quiet=quiet)


def delete_pages(src: str | Path, pages: Iterable[int], out: str | Path | None = None, *,
                 password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    drop = set(pages)

    def order(n: int) -> list[int]:
        bad = [i + 1 for i in drop if not 0 <= i < n]
        if bad:
            raise ValueError(f"page(s) {sorted(bad)} out of range 1–{n}")
        if len(drop) >= n:
            raise ValueError("cannot delete every page")
        return [i for i in range(n) if i not in drop]

    return _rewrite(src, order, "deleted", out, password=password, options=options, quiet=quiet,
                    data={"deleted": [i + 1 for i in sorted(drop)]})


def reorder(src: str | Path, order: Sequence[int], out: str | Path | None = None, *,
            password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Put pages in *order* (0-based; every page exactly once — use extract_pages for a subset)."""
    order = list(order)

    def check(n: int) -> list[int]:
        if sorted(order) != list(range(n)):
            missing = sorted(set(range(n)) - set(order))
            raise ValueError(f"order must list every page once (missing {[i + 1 for i in missing]}, "
                             f"{len(order)} given for {n} pages); use extract-pages for a subset")
        return order

    return _rewrite(src, check, "reordered", out, password=password, options=options, quiet=quiet)


def extract_pages(src: str | Path, pages: Sequence[int], out: str | Path | None = None, *,
                  password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Copy the selected pages (0-based, in the given order) to a new file."""
    pages = list(pages)
    return _rewrite(src, lambda n: pages, "extract", out, password=password, options=options, quiet=quiet)


def duplicate_pages(src: str | Path, pages: Iterable[int], out: str | Path | None = None, *, copies: int = 1,
                    password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Repeat each selected page *copies* times, right after itself."""
    if copies < 1:
        raise ValueError("copies must be ≥ 1")
    sel = set(pages)

    def order(n: int) -> list[int]:
        return [j for i in range(n) for j in [i] * (1 + (copies if i in sel else 0))]

    return _rewrite(src, order, "duplicated", out, password=password, options=options, quiet=quiet)


# ====================================================================== rotate / crop


def rotate(src: str | Path, angle: int, pages: Iterable[int] | None = None, out: str | Path | None = None, *,
           password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Rotate selected pages (default all) by *angle* (multiple of 90, relative to the current rotation)."""
    if angle % 90:
        raise ValueError("rotation must be a multiple of 90 degrees")
    dst = output_path(src, "rotated", out=out)
    with open_pdf(src, password) as pdf:
        idx = list(range(len(pdf.pages))) if pages is None else sorted(set(pages))
        for i in idx:
            pdf.pages[i].rotate(angle, relative=True)
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password, data={"pages": [i + 1 for i in idx]},
                    quiet=quiet)


_UNITS = {"pt": 1.0, "in": 72.0, "mm": 72 / 25.4, "cm": 72 / 2.54, "": 1.0}


def parse_length(s: str) -> float:
    """'36', '36pt', '0.5in', '12.7mm', '1.27cm' → points."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?|-?\.\d+)\s*(pt|in|mm|cm)?\s*", s.lower())
    if not m:
        raise ValueError(f"bad length {s!r} (use e.g. 36, 36pt, 0.5in, 12mm)")
    return float(m.group(1)) * _UNITS[m.group(2) or ""]


def parse_margins(spec: str) -> tuple[float, float, float, float]:
    """CSS order: 'a' | 'v,h' | 't,r,b,l' → (top, right, bottom, left) points."""
    v = [parse_length(x) for x in re.split(r"[,\s]+", spec.strip()) if x]
    if len(v) == 1:
        return (v[0],) * 4  # type: ignore[return-value]
    if len(v) == 2:
        return (v[0], v[1], v[0], v[1])
    if len(v) == 4:
        return tuple(v)  # type: ignore[return-value]
    raise ValueError("margins: give 1, 2 (vertical,horizontal) or 4 (top,right,bottom,left) values")


def _visual_rect_to_user(page: pikepdf.Page, x0: float, y0: float, x1: float, y1: float) -> list[float]:
    a, b, c, d, e, f = visual_to_user_matrix(page)
    pts = [(a * u + c * v + e, b * u + d * v + f) for u, v in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _crop_box(page: pikepdf.Page, margins, box) -> list[float]:
    vw, vh = visual_size(page)
    if box is not None:
        x0, y0, x1, y1 = box
    else:
        t, r, b, l = (margins,) * 4 if isinstance(margins, (int, float)) else margins
        x0, y0, x1, y1 = l, b, vw - r, vh - t
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise ValueError("crop leaves an empty page")
    ub = _visual_rect_to_user(page, x0, y0, x1, y1)
    mb = [float(v) for v in page.mediabox]
    mb = [min(mb[0], mb[2]), min(mb[1], mb[3]), max(mb[0], mb[2]), max(mb[1], mb[3])]
    clip = [max(ub[0], mb[0]), max(ub[1], mb[1]), min(ub[2], mb[2]), min(ub[3], mb[3])]
    if clip[2] - clip[0] < 1 or clip[3] - clip[1] < 1:
        raise ValueError("crop box lies outside the page")
    return [round(v, 4) for v in clip]


def crop(src: str | Path, out: str | Path | None = None, *,
         margins: float | Sequence[float] | None = None, box: Sequence[float] | None = None,
         pages: Iterable[int] | None = None, trim: bool = False, password: str | None = None, options=None,
         quiet: bool = False) -> OpResult:
    """Set the CropBox (and TrimBox with *trim*), in points, in what-you-see orientation.

    *margins*: one value or (top, right, bottom, left) cut from the currently visible box.
    *box*: (x0, y0, x1, y1) of the new visible area, origin at the visible bottom-left.
    """
    if (margins is None) == (box is None):
        raise ValueError("crop needs exactly one of: margins, box")
    dst = output_path(src, "cropped", out=out)
    boxes = {}
    with open_pdf(src, password) as pdf:
        idx = list(range(len(pdf.pages))) if pages is None else sorted(set(pages))
        for i in idx:
            pg = pdf.pages[i]
            cb = _crop_box(pg, margins, box)
            pg.obj.CropBox = Array(cb)
            if trim:
                pg.obj.TrimBox = Array(cb)
            boxes[i + 1] = cb
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password, data={"cropbox": boxes}, quiet=quiet)


# ====================================================================== page labels op


def page_labels(src: str | Path, ranges: Sequence[LabelRange | tuple] | None, out: str | Path | None = None, *,
                password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Set /PageLabels from ranges (start 0-based, style, prefix, first number). ``None`` or [] removes them.

    Example — roman front matter then E-1, E-2…: ``[(0, "r"), (4, "D", "E-", 1)]``.
    """
    rs = [r if isinstance(r, LabelRange) else LabelRange(*r) for r in (ranges or [])]
    rs = [r._replace(style=norm_style(r.style)) for r in rs]
    dst = output_path(src, "labeled", out=out)
    with open_pdf(src, password) as pdf:
        if rs:
            write_label_ranges(pdf, rs)
        elif "/PageLabels" in pdf.Root:
            del pdf.Root["/PageLabels"]
        save_pdf(pdf, dst)
        labels = [p.label for p in pdf.pages]
    return finalize(dst, src, options=options, password=password, data={"labels": labels}, quiet=quiet)


def parse_label_spec(spec: str, n: int) -> LabelRange:
    """'PAGES:STYLE[:PREFIX[:START]]' e.g. '1-4:roman', '5-:decimal:E-:1' (pages 1-based; range start counts)."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError(f"bad label range {spec!r} (use PAGES:STYLE[:PREFIX[:START]], e.g. 5-:decimal:E-:1)")
    start = min(parse_pages(parts[0], n))
    first = int(parts[3]) if len(parts) > 3 and parts[3] else 1
    return LabelRange(start, norm_style(parts[1]), parts[2] if len(parts) > 2 else "", first)


# ====================================================================== CLI


def _n(path: str, password: str | None) -> int:
    with open_pdf(path, password) as pdf:
        return len(pdf.pages)


def _kw(ns) -> dict:
    return dict(password=ns.password, options=gate_options_from(ns))


@command("merge", "Merge PDFs in order (each file's bookmarks nested under one per file)", group="organize")
def _cli_merge(p):
    p.add_argument("inputs", nargs="+")
    p.add_argument("--flat-bookmarks", action="store_true", help="no per-file bookmark; keep each file's at top level")
    add_common(p)
    return lambda ns: merge(ns.inputs, ns.out, file_bookmarks=not ns.flat_bookmarks, **_kw(ns))


@command("split", "Split into several files by ranges, every N pages, or top-level bookmarks", group="organize")
def _cli_split(p):
    p.add_argument("pdf")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ranges", help="e.g. '1-3,4-6,7-' (one file per comma-separated range)")
    g.add_argument("--every", type=int, metavar="N", help="one file per N pages")
    g.add_argument("--bookmarks", action="store_true", help="one file per top-level bookmark")
    add_common(p)
    return lambda ns: split(ns.pdf, ns.out, ranges=ns.ranges, every=ns.every, bookmarks=ns.bookmarks, **_kw(ns))


@command("insert", "Insert pages of another PDF into this one", group="organize")
def _cli_insert(p):
    p.add_argument("pdf")
    p.add_argument("other", help="PDF whose pages are inserted")
    p.add_argument("--after", type=int, help="insert after this page (1-based; 0 = at the front; default: end)")
    p.add_argument("--pages", help="pages of OTHER to insert (default all)")
    p.add_argument("--other-password")
    add_common(p)

    def run(ns):
        sel = parse_pages(ns.pages, _n(ns.other, ns.other_password or ns.password)) if ns.pages else None
        return insert(ns.pdf, ns.other, ns.out, at=ns.after, pages=sel, other_password=ns.other_password, **_kw(ns))

    return run


@command("delete-pages", "Delete pages", group="organize")
def _cli_delete(p):
    p.add_argument("pdf")
    p.add_argument("pages", help="e.g. 2,5-7,last")
    add_common(p)
    return lambda ns: delete_pages(ns.pdf, parse_pages(ns.pages, _n(ns.pdf, ns.password)), ns.out, **_kw(ns))


@command("reorder", "Reorder pages (every page once, e.g. '3,1,2,4-' or 'reverse')", group="organize")
def _cli_reorder(p):
    p.add_argument("pdf")
    p.add_argument("order")
    add_common(p)

    def run(ns):
        n = _n(ns.pdf, ns.password)
        order = list(range(n - 1, -1, -1)) if ns.order.strip().lower() == "reverse" else parse_pages(ns.order, n)
        return reorder(ns.pdf, order, ns.out, **_kw(ns))

    return run


@command("extract-pages", "Copy selected pages to a new PDF", group="organize")
def _cli_extract(p):
    p.add_argument("pdf")
    p.add_argument("pages")
    add_common(p)
    return lambda ns: extract_pages(ns.pdf, parse_pages(ns.pages, _n(ns.pdf, ns.password)), ns.out, **_kw(ns))


@command("duplicate-pages", "Repeat selected pages right after themselves", group="organize")
def _cli_duplicate(p):
    p.add_argument("pdf")
    p.add_argument("pages")
    p.add_argument("--copies", type=int, default=1)
    add_common(p)
    return lambda ns: duplicate_pages(ns.pdf, parse_pages(ns.pages, _n(ns.pdf, ns.password)), ns.out,
                                      copies=ns.copies, **_kw(ns))


@command("rotate", "Rotate pages by 90/180/270 (relative)", group="organize")
def _cli_rotate(p):
    p.add_argument("pdf")
    p.add_argument("angle", type=int, help="90, 180, 270 or -90")
    p.add_argument("--pages", help="default all")
    add_common(p)

    def run(ns):
        sel = parse_pages(ns.pages, _n(ns.pdf, ns.password)) if ns.pages else None
        return rotate(ns.pdf, ns.angle, sel, ns.out, **_kw(ns))

    return run


@command("crop", "Crop pages by margins or to a box (sets CropBox, optionally TrimBox)", group="organize")
def _cli_crop(p):
    p.add_argument("pdf")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--margins", help="'0.5in' | 'v,h' | 'top,right,bottom,left' (pt, in, mm, cm)")
    g.add_argument("--box", help="x0,y0,x1,y1 of the visible area, from its bottom-left (pt, in, mm, cm)")
    p.add_argument("--pages", help="default all")
    p.add_argument("--trim", action="store_true", help="also set TrimBox")
    add_common(p)

    def run(ns):
        sel = parse_pages(ns.pages, _n(ns.pdf, ns.password)) if ns.pages else None
        box = None
        if ns.box:
            box = [parse_length(x) for x in re.split(r"[,\s]+", ns.box.strip()) if x]
            if len(box) != 4:
                raise ValueError("--box needs x0,y0,x1,y1")
        margins = parse_margins(ns.margins) if ns.margins else None
        return crop(ns.pdf, ns.out, margins=margins, box=box, pages=sel, trim=ns.trim, **_kw(ns))

    return run


@command("page-labels", "Set page labels, e.g. --range 1-4:roman --range 5-:decimal:E-:1", group="organize")
def _cli_labels(p):
    p.add_argument("pdf")
    p.add_argument("--range", action="append", default=[], metavar="PAGES:STYLE[:PREFIX[:START]]",
                   help="style: decimal, roman, ROMAN, alpha, ALPHA, none")
    p.add_argument("--clear", action="store_true", help="remove all page labels")
    add_common(p)

    def run(ns):
        if not ns.range and not ns.clear:
            raise ValueError("give at least one --range, or --clear")
        n = _n(ns.pdf, ns.password)
        return page_labels(ns.pdf, [parse_label_spec(s, n) for s in ns.range], ns.out, **_kw(ns))

    return run
