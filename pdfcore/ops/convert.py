"""Conversions to and from PDF.

* Office documents (docx/xlsx/pptx/odt/ods/odp/rtf/txt/csv…) → PDF through LibreOffice, run as an
  external program (``soffice --headless --convert-to pdf``) with a throwaway user profile so it
  works while the user has LibreOffice open.
* Images (PNG/JPEG/TIFF incl. multi-page, GIF, BMP, WebP) → PDF. Page size comes from the image DPI;
  JPEGs are embedded byte-for-byte (no recompression), others losslessly (Flate), 1-bit stays 1-bit.
* PDF → images: alias of :func:`pdfcore.ops.extract.render`.
* HTML → PDF through WeasyPrint when installed (``pip install 'pdf-studio[html]'``).

Every PDF written ends with the quality gate (``finalize``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Sequence

from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from
from pdfcore.ops.extract import render
from pdfcore.result import OpResult, output_path

__all__ = [
    "OFFICE_EXTS", "IMAGE_EXTS", "HTML_EXTS",
    "find_soffice", "office_to_pdf", "images_to_pdf", "html_to_pdf", "pdf_to_images", "to_pdf",
]

OFFICE_EXTS = {
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".ott", ".rtf", ".txt", ".wpd", ".pages",
    ".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv",
    ".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".key",
    ".odg", ".vsd", ".vsdx", ".pub",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp"}
HTML_EXTS = {".html", ".htm", ".xhtml"}

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
                options=None, quiet: bool = False) -> OpResult:
    """Render HTML to PDF with WeasyPrint (BSD) when installed."""
    src = Path(src)
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("HTML → PDF needs WeasyPrint: pip install 'pdf-studio[html]'") from exc
    if not src.is_file():
        raise FileNotFoundError(src)
    dest = _pdf_out(src, out)
    HTML(filename=str(src), base_url=base_url or str(src.parent)).write_pdf(str(dest))
    return finalize(dest, baseline=None, options=options, quiet=quiet, data={"converter": "weasyprint"})


# ---------------------------------------------------------------- PDF → images, dispatch


def pdf_to_images(src: str | Path, **kw) -> OpResult:
    """Alias of :func:`pdfcore.ops.extract.render`."""
    return render(src, **kw)


def to_pdf(src: str | Path, *, out: str | Path | None = None, quiet: bool = False, **kw) -> OpResult:
    """Convert any supported file to PDF, choosing the converter from the extension."""
    src = Path(src)
    ext = src.suffix.lower()
    if ext == ".pdf":
        raise ValueError(f"{src.name} is already a PDF (to make images use: pdfcore render)")
    if ext in IMAGE_EXTS:
        return images_to_pdf([src], out=out, quiet=quiet, **kw)
    if ext in HTML_EXTS:
        return html_to_pdf(src, out=out, quiet=quiet, **kw)
    if ext in OFFICE_EXTS:
        return office_to_pdf(src, out=out, quiet=quiet, **kw)
    raise ValueError(f"don't know how to convert {ext or 'files without an extension'} to PDF")


# ---------------------------------------------------------------- CLI


def _gate_opts(ns):
    return gate_options_from(ns)


@command("convert", "Convert office documents, images or HTML to PDF (one PDF per input)", group="convert")
def _cli_convert(p):
    p.add_argument("inputs", nargs="+")
    add_common(p)
    p.add_argument("--timeout", type=float, default=180, help="LibreOffice timeout in seconds")

    def run(ns):
        if ns.out and len(ns.inputs) > 1 and not Path(ns.out).is_dir():
            raise ValueError("with several inputs, -o must be an existing folder")
        res: OpResult | None = None
        bad = None
        for f in ns.inputs:
            kw = {"timeout": ns.timeout} if Path(f).suffix.lower() in OFFICE_EXTS else {}
            res = to_pdf(f, out=ns.out, options=_gate_opts(ns), **kw)
            if not res.ok:
                bad = res
        return bad or res

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
