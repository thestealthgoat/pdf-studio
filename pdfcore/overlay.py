"""Draw on existing pages: build a ReportLab overlay in *visual* coordinates and merge it with pikepdf.

Visual coordinates = what the reader sees: origin bottom-left of the displayed (cropped, rotated) page,
in points. ``/Rotate`` and offset CropBoxes are handled, so a footer drawn at (36, 24) lands at the
visual bottom-left of every page whatever its rotation.

Usage::

    def draw(c, i, w, h):          # c: reportlab Canvas, i: page index, w/h: visual size
        c.drawString(36, 24, f"Page {i+1}")
    stamp_pages(pdf, draw, pages=None, under=False)
"""

from __future__ import annotations

import io
from typing import Callable, Iterable

import pikepdf
from pikepdf import Name

DrawFn = Callable[["object", int, float, float], None]


def visual_box(page: pikepdf.Page) -> tuple[float, float, float, float, int]:
    """(x0, y0, W, H, rotate) of the visible box in user space; W/H are unrotated user-space sizes."""
    box = [float(v) for v in (page.obj.get("/CropBox") or page.mediabox)]
    x0, y0, x1, y1 = min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])
    rot = int(page.obj.get("/Rotate", 0) or 0) % 360
    return x0, y0, x1 - x0, y1 - y0, rot


def visual_size(page: pikepdf.Page) -> tuple[float, float]:
    _, _, W, H, rot = visual_box(page)
    return (H, W) if rot in (90, 270) else (W, H)


def visual_to_user_matrix(page: pikepdf.Page) -> tuple[float, float, float, float, float, float]:
    """Matrix mapping visual (u, v) → user space (x, y), as a PDF ``cm`` [a b c d e f]."""
    x0, y0, W, H, rot = visual_box(page)
    if rot == 90:
        return (0, 1, -1, 0, x0 + W, y0)
    if rot == 180:
        return (-1, 0, 0, -1, x0 + W, y0 + H)
    if rot == 270:
        return (0, -1, 1, 0, x0, y0 + H)
    return (1, 0, 0, 1, x0, y0)


def user_to_visual(page: pikepdf.Page, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = visual_to_user_matrix(page)
    # invert the 2x2 (rotation matrices: inverse = transpose)
    xx, yy = x - e, y - f
    det = a * d - b * c
    return ((d * xx - c * yy) / det, (-b * xx + a * yy) / det)


def _isolate_existing(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
    """Wrap the page's current content in q…Q so our stamp is not affected by an unbalanced graphics state."""
    page.contents_coalesce()
    data = page.obj.Contents.read_bytes()
    page.obj.Contents = pdf.make_stream(b"q\n" + data + b"\nQ\n")


def stamp_pages(
    pdf: pikepdf.Pdf,
    draw: DrawFn,
    pages: Iterable[int] | None = None,
    *,
    under: bool = False,
    tag: str = "PdfStudio",
) -> int:
    """Run *draw* for each selected page on a ReportLab canvas sized to the visual page; merge results.

    Returns the number of pages stamped. Overlays go on top (default) or underneath existing content.
    """
    from reportlab.pdfgen import canvas

    idx = list(range(len(pdf.pages))) if pages is None else list(pages)
    if not idx:
        return 0
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pageCompression=1)
    sizes = []
    for i in idx:
        w, h = visual_size(pdf.pages[i])
        sizes.append((w, h))
        c.setPageSize((w, h))
        draw(c, i, w, h)
        c.showPage()
    c.save()
    buf.seek(0)
    ov = pikepdf.open(buf)
    for k, i in enumerate(idx):
        page = pdf.pages[i]
        form = pdf.copy_foreign(ov.pages[k].as_form_xobject())
        res = page.obj.get("/Resources")
        if res is None:
            page.obj.Resources = pikepdf.Dictionary()
            res = page.obj.Resources
        if "/XObject" not in res:
            res.XObject = pikepdf.Dictionary()
        n = 0
        while Name(f"/{tag}{n}") in res.XObject:
            n += 1
        name = Name(f"/{tag}{n}")
        res.XObject[name] = form
        a, b, cc, d, e, f = visual_to_user_matrix(page)
        op = f"q {a:g} {b:g} {cc:g} {d:g} {e:.4f} {f:.4f} cm {name} Do Q\n".encode()
        if under:
            page.contents_add(pdf.make_stream(op), prepend=True)
        else:
            _isolate_existing(pdf, page)
            page.contents_add(pdf.make_stream(op), prepend=False)
    return len(idx)
