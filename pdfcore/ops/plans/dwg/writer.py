"""Native, clean-room DWG **writer** (MIT): an ``ezdxf`` document → R2000 (AC1015) DWG file.

Implementation basis
--------------------
Written only from the **Open Design Specification for .dwg files v5.4.1** (section numbers below refer to
it), ezdxf's documentation for the source object model, and this package's own reader. Byte layouts the
spec leaves open (which sections an R2000 file carries, the AcDb:AuxHeader values, the second-header
bytes) were checked against AutoCAD-saved R2000 sample files by inspecting their bytes; no other DWG
implementation's source was consulted. LibreDWG's ``dwgread``/``dwg2dxf`` binaries are used only as
black-box validators in local tests.

File layout written (§3.1, R15):

=================================  ==============================================================
file header (§3.2)                 version, code page, 6 section locators, CRC (§3.2.6), sentinel
AcDb:AuxHeader (chapter 27)        123 bytes, locator 5
preview (§14.2)                    empty image area (no thumbnail)
AcDb:Header (chapter 9)            sentinel, RL size, header variables, CRC, sentinel; locator 0
AcDb:Classes (§10.1)               sentinel, RL size, classes, CRC, sentinel; locator 1
AcDb:AcDbObjects (chapter 20)      every object record with its CRC
AcDb:Handles (chapter 23)          object map in ≤2032-byte sections, deltas restarting per section,
                                   big-endian section CRCs; locator 2
second file header (chapter 26)    locators and table-control handles, CRC, sentinels
AcDb:Template (chapter 22)         MEASUREMENT; locator 4
=================================  ==============================================================

Objects written: the nine table controls plus the viewport-entity-header control; LTYPE (BYBLOCK,
BYLAYER, CONTINUOUS + the document's linetypes, simple dashes), LAYER, STYLE, APPID, DIMSTYLE, VPORT
``*Active``; BLOCK_HEADER/BLOCK/ENDBLK for ``*Model_Space``, every paper-space layout and every block;
the named-object dictionary with ACAD_GROUP, ACAD_LAYOUT, ACAD_MLINESTYLE (MLINESTYLE Standard),
ACAD_PLOTSETTINGS and ACAD_PLOTSTYLENAME (DICTIONARYWDFLT + the "Normal" ACDBPLACEHOLDER); one LAYOUT per
layout.

Entities: LINE, ARC, CIRCLE, ELLIPSE, POINT, SOLID, TRACE, 3DFACE, LWPOLYLINE, POLYLINE (2D, 3D, polyface,
mesh) with VERTEX/SEQEND, SPLINE (and HELIX as a spline), TEXT, MTEXT, INSERT/MINSERT with ATTRIB/SEQEND,
ATTDEF, HATCH (solid, pattern; polyline and line/arc/ellipse/spline edge paths), DIMENSION (all seven
kinds, referencing their ``*D`` geometry block), RAY, XLINE, VIEWPORT. Anything else that ezdxf can
explode (``virtual_entities()``: LEADER, MLEADER, MLINE, ACAD_TABLE, ARC_DIMENSION, dimensions without a
geometry block …) is written as its primitives; the rest is skipped. :class:`WriteReport` lists both.

Text is code page 1252 (``$DWGCODEPAGE`` ANSI_1252): characters outside it are written as ``\\U+XXXX``
escapes (which AutoCAD and the reader decode), characters outside the BMP as ``?``; counts are reported.
R2000 has no true colour or transparency: true colours become the nearest ACI colour (reported).
"""

from __future__ import annotations

import math
import struct
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pdfcore.ops.plans.dwg.bitwriter import BitWriter, crc_le, mc_encode
from pdfcore.ops.plans.dwg.bits import crc8
from pdfcore.ops.plans.dwg.container import SENT_CLASSES, SENT_HEADER
from pdfcore.ops.plans.dwg.records import (CLASSES, HARD_OWN, HARD_PTR, SOFT_PTR, T, EntCommon, EntityPlan,
                                           RecordWriter)
from pdfcore.ops.plans.dwg.wheader import header_getter, lineweight_index, write_header

__all__ = ["write_dwg", "WriteReport", "WRITABLE_VERSIONS"]

WRITABLE_VERSIONS = ("R2000", "AC1015")
CODEPAGE_1252 = 30  # §3.2.5 code page number of ANSI_1252
APP_VERSION, APP_MAINT = 23, 0  # "AC1015 = 23" (chapter 27 version numbering)
FILE_SENTINEL = bytes([0x95, 0xA0, 0x4E, 0x28, 0x99, 0x82, 0x1A, 0xE5, 0x5E, 0x41, 0xE0, 0x5F, 0x9D, 0x3A, 0x4D, 0x00])
END_HEADER = bytes([0x30, 0x84, 0xE0, 0xDC, 0x02, 0x21, 0xC7, 0x56, 0xA0, 0x83, 0x97, 0x47, 0xB1, 0x92, 0xCC, 0xA0])
END_CLASSES = bytes([0x72, 0x5E, 0x3B, 0x47, 0x3B, 0x56, 0x07, 0x3A, 0x3F, 0x23, 0x0B, 0xA0, 0x18, 0x30, 0x49, 0x75])
PREVIEW_START = bytes([0x1F, 0x25, 0x6D, 0x07, 0xD4, 0x36, 0x28, 0x28, 0x9D, 0x57, 0xCA, 0x3F, 0x9D, 0x44, 0x10, 0x2B])
PREVIEW_END = bytes([0xE0, 0xDA, 0x92, 0xF8, 0x2B, 0xC9, 0xD7, 0xD7, 0x62, 0xA8, 0x35, 0xC0, 0x62, 0xBB, 0xEF, 0xD4])
SECOND_START = bytes([0xD4, 0x7B, 0x21, 0xCE, 0x28, 0x93, 0x9F, 0xBF, 0x53, 0x24, 0x40, 0x09, 0x12, 0x3C, 0xAA, 0x01])
SECOND_END = bytes([0x2B, 0x84, 0xDE, 0x31, 0xD7, 0x6C, 0x60, 0x40, 0xAC, 0xDB, 0xBF, 0xF6, 0xED, 0xC3, 0x55, 0xFE])
MAP_SECTION_MAX = 2032  # §23.1 "each section is cut off at a maximum length of 2032"
_LOCATOR_CRC_XOR = {3: 0xA598, 4: 0x8101, 5: 0x3CC4, 6: 0x8461}  # §3.2.6
_RAD = math.pi / 180.0
_MAX_DEPTH = 4


@dataclass
class WriteReport:
    """What :func:`write_dwg` wrote. ``entities`` counts written DWG entities by DXF type (block contents and
    attributes included); ``exploded`` counts source entities written as primitives, ``skipped`` the ones
    dropped; ``text`` counts code-page substitutions (``escaped`` as ``\\U+XXXX``, ``replaced`` by ``?``,
    ``truncated`` bytes)."""

    path: str
    version: str = "AC1015"
    objects: int = 0
    bytes: int = 0
    entities: Counter = field(default_factory=Counter)
    exploded: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    text: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        ents = ", ".join(f"{k} {v}" for k, v in sorted(self.entities.items())) or "none"
        exp = ", ".join(f"{k} {v}" for k, v in sorted(self.exploded.items())) or "none"
        skip = ", ".join(f"{k} {v}" for k, v in sorted(self.skipped.items())) or "none"
        txt = ", ".join(f"{k} {v}" for k, v in sorted(self.text.items()) if v) or "none"
        return (f"{Path(self.path).name}: R2000 ({self.version}), {self.objects} objects, {self.bytes} bytes, "
                f"{sum(self.entities.values())} entities [{ents}]; exploded: {exp}; skipped: {skip}; "
                f"text substitutions: {txt}; {len(self.warnings)} warnings; {self.seconds:.2f}s")


def write_dwg(doc, path: str | Path, version: str = "R2000") -> WriteReport:
    """Write the ezdxf ``doc`` as a DWG file at ``path``. Only R2000 (AC1015) is supported; R2000 is read
    by every AutoCAD since 2000 and by all DWG-capable programs. Returns a :class:`WriteReport`."""
    if str(version).upper() not in WRITABLE_VERSIONS:
        raise ValueError(f"native DWG writer supports R2000 (AC1015) only, not {version!r}")
    t0 = time.perf_counter()
    p = Path(path)
    w = _Writer(doc, str(p))
    _TLS.nonfinite = 0
    data = w.build()
    if _TLS.nonfinite:
        w.rep.warnings.append(f"{_TLS.nonfinite} non-finite coordinate/value(s) (NaN/inf) written as 0")
    p.write_bytes(data)
    rep = w.rep
    rep.bytes = len(data)
    rep.seconds = time.perf_counter() - t0
    return rep


# ====================================================================== helpers
_TLS = threading.local()
# classic linetypes missing from ezdxf's standard set (total length, then dashes; drawing units)
_EXTRA_LINETYPES = [
    ("HIDDEN", "Hidden __ __ __ __ __ __ __ __ __ __ __ __ __ __", [0.375, 0.25, -0.125]),
    ("HIDDEN2", "Hidden (.5x) _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _", [0.1875, 0.125, -0.0625]),
    ("HIDDENX2", "Hidden (2x) ____ ____ ____ ____ ____ ____ ____ ", [0.75, 0.5, -0.25]),
]


def _f(v: Any, default: float = 0.0) -> float:
    """A finite float; NaN/inf (which no CAD program accepts) become ``default`` and are counted."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if math.isfinite(x):
        return x
    _TLS.nonfinite = getattr(_TLS, "nonfinite", 0) + 1
    return default


def _v3(p: Any, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    if p is None:
        return tuple(default)  # type: ignore[return-value]
    try:
        seq = list(p)
    except TypeError:
        return tuple(default)  # type: ignore[return-value]
    seq = [_f(c) for c in seq[:3]]
    while len(seq) < 3:
        seq.append(0.0)
    return (seq[0], seq[1], seq[2])


def _v2(p: Any) -> tuple[float, float]:
    x, y, _z = _v3(p)
    return (x, y)


def _ext(e) -> tuple[float, float, float]:
    ex = _v3(_sd(e).get("extrusion", (0.0, 0.0, 1.0)), (0.0, 0.0, 1.0))
    return ex if any(ex) else (0.0, 0.0, 1.0)


class _SafeDxf:
    """``e.dxf`` view whose ``get`` returns the default for attributes the entity type does not support
    (ezdxf raises for those, e.g. ``color`` on a BLOCK entity)."""

    __slots__ = ("_d",)

    def __init__(self, d) -> None:
        self._d = d

    def get(self, name: str, default: Any = None) -> Any:
        try:
            return self._d.get(name, default)
        except Exception:
            return default

    def hasattr(self, name: str) -> bool:
        try:
            return bool(self._d.hasattr(name))
        except Exception:
            return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._d, name)


def _sd(e) -> _SafeDxf:
    return _SafeDxf(e.dxf)


_ACI_RGB: list[tuple[int, int, int]] | None = None


def nearest_aci(rgb: tuple[int, int, int]) -> int:
    """Closest AutoCAD Color Index (1–255) to a true colour (R2000 has no true colour, §2.11)."""
    global _ACI_RGB
    if _ACI_RGB is None:
        from ezdxf.colors import DXF_DEFAULT_COLORS, int2rgb

        _ACI_RGB = [tuple(int2rgb(c)) for c in DXF_DEFAULT_COLORS]  # type: ignore[misc]
    r, g, b = rgb
    best, bd = 7, 1 << 30
    for i in range(1, 256):
        cr, cg, cb = _ACI_RGB[i]
        d = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
        if d < bd:
            best, bd = i, d
    return best


# ====================================================================== the writer
class _Writer:
    def __init__(self, doc, path: str) -> None:
        self.doc = doc
        self.rep = WriteReport(path)
        self.stats = self.rep.text
        self.codec = "cp1252"
        self.rw = RecordWriter(self.codec, self.stats)
        self.next = 1
        self.layers: dict[str, int] = {}      # upper-case name -> handle
        self.ltypes: dict[str, int] = {}
        self.styles: dict[str, int] = {}
        self.dimstyles: dict[str, int] = {}
        self.blocks: dict[str, int] = {}      # block name (upper) -> BLOCK_HEADER handle
        self.r: dict[str, int] = {}           # fixed objects for the header
        self.approx_colors = 0
        self.no_dim_block = 0
        self.vp_headers: list[tuple[int, int]] = []   # (VP_ENT_HDR handle, VIEWPORT handle)

    def alloc(self) -> int:
        h = self.next
        self.next += 1
        return h

    def warn(self, msg: str) -> None:
        if len(self.rep.warnings) < 200:
            self.rep.warnings.append(msg)

    # ------------------------------------------------------------------ top level
    def build(self) -> bytes:
        r = self.r
        for key in ("BLOCK_CONTROL", "LAYER_CONTROL", "STYLE_CONTROL", "LTYPE_CONTROL", "VIEW_CONTROL",
                    "UCS_CONTROL", "VPORT_CONTROL", "APPID_CONTROL", "DIMSTYLE_CONTROL", "DICT_NAMED",
                    "DICT_GROUP", "DICT_PLOTSTYLES", "PLACEHOLDER", "LTYPE_BYBLOCK", "LTYPE_BYLAYER",
                    "LTYPE_CONTINUOUS", "DICT_MLINESTYLE", "CMLSTYLE", "DICT_PLOTSETTINGS", "DICT_LAYOUTS",
                    "VP_ENT_HDR_CONTROL"):
            r[key] = self.alloc()
        self.ltypes = {"BYBLOCK": r["LTYPE_BYBLOCK"], "BYLAYER": r["LTYPE_BYLAYER"],
                       "CONTINUOUS": r["LTYPE_CONTINUOUS"]}
        lt_rows = self._plan_ltypes()
        layer_rows = self._plan_layers()
        style_rows = self._plan_styles()
        appid_rows = self._plan_appids()
        dim_rows = self._plan_dimstyles()
        r["VPORT_ACTIVE"] = self.alloc()
        layouts, blocks = self._plan_blocks()
        # entities (handles allocated in drawing order, children right after their parent)
        for b in layouts + blocks:
            b["plans"] = self._plan_entities(b["entities"], b)
        self._header_refs()
        # ---- encode
        self._enc_tables(lt_rows, layer_rows, style_rows, appid_rows, dim_rows)
        self._enc_objects(layouts)
        for b in layouts + blocks:
            self._enc_block(b)
        # BLOCK CONTROL (§20.4.51): ordinary blocks (and extra layout blocks) soft-owned, then *MODEL_SPACE
        # and *PAPER_SPACE hard-owned
        fixed = {r["MODEL_SPACE"], r["PAPER_SPACE"]}
        owned = [b["bh"] for b in layouts + blocks if b["bh"] not in fixed]
        self.rw.control("BLOCK_CONTROL", r["BLOCK_CONTROL"], owned, hard=[r["MODEL_SPACE"], r["PAPER_SPACE"]])
        t = self.stats
        if t.get("escaped") or t.get("replaced") or t.get("truncated"):
            self.warn(f"text outside code page 1252: {t.get('escaped', 0)} character(s) written as \\U+XXXX, "
                      f"{t.get('replaced', 0)} replaced by '?', {t.get('truncated', 0)} byte(s) truncated")
        if self.no_dim_block:
            self.warn(f"{self.no_dim_block} DIMENSION(s) without a geometry block written with a NULL block "
                      "reference (CAD programs regenerate the graphics)")
        if self.approx_colors:
            self.warn(f"{self.approx_colors} true colours mapped to the nearest ACI colour (R2000 has no true colour)")
        self.rep.objects = len(self.rw.out)
        return self._assemble()

    # ------------------------------------------------------------------ tables (pass 1: handles)
    def _plan_ltypes(self) -> list[tuple]:
        rows = []
        for lt in self.doc.linetypes:
            name = lt.dxf.name
            key = name.upper()
            if key in self.ltypes:
                continue
            total, dashes, complex_ = 0.0, [], False
            try:
                for tag in lt.pattern_tags.tags:
                    if tag.code == 40:
                        total = _f(tag.value)
                    elif tag.code == 49:
                        dashes.append(_f(tag.value))
                    elif tag.code == 74 and tag.value:
                        complex_ = True
            except AttributeError:
                pass
            if complex_:
                self.warn(f"linetype {name!r}: text/shape elements dropped (simple dashes kept)")
            if len(dashes) > 255:
                dashes = dashes[:255]
            if not total and dashes:
                total = sum(abs(x) for x in dashes)
            h = self.alloc()
            self.ltypes[key] = h
            rows.append((h, name, lt.dxf.get("description", "") or "", total, dashes))
        # linetypes referenced by layers/entities but missing from the table: standard definitions
        # (ezdxf's ANSI set) when the name is a standard one, else they fall back to CONTINUOUS
        std = None
        for name in self._scan_refs()[1]:
            key = name.upper()
            if not name or key in self.ltypes:
                continue
            if std is None:
                from ezdxf.tools.standards import linetypes as _std_lt

                std = {n.upper(): (n, d, pat) for n, d, pat in _std_lt()}
                for n, d, pat in _EXTRA_LINETYPES:
                    std.setdefault(n.upper(), (n, d, pat))
            if key in std:
                _n, desc, pat = std[key]
                h = self.alloc()
                self.ltypes[key] = h
                rows.append((h, name, desc, _f(pat[0]), [_f(x) for x in pat[1:]]))
            else:
                self.warn(f"linetype {name!r} is not defined: written as CONTINUOUS")
        return rows

    def _scan_refs(self) -> tuple[list[str], list[str]]:
        """Layer and linetype names used by the layer table and all entities (cached)."""
        cached = getattr(self, "_refs", None)
        if cached is not None:
            return cached
        layers: dict[str, None] = {}
        ltypes: dict[str, None] = {}
        for lay in self.doc.layers:
            ltypes.setdefault(str(lay.dxf.get("linetype", "Continuous")), None)
        for blk in self.doc.blocks:
            for e in blk:
                d = _sd(e)
                layers.setdefault(str(d.get("layer", "0")), None)
                ltypes.setdefault(str(d.get("linetype", "BYLAYER")), None)
        self._refs = (list(layers), list(ltypes))
        return self._refs

    def _plan_layers(self) -> list[tuple]:
        rows = []
        names = [lay.dxf.name for lay in self.doc.layers]
        if "0" not in names:
            names.insert(0, "0")
        names.sort(key=lambda n: n != "0")
        seen = set()
        for name in names:
            key = name.upper()
            if key in seen or not name:
                continue
            seen.add(key)
            h = self.alloc()
            self.layers[key] = h
            r_ = self.r
            if key == "0":
                r_["LAYER0"] = h
            rows.append((h, name))
        # layers used by entities but missing from the table
        for name in self._entity_layer_names():
            if name and name.upper() not in self.layers:
                h = self.alloc()
                self.layers[name.upper()] = h
                rows.append((h, name))
        return rows

    def _entity_layer_names(self) -> list[str]:
        return self._scan_refs()[0]

    def _plan_styles(self) -> list[tuple]:
        rows = []
        names = []
        for st in self.doc.styles:
            if st.dxf.name:
                names.append(st)
        if not any(st.dxf.name.upper() == "STANDARD" for st in names):
            rows.append((self._style_handle("Standard"), None))
        for st in names:
            key = st.dxf.name.upper()
            if key in self.styles:
                continue
            rows.append((self._style_handle(st.dxf.name), st))
        rows.sort(key=lambda row: row[0])
        return rows

    def _style_handle(self, name: str) -> int:
        h = self.alloc()
        self.styles[name.upper()] = h
        return h

    def _plan_appids(self) -> list[tuple]:
        rows = []
        names = ["ACAD"] + [a.dxf.name for a in self.doc.appids]
        seen = set()
        for n in names:
            if not n or n.upper() in seen:
                continue
            seen.add(n.upper())
            rows.append((self.alloc(), n))
        return rows

    def _plan_dimstyles(self) -> list[tuple]:
        rows = []
        styles = {d.dxf.name.upper(): d for d in self.doc.dimstyles if d.dxf.name}
        order = ["STANDARD"] + [k for k in styles if k != "STANDARD"]
        for k in order:
            d = styles.get(k)
            name = d.dxf.name if d is not None else "Standard"
            h = self.alloc()
            self.dimstyles[k] = h
            rows.append((h, name, d))
        return rows

    # ------------------------------------------------------------------ blocks & layouts (pass 1)
    def _plan_blocks(self) -> tuple[list[dict], list[dict]]:
        doc = self.doc
        r = self.r
        layouts: list[dict] = []
        msp = doc.modelspace()
        layouts.append(self._new_block("*Model_Space", "*Model_Space", msp, mode=2, layout=msp))
        r["MODEL_SPACE"] = layouts[0]["bh"]
        try:
            active = doc.layouts.active_layout()
        except Exception:
            active = None
        paper = []
        for name in doc.layouts.names_in_taborder():
            if name == "Model":
                continue
            lay = doc.layouts.get(name)
            paper.append(lay)
        if active is not None:
            paper.sort(key=lambda lay: lay is not active)
        n_other = 0
        for i, lay in enumerate(paper):
            if i == 0:
                b = self._new_block("*Paper_Space", "*Paper_Space", lay, mode=1, layout=lay)
                r["PAPER_SPACE"] = b["bh"]
            else:
                b = self._new_block("*Paper_Space", f"*Paper_Space{n_other}", lay, mode=0, layout=lay)
                n_other += 1
            layouts.append(b)
        if "PAPER_SPACE" not in r:  # a document without any paper space layout: AutoCAD needs one
            b = self._new_block("*Paper_Space", "*Paper_Space", [], mode=1, layout=None)
            r["PAPER_SPACE"] = b["bh"]
            layouts.append(b)
        blocks = []
        for blk in doc.blocks:
            if blk.is_any_layout:
                continue
            name = blk.name
            if not name or name.upper() in self.blocks:
                continue
            flags = blk.block.dxf.get("flags", 0) if blk.block is not None else 0
            anonymous = bool(flags & 1) or name.startswith("*")
            hdr_name = name
            if name.startswith("*") and len(name) > 1:
                # anonymous block records store the bare prefix ("*D", "*U" …); the BLOCK entity keeps the
                # full name (as in AutoCAD-saved files)
                i = 2
                while i < len(name) and not name[i].isdigit():
                    i += 1
                if name[i:].isdigit():
                    hdr_name = name[:i]
            b = self._new_block(hdr_name, name, blk, mode=0, layout=None, block=blk, anonymous=anonymous)
            blocks.append(b)
        return layouts, blocks

    def _new_block(self, hdr_name: str, blk_name: str, entities, mode: int, layout, block=None,
                   anonymous: bool = False) -> dict:
        b = {"name": hdr_name, "blk_name": blk_name, "entities": entities, "mode": mode, "layout": layout,
             "block": block, "anonymous": anonymous, "bh": self.alloc(), "blk": self.alloc(), "end": self.alloc()}
        b["lo"] = self.alloc() if layout is not None or blk_name.startswith("*Paper_Space") \
            or blk_name == "*Model_Space" else 0
        self.blocks[blk_name.upper()] = b["bh"]
        return b

    def _header_refs(self) -> None:
        r = self.r
        hv = self.doc.header
        r["CLAYER"] = self.layers.get(str(hv.get("$CLAYER", "0")).upper(), r["LAYER0"])
        r["TEXTSTYLE"] = self.styles.get(str(hv.get("$TEXTSTYLE", "Standard")).upper(), self.styles["STANDARD"])
        r["DIMTXSTY"] = self.styles["STANDARD"]
        celt = str(hv.get("$CELTYPE", "ByLayer")).upper()
        r["CELTYPE"] = self.ltypes.get(celt, r["LTYPE_BYLAYER"])
        if self.vp_headers:
            r["VP_ENT_HDR_CURRENT"] = self.vp_headers[-1][0]
        r["DIMSTYLE"] = self.dimstyles.get(str(hv.get("$DIMSTYLE", "Standard")).upper(), self.dimstyles["STANDARD"])

    # ------------------------------------------------------------------ entities (pass 1)
    def _common(self, e) -> EntCommon:
        dxf = _SafeDxf(e.dxf)
        layer = self.layers.get(str(dxf.get("layer", "0")).upper(), self.r["LAYER0"])
        color = dxf.get("color", 256)
        try:
            color = int(color)
        except (TypeError, ValueError):
            color = 256
        if not 0 <= color <= 257:
            color = 256
        if color == 257:
            color = 256
        tc = dxf.get("true_color", None)
        if tc is not None and not dxf.hasattr("color"):
            from ezdxf.colors import int2rgb

            color = nearest_aci(tuple(int2rgb(int(tc))))  # type: ignore[arg-type]
            self.approx_colors += 1
        lt = str(dxf.get("linetype", "BYLAYER")).upper()
        flags, lth = 0, 0
        if lt == "BYLAYER":
            flags = 0
        elif lt == "BYBLOCK":
            flags = 1
        elif lt == "CONTINUOUS":
            flags = 2
        elif lt in self.ltypes:
            flags, lth = 3, self.ltypes[lt]
        lw = dxf.get("lineweight", -1)
        try:
            lw = int(lw)
        except (TypeError, ValueError):
            lw = -1
        return EntCommon(layer=layer, color=color, ltype_flags=flags, ltype=lth,
                         ltscale=_f(dxf.get("ltscale", 1.0), 1.0) or 1.0, lineweight=lineweight_index(lw),
                         invisible=1 if dxf.get("invisible", 0) else 0)

    def _plan_entities(self, entities: Iterable, b: dict, depth: int = 0) -> list[EntityPlan]:
        out: list[EntityPlan] = []
        for e in entities:
            try:
                plans = self._plan(e, b, depth)
            except Exception as exc:  # keep going: one bad entity must not lose the drawing
                t = e.dxftype()
                self.rep.skipped[t] += 1
                self.warn(f"{t}: {type(exc).__name__}: {exc}")
                continue
            out.extend(plans)
        return out

    def _count(self, p: EntityPlan) -> None:
        name = p.kind
        self.rep.entities[name] += 1
        for c in p.children:
            if c.kind != "SEQEND" and not c.kind.startswith("VERTEX"):
                self.rep.entities[c.kind] += 1

    def _plan(self, e, b: dict, depth: int) -> list[EntityPlan]:
        t = e.dxftype()
        fn = getattr(self, "p_" + t.lower(), None)
        if fn is not None:
            plan = fn(e, b)
            if plan is not None:
                self._count(plan)
                return [plan]
            return []
        if depth < _MAX_DEPTH and hasattr(e, "virtual_entities"):
            try:
                parts = list(e.virtual_entities())
            except Exception as exc:
                parts = []
                self.warn(f"{t}: cannot explode ({type(exc).__name__}: {exc})")
            if parts:
                self.rep.exploded[t] += 1
                return self._plan_entities(parts, b, depth + 1)
        self.rep.skipped[t] += 1
        return []

    def _ent(self, kind: str, e, data, handles=None, children=None) -> EntityPlan:
        label = kind.split("_")[0] if kind.startswith(("POLYLINE", "DIMENSION_")) else kind
        return EntityPlan(self.alloc(), T[kind], label, self._common(e), data or (lambda d: None),
                          handles or (lambda w: None), children or [])

    # --- one method per DXF type ---------------------------------------
    def p_line(self, e, b):
        s, en = _v3(e.dxf.start), _v3(e.dxf.end)
        th, ex = _f(_sd(e).get("thickness", 0.0)), _ext(e)

        def data(d: BitWriter) -> None:  # §20.4.21 (R2000)
            zz = s[2] == 0.0 and en[2] == 0.0
            d.b(zz)
            d.rd(s[0])
            d.dd(en[0], s[0])
            d.rd(s[1])
            d.dd(en[1], s[1])
            if not zz:
                d.rd(s[2])
                d.dd(en[2], s[2])
            d.bt(th)
            d.be(ex)

        return self._ent("LINE", e, data)

    def p_point(self, e, b):
        p, th, ex = _v3(e.dxf.location), _f(_sd(e).get("thickness", 0.0)), _ext(e)
        ang = _f(_sd(e).get("angle", 0.0)) * _RAD

        def data(d: BitWriter) -> None:  # §20.4.31
            d.bd3(p)
            d.bt(th)
            d.be(ex)
            d.bd(ang)

        return self._ent("POINT", e, data)

    def p_circle(self, e, b):
        c, rad, th, ex = _v3(e.dxf.center), _f(e.dxf.radius, 1.0), _f(_sd(e).get("thickness", 0.0)), _ext(e)

        def data(d: BitWriter) -> None:  # §20.4.20
            d.bd3(c)
            d.bd(rad)
            d.bt(th)
            d.be(ex)

        return self._ent("CIRCLE", e, data)

    def p_arc(self, e, b):
        c, rad, th, ex = _v3(e.dxf.center), _f(e.dxf.radius, 1.0), _f(_sd(e).get("thickness", 0.0)), _ext(e)
        sa, ea = _f(e.dxf.start_angle) * _RAD, _f(e.dxf.end_angle) * _RAD

        def data(d: BitWriter) -> None:  # §20.4.18
            d.bd3(c)
            d.bd(rad)
            d.bt(th)
            d.be(ex)
            d.bd(sa)
            d.bd(ea)

        return self._ent("ARC", e, data)

    def p_ellipse(self, e, b):
        c, maj, ex = _v3(e.dxf.center), _v3(e.dxf.major_axis, (1.0, 0.0, 0.0)), _ext(e)
        ratio = min(max(_f(_sd(e).get("ratio", 1.0), 1.0), 1e-6), 1.0)
        sp, ep = _f(_sd(e).get("start_param", 0.0)), _f(_sd(e).get("end_param", math.tau), math.tau)

        def data(d: BitWriter) -> None:  # §20.4.39
            d.bd3(c)
            d.bd3(maj)
            d.bd3(ex)
            d.bd(ratio)
            d.bd(sp)
            d.bd(ep)

        return self._ent("ELLIPSE", e, data)

    def _quad(self, e) -> list[tuple[float, float, float]]:
        pts = [_v3(_sd(e).get(f"vtx{i}", None)) if e.dxf.hasattr(f"vtx{i}") else None for i in range(4)]
        if pts[0] is None:
            pts[0] = (0.0, 0.0, 0.0)
        for i in range(1, 4):
            if pts[i] is None:
                pts[i] = pts[i - 1]
        return pts  # type: ignore[return-value]

    def p_solid(self, e, b, kind: str = "SOLID"):
        pts, th, ex = self._quad(e), _f(_sd(e).get("thickness", 0.0)), _ext(e)

        def data(d: BitWriter) -> None:  # §20.4.35 / §20.4.36
            d.bt(th)
            d.bd(pts[0][2])
            for p in pts:
                d.rd2(p)
            d.be(ex)

        return self._ent(kind, e, data)

    def p_trace(self, e, b):
        return self.p_solid(e, b, "TRACE")

    def p_3dface(self, e, b):
        pts = self._quad(e)
        inv = int(_sd(e).get("invisible_edges", 0)) & 15

        def data(d: BitWriter) -> None:  # §20.4.32 (R2000: B no-flags, B z-is-zero, RD/DD corners)
            d.b(inv == 0)
            zz = pts[0][2] == 0.0
            d.b(zz)
            d.rd(pts[0][0])
            d.rd(pts[0][1])
            if not zz:
                d.rd(pts[0][2])
            prev = pts[0]
            for p in pts[1:]:
                d.dd(p[0], prev[0])
                d.dd(p[1], prev[1])
                d.dd(p[2], prev[2])
                prev = p
            if inv:
                d.bs(inv)

        return self._ent("3DFACE", e, data)

    def p_ray(self, e, b, kind: str = "RAY"):
        p, v = _v3(e.dxf.start), _v3(e.dxf.unit_vector, (1.0, 0.0, 0.0))

        def data(d: BitWriter) -> None:  # §20.4.42 / §20.4.43
            d.bd3(p)
            d.bd3(v)

        return self._ent(kind, e, data)

    def p_xline(self, e, b):
        return self.p_ray(e, b, "XLINE")

    def p_lwpolyline(self, e, b):
        rows = [tuple(_f(x) for x in row) for row in e.get_points("xyseb")]
        n = len(rows)
        const = _f(_sd(e).get("const_width", 0.0))
        elev, th, ex = _f(_sd(e).get("elevation", 0.0)), _f(_sd(e).get("thickness", 0.0)), _ext(e)
        has_bulge = any(r[4] for r in rows)
        has_width = any(r[2] or r[3] for r in rows) and not all(r[2] == const and r[3] == const for r in rows)
        flags = 0
        if const and not has_width:
            flags |= 4
        if elev:
            flags |= 8
        if th:
            flags |= 2
        if ex != (0.0, 0.0, 1.0):
            flags |= 1
        if has_bulge:
            flags |= 16
        if has_width:
            flags |= 32
        if e.closed:
            flags |= 512
        if _sd(e).get("flags", 0) & 128:
            flags |= 256

        def data(d: BitWriter) -> None:  # §20.4.85
            d.bs(flags)
            if flags & 4:
                d.bd(const)
            if flags & 8:
                d.bd(elev)
            if flags & 2:
                d.bd(th)
            if flags & 1:
                d.bd3(ex)
            d.bl(n)
            if flags & 16:
                d.bl(n)
            if flags & 32:
                d.bl(n)
            if n:
                x, y = rows[0][0], rows[0][1]
                d.rd(x)
                d.rd(y)
                for row in rows[1:]:
                    d.dd(row[0], x)
                    d.dd(row[1], y)
                    x, y = row[0], row[1]
            if flags & 16:
                for row in rows:
                    d.bd(row[4])
            if flags & 32:
                for row in rows:
                    d.bd(row[2])
                    d.bd(row[3])

        return self._ent("LWPOLYLINE", e, data)

    def p_polyline(self, e, b):
        mode = e.get_mode()
        verts = list(e.vertices)
        flags = int(_sd(e).get("flags", 0))
        if mode == "AcDb2dPolyline":
            return self._poly2d(e, verts, flags)
        if mode == "AcDb3dPolyline":
            return self._poly3d(e, verts, flags)
        if mode == "AcDbPolyFaceMesh":
            return self._pface(e, verts)
        return self._mesh(e, verts, flags)

    def _vertex_plans(self, verts, kind: str, data_of) -> list[EntityPlan]:
        out = []
        for v in verts:
            out.append(EntityPlan(self.alloc(), T[kind], kind, self._common(v), data_of(v)))
        return out

    def _poly2d(self, e, verts, flags):
        sw, ew = _f(_sd(e).get("default_start_width", 0.0)), _f(_sd(e).get("default_end_width", 0.0))
        th, elev, ex = _f(_sd(e).get("thickness", 0.0)), _v3(_sd(e).get("elevation", (0, 0, 0)))[2], _ext(e)
        curve = int(_sd(e).get("smooth_type", 0))
        parent = self._ent("POLYLINE_2D", e, None)

        def data(d: BitWriter) -> None:  # §20.4.16
            d.bs(flags)
            d.bs(curve)
            d.bd(sw)
            d.bd(ew)
            d.bt(th)
            d.bd(elev)
            d.be(ex)

        def vdata(v):
            loc = _v3(v.dxf.location)
            vf = int(_sd(v).get("flags", 0)) & 0xFF
            s, en = _f(_sd(v).get("start_width", sw)), _f(_sd(v).get("end_width", ew))
            bulge, tan = _f(_sd(v).get("bulge", 0.0)), _f(_sd(v).get("tangent", 0.0)) * _RAD

            def f(d: BitWriter) -> None:  # §20.4.11
                d.rc(vf)
                d.bd3((loc[0], loc[1], 0.0))
                if s == en and s != 0.0:
                    d.bd(-abs(s))
                else:
                    d.bd(s)
                    d.bd(en)
                d.bd(bulge)
                d.bd(tan)

            return f

        parent.data = data
        return self._finish_seq(parent, e, self._vertex_plans(verts, "VERTEX_2D", vdata))

    def _finish_seq(self, parent: EntityPlan, e, kids: list[EntityPlan], extra=None) -> EntityPlan:
        seq = EntityPlan(self.alloc(), T["SEQEND"], "SEQEND", self._common(e))
        first = kids[0].handle if kids else 0
        last = kids[-1].handle if kids else 0

        def handles(w: BitWriter) -> None:
            if extra is not None:
                extra(w)
            w.h(SOFT_PTR, first)
            w.h(SOFT_PTR, last)
            w.h(HARD_OWN, seq.handle)

        parent.handles = handles
        parent.children = kids + [seq]
        return parent

    def _poly3d(self, e, verts, flags):
        smooth = int(_sd(e).get("smooth_type", 0))
        sflags = (1 if smooth == 5 else 2 if smooth == 6 else 0) if flags & 4 else 0
        cflags = 1 if flags & 1 else 0
        parent = self._ent("POLYLINE_3D", e, None)

        def data(d: BitWriter) -> None:  # §20.4.17
            d.rc(sflags)
            d.rc(cflags)

        def vdata(v):
            loc, vf = _v3(v.dxf.location), int(_sd(v).get("flags", 32)) & 0xFF

            def f(d: BitWriter) -> None:  # §20.4.12
                d.rc(vf)
                d.bd3(loc)

            return f

        parent.data = data
        return self._finish_seq(parent, e, self._vertex_plans(verts, "VERTEX_3D", vdata))

    def _pface(self, e, verts):
        locs = [v for v in verts if not (int(_sd(v).get("flags", 0)) & 128 and not int(_sd(v).get("flags", 0)) & 64)]
        faces = [v for v in verts if v not in locs]
        parent = self._ent("POLYLINE_PFACE", e, None)

        def data(d: BitWriter) -> None:  # §20.4.33
            d.bs(len(locs))
            d.bs(len(faces))

        def vdata(v):
            loc, vf = _v3(v.dxf.location), int(_sd(v).get("flags", 192)) & 0xFF

            def f(d: BitWriter) -> None:  # §20.4.14
                d.rc(vf)
                d.bd3(loc)

            return f

        def fdata(v):
            idx = [int(_sd(v).get(f"vtx{i}", 0)) for i in range(4)]

            def f(d: BitWriter) -> None:  # §20.4.15
                for i in idx:
                    d.bs(i)

            return f

        kids = self._vertex_plans(locs, "VERTEX_PFACE", vdata) + self._vertex_plans(faces, "VERTEX_PFACE_FACE", fdata)
        parent.data = data
        return self._finish_seq(parent, e, kids)

    def _mesh(self, e, verts, flags):
        m, n = int(_sd(e).get("m_count", 0)), int(_sd(e).get("n_count", 0))
        md, nd = int(_sd(e).get("m_smooth_density", 0)), int(_sd(e).get("n_smooth_density", 0))
        curve = int(_sd(e).get("smooth_type", 0))
        parent = self._ent("POLYLINE_MESH", e, None)

        def data(d: BitWriter) -> None:  # §20.4.34
            d.bs(flags)
            d.bs(curve)
            d.bs(m)
            d.bs(n)
            d.bs(md)
            d.bs(nd)

        def vdata(v):
            loc, vf = _v3(v.dxf.location), int(_sd(v).get("flags", 64)) & 0xFF

            def f(d: BitWriter) -> None:
                d.rc(vf)
                d.bd3(loc)

            return f

        parent.data = data
        return self._finish_seq(parent, e, self._vertex_plans(verts, "VERTEX_MESH", vdata))

    def p_spline(self, e, b):
        deg = int(_sd(e).get("degree", 3))
        flags = int(_sd(e).get("flags", 0))
        ctrl = [_v3(p) for p in e.control_points]
        knots = [_f(k) for k in e.knots]
        weights = [_f(w, 1.0) for w in e.weights]
        fit = [_v3(p) for p in e.fit_points]
        if ctrl and len(knots) != len(ctrl) + deg + 1:
            ctrl = []  # inconsistent knot vector: fall back to the fit points or skip
        if not ctrl and len(fit) < 2:
            raise ValueError("SPLINE without control points or fit points")
        rational = bool(flags & 4) or bool(weights)
        if rational and len(weights) != len(ctrl):
            weights = [1.0] * len(ctrl)
        st = _v3(_sd(e).get("start_tangent", (0.0, 0.0, 0.0)))
        en = _v3(_sd(e).get("end_tangent", (0.0, 0.0, 0.0)))
        ktol = _f(_sd(e).get("knot_tolerance", 1e-10), 1e-10)
        ctol = _f(_sd(e).get("control_point_tolerance", 1e-10), 1e-10)
        ftol = _f(_sd(e).get("fit_tolerance", 1e-10), 1e-10)

        def data(d: BitWriter) -> None:  # §20.4.40
            if ctrl:
                d.bl(1)
                d.bl(deg)
                d.b(rational)
                d.b(flags & 1)
                d.b(flags & 2)
                d.bd(ktol)
                d.bd(ctol)
                d.bl(len(knots))
                d.bl(len(ctrl))
                d.b(rational)
                for k in knots:
                    d.bd(k)
                for i, p in enumerate(ctrl):
                    d.bd3(p)
                    if rational:
                        d.bd(weights[i])
            else:
                d.bl(2)
                d.bl(deg)
                d.bd(ftol)
                d.bd3(st)
                d.bd3(en)
                d.bl(len(fit))
                for p in fit:
                    d.bd3(p)

        return self._ent("SPLINE", e, data)

    def p_helix(self, e, b):
        p = self.p_spline(e, b)
        p.kind = "SPLINE"
        return p

    # --- text -------------------------------------------------------------
    def _text_data(self, e, extra=None):
        """§20.4.3 TEXT data (R2000 DataFlags form); ``extra`` appends ATTRIB/ATTDEF fields."""
        dxf = _SafeDxf(e.dxf)
        ins = _v3(dxf.get("insert", (0, 0, 0)))
        has_align = dxf.hasattr("align_point") and (int(dxf.get("halign", 0)) or int(dxf.get("valign", 0)))
        al = _v3(dxf.get("align_point", ins)) if has_align else ins
        th, ex = _f(dxf.get("thickness", 0.0)), _ext(e)
        obl, rot = _f(dxf.get("oblique", 0.0)) * _RAD, _f(dxf.get("rotation", 0.0)) * _RAD
        hgt, wid = _f(dxf.get("height", 1.0), 1.0) or 1.0, _f(dxf.get("width", 1.0), 1.0) or 1.0
        gen, ha, va = int(dxf.get("text_generation_flag", 0)), int(dxf.get("halign", 0)), int(dxf.get("valign", 0))
        text = str(dxf.get("text", "") or "")
        fl = 0
        if ins[2] == 0.0:
            fl |= 1
        if not has_align:
            fl |= 2
        if obl == 0.0:
            fl |= 4
        if rot == 0.0:
            fl |= 8
        if wid == 1.0:
            fl |= 0x10
        if gen == 0:
            fl |= 0x20
        if ha == 0:
            fl |= 0x40
        if va == 0:
            fl |= 0x80

        def data(d: BitWriter) -> None:
            d.rc(fl)
            if not fl & 1:
                d.rd(ins[2])
            d.rd2(ins)
            if not fl & 2:
                d.dd(al[0], ins[0])
                d.dd(al[1], ins[1])
            d.be(ex)
            d.bt(th)
            if not fl & 4:
                d.rd(obl)
            if not fl & 8:
                d.rd(rot)
            d.rd(hgt)
            if not fl & 0x10:
                d.rd(wid)
            d.tv(text)
            if not fl & 0x20:
                d.bs(gen)
            if not fl & 0x40:
                d.bs(ha)
            if not fl & 0x80:
                d.bs(va)
            if extra is not None:
                extra(d)

        style = self.styles.get(str(dxf.get("style", "Standard")).upper(), self.styles["STANDARD"])
        return data, (lambda w: w.h(HARD_PTR, style))

    def p_text(self, e, b):
        data, hs = self._text_data(e)
        return self._ent("TEXT", e, data, hs)

    def _attrib_tail(self, e, prompt: bool):
        tag = str(_sd(e).get("tag", "") or "TAG")
        flags = int(_sd(e).get("flags", 0)) & 0xFF
        flen = int(_sd(e).get("field_length", 0))
        pr = str(_sd(e).get("prompt", "") or "")

        def extra(d: BitWriter) -> None:  # §20.4.4 / §20.4.5
            d.tv(tag)
            d.bs(flen)
            d.rc(flags)
            if prompt:
                d.tv(pr)

        return extra

    def p_attdef(self, e, b):
        data, hs = self._text_data(e, self._attrib_tail(e, True))
        return self._ent("ATTDEF", e, data, hs)

    def p_mtext(self, e, b):
        dxf = _SafeDxf(e.dxf)
        ins, ex = _v3(dxf.get("insert", (0, 0, 0))), _ext(e)
        if dxf.hasattr("text_direction"):
            xd = _v3(dxf.text_direction, (1.0, 0.0, 0.0))
        else:
            a = _f(dxf.get("rotation", 0.0)) * _RAD
            xd = (math.cos(a), math.sin(a), 0.0)
        ln = math.hypot(*xd) or 1.0
        xd = (xd[0] / ln, xd[1] / ln, xd[2] / ln)
        hgt = _f(dxf.get("char_height", 1.0), 1.0) or 1.0
        width = _f(dxf.get("width", 0.0))
        att = int(dxf.get("attachment_point", 1))
        att = att if 1 <= att <= 9 else 1
        flow = int(dxf.get("flow_direction", 1))
        flow = flow if flow in (1, 3, 5) else 1
        text = e.text or ""
        lines = max(1, text.count("\\P") + 1)
        ext_w = width or hgt * 0.7 * max(len(t) for t in text.split("\\P"))
        ext_h = hgt * (1.0 + 1.6667 * (lines - 1))
        ls_style = int(dxf.get("line_spacing_style", 1))
        ls_style = ls_style if ls_style in (1, 2) else 1
        ls = _f(dxf.get("line_spacing_factor", 1.0), 1.0) or 1.0
        style = self.styles.get(str(dxf.get("style", "Standard")).upper(), self.styles["STANDARD"])

        def data(d: BitWriter) -> None:  # §20.4.46 (R2000)
            d.bd3(ins)
            d.bd3(ex)
            d.bd3(xd)
            d.bd(width)
            d.bd(hgt)
            d.bs(att)
            d.bs(flow)
            d.bd(ext_h)
            d.bd(ext_w)
            d.tv(text)
            d.bs(ls_style)
            d.bd(ls)
            d.b(0)

        return self._ent("MTEXT", e, data, lambda w: w.h(HARD_PTR, style))

    # --- blocks -------------------------------------------------------------
    def p_insert(self, e, b):
        dxf = _SafeDxf(e.dxf)
        bh = self.blocks.get(str(dxf.get("name", "")).upper())
        if bh is None:
            raise ValueError(f"INSERT of undefined block {dxf.get('name')!r}")
        ins, ex = _v3(dxf.get("insert", (0, 0, 0))), _ext(e)
        sx, sy, sz = (_f(dxf.get(k, 1.0), 1.0) for k in ("xscale", "yscale", "zscale"))
        rot = _f(dxf.get("rotation", 0.0)) * _RAD
        cols, rows = int(dxf.get("column_count", 1)), int(dxf.get("row_count", 1))
        csp, rsp = _f(dxf.get("column_spacing", 0.0)), _f(dxf.get("row_spacing", 0.0))
        minsert = cols > 1 or rows > 1
        attribs = list(e.attribs)
        kind = "MINSERT" if minsert else "INSERT"
        parent = self._ent(kind, e, None)
        parent.kind = "INSERT"

        def data(d: BitWriter) -> None:  # §20.4.9 / §20.4.10
            d.bd3(ins)
            if sx == 1.0 and sy == 1.0 and sz == 1.0:
                d.bb(3)
            elif sx == 1.0:
                d.bb(1)
                d.dd(sy, 1.0)
                d.dd(sz, 1.0)
            elif sx == sy == sz:
                d.bb(2)
                d.rd(sx)
            else:
                d.bb(0)
                d.rd(sx)
                d.dd(sy, sx)
                d.dd(sz, sx)
            d.bd(rot)
            d.bd3(ex)
            d.b(bool(attribs))
            if minsert:
                d.bs(max(cols, 1))
                d.bs(max(rows, 1))
                d.bd(csp)
                d.bd(rsp)

        parent.data = data
        if not attribs:
            parent.handles = lambda w: w.h(HARD_PTR, bh)
            return parent
        kids = []
        for a in attribs:
            ad, ahs = self._text_data(a, self._attrib_tail(a, False))
            kids.append(EntityPlan(self.alloc(), T["ATTRIB"], "ATTRIB", self._common(a), ad, ahs))
        return self._finish_seq(parent, e, kids, extra=lambda w: w.h(HARD_PTR, bh))

    # --- hatch --------------------------------------------------------------
    def p_hatch(self, e, b):
        dxf = _SafeDxf(e.dxf)
        elev = _v3(dxf.get("elevation", (0, 0, 0)))[2]
        ex = _ext(e)
        solid = bool(dxf.get("solid_fill", 1))
        if e.has_gradient_data:
            solid = True
            self.warn("HATCH gradient written as solid fill (R2000 has no gradients)")
        name = "SOLID" if solid else str(dxf.get("pattern_name", "ANSI31") or "USER")
        paths = []
        any_pixel = False
        from ezdxf.entities.boundary_paths import EdgePath, PolylinePath

        for path in e.paths:
            pflag = int(getattr(path, "path_type_flags", 1))
            if isinstance(path, PolylinePath):
                pts = [(_f(v[0]), _f(v[1]), _f(v[2]) if len(v) > 2 else 0.0) for v in path.vertices]
                paths.append(("poly", pflag | 2, pts, bool(path.is_closed)))
            elif isinstance(path, EdgePath):
                edges = []
                for ed in path.edges:
                    et = ed.type.name if hasattr(ed.type, "name") else str(ed.type)
                    edges.append((et, ed))
                paths.append(("edges", pflag & ~2, edges, None))
            else:
                continue
            if paths[-1][1] & 4:
                any_pixel = True
        lines = []
        if not solid and e.pattern is not None:
            for pl in e.pattern.lines:
                lines.append((_f(pl.angle) * _RAD, _v2(pl.base_point), _v2(pl.offset),
                              [_f(x) for x in pl.dash_length_items]))
        style = int(dxf.get("hatch_style", 0))
        ptype = int(dxf.get("pattern_type", 1))
        pangle = _f(dxf.get("pattern_angle", 0.0)) * _RAD
        pscale = _f(dxf.get("pattern_scale", 1.0), 1.0)
        pdouble = int(dxf.get("pattern_double", 0))
        pixel = _f(dxf.get("pixel_size", 0.0))
        seeds = [_v2(s) for s in (e.seeds or [])]

        def data(d: BitWriter) -> None:  # §20.4.75 (R2000: no gradient block)
            d.bd(elev)
            d.bd3(ex)
            d.tv(name)
            d.b(solid)
            d.b(0)  # not associative (no boundary objects are written)
            d.bl(len(paths))
            for kind, pflag, items, closed in paths:
                d.bl(pflag)
                if kind == "poly":
                    bul = any(p[2] for p in items)
                    d.b(bul)
                    d.b(closed)
                    d.bl(len(items))
                    for x, y, bg in items:
                        d.rd2((x, y))
                        if bul:
                            d.bd(bg)
                else:
                    d.bl(len(items))
                    for et, ed in items:
                        _hatch_edge(d, et, ed)
                d.bl(0)  # boundary object handles
            d.bs(style)
            d.bs(ptype)
            if not solid:
                d.bd(pangle)
                d.bd(pscale)
                d.b(pdouble)
                d.bs(len(lines))
                for ang, base, off, dashes in lines:
                    d.bd(ang)
                    d.bd2(base)
                    d.bd2(off)
                    d.bs(len(dashes))
                    for x in dashes:
                        d.bd(x)
            if any_pixel:
                d.bd(pixel)
            d.bl(len(seeds))
            for s in seeds:
                d.rd2(s)

        return self._ent("HATCH", e, data)

    # --- dimensions -----------------------------------------------------------
    def p_dimension(self, e, b):
        dxf = _SafeDxf(e.dxf)
        dt = int(dxf.get("dimtype", 0))
        kind = dt & 7
        names = {0: "DIMENSION_LINEAR", 1: "DIMENSION_ALIGNED", 2: "DIMENSION_ANG2LN", 3: "DIMENSION_DIAMETER",
                 4: "DIMENSION_RADIUS", 5: "DIMENSION_ANG3PT", 6: "DIMENSION_ORDINATE"}
        if kind not in names:
            raise ValueError(f"dimension type {dt} not supported")
        blk = self.blocks.get(str(dxf.get("geometry", "") or "").upper(), 0)
        if not blk:  # AutoCAD files carry such dimensions too (e.g. inside dynamic blocks)
            self.no_dim_block += 1
        ds = self.dimstyles.get(str(dxf.get("dimstyle", "Standard")).upper(), self.dimstyles["STANDARD"])
        ex = _ext(e)
        tm = _v3(dxf.get("text_midpoint", (0, 0, 0)))
        flags1 = (0 if dt & 128 else 1) | (2 if dt & 32 else 0)
        text = str(dxf.get("text", "") or "")
        trot = _f(dxf.get("text_rotation", 0.0)) * _RAD
        hdir = _f(dxf.get("horizontal_direction", 0.0)) * _RAD
        att = int(dxf.get("attachment_point", 5))
        lss = int(dxf.get("line_spacing_style", 1))
        lsf = _f(dxf.get("line_spacing_factor", 1.0), 1.0)
        meas = _f(dxf.get("actual_measurement", 0.0))
        p12 = _v3(dxf.get("insert", (0, 0, 0)))
        p10, p13, p14, p15, p16 = (_v3(dxf.get(k, (0, 0, 0))) for k in
                                   ("defpoint", "defpoint2", "defpoint3", "defpoint4", "defpoint5"))
        oblique = _f(dxf.get("oblique_angle", 0.0)) * _RAD
        angle = _f(dxf.get("angle", 0.0)) * _RAD
        leader = _f(dxf.get("leader_length", 0.0))

        def data(d: BitWriter) -> None:  # §20.4.22 common dimension data, then §20.4.23–§20.4.29
            d.bd3(ex)
            d.rd2(tm)
            d.bd(tm[2])
            d.rc(flags1)
            d.tv(text)
            d.bd(trot)
            d.bd(hdir)
            d.bd3((1.0, 1.0, 1.0))
            d.bd(0.0)
            d.bs(att if 1 <= att <= 9 else 5)
            d.bs(lss if lss in (1, 2) else 1)
            d.bd(lsf)
            d.bd(meas)
            d.rd2(p12)
            if kind == 6:
                d.bd3(p10)
                d.bd3(p13)
                d.bd3(p14)
                d.rc(1 if dt & 64 else 0)
            elif kind == 0:
                d.bd3(p13)
                d.bd3(p14)
                d.bd3(p10)
                d.bd(oblique)
                d.bd(angle)
            elif kind == 1:
                d.bd3(p13)
                d.bd3(p14)
                d.bd3(p10)
                d.bd(oblique)
            elif kind == 5:
                d.bd3(p10)
                d.bd3(p13)
                d.bd3(p14)
                d.bd3(p15)
            elif kind == 2:
                d.rd2(p16)
                d.bd3(p13)
                d.bd3(p14)
                d.bd3(p15)
                d.bd3(p10)
            elif kind == 4:
                d.bd3(p10)
                d.bd3(p15)
                d.bd(leader)
            elif kind == 3:
                d.bd3(p15)
                d.bd3(p10)
                d.bd(leader)

        def handles(w: BitWriter) -> None:
            w.h(HARD_PTR, ds)
            w.h(HARD_PTR, blk)

        return self._ent(names[kind], e, data, handles)

    def p_tolerance(self, e, b):
        dxf = _SafeDxf(e.dxf)
        ins, xd, ex = _v3(dxf.get("insert", (0, 0, 0))), _v3(dxf.get("x_axis_vector", (1, 0, 0))), _ext(e)
        text = str(dxf.get("content", "") or "")
        ds = self.dimstyles.get(str(dxf.get("dimstyle", "Standard")).upper(), self.dimstyles["STANDARD"])

        def data(d: BitWriter) -> None:  # §20.4.49 (R2000: no R13/R14 unknowns)
            d.bd3(ins)
            d.bd3(xd)
            d.bd3(ex)
            d.tv(text)

        return self._ent("TOLERANCE", e, data, lambda w: w.h(HARD_PTR, ds))

    # --- viewport -------------------------------------------------------------
    def p_viewport(self, e, b):
        if b["layout"] is None or b["mode"] == 2:
            raise ValueError("VIEWPORT outside paper space")
        dxf = _SafeDxf(e.dxf)
        frozen = []
        for n in getattr(e, "frozen_layers", []) or []:
            h = self.layers.get(str(n).upper())
            if h:
                frozen.append(h)
        vals = {
            "center": _v3(dxf.get("center", (0, 0, 0))), "width": _f(dxf.get("width", 1.0), 1.0),
            "height": _f(dxf.get("height", 1.0), 1.0),
            "target": _v3(dxf.get("view_target_point", (0, 0, 0))),
            "dir": _v3(dxf.get("view_direction_vector", (0, 0, 1)), (0.0, 0.0, 1.0)),
            "twist": _f(dxf.get("view_twist_angle", 0.0)) * _RAD,
            "vh": _f(dxf.get("view_height", 1.0), 1.0), "lens": _f(dxf.get("perspective_lens_length", 50.0), 50.0),
            "front": _f(dxf.get("front_clip_plane_z_value", 0.0)), "back": _f(dxf.get("back_clip_plane_z_value", 0.0)),
            "snap_angle": _f(dxf.get("snap_angle", 0.0)) * _RAD, "vc": _v2(dxf.get("view_center_point", (0, 0))),
            "snap_base": _v2(dxf.get("snap_base_point", (0, 0))), "snap": _v2(dxf.get("snap_spacing", (10, 10))),
            "grid": _v2(dxf.get("grid_spacing", (10, 10))), "zoom": int(dxf.get("circle_zoom", 1000)),
            "status": int(dxf.get("flags", 0)), "sheet": str(dxf.get("plot_style_name", "") or ""),
            "render": int(dxf.get("render_mode", 0)), "ucsvp": int(dxf.get("ucs_per_viewport", 0)),
            "uorg": _v3(dxf.get("ucs_origin", (0, 0, 0))), "ux": _v3(dxf.get("ucs_x_axis", (1, 0, 0))),
            "uy": _v3(dxf.get("ucs_y_axis", (0, 1, 0))), "uelev": _f(dxf.get("elevation", 0.0)),
            "uortho": int(dxf.get("ucs_ortho_type", 0)),
        }

        def data(d: BitWriter) -> None:  # §20.4.38 (R2000)
            d.bd3(vals["center"])
            d.bd(vals["width"])
            d.bd(vals["height"])
            d.bd3(vals["target"])
            d.bd3(vals["dir"])
            d.bd(vals["twist"])
            d.bd(vals["vh"])
            d.bd(vals["lens"])
            d.bd(vals["front"])
            d.bd(vals["back"])
            d.bd(vals["snap_angle"])
            d.rd2(vals["vc"])
            d.rd2(vals["snap_base"])
            d.rd2(vals["snap"])
            d.rd2(vals["grid"])
            d.bs(vals["zoom"])
            d.bl(len(frozen))
            d.bl(vals["status"])
            d.tv(vals["sheet"])
            d.rc(vals["render"])
            d.b(0)               # UCS at origin
            d.b(vals["ucsvp"])
            d.bd3(vals["uorg"])
            d.bd3(vals["ux"])
            d.bd3(vals["uy"])
            d.bd(vals["uelev"])
            d.bs(vals["uortho"])

        def handles(w: BitWriter) -> None:
            for h in frozen:
                w.h(HARD_PTR, h)
            w.h(SOFT_PTR, 0)   # clip boundary
            w.h(HARD_PTR, vph)  # viewport entity header (NULL for the overall paper viewport, as AutoCAD)
            w.h(HARD_PTR, 0)   # named UCS
            w.h(HARD_PTR, 0)   # base UCS

        # §20.4.70: floating viewports get a VIEWPORT ENTITY HEADER (AutoCAD-saved R2000 files have one per
        # floating viewport; the overall paper-space viewport, ID 1, has none)
        vph = self.alloc() if int(dxf.get("id", 2)) != 1 else 0
        plan = self._ent("VIEWPORT", e, data, handles)
        if vph:
            self.vp_headers.append((vph, plan.handle))
        return plan

    # ================================================================== pass 2: encode
    def _enc_tables(self, lt_rows, layer_rows, style_rows, appid_rows, dim_rows) -> None:
        rw, r = self.rw, self.r
        # LTYPE (§20.4.57/§20.4.58)
        user = [row[0] for row in lt_rows]
        rw.control("LTYPE_CONTROL", r["LTYPE_CONTROL"], [r["LTYPE_CONTINUOUS"]] + user,
                   hard=[r["LTYPE_BYBLOCK"], r["LTYPE_BYLAYER"]])
        rw.ltype(r["LTYPE_BYBLOCK"], r["LTYPE_CONTROL"], "ByBlock", "", 0.0, [])
        rw.ltype(r["LTYPE_BYLAYER"], r["LTYPE_CONTROL"], "ByLayer", "", 0.0, [])
        rw.ltype(r["LTYPE_CONTINUOUS"], r["LTYPE_CONTROL"], "Continuous", "Solid line", 0.0, [])
        for h, name, desc, total, dashes in lt_rows:
            rw.ltype(h, r["LTYPE_CONTROL"], name, desc, total, dashes)
        # LAYER (§20.4.53/§20.4.54)
        rw.control("LAYER_CONTROL", r["LAYER_CONTROL"], [row[0] for row in layer_rows])
        for h, name in layer_rows:
            lay = self.doc.layers.get(name) if name in self.doc.layers else None
            self._enc_layer(h, name, lay)
        # STYLE
        rw.control("STYLE_CONTROL", r["STYLE_CONTROL"], [row[0] for row in style_rows])
        for h, st in style_rows:
            if st is None:
                rw.style(h, r["STYLE_CONTROL"], "Standard", 0, 0.0, 1.0, 0.0, 0, 2.5, "txt", "")
                continue
            d = st.dxf
            rw.style(h, r["STYLE_CONTROL"], d.name, int(d.get("flags", 0)), _f(d.get("height", 0.0)),
                     _f(d.get("width", 1.0), 1.0) or 1.0, _f(d.get("oblique", 0.0)) * _RAD,
                     int(d.get("generation_flags", 0)), _f(d.get("last_height", 2.5), 2.5) or 2.5,
                     str(d.get("font", "txt") or "txt"), str(d.get("bigfont", "") or ""))
        # APPID
        rw.control("APPID_CONTROL", r["APPID_CONTROL"], [row[0] for row in appid_rows])
        for h, name in appid_rows:
            rw.appid(h, r["APPID_CONTROL"], name)
        # DIMSTYLE
        rw.control("DIMSTYLE_CONTROL", r["DIMSTYLE_CONTROL"], [row[0] for row in dim_rows])
        hv = header_getter(self.doc, {})
        for h, name, ds in dim_rows:
            def val(n: str, ds=ds) -> Any:
                if ds is not None:
                    try:
                        if ds.dxf.hasattr(n):
                            return ds.dxf.get(n)
                    except Exception:
                        pass
                return hv("$" + n.upper())
            txsty = self.styles["STANDARD"]
            if ds is not None and ds.dxf.hasattr("dimtxsty"):
                txsty = self.styles.get(str(ds.dxf.get("dimtxsty")).upper(), txsty)
            rw.dimstyle(h, r["DIMSTYLE_CONTROL"], name, val, txsty)
        # VPORT *Active, empty VIEW/UCS controls, viewport entity header control
        rw.control("VPORT_CONTROL", r["VPORT_CONTROL"], [r["VPORT_ACTIVE"]])
        (x0, y0, _z0), (x1, y1, _z1) = self.extents
        if x1 < x0:
            x0, y0, x1, y1 = 0.0, 0.0, 420.0, 297.0
        vh = max(y1 - y0, (x1 - x0) / 1.6, 1e-6) * 1.05
        vw = max(x1 - x0, 1e-6) * 1.05
        grid = 10.0
        rw.vport(r["VPORT_ACTIVE"], r["VPORT_CONTROL"], "*Active", vh, max(vw, vh * 0.5),
                 ((x0 + x1) / 2, (y0 + y1) / 2), grid, grid)
        rw.control("VIEW_CONTROL", r["VIEW_CONTROL"], [])
        rw.control("UCS_CONTROL", r["UCS_CONTROL"], [])
        rw.control("VP_ENT_HDR_CONTROL", r["VP_ENT_HDR_CONTROL"], [h for h, _vp in self.vp_headers])
        for i, (h, vp) in enumerate(self.vp_headers):
            rw.vp_ent_hdr(h, r["VP_ENT_HDR_CONTROL"], str(i + 1), vp)

    def _enc_layer(self, h: int, name: str, lay) -> None:
        r = self.r
        color, lw, flags, lth = 7, 31, 0, r["LTYPE_CONTINUOUS"]
        if lay is not None:
            d = lay.dxf
            color = int(d.get("color", 7))
            off = color < 0
            color = abs(color) or 7
            if color > 255:
                color = 7
            tc = d.get("true_color", None)
            if tc is not None and not d.hasattr("color"):
                from ezdxf.colors import int2rgb

                color = nearest_aci(tuple(int2rgb(int(tc))))  # type: ignore[arg-type]
                self.approx_colors += 1
            f70 = int(d.get("flags", 0))
            if f70 & 1:
                flags |= 1
            if off:
                flags |= 2
            if f70 & 2:
                flags |= 4
            if f70 & 4:
                flags |= 8
            if int(d.get("plot", 1)):
                flags |= 16
            lw = lineweight_index(int(d.get("lineweight", -3)))
            lt = str(d.get("linetype", "Continuous")).upper()
            if lt in self.ltypes and lt not in ("BYLAYER", "BYBLOCK"):
                lth = self.ltypes[lt]
            if off:
                color = -color
        else:
            flags |= 16
        flags |= (lw & 0x1F) << 5
        self.rw.layer(h, r["LAYER_CONTROL"], name, flags, color, lth, r["PLACEHOLDER"])

    def _layout_attrs(self, b: dict, tab: int) -> dict:
        lay = b["layout"]
        dl = None
        try:
            dl = lay.dxf_layout.dxf if lay is not None else None
        except AttributeError:
            dl = None

        def g(n: str, default):
            if dl is None:
                return default
            try:
                v = dl.get(n, None)
            except Exception:
                v = None
            return default if v is None else v

        model = b["blk_name"] == "*Model_Space"
        name = g("name", "Model" if model else "Layout1")
        if model:
            ext = self.extents
        else:
            ext = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        a = {
            "page_setup": str(g("page_setup_name", "")), "printer": str(g("plot_configuration_file", "none_device")),
            "plot_flags": int(g("plot_layout_flags", 688)),
            "margins": tuple(_f(g(k, 0.0)) for k in ("left_margin", "bottom_margin", "right_margin", "top_margin")),
            "paper_w": _f(g("paper_width", 0.0)), "paper_h": _f(g("paper_height", 0.0)),
            "paper_name": str(g("paper_size", "")),
            "plot_origin": (_f(g("plot_origin_x_offset", 0.0)), _f(g("plot_origin_y_offset", 0.0))),
            "paper_units": int(g("plot_paper_units", 1)), "plot_rotation": int(g("plot_rotation", 0)),
            "plot_type": int(g("plot_type", 5 if not model else 0)),
            "window_min": (_f(g("plot_window_x1", 0.0)), _f(g("plot_window_y1", 0.0))),
            "window_max": (_f(g("plot_window_x2", 0.0)), _f(g("plot_window_y2", 0.0))),
            "plot_view": str(g("plot_view_name", "")),
            "real_units": _f(g("scale_numerator", 1.0), 1.0), "drawing_units": _f(g("scale_denominator", 1.0), 1.0),
            "style_sheet": str(g("current_style_sheet", "")), "scale_type": int(g("standard_scale_type", 16)),
            "scale_factor": _f(g("unit_factor", 1.0), 1.0),
            "image_origin": (_f(g("paper_image_origin_x", 0.0)), _f(g("paper_image_origin_y", 0.0))),
            "name": str(name), "tab_order": int(g("taborder", tab)), "flags": int(g("layout_flags", 1)),
            "ucs_origin": _v3(g("ucs_origin", (0, 0, 0))), "limmin": _v2(g("limmin", (0, 0))),
            "limmax": _v2(g("limmax", (420.0, 297.0))), "insbase": _v3(g("insert_base", (0, 0, 0))),
            "ucs_x": _v3(g("ucs_xaxis", (1, 0, 0))), "ucs_y": _v3(g("ucs_yaxis", (0, 1, 0))),
            "elevation": _f(g("elevation", 0.0)), "ortho_type": int(g("ucs_type", 0)),
            "extmin": _v3(ext[0]), "extmax": _v3(ext[1]),
        }
        return a

    def _enc_objects(self, layouts: list[dict]) -> None:
        rw, r = self.rw, self.r
        nod = r["DICT_NAMED"]
        layout_items = []
        for i, b in enumerate(layouts):
            if b["lo"]:
                a = self._layout_attrs(b, i)
                b["layout_name"] = a["name"]
                rw.layout(b["lo"], r["DICT_LAYOUTS"], a, b["bh"])
                layout_items.append((a["name"], b["lo"]))
        seen = set()
        uniq = []
        for n, h in layout_items:
            k = n.upper()
            while k in seen:
                n += "_"
                k = n.upper()
            seen.add(k)
            uniq.append((n, h))
        rw.dictionary(nod, 0, sorted([("ACAD_GROUP", r["DICT_GROUP"]), ("ACAD_LAYOUT", r["DICT_LAYOUTS"]),
                                      ("ACAD_MLINESTYLE", r["DICT_MLINESTYLE"]),
                                      ("ACAD_PLOTSETTINGS", r["DICT_PLOTSETTINGS"]),
                                      ("ACAD_PLOTSTYLENAME", r["DICT_PLOTSTYLES"])]))
        rw.dictionary(r["DICT_GROUP"], nod, [], reactors=[nod])
        rw.dictionary(r["DICT_PLOTSTYLES"], nod, [("Normal", r["PLACEHOLDER"])], reactors=[nod],
                      default=r["PLACEHOLDER"])
        rw.placeholder(r["PLACEHOLDER"], r["DICT_PLOTSTYLES"])
        rw.dictionary(r["DICT_MLINESTYLE"], nod, [("Standard", r["CMLSTYLE"])], reactors=[nod])
        rw.mlinestyle(r["CMLSTYLE"], r["DICT_MLINESTYLE"])
        rw.dictionary(r["DICT_PLOTSETTINGS"], nod, [], reactors=[nod])
        rw.dictionary(r["DICT_LAYOUTS"], nod, sorted(uniq, key=lambda x: x[0].upper()), reactors=[nod])

    def _enc_block(self, b: dict) -> None:
        rw, r = self.rw, self.r
        plans: list[EntityPlan] = b["plans"]
        mode = b["mode"]
        bh = b["bh"]
        layer0 = r["LAYER0"]
        blk = b["block"]
        base = (0.0, 0.0, 0.0)
        bcommon = EntCommon(layer=layer0)
        has_attdefs = False
        description = ""
        if blk is not None:
            be = blk.block
            base = _v3(_sd(be).get("base_point", (0, 0, 0)))
            bcommon = self._common(be)
            has_attdefs = any(p.kind == "ATTDEF" for p in plans)
            description = str(_sd(be).get("description", "") or "") if be.dxf.is_supported("description") else ""
        first = plans[0].handle if plans else 0
        last = plans[-1].handle if plans else 0
        rw.block_header(bh, r["BLOCK_CONTROL"], b["name"], b["anonymous"], has_attdefs, base, b["blk"], first, last,
                        b["end"], b["lo"], description)
        owner = bh if mode == 0 else 0
        name = b["blk_name"]
        rw.entity(EntityPlan(b["blk"], T["BLOCK"], "BLOCK", bcommon, lambda d: d.tv(name)), mode, owner, 0, 0)
        rw.entity(EntityPlan(b["end"], T["ENDBLK"], "ENDBLK", bcommon), mode, owner, 0, 0)
        for i, p in enumerate(plans):
            prev = plans[i - 1].handle if i else 0
            nxt = plans[i + 1].handle if i + 1 < len(plans) else 0
            rw.entity(p, mode, owner, prev, nxt)
            kids = p.children
            subs = [k for k in kids if k.kind != "SEQEND"]
            for j, k in enumerate(kids):
                if k.kind == "SEQEND":
                    rw.entity(k, 0, p.handle, 0, 0)
                    continue
                kp = subs[j - 1].handle if j else 0
                kn = subs[j + 1].handle if j + 1 < len(subs) else 0
                rw.entity(k, 0, p.handle, kp, kn)

    # ================================================================== file assembly
    @property
    def extents(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        ext = getattr(self, "_extents", None)
        if ext is None:
            ext = self._compute_extents()
            self._extents = ext
        return ext

    def _compute_extents(self):
        try:
            from ezdxf import bbox

            box = bbox.extents(self.doc.modelspace(), fast=True)
            if box.has_data:
                lo, hi = _v3(box.extmin), _v3(box.extmax)
                return lo, hi
        except Exception as exc:
            self.warn(f"extents: {type(exc).__name__}: {exc}")
        return (1e20, 1e20, 1e20), (-1e20, -1e20, -1e20)

    def _assemble(self) -> bytes:
        r = self.r
        handseed = self.next
        (x0, y0, z0), (x1, y1, z1) = self.extents
        over = {"$EXTMIN": (x0, y0, z0), "$EXTMAX": (x1, y1, z1), "$HANDSEED": handseed}
        hv = header_getter(self.doc, over)
        hdr = write_header(hv, r, handseed, self.codec, self.stats)
        hdr_sec = _sentinel_section(SENT_HEADER, hdr, END_HEADER)
        cls_sec = _sentinel_section(SENT_CLASSES, self._classes(), END_CLASSES)
        out = bytearray(b"\0" * 0x61)
        aux_at = len(out)
        out += _aux_header(handseed)
        preview_at = len(out)
        out += PREVIEW_START + struct.pack("<I", 1) + b"\0" + PREVIEW_END
        hdr_at = len(out)
        out += hdr_sec
        cls_at = len(out)
        out += cls_sec
        out += struct.pack("<I", 0x0DCA)  # as in AutoCAD-saved files, before the first object
        offsets: dict[int, int] = {}
        for h in sorted(self.rw.out):
            offsets[h] = len(out)
            out += self.rw.out[h]
        map_at = len(out)
        omap = _object_map(offsets)
        out += omap
        second_at = len(out)
        locs = [(0, hdr_at, len(hdr_sec)), (1, cls_at, len(cls_sec)), (2, map_at, len(omap)), (3, 0, 0), (4, 0, 0),
                (5, aux_at, 123)]
        hrec = [handseed, r["BLOCK_CONTROL"], r["LAYER_CONTROL"], r["STYLE_CONTROL"], r["LTYPE_CONTROL"],
                r["VIEW_CONTROL"], r["UCS_CONTROL"], r["VPORT_CONTROL"], r["APPID_CONTROL"], r["DIMSTYLE_CONTROL"],
                r["VP_ENT_HDR_CONTROL"], r["DICT_NAMED"], r["DICT_MLINESTYLE"], r["DICT_GROUP"]]
        out += _second_header(second_at, locs, hrec)
        tmpl_at = len(out)
        measurement = 1 if int(self.doc.header.get("$MEASUREMENT", 0) or 0) else 0
        out += struct.pack("<hH", 0, measurement)
        # file header (§3.2)
        fh = bytearray()
        fh += b"AC1015"
        fh += b"\0" * 5 + bytes([0x0F, 0x01])
        fh += struct.pack("<I", preview_at)
        fh += bytes([APP_VERSION, APP_MAINT])     # writing application version / maintenance
        fh += struct.pack("<H", CODEPAGE_1252)
        recs = [(0, hdr_at, len(hdr_sec)), (1, cls_at, len(cls_sec)), (2, map_at, len(omap)), (3, 0, 0),
                (4, tmpl_at, 4), (5, aux_at, 123)]
        fh += struct.pack("<i", len(recs))
        for num, seek, size in recs:
            fh += struct.pack("<Bii", num, seek, size)
        crc = crc8(0, bytes(fh)) ^ _LOCATOR_CRC_XOR[len(recs)]
        fh += struct.pack("<H", crc)
        fh += FILE_SENTINEL
        assert len(fh) == 0x61, len(fh)
        out[0:0x61] = fh
        return bytes(out)

    def _classes(self) -> bytes:
        """§10.1: BS number, BS version, TV app, TV C++ name, TV DXF name, B zombie, BS item class id."""
        w = BitWriter(self.codec, self.stats)
        for i, (dxf, cpp, app, is_ent) in enumerate(CLASSES):
            w.bs(500 + i)
            w.bs(0)
            w.tv(app)
            w.tv(cpp)
            w.tv(dxf)
            w.b(0)
            w.bs(0x1F2 if is_ent else 0x1F3)
        return w.getvalue()


def _hatch_edge(d: BitWriter, et: str, ed) -> None:
    """One edge of a hatch edge path (§20.4.75 pathtypestatus 1–4); angles in radians."""
    et = et.upper()
    if et in ("LINE", "LINEEDGE"):
        d.rc(1)
        d.rd2(_v2(ed.start))
        d.rd2(_v2(ed.end))
    elif et in ("ARC", "ARCEDGE"):
        d.rc(2)
        d.rd2(_v2(ed.center))
        d.bd(_f(ed.radius))
        d.bd(_f(ed.start_angle) * _RAD)
        d.bd(_f(ed.end_angle) * _RAD)
        d.b(bool(ed.ccw))
    elif et in ("ELLIPSE", "ELLIPSEEDGE"):
        d.rc(3)
        d.rd2(_v2(ed.center))
        d.rd2(_v2(ed.major_axis))
        d.bd(_f(ed.ratio, 1.0))
        d.bd(_f(ed.start_angle) * _RAD)
        d.bd(_f(ed.end_angle) * _RAD)
        d.b(bool(ed.ccw))
    elif et in ("SPLINE", "SPLINEEDGE"):
        ctrl = [_v2(p) for p in ed.control_points]
        knots = [_f(k) for k in ed.knot_values]
        weights = [_f(w, 1.0) for w in (ed.weights or [])]
        rational = bool(weights) and len(weights) == len(ctrl)
        d.rc(4)
        d.bl(int(ed.degree))
        d.b(rational)
        d.b(bool(ed.periodic))
        d.bl(len(knots))
        d.bl(len(ctrl))
        for k in knots:
            d.bd(k)
        for i, p in enumerate(ctrl):
            d.rd2(p)
            if rational:
                d.bd(weights[i])
    else:
        raise ValueError(f"hatch edge type {et} not supported")


def _sentinel_section(start: bytes, data: bytes, end: bytes) -> bytes:
    """§9/§10.1: sentinel, RL size, data, RS CRC (seed 0xC0C1, over size + data), end sentinel."""
    body = struct.pack("<I", len(data)) + data
    return start + body + crc_le(0xC0C1, body) + end


def _object_map(offsets: dict[int, int]) -> bytes:
    """§23.1: sections of MC handle/offset deltas (restarting from 0 in every section), each preceded by
    its big-endian size (which counts itself) and followed by a big-endian CRC; a final empty section."""
    out = bytearray()
    items = sorted(offsets.items())
    i = 0
    while i < len(items):
        sec = bytearray()
        lh = lo = 0
        while i < len(items):
            h, off = items[i]
            pair = mc_encode(h - lh, signed=False) + mc_encode(off - lo, signed=True)
            if 2 + len(sec) + len(pair) > MAP_SECTION_MAX:
                break
            sec += pair
            lh, lo = h, off
            i += 1
        block = struct.pack(">H", len(sec) + 2) + sec
        out += block + struct.pack(">H", crc8(0xC0C1, bytes(block)))
    block = struct.pack(">H", 2)
    out += block + struct.pack(">H", crc8(0xC0C1, block))
    return bytes(out)


def _aux_header(handseed: int) -> bytes:
    """Chapter 27 AcDb:AuxHeader (123 bytes)."""
    from pdfcore.ops.plans.dwg.wheader import julian

    day, ms = julian()
    b = bytearray(b"\xff\x77\x01")
    b += struct.pack("<HH", 23, 0)                 # AC1015 = 23, maintenance 0
    b += struct.pack("<Ii", 1, -1)                 # number of saves, -1
    b += struct.pack("<HH", 1, 0)                  # saves part 1 / part 2
    b += struct.pack("<I", 0)
    b += struct.pack("<HHHH", 23, 0, 23, 0)
    b += struct.pack("<HHHHHH", 0x0005, 0x0893, 0x0005, 0x0893, 0x0000, 0x0001)
    b += struct.pack("<5I", 0, 0, 0, 0, 0)
    b += struct.pack("<II", day, ms) * 2           # TDCREATE, TDUPDATE
    b += struct.pack("<i", handseed if handseed < 0x7FFFFFFF else -1)
    b += struct.pack("<I", 0)                      # educational plot stamp
    b += struct.pack("<HH", 0, 1)
    b += struct.pack("<3I", 0, 0, 0)
    b += struct.pack("<I", 1)
    b += struct.pack("<4I", 0, 0, 0, 0)
    assert len(b) == 123, len(b)
    return bytes(b)


def _second_header(at: int, locs: list[tuple[int, int, int]], hrec: list[int]) -> bytes:
    """Chapter 26 second file header (bit coded after the RL size): location, version, the section
    locators, 14 handle records (handseed and table controls), CRC, 8 unused bytes, end sentinel."""
    w = BitWriter()
    w.bl(at)
    w.raw(b"AC1015")
    w.raw(b"\0" * 5 + bytes([0x0F]))   # maintenance version, as in the file header (§3.2.1)
    w.rc(0x01)                          # the file header's byte at 0x0C
    w.bs(APP_VERSION | (APP_MAINT << 8))  # writing application version / maintenance (file header 0x11)
    w.rs(CODEPAGE_1252)
    w.bs(len(locs))
    for num, addr, size in locs:
        w.rc(num)
        w.bl(addr)
        w.bl(size)
    w.bs(len(hrec))
    for i, h in enumerate(hrec):
        hb = h.to_bytes(max(1, (h.bit_length() + 7) // 8), "big")
        w.rc(len(hb))
        w.rc(i)
        w.raw(hb)
    data = w.getvalue()
    size = 4 + len(data) + 2 + 8
    body = struct.pack("<I", size) + data
    return SECOND_START + body + crc_le(0xC0C1, body) + b"\0" * 8 + SECOND_END
