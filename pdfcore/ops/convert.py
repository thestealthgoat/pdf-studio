"""Conversions — one front door, ``pdfcore convert IN… [--to FMT | -o OUT.ext]`` (``--list`` prints the matrix).

Into PDF
* Office documents (docx/xlsx/pptx/odt/ods/odp/rtf/csv…) through LibreOffice, run as an external program
  (``soffice --headless --convert-to pdf``) with a throwaway user profile so it works while LibreOffice is open.
* Images (PNG, JPEG any case/.jpe, TIFF incl. multi-page, GIF, BMP, WebP, AVIF, JPEG 2000) with Pillow + pikepdf.
  Page size comes from the image DPI; JPEGs are embedded byte-for-byte, others losslessly, 1-bit stays 1-bit.
  HEIC/HEIF through macOS ``sips`` (pillow-heif/libheif are LGPL and never used).
* SVG as vectors through Typst. DXF/DWG/HPGL through :func:`pdfcore.ops.plans.cad.dxf_to_pdf`.
* Markdown and plain text typeset in the house style (Typst; :mod:`pdfcore.ops.markdown`).
* HTML through WeasyPrint when installed, else LibreOffice.

Out of PDF (:mod:`pdfcore.ops.export`): PNG/JPEG/TIFF/WebP/GIF/BMP/AVIF/JPEG 2000, SVG, DXF, DWG, DOCX,
XLSX, CSV, Markdown, HTML, TXT, JSON. Image ↔ image with Pillow (sips for HEIC); DXF ↔ DWG through the
``dwg`` engine. Any other pair goes through a temporary PDF (e.g. DOCX → PNG, DXF → SVG).

Every PDF written ends with the quality gate (``finalize``); other outputs are opened again to verify them.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Any, Callable, Sequence

from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from
from pdfcore.ops.extract import render
from pdfcore.result import OpResult, output_path

__all__ = [
    "OFFICE_EXTS", "IMAGE_EXTS", "HTML_EXTS", "SVG_EXTS", "HEIC_EXTS", "CAD_EXTS", "MD_EXTS", "TEXT_EXTS",
    "find_soffice", "office_to_pdf", "images_to_pdf", "html_to_pdf", "svg_to_pdf", "heic_to_pdf", "cad_to_pdf",
    "pdf_to_images", "to_pdf", "convert", "convert_many", "fmt_of", "canonical", "matrix", "dwg_writer",
    "image_to_image",
]

OFFICE_EXTS = {
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".ott", ".rtf", ".txt", ".wpd", ".pages",
    ".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv",
    ".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".key",
    ".odg", ".vsd", ".vsdx", ".pub",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".avif",
              ".jp2", ".j2k", ".jpf", ".jpx", ".j2c"}
HEIC_EXTS = {".heic", ".heif", ".hif"}
HTML_EXTS = {".html", ".htm", ".xhtml"}
SVG_EXTS = {".svg", ".svgz"}
CAD_EXTS = {".dxf", ".dwg", ".plt", ".hpgl", ".hpg", ".hgl", ".hp2"}
MD_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
TEXT_EXTS = {".txt", ".text"}

PAGE_SIZES = {"letter": (612.0, 792.0), "legal": (612.0, 1008.0), "tabloid": (792.0, 1224.0),
              "a4": (595.28, 841.89), "a3": (841.89, 1190.55)}


def _pdf_out(src: Path, out: str | Path | None) -> Path:
    """``report.docx`` → ``report.pdf`` when that name is free, else ``report.converted.pdf``; never overwrites."""
    if out is not None:
        p = output_path(src, "converted", ".pdf", out=out)
    else:
        p = src.with_suffix(".pdf")
        if p.exists() or p.resolve() == src.resolve():
            p = output_path(src, "converted", ".pdf")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- office → PDF


def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice", "/opt/homebrew/bin/soffice",
              "/usr/local/bin/soffice", "/usr/bin/soffice", "/usr/lib/libreoffice/program/soffice",
              r"C:\Program Files\LibreOffice\program\soffice.exe"):
        if Path(p).exists():
            return p
    return None


def office_to_pdf(
    src: str | Path,
    *,
    out: str | Path | None = None,
    timeout: float = 180,
    soffice: str | None = None,
    options=None,
    quiet: bool = False,
) -> OpResult:
    """Convert an office/text document to PDF with LibreOffice (headless, private profile, timeout)."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    exe = soffice or find_soffice()
    if not exe:
        raise RuntimeError("LibreOffice not found (soffice). Install it: `pdfcore install` or https://www.libreoffice.org")
    dest = _pdf_out(src, out)
    with tempfile.TemporaryDirectory(prefix="pdfcore-lo-") as tmp:
        tmpd = Path(tmp)
        profile, outdir = tmpd / "profile", tmpd / "out"
        outdir.mkdir()
        cmd = [exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore", "--nologo",
               "--nolockcheck", "--convert-to", "pdf", "--outdir", str(outdir), str(src.resolve())]
        env = {**os.environ, "HOME": os.environ.get("HOME", tmp)}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"LibreOffice timed out after {timeout:g}s converting {src.name}") from exc
        except OSError as exc:
            raise RuntimeError(f"cannot run LibreOffice ({exe}): {exc}") from exc
        made = list(outdir.glob("*.pdf"))
        if not made:
            msg = (proc.stderr or proc.stdout or "").strip().splitlines()
            msg = [m for m in msg if "Fontconfig" not in m]
            raise RuntimeError(f"LibreOffice could not convert {src.name}: {msg[-1] if msg else f'exit {proc.returncode}'}")
        shutil.move(str(made[0]), dest)
    return finalize(dest, baseline=None, options=options, quiet=quiet, data={"converter": "libreoffice"})


# ---------------------------------------------------------------- images → PDF


def _img_dpi(im, default: float) -> tuple[float, float]:
    d = im.info.get("dpi")
    try:
        x, y = float(d[0]), float(d[1])
        if x >= 10 and y >= 10:
            # PNG stores pixels per metre: 100 dpi comes back as 99.9998
            return tuple(float(round(v)) if abs(v - round(v)) < 0.01 else v for v in (x, y))  # type: ignore[return-value]
    except (TypeError, ValueError, IndexError):
        pass
    return default, default


def _exif_rotation(im) -> tuple[int, bool]:
    """(clockwise degrees to display, mirrored) from EXIF orientation."""
    try:
        o = im.getexif().get(0x0112, 1)
    except Exception:
        o = 1
    return {1: (0, False), 3: (180, False), 6: (90, False), 8: (270, False)}.get(o, (0, o in (2, 4, 5, 7)))


def _image_xobject(pdf, frame, raw_jpeg: bytes | None):
    """Build an image XObject (and SMask) for a PIL frame. Returns (stream, width_px, height_px)."""
    import pikepdf
    from pikepdf import Name, Stream

    w, h = frame.size
    if raw_jpeg is not None and frame.mode in ("L", "RGB", "CMYK"):
        cs = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB, "CMYK": Name.DeviceCMYK}[frame.mode]
        st = Stream(pdf, raw_jpeg)
        st.Type, st.Subtype, st.Width, st.Height = Name.XObject, Name.Image, w, h
        st.ColorSpace, st.BitsPerComponent, st.Filter = cs, 8, Name.DCTDecode
        if frame.mode == "CMYK" and "adobe" in frame.info:  # Adobe/Photoshop CMYK JPEGs are inverted
            st.Decode = pikepdf.Array([1, 0, 1, 0, 1, 0, 1, 0])
        return st
    smask = None
    m = frame.mode
    if m == "P":
        frame = frame.convert("RGBA" if "transparency" in frame.info else "RGB")
    elif m in ("I;16", "I;16B", "I;16L", "I", "F"):
        frame = frame.convert("L")
    elif m in ("LA", "PA"):
        frame = frame.convert("LA")
    elif m not in ("1", "L", "RGB", "RGBA", "CMYK", "LA"):
        frame = frame.convert("RGB")
    if frame.mode in ("RGBA", "LA"):
        alpha = frame.getchannel("A")
        if alpha.getextrema() != (255, 255):
            smask = Stream(pdf, zlib.compress(alpha.tobytes(), 6))
            smask.Type, smask.Subtype, smask.Width, smask.Height = Name.XObject, Name.Image, w, h
            smask.ColorSpace, smask.BitsPerComponent, smask.Filter = Name.DeviceGray, 8, Name.FlateDecode
        frame = frame.convert("RGB" if frame.mode == "RGBA" else "L")
    if frame.mode == "1":
        data = frame.tobytes()  # packed 1 bit/px, 0 = black: matches DeviceGray with BPC 1
        bpc, cs = 1, Name.DeviceGray
    else:
        data = frame.tobytes()
        bpc = 8
        cs = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB, "CMYK": Name.DeviceCMYK}[frame.mode]
    st = Stream(pdf, zlib.compress(data, 6))
    st.Type, st.Subtype, st.Width, st.Height = Name.XObject, Name.Image, w, h
    st.ColorSpace, st.BitsPerComponent, st.Filter = cs, bpc, Name.FlateDecode
    if smask is not None:
        st.SMask = smask
    return st


def images_to_pdf(
    images: Sequence[str | Path] | str | Path,
    *,
    out: str | Path | None = None,
    dpi: float | None = None,
    default_dpi: float = 150,
    page_size: str | tuple[float, float] | None = None,
    margin: float = 0.0,
    options=None,
    quiet: bool = False,
) -> OpResult:
    """Combine images into one PDF, one page per image (every frame of a multi-page TIFF/GIF).

    Page size = pixels ÷ DPI (the file's DPI, or *dpi* to override, or *default_dpi* when the file has
    none). ``page_size`` ('letter', 'a4', … or (w, h) in points) instead fits each image centred on that
    page inside *margin* points, turning the page to landscape for landscape images.
    JPEG data is embedded unchanged; EXIF rotation is honoured via /Rotate.
    """
    import pikepdf
    from PIL import Image, ImageOps, ImageSequence

    paths = [Path(images)] if isinstance(images, (str, Path)) else [Path(p) for p in images]
    if not paths:
        raise ValueError("no images given")
    for p in paths:
        if not p.is_file():
            raise FileNotFoundError(p)
    if isinstance(page_size, str):
        if page_size.lower() not in PAGE_SIZES:
            raise ValueError(f"page_size must be one of {', '.join(PAGE_SIZES)} or (w, h) in points")
        fixed: tuple[float, float] | None = PAGE_SIZES[page_size.lower()]
    else:
        fixed = tuple(page_size) if page_size else None  # type: ignore[assignment]

    if out is None:
        first = paths[0]
        dest = output_path(first, "combined" if len(paths) > 1 else "converted", ".pdf") \
            if first.with_suffix(".pdf").exists() or len(paths) > 1 else first.with_suffix(".pdf")
    else:
        dest = output_path(paths[0], "converted", ".pdf", out=out)
    dest.parent.mkdir(parents=True, exist_ok=True)

    pdf = pikepdf.new()
    n_pages = 0
    for p in paths:
        with Image.open(p) as im:
            fmt = (im.format or "").upper()
            multi = getattr(im, "n_frames", 1) > 1
            rot0, mirrored = _exif_rotation(im)
            for frame in (ImageSequence.Iterator(im) if multi else [im]):
                frame = frame.copy() if multi else frame  # the iterator reuses one object
                rot = rot0
                raw = None
                if fmt == "JPEG" and not multi and not mirrored:
                    raw = p.read_bytes()
                if mirrored:
                    frame = ImageOps.exif_transpose(frame)
                    rot = 0
                xo = _image_xobject(pdf, frame, raw)
                wpx, hpx = frame.size
                xdpi, ydpi = (dpi, dpi) if dpi else _img_dpi(frame if frame.info.get("dpi") else im, default_dpi)
                iw, ih = wpx * 72.0 / xdpi, hpx * 72.0 / ydpi
                disp_w, disp_h = (ih, iw) if rot in (90, 270) else (iw, ih)
                if fixed:
                    pw, ph = fixed
                    if (disp_w > disp_h) != (pw > ph) and abs(disp_w - disp_h) > 1:
                        pw, ph = ph, pw
                    # work in display orientation, then map back to unrotated page space
                    s = min((pw - 2 * margin) / disp_w, (ph - 2 * margin) / disp_h)
                    dw, dh = disp_w * s, disp_h * s
                    if rot in (90, 270):
                        pw, ph = ph, pw
                        dw, dh = dh, dw
                    x, y = (pw - dw) / 2, (ph - dh) / 2
                else:
                    pw, ph = (iw + 2 * margin, ih + 2 * margin)
                    dw, dh, x, y = iw, ih, margin, margin
                page = pdf.add_blank_page(page_size=(pw, ph))
                page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xo))
                page.Contents = pdf.make_stream(f"q {dw:.4f} 0 0 {dh:.4f} {x:.4f} {y:.4f} cm /Im0 Do Q".encode())
                if rot:
                    page.Rotate = rot
                n_pages += 1
    pdf.docinfo["/Title"] = paths[0].stem if len(paths) == 1 else f"{paths[0].stem} (+{len(paths) - 1})"
    pdf.docinfo["/Producer"] = "pdfcore"
    pdf.save(dest)
    pdf.close()
    return finalize(dest, baseline=None, options=options, quiet=quiet, data={"pages": n_pages, "images": len(paths)})


# ---------------------------------------------------------------- HTML → PDF


def html_to_pdf(src: str | Path, *, out: str | Path | None = None, base_url: str | None = None,
                options=None, quiet: bool = False, timeout: float = 180) -> OpResult:
    """Render HTML to PDF with WeasyPrint (BSD) when installed, else LibreOffice."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except ImportError:
        if find_soffice():
            return office_to_pdf(src, out=out, options=options, quiet=quiet, timeout=timeout)
        raise RuntimeError("HTML → PDF needs WeasyPrint (pip install 'pdf-studio[html]') or LibreOffice") from None
    dest = _pdf_out(src, out)
    HTML(filename=str(src), base_url=base_url or str(src.parent)).write_pdf(str(dest))
    return finalize(dest, baseline=None, options=options, quiet=quiet, data={"converter": "weasyprint"})


# ---------------------------------------------------------------- SVG, HEIC, CAD, Markdown, text → PDF


def svg_to_pdf(src: str | Path, *, out: str | Path | None = None, options=None, quiet: bool = False) -> OpResult:
    """SVG → vector PDF through Typst (page = the SVG's size). Text uses the house fonts, then system fonts."""
    import typst

    from pdfcore.fonts import font_dir
    from pdfcore.ops.markdown import typst_str

    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    dest = _pdf_out(src, out)
    with tempfile.TemporaryDirectory(prefix="pdfcore-svg-") as tmp:
        root = Path(tmp)
        name = "in" + src.suffix.lower()
        shutil.copy(src, root / name)
        (root / "main.typ").write_text(
            f"#set document(title: {typst_str(src.stem)})\n"
            "#set page(width: auto, height: auto, margin: 0pt)\n"
            f'#image("/{name}", alt: {typst_str(src.stem)})\n', encoding="utf-8")
        try:
            pdf = typst.compile(str(root / "main.typ"), root=str(root), font_paths=[str(font_dir())])
        except Exception as exc:  # typst raises its own error types with the SVG parser's message
            raise RuntimeError(f"cannot convert {src.name}: {exc}") from exc
        dest.write_bytes(pdf)
    return finalize(dest, baseline=None, options=options, quiet=quiet, data={"converter": "typst"})


def find_sips() -> str | None:
    return shutil.which("sips") or (("/usr/bin/sips") if Path("/usr/bin/sips").exists() else None)


def sips_convert(src: Path, fmt: str, dst: Path, quality: int | None = None) -> Path:
    """macOS ``sips`` image conversion (HEIC/HEIF in and out)."""
    exe = find_sips()
    if not exe:
        raise RuntimeError("HEIC/HEIF needs macOS `sips` (built into macOS). On other systems export the photo as "
                           "JPEG first — pdfcore does not use pillow-heif/libheif (LGPL).")
    cmd = [exe, "-s", "format", fmt]
    if quality is not None and fmt in ("jpeg", "heic"):
        cmd += ["-s", "formatOptions", str(quality)]
    cmd += [str(src), "--out", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not dst.is_file():
        raise RuntimeError(f"sips could not convert {src.name}: {(proc.stderr or proc.stdout).strip()[-200:]}")
    return dst


def heic_to_pdf(src: str | Path, *, out: str | Path | None = None, options=None, quiet: bool = False, **kw) -> OpResult:
    """HEIC/HEIF photo → PDF: decoded by macOS ``sips`` to a high-quality JPEG, then :func:`images_to_pdf`."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    dest = _pdf_out(src, out)
    with tempfile.TemporaryDirectory(prefix="pdfcore-heic-") as tmp:
        jpg = sips_convert(src, "jpeg", Path(tmp) / f"{src.stem}.jpg", quality=95)
        res = images_to_pdf([jpg], out=dest, options=options, quiet=True, **kw)
    if not quiet:
        print(f"{res.output}  {res.line()}")
    return res


def cad_to_pdf(src: str | Path, *, out: str | Path | None = None, quiet: bool = False, **kw) -> OpResult:
    """DXF / DWG / HPGL plot → vector PDF (:func:`pdfcore.ops.plans.cad.dxf_to_pdf`)."""
    from pdfcore.ops.plans.cad import dxf_to_pdf

    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    kw.pop("options", None)
    return dxf_to_pdf(src, out=_pdf_out(src, out), quiet=quiet, **kw)


# ---------------------------------------------------------------- PDF → images, dispatch


def pdf_to_images(src: str | Path, **kw) -> OpResult:
    """Alias of :func:`pdfcore.ops.extract.render`."""
    return render(src, **kw)


def to_pdf(src: str | Path, *, out: str | Path | None = None, quiet: bool = False, **kw) -> OpResult:
    """Convert any supported file to PDF, choosing the converter from the extension (any case)."""
    src = Path(src)
    ext = src.suffix.lower()
    if ext == ".pdf":
        raise ValueError(f"{src.name} is already a PDF (to make images use: pdfcore convert {src.name} --to png)")
    brand = kw.pop("brand", "default")
    if ext in IMAGE_EXTS:
        return images_to_pdf([src], out=out, quiet=quiet, **kw)
    if ext in HEIC_EXTS:
        return heic_to_pdf(src, out=out, quiet=quiet, **kw)
    if ext in SVG_EXTS:
        return svg_to_pdf(src, out=out, quiet=quiet, **kw)
    if ext in CAD_EXTS:
        return cad_to_pdf(src, out=out, quiet=quiet, **kw)
    if ext in MD_EXTS or ext in TEXT_EXTS:
        try:
            import typst  # noqa: F401
        except ImportError:
            if ext in TEXT_EXTS and find_soffice():
                return office_to_pdf(src, out=out, quiet=quiet, **kw)
            raise RuntimeError("Markdown/text → PDF needs Typst: pip install 'pdf-studio[typeset]'") from None
        from pdfcore.ops import markdown

        fn = markdown.markdown_to_pdf if ext in MD_EXTS else markdown.text_to_pdf
        return fn(src, out=out, brand=brand, quiet=quiet)
    if ext in HTML_EXTS:
        return html_to_pdf(src, out=out, quiet=quiet, **kw)
    if ext in OFFICE_EXTS:
        return office_to_pdf(src, out=out, quiet=quiet, **kw)
    raise ValueError(f"don't know how to convert {ext or 'files without an extension'} to PDF (see: pdfcore convert --list)")


# ---------------------------------------------------------------- formats + matrix

ALIASES = {
    "jpeg": "jpg", "jpe": "jpg", "jfif": "jpg", "tif": "tiff", "htm": "html", "xhtml": "html", "markdown": "md",
    "mdown": "md", "mkd": "md", "heif": "heic", "hif": "heic", "j2k": "jp2", "jpf": "jp2", "jpx": "jp2", "j2c": "jp2",
    "jpeg2000": "jp2", "word": "docx", "excel": "xlsx", "text": "txt", "svgz": "svg", "hpgl": "plt", "hpg": "plt",
    "hgl": "plt", "hp2": "plt", "image": "png", "picture": "png", "autocad": "dwg", "acad": "dwg",
}
RASTER_IN = {"png", "jpg", "tiff", "gif", "bmp", "webp", "avif", "jp2", "heic"}
RASTER_OUT = {"png", "jpg", "tiff", "gif", "bmp", "webp", "avif", "jp2", "heic"}
PDF_TARGETS = ["png", "jpg", "tiff", "webp", "gif", "bmp", "avif", "jp2", "svg", "dxf", "dwg", "docx", "xlsx", "csv",
               "md", "html", "txt", "json"]
OUT_EXT = {"jpg": ".jpg", "tiff": ".tif", "jp2": ".jp2", "md": ".md", "html": ".html", "heic": ".heic"}

# (sources, targets, engine) — what --list prints
MATRIX: list[tuple[str, str, str]] = [
    ("png jpg/jpeg/jpe tiff gif bmp webp avif jp2", "pdf", "Pillow + pikepdf (JPEG embedded as-is, multi-page TIFF/GIF)"),
    ("heic heif", "pdf", "macOS sips → Pillow + pikepdf"),
    ("svg", "pdf", "Typst (vector)"),
    ("dxf dwg plt/hpgl", "pdf", "ezdxf + matplotlib (cad.dxf_to_pdf; DWG via ODA/dwg engine)"),
    ("md", "pdf", "Typst house style (report shell)"),
    ("txt", "pdf", "Typst house style (serif, reflowed)"),
    ("html htm", "pdf", "WeasyPrint, else LibreOffice"),
    ("docx doc odt rtf xlsx xls ods csv pptx ppt odp …", "pdf", "LibreOffice (headless)"),
    ("pdf", "png jpg tiff webp gif bmp avif jp2 heic", "pypdfium2 render + Pillow (--dpi, --multipage; heic via sips)"),
    ("pdf", "svg", "pypdfium2 page objects → SVG (paths, <text>, data-URI images)"),
    ("pdf", "dxf", "cad.pdf_to_dxf (ezdxf)"),
    ("pdf", "dwg", "pdf_to_dxf → dwg engine from_dxf (ODA File Converter)"),
    ("pdf", "docx", "python-docx (headings, lists, tables, images)"),
    ("pdf", "md html", "reading-order structure (headings, lists, tables, images)"),
    ("pdf", "xlsx csv", "extract.find_tables + openpyxl"),
    ("pdf", "txt json", "pdfplumber text (reading order / words with boxes)"),
    ("png jpg tiff gif bmp webp avif jp2 heic", "png jpg tiff gif bmp webp avif jp2 heic", "Pillow (sips for HEIC)"),
    ("dwg", "dxf", "dwg engine to_dxf (ODA File Converter or local edition)"),
    ("dxf", "dwg", "dwg engine from_dxf (ODA File Converter)"),
    ("plt/hpgl", "dxf", "cad HPGL/2 reader → ezdxf"),
    ("any → PDF source", "any PDF target", "two steps through a temporary PDF (e.g. docx→png, dxf→svg, md→docx)"),
]


def canonical(fmt: str) -> str:
    f = fmt.lower().lstrip(".").strip()
    return ALIASES.get(f, f)


def fmt_of(path: str | Path) -> str:
    """Canonical format of a file from its extension (case-insensitive)."""
    return canonical(Path(path).suffix)


def _known_source(ext: str) -> bool:
    return ext == ".pdf" or ext in (IMAGE_EXTS | HEIC_EXTS | HTML_EXTS | SVG_EXTS | CAD_EXTS | MD_EXTS | TEXT_EXTS | OFFICE_EXTS)


def matrix() -> str:
    w = max(len(a) for a, _, _ in MATRIX)
    v = max(len(b) for _, b, _ in MATRIX)
    lines = [f"{'SOURCE':<{w}}  {'TARGETS':<{v}}  ENGINE"]
    lines += [f"{a:<{w}}  {b:<{v}}  {c}" for a, b, c in MATRIX]
    lines.append("\nExtensions are case-insensitive. --to accepts aliases: jpeg, tif, word (docx), excel (xlsx), markdown.")
    return "\n".join(lines)


def dwg_writer() -> Any:
    """First installed ``dwg`` engine that can write DWG (has ``from_dxf``)."""
    from pdfcore import engines

    try:
        conv = engines.dwg_converter()
        if hasattr(conv, "from_dxf"):
            return conv
    except RuntimeError:
        pass
    for name in engines.available("dwg"):
        try:
            c = engines.get("dwg", name)
        except Exception:
            continue
        if hasattr(c, "from_dxf") and getattr(c, "installed", lambda: True)():
            return c
    raise RuntimeError("DWG writing needs ODA File Converter (free download: opendesign.com/guestfiles/oda_file_converter "
                       "— accept its terms yourself) or the upcoming native DWG writer. Convert --to dxf instead: "
                       "every CAD program opens DXF.")


# ---------------------------------------------------------------- image ↔ image, DXF ↔ DWG


def image_to_image(src: str | Path, fmt: str, *, out: str | Path | None = None, quality: int = 90,
                   quiet: bool = False) -> OpResult:
    """Raster → raster with Pillow (HEIC/HEIF in or out through macOS ``sips``). EXIF rotation is applied,
    DPI and ICC profile kept; multi-frame TIFF/GIF → one file per frame unless the target holds frames too."""
    from PIL import Image, ImageOps, ImageSequence

    from pdfcore.ops.export import RASTER_FORMATS, done, flatten_alpha, save_raster, single_target, target_paths

    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    fmt = canonical(fmt)
    if fmt not in RASTER_OUT:
        raise ValueError(f"image target must be one of {', '.join(sorted(RASTER_OUT))}")
    ext = OUT_EXT.get(fmt, "." + fmt)
    with tempfile.TemporaryDirectory(prefix="pdfcore-img-") as tmp:
        path = src
        if src.suffix.lower() in HEIC_EXTS:
            path = sips_convert(src, "png", Path(tmp) / f"{src.stem}.png")
        if fmt == "heic":
            dst = single_target(src, ".heic", out)
            mid = Path(tmp) / f"{src.stem}.mid.png"
            with Image.open(path) as im:
                ImageOps.exif_transpose(im).save(mid, "PNG")
            sips_convert(mid, "heic", dst, quality=quality)
            return done([dst], {"format": fmt}, quiet)
        with Image.open(path) as im:
            n = getattr(im, "n_frames", 1)
            dpi = im.info.get("dpi")
            dpi_v = float(dpi[0]) if dpi and dpi[0] and dpi[0] >= 10 else None
            if n > 1 and fmt in ("tiff", "gif", "webp"):
                frames = [f.copy() for f in ImageSequence.Iterator(im)]
                dst = single_target(src, ext, out)
                pil_fmt = RASTER_FORMATS[fmt][0]
                if fmt == "gif":
                    frames = [f if f.mode == "P" else f.convert("RGB").quantize(colors=256) for f in frames]
                    frames[0].save(dst, pil_fmt, save_all=True, append_images=frames[1:], loop=im.info.get("loop", 0),
                                   duration=im.info.get("duration", 100))
                elif fmt == "webp":
                    frames = [f.convert("RGBA") for f in frames]
                    frames[0].save(dst, pil_fmt, save_all=True, append_images=frames[1:], quality=quality,
                                   duration=im.info.get("duration", 100))
                else:
                    frames = [f if f.mode in ("1", "L", "RGB", "RGBA", "CMYK") else f.convert("RGB") for f in frames]
                    frames[0].save(dst, pil_fmt, save_all=True, append_images=frames[1:], compression="tiff_deflate",
                                   **({"dpi": (dpi_v, dpi_v)} if dpi_v else {}))
                return done([dst], {"format": fmt, "frames": n}, quiet, f"  ({n} frames)")
            dests = target_paths(src, ext, list(range(1, n + 1)), out, "frames")
            icc = im.info.get("icc_profile")
            for k, (frame, dst) in enumerate(zip(ImageSequence.Iterator(im), dests)):
                fr = ImageOps.exif_transpose(frame.copy()) if k == 0 and n == 1 else frame.copy()
                if fmt == "jpg" and fr.mode in ("RGBA", "LA", "P"):
                    fr = flatten_alpha(fr)
                save_raster(fr, dst, fmt, dpi=dpi_v, quality=quality, icc=icc)
    return done(dests, {"format": fmt, "frames": n}, quiet)


def cad_to_cad(src: str | Path, fmt: str, *, out: str | Path | None = None, quiet: bool = False) -> OpResult:
    """DWG → DXF (dwg engine ``to_dxf``), DXF → DWG (``from_dxf``), HPGL/2 plot → DXF."""
    from pdfcore.ops.export import done, single_target

    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    s, t = fmt_of(src), canonical(fmt)
    dst = single_target(src, "." + t, out)
    with tempfile.TemporaryDirectory(prefix="pdfcore-cad-") as tmp:
        if s == "dwg" and t == "dxf":
            from pdfcore import engines

            made = Path(engines.dwg_converter(src).to_dxf(src, Path(tmp)))
            from pdfcore.ops.plans.cad import _read_dxf

            _read_dxf(made)  # opens (strict or recover)
            shutil.move(str(made), dst)
            if not quiet:
                print(f"{dst}  PASS")
            return OpResult(dst, None, [], {"converter": "dwg-engine"})
        if s == "dxf" and t == "dwg":
            w = dwg_writer()
            made = Path(w.from_dxf(src, Path(tmp)))
            shutil.move(str(made), dst)
            return done([dst], {"converter": getattr(w, "name", "dwg")}, quiet)
        if s == "plt" and t == "dxf":
            from pdfcore.ops.plans.cad import _hpgl_to_dxf

            _hpgl_to_dxf(src.read_bytes(), src.name).saveas(str(dst))
            return done([dst], {"converter": "hpgl2"}, quiet)
    raise ValueError(f"no CAD route {s} → {t}")


# ---------------------------------------------------------------- from PDF


def from_pdf(src: str | Path, fmt: str, *, out: str | Path | None = None, pages: str | None = None, dpi: float = 150,
             quality: int = 90, multipage: bool = False, password: str | None = None, quiet: bool = False) -> OpResult:
    from pdfcore.ops import export, extract

    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    t = canonical(fmt)
    if t in export.RASTER_FORMATS:
        return export.pdf_to_raster(src, t, pages=pages, dpi=dpi, quality=quality, multipage=multipage, out=out,
                                    password=password, quiet=quiet)
    if t == "heic":
        with tempfile.TemporaryDirectory(prefix="pdfcore-heic-") as tmp:
            r = export.pdf_to_raster(src, "png", pages=pages, dpi=dpi, out=Path(tmp), password=password, quiet=True)
            pngs = [r.output, *r.extra_outputs]
            dests = export.target_paths(src, ".heic", r.data["pages"], out)
            for p, d in zip(pngs, dests):
                sips_convert(p, "heic", d, quality=quality)
        return export.done(dests, {"pages": r.data["pages"]}, quiet)
    if t == "svg":
        return export.pdf_to_svg(src, pages=pages, out=out, password=password, quiet=quiet)
    if t == "md":
        return export.pdf_to_markdown(src, pages=pages, out=out, password=password, quiet=quiet)
    if t == "html":
        return export.pdf_to_html(src, pages=pages, out=out, password=password, quiet=quiet)
    if t == "docx":
        return export.pdf_to_docx(src, pages=pages, out=out, password=password, quiet=quiet)
    if t == "csv":
        return export.pdf_to_csv(src, pages=pages, out=out, password=password, quiet=quiet)
    if t == "xlsx":
        return extract.tables(src, pages=pages, fmt="xlsx", out=export.single_target(src, ".xlsx", out),
                              password=password, quiet=quiet)
    if t == "txt":
        return extract.text(src, level="plain", pages=pages, out=export.single_target(src, ".txt", out),
                            password=password, quiet=quiet)
    if t == "json":
        return extract.text(src, level="words", pages=pages, out=export.single_target(src, ".json", out),
                            password=password, quiet=quiet)
    if t in ("dxf", "dwg"):
        from pdfcore.ops.plans.cad import pdf_to_dxf

        if t == "dxf":
            return pdf_to_dxf(src, page=pages, out=out, password=password, quiet=quiet)
        w = dwg_writer()  # fail before doing any work
        with tempfile.TemporaryDirectory(prefix="pdfcore-dwg-") as tmp:
            r = pdf_to_dxf(src, page=pages, out=Path(tmp), password=password, quiet=True)
            dxfs = [r.output, *r.extra_outputs]
            dests = [export.single_target(src, ".dwg", out)] if len(dxfs) == 1 else \
                export.target_paths(src, ".dwg", r.data["pages"], out, "dwg")
            for dxf, d in zip(dxfs, dests):
                shutil.move(str(Path(w.from_dxf(Path(dxf), Path(tmp)))), d)
        return export.done(dests, {"pages": r.data["pages"]}, quiet)
    raise ValueError(f"don't know how to convert PDF to {fmt} (see: pdfcore convert --list)")


# ---------------------------------------------------------------- the front door


def convert(src: str | Path, to: str, *, out: str | Path | None = None, pages: str | None = None, dpi: float = 150,
            quality: int = 90, multipage: bool = False, password: str | None = None, brand: str = "default",
            timeout: float = 180, options=None, quiet: bool = False) -> OpResult:
    """Convert *src* to format *to* (``pdf``, ``png``, ``docx``, ``dwg`` … or an extension), choosing the route."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    s, t = fmt_of(src), canonical(to)
    if not _known_source(src.suffix.lower()):
        raise ValueError(f"don't know how to read {src.suffix or 'files without an extension'} (see: pdfcore convert --list)")
    if s == t:
        raise ValueError(f"{src.name} is already {t.upper()}")
    if t == "pdf":
        kw: dict[str, Any] = {"options": options}
        ext = src.suffix.lower()
        if ext in OFFICE_EXTS and ext not in TEXT_EXTS or ext in HTML_EXTS:
            kw["timeout"] = timeout
        if ext in MD_EXTS or ext in TEXT_EXTS:
            kw = {"brand": brand}
        if ext in CAD_EXTS:
            kw = {}
        return to_pdf(src, out=out, quiet=quiet, **kw)
    if s == "pdf":
        return from_pdf(src, t, out=out, pages=pages, dpi=dpi, quality=quality, multipage=multipage,
                        password=password, quiet=quiet)
    if s in RASTER_IN and t in RASTER_OUT:
        return image_to_image(src, t, out=out, quality=quality, quiet=quiet)
    if (s, t) in (("dwg", "dxf"), ("dxf", "dwg"), ("plt", "dxf")):
        return cad_to_cad(src, t, out=out, quiet=quiet)
    if t in PDF_TARGETS or t == "heic":
        return _via_pdf(src, t, out=out, pages=pages, dpi=dpi, quality=quality, multipage=multipage, brand=brand,
                        timeout=timeout, quiet=quiet)
    raise ValueError(f"don't know how to convert {s} to {t} (see: pdfcore convert --list)")


def _via_pdf(src: Path, t: str, *, out, pages, dpi, quality, multipage, brand, timeout, quiet) -> OpResult:
    """Two-step route through a temporary PDF; outputs land next to *src* (or in *out*)."""
    from pdfcore.ops import export

    with tempfile.TemporaryDirectory(prefix="pdfcore-via-") as tmp:
        mid = Path(tmp) / f"{src.stem}.pdf"
        first = convert(src, "pdf", out=mid, brand=brand, timeout=timeout, quiet=True)
        if not first.ok and not quiet:
            print(f"note: intermediate PDF: {first.line()}")
        n = len(export._select(mid, pages, None))
        paged = t in export.RASTER_FORMATS or t in ("svg", "heic", "dxf", "dwg")
        if out is None:
            ext = OUT_EXT.get(t, "." + t)
            if paged and n > 1 and not (multipage and t in ("tiff", "gif", "webp")):
                from pdfcore.ops.extract import _out_dir

                out = _out_dir(src, "pages", None)
            else:
                out = export.single_target(src, ext, None)
        res = from_pdf(first.output, t, out=out, pages=pages, dpi=dpi, quality=quality, multipage=multipage, quiet=quiet)
    res.data["via"] = "pdf"
    return res


def expand_inputs(items: Sequence[str], recursive: bool = False) -> list[Path]:
    """Files, folders (supported files inside, sorted) and glob patterns → file list, in order, no duplicates."""
    out: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        r = p.resolve()
        if r not in seen:
            seen.add(r)
            out.append(p)

    for it in items:
        p = Path(it)
        if p.is_dir():
            it_ = p.rglob("*") if recursive else p.iterdir()
            for f in sorted(it_, key=lambda x: str(x).lower()):
                if f.is_file() and not f.name.startswith(".") and _known_source(f.suffix.lower()):
                    add(f)
        elif p.exists():
            add(p)
        elif any(c in it for c in "*?["):
            hits = sorted(glob.glob(it, recursive=True), key=str.lower)
            if not hits:
                raise FileNotFoundError(f"no files match {it}")
            for h in hits:
                if Path(h).is_file():
                    add(Path(h))
        else:
            raise FileNotFoundError(it)
    return out


def convert_many(inputs: Sequence[str | Path], to: str | None = None, *, out: str | Path | None = None,
                 merge: bool = False, recursive: bool = False, quiet: bool = False,
                 on_error: Callable[[Path, Exception], None] | None = None, **kw) -> list[OpResult]:
    """Convert many files/folders/globs. ``merge=True`` (target pdf) combines every input into one PDF, in order."""
    files = expand_inputs([str(i) for i in inputs], recursive=recursive)
    if not files:
        raise ValueError("no input files")
    target = canonical(to) if to else None
    if target is None and out is not None and Path(out).suffix and not Path(out).is_dir():
        target = canonical(Path(out).suffix)
    if target is None:
        if any(f.suffix.lower() == ".pdf" for f in files) and not merge:
            raise ValueError("PDF input: say what to convert it to, e.g. --to png, --to docx (see: pdfcore convert --list)")
        target = "pdf"
    if merge:
        if target != "pdf":
            raise ValueError("--merge combines into one PDF; use it with --to pdf")
        from pdfcore.ops.organize import merge as merge_pdfs

        with tempfile.TemporaryDirectory(prefix="pdfcore-merge-") as tmp:
            parts: list[Path] = []
            for k, f in enumerate(files):
                if f.suffix.lower() == ".pdf":
                    parts.append(f)
                    continue
                d = Path(tmp) / f"{k:04d}"
                d.mkdir()
                r = convert(f, "pdf", out=d / f"{f.stem}.pdf", quiet=True, **{k2: v for k2, v in kw.items() if k2 in ("brand", "timeout", "password")})
                parts.append(r.output)
            dest = output_path(files[0], "merged", ".pdf", out=out)
            dest.parent.mkdir(parents=True, exist_ok=True)
            res = merge_pdfs(parts, out=dest, titles=[f.stem for f in files], options=kw.get("options"), quiet=quiet)
        return [res]
    if out is not None and len(files) > 1 and not Path(out).is_dir():
        raise ValueError("with several inputs, -o must be an existing folder (or use --merge)")
    results: list[OpResult] = []
    for f in files:
        try:
            results.append(convert(f, target, out=out, quiet=quiet, **kw))
        except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
            if on_error is None or len(files) == 1:
                raise
            on_error(f, exc)
    return results


# ---------------------------------------------------------------- CLI


def _gate_opts(ns):
    return gate_options_from(ns)


@command("convert", "Convert between file types: anything → PDF, PDF → images/SVG/DXF/DWG/Word/Excel/Markdown/HTML/"
                    "text, image ↔ image, DXF ↔ DWG (--list prints every route)", group="convert")
def _cli_convert(p):
    p.add_argument("inputs", nargs="*", help="files, folders or glob patterns")
    p.add_argument("--to", help="target: pdf png jpg tiff webp gif bmp avif jp2 heic svg dxf dwg docx xlsx csv md html "
                                "txt json (aliases: jpeg, tif, word, excel, markdown). Default: -o's extension, else pdf")
    add_common(p)
    p.add_argument("--list", action="store_true", help="print the conversion matrix and exit")
    p.add_argument("--merge", action="store_true", help="combine every input into one PDF, in order")
    p.add_argument("--pages", help="PDF pages to convert, e.g. 1-3,5 (default: all)")
    p.add_argument("--dpi", type=float, default=150, help="raster DPI for PDF → image (default 150)")
    p.add_argument("--quality", type=int, default=90, help="JPEG/WebP/AVIF/HEIC quality (default 90)")
    p.add_argument("--multipage", action="store_true", help="PDF → one multi-page TIFF/GIF/WebP instead of one file per page")
    p.add_argument("--brand", default="default", help="brand for Markdown/text → PDF (templates/brands/)")
    p.add_argument("--timeout", type=float, default=180, help="LibreOffice timeout in seconds")
    p.add_argument("-r", "--recursive", action="store_true", help="descend into sub-folders")

    def run(ns):
        import sys

        if ns.list:
            print(matrix())
            return None
        if not ns.inputs:
            raise ValueError("give input files (or --list)")
        errors: list[tuple[Path, Exception]] = []

        def on_error(f: Path, exc: Exception) -> None:
            errors.append((f, exc))
            print(f"{f}  FAIL  {exc}", file=sys.stderr)

        try:
            results = convert_many(ns.inputs, ns.to, out=ns.out, merge=ns.merge, recursive=ns.recursive,
                                   on_error=on_error, pages=ns.pages, dpi=ns.dpi, quality=ns.quality,
                                   multipage=ns.multipage, password=ns.password, brand=ns.brand, timeout=ns.timeout,
                                   options=_gate_opts(ns))
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        bad = next((r for r in results if not r.ok), None)
        if errors:
            raise ValueError(f"{len(errors)} of {len(errors) + len(results)} conversions failed")
        return bad or (results[-1] if results else None)

    return run


@command("images-to-pdf", "Combine images (PNG/JPEG/TIFF…) into one PDF; page size from DPI", group="convert")
def _cli_img2pdf(p):
    p.add_argument("images", nargs="+")
    add_common(p)
    p.add_argument("--dpi", type=float, help="override the images' DPI")
    p.add_argument("--default-dpi", type=float, default=150, help="DPI for images that carry none (default 150)")
    p.add_argument("--page-size", help="fit onto letter, legal, tabloid, a4 or a3 instead")
    p.add_argument("--margin", type=float, default=0.0, help="margin in points")
    return lambda ns: images_to_pdf(ns.images, out=ns.out, dpi=ns.dpi, default_dpi=ns.default_dpi,
                                    page_size=ns.page_size, margin=ns.margin, options=_gate_opts(ns))


@command("pdf-to-images", "Render PDF pages to images (same as: render)", group="convert")
def _cli_pdf2img(p):
    p.add_argument("pdf")
    p.add_argument("--pages")
    p.add_argument("--dpi", type=float, default=150)
    p.add_argument("--format", dest="fmt", default="png", choices=["png", "jpeg", "jpg", "tiff"])
    p.add_argument("-o", "--out", help="output folder")
    p.add_argument("--password")
    return lambda ns: pdf_to_images(ns.pdf, pages=ns.pages, dpi=ns.dpi, fmt=ns.fmt, out=ns.out, password=ns.password)
