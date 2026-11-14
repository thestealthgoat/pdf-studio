"""Navigation: bookmarks (list / set / auto-build from headings), links, metadata, attachments.

Page numbers: library functions and :class:`Bookmark` use 0-based page indexes. Outline files
(JSON/YAML), the dicts returned in ``OpResult.data`` and the CLI use 1-based page numbers.

Rectangles for links use :class:`pdfcore.types.Rect` — points, top-left origin, unrotated page
space — the same space :func:`pdfcore.text.search` reports hits in.
"""

from __future__ import annotations

import json
import mimetypes
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.result import OpResult
from pdfcore.types import Rect, Word

# ====================================================================== open / save helpers


def open_pdf(src: str | Path, password: str | None = None) -> pikepdf.Pdf:
    return pikepdf.open(str(src), password=password or "")


def save_pdf(pdf: pikepdf.Pdf, out: Path) -> None:
    """Save, keeping the input's encryption (if any) so an op never silently decrypts a file."""
    pdf.save(str(out), encryption=True if pdf.is_encrypted else None)


# ====================================================================== outline model


@dataclass
class Bookmark:
    """One outline entry. ``page`` is 0-based (None = no page target); ``top`` is PDF user-space y."""

    title: str
    page: int | None
    children: list["Bookmark"] = field(default_factory=list)
    top: float | None = None
    left: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"title": self.title, "page": None if self.page is None else self.page + 1}
        if self.top is not None:
            d["top"] = round(self.top, 2)
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Bookmark":
        page = d.get("page")
        kids = d.get("children") or d.get("items") or []
        return cls(
            str(d["title"]),
            None if page is None else int(page) - 1,
            [cls.from_dict(k) for k in kids],
            None if d.get("top") is None else float(d["top"]),
        )


def named_dests(pdf: pikepdf.Pdf) -> dict[str, Any]:
    """All named destinations (old-style /Dests dict and the /Names /Dests name tree)."""
    out: dict[str, Any] = {}
    old = pdf.Root.get("/Dests")
    if isinstance(old, Dictionary):
        for k, v in old.items():
            out[str(k).lstrip("/")] = v
    names = pdf.Root.get("/Names")
    if isinstance(names, Dictionary) and "/Dests" in names:
        for k, v in pikepdf.NameTree(names.Dests).items():
            out[str(k)] = v
    return out


def page_index_map(pdf: pikepdf.Pdf) -> dict[tuple[int, int], int]:
    return {p.obj.objgen: i for i, p in enumerate(pdf.pages)}


def resolve_dest(pdf: pikepdf.Pdf, dest: Any, index: dict | None = None, names: dict | None = None,
                 depth: int = 0) -> tuple[int | None, float | None, float | None]:
    """Destination (array, name, string, /D dict) → (page index, left, top)."""
    if dest is None or depth > 8:
        return None, None, None
    index = index if index is not None else page_index_map(pdf)
    if isinstance(dest, (Name, String)):
        names = names if names is not None else named_dests(pdf)
        return resolve_dest(pdf, names.get(str(dest).lstrip("/")), index, names, depth + 1)
    if isinstance(dest, Dictionary):
        if "/D" in dest:
            return resolve_dest(pdf, dest.D, index, names, depth + 1)
        if str(dest.get("/S", "")) == "/GoTo":
            return resolve_dest(pdf, dest.get("/D"), index, names, depth + 1)
        return None, None, None
    if isinstance(dest, Array) and len(dest) > 0:
        tgt = dest[0]
        if isinstance(tgt, int):
            page = int(tgt) if 0 <= int(tgt) < len(pdf.pages) else None
        else:
            try:
                page = index.get(tgt.objgen)
            except Exception:
                page = None
        left = top = None
        if len(dest) >= 4 and str(dest[1]) == "/XYZ":
            left = _num(dest[2])
            top = _num(dest[3])
        return page, left, top
    return None, None, None


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_outline(pdf: pikepdf.Pdf) -> list[Bookmark]:
    """The document outline as a Bookmark tree (pages resolved to 0-based indexes)."""
    index, names = page_index_map(pdf), named_dests(pdf)

    def conv(item: pikepdf.OutlineItem) -> Bookmark:
        dest = item.destination if item.destination is not None else item.action
        page, left, top = resolve_dest(pdf, dest, index, names)
        return Bookmark(item.title, page, [conv(c) for c in item.children], top, left)

    with pdf.open_outline() as ol:
        return [conv(i) for i in ol.root]


def write_outline(pdf: pikepdf.Pdf, items: Sequence[Bookmark]) -> None:
    """Replace the document outline with *items*."""
    n = len(pdf.pages)

    def build(b: Bookmark) -> pikepdf.OutlineItem:
        if b.page is not None and not 0 <= b.page < n:
            raise ValueError(f"bookmark {b.title!r}: page {b.page + 1} out of range 1–{n}")
        if b.page is None:
            oi = pikepdf.OutlineItem(b.title)
        elif b.top is not None:
            oi = pikepdf.OutlineItem(b.title, b.page, "XYZ", left=b.left or 0, top=b.top)
        else:
            oi = pikepdf.OutlineItem(b.title, b.page, "Fit")
        oi.children.extend(build(c) for c in b.children)
        return oi

    built = [build(b) for b in items]
    with pdf.open_outline() as ol:
        ol.root.clear()
        ol.root.extend(built)
    if not built and "/Outlines" in pdf.Root:
        del pdf.Root["/Outlines"]


def remap_outline(items: Sequence[Bookmark], mapping: dict[int, int]) -> list[Bookmark]:
    """Move bookmarks to new page indexes; entries whose page vanished keep their children (or are dropped)."""
    out: list[Bookmark] = []
    for b in items:
        kids = remap_outline(b.children, mapping)
        if b.page is not None and b.page in mapping:
            out.append(Bookmark(b.title, mapping[b.page], kids, b.top, b.left))
        elif kids:
            out.append(Bookmark(b.title, kids[0].page, kids, kids[0].top, kids[0].left))
        elif b.page is None:
            out.append(Bookmark(b.title, None, [], None, None))
    return out


def flatten(items: Iterable[Bookmark], depth: int = 0) -> list[tuple[int, Bookmark]]:
    out: list[tuple[int, Bookmark]] = []
    for b in items:
        out.append((depth, b))
        out += flatten(b.children, depth + 1)
    return out


def outline_text(items: Sequence[Bookmark]) -> str:
    return "\n".join(f"{'  ' * d}{b.title}  (p.{'-' if b.page is None else b.page + 1})" for d, b in flatten(items))


def load_outline_file(path: str | Path) -> list[Bookmark]:
    """Read a JSON or YAML outline: a list of {title, page (1-based), children} (or {"bookmarks": [...]})."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("bookmarks") or data.get("outline") or []
    return [Bookmark.from_dict(d) for d in data]


# ====================================================================== bookmarks ops


def read_bookmarks(src: str | Path, *, password: str | None = None) -> list[Bookmark]:
    with open_pdf(src, password) as pdf:
        return read_outline(pdf)


def list_bookmarks(src: str | Path, *, as_json: bool = False, password: str | None = None,
                   quiet: bool = False) -> OpResult:
    items = read_bookmarks(src, password=password)
    dicts = [b.to_dict() for b in items]
    text = json.dumps(dicts, indent=2, ensure_ascii=False) if as_json else outline_text(items)
    if not quiet:
        print(text if items or as_json else "(no bookmarks)")
    return OpResult(None, data={"bookmarks": dicts, "text": text})


def set_bookmarks(src: str | Path, outline: str | Path | Sequence[Bookmark | dict], out: str | Path | None = None,
                  *, password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Replace the outline from a JSON/YAML file, Bookmark list, or list of dicts (1-based pages)."""
    if isinstance(outline, (str, Path)):
        items = load_outline_file(outline)
    else:
        items = [b if isinstance(b, Bookmark) else Bookmark.from_dict(b) for b in outline]
    dst = output_path(src, "bookmarks", out=out)
    with open_pdf(src, password) as pdf:
        write_outline(pdf, items)
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password,
                    data={"bookmarks": [b.to_dict() for b in items]}, quiet=quiet)


# ---------------------------------------------------------------------- heading detection

_BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.I)


@dataclass
class Heading:
    text: str
    page: int
    size: float
    bold: bool
    top: float  # pdfplumber top (top-left origin)
    bottom: float
    x0: float
    level: int = 0
    big: bool = True  # larger than body text (False: bold at body size)


def _is_bold(font: str) -> bool:
    return bool(_BOLD.search(font.split("+")[-1]))


def _page_words(page, pi: int) -> list[Word]:
    raw = page.extract_words(keep_blank_chars=False, use_text_flow=False, extra_attrs=["fontname", "size"])
    return [Word(w["text"], Rect(w["x0"], w["top"], w["x1"], w["bottom"]), pi, w.get("fontname", ""),
                 float(w.get("size", 0))) for w in raw]


def _body_size(pages_words: list[list[Word]]) -> float:
    """Most common text size, counted on paragraph-like lines (≥ 6 words) so that short documents whose
    headings outweigh their body text don't mistake a heading size for the body size."""
    cnt: Counter = Counter()
    fallback: Counter = Counter()
    for ws in pages_words:
        lines: dict[tuple[float, float], list[Word]] = {}
        for w in ws:
            fallback[round(w.size * 2) / 2] += len(w.text)
            lines.setdefault((round(w.rect.bottom), round(w.size * 2) / 2), []).append(w)
        for (_, size), lw in lines.items():
            if len(lw) >= 6:
                cnt[size] += sum(len(w.text) for w in lw)
    use = cnt if cnt else fallback
    return use.most_common(1)[0][0] if use else 0.0


def _line_candidate(line, body: float, min_ratio: float) -> Heading | None:
    ws = line.words
    sizes = [w.size for w in ws]
    if max(sizes) - min(sizes) > 0.6:
        return None
    text = " ".join(w.text for w in ws).strip()
    if not 2 <= len(text) <= 150 or not re.search(r"[A-Za-z]", text):
        return None
    size = sum(sizes) / len(sizes)
    bold = all(_is_bold(w.font) for w in ws)
    big = size >= body * min_ratio
    bold_body = bold and size >= body - 0.1 and len(ws) <= 12 and not text.endswith((".", ",", ";", ":"))
    if not (big or bold_body):
        return None
    r = line.rect
    return Heading(text, ws[0].page, round(size * 2) / 2, bold, r.top, r.bottom, r.x0, big=big)


def _drop_table_rows(cands: list[Heading]) -> list[Heading]:
    """Bold body-size words side by side on one row are table column headers, not headings."""
    rows = Counter((h.page, round(h.top)) for h in cands if not h.big)
    return [h for h in cands if h.big or rows[(h.page, round(h.top))] < 3]


def _merge_wrapped(cands: list[Heading]) -> list[Heading]:
    """Join a heading that wraps onto a second line with the same style."""
    out: list[Heading] = []
    for h in cands:
        p = out[-1] if out else None
        if (p and p.page == h.page and (p.size, p.bold) == (h.size, h.bold)
                and 0 <= h.top - p.bottom < 0.6 * h.size and abs(h.x0 - p.x0) < 2 * h.size):
            p.text, p.bottom = f"{p.text} {h.text}", h.bottom
        else:
            out.append(h)
    return out


def _drop_running(cands: list[Heading], n_pages: int) -> list[Heading]:
    """Remove text that repeats on many pages (running headers/footers), ignoring digits."""
    if n_pages < 2:
        return cands
    key = lambda h: re.sub(r"\d+", "#", h.text.lower()).strip()  # noqa: E731
    pages_per: dict[str, set[int]] = {}
    for h in cands:
        pages_per.setdefault(key(h), set()).add(h.page)
    limit = max(2, -(-n_pages // 2))
    return [h for h in cands if len(pages_per[key(h)]) < limit]


def _assign_levels(cands: list[Heading], max_levels: int) -> list[Heading]:
    styles = sorted({(h.size, h.bold) for h in cands}, key=lambda s: (-s[0], not s[1]))
    rank = {s: i for i, s in enumerate(styles)}
    for h in cands:
        h.level = rank[(h.size, h.bold)]
    top = [h for h in cands if h.level == 0]
    # A lone biggest heading on the first page is the document title: keep it, but don't nest everything under it.
    if len(styles) > 1 and len(top) == 1 and top[0].page == 0:
        for h in cands:
            h.level = max(0, h.level - 1)
    return [h for h in cands if h.level < max_levels]


def detect_headings(src: str | Path, *, max_levels: int = 3, min_ratio: float = 1.15,
                    password: str | None = None) -> list[Heading]:
    """Headings found by font size / weight: larger than body text (or bold body-size lines), ranked by size."""
    import pdfplumber

    from pdfcore.text import group_lines

    with pdfplumber.open(str(src), password=password or "") as doc:
        pages_words = [_page_words(p, i) for i, p in enumerate(doc.pages)]
    body = _body_size(pages_words)
    cands: list[Heading] = []
    for ws in pages_words:
        if not ws:
            continue
        lines = sorted(group_lines(ws), key=lambda ln: (ln.rect.top, ln.rect.x0))
        found = [h for h in (_line_candidate(ln, body, min_ratio) for ln in lines) if h]
        cands += _merge_wrapped(_drop_table_rows(found))
    cands = _drop_running(cands, len(pages_words))
    return _assign_levels(cands, max_levels) if cands else []


def headings_to_outline(heads: Sequence[Heading], page_tops: Sequence[float]) -> list[Bookmark]:
    """Nest headings by level; destinations are /XYZ at the heading's top."""
    roots: list[Bookmark] = []
    stack: list[tuple[int, Bookmark]] = []
    for h in heads:
        y = page_tops[h.page] - h.top + 2
        b = Bookmark(h.text, h.page, [], round(y, 2), 0)
        while stack and stack[-1][0] >= h.level:
            stack.pop()
        (stack[-1][1].children if stack else roots).append(b)
        stack.append((h.level, b))
    return roots


def auto_bookmarks(src: str | Path, out: str | Path | None = None, *, max_levels: int = 3, min_ratio: float = 1.15,
                   password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Build bookmarks from detected headings and replace the outline."""
    heads = detect_headings(src, max_levels=max_levels, min_ratio=min_ratio, password=password)
    if not heads:
        raise ValueError("no headings found (text larger or bolder than body text); set bookmarks from a file instead")
    dst = output_path(src, "bookmarks", out=out)
    with open_pdf(src, password) as pdf:
        tops = [float(p.mediabox[3]) for p in pdf.pages]
        items = headings_to_outline(heads, tops)
        write_outline(pdf, items)
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password,
                    data={"bookmarks": [b.to_dict() for b in items]}, quiet=quiet)


# ====================================================================== links


def _annots(pdf: pikepdf.Pdf, page: pikepdf.Page) -> Array:
    if "/Annots" not in page.obj or not isinstance(page.obj.Annots, Array):
        page.obj.Annots = pdf.make_indirect(Array())
    return page.obj.Annots


def _pdf_rect(page: pikepdf.Page, r: Rect) -> Array:
    mb = [float(v) for v in page.mediabox]
    x0, y_top = min(mb[0], mb[2]), max(mb[1], mb[3])
    return Array([x0 + r.x0, y_top - r.bottom, x0 + r.x1, y_top - r.top])


def _link_dict(pdf: pikepdf.Pdf, page: pikepdf.Page, rect: Rect, *, to_page: int | None, dest_name: str | None,
               uri: str | None, top: float | None) -> Dictionary:
    d = Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=_pdf_rect(page, rect), Border=Array([0, 0, 0]),
                   P=page.obj)
    if uri is not None:
        d.A = Dictionary(S=Name.URI, URI=String(uri))
    elif dest_name is not None:
        if dest_name not in named_dests(pdf):
            raise ValueError(f"named destination {dest_name!r} not found")
        d.Dest = String(dest_name)
    elif to_page is not None:
        if not 0 <= to_page < len(pdf.pages):
            raise ValueError(f"target page {to_page + 1} out of range 1–{len(pdf.pages)}")
        tgt = pdf.pages[to_page].obj
        d.Dest = Array([tgt, Name.XYZ, None, top, None]) if top is not None else Array([tgt, Name.Fit])
    else:
        raise ValueError("a link needs a target: to_page, dest_name or uri")
    return d


def add_link(src: str | Path, page: int, rect: Rect | Sequence[float], out: str | Path | None = None, *,
             to_page: int | None = None, dest_name: str | None = None, uri: str | None = None,
             top: float | None = None, password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Add a link annotation on *page* (0-based) over *rect* → page *to_page*, named dest, or external URI."""
    r = rect if isinstance(rect, Rect) else Rect(*map(float, rect))
    dst = output_path(src, "linked", out=out)
    with open_pdf(src, password) as pdf:
        if not 0 <= page < len(pdf.pages):
            raise ValueError(f"page {page + 1} out of range 1–{len(pdf.pages)}")
        pg = pdf.pages[page]
        _annots(pdf, pg).append(pdf.make_indirect(
            _link_dict(pdf, pg, r, to_page=to_page, dest_name=dest_name, uri=uri, top=top)))
        save_pdf(pdf, dst)
    target = uri or dest_name or (None if to_page is None else to_page + 1)
    return finalize(dst, src, options=options, password=password, data={"target": target}, quiet=quiet)


def add_named_dest(src: str | Path, name: str, page: int, out: str | Path | None = None, *, top: float | None = None,
                   password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Create (or replace) a named destination pointing at *page* (0-based)."""
    dst = output_path(src, "dest", out=out)
    with open_pdf(src, password) as pdf:
        if not 0 <= page < len(pdf.pages):
            raise ValueError(f"page {page + 1} out of range 1–{len(pdf.pages)}")
        if "/Names" not in pdf.Root:
            pdf.Root.Names = pdf.make_indirect(Dictionary())
        names = pdf.Root.Names
        tree = pikepdf.NameTree(names.Dests) if "/Dests" in names else pikepdf.NameTree.new(pdf)
        names.Dests = tree.obj
        tgt = pdf.pages[page].obj
        tree[name] = Array([tgt, Name.XYZ, None, top, None]) if top is not None else Array([tgt, Name.Fit])
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password, data={"name": name, "page": page + 1}, quiet=quiet)


URL_RX = r"(?:https?://|www\.)[^\s<>()\"']*[^\s<>()\"'.,;:!?]"
EMAIL_RX = r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"


def _existing_link_rects(page: pikepdf.Page) -> list[Rect]:
    out = []
    mb = [float(v) for v in page.mediabox]
    for a in page.obj.get("/Annots", []) or []:
        if a.get("/Subtype") == Name.Link and "/Rect" in a:
            llx, lly, urx, ury = (float(v) for v in a.Rect)
            out.append(Rect.from_pdf(llx - mb[0], lly - mb[1], urx - mb[0], ury - mb[1], mb[3] - mb[1]))
    return out


def find_link_targets(src: str | Path, *, urls: bool = True, emails: bool = True, pages: Iterable[int] | None = None,
                      password: str | None = None) -> list[tuple[int, Rect, str]]:
    """(page, rect, uri) for every bare URL / e-mail address in the text."""
    from pdfcore.text import search

    found: list[tuple[int, Rect, str]] = []
    pages = list(pages) if pages is not None else None
    if urls:
        for h in search(src, URL_RX, regex=True, pages=pages, password=password):
            uri = h.text if h.text.lower().startswith("http") else f"https://{h.text}"
            found += [(h.page, r, uri) for r in h.rects]
    if emails:
        for h in search(src, EMAIL_RX, regex=True, pages=pages, password=password):
            for r in h.rects:
                if not any(p == h.page and q.intersects(r) for p, q, _ in found):
                    found.append((h.page, r, f"mailto:{h.text}"))
    return found


def autolink(src: str | Path, out: str | Path | None = None, *, urls: bool = True, emails: bool = True,
             pages: Iterable[int] | None = None, password: str | None = None, options=None,
             quiet: bool = False) -> OpResult:
    """Turn bare URLs and e-mail addresses into link annotations (skips text that is already linked)."""
    targets = find_link_targets(src, urls=urls, emails=emails, pages=pages, password=password)
    dst = output_path(src, "linked", out=out)
    added: list[dict] = []
    with open_pdf(src, password) as pdf:
        existing = {i: _existing_link_rects(pdf.pages[i]) for i in {t[0] for t in targets}}
        for pi, r, uri in targets:
            if any(e.overlap_fraction(r) > 0.5 for e in existing[pi]):
                continue
            pg = pdf.pages[pi]
            _annots(pdf, pg).append(pdf.make_indirect(
                _link_dict(pdf, pg, r.expand(1), to_page=None, dest_name=None, uri=uri, top=None)))
            existing[pi].append(r)
            added.append({"page": pi + 1, "uri": uri})
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password, data={"links": added}, quiet=quiet)


def list_links(src: str | Path, *, password: str | None = None) -> list[dict]:
    """Every link annotation: page (1-based), rect (PDF user space), and uri or target page."""
    out: list[dict] = []
    with open_pdf(src, password) as pdf:
        index, names = page_index_map(pdf), named_dests(pdf)
        for pi, page in enumerate(pdf.pages):
            for a in page.obj.get("/Annots", []) or []:
                if a.get("/Subtype") != Name.Link:
                    continue
                row: dict[str, Any] = {"page": pi + 1, "rect": [round(float(v), 2) for v in a.get("/Rect", [])]}
                act = a.get("/A")
                if isinstance(act, Dictionary) and act.get("/S") == Name.URI:
                    row["uri"] = str(act.URI)
                else:
                    tgt = a.get("/Dest") if "/Dest" in a else act
                    if isinstance(a.get("/Dest"), (String, Name)):
                        row["dest_name"] = str(a.Dest).lstrip("/")
                    p, _, _ = resolve_dest(pdf, tgt, index, names)
                    row["to_page"] = None if p is None else p + 1
                out.append(row)
    return out


# ====================================================================== metadata

_INFO_KEYS = {"title": "/Title", "author": "/Author", "subject": "/Subject", "keywords": "/Keywords",
              "creator": "/Creator", "producer": "/Producer", "creation_date": "/CreationDate", "mod_date": "/ModDate"}


def read_metadata(src: str | Path, *, password: str | None = None) -> dict[str, Any]:
    """Info dict + XMP + /Lang, merged into plain fields (XMP wins where both exist)."""
    with open_pdf(src, password) as pdf:
        info = pdf.docinfo if pdf.trailer.get("/Info") is not None else {}
        meta: dict[str, Any] = {k: str(info[v]) for k, v in _INFO_KEYS.items() if v in info}
        with pdf.open_metadata(set_pikepdf_as_editor=False) as m:
            xmp = {k: m.get(k) for k in m}
        for k, xk in (("title", "dc:title"), ("subject", "dc:description"), ("keywords", "pdf:Keywords")):
            if xmp.get(xk):
                meta[k] = str(xmp[xk])
        if xmp.get("dc:creator"):
            c = xmp["dc:creator"]
            meta["author"] = "; ".join(c) if isinstance(c, (list, tuple)) else str(c)
        lang = pdf.Root.get("/Lang")
        if lang is not None:
            meta["language"] = str(lang)
        meta["xmp"] = {k: (list(v) if isinstance(v, (list, set, tuple)) else str(v)) for k, v in xmp.items()}
    return meta


def get_metadata(src: str | Path, *, as_json: bool = True, password: str | None = None, quiet: bool = False) -> OpResult:
    meta = read_metadata(src, password=password)
    if not quiet:
        if as_json:
            print(json.dumps(meta, indent=2, ensure_ascii=False))
        else:
            for k, v in meta.items():
                if k != "xmp":
                    print(f"{k:14} {v}")
    return OpResult(None, data=meta)


def set_metadata(src: str | Path, out: str | Path | None = None, *, title: str | None = None,
                 author: str | None = None, subject: str | None = None, keywords: str | Sequence[str] | None = None,
                 language: str | None = None, creator: str | None = None, password: str | None = None,
                 options=None, quiet: bool = False) -> OpResult:
    """Set Info + XMP fields and /Lang. ``None`` leaves a field alone; ``""`` clears it."""
    if isinstance(keywords, (list, tuple)):
        keywords = ", ".join(keywords)
    fields = {"title": title, "author": author, "subject": subject, "keywords": keywords, "creator": creator}
    dst = output_path(src, "meta", out=out)
    with open_pdf(src, password) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
            _set_xmp(m, fields, language)
        _set_info(pdf, fields)
        if language is not None:
            if language:
                pdf.Root.Lang = String(language)
            elif "/Lang" in pdf.Root:
                del pdf.Root["/Lang"]
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password, data=read_metadata(dst, password=password),
                    quiet=quiet)


def _set_info(pdf: pikepdf.Pdf, fields: dict[str, str | None]) -> None:
    info = pdf.docinfo
    for k, v in fields.items():
        if v is None:
            continue
        key = Name(_INFO_KEYS[k])
        if v:
            info[key] = String(v)
        elif key in info:
            del info[key]


def _set_xmp(m, fields: dict[str, str | None], language: str | None) -> None:
    xmp_keys = {"title": "dc:title", "subject": "dc:description", "keywords": "pdf:Keywords",
                "creator": "xmp:CreatorTool"}
    for k, xk in xmp_keys.items():
        v = fields[k]
        if v is None:
            continue
        if v:
            m[xk] = v
        elif xk in m:
            del m[xk]
    if fields["keywords"] is not None:
        kws = [s.strip() for s in re.split(r"[,;]", fields["keywords"] or "") if s.strip()]
        if kws:
            m["dc:subject"] = kws
        elif "dc:subject" in m:
            del m["dc:subject"]
    if fields["author"] is not None:
        if fields["author"]:
            m["dc:creator"] = [a.strip() for a in fields["author"].split(";") if a.strip()]
        elif "dc:creator" in m:
            del m["dc:creator"]
    if language:
        try:
            m["dc:language"] = [language]
        except Exception:
            pass


# ====================================================================== attachments


def list_attachments(src: str | Path, *, password: str | None = None, quiet: bool = True) -> OpResult:
    rows: list[dict] = []
    with open_pdf(src, password) as pdf:
        for name, spec in pdf.attachments.items():
            f = spec.get_file()
            rows.append({"name": name, "filename": spec.filename, "size": f.size,
                         "mime": str(f.mime_type or ""), "description": spec.description or ""})
    if not quiet:
        print("\n".join(f"{r['name']}  {r['size']} B  {r['mime']}  {r['description']}" for r in rows)
              or "(no attachments)")
    return OpResult(None, data={"attachments": rows})


def add_attachments(src: str | Path, files: Sequence[str | Path], out: str | Path | None = None, *,
                    description: str = "", password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Embed files in the document's EmbeddedFiles name tree (same name replaces)."""
    dst = output_path(src, "attached", out=out)
    with open_pdf(src, password) as pdf:
        for f in map(Path, files):
            mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            pdf.attachments[f.name] = pikepdf.AttachedFileSpec(
                pdf, f.read_bytes(), filename=f.name, description=description, mime_type=mime)
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password,
                    data={"attachments": [Path(f).name for f in files]}, quiet=quiet)


def extract_attachments(src: str | Path, dest_dir: str | Path, names: Sequence[str] | None = None, *,
                        password: str | None = None, quiet: bool = False) -> OpResult:
    """Write embedded files to *dest_dir* (never overwriting: name-2.ext …). Does not modify the PDF."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with open_pdf(src, password) as pdf:
        for name, spec in pdf.attachments.items():
            if names and name not in names:
                continue
            safe = Path(spec.filename or name).name or "attachment"
            target, n = dest_dir / safe, 2
            while target.exists():
                target = dest_dir / f"{Path(safe).stem}-{n}{Path(safe).suffix}"
                n += 1
            target.write_bytes(spec.get_file().read_bytes())
            written.append(target)
    if names and len(written) < len(names):
        raise KeyError(f"attachment(s) not found: {sorted(set(names) - {p.name for p in written})}")
    if not quiet:
        for p in written:
            print(p)
    return OpResult(None, extra_outputs=written, data={"files": [str(p) for p in written]})


def remove_attachments(src: str | Path, names: Sequence[str] | None = None, out: str | Path | None = None, *,
                       password: str | None = None, options=None, quiet: bool = False) -> OpResult:
    """Remove the named attachments (all when *names* is None)."""
    dst = output_path(src, "detached", out=out)
    with open_pdf(src, password) as pdf:
        present = list(pdf.attachments.keys())
        todo = present if not names else list(names)
        missing = [n for n in todo if n not in present]
        if missing:
            raise KeyError(f"attachment(s) not found: {missing}")
        for n in todo:
            del pdf.attachments[n]
        save_pdf(pdf, dst)
    return finalize(dst, src, options=options, password=password, data={"removed": todo}, quiet=quiet)


# ====================================================================== CLI


def _rect_arg(s: str) -> Rect:
    v = [float(x) for x in re.split(r"[,\s]+", s.strip()) if x]
    if len(v) != 4:
        raise ValueError("--rect needs x0,top,x1,bottom (points, top-left origin)")
    return Rect(*v)


def _n_pages(path: str, password: str | None) -> int:
    with open_pdf(path, password) as pdf:
        return len(pdf.pages)


@command("bookmarks", "List, set (from JSON/YAML) or auto-build bookmarks from headings", group="navigate")
def _cli_bookmarks(p):
    p.add_argument("pdf")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="print the outline (default)")
    g.add_argument("--set", metavar="FILE", help="replace the outline from a JSON/YAML file (1-based pages)")
    g.add_argument("--auto", action="store_true", help="build from headings (font size / weight)")
    g.add_argument("--clear", action="store_true", help="remove all bookmarks")
    p.add_argument("--json", action="store_true", help="with --list: JSON output")
    p.add_argument("--levels", type=int, default=3, help="with --auto: maximum outline depth")
    add_common(p)

    def run(ns):
        kw = dict(password=ns.password, options=gate_options_from(ns))
        if ns.set:
            return set_bookmarks(ns.pdf, ns.set, ns.out, **kw)
        if ns.auto:
            return auto_bookmarks(ns.pdf, ns.out, max_levels=ns.levels, **kw)
        if ns.clear:
            return set_bookmarks(ns.pdf, [], ns.out, **kw)
        return list_bookmarks(ns.pdf, as_json=ns.json, password=ns.password)

    return run


@command("link", "Add a link: rect on a page → another page, a named destination, or a URL", group="navigate")
def _cli_link(p):
    p.add_argument("pdf")
    p.add_argument("--page", required=True, help="page that gets the link (1-based)")
    p.add_argument("--rect", required=True, help="x0,top,x1,bottom in points from the top-left")
    t = p.add_mutually_exclusive_group(required=True)
    t.add_argument("--to-page", help="target page (1-based)")
    t.add_argument("--dest", help="target named destination")
    t.add_argument("--uri", help="external URL (https://…, mailto:…)")
    add_common(p)

    def run(ns):
        n = _n_pages(ns.pdf, ns.password)
        to = parse_pages(ns.to_page, n)[0] if ns.to_page else None
        return add_link(ns.pdf, parse_pages(ns.page, n)[0], _rect_arg(ns.rect), ns.out, to_page=to,
                        dest_name=ns.dest, uri=ns.uri, password=ns.password, options=gate_options_from(ns))

    return run


@command("named-dest", "Create a named destination pointing at a page", group="navigate")
def _cli_named_dest(p):
    p.add_argument("pdf")
    p.add_argument("name")
    p.add_argument("--page", required=True, help="target page (1-based)")
    add_common(p)

    def run(ns):
        page = parse_pages(ns.page, _n_pages(ns.pdf, ns.password))[0]
        return add_named_dest(ns.pdf, ns.name, page, ns.out, password=ns.password, options=gate_options_from(ns))

    return run


@command("autolink", "Turn bare URLs and e-mail addresses in the text into links", group="navigate")
def _cli_autolink(p):
    p.add_argument("pdf")
    p.add_argument("--pages", help="pages to scan (1-based, e.g. 1-3,5); default all")
    p.add_argument("--no-urls", action="store_true")
    p.add_argument("--no-emails", action="store_true")
    add_common(p)

    def run(ns):
        pages = parse_pages(ns.pages, _n_pages(ns.pdf, ns.password)) if ns.pages else None
        return autolink(ns.pdf, ns.out, urls=not ns.no_urls, emails=not ns.no_emails, pages=pages,
                        password=ns.password, options=gate_options_from(ns))

    return run


@command("meta", "Show or set metadata (title, author, subject, keywords, language)", group="navigate")
def _cli_meta(p):
    p.add_argument("pdf")
    for f in ("title", "author", "subject", "keywords", "creator"):
        p.add_argument(f"--{f}")
    p.add_argument("--lang", help="document language, e.g. en-US")
    p.add_argument("--text", action="store_true", help="show as text instead of JSON")
    add_common(p)

    def run(ns):
        vals = dict(title=ns.title, author=ns.author, subject=ns.subject, keywords=ns.keywords, creator=ns.creator)
        if all(v is None for v in vals.values()) and ns.lang is None:
            return get_metadata(ns.pdf, as_json=not ns.text, password=ns.password)
        return set_metadata(ns.pdf, ns.out, language=ns.lang, password=ns.password,
                            options=gate_options_from(ns), **vals)

    return run


@command("attach", "Add, list, extract or remove embedded files", group="navigate")
def _cli_attach(p):
    p.add_argument("pdf")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--add", nargs="+", metavar="FILE")
    g.add_argument("--list", action="store_true", help="(default)")
    g.add_argument("--extract", metavar="DIR", help="write attachments into DIR")
    g.add_argument("--remove", nargs="*", metavar="NAME", help="remove these (no names: all)")
    p.add_argument("--name", action="append", help="with --extract: only these attachments")
    p.add_argument("--description", default="")
    add_common(p)

    def run(ns):
        kw = dict(password=ns.password)
        if ns.add:
            return add_attachments(ns.pdf, ns.add, ns.out, description=ns.description,
                                   options=gate_options_from(ns), **kw)
        if ns.extract:
            return extract_attachments(ns.pdf, ns.extract, ns.name, **kw)
        if ns.remove is not None:
            return remove_attachments(ns.pdf, ns.remove or None, ns.out, options=gate_options_from(ns), **kw)
        return list_attachments(ns.pdf, quiet=False, **kw)

    return run
