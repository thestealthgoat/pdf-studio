"""Builtin permissive engines: pypdfium2 (render) and pdfplumber (text, vectors)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from pdfcore.types import Char, PathItem, Rect, Word


class PdfiumRender:
    name = "pdfium"

    def page_count(self, path: Path, password: str | None = None) -> int:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(path), password=password)
        try:
            return len(doc)
        finally:
            doc.close()

    def render(self, path: Path, page: int, dpi: float = 150, password: str | None = None) -> Image.Image:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(path), password=password)
        try:
            pg = doc[page]
            bm = pg.render(scale=dpi / 72.0, may_draw_forms=True, draw_annots=True)
            img = bm.to_pil().convert("RGB")
            pg.close()
            return img
        finally:
            doc.close()


def _color(c) -> tuple[float, ...] | None:
    if c is None:
        return None
    if isinstance(c, (int, float)):
        return (float(c),)
    try:
        return tuple(float(v) for v in c)
    except TypeError:
        return None


class PlumberText:
    name = "pdfplumber"

    def _open(self, path: Path, password: str | None):
        import pdfplumber

        return pdfplumber.open(str(path), password=password or "")

    def page_size(self, path: Path, page: int, password: str | None = None) -> tuple[float, float]:
        with self._open(path, password) as pdf:
            p = pdf.pages[page]
            return float(p.width), float(p.height)

    def chars(self, path: Path, page: int, password: str | None = None) -> list[Char]:
        with self._open(path, password) as pdf:
            p = pdf.pages[page]
            return [
                Char(c["text"], Rect(c["x0"], c["top"], c["x1"], c["bottom"]), c.get("fontname", ""), float(c.get("size", 0)))
                for c in p.chars
            ]

    def words(self, path: Path, page: int, password: str | None = None) -> list[Word]:
        with self._open(path, password) as pdf:
            p = pdf.pages[page]
            raw = p.extract_words(keep_blank_chars=False, use_text_flow=True, extra_attrs=["fontname", "size"])
            return [
                Word(w["text"], Rect(w["x0"], w["top"], w["x1"], w["bottom"]), page, w.get("fontname", ""), float(w.get("size", 0)))
                for w in raw
            ]

    def paths(self, path: Path, page: int, password: str | None = None) -> list[PathItem]:
        out: list[PathItem] = []
        with self._open(path, password) as pdf:
            p = pdf.pages[page]
            for ln in p.lines:
                pts = [(float(x), float(y)) for x, y in ln.get("pts", [])] or [
                    (ln["x0"], ln["top"]),
                    (ln["x1"], ln["bottom"]),
                ]
                out.append(PathItem("line", pts, _color(ln.get("stroking_color")), None, float(ln.get("linewidth", 0)), page))
            for r in p.rects:
                pts = [(r["x0"], r["top"]), (r["x1"], r["top"]), (r["x1"], r["bottom"]), (r["x0"], r["bottom"])]
                out.append(
                    PathItem(
                        "rect",
                        pts,
                        _color(r.get("stroking_color")) if r.get("stroke") else None,
                        _color(r.get("non_stroking_color")) if r.get("fill") else None,
                        float(r.get("linewidth", 0)),
                        page,
                    )
                )
            for c in p.curves:
                pts = [(float(x), float(y)) for x, y in c.get("pts", [])]
                if pts:
                    out.append(
                        PathItem(
                            "curve",
                            pts,
                            _color(c.get("stroking_color")) if c.get("stroke") else None,
                            _color(c.get("non_stroking_color")) if c.get("fill") else None,
                            float(c.get("linewidth", 0)),
                            page,
                        )
                    )
        return out


def register(reg) -> None:
    reg("render", "pdfium", PdfiumRender, default=True, source="pdf-studio")
    reg("text", "pdfplumber", PlumberText, default=True, source="pdf-studio")
