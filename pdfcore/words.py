"""`pdfcore-words/1`: the frozen word-level JSON contract shared with BAR Estimating.

Schema: ``schemas/pdfcore-words-1.schema.json`` (source of truth). Engine: PDFium via pypdfium2 (fast, permissive).

Rules (so another reader — e.g. BAR's pdf.js reader — can reproduce the output within ±1 pt):

* **Coordinates** — PDF points, origin at the top-left of the page's visible box (CropBox ∩ MediaBox, what
  pdf.js calls the viewport), after applying ``/Rotate``. ``x0 < x1``, ``top < bottom``.
* **Glyph boxes** — horizontally the glyph advance; vertically ``top = baseline − ascent·size`` and
  ``bottom = baseline − descent·size`` from the font's ascent/descent (PDFium "loose" char boxes).
* **Text normalisation** — ligatures U+FB00–U+FB06 are expanded to their letters, then Unicode NFC.
  PDFium's synthesised (generated) spaces/newlines are ignored.
* **Words** — a word ends at any whitespace character, at a change of baseline (> 0.5·size), or where the
  horizontal gap between consecutive glyph boxes exceeds 0.2·size.
* **Lines** — words whose baselines differ by ≤ 0.3·size and whose horizontal gap is ≤ 3·size join a line
  (left to right); otherwise a new line starts.
* **Blocks** — lines stacked with a vertical gap ≤ 0.9·line height that overlap horizontally join a block.
  Blocks are ordered by a recursive XY-cut (bands before columns), lines within a block top-to-bottom,
  words left-to-right. ``line`` and ``block`` are numbered 0-based per page in that reading order.
* **Numbers** rounded to 0.01; output is deterministic (same input + version → identical bytes).
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
def _page_glyphs(pdf, page_index: int):
    """Glyphs of one page in visible-box, top-left, rotated coordinates; returns (glyphs, width, height, rotation)."""
    import ctypes

    import pypdfium2.raw as R

    page = pdf[page_index]
    try:
        rot = int(page.get_rotation()) % 360
        l, b, r, t = (float(v) for v in page.get_cropbox())
        ml, mb, mr, mt = (float(v) for v in page.get_mediabox())
        l, b, r, t = max(l, ml), max(b, mb), min(r, mr), min(t, mt)
        W, H = r - l, t - b

        def to_view(x: float, y: float) -> tuple[float, float]:
            u, v = x - l, t - y  # unrotated, top-left origin
            if rot == 90:
                return H - v, u
            if rot == 180:
                return W - u, H - v
            if rot == 270:
                return v, W - u
            return u, v

        vw, vh = (H, W) if rot in (90, 270) else (W, H)
        tp = page.get_textpage()
        raw = tp.raw
        n = R.FPDFText_CountChars(raw)
        out: list[_Glyph] = []
        L, B, Rr, T = (ctypes.c_double() for _ in range(4))
        rect = R.FS_RECTF()
        ox, oy = ctypes.c_double(), ctypes.c_double()
        buf = ctypes.create_string_buffer(256)
        flags = ctypes.c_int()
        font_cache: dict[int, str] = {}
        for i in range(n):
            if R.FPDFText_IsGenerated(raw, i) == 1:
                continue
            cp = R.FPDFText_GetUnicode(raw, i)
            if cp == 0:
                continue
            ch = chr(cp)
            if ch in "\r\n":
                ch = " "
            if not R.FPDFText_GetLooseCharBox(raw, i, rect):
                continue
            R.FPDFText_GetCharOrigin(raw, i, ox, oy)
            size = float(R.FPDFText_GetFontSize(raw, i))
            nlen = R.FPDFText_GetFontInfo(raw, i, buf, 256, flags)
            font = buf.raw[: max(0, nlen - 1)].decode("utf-8", "replace") if nlen else ""
            font = font_cache.setdefault(hash(font), _strip_subset(font))
            corners = [to_view(rect.left, rect.top), to_view(rect.right, rect.bottom)]
            xs, ys = [c[0] for c in corners], [c[1] for c in corners]
            bx, by = to_view(ox.value, oy.value)
            baseline = by if rot in (0, 180) else bx
            out.append(_Glyph(ch, min(xs), min(ys), max(xs), max(ys), baseline, size, font))
        tp.close()
        return out, vw, vh, rot
    finally:
        page.close()


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
        if g.ch.isspace():
            flush()
            continue
        if cur:
            p = cur[-1]
            size = max(p.size, g.size, 1.0)
            if abs(g.baseline - p.baseline) > 0.5 * size or g.x0 - p.x1 > 0.2 * size or g.x1 < p.x0:
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
    if len(data) > max_mb * 1e6:
        raise WordsError("TooLarge", f"{len(data) / 1e6:.0f} MB exceeds the {max_mb:g} MB limit")
    if b"%PDF" not in data[:1024]:
        raise WordsError("NotPdf", "not a PDF file (no %PDF header)")
    encrypted = b"/Encrypt" in data[-65536:] or b"/Encrypt" in data[:65536]
    try:
        pdf = pdfium.PdfDocument(data, password=password)
    except pdfium.PdfiumError as exc:
        msg = str(exc)
        if "password" in msg.lower():
            raise WordsError("Encrypted", "the PDF is password-protected")
        raise WordsError("Damaged", f"the PDF can't be read ({msg})")
    try:
        n = len(pdf)
        idx = list(range(n)) if pages is None else [i for i in pages if 0 <= i < n]
        page_rows, word_rows, any_text = [], [], False
        for pi in idx:
            if timeout is not None and time.monotonic() - t0 > timeout:
                raise WordsError("Timeout", f"stopped after {timeout:g} s at page {pi + 1} of {n}")
            glyphs, W, H, rot = _page_glyphs(pdf, pi)
            page_rows.append({"index": pi, "width": _r(W), "height": _r(H), "rotation": rot})
            ws = _words(glyphs)
            if ws:
                any_text = True
            blocks = _xy_cut(_blocks(_lines(ws)))
            li = 0
            for bi, bk in enumerate(blocks):
                for ln in sorted(bk.lines, key=lambda l: (l.top, l.x0)):
                    for w in sorted(ln.words, key=lambda w: w.x0):
                        word_rows.append({"text": w.text, "page": pi, "x0": _r(w.x0), "top": _r(w.top), "x1": _r(w.x1),
                                          "bottom": _r(w.bottom), "font": w.font, "size": _r(w.size), "line": li, "block": bi})
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
