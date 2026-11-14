"""CAD interchange and electrical schematics.

``dxf_to_pdf``   DXF (DWG via the installed ``dwg`` engine, HPGL/2 .plt via ezdxf's hpgl2 add-on) → vector PDF,
                 with real line weights, paper sizes, plot-to-scale, layouts, monochrome, optional title block.
``pdf_to_dxf``   PDF page vectors and text → DXF (LINE, LWPOLYLINE, CIRCLE, ELLIPSE, SPLINE, HATCH, TEXT)
                 with layers by colour and line weight, in inches or real-world units at a given scale.
``schematic``    YAML → ladder (JIC / IEC) control schematic PDF with line numbers, wire numbers per node,
                 coil/contact cross-references, BOM and terminal/wire lists (also to XLSX).

Rendering uses ezdxf's Matplotlib drawing backend only (never its PyMuPDF backend). Text in DXF plots is
re-drawn as real text in the embedded Inter font (TrueType, ``pdf.fonttype 42``), so plotted sheets stay
searchable and pass the gate's no-Type-3 rule.

CLI::

    pdfcore plans dxf2pdf drawing.dxf --size arch-d --scale '1/4"=1'"'"'-0"' --lineweights by-layer
    pdfcore plans pdf2dxf set.pdf --sheet E-101 [--scale '1/4"=1'"'"'-0"']
    pdfcore plans schematic ladder.yaml [--xlsx]
"""

from __future__ import annotations

import argparse
import fnmatch
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pdfcore.ops._common import finalize, output_path
from pdfcore.ops.plans import sub
from pdfcore.result import OpResult
from pdfcore.types import Rect

__all__ = [
    "PAPER_SIZES", "paper_size", "parse_scale", "parse_length_in", "Scale", "find_sheet_pages",
    "dxf_to_pdf", "load_cad", "load_cad_ex", "pdf_to_dxf", "schematic", "load_ladder",
    "drawing_area", "LINEWEIGHT_POLICIES",
]

INCH = 72.0
MM = 72.0 / 25.4

# ============================================================== paper sizes and scales
# landscape (width, height) in inches
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "ansi-a": (11, 8.5), "ansi-b": (17, 11), "ansi-c": (22, 17), "ansi-d": (34, 22), "ansi-e": (44, 34),
    "arch-a": (12, 9), "arch-b": (18, 12), "arch-c": (24, 18), "arch-d": (36, 24), "arch-e": (48, 36),
    "arch-e1": (42, 30), "letter": (11, 8.5), "tabloid": (17, 11), "ledger": (17, 11),
    "iso-a4": (297 / 25.4, 210 / 25.4), "iso-a3": (420 / 25.4, 297 / 25.4), "iso-a2": (594 / 25.4, 420 / 25.4),
    "iso-a1": (841 / 25.4, 594 / 25.4), "iso-a0": (1189 / 25.4, 841 / 25.4),
}
_PAPER_ALIASES = {"a": "ansi-a", "b": "ansi-b", "c": "ansi-c", "d": "ansi-d", "e": "ansi-e",
                  "a4": "iso-a4", "a3": "iso-a3", "a2": "iso-a2", "a1": "iso-a1", "a0": "iso-a0"}
# sizes the drawing-sheet template knows
TEMPLATE_SIZES = ("ansi-a", "ansi-b", "ansi-c", "ansi-d", "ansi-e", "arch-a", "arch-b", "arch-c", "arch-d", "arch-e")


def _norm_size(name: str) -> str:
    s = re.sub(r"[\s_]+", "-", name.strip().lower())
    s = re.sub(r"^(ansi|arch|iso)-?", r"\1-", s)
    return _PAPER_ALIASES.get(s, s)


def paper_size(name: str | Sequence[float], orientation: str = "landscape") -> tuple[float, float]:
    """Paper size in inches (w, h). Names: ANSI A–E, ARCH A–E1, letter/tabloid, ISO A0–A4, or ``36x24`` /
    ``841x594mm``. *orientation*: landscape | portrait."""
    if not isinstance(name, str):
        w, h = float(name[0]), float(name[1])
    else:
        key = _norm_size(name)
        if key in PAPER_SIZES:
            w, h = PAPER_SIZES[key]
        else:
            m = re.fullmatch(r"\s*([\d.]+)\s*[x×]\s*([\d.]+)\s*(in|mm|cm)?\s*", name.lower())
            if not m:
                raise ValueError(f"unknown paper size {name!r}; use {', '.join(sorted(PAPER_SIZES))} or WxH[in|mm]")
            f = {"mm": 1 / 25.4, "cm": 1 / 2.54}.get(m.group(3) or "in", 1.0)
            w, h = float(m.group(1)) * f, float(m.group(2)) * f
    w, h = max(w, h), min(w, h)
    return (h, w) if orientation.startswith("p") else (w, h)


_LEN_UNITS = {"in": 1.0, '"': 1.0, "ft": 12.0, "'": 12.0, "mm": 1 / 25.4, "cm": 1 / 2.54, "m": 39.37007874, "yd": 36.0}


def _num(s: str) -> float:
    s = s.strip()
    m = re.fullmatch(r"(\d+)\s+(\d+)/(\d+)", s)  # 1 1/2
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.fullmatch(r"(\d+(?:\.\d*)?)/(\d+(?:\.\d*)?)", s)
    if m:
        return float(m.group(1)) / float(m.group(2))
    return float(s)


def parse_length_in(s: str | float, default_unit: str = "in") -> float:
    """Length → inches: ``1'-6"``, ``1'6"``, ``20'``, ``1/4"``, ``1 1/2in``, ``6mm``, ``2.5``, ``72pt``."""
    if isinstance(s, (int, float)):
        return float(s) * _LEN_UNITS[default_unit]
    t = s.strip().lower().replace("’", "'").replace("”", '"').replace("″", '"').replace("′", "'")
    m = re.fullmatch(r"([\d.\s/]+?)\s*'\s*-?\s*(?:([\d.\s/]+?)\s*(?:\"|in)?)?", t)
    if m and "'" in t:
        ft = _num(m.group(1))
        inch = _num(m.group(2)) if m.group(2) and m.group(2).strip() else 0.0
        return ft * 12 + inch
    m = re.fullmatch(r"([\d.\s/]+?)\s*(\"|in|inch|inches|ft|feet|mm|cm|m|yd|pt)?", t)
    if not m:
        raise ValueError(f"cannot parse length {s!r}")
    unit = m.group(2) or default_unit
    if unit == "pt":
        return _num(m.group(1)) / INCH
    unit = {"inch": "in", "inches": "in", "feet": "ft"}.get(unit, unit)
    return _num(m.group(1)) * _LEN_UNITS[unit]


@dataclass(frozen=True)
class Scale:
    """A drawing scale: *ratio* real lengths per paper length (1/4"=1'-0" → 48)."""

    ratio: float
    text: str
    units: str = "in"  # suggested real-world output units: in | ft | mm | m

    def paper_in_per_unit(self, unit_in: float) -> float:
        """Paper inches for one drawing unit (whose real size is *unit_in* inches)."""
        return unit_in / self.ratio


def parse_scale(s: str | float | None) -> Scale | None:
    """``1/4"=1'-0"`` · ``1"=20'`` · ``1:50`` · ``1/100`` · ``full`` · ``48`` (ratio). ``fit``/``nts``/None → None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return Scale(float(s), f"1:{s:g}")
    t = s.strip()
    low = t.lower()
    if low in ("", "fit", "nts", "none", "auto", "as noted"):
        return None
    if low in ("full", "1:1", "1=1"):
        return Scale(1.0, "1:1")
    m = re.fullmatch(r"\s*1\s*:\s*([\d.]+)\s*", low) or re.fullmatch(r"\s*1\s*/\s*([\d.]+)\s*", low)
    if m and "=" not in low:
        r = float(m.group(1))
        return Scale(r, f"1:{r:g}", "mm" if r in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000) else "in")
    if "=" in low:
        a, b = low.split("=", 1)
        pa, rb = parse_length_in(a), parse_length_in(b)
        if pa <= 0:
            raise ValueError(f"bad scale {s!r}")
        units = "ft" if ("'" in b or "ft" in b) else "m" if re.search(r"\dm\b", b) else "mm" if "mm" in b else "in"
        return Scale(rb / pa, t, units)
    try:
        r = float(low)
        return Scale(r, f"1:{r:g}")
    except ValueError:
        raise ValueError(f"cannot parse scale {s!r}; examples: 1/4\"=1'-0\", 1\"=20', 1:50") from None


def _scale_text(ratio: float) -> str:
    """Friendly text for a ratio: architectural if it matches a common one."""
    for txt, r in (('1/16"=1\'-0"', 192), ('3/32"=1\'-0"', 128), ('1/8"=1\'-0"', 96), ('3/16"=1\'-0"', 64),
                   ('1/4"=1\'-0"', 48), ('3/8"=1\'-0"', 32), ('1/2"=1\'-0"', 24), ('3/4"=1\'-0"', 16),
                   ('1"=1\'-0"', 12), ('1-1/2"=1\'-0"', 8), ('3"=1\'-0"', 4), ('1"=10\'', 120), ('1"=20\'', 240),
                   ('1"=30\'', 360), ('1"=40\'', 480), ('1"=50\'', 600), ('1"=100\'', 1200), ("1:1", 1)):
        if abs(ratio - r) / r < 1e-6:
            return txt
    return f"1:{ratio:.4g}"


# ============================================================== sheet lookup (shared with issue.py)
def find_sheet_pages(src: str | Path, sheets: Iterable[str], password: str | None = None) -> list[int]:
    """0-based page indexes for sheet numbers (E-101 …): the page where the number is printed largest
    (the title-block sheet number), so cross-references like "SEE E-101" do not match."""
    import pdfplumber

    want = [s.strip().upper() for s in sheets if s and s.strip()]
    best: dict[str, tuple[float, int]] = {}
    with pdfplumber.open(str(src), password=password or "") as pdf:
        for pi, page in enumerate(pdf.pages):
            for w in page.extract_words(extra_attrs=["size"]):
                t = w["text"].strip().upper().rstrip(".,;:")
                if t in want:
                    sz = float(w.get("size") or 0)
                    if sz > best.get(t, (0.0, -1))[0]:
                        best[t] = (sz, pi)
    missing = [s for s in want if s not in best]
    if missing:
        raise ValueError(f"sheet(s) not found: {', '.join(missing)}")
    return [best[s][1] for s in want]


def _pages_for(src: Path, *, sheet: str | None, page: str | int | None, n: int, password: str | None = None) -> list[int]:
    from pdfcore.ops._common import parse_pages

    if sheet:
        return find_sheet_pages(src, [s for s in re.split(r"[,\s]+", sheet) if s], password)
    if page is None:
        return list(range(n))
    return parse_pages(str(page), n)


# ============================================================== DXF / DWG / HPGL loading
def _dwg_to_dxf(src: Path, workdir: Path) -> Path:
    """DWG → DXF through the registered ``dwg`` engine (ODA File Converter or a local-edition converter).

    ``engines.dwg_converter()`` raises RuntimeError with install advice when none is installed.
    """
    from pdfcore import engines

    conv = engines.dwg_converter()
    dxf = Path(conv.to_dxf(Path(src), workdir))
    if not dxf.is_file():
        raise RuntimeError(f"DWG converter produced no DXF for {src.name}")
    return dxf


def _read_dxf(path: Path) -> tuple[Any, str]:
    """Strict ``ezdxf.readfile`` first, then ``ezdxf.recover`` (converter output is often slightly off);
    last resort strips invalid ``5 / 0`` handles that some DWG→DXF converters write. Returns (doc, mode)."""
    import ezdxf
    from ezdxf import recover

    try:
        return ezdxf.readfile(str(path)), "strict"
    except Exception:
        pass
    try:
        doc, _aud = recover.readfile(str(path))
        return doc, "recover"
    except Exception as exc:
        first = exc
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and lines[i].strip() == "5" and lines[i + 1].strip() == "0" and i % 2 == 0:
            i += 2
            continue
        out.append(lines[i])
        i += 1
    if not any(ln.strip() == "EOF" for ln in out[-3:]):
        out += ["  0", "EOF"]
    fixed = path.with_name(path.stem + ".fixed.dxf")
    fixed.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        doc, _aud = recover.readfile(str(fixed))
        return doc, "recover+sanitized"
    except Exception:
        raise RuntimeError(f"cannot read {path.name} as DXF: {first}") from first


def load_cad_ex(src: str | Path) -> tuple[Any, str]:
    """Like :func:`load_cad`; also returns how the file was read (strict | recover | recover+sanitized |
    hpgl2), with ``dwg:`` prefixed for DWG input."""
    src = Path(src)
    ext = src.suffix.lower()
    if ext == ".dwg":
        tmp = Path(tempfile.mkdtemp(prefix="pdfcore-dwg-"))
        try:
            doc, mode = _read_dxf(_dwg_to_dxf(src, tmp))
            return doc, "dwg:" + mode
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if ext in (".plt", ".hpgl", ".hpg", ".hgl", ".hp2", ".prn"):
        return _hpgl_to_dxf(src.read_bytes(), src.name), "hpgl2"
    return _read_dxf(src)


def load_cad(src: str | Path, workdir: Path | None = None):
    """Open a DXF, DWG (via the installed ``dwg`` engine) or HPGL/2 plot file as an ezdxf document."""
    return load_cad_ex(src)[0]


def _hpgl_to_dxf(data: bytes, name: str = "plot"):
    """HPGL/2 (or classic HP-GL) plot file → ezdxf document in millimetres.

    Uses the low-level parts of ezdxf's hpgl2 add-on (tokenizer, interpreter, recorder) and its DXF drawing
    backend directly — its ``api`` module is avoided because it imports the PyMuPDF backend.
    """
    try:
        import ezdxf
        from ezdxf.addons import xplayer
        from ezdxf.addons.drawing import dxf as dxf_backend
        from ezdxf.addons.hpgl2.backend import Recorder, placement_matrix
        from ezdxf.addons.hpgl2.interpreter import Interpreter
        from ezdxf.addons.hpgl2.plotter import Plotter
        from ezdxf.addons.hpgl2.tokenizer import hpgl2_commands
        from ezdxf.math import Matrix44
    except Exception as exc:  # pragma: no cover - ezdxf ships the add-on
        raise RuntimeError(f"HPGL/2 support needs ezdxf's hpgl2 add-on ({exc})") from exc
    if b"%1B" not in data[:256] and b"%0B" not in data[:256]:
        data = b"\x1b%1B" + data  # classic HP-GL file without the "enter HPGL/2" escape
    commands = hpgl2_commands(data)
    if not commands:
        raise RuntimeError(f"{name}: no HPGL/2 commands found")
    recorder = Recorder()
    plotter = Plotter(recorder)
    Interpreter(plotter).run(commands)
    player = recorder.player()
    if not player.bbox().has_data:
        raise RuntimeError(f"{name}: the plot file draws nothing")
    if plotter.has_merge_control:
        player.sort_filled_paths()
    player.transform(placement_matrix(player.bbox()))
    doc = ezdxf.new()
    msp = doc.modelspace()
    xplayer.hpgl2_to_drawing(player, dxf_backend.DXFBackend(msp), bg_color="#ffffff")
    m = Matrix44.scale(0.025, 0.025, 0.025)  # 1 plu = 0.025 mm
    for e in msp:
        try:
            e.transform(m)
        except Exception:
            pass
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    return doc


_INSUNITS_IN = {1: 1.0, 2: 12.0, 3: 63360.0, 4: 1 / 25.4, 5: 1 / 2.54, 6: 39.37007874, 7: 39370.07874, 8: 1e-6,
                9: 1e-3, 10: 36.0, 14: 3.937007874}
_UNIT_NAMES = {"in": 1, "inch": 1, "inches": 1, "ft": 2, "feet": 2, "mm": 4, "cm": 5, "m": 6, "yd": 10}


def _unit_inches(doc, units: str | None) -> float:
    if units:
        code = _UNIT_NAMES.get(units.lower())
        if code is None:
            raise ValueError(f"unknown units {units!r}; use in, ft, mm, cm, m")
        return _INSUNITS_IN[code]
    code = int(doc.header.get("$INSUNITS", 0) or 0)
    if code == 0:
        # unitless: metric drawings ($MEASUREMENT 1) are usually mm, imperial ones inches
        return 1 / 25.4 if int(doc.header.get("$MEASUREMENT", 0) or 0) == 1 else 1.0
    return _INSUNITS_IN.get(code, 1.0)


# ============================================================== DXF → PDF
LINEWEIGHT_POLICIES = ("absolute", "relative", "by-layer", "uniform")


def _inter_cap_ratio() -> float:
    try:
        from fontTools.ttLib import TTFont

        from pdfcore.fonts import font_path

        f = TTFont(str(font_path("Inter-Regular.ttf")), lazy=True)
        return float(f["OS/2"].sCapHeight) / float(f["head"].unitsPerEm)
    except Exception:
        return 0.727


def _make_text_pipeline():
    """RenderPipeline2d subclass that records text (final paper-space transform) instead of drawing glyph paths."""
    from ezdxf.addons.drawing.pipeline import RenderPipeline2d, prepare_string_for_rendering
    from ezdxf.math import Matrix44, Vec2, Vec3

    class TextCapture(RenderPipeline2d):
        def __init__(self, backend):
            super().__init__(backend)
            self.texts: list[dict[str, Any]] = []
            self._entity = None

        def enter_entity(self, entity, properties) -> None:
            self._entity = entity
            super().enter_entity(entity, properties)

        def draw_text(self, text, transform, properties, cap_height, dxftype="TEXT") -> None:
            if not text or not text.strip():
                return
            s = prepare_string_for_rendering(text, dxftype)
            local0 = Vec2(transform.transform(Vec3(0, 0, 0)))
            if self.clipping_portal.is_active and self.clipping_portal.clip_point(local0) is None:
                return
            m = self.clipping_portal.transform_matrix(Matrix44(transform))
            o = m.transform(Vec3(0, 0, 0))
            ux = m.transform_direction(Vec3(1, 0, 0))
            uy = m.transform_direction(Vec3(0, 1, 0))
            sy = math.hypot(uy.x, uy.y)
            sx = math.hypot(ux.x, ux.y)
            if sy <= 0 or sx <= 0:
                return
            font_face = properties.font or self.default_font_face
            try:
                w_local = self.text_engine.get_text_line_width(s, font_face, cap_height)
            except Exception:
                w_local = 0.6 * cap_height * len(s)
            ha = "left"
            e = self._entity
            try:
                t = e.dxftype() if e is not None else ""
                if t in ("TEXT", "ATTRIB", "ATTDEF") and dxftype == "TEXT":
                    ha = {1: "center", 2: "right", 3: "center", 4: "center", 5: "center"}.get(int(e.dxf.halign), "left")
                elif t == "MTEXT" and dxftype == "MTEXT":
                    ap = int(e.dxf.get("attachment_point", 1))
                    ha = "center" if ap in (2, 5, 8) else "right" if ap in (3, 6, 9) else "left"
            except Exception:
                pass
            f = {"left": 0.0, "center": 0.5, "right": 1.0}[ha]
            ax, ay = o.x + ux.x * w_local * f, o.y + ux.y * w_local * f
            self.texts.append({
                "text": s, "x": ax, "y": ay, "ha": ha, "angle": math.degrees(math.atan2(ux.y, ux.x)),
                "cap": cap_height * sy, "color": self.get_backend_properties(properties).color,
                "mirror": (ux.x * uy.y - ux.y * uy.x) < 0,
            })

    return TextCapture


def _layout_extents(doc, layout, mode: str | Sequence[float] = "auto", dropped: list[str] | None = None
                    ) -> tuple[float, float, float, float] | None:
    """Plot extents. *mode*: auto (ignore XLINE/RAY and lone far-away outliers, e.g. a block inserted at a
    huge scale) | all (everything, like ZOOM EXTENTS) | (x0, y0, x1, y1) window in drawing units."""
    from ezdxf import bbox

    if not isinstance(mode, str):
        x0, y0, x1, y1 = (float(v) for v in mode)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    if layout.name.lower() != "model" and not layout.is_modelspace:
        try:
            (x1, y1), (x2, y2) = layout.get_paper_limits()
            if x2 - x1 > 1e-6 and y2 - y1 > 1e-6:
                return (x1, y1, x2, y2)
        except Exception:
            pass
    if mode == "all":
        ext = bbox.extents(layout, fast=True)
        if not ext.has_data:
            return None
        return (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
    cache = bbox.Cache()
    boxes: list[tuple[Any, tuple[float, float, float, float]]] = []
    for e in layout:
        if e.dxftype() in ("XLINE", "RAY", "VIEWPORT", "LIGHT"):
            continue
        b = bbox.extents([e], fast=True, cache=cache)
        if b.has_data:
            boxes.append((e, (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)))
    if not boxes:
        return None

    def union(bs):
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))

    def area(b):
        return max(b[2] - b[0], 1e-9) * max(b[3] - b[1], 1e-9)

    for _ in range(3):
        if len(boxes) < 4:
            break
        u = union([b for _e, b in boxes])
        k = max(range(len(boxes)), key=lambda i: area(boxes[i][1]))
        rest = union([b for i, (_e, b) in enumerate(boxes) if i != k])
        if area(u) > 50 * area(rest):
            e = boxes.pop(k)[0]
            if dropped is not None:
                dropped.append(f"{e.dxftype()} {e.dxf.get('name', '')} #{e.dxf.handle}".replace("  ", " "))
        else:
            break
    return union([b for _e, b in boxes])


def drawing_area(size: str, orientation: str = "landscape") -> tuple[float, float, float, float]:
    """Drawing area of the drawing-sheet template (x, y_from_bottom, w, h) in inches for *size*.

    Mirrors templates/typst/drawing-sheet.typ: 0.5in page margin, title-block column max(2.9in, 4in·s) on the right,
    drawing inset 0.35in·s, s = max(0.55, W/36in). Content placed there at this exact size keeps its scale.
    """
    W, H = paper_size(size, orientation)
    s = max(0.55, W / 36.0)
    tbw = max(2.9, 4.0 * s)  # keep in sync with drawing-sheet.typ
    inset = 0.35 * s
    return (0.5 + inset, 0.5 + inset, W - 1.0 - tbw - 2 * inset, H - 1.0 - 2 * inset)


def _lineweight_override(doc, policy: str, layer_weights: dict[str, float] | None, uniform_mm: float) -> Callable:
    layers: dict[str, float] = {}
    default = 0.25
    try:
        lw_default = int(doc.header.get("$LWDEFAULT", 25))
        default = lw_default / 100.0 if lw_default > 0 else 0.25
    except Exception:
        pass
    for layer in doc.layers:
        lw = int(layer.dxf.get("lineweight", -3))
        layers[layer.dxf.name.lower()] = lw / 100.0 if lw >= 0 else default
    table = {k.lower(): float(v) for k, v in (layer_weights or {}).items()}

    def override(entity, properties) -> None:
        name = (properties.layer or "0").lower()
        for pat, mm in table.items():
            if fnmatch.fnmatchcase(name, pat):
                properties.lineweight = mm
                return
        if policy == "by-layer":
            properties.lineweight = layers.get(name, default)
        elif policy == "uniform":
            properties.lineweight = uniform_mm

    return override


def _render_dxf_pdf(doc, layout, dst: Path, *, page_in: tuple[float, float], area_in: tuple[float, float, float, float],
                    ppu_in: float | None, center: tuple[float, float] | None, lineweights: str, lineweight_scale: float,
                    layer_weights: dict[str, float] | None, color: str, ctb: str | None, title: str,
                    background: str = "#ffffff", extents: str | Sequence[float] = "auto") -> dict[str, Any]:
    """Plot *layout* to a one-page PDF. *ppu_in* = paper inches per drawing unit (None = fit area)."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties

    from ezdxf.addons.drawing.config import (BackgroundPolicy, ColorPolicy, Configuration, HatchPolicy,
                                             LineweightPolicy, TextPolicy)
    from ezdxf.addons.drawing.frontend import UniversalFrontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.addons.drawing.properties import RenderContext

    from pdfcore.fonts import font_path

    W, H = page_in
    ax0, ay0, aw, ah = area_in
    dropped: list[str] = []
    ext = _layout_extents(doc, layout, extents, dropped)
    if ext is None:
        raise ValueError(f"layout {layout.name!r} has no drawable entities")
    ex0, ey0, ex1, ey1 = ext
    ew, eh = max(ex1 - ex0, 1e-9), max(ey1 - ey0, 1e-9)
    pad = 0.06  # inches: keep heavy edge lines inside the plot
    fit = min(max(aw - 2 * pad, 0.1) / ew, max(ah - 2 * pad, 0.1) / eh)
    if ppu_in is None:
        ppu_in = fit
    cx, cy = center if center is not None else ((ex0 + ex1) / 2, (ey0 + ey1) / 2)
    fits = ppu_in <= fit * 1.0001

    rel = 1.0
    if lineweights == "relative":
        rel = math.hypot(W, H) / math.hypot(34.0, 22.0)  # weights authored for ANSI D, scaled to this sheet
    cpol = {"color": ColorPolicy.COLOR, "mono": ColorPolicy.BLACK, "black": ColorPolicy.BLACK,
            "gray": ColorPolicy.MONOCHROME_LIGHT_BG, "grayscale": ColorPolicy.MONOCHROME_LIGHT_BG}[color]
    cfg = Configuration(
        background_policy=BackgroundPolicy.WHITE, color_policy=cpol, lineweight_policy=LineweightPolicy.ABSOLUTE,
        lineweight_scaling=MM * lineweight_scale * rel, min_lineweight=0.2, text_policy=TextPolicy.FILLING,
        hatch_policy=HatchPolicy.NORMAL,
    )
    with matplotlib.rc_context({"pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
                                "path.simplify": False, "lines.solid_capstyle": "round"}):
        fig = Figure(figsize=(W, H), dpi=300, facecolor=background)
        ax = fig.add_axes((0, 0, 1, 1))
        ctx = RenderContext(doc, ctb=ctb or "")
        backend = MatplotlibBackend(ax, adjust_figure=False)
        pipe = _make_text_pipeline()(backend)
        fe = UniversalFrontend(ctx, pipe, cfg)
        if lineweights in ("by-layer", "uniform") or layer_weights:
            fe.push_property_override_function(_lineweight_override(doc, lineweights, layer_weights, 0.25 * lineweight_scale))
        fe.draw_layout(layout, finalize=True)
        ax.set_facecolor(background)
        # axes cover the whole sheet; the drawing centre lands on the centre of the plot area
        left = cx - (ax0 + aw / 2) / ppu_in
        bottom = cy - (ay0 + ah / 2) / ppu_in
        ax.set_xlim(left, left + W / ppu_in)
        ax.set_ylim(bottom, bottom + H / ppu_in)
        ax.set_aspect("auto")
        ax.set_position((0, 0, 1, 1))
        fp = FontProperties(fname=str(font_path("Inter-Regular.ttf")))
        cap = _inter_cap_ratio()
        ppt = ppu_in * INCH  # points per drawing unit
        n_text = 0
        from matplotlib.textpath import TextPath

        n_off = 0
        for t in pipe.texts:
            size = t["cap"] * ppt / cap
            if size < 0.5:
                continue
            # keep text that lies wholly on the sheet (text past the edge would be clipped in the PDF)
            px, py = (t["x"] - left) * ppt, (t["y"] - bottom) * ppt
            try:
                tw = TextPath((0, 0), t["text"], size=size, prop=fp).get_extents().width
            except Exception:
                tw = 0.6 * size * len(t["text"])
            f0 = {"left": 0.0, "center": -0.5, "right": -1.0}[t["ha"]]
            a = math.radians(t["angle"])
            corners = [(px + (f0 * tw + u) * math.cos(a) - v * math.sin(a), py + (f0 * tw + u) * math.sin(a) + v * math.cos(a))
                       for u in (0, tw) for v in (-0.25 * size, size)]
            if any(cx_ < -0.5 or cx_ > W * INCH + 0.5 or cy_ < -0.5 or cy_ > H * INCH + 0.5 for cx_, cy_ in corners):
                n_off += 1
                continue
            col = t["color"]
            if cpol == ColorPolicy.BLACK:
                col = "#000000"
            ax.text(t["x"], t["y"], t["text"], fontproperties=fp, fontsize=size, rotation=t["angle"],
                    rotation_mode="anchor", ha=t["ha"], va="baseline", color=col[:7] if isinstance(col, str) else col,
                    clip_on=True, zorder=1e6)
            n_text += 1
        fig.savefig(str(dst), format="pdf", facecolor=background,
                    metadata={"Title": title, "Author": "PDF Studio", "Subject": "CAD plot", "Creator": "pdfcore plans dxf2pdf"})
    ratio = None
    return {"ppu_in": ppu_in, "fits": fits, "extents": ext, "texts": n_text, "ratio": ratio, "dropped": dropped,
            "texts_off_sheet": n_off}


def dxf_to_pdf(
    src: str | Path,
    *,
    out: str | Path | None = None,
    size: str | None = None,
    orientation: str = "auto",
    scale: str | float | None = None,
    units: str | None = None,
    layout: str | None = None,
    lineweights: str = "absolute",
    lineweight_scale: float = 1.0,
    layer_weights: dict[str, float] | None = None,
    ctb: str | None = None,
    color: str = "color",
    margin_in: float = 0.5,
    title_block: dict[str, Any] | None = None,
    brand: str = "default",
    extents: str | Sequence[float] = "auto",
    quiet: bool = False,
) -> OpResult:
    """Plot a DXF / DWG / HPGL file to a vector PDF.

    *size*: paper (ANSI/ARCH name or WxH); default = the layout's paper for paperspace, else ANSI B.
    *scale*: ``1/4"=1'-0"``, ``1:50`` … (plot to scale, centred); default fits the sheet.
    *units*: drawing units when $INSUNITS is missing/wrong (in, ft, mm, m).
    *layout*: ``Model`` (default) or a paperspace layout name.
    *lineweights*: absolute (DXF mm on paper) | relative (scaled to sheet size, ANSI D = 1:1) |
    by-layer (layer table only, entity overrides ignored) | uniform (all 0.25 mm × scale).
    *layer_weights*: {"E-POWR": 0.7, "E-ANNO*": 0.18} mm per layer (glob) — overrides everything.
    *ctb*: AutoCAD .ctb plot-style table (colour → pen weight). *color*: color | mono | grayscale.
    *extents*: auto (skip XLINE/RAY and lone outliers) | all | (x0, y0, x1, y1) window, drawing units.
    *title_block*: drawing-sheet fields (sheet, title, project, rev, drawn, …) → plot goes into the
    title-block template's drawing area at the same scale.
    """
    src = Path(src)
    if lineweights not in LINEWEIGHT_POLICIES:
        raise ValueError(f"lineweights must be one of {', '.join(LINEWEIGHT_POLICIES)}")
    if color not in ("color", "mono", "black", "gray", "grayscale"):
        raise ValueError("color must be color, mono or grayscale")
    doc, loader = load_cad_ex(src)
    fallback = False
    if layout and layout.lower() not in ("model", "modelspace"):
        names = [n for n in doc.layouts.names() if n.lower() == layout.lower()]
        if not names:
            raise ValueError(f"no layout {layout!r}; layouts: {', '.join(doc.layouts.names())}")
        lay = doc.layouts.get(names[0])
    else:
        lay = doc.modelspace()
        if layout is None and _layout_extents(doc, lay) is None:
            # empty modelspace (content lives in paperspace): first layout with entities
            for name in doc.layouts.names_in_taborder():
                cand = doc.layouts.get(name)
                if not cand.is_modelspace and len(cand):
                    if _layout_extents(doc, cand) is not None:
                        lay, fallback = cand, True
                        break
    is_paper = not lay.is_modelspace
    ext = _layout_extents(doc, lay, extents)
    if ext is None:
        raise ValueError(f"{src.name}: layout {lay.name!r} is empty")
    ew, eh = ext[2] - ext[0], ext[3] - ext[1]
    sc = parse_scale(scale)
    # paper
    if size is None and is_paper:
        pw, ph = float(lay.dxf.get("paper_width", 0) or 0), float(lay.dxf.get("paper_height", 0) or 0)
        if pw > 0 and ph > 0:
            size_in: tuple[float, float] = (pw / 25.4, ph / 25.4)
        else:
            size_in = paper_size("ansi-b")
        size_name = None
    else:
        size_name = size or "ansi-b"
        size_in = paper_size(size_name)
    if orientation == "auto" and title_block is not None:
        orient = "landscape"  # title-block sheets are landscape
    elif orientation == "auto":
        orient = "portrait" if eh > ew * 1.05 and not is_paper else "landscape"
        if is_paper and size is None:
            orient = "landscape" if size_in[0] >= size_in[1] else "portrait"
    else:
        orient = orientation
    W, H = (max(size_in), min(size_in)) if orient.startswith("l") else (min(size_in), max(size_in))
    if title_block is not None:
        tb_size = _norm_size(size_name or "ansi-b")
        if tb_size not in TEMPLATE_SIZES or not orient.startswith("l"):
            raise ValueError(f"title block needs a landscape template size: {', '.join(TEMPLATE_SIZES)}")
        area = drawing_area(tb_size)
    elif is_paper and size is None:
        area = (0.0, 0.0, W, H)
    else:
        area = (margin_in, margin_in, W - 2 * margin_in, H - 2 * margin_in)
    unit_in = 1 / 25.4 if is_paper and int(lay.dxf.get("plot_paper_units", 1)) == 1 else (1.0 if is_paper else _unit_inches(doc, units))
    if is_paper and sc is None and size is None:
        sc = Scale(1.0, "1:1")
    ppu = sc.paper_in_per_unit(unit_in) if sc else None
    dst = output_path(src, "plot", ".pdf", out=out)
    title = (title_block or {}).get("title") or f"{src.stem} — {lay.name}"
    extra: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="pdfcore-dxf-") as tmp:
        if title_block is None:
            info = _render_dxf_pdf(doc, lay, dst, page_in=(W, H), area_in=area, ppu_in=ppu, center=None,
                                   lineweights=lineweights, lineweight_scale=lineweight_scale, layer_weights=layer_weights,
                                   color=color, ctb=ctb, title=title, extents=extents)
        else:
            from pdfcore.ops.typeset import make

            ax, ay, aw, ah = area
            img = Path(tmp) / "drawing.pdf"
            info = _render_dxf_pdf(doc, lay, img, page_in=(aw, ah), area_in=(0, 0, aw, ah), ppu_in=ppu, center=None,
                                   lineweights=lineweights, lineweight_scale=lineweight_scale, layer_weights=layer_weights,
                                   color=color, ctb=ctb, title=title, extents=extents)
            ratio_now = unit_in / info["ppu_in"]
            data = {"sheet": "S-1", "title": title, "project": src.stem, "size": _norm_size(size_name or "ansi-b"),
                    "scale": sc.text.upper() if sc else f"{_scale_text(ratio_now)} (FIT)", **title_block, "image": "drawing.pdf"}
            import yaml

            yml = Path(tmp) / "sheet.yaml"
            yml.write_text(yaml.safe_dump(data, sort_keys=False))
            res = make("drawing-sheet", yml, brand=brand, out=dst, quiet=True)
            extra["title_block_gate"] = res.line()
    ratio = unit_in / info["ppu_in"]
    if info["dropped"] and not quiet:
        print(f"note: left out of the plot extents (far outliers): {', '.join(info['dropped'])} — use --extents all to include")
    if not info["fits"] and not quiet:
        print(f"warning: drawing at {_scale_text(ratio)} does not fit the sheet — edges are clipped (use a larger --size or smaller scale)")
    data = {"layout": lay.name, "paper_in": (W, H), "scale_ratio": ratio, "scale": sc.text if sc else _scale_text(ratio) + " (fit)",
            "fits": info["fits"], "texts": info["texts"], "lineweights": lineweights, "loader": loader,
            "layout_fallback": fallback, "dropped_outliers": info["dropped"], "texts_off_sheet": info["texts_off_sheet"], **extra}
    from pdfcore.gate import GateOptions

    return finalize(dst, None, options=GateOptions(profile="drawing"), data=data, quiet=quiet)


# ============================================================== PDF → DXF
_STD_LW = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]
_COLOR_NAMES = {(0, 0, 0): "BLACK", (255, 0, 0): "RED", (0, 0, 255): "BLUE", (0, 128, 0): "GREEN", (0, 255, 0): "GREEN",
                (255, 255, 0): "YELLOW", (0, 255, 255): "CYAN", (255, 0, 255): "MAGENTA", (128, 128, 128): "GRAY",
                (255, 255, 255): "WHITE"}
_KAPPA = 0.5522847498


def _rgb255(c: Any) -> tuple[int, int, int] | None:
    from pdfcore.ops.extract import _rgb

    v = _rgb(c)
    if v is None:
        return None
    return tuple(max(0, min(255, round(x * 255))) for x in v)  # type: ignore[return-value]


def _std_lineweight(width_pt: float) -> int:
    mm100 = width_pt / MM * 100
    return min(_STD_LW, key=lambda v: abs(v - mm100))


def _color_label(rgb: tuple[int, int, int]) -> str:
    return _COLOR_NAMES.get(rgb, "C%02X%02X%02X" % rgb)


def _subpaths(cmds: list[list]) -> list[tuple[list[list], bool]]:
    """Split M/L/C/Z commands into subpaths: [(cmds starting with M, closed)]."""
    out: list[tuple[list[list], bool]] = []
    cur: list[list] = []
    closed = False
    for c in cmds:
        if c[0] == "M":
            if len(cur) > 1:
                out.append((cur, closed))
            cur, closed = [c], False
        elif c[0] == "Z":
            closed = True
        else:
            if not cur:
                cur = [["M", c[-2], c[-1]]]
            cur.append(c)
    if len(cur) > 1:
        out.append((cur, closed))
    return out


def _bezier_pts(p0, c1, c2, p3, n: int) -> list[tuple[float, float]]:
    pts = []
    for k in range(1, n + 1):
        t = k / n
        mt = 1 - t
        pts.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p3[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p3[1]))
    return pts


def _as_ellipse(sp: list[list]) -> tuple[float, float, float, float] | None:
    """(cx, cy, rx, ry) if the subpath is 4 cubic Béziers approximating an axis-aligned circle/ellipse."""
    curves = [c for c in sp[1:] if c[0] == "C"]
    if len(curves) != 4 or any(c[0] not in ("C",) for c in sp[1:]):
        return None
    pts = [(sp[0][1], sp[0][2])] + [(c[5], c[6]) for c in curves]
    if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > 0.5:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    rx, ry = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
    if rx < 0.2 or ry < 0.2:
        return None
    # every on-curve point and curve midpoint must lie on the ellipse
    prev = pts[0]
    for c in curves:
        mid = _bezier_pts(prev, (c[1], c[2]), (c[3], c[4]), (c[5], c[6]), 2)[0]
        for x, y in (mid, (c[5], c[6])):
            if abs(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 - 1) > 0.03:
                return None
        prev = (c[5], c[6])
    return cx, cy, rx, ry


@dataclass
class _Run:
    chars: list[dict]
    angle: float
    size: float


def _text_runs(chars: list[dict]) -> list[_Run]:
    """Group pdfplumber chars into text runs (same baseline direction, size, contiguous) in content order."""
    runs: list[_Run] = []
    cur: _Run | None = None
    for ch in chars:
        a, b, c, d, e, f = (float(v) for v in ch.get("matrix") or (1, 0, 0, 1, ch["x0"], ch["y0"]))
        ang = math.degrees(math.atan2(b, a))
        size = float(ch.get("size") or 0) or abs(ch["bottom"] - ch["top"])
        if cur is not None and cur.chars:
            pa, pb, pc, pd, pe, pf = (float(v) for v in cur.chars[-1].get("matrix"))
            ux, uy = math.cos(math.radians(cur.angle)), math.sin(math.radians(cur.angle))
            dx, dy = e - pe, f - pf
            along = dx * ux + dy * uy
            perp = -dx * uy + dy * ux
            adv = float(cur.chars[-1].get("adv") or (cur.chars[-1]["x1"] - cur.chars[-1]["x0"]))
            same = (abs(((ang - cur.angle + 180) % 360) - 180) < 1 and abs(size - cur.size) < 0.06 * max(size, 1)
                    and abs(perp) < 0.3 * size and -0.2 * size < along < adv + 0.9 * size)
            if same:
                cur.chars.append(ch)
                continue
        if cur is not None:
            runs.append(cur)
        cur = _Run([ch], ang, size)
    if cur is not None:
        runs.append(cur)
    return runs


def pdf_to_dxf(
    src: str | Path,
    *,
    sheet: str | None = None,
    page: str | int | None = None,
    scale: str | float | None = None,
    units: str | None = None,
    layers: str = "color+weight",
    curves: str = "polyline",
    text: bool = True,
    region: Sequence[float] | None = None,
    out: str | Path | None = None,
    password: str | None = None,
    dxf_version: str = "R2010",
    quiet: bool = False,
) -> OpResult:
    """Convert PDF page vectors and text to DXF for ProgeCAD/AutoCAD — one DXF per page.

    Geometry: LINE, closed LWPOLYLINE (rectangles), CIRCLE/ELLIPSE (4-Bézier circles), LWPOLYLINE for
    other paths (curves flattened) or SPLINE (*curves* = "spline"), solid HATCH for filled shapes, TEXT
    with height and rotation. *layers*: color+weight (PDF-BLACK-0.50) | color | weight | single.
    Units: paper inches (1 PDF inch = 1 unit). With *scale* (1/4"=1'-0", 1"=20', 1:50) geometry is in
    real-world units (feet for architectural/engineering scales, else *units*). *region*: x0,top,x1,bottom
    in points (top-left origin) to export only part of the sheet.
    """
    import ezdxf
    import pdfplumber
    from ezdxf.enums import TextEntityAlignment

    from pdfcore.ops.extract import _path_cmds, _verify

    src = Path(src)
    sc = parse_scale(scale)
    out_units = (units or (sc.units if sc else "in")).lower()
    if out_units not in _UNIT_NAMES:
        raise ValueError(f"units must be one of {', '.join(sorted(_UNIT_NAMES))}")
    unit_in = _INSUNITS_IN[_UNIT_NAMES[out_units]]
    k = (sc.ratio if sc else 1.0) / unit_in / INCH  # drawing units per PDF point
    if curves not in ("polyline", "spline"):
        raise ValueError("curves must be polyline or spline")
    reg = Rect(*region) if region else None
    files: list[Path] = []
    counts_all: list[dict[str, int]] = []
    with pdfplumber.open(str(src), password=password or "") as pdf:
        idx = _pages_for(src, sheet=sheet, page=page, n=len(pdf.pages), password=password)
        for pi in idx:
            pg = pdf.pages[pi]
            H = float(pg.height)
            doc = ezdxf.new(dxf_version, setup=True)
            doc.units = _UNIT_NAMES[out_units]
            doc.header["$INSUNITS"] = _UNIT_NAMES[out_units]
            doc.header["$MEASUREMENT"] = 1 if out_units in ("mm", "cm", "m") else 0
            doc.header["$LUNITS"] = 4 if out_units == "ft" else 2
            doc.header["$LWDISPLAY"] = 1
            msp = doc.modelspace()
            counts = {"LINE": 0, "LWPOLYLINE": 0, "CIRCLE": 0, "ELLIPSE": 0, "SPLINE": 0, "HATCH": 0, "TEXT": 0}

            def P(x: float, top: float) -> tuple[float, float]:
                return ((x - (reg.x0 if reg else 0)) * k, ((reg.bottom if reg else H) - top) * k)

            def layer_for(stroke: Any, width: float, fill: bool = False) -> str:
                rgb = _rgb255(stroke) or (0, 0, 0)
                lw = _std_lineweight(width)
                if layers == "single":
                    name = "PDF-FILL" if fill else "PDF-GEOMETRY"
                elif layers == "color":
                    name = f"PDF-{_color_label(rgb)}"
                elif layers == "weight":
                    name = "PDF-FILL" if fill else f"PDF-{lw / 100:.2f}MM"
                else:
                    name = f"PDF-{_color_label(rgb)}" + ("-FILL" if fill else f"-{lw / 100:.2f}")
                if name not in doc.layers:
                    lay = doc.layers.add(name, color=7)
                    if rgb not in ((0, 0, 0), (255, 255, 255)):
                        lay.rgb = rgb
                    lay.dxf.lineweight = lw if not fill else 0
                return name

            def inside(bb: tuple[float, float, float, float]) -> bool:
                if reg is None:
                    return True
                return reg.intersects(Rect(*bb)) or reg.contains_point(bb[0], bb[1])

            objs = [("line", o) for o in pg.lines] + [("rect", o) for o in pg.rects] + [("curve", o) for o in pg.curves]
            for kind, o in objs:
                if not inside((o["x0"], o["top"], o["x1"], o["bottom"])):
                    continue
                stroke = bool(o.get("stroke", True))
                filled = bool(o.get("fill", False))
                if not stroke and not filled:
                    continue
                width = float(o.get("linewidth") or 0)
                lname = layer_for(o.get("stroking_color", 0), width) if stroke else None
                fname = layer_for(o.get("non_stroking_color", 0), 0, fill=True) if filled else None
                cmds = _path_cmds(o)
                for sp, closed in _subpaths(cmds):
                    pts_tl = [(c[-2], c[-1]) for c in sp]
                    poly: list[tuple[float, float]] = [P(*pts_tl[0])]
                    ell = _as_ellipse(sp) if kind == "curve" else None
                    straight = all(c[0] in ("M", "L") for c in sp)
                    if ell is not None:
                        cx, cy, rx, ry = ell
                        c0 = P(cx, cy)
                        if fname:
                            h = msp.add_hatch(dxfattribs={"layer": fname})
                            h.set_solid_fill(color=256)
                            ep = h.paths.add_edge_path()
                            ep.add_ellipse(c0, major_axis=(rx * k, 0), ratio=ry / rx) if abs(rx - ry) > 0.02 * rx else ep.add_arc(c0, radius=rx * k, start_angle=0, end_angle=360)
                            counts["HATCH"] += 1
                        if lname:
                            if abs(rx - ry) <= 0.02 * max(rx, ry):
                                msp.add_circle(c0, (rx + ry) / 2 * k, dxfattribs={"layer": lname})
                                counts["CIRCLE"] += 1
                            else:
                                major = (rx * k, 0, 0) if rx >= ry else (0, ry * k, 0)
                                msp.add_ellipse(c0, major_axis=major, ratio=min(rx, ry) / max(rx, ry), dxfattribs={"layer": lname})
                                counts["ELLIPSE"] += 1
                        continue
                    if not straight and curves == "spline" and lname:
                        from ezdxf.math import Bezier4P, bezier_to_bspline

                        prev = pts_tl[0]
                        segs = []
                        for c in sp[1:]:
                            if c[0] == "C":
                                segs.append(Bezier4P([P(*prev), P(c[1], c[2]), P(c[3], c[4]), P(c[5], c[6])]))
                            else:
                                a, b = P(*prev), P(c[1], c[2])
                                segs.append(Bezier4P([a, (a[0] + (b[0] - a[0]) / 3, a[1] + (b[1] - a[1]) / 3),
                                                      (a[0] + 2 * (b[0] - a[0]) / 3, a[1] + 2 * (b[1] - a[1]) / 3), b]))
                            prev = (c[-2], c[-1])
                        try:
                            bs = bezier_to_bspline(segs)
                            msp.add_spline(dxfattribs={"layer": lname}).apply_construction_tool(bs)
                            counts["SPLINE"] += 1
                            continue
                        except Exception:
                            pass
                    prev = pts_tl[0]
                    for c in sp[1:]:
                        if c[0] == "C":
                            seglen = math.hypot(c[5] - prev[0], c[6] - prev[1])
                            n = max(4, min(32, int(seglen / 3)))
                            poly.extend(P(*q) for q in _bezier_pts(prev, (c[1], c[2]), (c[3], c[4]), (c[5], c[6]), n))
                        else:
                            poly.append(P(c[1], c[2]))
                        prev = (c[-2], c[-1])
                    if closed and len(poly) > 2 and math.hypot(poly[0][0] - poly[-1][0], poly[0][1] - poly[-1][1]) < 1e-9:
                        poly.pop()
                    if fname and len(poly) >= 3:
                        h = msp.add_hatch(dxfattribs={"layer": fname})
                        h.set_solid_fill(color=256)
                        h.paths.add_polyline_path(poly, is_closed=True)
                        counts["HATCH"] += 1
                    if not lname:
                        continue
                    if len(poly) == 2 and not closed:
                        msp.add_line(poly[0], poly[1], dxfattribs={"layer": lname})
                        counts["LINE"] += 1
                    elif len(poly) >= 2:
                        msp.add_lwpolyline(poly, close=closed or kind == "rect", dxfattribs={"layer": lname})
                        counts["LWPOLYLINE"] += 1
            if text:
                tl = "PDF-TEXT"
                doc.layers.add(tl, color=7)
                doc.styles.add("PDF-INTER", font="Inter-Regular.ttf") if "PDF-INTER" not in doc.styles else None
                chars = [c for c in pg.chars if c.get("text") is not None]
                for run in _text_runs(chars):
                    s = "".join(c["text"] for c in run.chars).strip()
                    if not s:
                        continue
                    first = next(c for c in run.chars if c["text"].strip())
                    if not inside((first["x0"], first["top"], first["x1"], first["bottom"])):
                        continue
                    a, b, c_, d, e, f = (float(v) for v in first["matrix"])
                    ins = P(e, H - f)
                    t = msp.add_text(s, height=run.size * 0.727 * k, rotation=run.angle,
                                     dxfattribs={"layer": tl, "style": "PDF-INTER"})
                    t.set_placement(ins, align=TextEntityAlignment.LEFT)
                    counts["TEXT"] += 1
            tag = (find_sheet_label(sheet, pi, idx) if sheet else f"p{pi + 1}")
            dst = output_path(src, tag, ".dxf", out=out if (out and len(idx) == 1) else (out if out and Path(out).is_dir() else None))
            doc.saveas(str(dst))
            check = ezdxf.readfile(str(dst))
            aud = check.audit()
            if aud.has_errors:
                raise RuntimeError(f"{dst.name}: DXF audit found {len(aud.errors)} errors")
            files.append(dst)
            counts_all.append(counts)
    _verify(files)
    if not quiet:
        for f_, c in zip(files, counts_all):
            print(f"{f_}  PASS  ({', '.join(f'{v} {k_}' for k_, v in c.items() if v)})")
    return OpResult(files[0] if files else None, None, files,
                    {"pages": [i + 1 for i in idx], "counts": counts_all, "units": out_units,
                     "scale": sc.text if sc else "1:1 (paper inches)", "units_per_pt": k})


def find_sheet_label(sheet: str, pi: int, idx: list[int]) -> str:
    names = [s for s in re.split(r"[,\s]+", sheet) if s]
    pos = idx.index(pi)
    return re.sub(r"[^A-Za-z0-9._-]", "_", names[pos] if pos < len(names) else f"p{pi + 1}")


# ============================================================== ladder schematics
OUTPUT_KINDS = {"coil", "timer", "light", "solenoid", "horn"}
COIL_KINDS = {"coil", "timer"}
CONTACT_KINDS = {"no", "nc"}
_KIND_ALIASES = {
    "relay": "coil", "starter": "coil", "contactor": "coil", "motor_starter": "coil", "cr": "coil",
    "timer_coil": "timer", "tr": "timer", "pilot_light": "light", "lamp": "light", "pl": "light", "indicator": "light",
    "overload": "ol", "ol_nc": "ol", "oln": "ol", "contact_no": "no", "contact_nc": "nc", "aux_no": "no", "aux_nc": "nc",
    "pushbutton_no": "pb_no", "pushbutton_nc": "pb_nc", "start": "pb_no", "stop": "pb_nc", "e_stop": "estop",
    "emergency_stop": "estop", "selector": "ss", "hoa": "ss", "selector_switch": "ss", "fu": "fuse",
    "breaker": "cb", "circuit_breaker": "cb", "term": "terminal", "tb": "terminal", "sol": "solenoid",
    "float": "switch_no", "float_no": "switch_no", "float_nc": "switch_nc", "ls_no": "switch_no", "ls_nc": "switch_nc",
    "ps_no": "switch_no", "ps_nc": "switch_nc",
}
_ACTUATOR_FROM_ALIAS = {"float": "float", "float_no": "float", "float_nc": "float", "ls_no": "limit", "ls_nc": "limit",
                        "ps_no": "pressure", "ps_nc": "pressure"}
_KNOWN_KINDS = {"no", "nc", "ol", "ol_no", "pb_no", "pb_nc", "estop", "ss", "switch_no", "switch_nc", "fuse", "cb",
                "terminal"} | OUTPUT_KINDS
_DEFAULT_TERMS = {"coil": ("A1", "A2"), "timer": ("A1", "A2"), "light": ("X1", "X2"), "solenoid": ("1", "2"),
                  "horn": ("1", "2"), "ol": ("95", "96"), "ol_no": ("97", "98"), "no": ("13", "14"), "nc": ("21", "22"),
                  "pb_no": ("3", "4"), "pb_nc": ("1", "2"), "estop": ("1", "2"), "ss": ("3", "4"),
                  "switch_no": ("C", "NO"), "switch_nc": ("C", "NC"), "fuse": ("1", "2"), "cb": ("1", "2")}
_LIGHT_COLORS = {"G": "GREEN", "R": "RED", "A": "AMBER", "Y": "YELLOW", "W": "WHITE", "B": "BLUE", "C": "CLEAR"}


@dataclass
class Dev:
    kind: str
    tag: str
    label: str = ""
    desc: str = ""
    rating: str = ""
    color: str = ""
    mfr: str = ""
    part: str = ""
    wire: str | None = None
    terminals: tuple[str, str] | None = None
    position: str | None = None
    positions: list[str] = field(default_factory=list)
    timed: str | None = None
    actuator: str = ""
    field_device: bool = False
    strip: str = ""
    number: str = ""
    # filled by layout
    line: int = 0
    cx: float = 0.0
    row: int = 0
    nodes: tuple[int, int] = (0, 0)

    @property
    def is_output(self) -> bool:
        return self.kind in OUTPUT_KINDS

    def term_names(self) -> tuple[str, str]:
        if self.terminals:
            return self.terminals
        if self.kind == "terminal":
            return (self.number, self.number)
        return _DEFAULT_TERMS.get(self.kind, ("1", "2"))


@dataclass
class Par:
    branches: list[list[Any]]


@dataclass
class Rung:
    elems: list[Any]
    desc: str = ""
    section: str = ""
    rows: int = 1
    first_line: int = 0


def _ystr(v: Any) -> str:
    """YAML 1.1 turns OFF/ON/NO/YES into booleans — map them back to the word."""
    if v is True:
        return "ON"
    if v is False:
        return "OFF"
    return str(v).upper()


def _parse_dev(d: dict[str, Any], defaults: dict[str, dict]) -> Dev:
    t = d.get("type", d.get("kind"))
    if t is False:  # YAML 1.1 reads a bare `no` as false
        t = "no"
    if isinstance(d.get("contact"), bool):
        d = {**d, "contact": "no"}
    if isinstance(d.get("timed"), bool):
        d = {**d, "timed": "on" if d["timed"] else "off"}
    raw = str(t or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        raise ValueError(f"device {d!r} has no type")
    kind = _KIND_ALIASES.get(raw, raw)
    if raw == "pushbutton":
        kind = "pb_nc" if str(d.get("contact", "no")).lower() == "nc" else "pb_no"
    if raw in ("switch", "float_switch", "limit_switch", "pressure_switch", "level_switch", "flow_switch", "temp_switch"):
        kind = "switch_nc" if str(d.get("contact", "no")).lower() == "nc" else "switch_no"
    if kind not in _KNOWN_KINDS:
        raise ValueError(f"unknown device type {raw!r}; known: {', '.join(sorted(_KNOWN_KINDS | set(_KIND_ALIASES)))}")
    tag = str(d.get("tag", "")).strip()
    actuator = str(d.get("actuator") or _ACTUATOR_FROM_ALIAS.get(raw) or
                   (raw.split("_")[0] if raw.endswith("_switch") else "") or ("float" if kind.startswith("switch") else "")).lower()
    strip, number = str(d.get("strip", "")), str(d.get("number", d.get("terminal", "")) or "")
    if kind == "terminal" and not strip and tag:
        m = re.fullmatch(r"(.+?)[-:](\w+)", tag)
        if m:
            strip, number = m.group(1), m.group(2)
    if kind == "terminal" and not strip:
        strip = "TB-1"
    base = dict(defaults.get(tag, {})) if tag else {}
    terms = d.get("terminals", base.get("terminals"))
    dev = Dev(
        kind=kind, tag=tag, label=str(d.get("label", "") or ""), desc=str(d.get("desc", d.get("description", base.get("desc", ""))) or ""),
        rating=str(d.get("rating", d.get("setting", base.get("rating", ""))) or ""),
        color=str(d.get("color", base.get("color", "")) or "").upper()[:1], mfr=str(d.get("mfr", d.get("manufacturer", base.get("mfr", base.get("manufacturer", "")))) or ""),
        part=str(d.get("part", base.get("part", "")) or ""), wire=str(d["wire"]) if d.get("wire") is not None else None,
        terminals=(str(terms[0]), str(terms[1])) if terms else None,
        position=_ystr(d.get("position")) if d.get("position") is not None else None,
        positions=[_ystr(p) for p in (d.get("positions") or base.get("positions") or [])],
        timed=str(d.get("timed")).lower() if d.get("timed") else None, actuator=actuator,
        field_device=bool(d.get("field", kind.startswith("switch"))), strip=strip, number=number,
    )
    if kind == "ss" and not dev.positions:
        dev.positions = ["HAND", "OFF", "AUTO"]
    if kind == "ss" and dev.position and dev.position not in dev.positions:
        raise ValueError(f"{tag}: position {dev.position} not in {dev.positions}")
    if kind not in ("terminal",) and not tag:
        raise ValueError(f"device {d!r} needs a tag")
    return dev


def _parse_elems(items: Sequence[Any], defaults: dict[str, dict]) -> list[Any]:
    out: list[Any] = []
    for it in items or []:
        if isinstance(it, dict) and ("parallel" in it or "branch" in it):
            br = it.get("parallel") or it.get("branch")
            out.append(Par([_parse_elems(b if isinstance(b, list) else [b], defaults) for b in br]))
        elif isinstance(it, dict):
            out.append(_parse_dev(it, defaults))
        elif isinstance(it, list):
            out.extend(_parse_elems(it, defaults))
        else:
            raise ValueError(f"bad rung element {it!r}")
    return out


def load_ladder(data: dict[str, Any] | str | Path) -> dict[str, Any]:
    """Parse ladder YAML (or a dict) into {'meta', 'rungs', 'devices'}; raises ValueError on mistakes."""
    import yaml

    if not isinstance(data, dict):
        data = yaml.safe_load(Path(data).read_text()) or {}
    defaults = {str(k): (v or {}) for k, v in (data.get("devices") or {}).items()}
    rungs: list[Rung] = []
    for r in data.get("rungs") or []:
        if isinstance(r, dict) and r.get("section") and not (r.get("devices") or r.get("series")):
            rungs.append(Rung([], section=str(r["section"])))
            continue
        items = r.get("devices") or r.get("series") or [] if isinstance(r, dict) else r
        rungs.append(Rung(_parse_elems(items, defaults), desc=str((r.get("desc") or r.get("description") or "") if isinstance(r, dict) else "")))
    if not rungs:
        raise ValueError("ladder has no rungs")
    return {"meta": data, "rungs": rungs, "devices": defaults}


def _walk_devs(elems: Sequence[Any]) -> Iterable[Dev]:
    for e in elems:
        if isinstance(e, Dev):
            yield e
        else:
            for b in e.branches:
                yield from _walk_devs(b)


def _has_output(e: Any) -> bool:
    return any(d.is_output for d in _walk_devs([e]))


def _width(e: Any) -> float:
    if isinstance(e, Dev):
        return 0.5 if e.kind == "terminal" else 1.0
    if isinstance(e, Par):
        return max([sum(_width(x) for x in b) for b in e.branches] or [1.0])
    return sum(_width(x) for x in e)


def _rows(e: Any) -> int:
    if isinstance(e, Dev):
        return 1
    if isinstance(e, Par):
        return sum(max([_rows(x) for x in b] or [1]) for b in e.branches)
    return max([_rows(x) for x in e] or [1])


class _Nodes:
    """Union-find over electrical nodes, with forced wire labels."""

    def __init__(self) -> None:
        self.parent: list[int] = []
        self.label: dict[int, str] = {}
        self.by_label: dict[str, int] = {}

    def new(self, label: str | None = None) -> int:
        n = len(self.parent)
        self.parent.append(n)
        if label is not None:
            self.force(n, label)
        return n

    def find(self, n: int) -> int:
        while self.parent[n] != n:
            self.parent[n] = self.parent[self.parent[n]]
            n = self.parent[n]
        return n

    def union(self, a: int, b: int) -> int:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        la, lb = self.label.get(ra), self.label.get(rb)
        if la and lb and la != lb:
            raise ValueError(f"wires {la} and {lb} are the same electrical node")
        self.parent[rb] = ra
        if lb and not la:
            self.label[ra] = lb
            self.by_label[lb] = ra
        return ra

    def force(self, n: int, label: str) -> int:
        r = self.find(n)
        if label in self.by_label:
            return self.union(self.by_label[label], r)
        old = self.label.get(r)
        if old and old != label:
            raise ValueError(f"node already numbered {old}, cannot also be {label}")
        self.label[r] = label
        self.by_label[label] = r
        return r


@dataclass
class _Geo:
    """Ladder column geometry (points, y-up within the drawing canvas)."""

    x_left: float
    x_right: float
    y_top: float  # y of row 0 centre
    row_h: float
    slot: float
    row0: int = 0

    def y(self, row: int) -> float:
        return self.y_top - (row - self.row0) * self.row_h


@dataclass
class _Placed:
    devs: list[Dev] = field(default_factory=list)
    wires: list[tuple[float, float, int, int]] = field(default_factory=list)  # x0, x1, row, node
    verts: list[tuple[float, int, int, int]] = field(default_factory=list)  # x, row0, row1, node
    dots: list[tuple[float, int]] = field(default_factory=list)


def _hw(d: Dev) -> float:
    return {"terminal": 4.0, "coil": 11.0, "timer": 11.0, "light": 10.0, "solenoid": 10.0, "horn": 10.0}.get(d.kind, 9.0)


def _place(e: Any, x0: float, align: str, row: int, nin: int, nout: int | None, nodes: _Nodes, g: _Geo, P: _Placed,
           line_of: Callable[[int], int]) -> tuple[int, float, float]:
    """Place element *e* starting at x0 (its natural width), top row *row*. Returns (out_node, left_conn_x, right_conn_x)."""
    if isinstance(e, Dev):
        w = _width(e) * g.slot
        e.cx = x0 + w / 2
        e.row = row
        e.line = line_of(row)
        if e.kind == "terminal":
            out = nin if nout is None else nodes.union(nin, nout)
        else:
            out = nout if nout is not None else nodes.new()
            if e.wire:
                out = nodes.force(out, e.wire)
        e.nodes = (nin, out)
        P.devs.append(e)
        hw = _hw(e)
        return out, e.cx - hw, e.cx + hw
    if isinstance(e, Par):
        w = _width(e) * g.slot
        a, b = x0, x0 + w
        out = nout if nout is not None else nodes.new()
        r = row
        rows_at: list[int] = []
        for br in e.branches:
            rows_at.append(r)
            bw = sum(_width(x) for x in br) * g.slot
            if not br:
                out = nodes.union(nin, out)
                P.wires.append((a, b, r, nin))
            else:
                start = a if align == "left" else b - bw
                cur = nin
                prev_r = a
                for i, x in enumerate(br):
                    last = i == len(br) - 1
                    o, lc, rc = _place(x, start, align, r, cur, out if last else None, nodes, g, P, line_of)
                    P.wires.append((prev_r, lc, r, cur))
                    prev_r = rc
                    cur = o
                    start += _width(x) * g.slot
                P.wires.append((prev_r, b, r, out))
            r += max([_rows(x) for x in br] or [1])
        if len(rows_at) > 1:
            P.verts.append((a, rows_at[0], rows_at[-1], nin))
            P.verts.append((b, rows_at[0], rows_at[-1], out))
            for rr in rows_at[:-1]:
                if abs(a - g.x_left) > 0.5:
                    P.dots.append((a, rr))
                if abs(b - g.x_right) > 0.5:
                    P.dots.append((b, rr))
        return out, a, b
    raise TypeError(e)


def _layout_rung(rung: Rung, row: int, nodes: _Nodes, rail_l: int, rail_r: int, g: _Geo, P: _Placed,
                 line_of: Callable[[int], int]) -> None:
    els = rung.elems
    split = next((i for i, e in enumerate(els) if _has_output(e)), len(els))
    left, right = els[:split], els[split:]
    wl = sum(_width(x) for x in left) * g.slot
    wr = sum(_width(x) for x in right) * g.slot
    avail = g.x_right - g.x_left - 12
    if wl + wr > avail + 0.5:
        raise ValueError(f"rung at line {line_of(row)} is too wide ({(wl + wr) / g.slot:.1f} devices; "
                         f"room for {avail / g.slot:.1f}) — split it or use a larger --size")
    cur = rail_l
    xc = g.x_left + (0 if els and isinstance(els[0], Par) else 6)
    prev_r = g.x_left
    seq: list[tuple[Any, float]] = [(x, 0.0) for x in left] + [(x, 1.0) for x in right]
    xr_start = g.x_right - (0 if right and isinstance(right[-1], Par) else 6) - wr
    for i, (x, side) in enumerate(seq):
        if side == 1.0 and xc < xr_start:
            xc = xr_start
        last = i == len(seq) - 1
        o, lc, rc = _place(x, xc, "left" if side == 0.0 else "right", row, cur, rail_r if last else None, nodes, g, P, line_of)
        P.wires.append((prev_r, lc, row, cur))
        prev_r, cur = rc, o
        xc += _width(x) * g.slot
    if not seq:
        nodes.union(rail_l, rail_r)
    P.wires.append((prev_r, g.x_right, row, cur if seq else rail_l))


def _assign_wires(nodes: _Nodes, order: list[tuple[int, int]], scheme: str, start: int) -> dict[int, str]:
    """Number unlabeled nodes in order of first appearance [(node, line)]. Returns root → wire number."""
    used = set(nodes.by_label)
    out: dict[int, str] = {r: lab for r, lab in nodes.label.items()}
    nxt = start
    per_line: dict[int, int] = {}
    for n, line in order:
        r = nodes.find(n)
        if r in out:
            continue
        if scheme == "line":
            k = per_line.get(line, 0)
            while True:
                lab = str(line) + ("" if k == 0 else chr(ord("A") + k - 1))
                k += 1
                if lab not in used:
                    break
            per_line[line] = k
        else:
            while str(nxt) in used:
                nxt += 1
            lab = str(nxt)
            nxt += 1
        used.add(lab)
        out[r] = lab
    return out


# ---------------------------------------------------------------- ladder drawing (ReportLab)
LW_RAIL, LW_WIRE, LW_SYMBOL, LW_RULE, LW_THIN = 1.7, 0.8, 0.9, 0.6, 0.4
TXT_MIN = 7.0  # 3/32 in at print size


class _Pen:
    """Small drawing helper over a ReportLab canvas (house fonts, min text size)."""

    def __init__(self, c, iec: bool = False):
        from pdfcore.fonts import register_reportlab

        register_reportlab()
        self.c = c
        self.iec = iec

    def line(self, x0: float, y0: float, x1: float, y1: float, w: float = LW_SYMBOL) -> None:
        self.c.setLineWidth(w)
        self.c.line(x0, y0, x1, y1)

    def circle(self, x: float, y: float, r: float, w: float = LW_SYMBOL, fill: int = 0) -> None:
        self.c.setLineWidth(w)
        self.c.circle(x, y, r, stroke=1, fill=fill)

    def text(self, x: float, y: float, s: str, size: float = TXT_MIN, font: str = "Inter", align: str = "center",
             color: tuple[float, float, float] = (0, 0, 0)) -> float:
        size = max(size, TXT_MIN)
        self.c.setFillColorRGB(*color)
        self.c.setFont(font, size)
        if align == "center":
            self.c.drawCentredString(x, y, s)
        elif align == "right":
            self.c.drawRightString(x, y, s)
        else:
            self.c.drawString(x, y, s)
        self.c.setFillColorRGB(0, 0, 0)
        from reportlab.pdfbase.pdfmetrics import stringWidth

        return stringWidth(s, font, size)

    @staticmethod
    def width(s: str, size: float = TXT_MIN, font: str = "Inter") -> float:
        from reportlab.pdfbase.pdfmetrics import stringWidth

        return stringWidth(s, font, max(size, TXT_MIN))


def _draw_symbol(pen: _Pen, d: Dev, y: float) -> tuple[float, float]:
    """Draw device symbol centred at (d.cx, y) between its terminals. Returns (top, bottom) extents (relative)."""
    c, x = pen.c, d.cx
    hw = _hw(d)
    k = d.kind
    W = LW_SYMBOL
    if k in ("no", "nc", "ol", "ol_no"):
        nc = k in ("nc", "ol")
        if pen.iec:
            pen.line(x - hw, y, x - 6, y, LW_WIRE)
            pen.line(x + 6, y, x + hw, y, LW_WIRE)
            if nc:
                pen.line(x - 6, y, x + 7, y - 4.5, W)
                pen.line(x + 6, y, x + 6, y - 5.5, W)
            else:
                pen.line(x - 6, y, x + 6, y + 6, W)
        else:
            pen.line(x - hw, y, x - 4, y, LW_WIRE)
            pen.line(x + 4, y, x + hw, y, LW_WIRE)
            pen.line(x - 4, y - 7, x - 4, y + 7, W)
            pen.line(x + 4, y - 7, x + 4, y + 7, W)
            if nc:
                pen.line(x - 8, y - 7, x + 8, y + 7, W)
        if k in ("ol", "ol_no"):  # thermal element mark
            c.setLineWidth(LW_THIN)
            p = c.beginPath()
            p.moveTo(x - 4, y + 10)
            p.curveTo(x - 2, y + 13, x - 1, y + 7, x, y + 10)
            p.curveTo(x + 1, y + 13, x + 2, y + 7, x + 4, y + 10)
            c.drawPath(p, stroke=1, fill=0)
        top = 13.0 if k in ("ol", "ol_no") else 7.0
        if d.timed:
            # timed contact: arc ("parachute") above; opens up for on-delay (timed closing/opening after energize)
            c.setLineWidth(W)
            pen.line(x, y + 7.5, x, y + 10, W)
            if d.timed in ("on", "ton", "on-delay", "tc", "notc"):
                c.arc(x - 5, y + 8, x + 5, y + 18, startAng=180, extent=180)
            else:
                c.arc(x - 5, y + 10, x + 5, y + 20, startAng=0, extent=180)
            top = 18.0
        return top, -7.0
    if k in ("pb_no", "pb_nc", "estop"):
        pen.line(x - hw, y, x - 8.6, y, LW_WIRE)
        pen.line(x + 8.6, y, x + hw, y, LW_WIRE)
        pen.circle(x - 6.8, y, 1.8, W)
        pen.circle(x + 6.8, y, 1.8, W)
        by = y + 4.5 if k == "pb_no" else y - 3.2
        pen.line(x - 9.5, by, x + 9.5, by, W)
        pen.line(x, by, x, y + 10, W)
        if k == "estop":
            c.setLineWidth(W)
            c.arc(x - 6, y + 7, x + 6, y + 19, startAng=0, extent=180)
            pen.line(x - 6, y + 13, x + 6, y + 13, W)
            return 13.0, -4.0
        pen.line(x - 3.5, y + 10, x + 3.5, y + 10, W)
        return 10.0, -4.0
    if k == "ss":
        pen.line(x - hw, y, x - 8.6, y, LW_WIRE)
        pen.line(x + 8.6, y, x + hw, y, LW_WIRE)
        pen.circle(x - 6.8, y, 1.8, W)
        pen.circle(x + 6.8, y, 1.8, W)
        pen.line(x - 9.5, y + 4.5, x + 9.5, y + 4.5, W)  # contact bar (NO in the shown position)
        pen.line(x, y + 4.5, x, y + 8, W)
        c.setLineWidth(W)
        c.arc(x - 4, y + 6, x + 4, y + 12, startAng=0, extent=180)  # selector knob
        return 12.0, -4.0
    if k in ("switch_no", "switch_nc"):
        pen.line(x - hw, y, x - 8.6, y, LW_WIRE)
        pen.line(x + 8.6, y, x + hw, y, LW_WIRE)
        pen.circle(x - 6.8, y, 1.8, W)
        pen.circle(x + 6.8, y, 1.8, W)
        if k == "switch_no":
            pen.line(x - 5.2, y + 0.6, x + 8.0, y + 6.5, W)
            mx, my = x + 1.2, y + 3.4
        else:
            pen.line(x - 5.2, y - 0.6, x + 8.5, y - 3.2, W)
            mx, my = x + 1.2, y - 1.8
        a = d.actuator or "float"
        if a in ("float", "level"):
            pen.line(mx, my, mx, my - 6.5, LW_THIN)
            pen.circle(mx, my - 9.0, 2.5, W)
        elif a == "pressure":
            pen.line(mx, my, mx, my - 5.5, LW_THIN)
            c.setLineWidth(W)
            c.arc(mx - 4, my - 9.5, mx + 4, my - 1.5, startAng=180, extent=180)
            pen.line(mx - 4, my - 5.5, mx + 4, my - 5.5, W)
        elif a == "limit":
            pen.line(mx, my, mx, my - 5, LW_THIN)
            p = c.beginPath()
            p.moveTo(mx - 3, my - 5); p.lineTo(mx + 3, my - 5); p.lineTo(mx, my - 9.5); p.close()
            c.drawPath(p, stroke=1, fill=0)
        elif a in ("temp", "temperature"):
            p = c.beginPath()
            p.moveTo(mx, my)
            for j in range(4):
                p.lineTo(mx + (2.5 if j % 2 == 0 else -2.5), my - 2.2 * (j + 1))
            c.drawPath(p, stroke=1, fill=0)
        elif a == "flow":
            pen.line(mx, my, mx, my - 8, LW_THIN)
            pen.line(mx, my - 8, mx + 5, my - 6, W)
        return 7.0, -12.0
    if k in ("coil", "timer", "solenoid", "horn"):
        pen.line(x - hw, y, x - 9 if not pen.iec else x - 8, y, LW_WIRE)
        pen.line(x + (9 if not pen.iec else 8), y, x + hw, y, LW_WIRE)
        if pen.iec:
            c.setLineWidth(W)
            c.rect(x - 8, y - 5.5, 16, 11, stroke=1, fill=0)
            if k == "timer":
                c.rect(x - 8, y - 5.5, 4, 11, stroke=1, fill=0)
            return 5.5, -5.5
        pen.circle(x, y, 9, W)
        tag = d.tag if pen.width(d.tag, 7, "Inter-SemiBold") <= 16.5 else ""
        if tag:
            pen.text(x, y - 2.5, tag, 7, "Inter-SemiBold")
        return 9.0, -9.0
    if k == "light":
        pen.line(x - hw, y, x - 7, y, LW_WIRE)
        pen.line(x + 7, y, x + hw, y, LW_WIRE)
        pen.circle(x, y, 7, W)
        if pen.iec:
            pen.line(x - 4.9, y - 4.9, x + 4.9, y + 4.9, W)
            pen.line(x - 4.9, y + 4.9, x + 4.9, y - 4.9, W)
        else:
            for ang in (45, 135, 225, 315):
                a = math.radians(ang)
                pen.line(x + 8.5 * math.cos(a), y + 8.5 * math.sin(a), x + 11.5 * math.cos(a), y + 11.5 * math.sin(a), W)
            pen.text(x, y - 2.5, (d.color or "W")[:1], 7, "Inter-Bold")
        return 11.5, -11.5
    if k in ("fuse", "cb"):
        pen.line(x - hw, y, x - 7, y, LW_WIRE)
        pen.line(x + 7, y, x + hw, y, LW_WIRE)
        c.setLineWidth(W)
        if k == "fuse":
            c.rect(x - 7, y - 3, 14, 6, stroke=1, fill=0)
            pen.line(x - 7, y, x + 7, y, LW_THIN)
        else:
            pen.circle(x - 6, y, 1.6, W)
            pen.circle(x + 6, y, 1.6, W)
            c.arc(x - 6, y - 3, x + 6, y + 7, startAng=20, extent=140)
        return 5.0, -3.0
    if k == "terminal":
        c.setFillColorRGB(1, 1, 1)
        c.setLineWidth(W)
        c.circle(x, y, 3.2, stroke=1, fill=1)
        c.setFillColorRGB(0, 0, 0)
        return 3.2, -3.2
    raise ValueError(k)


def _draw_device_text(pen: _Pen, d: Dev, y: float, top: float, bot: float, coil_line: dict[str, int]) -> None:
    x = d.cx
    k = d.kind
    # tag above (coils carry it inside the circle when it fits)
    inside = k in ("coil", "timer") and not pen.iec and pen.width(d.tag, 7, "Inter-SemiBold") <= 16.5
    if k == "terminal":
        pen.text(x, y + top + 3, d.number or "?", 7, "Inter-SemiBold")
        pen.text(x, y + bot - 8.5, d.strip, 7)
        return
    if not inside:
        pen.text(x, y + top + 3, d.tag, 7, "Inter-SemiBold")
    below: list[str] = []
    if k == "ss":
        # position chart: letters, X under the closed position
        letters = [p[:1] for p in d.positions]
        n = len(letters)
        sp = 8.0
        x0 = x - sp * (n - 1) / 2
        for i, (L, full) in enumerate(zip(letters, d.positions)):
            pen.text(x0 + i * sp, y + bot - 8.5, L, 7)
            if d.position == full:
                pen.text(x0 + i * sp, y + bot - 16, "X", 7, "Inter-Bold")
        return
    if d.label:
        below.append(d.label.upper())
    if k == "timer" and d.rating:
        below.append(d.rating.upper())
    elif k in ("fuse", "cb") and d.rating:
        below.append(d.rating.upper())
    if k in CONTACT_KINDS and d.tag in coil_line:
        below.append(str(coil_line[d.tag]))
    yy = y + bot - 8.5
    for i, s in enumerate(below[:2]):
        is_ref = k in CONTACT_KINDS and s == str(coil_line.get(d.tag, ""))
        pen.text(x, yy, s, 7, "Inter" if not is_ref else "Inter-Italic", color=(0, 0, 0))
        yy -= 8.0


def _draw_column(pen: _Pen, g: _Geo, rungs: list[Rung], P: _Placed, wires: dict[int, str], nodes: _Nodes,
                 coil_line: dict[str, int], xrefs: dict[str, list[tuple[int, bool]]], rails: tuple[str, str],
                 step: int, line0: int, rows: tuple[int, int], rail_fuse: Dev | None, xref_w: float, desc_w: float,
                 rail_wires: Sequence[str] = (), grounded: bool = False) -> None:
    c = pen.c
    r0, r1 = rows
    y_top_rail = g.y(r0) + g.row_h * 0.55
    y_bot_rail = g.y(r1 - 1) - g.row_h * 0.35
    c.setStrokeColorRGB(0, 0, 0)
    # rails
    pen.line(g.x_left, y_top_rail, g.x_left, y_bot_rail, LW_RAIL)
    pen.line(g.x_right, y_top_rail, g.x_right, y_bot_rail, LW_RAIL)
    lab_y = y_top_rail + 4
    if rail_fuse is not None and r0 == 0:
        # supply fuse drawn vertically at the top of the left rail
        fy = y_top_rail + 16
        pen.line(g.x_left, y_top_rail, g.x_left, fy - 7, LW_RAIL)
        pen.c.setLineWidth(LW_SYMBOL)
        pen.c.rect(g.x_left - 3, fy - 7, 6, 14, stroke=1, fill=0)
        pen.line(g.x_left, fy - 7, g.x_left, fy + 7, LW_THIN)
        pen.line(g.x_left, fy + 7, g.x_left, fy + 13, LW_RAIL)
        pen.line(g.x_right, y_top_rail, g.x_right, fy + 13, LW_RAIL)
        pen.text(g.x_left + 6, fy + 1, rail_fuse.tag, 7, "Inter-SemiBold", align="left")
        if rail_fuse.rating:
            pen.text(g.x_left + 6, fy - 7, rail_fuse.rating.upper(), 7, align="left")
        lab_y = fy + 16
    if grounded:
        gx, gy = g.x_right, y_bot_rail
        pen.line(gx, gy, gx, gy - 6, LW_RAIL)
        for i, hw_ in enumerate((6.0, 4.0, 2.0)):
            pen.line(gx - hw_, gy - 6 - i * 2.4, gx + hw_, gy - 6 - i * 2.4, LW_SYMBOL)
    pen.text(g.x_left, lab_y, rails[0], 8, "Inter-Bold")
    pen.text(g.x_right, lab_y, rails[1], 8, "Inter-Bold")
    if rail_wires and tuple(rail_wires) != tuple(rails):
        pen.text(g.x_left + 4, y_top_rail - 8, rail_wires[0], 7, align="left")
        pen.text(g.x_right - 4, y_top_rail - 8, rail_wires[1], 7, align="right")
    # header labels for the line-number / xref / description columns
    pen.text(g.x_left - max(8.0, pen.width(rails[0], 8, "Inter-Bold") / 2 + 4), lab_y, "LINE", 7, "Inter-SemiBold", align="right",
             color=(0.33, 0.37, 0.43))
    pen.text(g.x_right + 13, lab_y, "CONTACTS", 7, "Inter-SemiBold", align="left", color=(0.33, 0.37, 0.43))
    pen.text(g.x_right + xref_w + 6, lab_y, "FUNCTION", 7, "Inter-SemiBold", align="left", color=(0.33, 0.37, 0.43))
    # line numbers
    for r in range(r0, r1):
        pen.text(g.x_left - 8, g.y(r) - 2.5, str(line0 + r * step), 7.5, "Inter", align="right")
    # wires and verticals
    for x0, x1, r, n in P.wires:
        if r0 <= r < r1 and x1 - x0 > 0.2:
            pen.line(x0, g.y(r), x1, g.y(r), LW_WIRE)
    for x, ra, rb, n in P.verts:
        if r0 <= ra < r1:
            pen.line(x, g.y(ra), x, g.y(rb), LW_WIRE)
    for x, r in P.dots:
        if r0 <= r < r1:
            c.circle(x, g.y(r), 1.9, stroke=0, fill=1)
    # devices
    for d in P.devs:
        if not (r0 <= d.row < r1):
            continue
        y = g.y(d.row)
        top, bot = _draw_symbol(pen, d, y)
        _draw_device_text(pen, d, y, top, bot, coil_line)
    # wire numbers: one label per node per row, on the longest segment of that row
    best: dict[tuple[int, int], tuple[float, float, float]] = {}
    for x0, x1, r, n in P.wires:
        if not (r0 <= r < r1):
            continue
        root = nodes.find(n)
        if (abs(x0 - g.x_left) < 0.5 and wires.get(root) == rails[0]) or (abs(x1 - g.x_right) < 0.5 and wires.get(root) == rails[1]):
            continue  # rail itself
        L = x1 - x0
        key = (root, r)
        if L > best.get(key, (-1.0, 0.0, 0.0))[0]:
            best[key] = (L, x0, x1)
    for (root, r), (L, x0, x1) in best.items():
        lab = wires.get(root, "?")
        if lab in rails:
            continue
        if L < 6:
            continue
        pen.text((x0 + x1) / 2, g.y(r) + 2.6, lab, 7, "Inter")
    # cross references (coil rows) and functions
    for d in P.devs:
        if d.kind in COIL_KINDS and r0 <= d.row < r1 and d.tag in xrefs:
            xx = g.x_right + 7
            y = g.y(d.row) - 2.5
            for line, is_nc in sorted(xrefs[d.tag]):
                s = str(line)
                w = pen.width(s, 7)
                if xx + w > g.x_right + xref_w - 4:
                    xx = g.x_right + 7
                    y -= 8.5
                pen.text(xx, y, s, 7, align="left")
                if is_nc:
                    pen.line(xx, y - 1.6, xx + w, y - 1.6, 0.5)
                xx += w + 4.5
    row = 0
    for rg in rungs:
        if r0 <= rg.first_line < r1:
            y = g.y(rg.first_line)
            if rg.section:
                pen.text(g.x_left + 10, y - 2.5, rg.section.upper(), 8, "Inter-SemiBold", align="left")
            elif rg.desc:
                from reportlab.lib.utils import simpleSplit

                lines = simpleSplit(rg.desc.upper(), "Inter", TXT_MIN, desc_w)
                for i, ln in enumerate(lines[: max(2, rg.rows * 4)]):
                    pen.text(g.x_right + xref_w + 6, y - 2.5 - i * 8.5, ln, 7, align="left")
        row += rg.rows


def _table(pen: _Pen, x: float, y_top: float, title: str, headers: list[str], widths: list[float],
           rows: list[list[str]], aligns: list[str] | None = None) -> float:
    """Ruled table with a title; text wraps inside cells. Returns the y of the bottom edge."""
    from reportlab.lib.utils import simpleSplit

    aligns = aligns or ["left"] * len(headers)
    W = sum(widths)
    pen.text(x, y_top - 9, title.upper(), 9, "Inter-Bold", align="left")
    y = y_top - 14
    pad = 3.0
    lead = 8.6

    def draw_row(cells: list[str], font: str, shade: bool) -> None:
        nonlocal y
        wrapped = [simpleSplit(str(cv), font, TXT_MIN, w - 2 * pad) or [""] for cv, w in zip(cells, widths)]
        h = max(len(wl) for wl in wrapped) * lead + 4.5
        if shade:
            pen.c.setFillColorRGB(0.93, 0.94, 0.96)
            pen.c.rect(x, y - h, W, h, stroke=0, fill=1)
            pen.c.setFillColorRGB(0, 0, 0)
        xx = x
        for wl, w, al in zip(wrapped, widths, aligns):
            yy = y - 8.2
            for ln in wl:
                if al == "center":
                    pen.text(xx + w / 2, yy, ln, 7, font)
                elif al == "right":
                    pen.text(xx + w - pad, yy, ln, 7, font, align="right")
                else:
                    pen.text(xx + pad, yy, ln, 7, font, align="left")
                yy -= lead
            xx += w
        y -= h
        pen.line(x, y, x + W, y, LW_THIN)

    top = y
    pen.line(x, y, x + W, y, LW_RULE)
    draw_row(headers, "Inter-SemiBold", True)
    pen.line(x, y, x + W, y, LW_RULE)
    for r in rows:
        draw_row(r, "Inter", False)
    pen.line(x, y, x + W, y, LW_RULE)
    xx = x
    for w in widths[:-1]:
        xx += w
        pen.line(xx, top, xx, y, LW_THIN)
    pen.line(x, top, x, y, LW_RULE)
    pen.line(x + W, top, x + W, y, LW_RULE)
    return y


def _table_height(headers: list[str], widths: list[float], rows: list[list[str]]) -> float:
    from reportlab.lib.utils import simpleSplit

    from pdfcore.fonts import register_reportlab

    register_reportlab()

    def h(cells, font):
        return max(len(simpleSplit(str(cv), font, TXT_MIN, w - 6) or [""]) for cv, w in zip(cells, widths)) * 8.6 + 4.5

    return 14 + h(headers, "Inter-SemiBold") + sum(h(r, "Inter") for r in rows)


def _terminal_strip_view(pen: _Pen, x: float, y_top: float, strips: dict[str, list[dict]], width: float) -> float:
    """Graphic terminal strips: panel wiring on the left, field wiring on the right, wire numbers on each side."""
    y = y_top
    for strip, terms in strips.items():
        pen.text(x, y - 9, f"TERMINAL STRIP {strip}", 9, "Inter-Bold", align="left")
        y -= 16
        fmax = max([pen.width(", ".join(t["field"]) or "—") for t in terms] or [20])
        bx = max(x + 90, x + width - 78 - fmax - 28 - 4)
        avail = bx - 50 - x
        pen.text(bx - 10, y - 6, "PANEL", 7, "Inter-SemiBold", align="right", color=(0.33, 0.37, 0.43))
        pen.text(bx + 38, y - 6, "FIELD", 7, "Inter-SemiBold", align="left", color=(0.33, 0.37, 0.43))
        y -= 10
        th = 15.0
        for t in terms:
            pen.c.setLineWidth(LW_SYMBOL)
            pen.c.rect(bx, y - th, 28, th, stroke=1, fill=0)
            pen.text(bx + 14, y - th + 4.5, t["number"], 7, "Inter-SemiBold")
            ym = y - th / 2
            pen.line(bx - 46, ym, bx, ym, LW_WIRE)
            pen.line(bx + 28, ym, bx + 74, ym, LW_WIRE)
            pen.text(bx - 23, ym + 2.2, t["wire"], 7)
            pen.text(bx + 51, ym + 2.2, t["wire"], 7)
            items = list(t["panel"])
            txt = ", ".join(items) or "—"
            while len(items) > 1 and pen.width(txt) > avail:
                items.pop()
                txt = ", ".join(items) + f" +{len(t['panel']) - len(items)}"
            pen.text(bx - 50, ym - 2.5, txt, 7, align="right")
            pen.text(bx + 78, ym - 2.5, ", ".join(t["field"]) or "—", 7, align="left")
            y -= th
        y -= 14
    return y


# ---------------------------------------------------------------- ladder assembly
ROW_H, SLOT_W = 46.0, 58.0
_DEFAULT_NOTES = [
    "WIRE NUMBERS ARE SHOWN ABOVE THE WIRES; EACH ELECTRICAL NODE HAS ONE NUMBER.",
    "NUMBERS RIGHT OF A COIL ARE THE LINES OF ITS CONTACTS; UNDERLINED = NORMALLY CLOSED.",
    "ITALIC NUMBERS UNDER A CONTACT ARE THE LINE OF ITS COIL.",
    "DEVICES SHOWN DE-ENERGIZED. SELECTOR CONTACTS ARE CLOSED IN THE POSITIONS MARKED X.",
]


def _default_desc(d: Dev, supply: str) -> str:
    k = d.kind
    if d.desc:
        return d.desc
    if k == "coil":
        t = d.tag.upper()
        return "MOTOR STARTER" if t.startswith("M") else "CONTROL RELAY" if t.startswith("CR") else "CONTACTOR" if t.startswith("C") else "RELAY"
    return {
        "timer": f"TIMING RELAY{', ' + d.rating.upper() if d.rating else ''}",
        "light": f"PILOT LIGHT, {_LIGHT_COLORS.get(d.color or 'W', 'WHITE')}{', ' + supply if supply else ''}",
        "pb_no": "PUSHBUTTON, 1 N.O.", "pb_nc": "PUSHBUTTON, 1 N.C.", "estop": "EMERGENCY STOP, MUSHROOM HEAD, 1 N.C.",
        "ss": f"SELECTOR SWITCH, {len(d.positions)}-POSITION ({'-'.join(d.positions)})",
        "fuse": f"FUSE{', ' + d.rating.upper() if d.rating else ''}", "cb": f"CIRCUIT BREAKER{', ' + d.rating.upper() if d.rating else ''}",
        "ol": "OVERLOAD RELAY", "ol_no": "OVERLOAD RELAY",
        "switch_no": f"{(d.actuator or 'float').upper()} SWITCH", "switch_nc": f"{(d.actuator or 'float').upper()} SWITCH",
        "solenoid": "SOLENOID", "horn": "HORN",
    }.get(k, k.upper())


def _next_sheet(s: str, k: int) -> str:
    m = re.search(r"(\d+)(\D*)$", s)
    if not m:
        return f"{s}.{k + 1}"
    n = int(m.group(1)) + k
    return s[: m.start(1)] + str(n).zfill(len(m.group(1))) + m.group(2)


def schematic(
    data: dict[str, Any] | str | Path,
    *,
    out: str | Path | None = None,
    size: str | None = None,
    xlsx: bool = True,
    terminal_view: str | None = None,
    brand: str = "default",
    png_dir: str | Path | None = None,
    quiet: bool = False,
) -> OpResult:
    """Ladder (JIC/IEC) control schematic from YAML → PDF on a title-block sheet, plus BOM / wire / terminal XLSX.

    YAML keys: title, sheet, project, client, rev, date, drawn, checked, revisions, size (ansi-b, arch-c …),
    standard (jic | iec), rails ([L1, L2] or ["+24V", "0V"]), supply (note), line_start (100), line_step (1),
    wires ({start: 3, scheme: sequential | line}), devices ({TAG: {desc, mfr, part, positions …}}), notes, and
    rungs — each ``{desc, devices: [...]}`` where devices run left rail → right rail and
    ``{parallel: [[...], [...]]}`` makes branches (seal-ins, H-O-A paths). Device types: no, nc (contacts of a
    coil/timer by tag; ``timed: on|off``), coil, timer, light, pb_no, pb_nc, estop, ss (``position``), switch_no /
    switch_nc (``actuator: float|pressure|limit|temp|flow``), ol, ol_no, fuse, cb, terminal (``TB-1-3``).
    *terminal_view*: "table" (default) or "strip" (graphic terminal strip with panel/field wire numbers).
    """
    import pikepdf
    import yaml
    from reportlab.pdfgen import canvas

    from pdfcore.gate import GateOptions
    from pdfcore.ops.typeset import make

    from pdfcore.fonts import register_reportlab

    register_reportlab()
    src_path = None if isinstance(data, dict) else Path(data)
    lad = load_ladder(data)
    meta: dict[str, Any] = lad["meta"]
    rungs: list[Rung] = lad["rungs"]
    iec = str(meta.get("standard", "jic")).lower() == "iec"
    rails = tuple(str(r) for r in (meta.get("rails") or (["+24V", "0V"] if iec else ["L1", "L2"])))[:2]
    if len(rails) != 2:
        raise ValueError("rails needs two names, e.g. [L1, L2]")
    wcfg = meta.get("wires") or {}
    scheme = str(wcfg.get("scheme", "sequential")).lower()
    rail_wires = [str(v) for v in (wcfg.get("rails") or rails)]
    line0 = int(meta.get("line_start", 100))
    step = int(meta.get("line_step", 1))
    supply = str(meta.get("supply", "") or "")
    size_name = _norm_size(size or str(meta.get("size", "ansi-b")))
    if size_name not in TEMPLATE_SIZES:
        raise ValueError(f"size must be one of {', '.join(TEMPLATE_SIZES)}")
    tv = (terminal_view or str(meta.get("terminal_view", "table"))).lower()
    fz = meta.get("fuse") or meta.get("supply_fuse")
    rail_fuse = _parse_dev({"type": "fuse", **fz}, lad["devices"]) if isinstance(fz, dict) else None
    grounded = bool(meta.get("grounded", False))
    ax_in, ay_in, aw_in, ah_in = drawing_area(size_name)
    AW, AH = aw_in * INCH, ah_in * INCH

    # ---- geometry
    maxw = max([sum(_width(x) for x in r.elems) for r in rungs if r.elems] or [4])
    rail_gap = max(5 * SLOT_W, maxw * SLOT_W + 24)
    num_w, xref_w, desc_w, col_gap = 36.0, 70.0, 104.0, 26.0
    col_w = num_w + rail_gap + xref_w + desc_w
    header_h = 40.0
    y_first = AH - header_h - 26 - ROW_H * 0.55 - (30 if rail_fuse is not None else 0)  # row-0 centre
    cap = max(1, int((y_first - 18) // ROW_H) + 1)
    for r in rungs:
        r.rows = _rows(r.elems) if r.elems else 1
        if r.rows > cap:
            raise ValueError(f"a rung needs {r.rows} lines but a column holds {cap}; use a larger --size")
    # pack rungs into columns
    cols: list[tuple[int, int]] = []
    row = 0
    start = 0
    for r in rungs:
        if row + r.rows - start > cap:
            cols.append((start, row))
            start = row
        r.first_line = row
        row += r.rows
    cols.append((start, row))
    total_rows = row
    if len(cols) > 1:
        # several ladder columns: tighten the columns so two (or more) share a sheet
        tight_gap = max(4 * SLOT_W, maxw * SLOT_W + 24)
        for n_try in (3, 2):
            dw = (AW - (n_try - 1) * col_gap) / n_try - num_w - tight_gap - xref_w
            if dw >= 72 and len(cols) >= n_try:
                rail_gap, desc_w = tight_gap, min(desc_w, dw)
                col_w = num_w + rail_gap + xref_w + desc_w
                break
    per_sheet = max(1, int((AW + col_gap) // (col_w + col_gap)))
    if col_w > AW:
        raise ValueError(f"ladder is {col_w / INCH:.1f} in wide but the drawing area is {AW / INCH:.1f} in; use a larger --size")

    # ---- place
    nodes = _Nodes()
    rail_l, rail_r = nodes.new(rail_wires[0]), nodes.new(rail_wires[1])
    P = _Placed()
    geos: list[_Geo] = []
    for ci, (ra, rb) in enumerate(cols):
        slot_col = ci % per_sheet
        x0 = slot_col * (col_w + col_gap)
        g = _Geo(x0 + num_w, x0 + num_w + rail_gap, y_first, ROW_H, SLOT_W, row0=ra)
        geos.append(g)
        for rg in rungs:
            if ra <= rg.first_line < rb and rg.elems:
                _layout_rung(rg, rg.first_line, nodes, rail_l, rail_r, g, P, lambda rr: line0 + rr * step)
    # wire numbers in order of first appearance
    order = sorted(((n, line0 + r * step, x0) for x0, x1, r, n in P.wires), key=lambda t: (t[1], t[2]))
    wires = _assign_wires(nodes, [(n, ln) for n, ln, _x in order], scheme, int(wcfg.get("start", 3 if rail_wires == ["L1", "L2"] or rail_wires == list(rails) else 1)))

    # ---- cross references
    coil_line: dict[str, int] = {}
    for d in P.devs:
        if d.kind in COIL_KINDS:
            if d.tag in coil_line:
                raise ValueError(f"coil {d.tag} appears twice (lines {coil_line[d.tag]} and {d.line})")
            coil_line[d.tag] = d.line
    xrefs: dict[str, list[tuple[int, bool]]] = {}
    for d in P.devs:
        if d.kind in CONTACT_KINDS:
            xrefs.setdefault(d.tag, []).append((d.line, d.kind == "nc"))
    orphans = sorted({t for t in xrefs if t not in coil_line})

    # ---- BOM
    bom: dict[str, dict[str, Any]] = {}
    for d in P.devs:
        if d.kind == "terminal":
            continue
        main = d.kind not in CONTACT_KINDS
        row_ = bom.get(d.tag)
        if row_ is None:
            row_ = {"tag": d.tag, "desc": "", "mfr": "", "part": "", "qty": 1, "lines": set(), "main": False}
            bom[d.tag] = row_
        row_["lines"].add(d.line)
        if main and not row_["main"]:
            row_.update(desc=_default_desc(d, supply), main=True)
        if d.mfr:
            row_["mfr"] = d.mfr
        if d.part:
            row_["part"] = d.part
    for tag, row_ in bom.items():
        base = lad["devices"].get(tag, {})
        if not row_["desc"]:
            row_["desc"] = str(base.get("desc") or ("CONTACT (DEVICE NOT SHOWN)" if tag in orphans else ""))
        row_["mfr"] = row_["mfr"] or str(base.get("mfr", base.get("manufacturer", "")) or "")
        row_["part"] = row_["part"] or str(base.get("part", "") or "")
        row_["qty"] = int(base.get("qty", 1))
    # terminals
    term_auto: dict[str, int] = {}
    strips: dict[str, list[dict]] = {}
    for d in sorted((d for d in P.devs if d.kind == "terminal"), key=lambda d: (d.line, d.cx)):
        if not d.number:
            term_auto[d.strip] = term_auto.get(d.strip, 0) + 1
            d.number = str(term_auto[d.strip])
            d.tag = f"{d.strip}-{d.number}"
        elif not d.tag:
            d.tag = f"{d.strip}-{d.number}"
    endpoints: dict[int, list[tuple[Dev, str]]] = {}
    if rail_fuse is not None:
        rail_fuse.nodes = (rail_l, rail_l)
        rail_fuse.line = line0
        endpoints.setdefault(nodes.find(rail_l), []).append((rail_fuse, rail_fuse.term_names()[1]))
        bom[rail_fuse.tag] = {"tag": rail_fuse.tag, "desc": _default_desc(rail_fuse, supply), "mfr": rail_fuse.mfr,
                              "part": rail_fuse.part, "qty": 1, "lines": {line0}, "main": True}
    for d in P.devs:
        if d.kind == "terminal":
            continue
        t1, t2 = d.term_names()
        endpoints.setdefault(nodes.find(d.nodes[0]), []).append((d, t1))
        endpoints.setdefault(nodes.find(d.nodes[1]), []).append((d, t2))
    for d in sorted((d for d in P.devs if d.kind == "terminal"), key=lambda d: (d.strip, _natural(d.number))):
        root = nodes.find(d.nodes[0])
        conns = endpoints.get(root, [])
        strips.setdefault(d.strip, []).append({
            "strip": d.strip, "number": d.number, "wire": wires.get(root, "?"), "line": d.line,
            "panel": [n for n, (x, _t) in zip(_endpoint_names(conns), conns) if not x.field_device],
            "field": [n for n, (x, _t) in zip(_endpoint_names(conns), conns) if x.field_device],
        })
    for strip, terms in strips.items():
        if strip not in bom:
            bom[strip] = {"tag": strip, "desc": "TERMINAL BLOCK", "mfr": "", "part": "", "qty": len(terms),
                          "lines": {t["line"] for t in terms}, "main": True}
            base = lad["devices"].get(strip, {})
            bom[strip].update({k: str(base[k]) for k in ("desc", "mfr", "part") if base.get(k)})
    for extra in meta.get("bom_extra") or []:
        tag = str(extra.get("tag", ""))
        bom[tag or f"ITEM{len(bom) + 1}"] = {"tag": tag, "desc": str(extra.get("desc", "")), "mfr": str(extra.get("mfr", "")),
                                            "part": str(extra.get("part", "")), "qty": int(extra.get("qty", 1)), "lines": set(), "main": True}
    bom_rows = sorted(bom.values(), key=lambda r: (_tag_sort(r["tag"]), r["tag"]))
    # wire list
    wire_list: list[dict[str, Any]] = []
    seen_roots: dict[int, dict] = {}
    for x0, x1, r, n in sorted(P.wires, key=lambda w: (w[2], w[0])):
        root = nodes.find(n)
        ent = seen_roots.get(root)
        if ent is None:
            ent = {"wire": wires.get(root, "?"), "lines": set(), "connections": []}
            seen_roots[root] = ent
            wire_list.append(ent)
        ent["lines"].add(line0 + r * step)
    for root, ent in seen_roots.items():
        ent["connections"] = _endpoint_names(endpoints.get(root, []))
        ent["terminals"] = [d.tag for d in P.devs if d.kind == "terminal" and nodes.find(d.nodes[0]) == root]
    labels = [e["wire"] for e in wire_list]
    if len(labels) != len(set(labels)):
        raise RuntimeError(f"duplicate wire numbers: {sorted(l for l in labels if labels.count(l) > 1)}")

    # ---- render sheets
    title = str(meta.get("title", "CONTROL SCHEMATIC"))
    heading = str(meta.get("heading", title))
    notes = list(_DEFAULT_NOTES) + [str(n) for n in (meta.get("notes") or [])]
    bom_headers = ["TAG", "QTY", "DESCRIPTION", "MANUFACTURER", "PART NO.", "LINES"]
    bom_table = [[r["tag"], str(r["qty"]), r["desc"], r["mfr"], r["part"], _ranges(r["lines"])] for r in bom_rows]
    term_headers = ["STRIP", "TERM", "WIRE", "LINE", "PANEL SIDE", "FIELD SIDE"]
    term_table = [[t["strip"], t["number"], t["wire"], str(t["line"]), ", ".join(t["panel"]) or "—", ", ".join(t["field"]) or "—"]
                  for terms in strips.values() for t in terms]

    def panel_widths(avail: float) -> tuple[list[float], list[float]]:
        bw = [44, 26, 150, 76, 82, 46]
        tw = [40, 34, 38, 34, 110, 110]
        f = min(1.0, avail / sum(bw))
        bw = [w * f for w in bw[:2]] + [bw[2] * f + (avail - sum(bw) * f) if avail > sum(bw) else bw[2] * f] + [w * f for w in bw[3:]]
        bw[2] = avail - (sum(bw) - bw[2])
        tf = min(1.0, avail / sum(tw))
        tw = [w * tf for w in tw]
        return bw, tw

    def notes_height(width: float) -> float:
        from reportlab.lib.utils import simpleSplit

        return 16 + sum(len(simpleSplit(f"{i + 1}. {n}", "Inter", TXT_MIN, width - 12)) * 8.6 for i, n in enumerate(notes))

    def draw_notes(pen: _Pen, x: float, y_top: float, width: float) -> float:
        from reportlab.lib.utils import simpleSplit

        pen.text(x, y_top - 9, "NOTES", 9, "Inter-Bold", align="left")
        y = y_top - 20
        for i, n in enumerate(notes):
            lines = simpleSplit(n.upper(), "Inter", TXT_MIN, width - 14)
            pen.text(x, y, f"{i + 1}.", 7, align="left")
            for ln in lines:
                pen.text(x + 12, y, ln, 7, align="left")
                y -= 8.6
        return y

    def strip_height() -> float:
        return sum(26 + 15 * len(t) + 14 for t in strips.values())

    n_ladder_sheets = math.ceil(len(cols) / per_sheet)
    right_x = col_w + col_gap
    right_w = AW - right_x
    panel: list[str] = []  # what goes in the right panel of sheet 1
    if n_ladder_sheets == 1 and len(cols) == 1 and right_w >= 300:
        bw, tw = panel_widths(right_w)
        hleft = AH - 4
        for name, hh in (("bom", _table_height(bom_headers, bw, bom_table) + 14),
                         ("terms", (strip_height() if tv == "strip" else _table_height(term_headers, tw, term_table) + 14) if strips else 0),
                         ("notes", notes_height(right_w))):
            if hh == 0:
                continue
            if hh <= hleft:
                panel.append(name)
                hleft -= hh
            else:
                break
    leftover = [n for n in ("bom", "terms", "notes") if n not in panel and not (n == "terms" and not strips)]
    if n_ladder_sheets > 1 or len(cols) > 1:
        leftover = [n for n in ("bom", "terms", "notes") if not (n == "terms" and not strips)]
    sheet_pdfs: list[tuple[Path, str]] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="pdfcore-ladder-"))
    try:
        def new_canvas(path: Path):
            cv = canvas.Canvas(str(path), pagesize=(AW, AH), pageCompression=1, initialFontName="Inter")
            cv.setTitle(title)
            cv.setAuthor("PDF Studio")
            return cv

        for si in range(n_ladder_sheets):
            pth = tmpdir / f"ladder{si}.pdf"
            cv = new_canvas(pth)
            pen = _Pen(cv, iec)
            pen.text(0, AH - 12, heading.upper() + ("" if si == 0 else " (CONTINUED)"), 11, "Inter-Bold", align="left")
            sub_t = " · ".join(s for s in [supply.upper(), "JIC / NFPA 79 SYMBOLS" if not iec else "IEC 60617 SYMBOLS",
                                            f"LINES {line0 + cols[si * per_sheet][0] * step}–{line0 + (cols[min(len(cols), (si + 1) * per_sheet) - 1][1] - 1) * step}"] if s)
            pen.text(0, AH - 24, sub_t, 7.5, "Inter", align="left", color=(0.33, 0.37, 0.43))
            pen.line(0, AH - header_h + 8, AW, AH - header_h + 8, LW_RULE)
            for ci in range(si * per_sheet, min(len(cols), (si + 1) * per_sheet)):
                g = geos[ci]
                _draw_column(pen, g, rungs, P, wires, nodes, coil_line, xrefs, rails, step, line0, cols[ci], rail_fuse, xref_w,
                             desc_w, rail_wires, grounded)
            if si == 0 and panel:
                bw, tw = panel_widths(right_w)
                y = AH - 4
                pen.line(right_x - col_gap / 2, 0, right_x - col_gap / 2, AH - header_h + 8, LW_THIN)
                if "bom" in panel:
                    y = _table(pen, right_x, y, "Bill of materials", bom_headers, bw, bom_table,
                               ["left", "center", "left", "left", "left", "left"]) - 14
                if "terms" in panel:
                    if tv == "strip":
                        y = _terminal_strip_view(pen, right_x, y, strips, right_w)
                    else:
                        y = _table(pen, right_x, y, "Terminal list", term_headers, tw, term_table,
                                   ["left", "center", "center", "center", "left", "left"]) - 14
                if "notes" in panel:
                    draw_notes(pen, right_x, y, right_w)
            cv.showPage()
            cv.save()
            sheet_pdfs.append((pth, title if si == 0 else f"{title} (CONT.)"))
        if leftover:
            # flow BOM / terminals / notes through two columns per sheet, continuing on more sheets
            half = (AW - col_gap) / 2
            bw, tw = panel_widths(half)
            blocks: list[tuple[str, Any]] = []
            if "bom" in leftover:
                blocks.append(("table", ("Bill of materials", bom_headers, bw, bom_table, ["left", "center", "left", "left", "left", "left"])))
            if "terms" in leftover:
                if tv == "strip":
                    for strip, terms in strips.items():
                        blocks.append(("strip", (strip, terms)))
                else:
                    blocks.append(("table", ("Terminal list", term_headers, tw, term_table, ["left", "center", "center", "center", "left", "left"])))
            if "notes" in leftover:
                blocks.append(("notes", None))
            top_y, bottom_y = AH - header_h, 6.0
            pages: list[list[tuple[int, float, str, Any]]] = [[]]  # per sheet: (column, y_top, kind, payload)
            col, y = 0, top_y

            def advance() -> None:
                nonlocal col, y
                col += 1
                y = top_y
                if col > 1:
                    pages.append([])
                    col = 0

            for kind, payload in blocks:
                if kind == "table":
                    ttl, hdr, wid, rows_, al = payload
                    rest, first = list(rows_), True
                    while rest:
                        room = y - bottom_y - _table_height(hdr, wid, [])
                        take, used = 0, 0.0
                        for rr in rest:
                            hh = _table_height(hdr, wid, [rr]) - _table_height(hdr, wid, [])
                            if used + hh > room:
                                break
                            used += hh
                            take += 1
                        if take == 0:
                            if y == top_y:
                                take = 1
                            else:
                                advance()
                                continue
                        part = rest[:take]
                        rest = rest[take:]
                        pages[-1].append((col, y, "table", (ttl if first else f"{ttl} (cont.)", hdr, wid, part, al)))
                        first = False
                        y -= _table_height(hdr, wid, part) + 14
                        if rest:
                            advance()
                elif kind == "strip":
                    strip, terms = payload
                    rest = list(terms)
                    while rest:
                        n_fit = int((y - bottom_y - 40) // 15)
                        if n_fit <= 0:
                            advance()
                            continue
                        part, rest = rest[:n_fit], rest[n_fit:]
                        pages[-1].append((col, y, "strip", {strip: part}))
                        y -= 26 + 15 * len(part) + 14
                        if rest:
                            advance()
                else:
                    if notes_height(half) > y - bottom_y:
                        advance()
                    pages[-1].append((col, y, "notes", None))
                    y -= notes_height(half) + 14
            for k, items in enumerate(p_ for p_ in pages if p_):
                pth = tmpdir / f"tables{k}.pdf"
                cv = new_canvas(pth)
                pen = _Pen(cv, iec)
                pen.text(0, AH - 12, f"{heading.upper()} — BILL OF MATERIALS AND TERMINALS" + (" (CONTINUED)" if k else ""),
                         11, "Inter-Bold", align="left")
                pen.line(0, AH - header_h + 8, AW, AH - header_h + 8, LW_RULE)
                for c_, y_, kind, payload in items:
                    x_ = 0 if c_ == 0 else half + col_gap
                    if kind == "table":
                        _table(pen, x_, y_, *payload)
                    elif kind == "strip":
                        _terminal_strip_view(pen, x_, y_, payload, half)
                    else:
                        draw_notes(pen, x_, y_, half)
                cv.showPage()
                cv.save()
                sheet_pdfs.append((pth, f"{title} — BOM & TERMINALS" + (f" ({k + 1})" if len([p_ for p_ in pages if p_]) > 1 else "")))

        # ---- title-block sheets
        base_sheet = str(meta.get("sheet", "E-601"))
        tb_keys = ("project", "client", "rev", "date", "drawn", "checked", "revisions", "notes_title")
        parts: list[Path] = []
        for k, (pth, stitle) in enumerate(sheet_pdfs):
            sd = {kk: meta[kk] for kk in tb_keys if kk in meta}
            sd.update({"sheet": _next_sheet(base_sheet, k), "title": stitle, "size": size_name, "scale": "NONE",
                       "project": str(meta.get("project", "")) or title, "image": pth.name})
            if sd.get("date") is not None and not isinstance(sd.get("date"), str):
                sd["date"] = str(sd["date"])
            if sd.get("rev") is not None:
                sd["rev"] = str(sd["rev"])
            sd["revisions"] = [{"rev": str(r.get("rev", "")), "date": str(r.get("date", "")), "description": str(r.get("description", ""))}
                               for r in (sd.get("revisions") or [])]
            yml = tmpdir / f"sheet{k}.yaml"
            yml.write_text(yaml.safe_dump(sd, sort_keys=False, default_flow_style=False, allow_unicode=True))
            o = tmpdir / f"sheet{k}.out.pdf"
            make("drawing-sheet", yml, brand=brand, out=o, quiet=True)
            parts.append(o)
        dst = output_path(src_path or Path.cwd() / "schematic.yaml", "schematic", ".pdf", out=out)
        with pikepdf.open(str(parts[0])) as first:
            for pth in parts[1:]:
                with pikepdf.open(str(pth)) as more:
                    first.pages.extend(more.pages)
            first.docinfo["/Title"] = title
            first.docinfo["/Author"] = "PDF Studio"
            first.docinfo["/Subject"] = "Ladder control schematic"
            first.Root.Lang = pikepdf.String("en-US")
            first.save(str(dst))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    extra: list[Path] = []
    if xlsx:
        xp = output_path(dst, "lists", ".xlsx")
        _ladder_xlsx(xp, bom_rows, wire_list, strips, coil_line, xrefs)
        from pdfcore.ops.extract import _verify

        _verify([xp])
        extra.append(xp)
    data_out = {
        "sheets": len(sheet_pdfs), "lines": [line0, line0 + (total_rows - 1) * step],
        "wires": {e["wire"]: {"lines": sorted(e["lines"]), "connections": e["connections"], "terminals": e["terminals"]} for e in wire_list},
        "coils": coil_line, "xrefs": {k: [{"line": ln, "nc": nc} for ln, nc in sorted(v)] for k, v in xrefs.items()},
        "bom": [{k: (sorted(v) if isinstance(v, set) else v) for k, v in r.items() if k != "main"} for r in bom_rows],
        "terminals": [t for ts in strips.values() for t in ts],
        "devices": [{"tag": d.tag, "kind": d.kind, "line": d.line, "nodes": [wires.get(nodes.find(n), "?") for n in d.nodes]} for d in P.devs],
        "orphan_contacts": orphans,
    }
    opts = GateOptions(profile="drawing", png_dir=Path(png_dir) if png_dir else None, png_dpi=110 if png_dir else 60)
    return finalize(dst, None, options=opts, extra=extra, data=data_out, quiet=quiet)


def _ranges(lines: Iterable[int]) -> str:
    """100, 101, 102, 105 → "100–102, 105"."""
    vals = sorted(set(int(v) for v in lines))
    out: list[str] = []
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[j] + 1:
            j += 1
        out.append(str(vals[i]) if j - i < 2 else f"{vals[i]}–{vals[j]}")
        if j - i == 1:
            out.append(str(vals[j]))
        i = j + 1
    return ", ".join(out)


def _endpoint_names(conns: list[tuple["Dev", str]]) -> list[str]:
    names = [f"{d.tag}:{t}" for d, t in conns]
    return [f"{n}({d.line})" if names.count(n) > 1 else n for n, (d, _t) in zip(names, conns)]


def _natural(s: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s))


def _tag_sort(tag: str) -> tuple:
    order = ["FU", "CB", "DS", "CPT", "T", "M", "OL", "CR", "TR", "PB", "SS", "LS", "LSH", "LSL", "FS", "PS", "PL", "LT", "TB"]
    m = re.match(r"[A-Za-z]+", tag)
    pre = m.group(0).upper() if m else ""
    return (order.index(pre) if pre in order else len(order), _natural(tag))


def _ladder_xlsx(path: Path, bom_rows: list[dict], wire_list: list[dict], strips: dict[str, list[dict]],
                 coil_line: dict[str, int], xrefs: dict[str, list[tuple[int, bool]]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_fill = PatternFill("solid", fgColor="1D4A6E")

    def sheet(ws, headers: list[str], rows: list[list[Any]], widths: list[int]) -> None:
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill = hdr_font, hdr_fill
            c.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths):
            ws.column_dimensions[chr(ord("A") + i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    ws = wb.active
    ws.title = "BOM"
    sheet(ws, ["Tag", "Qty", "Description", "Manufacturer", "Part No.", "Lines"],
          [[r["tag"], r["qty"], r["desc"], r["mfr"], r["part"], _ranges(r["lines"])] for r in bom_rows],
          [10, 6, 46, 18, 20, 18])
    sheet(wb.create_sheet("Wire List"), ["Wire No.", "Lines", "Connections", "Terminals"],
          [[w["wire"], ", ".join(str(v) for v in sorted(w["lines"])), ", ".join(w["connections"]), ", ".join(w["terminals"])] for w in wire_list],
          [10, 16, 60, 20])
    sheet(wb.create_sheet("Terminals"), ["Strip", "Terminal", "Wire No.", "Line", "Panel Side", "Field Side"],
          [[t["strip"], t["number"], t["wire"], t["line"], ", ".join(t["panel"]), ", ".join(t["field"])] for ts in strips.values() for t in ts],
          [10, 10, 10, 8, 34, 34])
    sheet(wb.create_sheet("Cross-Reference"), ["Coil", "Coil Line", "N.O. Contact Lines", "N.C. Contact Lines"],
          [[t, coil_line.get(t, ""), ", ".join(str(l) for l, nc in sorted(v) if not nc), ", ".join(str(l) for l, nc in sorted(v) if nc)]
           for t, v in sorted(xrefs.items())], [10, 10, 24, 24])
    wb.save(str(path))


# ============================================================== CLI
def _kv_floats(s: str | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = float(v.strip().lower().rstrip("mm"))
    return out


@sub("dxf2pdf", "Plot DXF/DWG (DWG via the installed converter, HPGL .plt) to vector PDF with line weights, paper size, scale")
def _cli_dxf2pdf(p: argparse.ArgumentParser):
    p.add_argument("input", help=".dxf, .dwg (needs a DWG converter: ODA File Converter) or .plt/.hpgl")
    p.add_argument("-o", "--out", help="output PDF (default: <name>.plot.pdf next to the input)")
    p.add_argument("--size", help="paper: ansi-a…ansi-e, arch-a…arch-e1, letter, tabloid, a4…a0 or WxH[in|mm]")
    p.add_argument("--orientation", choices=["auto", "landscape", "portrait"], default="auto")
    p.add_argument("--scale", help="plot to scale, e.g. 1/4\"=1'-0\", 1\"=20', 1:50 (default: fit to sheet)")
    p.add_argument("--units", help="drawing units if $INSUNITS is missing or wrong: in, ft, mm, cm, m")
    p.add_argument("--layout", help="paperspace layout name (default: Model; empty Model → first non-empty layout)")
    p.add_argument("--extents", default="auto", help="auto (ignore XLINE/RAY and far outliers) | all | x0,y0,x1,y1 window")
    p.add_argument("--lineweights", choices=list(LINEWEIGHT_POLICIES), default="absolute",
                   help="absolute = DXF mm on paper; relative = scaled to sheet size; by-layer = layer table only; uniform")
    p.add_argument("--lineweight-scale", type=float, default=1.0, help="multiply all line weights")
    p.add_argument("--layer-weights", help='mm per layer (glob), e.g. "E-POWR=0.7,E-ANNO*=0.18"')
    p.add_argument("--ctb", help="AutoCAD .ctb plot-style table (colour → pen weight)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--mono", action="store_true", help="all black (monochrome plot)")
    g.add_argument("--grayscale", action="store_true", help="colours to grey shades")
    p.add_argument("--title-block", action="store_true", help="place the plot on the drawing-sheet title block")
    p.add_argument("--sheet", help="title block: sheet number")
    p.add_argument("--title", help="title block: sheet title")
    p.add_argument("--project", help="title block: project")
    p.add_argument("--rev", help="title block: revision")
    p.add_argument("--brand", default="default", help="title block brand (templates/brands)")

    def run(ns):
        tb = None
        if ns.title_block or ns.sheet or ns.title or ns.project:
            tb = {k: v for k, v in {"sheet": ns.sheet, "title": ns.title, "project": ns.project, "rev": ns.rev}.items() if v}
        return dxf_to_pdf(ns.input, out=ns.out, size=ns.size, orientation=ns.orientation, scale=ns.scale, units=ns.units,
                          layout=ns.layout, lineweights=ns.lineweights, lineweight_scale=ns.lineweight_scale,
                          layer_weights=_kv_floats(ns.layer_weights) or None, ctb=ns.ctb,
                          color="mono" if ns.mono else "grayscale" if ns.grayscale else "color", title_block=tb, brand=ns.brand,
                          extents=ns.extents if ns.extents in ("auto", "all") else [float(v) for v in ns.extents.split(",")])

    return run


sub("dwg2pdf", "Alias of dxf2pdf for DWG input")(_cli_dxf2pdf)


@sub("pdf2dxf", "PDF page vectors and text → DXF (layers by colour/line weight, inches or real units at a scale)")
def _cli_pdf2dxf(p: argparse.ArgumentParser):
    p.add_argument("input")
    p.add_argument("-o", "--out", help="output .dxf (one page) or folder")
    p.add_argument("--sheet", help="sheet number(s), e.g. E-101 or E-101,E-201")
    p.add_argument("--page", help="page(s), 1-based: 2 or 1-3")
    p.add_argument("--scale", help="drawing scale → real-world units, e.g. 1/4\"=1'-0\" (feet), 1:50")
    p.add_argument("--units", help="output units: in, ft, mm, cm, m (default: in, or as the scale suggests)")
    p.add_argument("--layers", choices=["color+weight", "color", "weight", "single"], default="color+weight")
    p.add_argument("--curves", choices=["polyline", "spline"], default="polyline")
    p.add_argument("--no-text", action="store_true", help="geometry only")
    p.add_argument("--region", help="x0,top,x1,bottom in points (top-left origin) to export part of the sheet")
    p.add_argument("--password")

    def run(ns):
        reg = [float(v) for v in ns.region.split(",")] if ns.region else None
        return pdf_to_dxf(ns.input, sheet=ns.sheet, page=ns.page, scale=ns.scale, units=ns.units, layers=ns.layers,
                          curves=ns.curves, text=not ns.no_text, region=reg, out=ns.out, password=ns.password)

    return run


@sub("schematic", "Ladder (JIC/IEC) control schematic from YAML: wire numbers, cross-refs, BOM, terminals (+ XLSX)")
def _cli_schematic(p: argparse.ArgumentParser):
    p.add_argument("input", help="ladder YAML (see pdfcore/ops/plans/examples/ladder-motor-starter.yaml)")
    p.add_argument("-o", "--out", help="output PDF (default: <name>.schematic.pdf)")
    p.add_argument("--size", help="sheet size (ansi-b default; ansi-a…e, arch-a…e)")
    p.add_argument("--terminal-view", choices=["table", "strip"], help="terminal list as a table or a graphic strip")
    p.add_argument("--no-xlsx", action="store_true", help="skip the BOM / wire list / terminal XLSX")
    p.add_argument("--brand", default="default")
    p.add_argument("--png-dir", help="save page renders here")

    def run(ns):
        return schematic(ns.input, out=ns.out, size=ns.size, xlsx=not ns.no_xlsx, terminal_view=ns.terminal_view,
                         brand=ns.brand, png_dir=ns.png_dir)

    return run
