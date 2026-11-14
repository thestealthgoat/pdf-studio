"""`pdfcore-words/1`: the word-level JSON contract shared with BAR Estimating.

Schema: ``schemas/pdfcore-words-1.schema.json``. Exact rules: ``docs/pdfcore-words-1.md`` (every threshold and
tie-break, so BAR's pdf.js reader reproduces the output). Engine: PDFium via pypdfium2. In short: glyphs in
CropBox ∩ MediaBox space, grouped in the page's dominant reading frame (so ``/Rotate`` never changes grouping),
effective font size (Tf × matrix y-scale), font ascent/descent boxes (0.8/−0.2 for non-embedded fonts), word gap
0.15·size, lines, blocks, horizontal-first XY-cut; output in display coordinates, rounded to 0.01, deterministic.
"""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SCHEMA = "pdfcore-words/1"
WORD_GAP = 0.15  # × size: a larger horizontal gap between glyphs starts a new word
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}


class WordsError(Exception):
    """Processing error with a contract code: Encrypted | Damaged | NotPdf | TooLarge | Timeout."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Glyph:
    ch: str
    x0: float
    top: float
    x1: float
    bottom: float
    baseline: float
    size: float
    font: str


@dataclass
class _Word:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    baseline: float
    size: float
    font: str
    line: int = 0
    block: int = 0


@dataclass
class _Line:
    words: list[_Word] = field(default_factory=list)

    @property
    def x0(self): return min(w.x0 for w in self.words)
    @property
    def x1(self): return max(w.x1 for w in self.words)
    @property
    def top(self): return min(w.top for w in self.words)
    @property
    def bottom(self): return max(w.bottom for w in self.words)


@dataclass
class _Block:
    lines: list[_Line] = field(default_factory=list)

    @property
    def x0(self): return min(l.x0 for l in self.lines)
    @property
    def x1(self): return max(l.x1 for l in self.lines)
    @property
    def top(self): return min(l.top for l in self.lines)
    @property
    def bottom(self): return max(l.bottom for l in self.lines)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", "".join(LIGATURES.get(c, c) for c in s))


def _strip_subset(name: str) -> str:
    if len(name) > 7 and name[6] == "+" and name[:6].isalpha() and name[:6].isupper():
        return name[7:]
    return name


# ----------------------------------------------------------------------------- PDFium extraction
# Unicode White_Space (spelled out: Python's str.isspace() also matches U+001C–U+001F, JS \s also matches U+FEFF)
WHITESPACE = frozenset("\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
                       "\u200a\u2028\u2029\u202f\u205f\u3000")
FALLBACK_ASCENT, FALLBACK_DESCENT = 0.8, -0.2  # used for every non-embedded font (readers' substitutes differ)


@dataclass
class _Raw:
    """One glyph in unrotated visible-box space (u right, v down, origin top-left of CropBox ∩ MediaBox)."""
    ch: str
    u0: float
    v0: float
    u1: float
    v1: float
    ou: float
    ov: float
    size: float      # effective size: Tf × y-scale of the glyph's matrix
    font: str
    embedded: bool
    direction: int   # writing direction in unrotated space, 0/90/180/270 clockwise


def _rot_pt(x: float, y: float, r: int, w: float, h: float) -> tuple[float, float]:
    """Rotate a point of a w×h top-left-origin box clockwise by r degrees."""
    if r == 90:
        return h - y, x
    if r == 180:
        return w - x, h - y
    if r == 270:
        return y, w - x
    return x, y


def _rot_box(x0: float, y0: float, x1: float, y1: float, r: int, w: float, h: float):
    ax, ay = _rot_pt(x0, y0, r, w, h)
    bx, by = _rot_pt(x1, y1, r, w, h)
    return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)


def _page_glyphs(pdf, page_index: int):
    """Glyphs of one page in unrotated visible-box space; returns (raws, W, H, rotation)."""
    import ctypes
    import math

    import pypdfium2.raw as R

    page = pdf[page_index]
    try:
        rot = int(page.get_rotation()) % 360
        l, b, r, t = (float(v) for v in page.get_cropbox())
        ml, mb, mr, mt = (float(v) for v in page.get_mediabox())
        l, b, r, t = max(l, ml), max(b, mb), min(r, mr), min(t, mt)
        W, H = r - l, t - b
        tp = page.get_textpage()
        raw = tp.raw
        n = R.FPDFText_CountChars(raw)
        out: list[_Raw] = []
        rect, m = R.FS_RECTF(), R.FS_MATRIX()
        ox, oy = ctypes.c_double(), ctypes.c_double()
        buf = ctypes.create_string_buffer(256)
        flags = ctypes.c_int()
        fonts: dict[str, str] = {}
        embedded_cache: dict[int, bool] = {}
        for i in range(n):
            if R.FPDFText_IsGenerated(raw, i) == 1:
                continue
            cp = R.FPDFText_GetUnicode(raw, i)
            if R.FPDFText_IsHyphen(raw, i) == 1:
                ch = "-"  # PDFium reports a line-final hyphen as U+0002
            elif cp == 0:
                continue
            else:
                ch = chr(cp)
            if not R.FPDFText_GetLooseCharBox(raw, i, rect):
                continue
            R.FPDFText_GetCharOrigin(raw, i, ox, oy)
            R.FPDFText_GetMatrix(raw, i, m)
            size = float(R.FPDFText_GetFontSize(raw, i)) * math.hypot(m.c, m.d)
            angle = math.degrees(math.atan2(-m.b, m.a))  # glyph x-axis in v-down space
            direction = int(round(angle / 90.0)) % 4 * 90
            nlen = R.FPDFText_GetFontInfo(raw, i, buf, 256, flags)
            name = buf.raw[: max(0, nlen - 1)].decode("utf-8", "replace") if nlen else ""
            font = fonts.get(name)
            if font is None:
                font = fonts[name] = _strip_subset(name)
            obj = R.FPDFText_GetTextObject(raw, i)
            fh = R.FPDFTextObj_GetFont(obj) if obj else None
            key = ctypes.cast(fh, ctypes.c_void_p).value or 0 if fh else 0
            if key not in embedded_cache:
                embedded_cache[key] = bool(fh) and R.FPDFFont_GetIsEmbedded(fh) == 1
            out.append(_Raw(ch, rect.left - l, t - rect.top, rect.right - l, t - rect.bottom, ox.value - l, t - oy.value,
                            size, font, embedded_cache[key], direction))
        tp.close()
        return out, W, H, rot
    finally:
        page.close()


def _dominant_direction(raws: list[_Raw], rot: int) -> int:
    """Most common writing direction in display space (ties → smallest); 0 for a page without text."""
    counts = {0: 0, 90: 0, 180: 0, 270: 0}
    for g in raws:
        if g.ch not in WHITESPACE:
            counts[(g.direction + rot) % 360] += 1
    return max(counts, key=lambda d: (counts[d], -d))


@dataclass
class _Placed:
    """A block positioned in the dominant frame for ordering, remembering its own frame."""
    block: _Block
    turn: int        # own frame -> display: turn clockwise by this
    fw: float        # own frame size
    fh: float
    x0: float
    top: float
    x1: float
    bottom: float


def _frame_glyphs(raws: list[_Raw], rf: int, W: float, H: float) -> list[_Glyph]:
    """Glyphs in a reading frame: unrotated space turned clockwise by rf (rf is chosen so these glyphs run +x)."""
    out = []
    for g in raws:
        x0, top, x1, bottom = _rot_box(g.u0, g.v0, g.u1, g.v1, rf, W, H)
        bx, by = _rot_pt(g.ou, g.ov, rf, W, H)
        if not g.embedded:
            top, bottom = by - FALLBACK_ASCENT * g.size, by - FALLBACK_DESCENT * g.size
        out.append(_Glyph(g.ch, x0, top, x1, bottom, by, g.size, g.font))
    return out


# ----------------------------------------------------------------------------- grouping
def _words(glyphs: list[_Glyph]) -> list[_Word]:
    words: list[_Word] = []
    cur: list[_Glyph] = []

    def flush():
        if cur:
            text = _norm("".join(g.ch for g in cur))
            if text:
                words.append(_Word(text, min(g.x0 for g in cur), min(g.top for g in cur), max(g.x1 for g in cur),
                                   max(g.bottom for g in cur), cur[0].baseline, max(g.size for g in cur), cur[0].font))
            cur.clear()

    for g in glyphs:
        if g.ch in WHITESPACE:
            flush()
            continue
        if cur:
            p = cur[-1]
            size = max(p.size, g.size, 1.0)
            if abs(g.baseline - p.baseline) > 0.5 * size or g.x0 - p.x1 > WORD_GAP * size or g.x1 < p.x0:
                flush()
        cur.append(g)
    flush()
    return words


def _lines(words: list[_Word]) -> list[_Line]:
    lines: list[_Line] = []
    for w in sorted(words, key=lambda w: (w.baseline, w.x0)):
        placed = False
        for ln in reversed(lines):
            last = ln.words[-1]
            size = max(last.size, w.size, 1.0)
            if abs(last.baseline - w.baseline) <= 0.3 * size and 0 <= w.x0 - last.x1 <= 3 * size:
                ln.words.append(w)
                placed = True
                break
        if not placed:
            lines.append(_Line([w]))
    return lines


def _blocks(lines: list[_Line]) -> list[_Block]:
    blocks: list[_Block] = []
    for ln in sorted(lines, key=lambda l: (l.top, l.x0)):
        target = None
        for bk in reversed(blocks):
            last = bk.lines[-1]
            gap = ln.top - last.bottom
            h = max(last.bottom - last.top, ln.bottom - ln.top, 1.0)
            overlap = min(last.x1, ln.x1) - max(last.x0, ln.x0)
            if -0.5 * h <= gap <= 0.9 * h and overlap > 0:
                target = bk
                break
        if target is None:
            blocks.append(_Block([ln]))
        else:
            target.lines.append(ln)
    return blocks


def _bands(blocks: list[_Block]) -> list[list[_Block]]:
    """Horizontal bands: sorted by (top, x0); a new band starts when top > (max bottom so far) + 0.5."""
    order = sorted(blocks, key=lambda b: (b.top, b.x0))
    runs, reach = [[order[0]]], order[0].bottom
    for b in order[1:]:
        if b.top - reach > 0.5:
            runs.append([b])
        else:
            runs[-1].append(b)
        reach = max(reach, b.bottom)
    return runs


def _columns(blocks: list[_Block], min_gutter: float) -> list[list[_Block]]:
    """Columns: sorted by (x0, top); a new column starts when x0 - (max x1 so far) >= min_gutter."""
    order = sorted(blocks, key=lambda b: (b.x0, b.top))
    runs, reach = [[order[0]]], order[0].x1
    for b in order[1:]:
        if b.x0 - reach >= min_gutter:
            runs.append([b])
        else:
            runs[-1].append(b)
        reach = max(reach, b.x1)
    return runs


def _xy_cut(blocks: list[_Block], min_gutter: float = 12.0) -> list[_Block]:
    """Recursive XY-cut, horizontal bands first, then columns (see docs/pdfcore-words-1.md §6)."""
    if len(blocks) <= 1:
        return list(blocks)
    for groups in (_bands(blocks), _columns(blocks, min_gutter)):
        if len(groups) > 1:
            return [x for g in groups for x in _xy_cut(g, min_gutter)]
    return sorted(blocks, key=lambda b: (b.top, b.x0))


def _r(v: float) -> float:
    x = round(float(v) + 0.0, 2)
    return 0.0 if x == 0 else x  # no negative zero


# ----------------------------------------------------------------------------- public API
def words_document(path: str | Path, *, pages: Iterable[int] | None = None, password: str | None = None,
                   timeout: float | None = None, max_mb: float = 1024.0, version: str | None = None) -> dict:
    """Build the `pdfcore-words/1` document for a PDF. Raises WordsError with a contract code."""
    import pypdfium2 as pdfium

    from pdfcore import __version__

    p = Path(path)
    t0 = time.monotonic()
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise WordsError("NotPdf", f"file not found: {p}")
    except OSError as exc:  # permission denied, a folder, a device error …
        raise WordsError("NotPdf", f"can't read the file ({exc.strerror or exc})")
    if len(data) > max_mb * 1e6:
        raise WordsError("TooLarge", f"{len(data) / 1e6:.0f} MB exceeds the {max_mb:g} MB limit")
    if b"%PDF" not in data[:1024]:
        raise WordsError("NotPdf", "not a PDF file (no %PDF header)")
    try:
        pdf = pdfium.PdfDocument(data, password=password)
    except pdfium.PdfiumError as exc:
        msg = str(exc)
        if "password" in msg.lower():
            raise WordsError("Encrypted", "the PDF is password-protected")
        raise WordsError("Damaged", f"the PDF can't be read ({msg})")
    try:
        import pypdfium2.raw as pdfium_c

        encrypted = pdfium_c.FPDF_GetSecurityHandlerRevision(pdf.raw) != -1  # exact, unlike scanning for /Encrypt
        n = len(pdf)
        idx = list(range(n)) if pages is None else sorted({i for i in pages if 0 <= i < n})
        page_rows, word_rows, any_text = [], [], False
        for pi in idx:
            if timeout is not None and time.monotonic() - t0 > timeout:
                raise WordsError("Timeout", f"stopped after {timeout:g} s at page {pi + 1} of {n}")
            raws, W, H, rot = _page_glyphs(pdf, pi)
            vw, vh = (H, W) if rot in (90, 270) else (W, H)
            page_rows.append({"index": pi, "width": _r(vw), "height": _r(vh), "rotation": rot})
            D = _dominant_direction(raws, rot)
            back = (360 - D) % 360  # display -> dominant frame
            placed: list[_Placed] = []
            for delta in (0, 90, 180, 270):  # each writing direction is grouped in its own frame
                group = [g for g in raws if (g.direction + rot) % 360 == delta]
                if not group:
                    continue
                rf = (rot - delta) % 360
                fw, fh = (H, W) if rf in (90, 270) else (W, H)
                ws = _words(_frame_glyphs(group, rf, W, H))
                any_text = any_text or bool(ws)
                for bk in _blocks(_lines(ws)):
                    dx0, dt, dx1, db = _rot_box(bk.x0, bk.top, bk.x1, bk.bottom, delta, fw, fh)
                    x0, top, x1, bottom = _rot_box(dx0, dt, dx1, db, back, vw, vh)
                    placed.append(_Placed(bk, delta, fw, fh, x0, top, x1, bottom))
            li = 0
            for bi, pl in enumerate(_xy_cut(placed)):
                for ln in sorted(pl.block.lines, key=lambda l: (l.top, l.x0)):
                    for w in sorted(ln.words, key=lambda w: w.x0):
                        x0, top, x1, bottom = _rot_box(w.x0, w.top, w.x1, w.bottom, pl.turn, pl.fw, pl.fh)
                        word_rows.append({"text": w.text, "page": pi, "x0": _r(x0), "top": _r(top), "x1": _r(x1),
                                          "bottom": _r(bottom), "font": w.font, "size": _r(w.size), "line": li, "block": bi})
                    li += 1
        return {
            "schema": SCHEMA,
            "producer": {"name": "pdfcore", "version": version or __version__},
            "source": {"sha256": hashlib.sha256(data).hexdigest(), "pageCount": n, "encrypted": encrypted,
                       "hasTextLayer": any_text},
            "pages": page_rows,
            "words": word_rows,
        }
    finally:
        pdf.close()


def dumps(doc: dict) -> str:
    """Deterministic serialisation: UTF-8, LF, fixed key order, no NaN, trailing newline."""
    return json.dumps(doc, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


def schema_path() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here / "_schemas" / "pdfcore-words-1.schema.json", here.parent / "schemas" / "pdfcore-words-1.schema.json"):
        if cand.exists():
            return cand
    return here.parent / "schemas" / "pdfcore-words-1.schema.json"
