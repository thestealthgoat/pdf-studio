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
            try:
                doc.init_forms()  # required for may_draw_forms to draw field values
            except Exception:
                pass
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


class OdaDwg:
    """ODA File Converter (external program, free download, own license — never bundled)."""

    name = "oda"
    CANDIDATES = [
        "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
        "C:/Program Files/ODA/ODAFileConverter/ODAFileConverter.exe",
    ]

    def exe(self) -> str | None:
        import shutil

        return shutil.which("ODAFileConverter") or next((c for c in self.CANDIDATES if Path(c).exists()), None)

    def installed(self) -> bool:
        return self.exe() is not None

    def to_dxf(self, src: Path, out_dir: Path, version: str = "ACAD2018") -> Path:
        import shutil
        import subprocess
        import tempfile

        exe = self.exe()
        if not exe:
            raise RuntimeError("ODA File Converter not installed")
        with tempfile.TemporaryDirectory() as tin:
            shutil.copy(src, Path(tin) / src.name)
            subprocess.run([exe, tin, str(out_dir), version, "DXF", "0", "1", src.name], check=True,
                           capture_output=True, timeout=600)
        out = Path(out_dir) / (src.stem + ".dxf")
        if not out.exists():
            raise RuntimeError(f"ODA File Converter produced no DXF for {src.name}")
        return out

    def from_dxf(self, src: Path, out_dir: Path, version: str = "ACAD2018") -> Path:
        """DXF → DWG (ODA writes DWG; used only if the user installed ODA)."""
        import shutil
        import subprocess
        import tempfile

        exe = self.exe()
        if not exe:
            raise RuntimeError("ODA File Converter not installed")
        src = Path(src)
        with tempfile.TemporaryDirectory() as tin:
            shutil.copy(src, Path(tin) / src.name)
            subprocess.run([exe, tin, str(out_dir), version, "DWG", "0", "1", src.name], check=True,
                           capture_output=True, timeout=600)
        out = Path(out_dir) / (src.stem + ".dwg")
        if not out.exists():
            raise RuntimeError(f"ODA File Converter produced no DWG for {src.name}")
        return out


class NativeDwg:
    """Built-in clean-room DWG reader (pdfcore.ops.plans.dwg, MIT; R2000–R2018). Needs only ezdxf.

    Reads DWG only (no ``from_dxf``). ``last_report`` holds the :class:`ReadReport` of the last conversion.
    """

    name = "native"

    def __init__(self) -> None:
        self.last_report = None

    def installed(self) -> bool:
        return True

    def can_read(self, src: Path) -> bool:
        from pdfcore.ops.plans.dwg import can_read

        return can_read(src)

    def to_dxf(self, src: Path, out_dir: Path) -> Path:
        from pdfcore.ops.plans.dwg import read_dwg

        src = Path(src)
        doc, report = read_dwg(src)
        self.last_report = report
        out = Path(out_dir) / (src.stem + ".dxf")
        doc.saveas(str(out))
        return out


def register(reg) -> None:
    reg("render", "pdfium", PdfiumRender, default=True, source="pdf-studio")
    reg("text", "pdfplumber", PlumberText, default=True, source="pdf-studio")
    reg("dwg", "oda", OdaDwg, default=False, source="pdf-studio")
    reg("dwg", "native", NativeDwg, default=False, source="pdf-studio")
