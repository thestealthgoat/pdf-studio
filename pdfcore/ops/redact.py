"""True redaction: remove — not cover — what lies under each box.

For every redaction box the op:

1. **Text** — interprets the page content stream (and any form XObjects it draws), computes each
   glyph's box from the text state and font widths, and deletes glyphs whose box falls in a
   redaction area. Remaining glyphs keep their positions (removed advances become TJ offsets).
   Marked-content /ActualText and /Alt around removed glyphs are stripped.
2. **Images** — decodes every image drawn under a box, paints the covered pixels black (and its
   soft mask opaque), re-encodes it as a new page-local image. Images that cannot be decoded are
   removed entirely. Inline images under a box are removed.
3. **Vector paths** — paths entirely inside a box are deleted; stroked paths that cross a box are
   cut at the box edge (curves flattened); filled paths that cross a box are clipped out.
4. **Annotations** under a box are removed (links, comments, widgets).
5. Draws the redaction mark (black box, optional overlay text such as "(b)(6)").
6. **Scrubs** the document: metadata (Info + XMP), hidden layers (content in OFF optional-content
   groups), comments, attachments, JavaScript, thumbnails, page-piece data; and removes redacted
   strings from bookmarks, structure-tree alt text, form field values.
7. Saves with a full rewrite (no incremental history) and runs the gate with redaction checks:
   two text engines must read nothing in any box, and no redacted term may remain.

Rectangles are in pdfcore's convention: points, top-left origin, **unrotated** page space
(``pdfcore.text.search(..., unrotated=True)`` reports hits in that space).
"""

from __future__ import annotations

import io
import math
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String

from pdfcore.gate import GateOptions
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.result import OpResult
from pdfcore.types import Rect

# ----------------------------------------------------------------------------- patterns
PATTERNS: dict[str, str] = {
    "ssn": r"(?<!\d)(?!000|666|9\d\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)",
    "phone": r"(?<!\w)(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "ein": r"(?<!\d)\d{2}-\d{7}(?!\d)",
    "card": r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)",
}


def _luhn(s: str) -> bool:
    d = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(d) <= 19:
        return False
    tot = 0
    for i, v in enumerate(reversed(d)):
        if i % 2:
            v *= 2
            v -= 9 if v > 9 else 0
        tot += v
    return tot % 10 == 0


# ----------------------------------------------------------------------------- matrix helpers
M = tuple[float, float, float, float, float, float]
IDENT: M = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(m1: M, m2: M) -> M:
    """m1 × m2 (apply m1 first, then m2) — PDF convention."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2,
    )


def apply(m: M, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def inv(m: M) -> M:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return IDENT
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_))


def _num(o) -> float:
    try:
        return float(o)
    except Exception:
        return 0.0


# ----------------------------------------------------------------------------- user-space boxes
@dataclass
class UBox:
    """Redaction box in default user space (bottom-left origin)."""

    x0: float
    y0: float
    x1: float
    y1: float

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def overlap(self, bx0, by0, bx1, by1) -> float:
        ix = min(self.x1, bx1) - max(self.x0, bx0)
        iy = min(self.y1, by1) - max(self.y0, by0)
        if ix <= 0 or iy <= 0:
            return 0.0
        a = (bx1 - bx0) * (by1 - by0)
        return (ix * iy) / a if a > 0 else 1.0

    def intersects(self, bx0, by0, bx1, by1) -> bool:
        return not (bx0 >= self.x1 or bx1 <= self.x0 or by0 >= self.y1 or by1 <= self.y0)


def _to_ubox(r: Rect, page: pikepdf.Page) -> UBox:
    mb = [float(v) for v in page.mediabox]
    x0, top = min(mb[0], mb[2]), max(mb[1], mb[3])
    return UBox(x0 + r.x0, top - r.bottom, x0 + r.x1, top - r.top)


def _bbox(pts: Iterable[tuple[float, float]]) -> tuple[float, float, float, float]:
    pts = list(pts)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# ----------------------------------------------------------------------------- fonts
class FontInfo:
    def __init__(self, fd):
        self.two_byte = False
        self.widths: dict[int, float] = {}
        self.default = 500.0
        self.scale = 0.001
        self.ascent, self.descent = 0.8, -0.25
        self.std = None
        try:
            sub = str(fd.get("/Subtype", ""))
            desc = fd.get("/FontDescriptor")
            if sub == "/Type0":
                enc = str(fd.get("/Encoding", ""))
                self.two_byte = True  # Identity-H/V and nearly every CID CMap in practice use 2-byte codes
                df = fd.DescendantFonts[0]
                desc = df.get("/FontDescriptor")
                self.default = _num(df.get("/DW", 1000))
                w = df.get("/W")
                if w is not None:
                    i = 0
                    w = list(w)
                    while i < len(w):
                        first = int(w[i])
                        if i + 1 < len(w) and isinstance(w[i + 1], pikepdf.Array):
                            for k, v in enumerate(w[i + 1]):
                                self.widths[first + k] = _num(v)
                            i += 2
                        elif i + 2 < len(w):
                            last, val = int(w[i + 1]), _num(w[i + 2])
                            for c in range(first, last + 1):
                                self.widths[c] = val
                            i += 3
                        else:
                            break
                _ = enc
            else:
                if sub == "/Type3":
                    fm = fd.get("/FontMatrix")
                    self.scale = _num(fm[0]) if fm is not None else 0.001
                fc = int(fd.get("/FirstChar", 0))
                ws = fd.get("/Widths")
                if ws is not None:
                    for k, v in enumerate(ws):
                        self.widths[fc + k] = _num(v)
                    self.default = _num(desc.get("/MissingWidth", 0)) if desc is not None else 0
                else:
                    base = str(fd.get("/BaseFont", "")).lstrip("/").split("+")[-1]
                    self.std = base
            if desc is not None:
                a, d = _num(desc.get("/Ascent", 0)), _num(desc.get("/Descent", 0))
                if a > 0:
                    self.ascent = a / 1000.0
                if d < 0:
                    self.descent = d / 1000.0
        except Exception:
            pass

    def codes(self, data: bytes) -> list[bytes]:
        if self.two_byte:
            return [data[i : i + 2] for i in range(0, len(data) - 1, 2)] or ([data] if data else [])
        return [bytes([b]) for b in data]

    def width(self, code: bytes) -> float:
        """Advance in text-space units for 1 pt font size (before Tc/Tw/Th)."""
        c = int.from_bytes(code, "big")
        if self.std:
            try:
                from reportlab.pdfbase import pdfmetrics

                return pdfmetrics.stringWidth(code.decode("latin-1"), self.std if self.std in pdfmetrics.standardFonts else "Helvetica", 1)
            except Exception:
                return 0.5
        return self.widths.get(c, self.default) * self.scale


# ----------------------------------------------------------------------------- content rewriting
PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
CONSTRUCT = {"m", "l", "c", "v", "y", "h", "re"}


@dataclass
class Stats:
    glyphs: int = 0
    images: int = 0
    images_removed: int = 0
    inline_images: int = 0
    paths_removed: int = 0
    paths_cut: int = 0
    paths_clipped: int = 0
    annots: int = 0
    notes: list[str] = field(default_factory=list)


class Redactor:
    def __init__(self, pdf: pikepdf.Pdf, boxes: list[UBox], stats: Stats, glyph_overlap: float = 0.3):
        self.pdf = pdf
        self.boxes = boxes
        self.stats = stats
        self.glyph_overlap = glyph_overlap
        self._fonts: dict = {}

    # -------------------------------------------------- helpers
    def hits(self, bx0, by0, bx1, by1) -> list[UBox]:
        return [b for b in self.boxes if b.intersects(bx0, by0, bx1, by1)]

    def font(self, res, name) -> FontInfo | None:
        try:
            fd = res.Font[name]
        except Exception:
            return None
        key = fd.objgen if fd.is_indirect else id(fd)
        if key not in self._fonts:
            self._fonts[key] = FontInfo(fd)
        return self._fonts[key]

    # -------------------------------------------------- main entry
    def rewrite(self, owner, res, ctm0: M) -> tuple[list, bool]:
        """Return (new instruction list, changed?) for owner's content (a page or a form XObject)."""
        try:
            instrs = list(pikepdf.parse_content_stream(owner))
        except Exception as exc:
            self.stats.notes.append(f"could not parse content: {exc}")
            return [], False
        out: list = []
        changed = False
        ctm = ctm0
        gstack: list = []
        # text state
        ts = dict(Tc=0.0, Tw=0.0, Th=1.0, TL=0.0, Tf=None, Tfs=0.0, Ts=0.0)
        tm = lm = IDENT
        # path accumulation
        path_ops: list = []
        path_pts: list[tuple[float, float]] = []
        # marked content
        mc_stack: list[int] = []  # indexes into out of BDC ops
        mc_dirty: set[int] = set()

        def flush_path(paint_instr):
            nonlocal changed, path_ops, path_pts
            op = str(paint_instr.operator)
            clip = any(str(i.operator) in ("W", "W*") for i in path_ops)
            if not path_pts or op == "n" or clip:
                out.extend(path_ops)
                out.append(paint_instr)
            else:
                bx = _bbox(path_pts)
                hit = self.hits(*bx)
                if not hit:
                    out.extend(path_ops)
                    out.append(paint_instr)
                elif all(any(b.contains(x, y) for b in hit) for x, y in path_pts):
                    self.stats.paths_removed += 1
                    changed = True
                elif op in ("S", "s"):
                    new = self._cut_stroke(path_ops, ctm, op == "s", hit)
                    out.extend(new)
                    if new:
                        out.append(pikepdf.ContentStreamInstruction([], Operator("S")))
                    self.stats.paths_cut += 1
                    changed = True
                else:
                    out.extend(self._clip_out(ctm, hit))
                    out.extend(path_ops)
                    out.append(paint_instr)
                    out.append(pikepdf.ContentStreamInstruction([], Operator("Q")))
                    self.stats.paths_clipped += 1
                    changed = True
            path_ops, path_pts = [], []

        for ins in instrs:
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                ux = [apply(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
                if self.hits(*_bbox(ux)):
                    self.stats.inline_images += 1
                    changed = True
                    continue
                out.append(ins)
                continue
            op = str(ins.operator)
            ops = list(ins.operands)

            # ---------------- graphics state
            if op == "q":
                gstack.append((ctm, dict(ts)))
            elif op == "Q":
                if gstack:
                    ctm, ts = gstack.pop()
            elif op == "cm" and len(ops) == 6:
                ctm = mul(tuple(_num(v) for v in ops), ctm)  # type: ignore[arg-type]

            # ---------------- paths
            if op in CONSTRUCT or op in ("W", "W*"):
                path_ops.append(ins)
                if op in ("m", "l"):
                    path_pts.append(apply(ctm, _num(ops[0]), _num(ops[1])))
                elif op in ("c", "v", "y"):
                    nums = [_num(v) for v in ops]
                    for k in range(0, len(nums) - 1, 2):
                        path_pts.append(apply(ctm, nums[k], nums[k + 1]))
                elif op == "re":
                    x, y, w, h = (_num(v) for v in ops)
                    path_pts += [apply(ctm, x, y), apply(ctm, x + w, y), apply(ctm, x, y + h), apply(ctm, x + w, y + h)]
                continue
            if op in PAINT:
                flush_path(ins)
                continue

            # ---------------- marked content
            if op in ("BDC", "BMC"):
                mc_stack.append(len(out))
                out.append(ins)
                continue
            if op == "EMC":
                if mc_stack:
                    mc_stack.pop()
                out.append(ins)
                continue

            # ---------------- text state
            if op == "BT":
                tm = lm = IDENT
            elif op == "Tc":
                ts["Tc"] = _num(ops[0])
            elif op == "Tw":
                ts["Tw"] = _num(ops[0])
            elif op == "Tz":
                ts["Th"] = _num(ops[0]) / 100.0
            elif op == "TL":
                ts["TL"] = _num(ops[0])
            elif op == "Ts":
                ts["Ts"] = _num(ops[0])
            elif op == "Tf" and len(ops) == 2:
                ts["Tf"], ts["Tfs"] = ops[0], _num(ops[1])
            elif op == "Td":
                lm = mul((1, 0, 0, 1, _num(ops[0]), _num(ops[1])), lm)
                tm = lm
            elif op == "TD":
                ts["TL"] = -_num(ops[1])
                lm = mul((1, 0, 0, 1, _num(ops[0]), _num(ops[1])), lm)
                tm = lm
            elif op == "Tm" and len(ops) == 6:
                lm = tm = tuple(_num(v) for v in ops)  # type: ignore[assignment]
            elif op == "T*":
                lm = mul((1, 0, 0, 1, 0, -ts["TL"]), lm)
                tm = lm

            if op in ("Tj", "TJ", "'", '"'):
                prefix = []
                if op == "'":
                    lm = mul((1, 0, 0, 1, 0, -ts["TL"]), lm)
                    tm = lm
                    prefix = [pikepdf.ContentStreamInstruction([], Operator("T*"))]
                    items = [ops[0]]
                elif op == '"':
                    ts["Tw"], ts["Tc"] = _num(ops[0]), _num(ops[1])
                    lm = mul((1, 0, 0, 1, 0, -ts["TL"]), lm)
                    tm = lm
                    prefix = [
                        pikepdf.ContentStreamInstruction([ops[0]], Operator("Tw")),
                        pikepdf.ContentStreamInstruction([ops[1]], Operator("Tc")),
                        pikepdf.ContentStreamInstruction([], Operator("T*")),
                    ]
                    items = [ops[2]]
                elif op == "Tj":
                    items = [ops[0]]
                else:
                    items = list(ops[0])
                new_items, removed, tm = self._show(items, ts, tm, ctm, res)
                if removed:
                    changed = True
                    self.stats.glyphs += removed
                    for idx in mc_stack:
                        mc_dirty.add(idx)
                    out.extend(prefix)
                    if any(isinstance(i, pikepdf.String) for i in new_items):
                        out.append(pikepdf.ContentStreamInstruction([Array(new_items)], Operator("TJ")))
                    else:
                        # nothing left to show; keep text position consistent for following ops
                        out.append(pikepdf.ContentStreamInstruction([Array(new_items or [0])], Operator("TJ")))
                else:
                    out.append(ins)
                continue

            # ---------------- XObjects
            if op == "Do" and ops:
                rep = self._do(ops[0], res, ctm)
                if rep is False:  # remove
                    changed = True
                    continue
                if isinstance(rep, Name):
                    changed = True
                    out.append(pikepdf.ContentStreamInstruction([rep], Operator("Do")))
                    continue
            out.append(ins)

        # strip ActualText/Alt from marked content that contained removed glyphs
        for idx in mc_dirty:
            ins = out[idx]
            if str(ins.operator) == "BDC" and len(ins.operands) == 2 and isinstance(ins.operands[1], Dictionary):
                d = Dictionary({k: v for k, v in ins.operands[1].items() if k not in ("/ActualText", "/Alt", "/E")})
                out[idx] = pikepdf.ContentStreamInstruction([ins.operands[0], d], Operator("BDC"))
        return out, changed

    # -------------------------------------------------- text show
    def _show(self, items, ts, tm, ctm, res):
        fi = self.font(res, ts["Tf"]) if ts["Tf"] is not None else None
        Tfs, Th, Tc, Tw, Ts = ts["Tfs"], ts["Th"], ts["Tc"], ts["Tw"], ts["Ts"]
        new_items: list = []
        removed = 0
        pending_adj = 0.0
        if fi is None or Tfs == 0:
            return list(items), 0, tm

        def emit_adj():
            nonlocal pending_adj
            if abs(pending_adj) > 1e-6:
                new_items.append(round(pending_adj, 4))
            pending_adj = 0.0

        for it in items:
            if not isinstance(it, (pikepdf.String, bytes, str)):
                n = _num(it)
                pending_adj += n
                tm = mul((1, 0, 0, 1, -n / 1000.0 * Tfs * Th, 0), tm)
                continue
            data = bytes(it)
            keep = bytearray()
            for code in fi.codes(data):
                w0 = fi.width(code)
                tx = (w0 * Tfs + Tc + (Tw if code == b" " else 0.0)) * Th
                trm = mul((Tfs * Th, 0, 0, Tfs, 0, Ts), mul(tm, ctm))
                gw = w0 if w0 > 0 else 0.5
                corners = [apply(trm, x, y) for x, y in ((0, fi.descent), (gw, fi.descent), (0, fi.ascent), (gw, fi.ascent))]
                bx = _bbox(corners)
                cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
                hit = any(b.contains(cx, cy) or b.overlap(*bx) >= self.glyph_overlap for b in self.boxes)
                if hit:
                    removed += 1
                    if keep:
                        emit_adj()
                        new_items.append(String(bytes(keep)))
                        keep = bytearray()
                    pending_adj -= tx / (Tfs * Th) * 1000.0 if Tfs * Th else 0.0
                else:
                    if pending_adj and not keep:
                        emit_adj()
                    keep += code
                tm = mul((1, 0, 0, 1, tx, 0), tm)
            if keep:
                emit_adj()
                new_items.append(String(bytes(keep)))
        emit_adj()
        return new_items, removed, tm

    # -------------------------------------------------- XObjects
    def _do(self, name, res, ctm):
        """Return None (unchanged), False (remove the Do), or a new Name to draw instead."""
        try:
            xo = res.XObject[name]
        except Exception:
            return None
        sub = str(xo.get("/Subtype", ""))
        if sub == "/Image":
            corners = [apply(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
            hit = self.hits(*_bbox(corners))
            if not hit:
                return None
            new = self._redact_image(xo, ctm, hit)
            if new is None:
                self.stats.images_removed += 1
                return False
            self.stats.images += 1
            return self._add_res(res, "XObject", new, "RdImg")
        if sub == "/Form":
            fm = tuple(_num(v) for v in xo.get("/Matrix", [1, 0, 0, 1, 0, 0]))
            fctm = mul(fm, ctm)  # type: ignore[arg-type]
            bb = xo.get("/BBox")
            if bb is not None:
                b = [_num(v) for v in bb]
                corners = [apply(fctm, x, y) for x, y in ((b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3]))]
                if not self.hits(*_bbox(corners)):
                    return None
            src_res = xo.get("/Resources") or res
            fres = Dictionary(dict(src_res.items()))
            if "/XObject" in fres:
                fres.XObject = Dictionary(dict(fres.XObject.items()))
            instrs, changed = self.rewrite(xo, fres, fctm)
            if not changed:
                return None
            _prune_xobjects(fres, instrs)
            data = pikepdf.unparse_content_stream(instrs)
            newx = self.pdf.make_stream(data)
            for k, v in xo.items():
                if k not in ("/Length", "/Filter", "/DecodeParms", "/Resources"):
                    newx[k] = v
            newx.Resources = fres
            return self._add_res(res, "XObject", self.pdf.make_indirect(newx), "RdForm")
        return None

    def _add_res(self, res, cat: str, obj, prefix: str) -> Name:
        if f"/{cat}" not in res:
            res[f"/{cat}"] = Dictionary()
        d = res[f"/{cat}"]
        n = 0
        while Name(f"/{prefix}{n}") in d:
            n += 1
        nm = Name(f"/{prefix}{n}")
        d[nm] = obj
        return nm

    def _redact_image(self, xo, ctm, boxes: list[UBox]):
        from PIL import Image, ImageDraw

        try:
            pim = pikepdf.PdfImage(xo)
            img = pim.as_pil_image()
        except Exception as exc:
            self.stats.notes.append(f"image removed (cannot decode: {exc})")
            return None
        W, H = img.size
        ic = inv(ctm)
        mask = Image.new("L", (W, H), 0)
        dr = ImageDraw.Draw(mask)
        for b in boxes:
            poly = []
            for x, y in ((b.x0, b.y0), (b.x1, b.y0), (b.x1, b.y1), (b.x0, b.y1)):
                u, v = apply(ic, x, y)
                poly.append((u * W, (1 - v) * H))
            dr.polygon(poly, fill=255)
        if bool(xo.get("/ImageMask", False)):
            # Stencil mask: clear the covered samples to "not painted" and keep it a stencil.
            dec = [int(_num(v)) for v in xo.get("/Decode", [0, 1])]
            painted = 0 if dec[:2] == [0, 1] else 1
            st = img.convert("1")
            st.paste(255 if painted == 0 else 0, mask=mask)
            new = self.pdf.make_stream(zlib.compress(st.tobytes(), 6))
            new.Type, new.Subtype = Name.XObject, Name.Image
            new.Width, new.Height = W, H
            new.ImageMask, new.BitsPerComponent, new.Filter = True, 1, Name.FlateDecode
            if "/Decode" in xo:
                new.Decode = xo.Decode
            return self.pdf.make_indirect(new)
        mode = img.mode
        if mode not in ("L", "RGB", "CMYK"):
            try:
                img = img.convert("RGB")
            except Exception as exc:
                self.stats.notes.append(f"image removed (cannot convert {mode}: {exc})")
                return None
            mode = "RGB"
        black = {"L": 0, "RGB": (0, 0, 0), "CMYK": (0, 0, 0, 255)}[mode]
        img.paste(Image.new(mode, (W, H), black), mask=mask)
        cs = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB, "CMYK": Name.DeviceCMYK}[mode]
        raw = img.tobytes()
        new = self.pdf.make_stream(zlib.compress(raw, 6))
        new.Type, new.Subtype = Name.XObject, Name.Image
        new.Width, new.Height = W, H
        new.ColorSpace, new.BitsPerComponent = cs, 8
        new.Filter = Name.FlateDecode
        if mode == "CMYK" and "/Decode" in xo and str(xo.get("/Filter", "")) == "/DCTDecode":
            pass  # PIL already un-inverted Adobe CMYK JPEGs
        sm = xo.get("/SMask")
        if sm is not None:
            try:
                simg = pikepdf.PdfImage(sm).as_pil_image().convert("L")
                if simg.size != (W, H):
                    m2 = mask.resize(simg.size)
                else:
                    m2 = mask
                simg.paste(255, mask=m2)
                s = self.pdf.make_stream(zlib.compress(simg.tobytes(), 6))
                s.Type, s.Subtype = Name.XObject, Name.Image
                s.Width, s.Height = simg.size
                s.ColorSpace, s.BitsPerComponent, s.Filter = Name.DeviceGray, 8, Name.FlateDecode
                new.SMask = s
            except Exception:
                self.stats.notes.append("soft mask dropped from redacted image")
        if "/Interpolate" in xo:
            new.Interpolate = xo.Interpolate
        return self.pdf.make_indirect(new)

    # -------------------------------------------------- vector helpers
    def _cut_stroke(self, path_ops, ctm, closed: bool, boxes: list[UBox]) -> list:
        """Rebuild a stroked path in current user space with the parts inside boxes removed."""
        ic = inv(ctm)
        subpaths: list[list[tuple[float, float]]] = []
        cur: list[tuple[float, float]] = []
        start = None
        for ins in path_ops:
            op = str(ins.operator)
            v = [_num(x) for x in ins.operands]
            if op == "m":
                if cur:
                    subpaths.append(cur)
                cur = [apply(ctm, v[0], v[1])]
                start = cur[0]
            elif op == "l":
                cur.append(apply(ctm, v[0], v[1]))
            elif op in ("c", "v", "y") and cur:
                p0 = cur[-1]
                if op == "c":
                    p1, p2, p3 = apply(ctm, v[0], v[1]), apply(ctm, v[2], v[3]), apply(ctm, v[4], v[5])
                elif op == "v":
                    p1, p2, p3 = p0, apply(ctm, v[0], v[1]), apply(ctm, v[2], v[3])
                else:
                    p1, p2, p3 = apply(ctm, v[0], v[1]), apply(ctm, v[2], v[3]), apply(ctm, v[2], v[3])
                for k in range(1, 13):
                    t = k / 12
                    mt = 1 - t
                    cur.append((
                        mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0],
                        mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1],
                    ))
            elif op == "re":
                if cur:
                    subpaths.append(cur)
                x, y, w, h = v
                sq = [apply(ctm, x, y), apply(ctm, x + w, y), apply(ctm, x + w, y + h), apply(ctm, x, y + h)]
                subpaths.append(sq + [sq[0]])
                cur, start = [], None
            elif op == "h" and cur and start:
                cur.append(start)
        if cur:
            if closed and start:
                cur.append(start)
            subpaths.append(cur)
        out = []
        for sp in subpaths:
            for a, b in zip(sp, sp[1:]):
                for s, e in _segment_outside(a, b, boxes):
                    p, q = apply(ic, *s), apply(ic, *e)
                    out.append(pikepdf.ContentStreamInstruction([p[0], p[1]], Operator("m")))
                    out.append(pikepdf.ContentStreamInstruction([q[0], q[1]], Operator("l")))
        return out

    def _clip_out(self, ctm, boxes: list[UBox]) -> list:
        """q + an even-odd clip that excludes the boxes (expressed in current user space)."""
        ic = inv(ctm)
        I = pikepdf.ContentStreamInstruction
        out = [I([], Operator("q"))]
        big = 1e5
        for x, y in ((-big, -big), (big, -big), (big, big), (-big, big)):
            p = apply(ic, x, y)
            out.append(I([p[0], p[1]], Operator("m" if (x, y) == (-big, -big) else "l")))
        out.append(I([], Operator("h")))
        for b in boxes:
            pts = [apply(ic, x, y) for x, y in ((b.x0, b.y0), (b.x1, b.y0), (b.x1, b.y1), (b.x0, b.y1))]
            out.append(I([pts[0][0], pts[0][1]], Operator("m")))
            for p in pts[1:]:
                out.append(I([p[0], p[1]], Operator("l")))
            out.append(I([], Operator("h")))
        out += [I([], Operator("W*")), I([], Operator("n"))]
        return out


def _prune_xobjects(res, instrs) -> None:
    """Drop XObject resources the rewritten content no longer draws (e.g. the unredacted original image)."""
    if res is None or "/XObject" not in res:
        return
    used = {str(i.operands[0]) for i in instrs
            if not isinstance(i, pikepdf.ContentStreamInlineImage) and str(i.operator) == "Do" and i.operands}
    for k in list(res.XObject.keys()):
        if str(k) not in used:
            del res.XObject[k]


def _segment_outside(a, b, boxes: list[UBox]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Parts of segment a→b that lie outside every box (Liang–Barsky per box)."""
    pieces = [(0.0, 1.0)]
    dx, dy = b[0] - a[0], b[1] - a[1]
    for bx in boxes:
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, a[0] - bx.x0), (dx, bx.x1 - a[0]), (-dy, a[1] - bx.y0), (dy, bx.y1 - a[1])):
            if abs(p) < 1e-12:
                if q < 0:
                    ok = False
                    break
            else:
                r = q / p
                if p < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
        if not ok or t0 >= t1:
            continue
        nxt = []
        for s, e in pieces:
            if e <= t0 or s >= t1:
                nxt.append((s, e))
            else:
                if s < t0:
                    nxt.append((s, t0))
                if e > t1:
                    nxt.append((t1, e))
        pieces = nxt
    return [((a[0] + dx * s, a[1] + dy * s), (a[0] + dx * e, a[1] + dy * e)) for s, e in pieces if e - s > 1e-6]


# ----------------------------------------------------------------------------- scrubbing
def _strip_hidden_layers(pdf: pikepdf.Pdf) -> int:
    """Remove content inside marked-content sequences tagged with OFF optional-content groups."""
    ocp = pdf.Root.get("/OCProperties")
    if ocp is None:
        return 0
    off = set()
    try:
        for g in ocp.D.get("/OFF", []) or []:
            off.add(g.objgen)
    except Exception:
        pass
    removed = 0
    if off:
        for page in pdf.pages:
            props = (page.obj.get("/Resources") or {}).get("/Properties") if page.obj.get("/Resources") else None
            if props is None:
                continue
            off_names = {str(k) for k, v in props.items() if getattr(v, "objgen", None) in off}
            if not off_names:
                continue
            instrs = list(pikepdf.parse_content_stream(page))
            out, depth, skipping = [], 0, 0
            for ins in instrs:
                op = str(ins.operator) if not isinstance(ins, pikepdf.ContentStreamInlineImage) else ""
                if skipping:
                    if op in ("BDC", "BMC"):
                        skipping += 1
                    elif op == "EMC":
                        skipping -= 1
                    continue
                if op == "BDC" and len(ins.operands) == 2 and str(ins.operands[0]) == "/OC" and str(ins.operands[1]) in off_names:
                    skipping = 1
                    removed += 1
                    continue
                out.append(ins)
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    return removed


def _scrub_strings(obj, needles: list[str], depth=0, seen=None) -> int:
    """Blank string values containing any needle within a dictionary/array tree (outlines, struct tree…)."""
    if seen is None:
        seen = set()
    n = 0
    if depth > 60:
        return 0
    try:
        if obj.is_indirect:
            if obj.objgen in seen:
                return 0
            seen.add(obj.objgen)
    except Exception:
        pass
    if isinstance(obj, Dictionary) or isinstance(obj, pikepdf.Stream):
        for k in list(obj.keys()):
            if k in ("/Parent", "/P", "/Pg", "/Prev", "/Last"):
                continue
            v = obj[k]
            if isinstance(v, String):
                s = str(v)
                if any(nd and nd.lower() in s.lower() for nd in needles):
                    for nd in needles:
                        s = re.sub(re.escape(nd), "█" * min(len(nd), 8), s, flags=re.I)
                    obj[k] = String(s)
                    n += 1
            elif isinstance(v, (Dictionary, Array)):
                n += _scrub_strings(v, needles, depth + 1, seen)
    elif isinstance(obj, Array):
        for i, v in enumerate(obj):
            if isinstance(v, String):
                s = str(v)
                if any(nd and nd.lower() in s.lower() for nd in needles):
                    obj[i] = String("")
                    n += 1
            elif isinstance(v, (Dictionary, Array)):
                n += _scrub_strings(v, needles, depth + 1, seen)
    return n


@dataclass
class Scrub:
    metadata: bool = True
    hidden_layers: bool = True
    comments: bool = True
    attachments: bool = True
    javascript: bool = True
    thumbnails: bool = True


def _scrub(pdf: pikepdf.Pdf, sc: Scrub, needles: list[str], stats: Stats) -> None:
    if sc.metadata:
        try:
            for k in list(pdf.docinfo.keys()):
                del pdf.docinfo[k]
        except Exception:
            pass
        if "/Metadata" in pdf.Root:
            del pdf.Root["/Metadata"]
        for p in pdf.pages:
            for k in ("/Metadata", "/PieceInfo"):
                if k in p.obj:
                    del p.obj[k]
        if "/PieceInfo" in pdf.Root:
            del pdf.Root["/PieceInfo"]
    if sc.hidden_layers:
        n = _strip_hidden_layers(pdf)
        if n:
            stats.notes.append(f"removed {n} hidden-layer sequences")
        if "/OCProperties" in pdf.Root:
            # keep visible layers' content, drop the layer machinery so nothing can be toggled back on
            del pdf.Root["/OCProperties"]
    if sc.comments:
        for p in pdf.pages:
            annots = p.obj.get("/Annots")
            if annots is None:
                continue
            keep = Array([a for a in annots if str(a.get("/Subtype", "")) in ("/Link", "/Widget")])
            stats.annots += len(annots) - len(keep)
            p.obj.Annots = keep
    if sc.attachments:
        names = pdf.Root.get("/Names")
        if names is not None and "/EmbeddedFiles" in names:
            del names["/EmbeddedFiles"]
        for p in pdf.pages:
            annots = p.obj.get("/Annots")
            if annots is not None:
                p.obj.Annots = Array([a for a in annots if str(a.get("/Subtype", "")) != "/FileAttachment"])
    if sc.javascript:
        names = pdf.Root.get("/Names")
        if names is not None and "/JavaScript" in names:
            del names["/JavaScript"]
        if "/OpenAction" in pdf.Root:
            del pdf.Root["/OpenAction"]
        if "/AA" in pdf.Root:
            del pdf.Root["/AA"]
    if sc.thumbnails:
        for p in pdf.pages:
            if "/Thumb" in p.obj:
                del p.obj["/Thumb"]
    af = pdf.Root.get("/AcroForm")
    if af is not None and "/XFA" in af:
        del af["/XFA"]
    if needles:
        n = 0
        for key in ("/Outlines", "/StructTreeRoot", "/AcroForm", "/Names"):
            if key in pdf.Root:
                n += _scrub_strings(pdf.Root[key], needles)
        for p in pdf.pages:
            for a in p.obj.get("/Annots", []) or []:
                n += _scrub_strings(a, needles)
        if n:
            stats.notes.append(f"scrubbed {n} strings in bookmarks/tags/forms/annotations")


# ----------------------------------------------------------------------------- planning
@dataclass
class Area:
    page: int
    rect: Rect
    text: str = ""


def find_areas(
    path: str | Path,
    *,
    terms: Sequence[str] = (),
    patterns: Sequence[str] = (),
    regexes: Sequence[str] = (),
    pages: Iterable[int] | None = None,
    case: bool = False,
    whole_word: bool = False,
    password: str | None = None,
    pad: float = 0.75,
) -> list[Area]:
    """Search the document and return redaction areas (one per line a match spans)."""
    from pdfcore.text import search

    found: list[Area] = []
    pg = list(pages) if pages is not None else None
    for t in terms:
        for h in search(path, t, case=case, whole_word=whole_word, pages=pg, password=password, unrotated=True):
            found += [Area(h.page, r.expand(pad), h.text) for r in h.rects]
    for name in patterns:
        rx = PATTERNS.get(name.lower())
        if rx is None:
            raise ValueError(f"unknown pattern {name!r}; choose from {sorted(PATTERNS)}")
        for h in search(path, rx, regex=True, case=True, pages=pg, password=password, unrotated=True):
            if name.lower() == "card" and not _luhn(h.text):
                continue
            found += [Area(h.page, r.expand(pad), h.text) for r in h.rects]
    for rx in regexes:
        for h in search(path, rx, regex=True, case=case, pages=pg, password=password, unrotated=True):
            found += [Area(h.page, r.expand(pad), h.text) for r in h.rects]
    return found


def _text_in(path: Path, areas: list[Area], password: str | None) -> list[str]:
    from pdfcore.text import open_plumber

    out = []
    with open_plumber(path, password, unrotated=True) as pdf:
        for a in areas:
            pg = pdf.pages[a.page]
            s = "".join(
                c["text"] for c in pg.chars
                if a.rect.contains_point((c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2)
            ).strip()
            if s:
                out.append(s)
    return out


# ----------------------------------------------------------------------------- public op
def redact(
    src: str | Path,
    *,
    areas: Sequence[tuple[int, Rect]] = (),
    terms: Sequence[str] = (),
    patterns: Sequence[str] = (),
    regexes: Sequence[str] = (),
    pages: Iterable[int] | None = None,
    case: bool = False,
    whole_word: bool = False,
    fill: tuple[float, float, float] = (0, 0, 0),
    label: str | None = None,
    scrub: Scrub | None = None,
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    src = Path(src)
    sc = scrub or Scrub()
    plan = [Area(p, r) for p, r in areas]
    plan += find_areas(src, terms=terms, patterns=patterns, regexes=regexes, pages=pages, case=case,
                       whole_word=whole_word, password=password)
    if not plan and not quiet:
        print("nothing to redact (no areas and no matches)")
    removed_text = _text_in(src, plan, password)
    needles = sorted({t for t in [*terms, *removed_text, *(a.text for a in plan)] if t and len(t.strip()) >= 3}, key=len, reverse=True)

    stats = Stats()
    pdf = pikepdf.open(str(src), password=password or "")
    by_page: dict[int, list[Area]] = {}
    for a in plan:
        by_page.setdefault(a.page, []).append(a)
    for pi, alist in sorted(by_page.items()):
        page = pdf.pages[pi]
        boxes = [_to_ubox(a.rect, page) for a in alist]
        # page-local resources so edits never leak into other pages sharing them
        res = page.obj.get("/Resources")
        res = Dictionary(dict(res.items())) if res is not None else Dictionary()
        if "/XObject" in res:
            res.XObject = Dictionary(dict(res.XObject.items()))
        page.obj.Resources = res
        rd = Redactor(pdf, boxes, stats)
        instrs, changed = rd.rewrite(page, res, IDENT)
        if changed:
            _prune_xobjects(res, instrs)
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instrs))
        # annotations under boxes
        annots = page.obj.get("/Annots")
        if annots is not None:
            keep = []
            for an in annots:
                r = [_num(v) for v in an.get("/Rect", [0, 0, 0, 0])]
                if any(b.intersects(min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3])) for b in boxes):
                    stats.annots += 1
                    continue
                keep.append(an)
            page.obj.Annots = Array(keep)
        _draw_marks(pdf, page, boxes, fill, label)
    _scrub(pdf, sc, needles, stats)
    dst = output_path(src, "redacted", out=out)
    pdf.save(dst, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate, linearize=False)
    pdf.close()

    o = options or GateOptions()
    o.redact_regions = [(a.page, a.rect.expand(-0.75)) for a in plan]
    o.redact_allow = [label] if label else None
    # Whole-document term check only when the whole document was searched.
    o.redact_terms = [t for t in [*terms, *(a.text for a in plan)] if t] if pages is None else None
    data = {
        "areas": len(plan), "glyphs_removed": stats.glyphs, "images_redacted": stats.images,
        "images_removed": stats.images_removed, "inline_images_removed": stats.inline_images,
        "paths_removed": stats.paths_removed, "paths_cut": stats.paths_cut, "paths_clipped": stats.paths_clipped,
        "annotations_removed": stats.annots, "notes": stats.notes,
    }
    res = finalize(dst, src, options=o, data=data, quiet=quiet)
    return res


def _draw_marks(pdf: pikepdf.Pdf, page: pikepdf.Page, boxes: list[UBox], fill, label: str | None) -> None:
    r, g, b = fill
    ops = [f"q {r:g} {g:g} {b:g} rg"]
    for bx in boxes:
        ops.append(f"{bx.x0:.3f} {bx.y0:.3f} {bx.x1 - bx.x0:.3f} {bx.y1 - bx.y0:.3f} re f")
    ops.append("Q")
    page.contents_coalesce()
    data = page.obj.Contents.read_bytes()
    page.obj.Contents = pdf.make_stream(b"q\n" + data + b"\nQ\n" + "\n".join(ops).encode() + b"\n")
    if label:
        from pdfcore.overlay import stamp_pages, user_to_visual
        from pdfcore.fonts import register_reportlab

        f = register_reportlab()
        idx = [i for i, p in enumerate(pdf.pages) if p.obj.objgen == page.obj.objgen]

        def draw(c, i, w, h):
            c.setFillColorRGB(1, 1, 1)
            for bx in boxes:
                u0, v0 = user_to_visual(page, bx.x0, bx.y0)
                u1, v1 = user_to_visual(page, bx.x1, bx.y1)
                x0, x1 = sorted((u0, u1))
                y0, y1 = sorted((v0, v1))
                size = max(4.0, min(9.0, (y1 - y0) * 0.7))
                c.setFont(f["sans-bold"], size)
                c.drawCentredString((x0 + x1) / 2, (y0 + y1) / 2 - size * 0.35, label)

        stamp_pages(pdf, draw, idx, tag="RedactLabel")


# ----------------------------------------------------------------------------- CLI
def _parse_area(s: str) -> tuple[int, Rect]:
    """'PAGE:x0,top,x1,bottom' in points (1-based page)."""
    m = re.fullmatch(r"\s*(\d+)\s*:\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*", s)
    if not m:
        raise ValueError(f"bad --area {s!r}; use PAGE:x0,top,x1,bottom (points, top-left origin)")
    p, *v = m.groups()
    return int(p) - 1, Rect(*(float(x) for x in v))


@command("redact", "True redaction by area, word, or pattern (ssn, phone, email, ein, card); scrubs hidden data", group="redact")
def _cli(p):
    p.add_argument("pdf")
    p.add_argument("--term", action="append", default=[], help="word or phrase to redact (repeatable)")
    p.add_argument("--pattern", action="append", default=[], choices=sorted(PATTERNS), help="built-in pattern (repeatable)")
    p.add_argument("--regex", action="append", default=[], help="custom regular expression (repeatable)")
    p.add_argument("--area", action="append", default=[], help="PAGE:x0,top,x1,bottom in points (repeatable)")
    p.add_argument("--pages", help="limit searching to these pages (e.g. 1-3,5)")
    p.add_argument("--case", action="store_true", help="case-sensitive term matching")
    p.add_argument("--whole-word", action="store_true")
    p.add_argument("--label", help='text printed on each box, e.g. "(b)(6)"')
    p.add_argument("--keep-metadata", action="store_true")
    p.add_argument("--keep-comments", action="store_true")
    p.add_argument("--keep-attachments", action="store_true")
    p.add_argument("--keep-layers", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="list what would be redacted")
    add_common(p)

    def run(ns):
        import pikepdf as _pk

        with _pk.open(ns.pdf, password=ns.password or "") as d:
            n = len(d.pages)
        pages = parse_pages(ns.pages, n) if ns.pages else None
        areas = [_parse_area(a) for a in ns.area]
        if ns.dry_run:
            found = find_areas(ns.pdf, terms=ns.term, patterns=ns.pattern, regexes=ns.regex, pages=pages, case=ns.case,
                               whole_word=ns.whole_word, password=ns.password)
            for a in [Area(p_, r) for p_, r in areas] + found:
                print(f"p.{a.page + 1}  {a.rect.x0:.1f},{a.rect.top:.1f},{a.rect.x1:.1f},{a.rect.bottom:.1f}  {a.text}")
            return None
        sc = Scrub(metadata=not ns.keep_metadata, comments=not ns.keep_comments, attachments=not ns.keep_attachments,
                   hidden_layers=not ns.keep_layers)
        return redact(ns.pdf, areas=areas, terms=ns.term, patterns=ns.pattern, regexes=ns.regex, pages=pages, case=ns.case,
                      whole_word=ns.whole_word, label=ns.label, scrub=sc, out=ns.out, password=ns.password,
                      options=gate_options_from(ns))

    return run
