"""Individual gate checks. Each returns Findings; nothing here raises on a bad PDF."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pdfcore.types import Rect

INCH = 72.0
# Checks whose failures are never excused by the baseline.
NEVER_INHERITED = {"redaction", "open", "standard", "size"}


@dataclass
class Finding:
    check: str
    message: str
    page: int | None = None
    fix: str = ""
    severity: str = "fail"  # "fail" | "warn"

    def where(self) -> str:
        return f"p.{self.page + 1}: " if self.page is not None else ""


@dataclass
class GateOptions:
    profile: str = "edit"  # edit | typeset | drawing
    margins_in: float | None = None  # typeset: minimum margin on every side
    min_image_dpi: float | None = None  # 300 print, 150 screen
    max_size_mb: float | None = None
    allow_blank_pages: bool = False
    require_metadata: bool | None = None
    min_text_pt: float | None = None  # drawing: minimum text height at print size
    redact_regions: list[tuple[int, Rect]] | None = None
    redact_terms: list[str] | None = None
    redact_allow: list[str] | None = None  # label text printed on redaction boxes
    standard: str | None = None  # e.g. "pdfa-2b", "pdfua-1"
    png_dir: Path | None = None
    png_dpi: float = 60
    max_pages: int = 400  # pages rendered/inspected; very long docs are sampled


@dataclass
class Found:
    findings: list[Finding] = field(default_factory=list)
    pngs: list[Path] = field(default_factory=list)


def run_all(path: Path, o: GateOptions, password: str | None = None) -> Found:
    out = Found()
    f = out.findings
    try:
        import pikepdf

        pdf = pikepdf.open(str(path), password=password or "")
    except Exception as exc:
        f.append(Finding("open", f"cannot open PDF: {exc}", None, "repair the file (pdfcore repair) or re-export it"))
        return out

    with pdf:
        n = len(pdf.pages)
        if n == 0:
            f.append(Finding("open", "document has no pages", None, "check the op's page selection"))
            return out
        f += check_fonts(pdf)
        f += check_links(pdf)
        f += check_metadata(pdf, o)
    f += check_render_and_blank(path, n, o, password, out)
    f += check_layout(path, n, o, password)
    if o.max_size_mb is not None:
        mb = path.stat().st_size / 1e6
        if mb > o.max_size_mb:
            f.append(Finding("size", f"file is {mb:.1f} MB, budget {o.max_size_mb:g} MB", None, "run pdfcore optimize"))
    if o.redact_regions or o.redact_terms:
        f += check_redaction(path, o, password)
    if o.standard:
        f += check_standard(path, o.standard)
    return out


# ---------------------------------------------------------------- fonts
_STD14 = {
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique", "Helvetica", "Helvetica-Bold",
    "Helvetica-Oblique", "Helvetica-BoldOblique", "Times-Roman", "Times-Bold", "Times-Italic",
    "Times-BoldItalic", "Symbol", "ZapfDingbats",
}


def _iter_fonts(pdf):
    """Yield (page_index, font_name, font_dict) for every font a page can use, including form XObjects."""
    import pikepdf

    seen: set = set()

    def used_names(obj) -> tuple[set[str] | None, set[str] | None]:
        """Font and XObject resource names actually referenced by obj's content (None = unknown → all)."""
        try:
            fonts, xobjs = set(), set()
            cur, stack = None, []
            for operands, op in pikepdf.parse_content_stream(obj):
                o = str(op)
                if o == "Tf" and operands:
                    cur = str(operands[0])
                elif o == "q":
                    stack.append(cur)
                elif o == "Q":
                    cur = stack.pop() if stack else cur
                elif o in ("Tj", "TJ", "'", '"') and cur:
                    arg = operands[-1] if operands else None
                    nonempty = True
                    if isinstance(arg, pikepdf.String):
                        nonempty = len(bytes(arg)) > 0
                    elif isinstance(arg, pikepdf.Array):
                        nonempty = any(isinstance(a, pikepdf.String) and len(bytes(a)) for a in arg)
                    if nonempty:
                        fonts.add(cur)
                elif o == "Do" and operands:
                    xobjs.add(str(operands[0]))
            return fonts, xobjs
        except Exception:
            return None, None

    def walk(obj, res, pi, depth=0):
        if res is None or depth > 8:
            return
        ufonts, uxobjs = used_names(obj)
        fonts = res.get("/Font")
        if isinstance(fonts, pikepdf.Dictionary):
            for name, fd in fonts.items():
                if ufonts is not None and str(name) not in ufonts:
                    continue
                try:
                    og = fd.objgen
                except Exception:
                    og = None
                if og and og != (0, 0) and og in seen:
                    continue
                if og:
                    seen.add(og)
                yield pi, str(name), fd
        xo = res.get("/XObject")
        if isinstance(xo, pikepdf.Dictionary):
            for name, x in xo.items():
                if uxobjs is not None and str(name) not in uxobjs:
                    continue
                try:
                    if x.get("/Subtype") == "/Form":
                        yield from walk(x, x.get("/Resources") or res, pi, depth + 1)
                except Exception:
                    continue

    for pi, page in enumerate(pdf.pages):
        try:
            yield from walk(page, page.obj.get("/Resources"), pi)
        except Exception:
            continue
        for annot in page.obj.get("/Annots", []) or []:
            try:
                ap = annot.get("/AP")
                n = ap.get("/N") if ap is not None else None
                if n is None:
                    continue
                streams = [n] if isinstance(n, pikepdf.Stream) else [v for _, v in n.items() if isinstance(v, pikepdf.Stream)]
                for s in streams:
                    yield from walk(s, s.get("/Resources"), pi)
            except Exception:
                continue


def _embedded(fd) -> bool:
    desc = fd.get("/FontDescriptor")
    if desc is None:
        return False
    return any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))


def check_fonts(pdf) -> list[Finding]:
    out: list[Finding] = []
    for pi, _name, fd in _iter_fonts(pdf):
        try:
            sub = str(fd.get("/Subtype", ""))
            base = str(fd.get("/BaseFont", "")).lstrip("/")
            if sub == "/Type3":
                out.append(Finding("fonts", f"Type 3 font {base or '(unnamed)'}", pi, "re-export with outline (TrueType/OpenType) fonts"))
                continue
            if sub == "/Type0":
                desc_fonts = fd.get("/DescendantFonts") or []
                ok = all(_embedded(d) for d in desc_fonts) and len(desc_fonts) > 0
            else:
                ok = _embedded(fd)
            if not ok:
                clean = base.split("+", 1)[-1]
                extra = " (standard 14)" if clean in _STD14 else ""
                out.append(Finding("fonts", f"font not embedded: {clean}{extra}", pi, "embed and subset fonts (pdfcore optimize --embed-fonts, or re-export)"))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- links / outline
def check_links(pdf) -> list[Finding]:
    import pikepdf

    out: list[Finding] = []
    n = len(pdf.pages)
    page_objs = {p.obj.objgen for p in pdf.pages}
    names = _named_dests(pdf)

    def dest_ok(d) -> bool:
        if d is None:
            return False
        if isinstance(d, (pikepdf.Name, pikepdf.String)):
            key = str(d).lstrip("/")
            return key in names and dest_ok(names[key])
        if isinstance(d, pikepdf.Dictionary) and "/D" in d:
            return dest_ok(d.D)
        if isinstance(d, pikepdf.Array) and len(d) > 0:
            tgt = d[0]
            if isinstance(tgt, int):
                return 0 <= int(tgt) < n
            try:
                return tgt.objgen in page_objs
            except Exception:
                return False
        return False

    for pi, page in enumerate(pdf.pages):
        for a in page.obj.get("/Annots", []) or []:
            try:
                if a.get("/Subtype") != "/Link":
                    continue
                if "/Dest" in a:
                    if not dest_ok(a.Dest):
                        out.append(Finding("links", "internal link has no valid target", pi, "rebuild links (pdfcore link) or remove it"))
                elif "/A" in a:
                    act = a.A
                    s = str(act.get("/S", ""))
                    if s == "/GoTo" and not dest_ok(act.get("/D")):
                        out.append(Finding("links", "internal link has no valid target", pi, "rebuild links (pdfcore link) or remove it"))
                    elif s == "/URI":
                        uri = str(act.get("/URI", ""))
                        if not re.match(r"^(https?|mailto|ftp|tel):", uri, re.I):
                            out.append(Finding("links", f"link URI looks malformed: {uri[:60]}", pi, "use a full https:// or mailto: address"))
            except Exception:
                continue

    def walk(item, depth=0):
        while item is not None and depth < 50:
            try:
                ok = True
                if "/Dest" in item:
                    ok = dest_ok(item.Dest)
                elif "/A" in item and str(item.A.get("/S", "")) == "/GoTo":
                    ok = dest_ok(item.A.get("/D"))
                if not ok:
                    title = str(item.get("/Title", ""))[:40]
                    out.append(Finding("links", f"bookmark '{title}' has no valid target", None, "rebuild bookmarks (pdfcore bookmarks --auto)"))
                if "/First" in item:
                    walk(item.First, depth + 1)
                item = item.get("/Next")
            except Exception:
                break

    try:
        ol = pdf.Root.get("/Outlines")
        if ol is not None and "/First" in ol:
            walk(ol.First)
    except Exception:
        pass
    return out


def _named_dests(pdf) -> dict:
    out: dict = {}
    try:
        old = pdf.Root.get("/Dests")
        if old is not None:
            for k, v in old.items():
                out[str(k).lstrip("/")] = v
        names = pdf.Root.get("/Names")
        if names is not None and "/Dests" in names:
            from pikepdf import NameTree

            for k, v in NameTree(names.Dests).items():
                out[str(k)] = v
    except Exception:
        pass
    return out


def check_metadata(pdf, o: GateOptions) -> list[Finding]:
    need = o.require_metadata if o.require_metadata is not None else (o.profile == "typeset")
    out: list[Finding] = []
    title = ""
    try:
        title = str(pdf.docinfo.get("/Title", "")) if pdf.docinfo is not None else ""
        if not title:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as m:
                title = m.get("dc:title", "") or ""
    except Exception:
        pass
    if not title:
        out.append(Finding("metadata", "document title missing", None, "set metadata (pdfcore meta --title …)", "fail" if need else "warn"))
    if need:
        try:
            lang = str(pdf.Root.get("/Lang", ""))
        except Exception:
            lang = ""
        if not lang:
            out.append(Finding("metadata", "document language missing", None, "set /Lang (pdfcore meta --lang en-US)"))
    return out


# ---------------------------------------------------------------- render, blank pages, images
def check_render_and_blank(path: Path, n: int, o: GateOptions, password: str | None, found: Found) -> list[Finding]:
    from pdfcore import engines

    out: list[Finding] = []
    r = engines.get("render")
    pages = range(n) if n <= o.max_pages else sorted(set(range(0, n, max(1, n // o.max_pages))) | {n - 1})
    if o.png_dir:
        Path(o.png_dir).mkdir(parents=True, exist_ok=True)
    for pi in pages:
        try:
            img = r.render(path, pi, dpi=o.png_dpi, password=password)
        except Exception as exc:
            out.append(Finding("render", f"page fails to render: {exc}", pi, "repair the file"))
            continue
        if o.png_dir:
            p = Path(o.png_dir) / f"{path.stem}-p{pi + 1:03d}.png"
            img.save(p)
            found.pngs.append(p)
        if not o.allow_blank_pages and _is_blank(img) and n > 1:
            out.append(Finding("blank", "blank page", pi, "remove it (pdfcore delete-pages) or pass --allow-blank"))
    return out


def _is_blank(img) -> bool:
    g = img.convert("L")
    lo, hi = g.getextrema()
    if hi - lo < 8:
        return True
    hist = g.histogram()
    dark = sum(hist[:235])
    return dark / max(1, g.width * g.height) < 0.0005


def check_layout(path: Path, n: int, o: GateOptions, password: str | None) -> list[Finding]:
    """Clipped text (always), margins + headings at foot (typeset), image dpi, min text height (drawing)."""
    import pdfplumber

    out: list[Finding] = []
    try:
        pdf = pdfplumber.open(str(path), password=password or "")
    except Exception as exc:
        return [Finding("layout", f"cannot read text layer: {exc}", None, "repair the file")]
    with pdf:
        pages = pdf.pages if n <= o.max_pages else [pdf.pages[i] for i in range(0, n, max(1, n // o.max_pages))]
        body_size = _body_font_size(pages[: min(len(pages), 10)])
        for page in pages:
            pi = page.page_number - 1
            try:
                W, H = float(page.width), float(page.height)
                chars = [c for c in page.chars if c.get("text", "").strip()]
                # Text outside the visible page is clipped.
                sides = set()
                for c in chars:
                    if c["x1"] > W + 1:
                        sides.add("right")
                    if c["x0"] < -1:
                        sides.add("left")
                    if c["bottom"] > H + 1:
                        sides.add("bottom")
                    if c["top"] < -1:
                        sides.add("top")
                for s in sorted(sides):
                    out.append(Finding("layout", f"text clipped past {s} page edge", pi, "move or shrink the text"))
                if any(c.get("text") == "�" or str(c.get("text", "")).startswith("(cid:") for c in chars):
                    out.append(Finding("fonts", "missing glyphs (.notdef / unmapped characters)", pi, "use a font that covers every character"))
                if o.profile == "typeset":
                    m = (o.margins_in if o.margins_in is not None else 0.5) * INCH
                    # Running header/footer may sit inside the margin band down to half the margin.
                    hard = m * 0.45
                    bad = set()
                    for c in chars:
                        if c["x1"] > W - hard:
                            bad.add("right")
                        if c["x0"] < hard:
                            bad.add("left")
                        if c["bottom"] > H - hard:
                            bad.add("bottom")
                        if c["top"] < hard:
                            bad.add("top")
                    for s in sorted(bad):
                        out.append(Finding("layout", f"text past {s} margin", pi, "reduce width/size or increase margin in the template"))
                    out += _heading_at_foot(page, pi, body_size)
                if o.min_text_pt is not None and chars:
                    small = [c for c in chars if 0 < float(c.get("size", 0)) < o.min_text_pt - 0.05]
                    if len(small) > max(3, 0.02 * len(chars)):
                        out.append(Finding("layout", f"{len(small)} characters below {o.min_text_pt:g} pt at print size", pi, "enlarge text or print at a larger sheet size"))
                if o.min_image_dpi:
                    for im in page.images:
                        w_in = (float(im["x1"]) - float(im["x0"])) / INCH
                        sw = (im.get("srcsize") or (0, 0))[0]
                        if w_in > 0.4 and sw:
                            dpi = sw / w_in
                            if dpi + 0.5 < o.min_image_dpi:
                                out.append(Finding("images", f"image at {dpi:.0f} dpi (target {o.min_image_dpi:g})", pi, "supply a higher-resolution image or use vector art"))
            except Exception:
                continue
    return out


def _body_font_size(pages) -> float:
    from collections import Counter

    cnt: Counter = Counter()
    for p in pages:
        for c in p.chars:
            cnt[round(float(c.get("size", 0)), 1)] += 1
    return cnt.most_common(1)[0][0] if cnt else 10.0


def _heading_at_foot(page, pi: int, body: float) -> list[Finding]:
    """A line set larger/bolder than body text that is the last text in the body area of the page."""
    try:
        words = page.extract_words(extra_attrs=["size", "fontname"])
    except Exception:
        return []
    H = float(page.height)
    body_words = [w for w in words if w["bottom"] < H - 0.6 * INCH]  # ignore running footer
    if len(body_words) < 6:
        return []
    last_bottom = max(w["bottom"] for w in body_words)
    last_line = sorted([w for w in body_words if abs(w["bottom"] - last_bottom) < 2], key=lambda w: w["x0"])
    left_edge = min(w["x0"] for w in body_words)
    # Headings start at a left text edge and are not numeric rows (table subtotals, totals).
    starts_left = last_line[0]["x0"] - left_edge < 3 or any(abs(last_line[0]["x0"] - w["x0"]) < 1 for w in body_words if w is not last_line[0] and w["bottom"] < last_bottom - 20)
    numeric = re.search(r"\d[\d,]*\.\d{2}\b|\d{2,}$", " ".join(w["text"] for w in last_line))
    if not starts_left or numeric:
        return []
    size = max(float(w["size"]) for w in last_line)
    boldish = all("bold" in str(w.get("fontname", "")).lower() or "semibold" in str(w.get("fontname", "")).lower() for w in last_line)
    if (size >= body * 1.15 or boldish) and len(last_line) <= 10 and last_bottom > H * 0.7:
        return [Finding("flow", "heading at page foot", pi, "keep headings with the next paragraph (template: block(breakable: false))")]
    return []


# ---------------------------------------------------------------- redaction
def check_redaction(path: Path, o: GateOptions, password: str | None) -> list[Finding]:
    """Redaction regions are in unrotated page space (see pdfcore.ops.redact)."""
    import pypdfium2 as pdfium

    from pdfcore.text import open_plumber

    out: list[Finding] = []
    with open_plumber(path, password, unrotated=True) as pdf:
        for pi, rect in o.redact_regions or []:
            if pi >= len(pdf.pages):
                continue
            pg = pdf.pages[pi]
            inner = rect.expand(-0.5)
            left = "".join(c["text"] for c in pg.chars if inner.contains_point((c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2))
            for allow in o.redact_allow or []:
                left = left.replace(allow, "")
            if left.strip():
                out.append(Finding("redaction", f"text still extractable in redacted area: {''.join(left)[:20]!r}", pi, "re-run redact; the area was not scrubbed"))
        if o.redact_terms:
            full = []
            for pg in pdf.pages:
                full.append(pg.extract_text() or "")
            blob = "\n".join(full)
            for t in o.redact_terms:
                if t and t in blob:
                    out.append(Finding("redaction", f"redacted term still present: {t[:20]!r}", None, "re-run redact on every page"))
    # Second opinion from a different text engine.
    try:
        doc = pdfium.PdfDocument(str(path), password=password)
        for pi, rect in o.redact_regions or []:
            if pi >= len(doc):
                continue
            page = doc[pi]
            tp = page.get_textpage()
            # pdfium text coordinates are unrotated user space; get_height() would apply /Rotate.
            ml, mb, mr, mt = page.get_mediabox()
            r = rect.expand(-0.5)
            llx, lly, urx, ury = ml + r.x0, mt - r.bottom, ml + r.x1, mt - r.top
            txt = tp.get_text_bounded(left=llx, bottom=lly, right=urx, top=ury)
            for allow in o.redact_allow or []:
                txt = txt.replace(allow, "")
            if txt.strip():
                out.append(Finding("redaction", f"pdfium still reads text in redacted area: {txt.strip()[:20]!r}", pi, "re-run redact"))
        doc.close()
    except Exception as exc:
        out.append(Finding("redaction", f"second-engine redaction check could not run: {exc}", None, "", "warn"))
    return out


# ---------------------------------------------------------------- standards
_VERA_FLAVOURS = {
    "pdfa-1a": "1a", "pdfa-1b": "1b", "pdfa-2a": "2a", "pdfa-2b": "2b", "pdfa-2u": "2u",
    "pdfa-3a": "3a", "pdfa-3b": "3b", "pdfa-3u": "3u", "pdfa-4": "4", "pdfua-1": "ua1", "pdfua-2": "ua2",
}


def check_standard(path: Path, standard: str) -> list[Finding]:
    exe = shutil.which("verapdf")
    if not exe:
        return [Finding("standard", "veraPDF not installed; cannot validate", None, "run pdfcore install")]
    flav = _VERA_FLAVOURS.get(standard.lower(), standard)
    try:
        res = subprocess.run([exe, "--flavour", flav, "--format", "text", str(path)], capture_output=True, text=True, timeout=300)
    except Exception as exc:
        return [Finding("standard", f"veraPDF failed: {exc}", None, "")]
    txt = res.stdout
    if re.search(r"^PASS\b", txt, re.M):
        return []
    first = next((ln for ln in txt.splitlines() if ln.strip()), "veraPDF reported failures")
    return [Finding("standard", f"not {standard}: {first.strip()[:80]}", None, "run pdfcore preflight for the rule list")]
