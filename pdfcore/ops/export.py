"""PDF → other formats, for the ``pdfcore convert`` front door.

* **SVG** — one file per page from PDFium's page objects, in painting order: paths (fill/stroke colour,
  opacity, width, dash, caps/joins, even-odd), text as ``<text>`` (font family/weight/style, size, exact
  position and rotation; ``textLength`` keeps each run's width), images as ``data:`` URIs, Form XObjects as
  nested groups, rectangular/path clips. Annotations and form fields are flattened first.
* **Markdown / HTML / DOCX** — a reading-order structure: headings (font size/weight, via
  :func:`pdfcore.ops.navigate.detect_headings`), paragraphs (de-hyphenated), bullet/numbered lists,
  tables (:func:`pdfcore.ops.extract.find_tables`) and images; running headers/footers dropped.
  DOCX is written with python-docx (MIT).
* **Raster** — PNG, JPEG, TIFF (optionally one multi-page TIFF), WebP, GIF, BMP, AVIF, JPEG 2000.

Nothing here writes a PDF: each function verifies what it wrote (opens it with the matching library)
and prints ``<path>  PASS``. Coordinates in the structure are points, top-left origin.
"""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from pdfcore.result import OpResult, output_path

__all__ = [
    "RASTER_FORMATS", "target_paths", "verify_output", "pdf_to_raster", "pdf_to_svg", "read_structure",
    "pdf_to_markdown", "pdf_to_html", "pdf_to_docx", "pdf_to_csv", "DocBlock",
]

# format → (Pillow format name, extension)
RASTER_FORMATS: dict[str, tuple[str, str]] = {
    "png": ("PNG", ".png"), "jpg": ("JPEG", ".jpg"), "tiff": ("TIFF", ".tif"), "webp": ("WEBP", ".webp"),
    "gif": ("GIF", ".gif"), "bmp": ("BMP", ".bmp"), "avif": ("AVIF", ".avif"), "jp2": ("JPEG2000", ".jp2"),
}


# ---------------------------------------------------------------- output paths + verification


def _unique(p: Path) -> Path:
    cand, n = p, 2
    while cand.exists():
        cand = p.with_name(f"{p.stem}-{n}{p.suffix}")
        n += 1
    return cand


def single_target(src: Path, ext: str, out: str | Path | None) -> Path:
    """``report.pdf`` → ``report.<ext>`` when free, else ``report.converted.<ext>``; *out* (file or folder) wins."""
    if out is not None:
        o = Path(out)
        if o.is_dir():
            p = _unique(o / f"{src.stem}{ext}")
        else:
            if o.exists() and o.resolve() == src.resolve():
                raise ValueError("refusing to overwrite the input file")
            p = o
    else:
        p = src.with_suffix(ext)
        if p.exists() or p.resolve() == src.resolve():
            p = output_path(src, "converted", ext)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def target_paths(src: Path, ext: str, numbers: Sequence[int], out: str | Path | None,
                 folder_suffix: str = "pages") -> list[Path]:
    """One path per part (page/frame/table). One part: :func:`single_target`. Several: ``<stem>_pNNN<ext>``
    in *out* (a folder), beside *out* (a file name: its stem is used), or in a fresh ``<stem>_<suffix>/``."""
    if len(numbers) == 1:
        return [single_target(src, ext, out)]
    digits = max(3, len(str(max(numbers))))
    base = src.stem
    if out is not None:
        o = Path(out)
        if o.is_dir() or not o.suffix:
            folder = o
        else:
            folder, base = o.parent, o.stem
        folder.mkdir(parents=True, exist_ok=True)
    else:
        from pdfcore.ops.extract import _out_dir

        folder = _out_dir(src, folder_suffix, None)
    return [_unique(folder / f"{base}_p{n:0{digits}d}{ext}") for n in numbers]


def verify_output(p: Path) -> None:
    """Raise unless *p* exists, is non-empty and opens as its type."""
    p = Path(p)
    if not p.is_file() or p.stat().st_size == 0:
        raise RuntimeError(f"output not written: {p}")
    ext = p.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".bmp", ".avif", ".jp2"):
        from PIL import Image

        with Image.open(p) as im:
            im.verify()
    elif ext in (".heic", ".heif"):
        head = p.read_bytes()[:32]
        if b"ftyp" not in head:
            raise RuntimeError(f"{p.name} is not a HEIF file")
    elif ext in (".svg", ".html", ".htm", ".xhtml"):
        import xml.etree.ElementTree as ET

        ET.fromstring(p.read_bytes())
    elif ext == ".json":
        json.loads(p.read_text(encoding="utf-8"))
    elif ext in (".md", ".txt", ".csv"):
        p.read_text(encoding="utf-8-sig")
    elif ext == ".xlsx":
        import openpyxl

        openpyxl.load_workbook(p, read_only=True).close()
    elif ext == ".docx":
        import docx

        docx.Document(str(p))
    elif ext == ".dxf":
        import ezdxf

        ezdxf.readfile(str(p))
    elif ext == ".dwg":
        if not p.read_bytes()[:4].startswith(b"AC1"):
            raise RuntimeError(f"{p.name} is not a DWG file")


def done(files: list[Path], data: dict, quiet: bool, note: str = "") -> OpResult:
    for f in files:
        verify_output(f)
    if not quiet:
        for f in files:
            print(f"{f}  PASS{note}")
    return OpResult(files[0] if files else None, None, files[1:], data)


def _select(src: Path, pages: str | Sequence[int] | None, password: str | None) -> list[int]:
    from pdfcore.ops.extract import _select as sel

    return sel(src, pages, password)


# ---------------------------------------------------------------- raster


def save_raster(img, path: Path, fmt: str, *, dpi: float | None = None, quality: int = 90,
                icc: bytes | None = None) -> None:
    """Save a PIL image as *fmt* (key of RASTER_FORMATS), converting the mode the format needs."""
    pil_fmt, _ = RASTER_FORMATS[fmt]
    kw: dict[str, Any] = {}
    if dpi:
        kw["dpi"] = (dpi, dpi)
    if icc and fmt in ("png", "jpg", "tiff", "webp", "avif"):
        kw["icc_profile"] = icc
    if fmt in ("jpg",) or (fmt in ("bmp",) and img.mode not in ("1", "L", "P", "RGB", "RGBA")):
        img = flatten_alpha(img)
    if fmt == "jpg":
        if img.mode not in ("L", "RGB", "CMYK"):
            img = img.convert("RGB")
        kw.update(quality=quality, optimize=True)
    elif fmt == "png":
        if img.mode == "CMYK":
            img = img.convert("RGB")
        kw.update(compress_level=6)
    elif fmt == "tiff":
        kw.update(compression="tiff_deflate")
    elif fmt == "webp":
        kw.update(quality=quality, method=4)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
    elif fmt == "gif":
        if img.mode != "P":
            from PIL import Image

            img = img.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        kw.pop("dpi", None)
    elif fmt == "avif":
        kw.update(quality=quality)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode else "RGB")
        kw.pop("dpi", None)
    elif fmt == "jp2":
        if img.mode not in ("L", "RGB", "RGBA", "LA"):
            img = img.convert("RGB")
        kw.pop("dpi", None)
    img.save(path, pil_fmt, **kw)


def flatten_alpha(img):
    """Composite transparency onto white (for formats without alpha)."""
    from PIL import Image

    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA", "PA"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").getchannel("A"))
        return bg
    return img


def pdf_to_raster(
    src: str | Path,
    fmt: str,
    *,
    pages: str | Sequence[int] | None = None,
    dpi: float = 150,
    quality: int = 90,
    multipage: bool = False,
    out: str | Path | None = None,
    password: str | None = None,
    quiet: bool = False,
) -> OpResult:
    """Render pages to *fmt* images. One page → ``<stem>.<ext>``; several → ``<stem>_pages/<stem>_pNNN.<ext>``.
    ``multipage=True`` (TIFF, GIF, WebP) writes every page into one file instead."""
    from pdfcore import engines

    src = Path(src)
    if fmt not in RASTER_FORMATS:
        raise ValueError(f"image format must be one of {', '.join(RASTER_FORMATS)}")
    if not 10 <= dpi <= 2400:
        raise ValueError("dpi must be between 10 and 2400")
    idx = _select(src, pages, password)
    if not idx:
        raise ValueError("no pages selected")
    ext = RASTER_FORMATS[fmt][1]
    eng = engines.get("render")
    if multipage and len(idx) > 1:
        if fmt not in ("tiff", "gif", "webp"):
            raise ValueError("multi-page output needs tiff, gif or webp")
        imgs = [eng.render(src, i, dpi=dpi, password=password) for i in idx]
        dst = single_target(src, ext, out)
        pil_fmt = RASTER_FORMATS[fmt][0]
        if fmt == "gif":
            imgs = [im.convert("RGB").quantize(colors=256) for im in imgs]
            imgs[0].save(dst, pil_fmt, save_all=True, append_images=imgs[1:], duration=1000, loop=0)
        elif fmt == "webp":
            imgs[0].save(dst, pil_fmt, save_all=True, append_images=imgs[1:], quality=quality, duration=1000)
        else:
            imgs[0].save(dst, pil_fmt, save_all=True, append_images=imgs[1:], compression="tiff_deflate", dpi=(dpi, dpi))
        return done([dst], {"pages": [i + 1 for i in idx], "dpi": dpi, "multipage": True}, quiet,
                    f"  ({len(idx)} pages)")
    dests = target_paths(src, ext, [i + 1 for i in idx], out)
    for i, dst in zip(idx, dests):
        save_raster(eng.render(src, i, dpi=dpi, password=password), dst, fmt, dpi=dpi, quality=quality)
    return done(dests, {"pages": [i + 1 for i in idx], "dpi": dpi, "dir": str(dests[0].parent)}, quiet)


# ---------------------------------------------------------------- PDF → SVG


def _f(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _mat(m) -> str:
    return "matrix(" + " ".join(_f(x) for x in (m.a, m.b, m.c, m.d, m.e, m.f)) + ")"


_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")


def _xml_text(s: str) -> str:
    return html.escape(_XML_BAD.sub("", s), quote=False)


def _attr(s: str) -> str:
    return html.escape(_XML_BAD.sub("", s), quote=True)


def _rgba(fn, obj) -> tuple[str, float] | None:
    import ctypes

    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    if not fn(obj, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)):
        return None
    return f"#{r.value:02x}{g.value:02x}{b.value:02x}", a.value / 255.0


_STD_FAMILY = {"helvetica": "Helvetica, Arial, sans-serif", "arial": "Arial, Helvetica, sans-serif",
               "times": "'Times New Roman', Times, serif", "courier": "'Courier New', Courier, monospace",
               "symbol": "Symbol", "zapfdingbats": "'Zapf Dingbats', ZapfDingbats"}


def font_css(base: str, weight: int | None, flags: int = 0) -> tuple[str, str, str]:
    """(font-family, font-weight, font-style) for a PDF base font name like ``ABCDEF+Inter-SemiBoldItalic``."""
    name = base.split("+", 1)[-1]
    fam_raw = re.split(r"[-,]", name, maxsplit=1)[0]
    style_part = name[len(fam_raw):].lower()
    fam = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", fam_raw).replace("MT", "").strip() or "sans-serif"
    for std in ("Bold", "Italic", "Oblique", "Regular", "Roman", "PS"):
        fam = re.sub(rf"\s*{std}$", "", fam)
    low = name.lower()
    w = "400"
    for key, val in (("black", "900"), ("heavy", "800"), ("extrabold", "800"), ("semibold", "600"), ("demi", "600"),
                     ("bold", "700"), ("medium", "500"), ("light", "300"), ("thin", "100")):
        if key in low:
            w = val
            break
    else:
        if weight and weight >= 600:
            w = str(weight)
    style = "italic" if ("italic" in low or "oblique" in low or "-it" in style_part) else "normal"
    generic = "monospace" if flags & 1 else "serif" if flags & 2 else "sans-serif"
    std = next((v for k, v in _STD_FAMILY.items() if fam_raw.lower().startswith(k)), None)
    family = std or f"'{fam}', {generic}"
    return family, w, style


@dataclass
class _SvgState:
    defs: list[str] = field(default_factory=list)
    clips: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=lambda: {"path": 0, "text": 0, "image": 0, "skipped": 0})
    images: bool = True


def _path_d(obj, R) -> str:
    import ctypes

    parts: list[str] = []
    n = R.FPDFPath_CountSegments(obj)
    x, y = ctypes.c_float(), ctypes.c_float()
    bez: list[str] = []
    for s in range(n):
        seg = R.FPDFPath_GetPathSegment(obj, s)
        R.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
        t = R.FPDFPathSegment_GetType(seg)
        pt = f"{_f(x.value)} {_f(y.value)}"
        if t == R.FPDF_SEGMENT_MOVETO:
            parts.append("M" + pt)
        elif t == R.FPDF_SEGMENT_LINETO:
            parts.append("L" + pt)
        elif t == R.FPDF_SEGMENT_BEZIERTO:
            bez.append(pt)
            if len(bez) == 3:
                parts.append("C" + " ".join(bez))
                bez = []
        if R.FPDFPathSegment_GetClose(seg):
            parts.append("Z")
    return "".join(parts)


def _clip_id(obj, R, st: _SvgState, page_box: tuple[float, float, float, float]) -> str | None:
    """Clip path of a page object as a <clipPath> id (page-space coordinates), or None when unclipped/trivial."""
    import ctypes

    clip = R.FPDFPageObj_GetClipPath(obj)
    if not clip:
        return None
    n_paths = R.FPDFClipPath_CountPaths(clip)
    if n_paths <= 0:
        return None
    x, y = ctypes.c_float(), ctypes.c_float()
    ds: list[str] = []
    pts_all: list[tuple[float, float]] = []
    for pi in range(n_paths):
        parts, bez = [], []
        for s in range(R.FPDFClipPath_CountPathSegments(clip, pi)):
            seg = R.FPDFClipPath_GetPathSegment(clip, pi, s)
            R.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
            pts_all.append((x.value, y.value))
            t = R.FPDFPathSegment_GetType(seg)
            pt = f"{_f(x.value)} {_f(y.value)}"
            if t == R.FPDF_SEGMENT_MOVETO:
                parts.append("M" + pt)
            elif t == R.FPDF_SEGMENT_LINETO:
                parts.append("L" + pt)
            else:
                bez.append(pt)
                if len(bez) == 3:
                    parts.append("C" + " ".join(bez))
                    bez = []
            if R.FPDFPathSegment_GetClose(seg):
                parts.append("Z")
        if parts:
            ds.append("".join(parts))
    if not ds:
        return None
    if n_paths == 1 and pts_all:
        l, b, r, t = page_box
        xs, ys = [p[0] for p in pts_all], [p[1] for p in pts_all]
        if min(xs) <= l + 0.5 and min(ys) <= b + 0.5 and max(xs) >= r - 0.5 and max(ys) >= t - 0.5:
            return None  # clip covers the whole page
    key = "|".join(ds)
    if key not in st.clips:
        cid = f"c{len(st.clips) + 1}"
        st.clips[key] = cid
        # each subpath intersects in PDF; nest one clipPath per subpath
        inner = "".join(f'<path d="{d}"/>' for d in ds[:1])
        st.defs.append(f'<clipPath id="{cid}" clipPathUnits="userSpaceOnUse">{inner}</clipPath>')
        prev = cid
        for k, d in enumerate(ds[1:], start=2):
            cid2 = f"{cid}_{k}"
            st.defs.append(f'<clipPath id="{cid2}" clip-path="url(#{prev})" clipPathUnits="userSpaceOnUse"><path d="{d}"/></clipPath>')
            prev = cid2
        st.clips[key] = prev
    return st.clips[key]


def _image_href(obj, R) -> str | None:
    m = obj.get_matrix()
    pil = None
    axis = abs(m.b) < 1e-6 and abs(m.c) < 1e-6 and m.a > 0 and m.d > 0
    try:
        if axis and R.FPDFPageObj_HasTransparency(obj):
            pil = obj.get_bitmap(render=True).to_pil()  # applies the soft mask; upright for axis-aligned images
        else:
            pil = obj.get_bitmap(render=False).to_pil()
    except Exception:
        try:
            pil = obj.get_bitmap(render=True).to_pil()
        except Exception:
            return None
    if pil is None:
        return None
    try:
        filters = [str(f) for f in obj.get_filters()]
    except Exception:
        filters = []
    buf = io.BytesIO()
    if pil.mode in ("RGBA", "LA") and pil.getchannel("A").getextrema() != (255, 255):
        pil.save(buf, "PNG", compress_level=6)
        mime = "image/png"
    elif any(f in ("DCTDecode", "JPXDecode") for f in filters):
        if pil.mode not in ("L", "RGB"):
            pil = pil.convert("RGB")
        pil.save(buf, "JPEG", quality=90)
        mime = "image/jpeg"
    else:
        if pil.mode not in ("1", "L", "RGB", "RGBA", "P"):
            pil = pil.convert("RGB")
        if pil.mode == "RGBA":
            pil = pil.convert("RGB")
        pil.save(buf, "PNG", compress_level=6)
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _path_el(obj, R) -> str | None:
    import ctypes

    d = _path_d(obj, R)
    if not d:
        return None
    fillmode, stroke = ctypes.c_int(), ctypes.c_int()
    R.FPDFPath_GetDrawMode(obj, ctypes.byref(fillmode), ctypes.byref(stroke))
    a = [f'd="{d}"', f'transform="{_mat(obj.get_matrix())}"']
    if fillmode.value:
        c = _rgba(R.FPDFPageObj_GetFillColor, obj) or ("#000000", 1.0)
        a.append(f'fill="{c[0]}"')
        if c[1] < 0.999:
            a.append(f'fill-opacity="{_f(c[1])}"')
        if fillmode.value == R.FPDF_FILLMODE_ALTERNATE:
            a.append('fill-rule="evenodd"')
    else:
        a.append('fill="none"')
    if stroke.value:
        c = _rgba(R.FPDFPageObj_GetStrokeColor, obj) or ("#000000", 1.0)
        w = ctypes.c_float()
        R.FPDFPageObj_GetStrokeWidth(obj, ctypes.byref(w))
        a.append(f'stroke="{c[0]}" stroke-width="{_f(w.value) if w.value > 0 else "0.1"}"')
        if c[1] < 0.999:
            a.append(f'stroke-opacity="{_f(c[1])}"')
        if w.value <= 0:
            a.append('vector-effect="non-scaling-stroke"')  # PDF width 0 = thinnest line the device can draw
        cap, join = R.FPDFPageObj_GetLineCap(obj), R.FPDFPageObj_GetLineJoin(obj)
        if cap in (1, 2):
            a.append(f'stroke-linecap="{"round" if cap == 1 else "square"}"')
        if join in (1, 2):
            a.append(f'stroke-linejoin="{"round" if join == 1 else "bevel"}"')
        n = R.FPDFPageObj_GetDashCount(obj)
        if n and n > 0:
            arr = (ctypes.c_float * n)()
            if R.FPDFPageObj_GetDashArray(obj, arr, n) and any(v > 0 for v in arr):
                a.append(f'stroke-dasharray="{" ".join(_f(v) for v in arr)}"')
                ph = ctypes.c_float()
                if R.FPDFPageObj_GetDashPhase(obj, ctypes.byref(ph)) and ph.value:
                    a.append(f'stroke-dashoffset="{_f(ph.value)}"')
    if not fillmode.value and not stroke.value:
        return None  # clipping-only path (its effect is carried by the clip of later objects)
    return f'<path {" ".join(a)}/>'


def _compose(m1: tuple, m2: tuple) -> tuple:
    """Apply m1, then m2 (PDF matrices a b c d e f)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _invert(m: tuple) -> tuple | None:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_))


def _char_map(tp, R) -> dict[int, list[tuple[str, float, float]]]:
    """Text object handle → [(char, origin x, origin y)] in page space, from PDFium's text page."""
    import ctypes

    out: dict[int, list[tuple[str, float, float]]] = {}
    x, y = ctypes.c_double(), ctypes.c_double()
    for ci in range(R.FPDFText_CountChars(tp)):
        o = R.FPDFText_GetTextObject(tp, ci)
        if not o:
            continue
        u = R.FPDFText_GetUnicode(tp, ci)
        if not u or 0xD800 <= u <= 0xDFFF:
            continue
        R.FPDFText_GetCharOrigin(tp, ci, ctypes.byref(x), ctypes.byref(y))
        out.setdefault(ctypes.cast(o, ctypes.c_void_p).value or 0, []).append((chr(u), x.value, y.value))
    return out


def _text_el(obj, R, chars: list[tuple[str, float, float]] | None = None, parents: tuple | None = None) -> str | None:
    xs: list[float] | None = None
    if chars:
        mm = obj.get_matrix()
        total = (mm.a, mm.b, mm.c, mm.d, mm.e, mm.f)
        if parents is not None:
            total = _compose(total, parents)
        inv = _invert(total)
        if inv is not None:
            txt = "".join(c for c, _, _ in chars)
            xs = [inv[0] * px + inv[2] * py + inv[4] for _, px, py in chars]
    if xs is None:
        try:
            txt = obj.extract()
        except Exception:
            return None
    txt = txt.replace("\r", " ").replace("\n", " ").replace("\x02", "-")
    if xs is not None:
        keep = [k for k, ch in enumerate(txt) if not _XML_BAD.match(ch)]
        txt, xs = "".join(txt[k] for k in keep), [xs[k] for k in keep]
    else:
        txt = _XML_BAD.sub("", txt)
    if not txt.strip():
        return None
    try:
        size = obj.get_font_size()
    except Exception:
        size = 0
    if size <= 0:
        size = 1.0
    try:
        font = obj.get_font()
        base = font.get_base_name()
        try:
            weight = font.get_weight()
        except Exception:
            weight = None
        flags = R.FPDFFont_GetFlags(font)
    except Exception:
        base, weight, flags = "Helvetica", None, 0
    family, w, style = font_css(base, weight, flags or 0)
    m = obj.get_matrix()
    mode = R.FPDFTextObj_GetTextRenderMode(obj)
    a = [f'transform="{_mat(m)} scale(1 -1)"', f'font-size="{_f(size)}"', f'font-family="{_attr(family)}"']
    if w != "400":
        a.append(f'font-weight="{w}"')
    if style != "normal":
        a.append('font-style="italic"')
    fill = _rgba(R.FPDFPageObj_GetFillColor, obj) or ("#000000", 1.0)
    if mode in (R.FPDF_TEXTRENDERMODE_INVISIBLE, R.FPDF_TEXTRENDERMODE_CLIP):
        a.append('fill="#000000" fill-opacity="0"')  # OCR layer: keep it searchable, not visible
    elif mode in (R.FPDF_TEXTRENDERMODE_STROKE, R.FPDF_TEXTRENDERMODE_STROKE_CLIP):
        sc = _rgba(R.FPDFPageObj_GetStrokeColor, obj) or ("#000000", 1.0)
        a.append(f'fill="none" stroke="{sc[0]}" stroke-width="{_f(size / 40)}"')
    else:
        a.append(f'fill="{fill[0]}"')
        if fill[1] < 0.999:
            a.append(f'fill-opacity="{_f(fill[1])}"')
    if xs is not None and len(xs) == len(txt):
        a.append(f'x="{" ".join(_f(v) for v in xs)}"')  # every glyph at its PDF position
    else:  # width of the run in text space, so the viewer's font substitute keeps the PDF's line length
        try:
            q = obj.get_quad_points()
            (x1, y1), (x2, y2) = q[0], q[1]
            scale = math.hypot(m.a, m.b) or 1.0
            width = math.hypot(x2 - x1, y2 - y1) / scale
            if width > size * 0.3 and len(txt) > 1:
                a.append(f'textLength="{_f(width)}" lengthAdjust="spacingAndGlyphs"')
        except Exception:
            pass
    if "  " in txt or txt != txt.strip():
        a.append('xml:space="preserve"')
    return f'<text {" ".join(a)}>{_xml_text(txt)}</text>'


def _page_svg(pdf, i: int, st: _SvgState, password: str | None = None) -> tuple[str, float, float]:
    import pypdfium2.raw as R

    page = pdf[i]
    try:
        page.flatten()  # annotation appearances + form field values become page content
        page.close()
        page = pdf[i]
    except Exception:
        pass
    l, b, r, t = page.get_cropbox()
    W, H = r - l, t - b
    rot = page.get_rotation() % 360
    vw, vh = (H, W) if rot in (90, 270) else (W, H)
    flip = f"matrix(1 0 0 -1 {_f(-l)} {_f(t)})"
    rotm = {0: "", 90: f"matrix(0 1 -1 0 {_f(H)} 0) ", 180: f"matrix(-1 0 0 -1 {_f(W)} {_f(H)}) ",
            270: f"matrix(0 -1 1 0 0 {_f(W)}) "}[rot]
    tp = page.get_textpage()
    cmap = _char_map(tp, R)
    body: list[str] = []
    open_levels: list[int] = []
    form_mats: list[tuple] = []
    box = (l, b, r, t)
    for obj in page.get_objects(textpage=tp):
        lvl = getattr(obj, "level", 0)
        while open_levels and open_levels[-1] >= lvl:
            body.append("</g>")
            open_levels.pop()
            form_mats.pop()
        typ = obj.type
        el: str | None = None
        if typ == R.FPDF_PAGEOBJ_FORM:
            fm = obj.get_matrix()
            body.append(f'<g transform="{_mat(fm)}">')
            open_levels.append(lvl)
            form_mats.append((fm.a, fm.b, fm.c, fm.d, fm.e, fm.f))
            continue
        if typ == R.FPDF_PAGEOBJ_PATH:
            el = _path_el(obj, R)
            kind = "path"
        elif typ == R.FPDF_PAGEOBJ_TEXT:
            import ctypes

            parents = None
            for fm_ in reversed(form_mats):
                parents = fm_ if parents is None else _compose(parents, fm_)
            el = _text_el(obj, R, cmap.get(ctypes.cast(obj.raw, ctypes.c_void_p).value or 0), parents)
            kind = "text"
        elif typ == R.FPDF_PAGEOBJ_IMAGE and st.images:
            href = _image_href(obj, R)
            if href:
                el = (f'<image width="1" height="1" preserveAspectRatio="none" '
                      f'transform="{_mat(obj.get_matrix())} translate(0 1) scale(1 -1)" href="{href}"/>')
            kind = "image"
        else:
            kind = "skipped"
        if el is None:
            if typ not in (R.FPDF_PAGEOBJ_PATH, R.FPDF_PAGEOBJ_TEXT):
                st.counts["skipped"] += 1
            continue
        st.counts[kind] += 1
        cid = _clip_id(obj, R, st, box) if lvl == 0 else None
        body.append(f'<g clip-path="url(#{cid})">{el}</g>' if cid else el)
    body.extend("</g>" for _ in open_levels)
    tp.close()
    page.close()
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{_f(vw)}pt" height="{_f(vh)}pt" viewBox="0 0 {_f(vw)} {_f(vh)}">')
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', head]
    if st.defs:
        parts.append("<defs>" + "".join(st.defs) + "</defs>")
    parts.append(f'<rect width="{_f(vw)}" height="{_f(vh)}" fill="#ffffff"/>')
    parts.append(f'<g transform="{rotm}{flip}">')
    parts += body
    parts += ["</g>", "</svg>"]
    return "\n".join(parts) + "\n", vw, vh


def pdf_to_svg(
    src: str | Path,
    *,
    pages: str | Sequence[int] | None = None,
    images: bool = True,
    out: str | Path | None = None,
    password: str | None = None,
    quiet: bool = False,
) -> OpResult:
    """One SVG per page (``<stem>.svg``, or ``<stem>_svg/<stem>_pNNN.svg`` for several pages)."""
    import pypdfium2 as pdfium

    src = Path(src)
    idx = _select(src, pages, password)
    dests = target_paths(src, ".svg", [i + 1 for i in idx], out, "svg")
    pdf = pdfium.PdfDocument(str(src), password=password)
    counts: list[dict[str, int]] = []
    try:
        try:
            pdf.init_forms()
        except Exception:
            pass
        for i, dst in zip(idx, dests):
            st = _SvgState(images=images)
            svg, _, _ = _page_svg(pdf, i, st, password)
            dst.write_text(svg, encoding="utf-8")
            counts.append(st.counts)
    finally:
        pdf.close()
    res = done(dests, {"pages": [i + 1 for i in idx], "counts": counts}, quiet=True)
    if not quiet:
        for d, c in zip(dests, counts):
            print(f"{d}  PASS  ({c['path']} paths, {c['text']} text runs, {c['image']} images"
                  + (f", {c['skipped']} shadings/other skipped" if c["skipped"] else "") + ")")
    return res


# ---------------------------------------------------------------- reading-order structure


@dataclass
class DocBlock:
    kind: str  # heading | para | bullet | number | table | image
    page: int  # 0-based
    top: float
    text: str = ""
    level: int = 1  # heading level 1–6
    rows: list[list[str]] | None = None
    image: bytes | None = None  # PNG/JPEG bytes
    image_ext: str = ".png"
    width_pt: float = 0.0
    height_pt: float = 0.0


_BULLET = re.compile(r"^\s*([•◦▪▫●○■□‣⁃∙·–—\-\*➢►▶✓✔])\s+(.*)$")
_NUMBER = re.compile(r"^\s*(\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|[ivxIVX]{1,4}[.)])\s+(.*)$")
_PAGENO = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$", re.I)


def _join_lines(lines: list[str]) -> str:
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out.endswith("-") and len(out) > 1 and out[-2].isalpha() and ln[:1].islower():
            out = out[:-1] + ln  # de-hyphenate a word broken across lines
        else:
            out = f"{out} {ln}" if out else ln
    return out


def _norm(s: str) -> str:
    return re.sub(r"\d+", "#", s.lower()).strip()


def _page_images(pdf, i: int, min_pt: float = 24) -> list[DocBlock]:
    """Embedded images on page *i* (top-level and inside forms) as DocBlocks with PNG/JPEG bytes."""
    import pypdfium2.raw as R

    page = pdf[i]
    top_y = page.get_cropbox()[3]
    out: list[DocBlock] = []
    for obj in page.get_objects(filter=[R.FPDF_PAGEOBJ_IMAGE]):
        try:
            x0, y0, x1, y1 = obj.get_bounds()
        except Exception:
            continue
        if (x1 - x0) < min_pt or (y1 - y0) < min_pt:
            continue
        try:
            pil = obj.get_bitmap(render=True).to_pil()
        except Exception:
            try:
                pil = obj.get_bitmap(render=False).to_pil()
            except Exception:
                continue
        buf = io.BytesIO()
        if pil.mode in ("RGBA", "LA") and pil.getchannel("A").getextrema() != (255, 255):
            pil.save(buf, "PNG")
            ext = ".png"
        else:
            pil = pil.convert("RGB") if pil.mode not in ("L", "RGB") else pil
            filters = []
            try:
                filters = [str(f) for f in obj.get_filters()]
            except Exception:
                pass
            if "DCTDecode" in filters or "JPXDecode" in filters:
                pil.save(buf, "JPEG", quality=90)
                ext = ".jpg"
            else:
                pil.save(buf, "PNG")
                ext = ".png"
        out.append(DocBlock("image", i, top_y - y1, image=buf.getvalue(), image_ext=ext,
                            width_pt=x1 - x0, height_pt=y1 - y0))
    page.close()
    return out


def _heading_like(h) -> bool:
    """Drop heading candidates that read like body text (sentences, bullet runs) — detect_headings judges by size
    alone, which misfires on short documents whose most common size is small print."""
    t = h.text.strip()
    if _BULLET.match(t) or re.search(r"\s[•◦▪●■]\s", t):
        return False
    if not h.bold and (t.endswith((".", ",", ";", ":")) or len(t.split()) > 12):
        return False
    return len(t.split()) <= 20


def read_structure(src: str | Path, *, pages: str | Sequence[int] | None = None, password: str | None = None,
                   with_images: bool = True) -> tuple[str, list[DocBlock]]:
    """(title, blocks in reading order) for a text PDF. Scanned pages without a text layer give no text
    (run ``pdfcore ocr`` first)."""
    import pikepdf
    import pypdfium2 as pdfium

    from pdfcore import engines
    from pdfcore import text as ptext
    from pdfcore.ops.extract import find_tables, reading_order
    from pdfcore.ops.navigate import detect_headings

    src = Path(src)
    idx = _select(src, pages, password)
    te = engines.get("text")
    heads = [h for h in detect_headings(src, password=password) if _heading_like(h)]
    lvl_of: dict[int, int] = {}
    if heads:
        top = max(h.size for h in heads)
        lone_title = heads[0].size == top and sum(1 for h in heads if h.size == top) == 1 and len(heads) > 1
        for k, h_ in enumerate(heads):
            lvl_of[id(h_)] = 1 if (lone_title and k == 0) else min(6, h_.level + (2 if lone_title else 1))
    tables = find_tables(src, pages=idx, password=password)

    # running headers / footers: short lines in the top/bottom 7 % that repeat on many pages
    page_lines: dict[int, list] = {}
    sizes: dict[int, tuple[float, float]] = {}
    for i in idx:
        sizes[i] = te.page_size(src, i, password=password)
        ws = te.words(src, i, password=password)
        tb = [t.bbox for t in tables if t.page == i]
        ws = [w for w in ws if not any(r.contains_point((w.rect.x0 + w.rect.x1) / 2, (w.rect.top + w.rect.bottom) / 2)
                                       for r in tb)]
        page_lines[i] = ws
    band_count: dict[str, set[int]] = {}
    for i, ws in page_lines.items():
        h = sizes[i][1]
        for ln in ptext.group_lines(ws):
            if ln.rect.top < 0.07 * h or ln.rect.bottom > 0.93 * h:
                band_count.setdefault(_norm(ln.text), set()).add(i)
    limit = max(2, -(-len(idx) // 2))
    running = {k for k, v in band_count.items() if len(v) >= limit and len(idx) >= 2}

    blocks: list[DocBlock] = []
    for i in idx:
        h = sizes[i][1]
        hs = [x for x in heads if x.page == i]
        items: list[DocBlock] = []
        lines_all = ptext.group_lines(page_lines[i])
        for blk in reading_order(ptext.group_blocks(lines_all)):
            para: list[str] = []
            para_top = blk.rect.top
            cur_item: DocBlock | None = None
            item_x0 = 0.0

            def flush() -> None:
                nonlocal para, cur_item
                if para:
                    items.append(DocBlock("para", i, para_top, _join_lines(para)))
                    para = []
                cur_item = None

            skip_until = -1.0
            for ln in blk.lines:
                text_ = ln.text.strip()
                r = ln.rect
                if not text_ or r.top < skip_until:
                    continue
                in_band = r.top < 0.07 * h or r.bottom > 0.93 * h
                if in_band and (_norm(text_) in running or _PAGENO.match(text_)):
                    continue
                hd = next((x for x in hs if abs(x.top - r.top) < max(2.0, 0.3 * x.size)), None)
                if hd is not None:
                    flush()
                    items.append(DocBlock("heading", i, r.top, re.sub(r"\s+", " ", hd.text).strip(), level=lvl_of.get(id(hd), 1)))
                    skip_until = hd.bottom - 1
                    continue
                mb, mn = _BULLET.match(text_), _NUMBER.match(text_)
                if mb or (mn and len(text_) < 400):
                    flush()
                    cur_item = DocBlock("bullet" if mb else "number", i, r.top, (mb or mn).group(2))  # type: ignore[union-attr]
                    items.append(cur_item)
                    item_x0 = r.x0
                    continue
                if cur_item is not None and r.x0 > item_x0 + 2:
                    cur_item.text = _join_lines([cur_item.text, text_])
                    continue
                if cur_item is not None:
                    cur_item = None
                if not para:
                    para_top = r.top
                para.append(text_)
            flush()
        extras: list[DocBlock] = [DocBlock("table", i, t.bbox.top, rows=t.rows) for t in tables if t.page == i]
        if with_images:
            pdf = pdfium.PdfDocument(str(src), password=password)
            try:
                extras += _page_images(pdf, i)
            finally:
                pdf.close()
        for ex in sorted(extras, key=lambda e: e.top):
            pos = next((k for k, it in enumerate(items) if it.top > ex.top), len(items))
            items.insert(pos, ex)
        blocks += items

    title = ""
    try:
        with pikepdf.open(str(src), password=password or "") as pk:
            title = str(pk.docinfo.get("/Title", "") or "").strip()
    except Exception:
        pass
    if not title:
        first = next((b for b in blocks if b.kind == "heading"), None)
        title = first.text if first else src.stem
    return title, blocks


# ---------------------------------------------------------------- writers


def _md_inline(s: str) -> str:
    return re.sub(r"([\\`*_\[\]<>|])", r"\\\1", s)


def _rect_rows(rows: list[list[str]]) -> list[list[str]]:
    n = max((len(r) for r in rows), default=0)
    return [[(c or "").replace("\n", " ").strip() for c in r] + [""] * (n - len(r)) for r in rows]


def _numeric_cols(rows: list[list[str]]) -> list[bool]:
    """Columns whose body cells are all numbers (or blank) — set flush right."""
    from pdfcore.ops.extract import to_number

    n = len(rows[0]) if rows else 0
    out = []
    for c in range(n):
        vals = [r[c] for r in rows[1:] if r[c].strip()]
        out.append(bool(vals) and all(to_number(v) is not None for v in vals))
    return out


def pdf_to_markdown(src: str | Path, *, pages=None, out: str | Path | None = None, password: str | None = None,
                    quiet: bool = False) -> OpResult:
    """Markdown with headings, paragraphs, lists, pipe tables; images go to ``<stem>_files/`` beside it."""
    src = Path(src)
    title, blocks = read_structure(src, pages=pages, password=password)
    dst = single_target(src, ".md", out)
    img_dir = dst.with_name(f"{dst.stem}_files")
    lines: list[str] = []
    files: list[Path] = []
    prev = ""
    n_img = 0
    for bl in blocks:
        if bl.kind in ("bullet", "number"):
            if prev not in ("bullet", "number"):
                lines.append("")
            lines.append(("- " if bl.kind == "bullet" else "1. ") + _md_inline(bl.text))
        elif bl.kind == "heading":
            lines += ["", "#" * bl.level + " " + _md_inline(bl.text)]
        elif bl.kind == "para":
            lines += ["", _md_inline(bl.text)]
        elif bl.kind == "table" and bl.rows:
            rows = _rect_rows(bl.rows)
            esc = lambda c: c.replace("|", "\\|")  # noqa: E731
            lines += ["", "| " + " | ".join(esc(c) for c in rows[0]) + " |",
                      "|" + "|".join(" ---: " if num else " --- " for num in _numeric_cols(rows)) + "|"]
            lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows[1:]]
        elif bl.kind == "image" and bl.image:
            n_img += 1
            img_dir.mkdir(exist_ok=True)
            p = _unique(img_dir / f"image{n_img:03d}{bl.image_ext}")
            p.write_bytes(bl.image)
            files.append(p)
            lines += ["", f"![image {n_img}]({img_dir.name}/{p.name})"]
        prev = bl.kind
    text_ = "\n".join(lines).strip() + "\n"
    dst.write_text(text_, encoding="utf-8")
    counts = {k: sum(1 for b in blocks if b.kind == k) for k in ("heading", "para", "bullet", "number", "table", "image")}
    res = done([dst, *files], {"title": title, "counts": counts}, quiet=True)
    if not quiet:
        print(f"{dst}  PASS  ({', '.join(f'{v} {k}' for k, v in counts.items() if v)})")
    return res


_HTML_CSS = """body{font-family:'Source Serif 4',Georgia,serif;max-width:46rem;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#1b2129}
h1,h2,h3,h4{font-family:Inter,Helvetica,Arial,sans-serif;color:#1d4a6e;line-height:1.25}
table{border-collapse:collapse;margin:1rem 0;font-family:Inter,Helvetica,Arial,sans-serif;font-size:.9rem}
th,td{border-bottom:1px solid #c7cfd8;padding:.3rem .6rem;text-align:left;vertical-align:top}
th{border-bottom:1.5px solid #1b2129}img{max-width:100%;height:auto}figure{margin:1rem 0}"""


def pdf_to_html(src: str | Path, *, pages=None, out: str | Path | None = None, password: str | None = None,
                quiet: bool = False) -> OpResult:
    """One self-contained HTML file: semantic headings, paragraphs, lists, tables, images as data URIs."""
    src = Path(src)
    title, blocks = read_structure(src, pages=pages, password=password)
    dst = single_target(src, ".html", out)
    e = lambda s: html.escape(_XML_BAD.sub("", s))  # noqa: E731
    body: list[str] = []
    open_list = ""
    for bl in blocks:
        want = {"bullet": "ul", "number": "ol"}.get(bl.kind, "")
        if open_list and want != open_list:
            body.append(f"</{open_list}>")
            open_list = ""
        if want and not open_list:
            body.append(f"<{want}>")
            open_list = want
        if want:
            body.append(f"<li>{e(bl.text)}</li>")
        elif bl.kind == "heading":
            body.append(f"<h{bl.level}>{e(bl.text)}</h{bl.level}>")
        elif bl.kind == "para":
            body.append(f"<p>{e(bl.text)}</p>")
        elif bl.kind == "table" and bl.rows:
            rows = _rect_rows(bl.rows)
            num = _numeric_cols(rows)
            al = lambda k: ' style="text-align:right"' if num[k] else ""  # noqa: E731
            t = ["<table>", "<thead><tr>" + "".join(f"<th{al(k)}>{e(c)}</th>" for k, c in enumerate(rows[0]))
                 + "</tr></thead>", "<tbody>"]
            t += ["<tr>" + "".join(f"<td{al(k)}>{e(c)}</td>" for k, c in enumerate(r)) + "</tr>" for r in rows[1:]]
            t += ["</tbody>", "</table>"]
            body.append("\n".join(t))
        elif bl.kind == "image" and bl.image:
            mime = "image/jpeg" if bl.image_ext == ".jpg" else "image/png"
            uri = f"data:{mime};base64," + base64.b64encode(bl.image).decode("ascii")
            body.append(f'<figure><img src="{uri}" alt="" width="{round(bl.width_pt * 4 / 3)}"/></figure>')
    if open_list:
        body.append(f"</{open_list}>")
    doc = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8"/>',
           '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
           f"<title>{e(title)}</title>", f"<style>{_HTML_CSS}</style>", "</head>", "<body>", "<main>",
           *body, "</main>", "</body>", "</html>"]
    dst.write_text("\n".join(doc) + "\n", encoding="utf-8")
    return done([dst], {"title": title, "blocks": len(blocks)}, quiet)


def pdf_to_docx(src: str | Path, *, pages=None, out: str | Path | None = None, password: str | None = None,
                quiet: bool = False) -> OpResult:
    """Word document (python-docx, MIT): headings, paragraphs, bullet/numbered lists, tables, images."""
    try:
        import docx
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches, Pt
    except ImportError as exc:
        raise RuntimeError("PDF → DOCX needs python-docx (MIT): pip install 'pdf-studio[docx]'") from exc
    src = Path(src)
    title, blocks = read_structure(src, pages=pages, password=password)
    dst = single_target(src, ".docx", out)
    d = docx.Document()
    d.core_properties.title = title
    d.core_properties.comments = f"Converted from {src.name} by pdfcore"
    st = d.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(11)
    for bl in blocks:
        if bl.kind == "heading":
            d.add_heading(bl.text, level=min(bl.level, 9))
        elif bl.kind == "para":
            d.add_paragraph(bl.text)
        elif bl.kind == "bullet":
            d.add_paragraph(bl.text, style="List Bullet")
        elif bl.kind == "number":
            d.add_paragraph(bl.text, style="List Number")
        elif bl.kind == "table" and bl.rows:
            rows = _rect_rows(bl.rows)
            t = d.add_table(rows=len(rows), cols=len(rows[0]))
            t.style = "Table Grid"
            num = _numeric_cols(rows)
            for ri, r in enumerate(rows):
                for ci, v in enumerate(r):
                    cell = t.cell(ri, ci)
                    cell.text = v
                    if num[ci]:
                        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
                    if ri == 0:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
            d.add_paragraph()
        elif bl.kind == "image" and bl.image:
            w = min(6.5, max(0.5, bl.width_pt / 72))
            try:
                d.add_picture(io.BytesIO(bl.image), width=Inches(w))
            except Exception:
                from PIL import Image

                b2 = io.BytesIO()
                Image.open(io.BytesIO(bl.image)).convert("RGB").save(b2, "PNG")
                d.add_picture(b2, width=Inches(w))
    d.save(str(dst))
    counts = {k: sum(1 for b in blocks if b.kind == k) for k in ("heading", "para", "bullet", "number", "table", "image")}
    res = done([dst], {"title": title, "counts": counts}, quiet=True)
    if not quiet:
        print(f"{dst}  PASS  ({', '.join(f'{v} {k}' for k, v in counts.items() if v)})")
    return res


def pdf_to_csv(src: str | Path, *, pages=None, out: str | Path | None = None, password: str | None = None,
               quiet: bool = False) -> OpResult:
    """Tables → CSV: one table → ``<stem>.csv``; several → ``<stem>_tables/<stem>_pNNN_tNN.csv``."""
    from pdfcore.ops.extract import _out_dir, find_tables

    src = Path(src)
    found = find_tables(src, pages=pages, password=password)
    if not found:
        raise ValueError(f"no tables found in {src.name} (try --to txt, or pdfcore tables --strategy text)")
    files: list[Path] = []
    if len(found) == 1:
        files = [single_target(src, ".csv", out)]
    else:
        if out is not None and Path(out).suffix.lower() == ".csv" and not Path(out).is_dir():
            folder, base = Path(out).parent, Path(out).stem
            folder.mkdir(parents=True, exist_ok=True)
        else:
            folder, base = _out_dir(src, "tables", out), src.stem
        per: dict[int, int] = {}
        for t in found:
            per[t.page] = per.get(t.page, 0) + 1
            files.append(_unique(folder / f"{base}_p{t.page + 1:03d}_t{per[t.page]:02d}.csv"))
    for t, p in zip(found, files):
        with p.open("w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(t.rows)
    return done(files, {"count": len(found), "tables": [t.to_dict() for t in found]}, quiet)
