"""Text with positions: chars, words, lines, blocks, and search with hit rectangles.

All rectangles: points, top-left origin, *unrotated page space* as pdfplumber reports it
(i.e. relative to the MediaBox; /Rotate is not applied).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from pdfcore import engines
from pdfcore.types import Block, Hit, Line, Rect, Word


def words(path: str | Path, page: int, *, password: str | None = None, engine: str | None = None) -> list[Word]:
    return engines.get("text", engine).words(Path(path), page, password=password)


def group_lines(ws: Iterable[Word], tol: float = 0.45) -> list[Line]:
    """Group words into lines: same baseline band (vertical overlap ≥ tol·height), sorted left→right."""
    ws = sorted(ws, key=lambda w: (round(w.rect.top, 1), w.rect.x0))
    lines: list[list[Word]] = []
    for w in ws:
        placed = False
        for ln in reversed(lines[-4:]):
            ref = ln[0].rect
            ov = min(ref.bottom, w.rect.bottom) - max(ref.top, w.rect.top)
            h = min(ref.height, w.rect.height) or 1
            if ov / h >= tol:
                ln.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])
    out = []
    for k, ln in enumerate(lines):
        ln.sort(key=lambda w: w.rect.x0)
        # split a "line" that spans two columns separated by a wide gap
        segs: list[list[Word]] = [[ln[0]]]
        for w in ln[1:]:
            prev = segs[-1][-1]
            gap = w.rect.x0 - prev.rect.x1
            if gap > max(3.5 * (prev.size or prev.rect.height), 24):
                segs.append([w])
            else:
                segs[-1].append(w)
        for s in segs:
            for w in s:
                w.line_no = len(out)
            out.append(Line(s, s[0].page))
    out.sort(key=lambda l: (l.rect.x0 > 0 and 0, round(l.rect.top, 1), l.rect.x0))
    return out


def group_blocks(lines: list[Line], gap_factor: float = 0.9) -> list[Block]:
    """Group lines into blocks (paragraphs): consecutive lines with small vertical gaps and horizontal overlap."""
    blocks: list[list[Line]] = []
    for ln in sorted(lines, key=lambda l: (round(l.rect.top, 1), l.rect.x0)):
        r = ln.rect
        target = None
        for b in reversed(blocks[-6:]):
            last = b[-1].rect
            gap = r.top - last.bottom
            hov = min(last.x1, r.x1) - max(last.x0, r.x0)
            if -1 <= gap <= gap_factor * max(last.height, r.height) and hov > 0:
                target = b
                break
        if target is None:
            blocks.append([ln])
        else:
            target.append(ln)
    out = []
    for k, b in enumerate(blocks):
        for ln in b:
            ln.block_no = k
            for w in ln.words:
                w.block_no = k
        out.append(Block(b, b[0].page))
    return out


def lines(path: str | Path, page: int, **kw) -> list[Line]:
    return group_lines(words(path, page, **kw))


def blocks(path: str | Path, page: int, **kw) -> list[Block]:
    return group_blocks(lines(path, page, **kw))


def page_text(path: str | Path, page: int, **kw) -> str:
    return "\n\n".join(b.text for b in blocks(path, page, **kw))


def search(
    path: str | Path,
    query: str,
    *,
    regex: bool = False,
    case: bool = False,
    whole_word: bool = False,
    pages: Iterable[int] | None = None,
    password: str | None = None,
) -> list[Hit]:
    """Find text; each Hit has one rectangle per line the match spans."""
    import pdfplumber

    pat = query if regex else re.escape(query)
    if whole_word:
        pat = rf"(?<!\w){pat}(?!\w)"
    flags = 0 if case else re.I
    rx = re.compile(pat, flags)
    hits: list[Hit] = []
    with pdfplumber.open(str(path), password=password or "") as pdf:
        idx = range(len(pdf.pages)) if pages is None else pages
        for pi in idx:
            page = pdf.pages[pi]
            try:
                matches = page.search(rx, regex=True, case=case, return_chars=True, return_groups=False)
            except Exception:
                continue
            for m in matches:
                chars = m.get("chars") or []
                rects = _line_rects(chars) if chars else [Rect(m["x0"], m["top"], m["x1"], m["bottom"])]
                hits.append(Hit(m["text"], pi, rects))
    return hits


def _line_rects(chars: list[dict]) -> list[Rect]:
    rows: list[list[dict]] = []
    for c in sorted(chars, key=lambda c: (round(c["top"], 0), c["x0"])):
        if rows and abs(rows[-1][0]["top"] - c["top"]) < 0.5 * (c["bottom"] - c["top"] or 1):
            rows[-1].append(c)
        else:
            rows.append([c])
    return [Rect(min(c["x0"] for c in r), min(c["top"] for c in r), max(c["x1"] for c in r), max(c["bottom"] for c in r)) for r in rows]
