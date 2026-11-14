"""OCR: scanned PDF → searchable PDF.

Per page:

1. skip pages that already carry real text (unless ``force``);
2. render with pypdfium2 at ``dpi`` (300 by default);
3. auto-rotate 90/180/270 with Tesseract orientation detection (``--psm 0``);
4. deskew small angles (−5…+5°, refined to 0.1°) with a projection-profile variance search in numpy;
5. optionally clean (autocontrast + light despeckle);
6. run Tesseract for word boxes (TSV via ``pytesseract.image_to_data``);
7. rebuild the page as the corrected image plus an invisible text layer (render mode 3) in the
   embedded house font, each word placed on its box and stretched with ``Tz`` so selection and search
   highlights land on the printed words.

``keep_original=True`` leaves the page content untouched (no rotation of pixels, no deskew, no
re-encoding) and only lays the text on top; a 90/180/270 correction is then applied with ``/Rotate``.
"""

from __future__ import annotations

import io
import math
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pikepdf
from PIL import Image, ImageFilter, ImageOps

from pdfcore import fonts
from pdfcore.gate import GateOptions
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.result import OpResult

__all__ = ["ocr", "has_text", "detect_skew", "detect_orientation", "PageReport"]

OCR_FONT = "Inter"
# Inter's descender (hhea) as a fraction of the em; pdfminer puts a glyph box at [descent, descent + size].
_DESCENT = 0.2421875


@dataclass
class PageReport:
    page: int  # 1-based
    status: str  # "ocr" | "skipped-text" | "skipped-blank" | "not-selected"
    words: int = 0
    confidence: float | None = None  # mean Tesseract word confidence 0–100
    rotation: int = 0  # degrees clockwise applied to make the page upright
    deskew: float = 0.0  # degrees counter-clockwise applied (PIL convention)
    orientation_conf: float | None = None
    image: str = ""  # encoding used for the page image

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


# ---------------------------------------------------------------- detection helpers
def has_text(pdf_doc, index: int, min_chars: int = 12) -> bool:
    """True if page *index* of an open pypdfium2 document already has ≥ min_chars non-space characters."""
    page = pdf_doc[index]
    try:
        tp = page.get_textpage()
        txt = tp.get_text_range() or ""
        tp.close()
    finally:
        page.close()
    return sum(1 for ch in txt if not ch.isspace()) >= min_chars


def _ink_points(gray: np.ndarray, max_points: int = 400_000) -> np.ndarray:
    """(N, 2) array of (x, y) for dark pixels, centred on the image, subsampled if huge."""
    thr = min(160.0, float(np.percentile(gray, 50)) - 40)
    ys, xs = np.nonzero(gray < thr)
    if xs.size == 0:
        return np.empty((0, 2))
    if xs.size > max_points:
        step = xs.size // max_points + 1
        xs, ys = xs[::step], ys[::step]
    h, w = gray.shape
    return np.stack([xs - w / 2.0, ys - h / 2.0], axis=1).astype(np.float64)


def _profile_score(pts: np.ndarray, angle_deg: float, h: int) -> float:
    a = math.radians(angle_deg)
    # Row each ink pixel lands on after PIL's rotate(angle) (counter-clockwise on screen, y down).
    rows = -pts[:, 0] * math.sin(a) + pts[:, 1] * math.cos(a)
    idx = np.round(rows + 2 * h).astype(np.int64)
    idx = idx[(idx >= 0)]
    hist = np.bincount(idx)
    return float(np.var(hist.astype(np.float64)))


def detect_skew(img: Image.Image, limit: float = 5.0, step: float = 0.5, fine: float = 0.1) -> float:
    """Angle (degrees, PIL convention: positive = counter-clockwise) that straightens text lines.

    Projection-profile method: text lines produce sharp peaks in the row histogram of ink pixels when
    they are horizontal; we pick the rotation that maximises the histogram's variance. Coarse search
    over ±limit, then a ±step neighbourhood at *fine* resolution. Works on a ~100 dpi copy.
    """
    g = img.convert("L")
    scale = 1000 / max(g.size)
    if scale < 1:
        g = g.resize((max(1, int(g.width * scale)), max(1, int(g.height * scale))), Image.BILINEAR)
    arr = np.asarray(g, dtype=np.float32)
    pts = _ink_points(arr)
    if len(pts) < 200:
        return 0.0
    h = arr.shape[0]
    coarse = np.arange(-limit, limit + 1e-9, step)
    scores = [_profile_score(pts, a, h) for a in coarse]
    best = float(coarse[int(np.argmax(scores))])
    finer = np.arange(best - step, best + step + 1e-9, fine)
    scores = [_profile_score(pts, a, h) for a in finer]
    best = float(finer[int(np.argmax(scores))])
    base = _profile_score(pts, 0.0, h)
    # Only report a skew when it clearly improves line separation.
    if abs(best) < fine / 2 or max(scores) < base * 1.02:
        return 0.0
    return round(best, 2)


def detect_orientation(img: Image.Image, min_conf: float = 1.5) -> tuple[int, float | None]:
    """Clockwise rotation (0/90/180/270) Tesseract OSD says makes the page upright, and its confidence."""
    import pytesseract

    try:
        small = img.convert("L")
        if max(small.size) > 2600:
            f = 2600 / max(small.size)
            small = small.resize((int(small.width * f), int(small.height * f)), Image.BILINEAR)
        osd = pytesseract.image_to_osd(small, config="--psm 0 -c min_characters_to_try=5", output_type=pytesseract.Output.DICT)
    except Exception:
        return 0, None
    rot = int(osd.get("rotate", 0) or 0) % 360
    conf = float(osd.get("orientation_conf", 0) or 0)
    if rot not in (90, 180, 270) or conf < min_conf:
        return 0, conf
    return rot, conf


def _clean(img: Image.Image) -> Image.Image:
    out = ImageOps.autocontrast(img, cutoff=1)
    return out.filter(ImageFilter.MedianFilter(3))


# ---------------------------------------------------------------- image encoding
def _classify(img: Image.Image) -> str:
    """'color', 'gray' or 'bilevel'."""
    rgb = img.convert("RGB")
    small = np.asarray(rgb.resize((max(1, rgb.width // 4), max(1, rgb.height // 4))), dtype=np.int16)
    chroma = np.max(np.abs(small[..., 0] - small[..., 1])) if small.size else 0
    chroma = max(chroma, np.max(np.abs(small[..., 1] - small[..., 2])) if small.size else 0)
    if np.percentile(np.abs(small[..., 0] - small[..., 2]), 99.5) > 18 or chroma > 60:
        return "color"
    g = np.asarray(rgb.convert("L"))
    mid = np.count_nonzero((g > 24) & (g < 231))
    return "bilevel" if mid / max(1, g.size) < 0.002 else "gray"


def _image_xobject(pdf: pikepdf.Pdf, img: Image.Image, kind: str, quality: int) -> tuple[pikepdf.Stream, str]:
    if kind == "bilevel":
        bits = np.asarray(img.convert("L")) > 127  # True = white = 1 in DeviceGray
        packed = np.packbits(bits, axis=1)
        st = pikepdf.Stream(pdf, zlib.compress(packed.tobytes(), 9))
        st.Filter = pikepdf.Name.FlateDecode
        st.ColorSpace = pikepdf.Name.DeviceGray
        st.BitsPerComponent = 1
        enc = "flate-1bit"
    else:
        mode = "RGB" if kind == "color" else "L"
        buf = io.BytesIO()
        img.convert(mode).save(buf, "JPEG", quality=quality, optimize=True)
        st = pikepdf.Stream(pdf, buf.getvalue())
        st.Filter = pikepdf.Name.DCTDecode
        st.ColorSpace = pikepdf.Name.DeviceRGB if kind == "color" else pikepdf.Name.DeviceGray
        st.BitsPerComponent = 8
        enc = f"jpeg-{mode.lower()}-q{quality}"
    st.Type = pikepdf.Name.XObject
    st.Subtype = pikepdf.Name.Image
    st.Width, st.Height = img.width, img.height
    return st, enc


def _native_dpi(path: Path, index: int, password: str | None) -> float | None:
    """Resolution of the largest image on the page (pixels per inch at its drawn size), if any."""
    import pdfplumber

    try:
        with pdfplumber.open(str(path), password=password or "") as doc:
            pg = doc.pages[index]
            best = None
            for im in pg.images:
                w = float(im["x1"]) - float(im["x0"])
                sw = (im.get("srcsize") or (0, 0))[0]
                if w > 0 and sw and (best is None or w > best[0]):
                    best = (w, sw / (w / 72.0))
            if best and best[0] > 0.6 * float(pg.width):
                return best[1]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- tesseract
@dataclass
class _OcrWord:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float
    line_key: tuple[int, int, int]


def _tesseract_words(img: Image.Image, lang: str, psm: int, min_conf: float) -> list[_OcrWord]:
    import pytesseract

    d = pytesseract.image_to_data(img, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    out: list[_OcrWord] = []
    for i, txt in enumerate(d["text"]):
        t = (txt or "").strip()
        try:
            conf = float(d["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not t or conf < 0 or conf < min_conf:
            continue
        out.append(
            _OcrWord(t, int(d["left"][i]), int(d["top"][i]), int(d["width"][i]), int(d["height"][i]), conf,
                     (int(d["block_num"][i]), int(d["par_num"][i]), int(d["line_num"][i])))
        )
    return out


def _font_ok_chars() -> set[int]:
    from reportlab.pdfbase import pdfmetrics

    try:
        f = pdfmetrics.getFont(OCR_FONT)
        return set(f.face.charToGlyph.keys())
    except Exception:
        return set()


def _draw_text_layer(c, words: list[_OcrWord], img_w: int, img_h: int, W: float, H: float, cover: set[int]) -> None:
    """Invisible text: one Tm + Tz per word; font size and baseline from the Tesseract line box."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    sx, sy = W / img_w, H / img_h
    lines: dict[tuple[int, int, int], list[_OcrWord]] = {}
    for w in words:
        lines.setdefault(w.line_key, []).append(w)
    for ws in lines.values():
        ws.sort(key=lambda w: w.left)
        top = min(w.top for w in ws) * sy
        bottom = max(w.top + w.height for w in ws) * sy
        size = max(1.0, bottom - top)
        baseline = H - bottom + _DESCENT * size
        for k, w in enumerate(ws):
            text = "".join(ch if ord(ch) in cover or not cover else "?" for ch in w.text)
            x0 = w.left * sx
            x1 = min((w.left + w.width) * sx, W - 0.5)
            natural = stringWidth(text, OCR_FONT, size)
            if natural <= 0 or x1 <= x0:
                continue
            scale = max(1.0, min(1000.0, 100.0 * (x1 - x0) / natural))
            t = c.beginText()
            t.setTextRenderMode(3)
            t.setFont(OCR_FONT, size)
            t.setHorizScale(scale)
            t.setTextOrigin(x0, baseline)
            # A trailing space keeps words apart for extractors that ignore position gaps; skip it at line end
            # and where it would run past the page edge.
            gap_ok = k + 1 < len(ws) and x1 + stringWidth(" ", OCR_FONT, size) * scale / 100 < W - 1
            t.textOut(text + (" " if gap_ok else ""))
            c.drawText(t)


# ---------------------------------------------------------------- main op
def ocr(
    src: str | Path,
    *,
    out: str | Path | None = None,
    language: str | Sequence[str] = "eng",
    dpi: int = 300,
    pages: str | None = None,
    force: bool = False,
    keep_original: bool = False,
    rotate: bool = True,
    deskew: bool = True,
    clean: bool = False,
    psm: int = 3,
    min_conf: float = 0.0,
    jpeg_quality: int = 80,
    image_dpi: float | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Make *src* searchable. Returns OpResult with ``data['pages']`` = per-page PageReport dicts.

    ``language``: Tesseract code(s), e.g. "eng" or ["eng", "deu"] (joined with '+').
    ``image_dpi``: resolution of the rebuilt page image (default: the scan's native resolution, capped at dpi).
    """
    import pypdfium2 as pdfium
    import pytesseract

    src = Path(src)
    lang = language if isinstance(language, str) else "+".join(language)
    try:
        have = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # tesseract missing
        raise FileNotFoundError(f"tesseract not available: {exc} (run pdfcore install)") from exc
    missing = [l for l in lang.split("+") if l and l not in have]
    if missing:
        raise ValueError(f"Tesseract language data not installed: {', '.join(missing)} (have: {', '.join(sorted(have))})")
    fonts.register_reportlab()
    cover = _font_ok_chars()
    dst = output_path(src, "ocr", out=out)

    doc = pdfium.PdfDocument(str(src), password=password)
    pdf = pikepdf.open(str(src), password=password or "")
    n = len(pdf.pages)
    sel = set(parse_pages(pages, n))
    reports: list[PageReport] = []
    layers: dict[int, tuple[list[_OcrWord], int, int]] = {}  # page → words, img size (for keep_original)
    try:
        for i in range(n):
            rep = PageReport(i + 1, "not-selected")
            reports.append(rep)
            if i not in sel:
                continue
            if not force and has_text(doc, i):
                rep.status = "skipped-text"
                continue
            page = doc[i]
            img = page.render(scale=dpi / 72.0, draw_annots=False).to_pil().convert("RGB")
            page.close()
            gray = img.convert("L")
            lo, hi = gray.getextrema()
            if hi - lo < 16:
                rep.status = "skipped-blank"
                continue
            if rotate:
                rot, oconf = detect_orientation(gray)
                rep.orientation_conf = oconf
                if rot:
                    img = img.rotate(-rot, expand=True)  # PIL rotates counter-clockwise
                    rep.rotation = rot
            if deskew and not keep_original:
                ang = detect_skew(img)
                if ang:
                    img = img.rotate(ang, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))
                    rep.deskew = ang
            if clean and not keep_original:
                img = _clean(img)
            ocr_in = _clean(img) if clean and keep_original else img
            words = _tesseract_words(ocr_in.convert("L"), lang, psm, min_conf)
            rep.status = "ocr"
            rep.words = len(words)
            rep.confidence = round(sum(w.conf for w in words) / len(words), 1) if words else None
            layers[i] = (words, img.width, img.height)
            if not keep_original:
                _rebuild_page(pdf, src, i, img, dpi, image_dpi, jpeg_quality, rep, password)
            elif rep.rotation:
                p = pdf.pages[i]
                p.obj.Rotate = (int(p.obj.get("/Rotate", 0) or 0) + rep.rotation) % 360
    finally:
        doc.close()

    if layers:
        from pdfcore.overlay import stamp_pages

        def draw(c, i, w, h):
            ws, iw, ih = layers[i]
            _draw_text_layer(c, ws, iw, ih, w, h, cover)

        stamp_pages(pdf, draw, pages=sorted(layers), tag="OCRText")

    pdf.save(str(dst), compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    pdf.close()
    page_data = [r.as_dict() for r in reports]
    done = [r for r in reports if r.status == "ocr"]
    data = {
        "pages": page_data,
        "ocr_pages": len(done),
        "skipped_text": sum(1 for r in reports if r.status == "skipped-text"),
        "language": lang,
        "dpi": dpi,
        "mean_confidence": round(sum(r.confidence or 0 for r in done) / len(done), 1) if done else None,
    }
    return finalize(dst, baseline=src, options=options, password=password, data=data, quiet=quiet)


def _rebuild_page(pdf: pikepdf.Pdf, src: Path, i: int, img: Image.Image, dpi: int, image_dpi: float | None,
                  quality: int, rep: PageReport, password: str | None) -> None:
    """Replace page *i*'s content with *img* filling an upright page of the same physical size."""
    page = pdf.pages[i]
    from pdfcore.overlay import visual_size

    vw, vh = visual_size(page)
    if rep.rotation in (90, 270):
        vw, vh = vh, vw
    target = image_dpi or _native_dpi(src, i, password) or dpi
    target = max(72.0, min(float(dpi), float(target)))
    if target < dpi - 1:
        size = (max(1, round(vw * target / 72.0)), max(1, round(vh * target / 72.0)))
        out_img = img.resize(size, Image.LANCZOS)
    else:
        out_img = img
    kind = _classify(out_img)
    xobj, enc = _image_xobject(pdf, out_img, kind, quality)
    rep.image = f"{enc} {out_img.width}x{out_img.height}"
    page.obj.MediaBox = pikepdf.Array([0, 0, round(vw, 4), round(vh, 4)])
    for k in ("/CropBox", "/TrimBox", "/BleedBox", "/ArtBox", "/Rotate"):
        if k in page.obj:
            del page.obj[k]
    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xobj))
    page.obj.Contents = pdf.make_stream(f"q {vw:.4f} 0 0 {vh:.4f} 0 0 cm /Im0 Do Q\n".encode())


# ---------------------------------------------------------------- CLI
@command("ocr", "Make a scanned PDF searchable (auto-rotate, deskew, invisible text layer)", group="ocr")
def configure(p):
    p.add_argument("pdf")
    p.add_argument("-l", "--lang", default="eng", help="Tesseract language(s), e.g. eng or eng+deu")
    p.add_argument("--dpi", type=int, default=300, help="render resolution for OCR (default 300)")
    p.add_argument("--pages", help="page range, e.g. 1-3,7 (default all)")
    p.add_argument("--force", action="store_true", help="OCR pages that already contain text")
    p.add_argument("--keep-original", action="store_true", help="keep the page image untouched; only add the text layer")
    p.add_argument("--no-rotate", action="store_true", help="skip automatic 90/180/270 orientation fix")
    p.add_argument("--no-deskew", action="store_true", help="skip small-angle deskew")
    p.add_argument("--clean", action="store_true", help="boost contrast and despeckle the page image")
    p.add_argument("--psm", type=int, default=3, help="Tesseract page segmentation mode (default 3)")
    p.add_argument("--image-dpi", type=float, help="resolution of the rebuilt page image (default: native scan dpi)")
    p.add_argument("--jpeg-quality", type=int, default=80)
    p.add_argument("--json", action="store_true", help="print per-page report as JSON")
    add_common(p)

    def run(ns):
        res = ocr(ns.pdf, out=ns.out, language=ns.lang, dpi=ns.dpi, pages=ns.pages, force=ns.force,
                  keep_original=ns.keep_original, rotate=not ns.no_rotate, deskew=not ns.no_deskew, clean=ns.clean,
                  psm=ns.psm, image_dpi=ns.image_dpi, jpeg_quality=ns.jpeg_quality, password=ns.password,
                  options=gate_options_from(ns))
        if ns.json:
            import json

            print(json.dumps(res.data, indent=2))
        else:
            for r in res.data["pages"]:
                if r["status"] == "ocr":
                    print(f"  p.{r['page']}: {r['words']} words, conf {r['confidence']}, rotated {r['rotation']}°, deskew {r['deskew']}°")
                elif r["status"] != "not-selected":
                    print(f"  p.{r['page']}: {r['status']}")
        return res

    return run
