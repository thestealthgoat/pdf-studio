"""Object-record encoders for the DWG writer (R2000 / AC1015), ODA spec chapter 20.

Each record is framed as §20.1/§20.2 describe: ``MS`` size, then a bit stream holding the object type
(BS, §2.12), the RL "endbit" (number of bits before the handle stream), the object's own handle (code 0),
the EED (none), the common data, the type-specific data, then the handle stream, padding to a byte and
the CRC (§2.14.1, seed 0xC0C1) over the size bytes and the data.

:class:`RecordWriter` holds the common framing (§20.1 non-entity, §20.4.1/§20.4.2 entity) and one method per
prescription the writer emits; section numbers are given at each method. Field order mirrors the reader in
:mod:`pdfcore.ops.plans.dwg.objects`, and the fields the reader skips (LTYPE text area, DIMSTYLE variables,
VPORT, VIEWPORT tail, MLINESTYLE, LAYOUT, APPID …) follow the spec text.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from pdfcore.ops.plans.dwg.bitwriter import BitWriter, crc_le, ms_encode, rl_bits, set_bits

# §20.3 fixed type codes used by the writer
T = {
    "TEXT": 0x01, "ATTRIB": 0x02, "ATTDEF": 0x03, "BLOCK": 0x04, "ENDBLK": 0x05, "SEQEND": 0x06,
    "INSERT": 0x07, "MINSERT": 0x08, "VERTEX_2D": 0x0A, "VERTEX_3D": 0x0B, "VERTEX_MESH": 0x0C,
    "VERTEX_PFACE": 0x0D, "VERTEX_PFACE_FACE": 0x0E, "POLYLINE_2D": 0x0F, "POLYLINE_3D": 0x10, "ARC": 0x11,
    "CIRCLE": 0x12, "LINE": 0x13, "DIMENSION_ORDINATE": 0x14, "DIMENSION_LINEAR": 0x15,
    "DIMENSION_ALIGNED": 0x16, "DIMENSION_ANG3PT": 0x17, "DIMENSION_ANG2LN": 0x18, "DIMENSION_RADIUS": 0x19,
    "DIMENSION_DIAMETER": 0x1A, "POINT": 0x1B, "3DFACE": 0x1C, "POLYLINE_PFACE": 0x1D, "POLYLINE_MESH": 0x1E,
    "SOLID": 0x1F, "TRACE": 0x20, "VIEWPORT": 0x22, "ELLIPSE": 0x23, "SPLINE": 0x24, "RAY": 0x28,
    "XLINE": 0x29, "DICTIONARY": 0x2A, "MTEXT": 0x2C, "TOLERANCE": 0x2E, "BLOCK_CONTROL": 0x30, "BLOCK_HEADER": 0x31,
    "LAYER_CONTROL": 0x32, "LAYER": 0x33, "STYLE_CONTROL": 0x34, "STYLE": 0x35, "LTYPE_CONTROL": 0x38,
    "LTYPE": 0x39, "VIEW_CONTROL": 0x3C, "UCS_CONTROL": 0x3E, "VPORT_CONTROL": 0x40, "VPORT": 0x41,
    "APPID_CONTROL": 0x42, "APPID": 0x43, "DIMSTYLE_CONTROL": 0x44, "DIMSTYLE": 0x45,
    "VP_ENT_HDR_CONTROL": 0x46, "VP_ENT_HDR": 0x47, "MLINESTYLE": 0x49, "LWPOLYLINE": 0x4D, "HATCH": 0x4E,
}

#: classes written to AcDb:Classes (§10.1): (dxf name, C++ class, application, is entity). The first
#: class gets number 500 (§20.3: "type minus 500 gives an index into the class list").
CLASSES = [
    ("ACDBDICTIONARYWDFLT", "AcDbDictionaryWithDefault", "ObjectDBX Classes", False),
    ("ACDBPLACEHOLDER", "AcDbPlaceHolder", "ObjectDBX Classes", False),
    ("LAYOUT", "AcDbLayout", "ObjectDBX Classes", False),
]
CLASS_NUM = {name: 500 + i for i, (name, *_rest) in enumerate(CLASSES)}

# handle reference codes (§2.13)
SOFT_OWN, HARD_OWN, SOFT_PTR, HARD_PTR = 2, 3, 4, 5


@dataclass
class EntCommon:
    """§20.4.1 common entity data (R2000): layer/linetype handles, colour index, lineweight index …"""

    layer: int
    color: int = 256
    ltype_flags: int = 0      # 0 BYLAYER, 1 BYBLOCK, 2 CONTINUOUS, 3 handle follows
    ltype: int = 0
    ltscale: float = 1.0
    lineweight: int = 29      # index; 29 = BYLAYER
    invisible: int = 0


Fn = Callable[[BitWriter], None]


def _nothing(_w: BitWriter) -> None:
    pass


@dataclass
class EntityPlan:
    """An entity whose handle is allocated and whose data/handle streams are ready to encode."""

    handle: int
    type: int
    kind: str
    common: EntCommon
    data: Fn = _nothing
    handles: Fn = _nothing
    children: list["EntityPlan"] = field(default_factory=list)


class RecordWriter:
    """Encodes framed records into ``self.out`` (handle → bytes)."""

    def __init__(self, codec: str, stats: Counter) -> None:
        self.codec = codec
        self.stats = stats
        self.out: dict[int, bytes] = {}

    def bw(self) -> BitWriter:
        return BitWriter(self.codec, self.stats)

    # ------------------------------------------------------------------ framing (§20.1, §2.14.1)
    def frame(self, type_code: int, handle: int, data: BitWriter, hstream: BitWriter) -> bytes:
        w = self.bw()
        w.bs(type_code)
        rlpos = w.tell()
        w.rl(0)              # endbit, patched below
        w.h(0, handle)
        w.bs(0)              # EED size: none (chapter 28)
        w.append(data)
        endbit = w.tell()
        w.append(hstream)
        buf = bytearray(w.getvalue())
        set_bits(buf, rlpos, rl_bits(endbit), 32)
        ms = ms_encode(len(buf))
        rec = ms + bytes(buf)
        return rec + crc_le(0xC0C1, rec)

    def obj(self, type_code: int, handle: int, owner: int, data: Fn, handles: Fn,
            reactors: Sequence[int] = (), xdic: int = 0) -> None:
        """Non-entity object (§20.1): BL reactor count; handles owner, reactors, xdictionary."""
        d = self.bw()
        d.bl(len(reactors))
        data(d)
        hw = self.bw()
        hw.h(SOFT_PTR, owner)
        for r in reactors:
            hw.h(SOFT_PTR, r)
        hw.h(HARD_OWN, xdic)
        handles(hw)
        self.out[handle] = self.frame(type_code, handle, d, hw)

    def entity(self, p: EntityPlan, mode: int, owner: int, prev: int, nxt: int) -> None:
        """Entity (§20.2, §20.4.1, §20.4.2). ``mode``: 0 owner handle stored, 1 paper space, 2 model space.
        The previous/next links are stored unless they are the implicit ±1 (the "Nolinks" bit)."""
        c = p.common
        h = p.handle
        nolinks = 1 if (prev == h - 1 and nxt == h + 1) else 0
        d = self.bw()
        d.b(0)                 # no graphic image
        d.bb(mode)
        d.bl(0)                # reactors
        d.b(nolinks)
        d.cmc(c.color)         # ENC for R2000 = BS colour index
        d.bd(c.ltscale)
        d.bb(c.ltype_flags)
        d.bb(0)                # plot style BYLAYER
        d.bs(c.invisible)
        d.rc(c.lineweight)
        p.data(d)
        hw = self.bw()
        if mode == 0:
            hw.h(SOFT_PTR, owner)
        hw.h(HARD_OWN, 0)      # xdictionary: none (R2000 always stores the handle)
        if not nolinks:
            hw.h(SOFT_PTR, prev)
            hw.h(SOFT_PTR, nxt)
        hw.h(HARD_PTR, c.layer)
        if c.ltype_flags == 3:
            hw.h(HARD_PTR, c.ltype)
        p.handles(hw)
        self.out[h] = self.frame(p.type, h, d, hw)

    # ------------------------------------------------------------------ control objects (§20.4.51ff)
    def control(self, kind: str, handle: int, entries: Sequence[int], hard: Sequence[int] = ()) -> None:
        """Table control object: BL count; NULL owner, xdictionary, entries (soft owner), then the hard-owned
        extras (*MODEL_SPACE/*PAPER_SPACE for BLOCK, BYBLOCK/BYLAYER for LTYPE). DIMSTYLE CONTROL has one
        more RC in R2000 files (count of extra hard pointers; written 0)."""

        def data(d: BitWriter) -> None:
            d.bl(len(entries))
            if kind == "DIMSTYLE_CONTROL":
                d.rc(0)

        def hs(w: BitWriter) -> None:
            for e in entries:
                w.h(SOFT_OWN, e)
            for e in hard:
                w.h(HARD_OWN, e)

        self.obj(T[kind], handle, 0, data, hs)

    @staticmethod
    def _table_common(d: BitWriter, name: str) -> None:
        """§20.4.54 etc.: entry name, 64-flag (referenced), xrefindex+1 (0), xdep."""
        d.tv(name)
        d.b(1)
        d.bs(0)
        d.b(0)

    # ------------------------------------------------------------------ table records
    def ltype(self, handle: int, owner: int, name: str, desc: str, length: float, dashes: Sequence[float]) -> None:
        """LTYPE (§20.4.58): simple dashes only; the R2000 256-byte text area is written empty."""

        def data(d: BitWriter) -> None:
            self._table_common(d, name)
            d.tv(desc)
            d.bd(length)
            d.rc(ord("A"))
            d.rc(len(dashes))
            for x in dashes:
                d.bd(x)
                d.bs(0)          # complex shape code
                d.rd(0.0)
                d.rd(0.0)
                d.bd(1.0)        # scale
                d.bd(0.0)        # rotation
                d.bs(0)          # shape flag
            d.raw(b"\0" * 256)   # strings area (R2004 and earlier)

        def hs(w: BitWriter) -> None:
            w.h(HARD_PTR, 0)     # xref block
            for _ in dashes:
                w.h(HARD_PTR, 0)  # shape file per dash

        self.obj(T["LTYPE"], handle, owner, data, hs)

    def layer(self, handle: int, owner: int, name: str, flags: int, color: int, ltype: int, plotstyle: int) -> None:
        """LAYER (§20.4.54, R2000): BS flag word (frozen 1, off 2, frozen-in-new-VP 4, locked 8, plot 16,
        lineweight index << 5) and the colour (negative when off)."""

        def data(d: BitWriter) -> None:
            self._table_common(d, name)
            d.bs(flags)
            d.cmc(color)

        def hs(w: BitWriter) -> None:
            w.h(HARD_PTR, 0)          # xref block
            w.h(HARD_PTR, plotstyle)  # plot style (the "Normal" placeholder)
            w.h(HARD_PTR, ltype)

        self.obj(T["LAYER"], handle, owner, data, hs)

    def style(self, handle: int, owner: int, name: str, flags: int, height: float, width: float, oblique: float,
              generation: int, last_height: float, font: str, bigfont: str) -> None:
        """STYLE ("SHAPEFILE", §20.4.56). ``oblique`` in radians."""

        def data(d: BitWriter) -> None:
            self._table_common(d, name)
            d.b(flags & 4)       # vertical
            d.b(flags & 1)       # shape file
            d.bd(height)
            d.bd(width)
            d.bd(oblique)
            d.rc(generation)
            d.bd(last_height)
            d.tv(font)
            d.tv(bigfont)

        self.obj(T["STYLE"], handle, owner, data, lambda w: w.h(HARD_PTR, 0))

    def appid(self, handle: int, owner: int, name: str) -> None:
        """APPID (§20.4.66)."""

        def data(d: BitWriter) -> None:
            self._table_common(d, name)
            d.rc(0)

        self.obj(T["APPID"], handle, owner, data, lambda w: w.h(HARD_PTR, 0))

    def dimstyle(self, handle: int, owner: int, name: str, v: Callable[[str], float], txsty: int) -> None:
        """DIMSTYLE (§20.4.68, R2000+ field list). ``v(name)`` gives each DIMxxx value."""

        def data(d: BitWriter) -> None:
            self._table_common(d, name)
            d.tv(str(v("dimpost") or ""))
            d.tv(str(v("dimapost") or ""))
            for n in ("dimscale", "dimasz", "dimexo", "dimdli", "dimexe", "dimrnd", "dimdle", "dimtp", "dimtm"):
                d.bd(v(n))
            for n in ("dimtol", "dimlim", "dimtih", "dimtoh", "dimse1", "dimse2"):
                d.b(int(v(n)))
            for n in ("dimtad", "dimzin", "dimazin"):
                d.bs(int(v(n)))
            for n in ("dimtxt", "dimcen", "dimtsz", "dimaltf", "dimlfac", "dimtvp", "dimtfac", "dimgap", "dimaltrnd"):
                d.bd(v(n))
            d.b(int(v("dimalt")))
            d.bs(int(v("dimaltd")))
            for n in ("dimtofl", "dimsah", "dimtix", "dimsoxd"):
                d.b(int(v(n)))
            for n in ("dimclrd", "dimclre", "dimclrt", "dimadec", "dimdec", "dimtdec", "dimaltu", "dimalttd",
                      "dimaunit", "dimfrac", "dimlunit", "dimdsep", "dimtmove", "dimjust"):
                d.bs(int(v(n)))
            d.b(int(v("dimsd1")))
            d.b(int(v("dimsd2")))
            for n in ("dimtolj", "dimtzin", "dimaltz", "dimalttz"):
                d.bs(int(v(n)))
            d.b(int(v("dimupt")))
            d.bs(int(v("dimatfit")))
            d.bs(int(v("dimlwd")))
            d.bs(int(v("dimlwe")))
            d.b(0)               # unknown (70 bit 0)

        def hs(w: BitWriter) -> None:
            w.h(HARD_PTR, 0)      # xref block
            w.h(HARD_PTR, txsty)  # DIMTXSTY
            for _ in range(4):    # DIMLDRBLK, DIMBLK, DIMBLK1, DIMBLK2: default arrows
                w.h(HARD_PTR, 0)

        self.obj(T["DIMSTYLE"], handle, owner, data, hs)

    def vport(self, handle: int, owner: int, name: str, height: float, width: float, center: tuple[float, float],
              grid: float, snap: float) -> None:
        """VPORT (§20.4.64, R2000): the ``*Active`` model-space view. ``width`` is aspect × height."""

        def data(d: BitWriter) -> None:
            self._table_common(d, name)
            d.bd(height)
            d.bd(width)
            d.rd2(center)
            d.bd3((0.0, 0.0, 0.0))   # target
            d.bd3((0.0, 0.0, 1.0))   # direction
            d.bd(0.0)                # twist
            d.bd(50.0)               # lens length
            d.bd(0.0)
            d.bd(0.0)                # front / back clip
            for bit in (0, 0, 0, 1):  # VIEWMODE bits 0,1,2 and NOT(71 & 16)
                d.b(bit)
            d.rc(0)                  # render mode
            d.rd2((0.0, 0.0))
            d.rd2((1.0, 1.0))
            d.b(0)                   # UCSFOLLOW
            d.bs(1000)               # circle zoom
            d.b(1)                   # fast zoom
            d.bb(3)                  # UCSICON
            d.b(0)                   # grid off
            d.rd2((grid, grid))
            d.b(0)                   # snap off
            d.b(0)                   # snap style
            d.bs(0)                  # isopair
            d.bd(0.0)                # snap rotation
            d.rd2((0.0, 0.0))        # snap base
            d.rd2((snap, snap))
            d.b(0)                   # unknown (UCS at origin)
            d.b(1)                   # UCS per viewport
            d.bd3((0.0, 0.0, 0.0))
            d.bd3((1.0, 0.0, 0.0))
            d.bd3((0.0, 1.0, 0.0))
            d.bd(0.0)                # UCS elevation
            d.bs(0)                  # orthographic type

        def hs(w: BitWriter) -> None:
            w.h(HARD_PTR, 0)  # xref block
            w.h(HARD_PTR, 0)  # named UCS
            w.h(HARD_PTR, 0)  # base UCS

        self.obj(T["VPORT"], handle, owner, data, hs)

    def vp_ent_hdr(self, handle: int, owner: int, name: str, viewport: int) -> None:
        """VIEWPORT ENTITY HEADER (§20.4.70): name, the "on" bit; handles xref block, the viewport (soft),
        previous header in the chain (NULL)."""

        def data(d: BitWriter) -> None:
            self._table_common(d, name)
            d.b(1)

        def hs(w: BitWriter) -> None:
            w.h(HARD_PTR, 0)
            w.h(SOFT_PTR, viewport)
            w.h(HARD_PTR, 0)

        self.obj(T["VP_ENT_HDR"], handle, owner, data, hs)

    def block_header(self, handle: int, owner: int, name: str, anonymous: bool, has_attdefs: bool,
                     base: Sequence[float], block_ent: int, first: int, last: int, endblk: int, layout: int,
                     description: str = "") -> None:
        """BLOCK HEADER (§20.4.52, R2000): first/last entity soft pointers, no insert handles."""

        def data(d: BitWriter) -> None:
            self._table_common(d, name)
            d.b(anonymous)
            d.b(has_attdefs)
            d.b(0)          # is xref
            d.b(0)          # xref overlaid
            d.b(0)          # loaded bit
            d.bd3(base)
            d.tv("")        # xref path
            d.rc(0)         # insert count list: terminator only
            d.tv(description)
            d.bl(0)         # preview size

        def hs(w: BitWriter) -> None:
            w.h(HARD_PTR, 0)        # NULL
            w.h(HARD_OWN, block_ent)
            w.h(SOFT_PTR, first)
            w.h(SOFT_PTR, last)
            w.h(HARD_OWN, endblk)
            w.h(HARD_PTR, layout)

        self.obj(T["BLOCK_HEADER"], handle, owner, data, hs)

    # ------------------------------------------------------------------ objects
    def dictionary(self, handle: int, owner: int, items: Iterable[tuple[str, int]], reactors: Sequence[int] = (),
                   default: int | None = None, cloning: int = 1) -> None:
        """DICTIONARY (§20.4.44); with ``default`` the DICTIONARYWDFLT class (§20.4.45)."""
        items = list(items)

        def data(d: BitWriter) -> None:
            d.bl(len(items))
            d.bs(cloning)
            d.rc(0)          # hard owner flag
            for n, _h in items:
                d.tv(n)

        def hs(w: BitWriter) -> None:
            for _n, h in items:
                w.h(SOFT_OWN, h)
            if default is not None:
                w.h(HARD_PTR, default)

        t = T["DICTIONARY"] if default is None else CLASS_NUM["ACDBDICTIONARYWDFLT"]
        self.obj(t, handle, owner, data, hs, reactors=reactors)

    def placeholder(self, handle: int, owner: int) -> None:
        """ACDBPLACEHOLDER: no data beyond the common object data."""
        self.obj(CLASS_NUM["ACDBPLACEHOLDER"], handle, owner, _nothing, _nothing, reactors=[owner])

    def mlinestyle(self, handle: int, owner: int, name: str = "Standard") -> None:
        """MLINESTYLE (§20.4.73): the default two-line style (offsets ±0.5, BYLAYER colour/linetype)."""

        def data(d: BitWriter) -> None:
            d.tv(name)
            d.tv("")
            d.bs(0)
            d.cmc(256)
            d.bd(math.pi / 2)
            d.bd(math.pi / 2)
            d.rc(2)
            for off in (0.5, -0.5):
                d.bd(off)
                d.cmc(256)
                d.bs(32767)   # linetype index: BYLAYER

        self.obj(T["MLINESTYLE"], handle, owner, data, _nothing, reactors=[owner])

    def layout(self, handle: int, owner: int, a: dict, block_record: int, last_vp: int = 0) -> None:
        """LAYOUT (§20.4.84, R2000): plot settings, then the layout fields."""

        def data(d: BitWriter) -> None:
            d.tv(a["page_setup"])
            d.tv(a["printer"])
            d.bs(a["plot_flags"])
            for m in a["margins"]:
                d.bd(m)
            d.bd(a["paper_w"])
            d.bd(a["paper_h"])
            d.tv(a["paper_name"])
            d.bd2(a["plot_origin"])
            d.bs(a["paper_units"])
            d.bs(a["plot_rotation"])
            d.bs(a["plot_type"])
            d.bd2(a["window_min"])
            d.bd2(a["window_max"])
            d.tv(a["plot_view"])       # R13–R2000: plot view name
            d.bd(a["real_units"])
            d.bd(a["drawing_units"])
            d.tv(a["style_sheet"])
            d.bs(a["scale_type"])
            d.bd(a["scale_factor"])
            d.bd2(a["image_origin"])
            d.tv(a["name"])
            d.bl(a["tab_order"])
            d.bs(a["flags"])
            d.bd3(a["ucs_origin"])
            d.rd2(a["limmin"])
            d.rd2(a["limmax"])
            d.bd3(a["insbase"])
            d.bd3(a["ucs_x"])
            d.bd3(a["ucs_y"])
            d.bd(a["elevation"])
            d.bs(a["ortho_type"])
            d.bd3(a["extmin"])
            d.bd3(a["extmax"])

        def hs(w: BitWriter) -> None:
            w.h(SOFT_PTR, block_record)
            w.h(SOFT_PTR, last_vp)
            w.h(HARD_PTR, 0)   # base UCS
            w.h(HARD_PTR, 0)   # named UCS

        self.obj(CLASS_NUM["LAYOUT"], handle, owner, data, hs, reactors=[owner])
