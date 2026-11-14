"""AcDb:AcDbObjects — object and entity records (ODA spec chapter 20).

Each record is decoded with two independent bit readers: the *data* stream from the start of the record,
and the *handle* stream that begins at the "endbit" (R2000–R2007: the RL "size of object data in bits",
§20.1; R2010+: record size minus the MC handle-stream size). For R2007+ a third reader covers the
*string stream* at the end of the data stream (§20.1, "String stream present bit"). Because the handle
stream position is known up front, the owner/layer/linetype references can be read even for types whose
data we do not decode.

Implemented prescriptions (section numbers of spec v5.4.1):
§20.4.1/§20.4.2 common entity data and handles, §20.4.3 TEXT, §20.4.4 ATTRIB, §20.4.5 ATTDEF,
§20.4.6 BLOCK, §20.4.7 ENDBLK, §20.4.8 SEQEND, §20.4.9 INSERT, §20.4.10 MINSERT, §20.4.11–§20.4.15
VERTEX variants, §20.4.16 2D POLYLINE, §20.4.17 3D POLYLINE, §20.4.18 ARC, §20.4.20 CIRCLE, §20.4.21
LINE, §20.4.22–§20.4.29 DIMENSION, §20.4.31 POINT, §20.4.32 3DFACE, §20.4.35 SOLID, §20.4.36 TRACE,
§20.4.38 VIEWPORT, §20.4.39 ELLIPSE, §20.4.40 SPLINE, §20.4.42 RAY, §20.4.43 XLINE, §20.4.44 DICTIONARY,
§20.4.46 MTEXT, §20.4.47 LEADER, §20.4.51/§20.4.52 BLOCK CONTROL / BLOCK HEADER, §20.4.53/§20.4.54 LAYER,
§20.4.56 STYLE ("SHAPEFILE"), §20.4.58 LTYPE, §20.4.68 DIMSTYLE (name only), §20.4.75 HATCH,
§20.4.84 LAYOUT, §20.4.85 LWPOLYLINE, §20.4.80 IMAGE (also used for WIPEOUT), §20.4.49 TOLERANCE,
§20.4.19 ARC_DIMENSION, §20.4.30 LARGE_RADIAL_DIMENSION, chapter 28 (EED, skipped).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from pdfcore.ops.plans.dwg.bits import R14, R2000, R2004, R2007, R2010, R2013, R2018, BitReader, mc_bytes, ms_bytes
from pdfcore.ops.plans.dwg.errors import DwgReadError

# §20.3 fixed object type numbers
FIXED_TYPES: dict[int, str] = {
    0x01: "TEXT", 0x02: "ATTRIB", 0x03: "ATTDEF", 0x04: "BLOCK", 0x05: "ENDBLK", 0x06: "SEQEND", 0x07: "INSERT",
    0x08: "MINSERT", 0x0A: "VERTEX_2D", 0x0B: "VERTEX_3D", 0x0C: "VERTEX_MESH", 0x0D: "VERTEX_PFACE",
    0x0E: "VERTEX_PFACE_FACE", 0x0F: "POLYLINE_2D", 0x10: "POLYLINE_3D", 0x11: "ARC", 0x12: "CIRCLE",
    0x13: "LINE", 0x14: "DIMENSION_ORDINATE", 0x15: "DIMENSION_LINEAR", 0x16: "DIMENSION_ALIGNED",
    0x17: "DIMENSION_ANG3PT", 0x18: "DIMENSION_ANG2LN", 0x19: "DIMENSION_RADIUS", 0x1A: "DIMENSION_DIAMETER",
    0x1B: "POINT", 0x1C: "3DFACE", 0x1D: "POLYLINE_PFACE", 0x1E: "POLYLINE_MESH", 0x1F: "SOLID", 0x20: "TRACE",
    0x21: "SHAPE", 0x22: "VIEWPORT", 0x23: "ELLIPSE", 0x24: "SPLINE", 0x25: "REGION", 0x26: "3DSOLID",
    0x27: "BODY", 0x28: "RAY", 0x29: "XLINE", 0x2A: "DICTIONARY", 0x2B: "OLEFRAME", 0x2C: "MTEXT",
    0x2D: "LEADER", 0x2E: "TOLERANCE", 0x2F: "MLINE", 0x30: "BLOCK_CONTROL", 0x31: "BLOCK_HEADER",
    0x32: "LAYER_CONTROL", 0x33: "LAYER", 0x34: "STYLE_CONTROL", 0x35: "STYLE", 0x38: "LTYPE_CONTROL",
    0x39: "LTYPE", 0x3C: "VIEW_CONTROL", 0x3D: "VIEW", 0x3E: "UCS_CONTROL", 0x3F: "UCS",
    0x40: "VPORT_CONTROL", 0x41: "VPORT", 0x42: "APPID_CONTROL", 0x43: "APPID", 0x44: "DIMSTYLE_CONTROL",
    0x45: "DIMSTYLE", 0x46: "VP_ENT_HDR_CONTROL", 0x47: "VP_ENT_HDR", 0x48: "GROUP", 0x49: "MLINESTYLE",
    0x4A: "OLE2FRAME", 0x4B: "DUMMY", 0x4C: "LONG_TRANSACTION", 0x4D: "LWPOLYLINE", 0x4E: "HATCH",
    0x4F: "XRECORD", 0x50: "ACDBPLACEHOLDER", 0x51: "VBA_PROJECT", 0x52: "LAYOUT",
    0x1F2: "ACAD_PROXY_ENTITY", 0x1F3: "ACAD_PROXY_OBJECT",
}
_ENTITY_FIXED = set(range(0x01, 0x2A)) | {0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x4A, 0x4D, 0x4E, 0x1F2}
# class DXF names (§20.3 "non-fixed values") that we decode, mapped to our type names
_CLASS_ALIASES = {"LWPLINE": "LWPOLYLINE", "LWPOLYLINE": "LWPOLYLINE", "HATCH": "HATCH", "LAYOUT": "LAYOUT",
                  "DICTIONARYWDFLT": "DICTIONARYWDFLT", "ACDBDICTIONARYWDFLT": "DICTIONARYWDFLT"}


@dataclass
class DwgClass:
    num: int
    app: str
    cpp: str
    dxf: str
    is_entity: bool


@dataclass
class DwgObject:
    handle: int
    type: int
    name: str
    is_entity: bool
    owner: int = 0
    reactors: list[int] = field(default_factory=list)
    xdic: int = 0
    # entity common data (§20.4.1)
    entmode: int = 0
    color: dict = field(default_factory=dict)
    ltscale: float = 1.0
    ltype_flags: int = 0
    invisible: int = 0
    lineweight: int = 29
    layer: int = 0
    ltype: int = 0
    nolinks: int = 1
    prev: int = 0
    next: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def read_classes(buf: bytes, ver: int, maint: int, codec: str) -> dict[int, DwgClass]:
    """Class definitions (§10). Returns {class number: DwgClass}."""
    out: dict[int, DwgClass] = {}
    r = BitReader(buf, ver, codec=codec)
    if ver >= R2007:
        # R2007+: RL bit size of the data, strings at its end (§10.2 note, §20.1 string-stream rule)
        if len(buf) < 4:
            return out
        nbits = r.rlu()
        if not 32 < nbits <= len(buf) * 8:
            return out
        _attach_section_strings(r, nbits)
        r.end = nbits
    try:
        if ver >= R2004:
            r.bs()  # max class number
            r.rc(); r.rc(); r.b()
        while r.remaining() > 40:
            num = r.bs()
            r.bs()  # version / proxy flags
            app = r.tv()
            cpp = r.tv()
            dxf = r.tv()
            r.b()
            item = r.bs()
            if ver >= R2004:
                r.bl(); r.bs(); r.bs(); r.bl(); r.bl()
            if num < 500 or num > 5000:
                break
            out[num] = DwgClass(num, app, cpp, dxf, item == 0x1F2)
            if ver >= R2007 and r.strings is not None and r.strings.remaining() <= 0:
                break
    except DwgReadError:
        pass
    return out


def _attach_section_strings(r: BitReader, endbit: int) -> None:
    """R2007+: locate a string stream that ends just before ``endbit`` (§20.1 rule), used for sections
    that carry their own string stream (classes)."""
    try:
        s = _string_stream(r, endbit)
    except DwgReadError:
        s = None
    r.strings = s


def _string_stream(r: BitReader, endbit: int) -> BitReader | None:
    """§20.1: the bit before ``endbit`` says whether a string stream is present; its size sits in the
    16 (or 32) bits before that, and the stream itself precedes the size."""
    save = r.pos
    try:
        r.seek(endbit - 1)
        if not r.b():
            return None
        p = endbit - 1 - 16
        r.seek(p)
        size = r.rsu()
        if size & 0x8000:
            p -= 16
            r.seek(p)
            hi = r.rsu()
            size = (size & 0x7FFF) | (hi << 15)
        start = p - size
        if start < 0:
            raise DwgReadError("string stream size exceeds object")
        s = BitReader(b"", r.ver, codec=r.codec)
        s.data = r.data
        s.pos = start
        s.end = p
        return s
    finally:
        r.pos = save


class ObjectReader:
    """Decodes records of one container (shared version, codec and class table)."""

    def __init__(self, buf: bytes, ver: int, codec: str, classes: dict[int, DwgClass]) -> None:
        self.buf = buf
        self.ver = ver
        self.codec = codec
        self.classes = classes

    def type_name(self, t: int) -> tuple[str, bool]:
        if t in FIXED_TYPES:
            return FIXED_TYPES[t], t in _ENTITY_FIXED
        cls = self.classes.get(t)
        if cls is None:
            return f"CLASS_{t}", False
        return _CLASS_ALIASES.get(cls.dxf, cls.dxf), cls.is_entity

    def read(self, off: int, expect_handle: int | None = None) -> DwgObject:
        buf = self.buf
        ver = self.ver
        if off < 0 or off >= len(buf):
            raise DwgReadError(f"object offset 0x{off:x} outside objects data")
        size, p = ms_bytes(buf, off)
        hbits = 0
        if ver >= R2010:
            hbits, p = mc_bytes(buf, p, signed=False)
        if size <= 0 or p + size > len(buf) + 2:
            raise DwgReadError(f"object at 0x{off:x}: bad size {size}")
        data = buf[p:p + size]
        r = BitReader(data, ver, codec=self.codec)
        if ver >= R2010:
            pair = r.bb()
            if pair == 0:
                t = r.rc()
            elif pair == 1:
                t = r.rc() + 0x1F0
            else:
                t = r.rsu()
            endbit = size * 8 - hbits
        else:
            t = r.bs()
            endbit = r.rlu() if ver >= R2000 else 0
        if not 0 < endbit <= size * 8:
            raise DwgReadError(f"object at 0x{off:x}: bad data bit size {endbit}")
        name, is_ent = self.type_name(t)
        try:
            return self._read_body(r, name, is_ent, t, endbit, size)
        except DwgReadError as exc:
            exc.obj_name = name  # type: ignore[attr-defined]
            exc.is_entity = is_ent  # type: ignore[attr-defined]
            raise

    def _read_body(self, r: BitReader, name: str, is_ent: bool, t: int, endbit: int, size: int) -> DwgObject:
        ver = self.ver
        handle = r.h()[1]
        o = DwgObject(handle, t, name, is_ent)
        # EED (chapter 28): skip
        while True:
            n = r.bs()
            if n <= 0:
                break
            r.h()
            r.pos += n * 8
            if r.pos > r.end:
                raise DwgReadError(f"object {handle:X}: EED runs past end")
        if ver >= R2007:
            r.strings = _string_stream(r, endbit)
        hr = BitReader(b"", ver, codec=self.codec)
        hr.data = r.data
        hr.pos = endbit
        hr.end = size * 8
        if is_ent:
            self._entity_common(r, o)
        else:
            o_nreact = r.bl()
            if ver >= R2004:
                o.data["_xdic_missing"] = r.b()
            else:
                o.data["_xdic_missing"] = 0
            if ver >= R2013:
                r.b()
            o.data["_nreact"] = o_nreact
        # common handles
        if is_ent:
            self._entity_handles(hr, o)
        else:
            o.owner = hr.href(handle)
            for _ in range(_cap(o.data.pop("_nreact"), 100000)):
                o.reactors.append(hr.href(handle))
            if not o.data.pop("_xdic_missing"):
                o.xdic = hr.href(handle)
        fn = _PARSERS.get(name)
        if fn is not None:
            try:
                fn(self, r, hr, o)
            except DwgReadError as exc:
                o.error = str(exc)
        return o

    # -------------------------------------------------------------- §20.4.1
    def _entity_common(self, r: BitReader, o: DwgObject) -> None:
        ver = self.ver
        if r.b():  # graphic present
            gsize = r.bll() if ver >= R2010 else r.rlu()
            r.pos += gsize * 8
            if r.pos > r.end:
                raise DwgReadError(f"entity {o.handle:X}: graphics data runs past end")
        o.entmode = r.bb()
        o.data["_nreact"] = r.bl()
        o.data["_xdic_missing"] = r.b() if ver >= R2004 else 0
        if ver >= R2013:
            r.b()
        # "Nolinks": present up to R2000; from R2004 on the links are never stored (always 1, no bit)
        o.nolinks = r.b() if ver < R2004 else 1
        o.color = r.enc()
        o.ltscale = r.bd()
        o.ltype_flags = r.bb()
        o.data["_psflags"] = r.bb()
        if ver >= R2007:
            o.data["_matflags"] = r.bb()
            r.rc()
        if ver >= R2010:
            o.data["_vs"] = (r.b(), r.b(), r.b())
        o.invisible = r.bs()
        o.lineweight = r.rc()

    # -------------------------------------------------------------- §20.4.2
    def _entity_handles(self, hr: BitReader, o: DwgObject) -> None:
        ver = self.ver
        h = o.handle
        if o.entmode == 0:
            o.owner = hr.href(h)
        for _ in range(_cap(o.data.pop("_nreact"), 100000)):
            o.reactors.append(hr.href(h))
        if not o.data.pop("_xdic_missing"):
            o.xdic = hr.href(h)
        if ver <= R2000 and not o.nolinks:
            o.prev = hr.href(h)
            o.next = hr.href(h)
        if ver >= R2004 and o.color.get("has_book_handle"):
            hr.href(h)
        o.layer = hr.href(h)
        if o.ltype_flags == 3:
            o.ltype = hr.href(h)
        if ver >= R2007 and o.data.pop("_matflags", 0) == 3:
            hr.href(h)
        if o.data.pop("_psflags") == 3:
            hr.href(h)
        if ver >= R2010:
            for flag in o.data.pop("_vs", ()):
                if flag:
                    hr.href(h)


def _cap(n: int, limit: int) -> int:
    if n < 0 or n > limit:
        raise DwgReadError(f"implausible count {n}")
    return n


# ====================================================================== entity parsers
def _text_common(rd: ObjectReader, r: BitReader, o: DwgObject) -> None:
    d = o.data
    if rd.ver >= R2000:
        f = r.rc()
        d["elevation"] = 0.0 if f & 1 else r.rd()
        ins = r.rd2()
        d["insert"] = ins
        d["align"] = None if f & 2 else (r.dd(ins[0]), r.dd(ins[1]))
        d["extrusion"] = r.be()
        d["thickness"] = r.bt()
        d["oblique"] = 0.0 if f & 4 else r.rd()
        d["rotation"] = 0.0 if f & 8 else r.rd()
        d["height"] = r.rd()
        d["width"] = 1.0 if f & 0x10 else r.rd()
        d["text"] = r.tv()
        d["generation"] = 0 if f & 0x20 else r.bs()
        d["halign"] = 0 if f & 0x40 else r.bs()
        d["valign"] = 0 if f & 0x80 else r.bs()
    else:
        d["elevation"] = r.bd()
        d["insert"] = r.rd2()
        d["align"] = r.rd2()
        d["extrusion"] = r.bd3()
        d["thickness"] = r.bd()
        d["oblique"] = r.bd()
        d["rotation"] = r.bd()
        d["height"] = r.bd()
        d["width"] = r.bd()
        d["text"] = r.tv()
        d["generation"] = r.bs()
        d["halign"] = r.bs()
        d["valign"] = r.bs()


def p_text(rd, r, hr, o):
    _text_common(rd, r, o)
    o.data["style"] = hr.href(o.handle)


def _attrib_tail(rd, r, o):
    d = o.data
    if rd.ver >= R2010:
        r.rc()
    atype = 1
    if rd.ver >= R2018:
        atype = r.rc()
    if atype in (2, 4):
        d["multiline"] = True
        raise DwgReadError("multi-line attribute (embedded MTEXT) not decoded")
    d["tag"] = r.tv()
    r.bs()
    d["flags"] = r.rc()
    if rd.ver >= R2007:
        d["lock"] = r.b()


def p_attrib(rd, r, hr, o):
    _text_common(rd, r, o)
    o.data["style"] = hr.href(o.handle)
    _attrib_tail(rd, r, o)


def p_attdef(rd, r, hr, o):
    _text_common(rd, r, o)
    o.data["style"] = hr.href(o.handle)
    _attrib_tail(rd, r, o)
    if rd.ver >= R2010:
        r.rc()
    o.data["prompt"] = r.tv()


def p_block(rd, r, hr, o):
    o.data["name"] = r.tv()


def p_nothing(rd, r, hr, o):
    pass


def _insert_common(rd, r, o):
    d = o.data
    d["insert"] = r.bd3()
    if rd.ver >= R2000:
        f = r.bb()
        if f == 3:
            sc = (1.0, 1.0, 1.0)
        elif f == 1:
            sc = (1.0, r.dd(1.0), r.dd(1.0))
        elif f == 2:
            x = r.rd()
            sc = (x, x, x)
        else:
            x = r.rd()
            sc = (x, r.dd(x), r.dd(x))
    else:
        sc = r.bd3()
    d["scale"] = sc
    d["rotation"] = r.bd()
    d["extrusion"] = r.bd3()
    d["has_attribs"] = r.b()
    d["owned"] = r.bl() if rd.ver >= R2004 and d["has_attribs"] else 0


def _insert_handles(rd, hr, o):
    d = o.data
    h = o.handle
    d["block"] = hr.href(h)
    attribs = []
    if d["has_attribs"]:
        if rd.ver <= R2000:
            d["first_attrib"] = hr.href(h)
            d["last_attrib"] = hr.href(h)
        else:
            for _ in range(_cap(d["owned"], 100000)):
                attribs.append(hr.href(h))
        d["seqend"] = hr.href(h)
    d["attribs"] = attribs


def p_insert(rd, r, hr, o):
    _insert_common(rd, r, o)
    _insert_handles(rd, hr, o)


def p_minsert(rd, r, hr, o):
    _insert_common(rd, r, o)
    d = o.data
    d["cols"] = r.bs()
    d["rows"] = r.bs()
    d["col_spacing"] = r.bd()
    d["row_spacing"] = r.bd()
    _insert_handles(rd, hr, o)


def p_vertex2d(rd, r, hr, o):
    d = o.data
    d["flags"] = r.rc()
    d["point"] = r.bd3()
    sw = r.bd()
    if sw < 0:
        sw = ew = -sw
    else:
        ew = r.bd()
    d["start_width"], d["end_width"] = sw, ew
    d["bulge"] = r.bd()
    if rd.ver >= R2010:
        r.bl()
    d["tangent"] = r.bd()


def p_vertex3d(rd, r, hr, o):
    o.data["flags"] = r.rc()
    o.data["point"] = r.bd3()


def p_pface_face(rd, r, hr, o):
    o.data["indices"] = (r.bs(), r.bs(), r.bs(), r.bs())


def _owned_vertices(rd, hr, o, count):
    d = o.data
    h = o.handle
    verts = []
    if rd.ver <= R2000:
        d["first_vertex"] = hr.href(h)
        d["last_vertex"] = hr.href(h)
    else:
        for _ in range(_cap(count, 10_000_000)):
            verts.append(hr.href(h))
    d["vertices"] = verts
    d["seqend"] = hr.href(h)


def p_polyline2d(rd, r, hr, o):
    d = o.data
    d["flags"] = r.bs()
    d["curve_type"] = r.bs()
    d["start_width"] = r.bd()
    d["end_width"] = r.bd()
    d["thickness"] = r.bt()
    d["elevation"] = r.bd()
    d["extrusion"] = r.be()
    n = r.bl() if rd.ver >= R2004 else 0
    _owned_vertices(rd, hr, o, n)


def p_polyline3d(rd, r, hr, o):
    d = o.data
    d["spline_flags"] = r.rc()
    d["closed_flags"] = r.rc()
    n = r.bl() if rd.ver >= R2004 else 0
    _owned_vertices(rd, hr, o, n)


def p_polyline_pface(rd, r, hr, o):
    d = o.data
    d["num_verts"] = r.bs()
    d["num_faces"] = r.bs()
    n = r.bl() if rd.ver >= R2004 else 0
    _owned_vertices(rd, hr, o, n)


def p_polyline_mesh(rd, r, hr, o):
    d = o.data
    d["flags"] = r.bs()
    d["curve_type"] = r.bs()
    d["m"] = r.bs()
    d["n"] = r.bs()
    d["m_density"] = r.bs()
    d["n_density"] = r.bs()
    n = r.bl() if rd.ver >= R2004 else 0
    _owned_vertices(rd, hr, o, n)


def p_arc(rd, r, hr, o):
    d = o.data
    d["center"] = r.bd3()
    d["radius"] = r.bd()
    d["thickness"] = r.bt()
    d["extrusion"] = r.be()
    d["start_angle"] = r.bd()
    d["end_angle"] = r.bd()


def p_circle(rd, r, hr, o):
    d = o.data
    d["center"] = r.bd3()
    d["radius"] = r.bd()
    d["thickness"] = r.bt()
    d["extrusion"] = r.be()


def p_line(rd, r, hr, o):
    d = o.data
    if rd.ver >= R2000:
        zz = r.b()
        x1 = r.rd()
        x2 = r.dd(x1)
        y1 = r.rd()
        y2 = r.dd(y1)
        z1 = z2 = 0.0
        if not zz:
            z1 = r.rd()
            z2 = r.dd(z1)
        d["start"], d["end"] = (x1, y1, z1), (x2, y2, z2)
    else:
        d["start"], d["end"] = r.bd3(), r.bd3()
    d["thickness"] = r.bt()
    d["extrusion"] = r.be()


def _dim_common(rd, r, o):
    d = o.data
    if rd.ver >= R2010:
        r.rc()
    d["extrusion"] = r.bd3()
    d["text_mid"] = r.rd2()
    d["elevation"] = r.bd()
    d["flags1"] = r.rc()
    d["text"] = r.tv()
    d["text_rotation"] = r.bd()
    d["horizontal_direction"] = r.bd()
    d["ins_scale"] = r.bd3()
    d["ins_rotation"] = r.bd()
    if rd.ver >= R2000:
        d["attachment"] = r.bs()
        d["linespacing_style"] = r.bs()
        d["linespacing"] = r.bd()
        d["measurement"] = r.bd()
    if rd.ver >= R2007:
        r.b(); r.b(); r.b()
    d["pt12"] = r.rd2()


def _dim_handles(hr, o):
    o.data["dimstyle"] = hr.href(o.handle)
    o.data["block"] = hr.href(o.handle)


def p_dim_ordinate(rd, r, hr, o):
    _dim_common(rd, r, o)
    d = o.data
    d["pt10"], d["pt13"], d["pt14"] = r.bd3(), r.bd3(), r.bd3()
    d["flags2"] = r.rc()
    d["dimtype"] = 6
    _dim_handles(hr, o)


def p_dim_linear(rd, r, hr, o):
    _dim_common(rd, r, o)
    d = o.data
    d["pt13"], d["pt14"], d["pt10"] = r.bd3(), r.bd3(), r.bd3()
    d["oblique"] = r.bd()
    d["angle"] = r.bd()
    d["dimtype"] = 0
    _dim_handles(hr, o)


def p_dim_aligned(rd, r, hr, o):
    _dim_common(rd, r, o)
    d = o.data
    d["pt13"], d["pt14"], d["pt10"] = r.bd3(), r.bd3(), r.bd3()
    d["oblique"] = r.bd()
    d["dimtype"] = 1
    _dim_handles(hr, o)


def p_dim_ang3pt(rd, r, hr, o):
    _dim_common(rd, r, o)
    d = o.data
    d["pt10"], d["pt13"], d["pt14"], d["pt15"] = r.bd3(), r.bd3(), r.bd3(), r.bd3()
    d["dimtype"] = 5
    _dim_handles(hr, o)


def p_dim_ang2ln(rd, r, hr, o):
    _dim_common(rd, r, o)
    d = o.data
    d["pt16"] = r.rd2()
    d["pt13"], d["pt14"], d["pt15"], d["pt10"] = r.bd3(), r.bd3(), r.bd3(), r.bd3()
    d["dimtype"] = 2
    _dim_handles(hr, o)


def p_dim_radius(rd, r, hr, o):
    _dim_common(rd, r, o)
    d = o.data
    d["pt10"], d["pt15"] = r.bd3(), r.bd3()
    d["leader_len"] = r.bd()
    d["dimtype"] = 4
    _dim_handles(hr, o)


def p_dim_diameter(rd, r, hr, o):
    _dim_common(rd, r, o)
    d = o.data
    d["pt15"], d["pt10"] = r.bd3(), r.bd3()
    d["leader_len"] = r.bd()
    d["dimtype"] = 3
    _dim_handles(hr, o)


def p_point(rd, r, hr, o):
    d = o.data
    d["point"] = r.bd3()
    d["thickness"] = r.bt()
    d["extrusion"] = r.be()
    d["x_angle"] = r.bd()


def p_3dface(rd, r, hr, o):
    d = o.data
    if rd.ver >= R2000:
        noflags = r.b()
        zz = r.b()
        x, y = r.rd(), r.rd()
        z = 0.0 if zz else r.rd()
        c1 = (x, y, z)
        c2 = r.dd3(c1)
        c3 = r.dd3(c2)
        c4 = r.dd3(c3)
        d["invisible_edges"] = 0 if noflags else r.bs()
    else:
        c1, c2, c3, c4 = r.bd3(), r.bd3(), r.bd3(), r.bd3()
        d["invisible_edges"] = r.bs()
    d["vtx"] = (c1, c2, c3, c4)


def p_solid(rd, r, hr, o):
    d = o.data
    d["thickness"] = r.bt()
    z = r.bd()
    pts = [r.rd2() for _ in range(4)]
    d["vtx"] = tuple((x, y, z) for x, y in pts)
    d["extrusion"] = r.be()


def p_viewport(rd, r, hr, o):
    d = o.data
    d["center"] = r.bd3()
    d["width"] = r.bd()
    d["height"] = r.bd()
    if rd.ver >= R2000:
        d["view_target"] = r.bd3()
        d["view_direction"] = r.bd3()
        d["view_twist"] = r.bd()
        d["view_height"] = r.bd()
        d["lens"] = r.bd()
        d["front_clip"] = r.bd()
        d["back_clip"] = r.bd()
        d["snap_angle"] = r.bd()
        d["view_center"] = r.rd2()
        d["snap_base"] = r.rd2()
        d["snap_spacing"] = r.rd2()
        d["grid_spacing"] = r.rd2()
        d["circle_zoom"] = r.bs()
        if rd.ver >= R2007:
            r.bs()
        d["frozen_count"] = r.bl()
        d["status"] = r.bl()
        d["style_sheet"] = r.tv()
        d["render_mode"] = r.rc()


def p_ellipse(rd, r, hr, o):
    d = o.data
    d["center"] = r.bd3()
    d["major_axis"] = r.bd3()
    d["extrusion"] = r.bd3()
    d["ratio"] = r.bd()
    d["start"] = r.bd()
    d["end"] = r.bd()


def p_spline(rd, r, hr, o):
    d = o.data
    scen = r.bl()
    if rd.ver >= R2013:
        f1 = r.bl()
        kp = r.bl()
        d["flags1"], d["knot_param"] = f1, kp
        scen = 1 if kp == 15 or not f1 & 1 else 2
        d["closed_flag13"] = bool(f1 & 4)
    d["scenario"] = scen
    d["degree"] = r.bl()
    nfit = nknots = nctrl = 0
    weighted = False
    if scen == 2:
        d["fit_tol"] = r.bd()
        d["start_tangent"] = r.bd3()
        d["end_tangent"] = r.bd3()
        nfit = r.bl()
    elif scen == 1:
        d["rational"] = r.b()
        d["closed"] = r.b()
        d["periodic"] = r.b()
        d["knot_tol"] = r.bd()
        d["ctrl_tol"] = r.bd()
        nknots = r.bl()
        nctrl = r.bl()
        weighted = bool(r.b())
    else:
        raise DwgReadError(f"spline scenario {scen} unknown")
    d["knots"] = [r.bd() for _ in range(_cap(nknots, 1_000_000))]
    ctrl, w = [], []
    for _ in range(_cap(nctrl, 1_000_000)):
        ctrl.append(r.bd3())
        if weighted:
            w.append(r.bd())
    d["ctrl"] = ctrl
    d["weights"] = w
    d["fit"] = [r.bd3() for _ in range(_cap(nfit, 1_000_000))]


def p_ray(rd, r, hr, o):
    o.data["point"] = r.bd3()
    o.data["vector"] = r.bd3()


def p_mtext(rd, r, hr, o):
    d = o.data
    d["insert"] = r.bd3()
    d["extrusion"] = r.bd3()
    d["x_axis"] = r.bd3()
    d["rect_width"] = r.bd()
    if rd.ver >= R2007:
        d["rect_height"] = r.bd()
    d["height"] = r.bd()
    d["attachment"] = r.bs()
    d["direction"] = r.bs()
    r.bd(); r.bd()
    d["text"] = r.tv()
    d["style"] = hr.href(o.handle)
    if rd.ver >= R2000:
        d["linespacing_style"] = r.bs()
        d["linespacing"] = r.bd()
        r.b()


def p_leader(rd, r, hr, o):
    d = o.data
    d["annotation"] = hr.href(o.handle)
    d["dimstyle"] = hr.href(o.handle)
    r.b()
    d["annot_type"] = r.bs()
    d["path_type"] = r.bs()
    n = _cap(r.bl(), 1_000_000)
    d["points"] = [r.bd3() for _ in range(n)]
    r.bd3()  # origin
    d["extrusion"] = r.bd3()
    d["x_dir"] = r.bd3()
    r.bd3()  # offset to block insertion point
    if rd.ver >= R14:
        r.bd3()  # end point projection
    d["box_height"] = d["box_width"] = 0.0
    d["hookline"] = d["arrowhead"] = 1
    # The tail (box size, hook/arrowhead flags) does not follow the §20.4.47 layout in R2007+ files we
    # tested; it is optional for drawing, so decode it best-effort only.
    save = r.pos
    try:
        if rd.ver <= R14:
            r.bd()
        bh, bw = r.bd(), r.bd()
        hook, arrow = r.b(), r.b()
        if all(map(math.isfinite, (bh, bw))) and abs(bh) < 1e12 and abs(bw) < 1e12:
            d.update(box_height=bh, box_width=bw, hookline=hook, arrowhead=arrow)
    except DwgReadError:
        r.pos = save


def p_lwpolyline(rd, r, hr, o):
    d = o.data
    f = r.bs()
    d["flags"] = f
    d["const_width"] = r.bd() if f & 4 else 0.0
    d["elevation"] = r.bd() if f & 8 else 0.0
    d["thickness"] = r.bd() if f & 2 else 0.0
    d["extrusion"] = r.bd3() if f & 1 else (0.0, 0.0, 1.0)
    n = _cap(r.bl(), 10_000_000)
    nb = _cap(r.bl(), 10_000_000) if f & 16 else 0
    nid = _cap(r.bl(), 10_000_000) if rd.ver >= R2010 and f & 1024 else 0
    nw = _cap(r.bl(), 10_000_000) if f & 32 else 0
    pts = []
    if n:
        if rd.ver >= R2000:
            x, y = r.rd(), r.rd()
            pts.append((x, y))
            for _ in range(n - 1):
                x = r.dd(x)
                y = r.dd(y)
                pts.append((x, y))
        else:
            pts = [r.rd2() for _ in range(n)]
    d["points"] = pts
    d["bulges"] = [r.bd() for _ in range(nb)]
    for _ in range(nid):
        r.bl()
    d["widths"] = [(r.bd(), r.bd()) for _ in range(nw)]


def p_hatch(rd, r, hr, o):
    d = o.data
    ver = rd.ver
    if ver >= R2004:
        grad = r.bl()
        r.bl(); r.bd(); r.bd(); r.bl(); r.bd()
        ncol = _cap(r.bl(), 1000)
        for _ in range(ncol):
            r.bd(); r.bs(); r.bl(); r.rc()
        gname = r.tv()
        d["gradient"] = (grad, gname)
    d["elevation"] = r.bd()
    d["extrusion"] = r.bd3()
    d["pattern_name"] = r.tv()
    d["solid"] = r.b()
    d["associative"] = r.b()
    paths = []
    nboundary = 0
    any_pixel = False
    for _ in range(_cap(r.bl(), 100000)):
        pflag = r.bl()
        if pflag & 4:
            any_pixel = True
        path: dict[str, Any] = {"flags": pflag}
        if not pflag & 2:
            edges = []
            for _ in range(_cap(r.bl(), 1_000_000)):
                et = r.rc()
                if et == 1:
                    edges.append(("line", r.rd2(), r.rd2()))
                elif et == 2:
                    edges.append(("arc", r.rd2(), r.bd(), r.bd(), r.bd(), r.b()))
                elif et == 3:
                    edges.append(("ellipse", r.rd2(), r.rd2(), r.bd(), r.bd(), r.bd(), r.b()))
                elif et == 4:
                    deg = r.bl()
                    rat = r.b()
                    per = r.b()
                    nk = _cap(r.bl(), 1_000_000)
                    nc = _cap(r.bl(), 1_000_000)
                    knots = [r.bd() for _ in range(nk)]
                    cps, ws = [], []
                    for _ in range(nc):
                        cps.append(r.rd2())
                        if rat:
                            ws.append(r.bd())
                    fit = []
                    if ver >= R2010:
                        nf = _cap(r.bl(), 1_000_000)
                        fit = [r.rd2() for _ in range(nf)]
                        if nf:
                            r.rd2(); r.rd2()
                    edges.append(("spline", deg, rat, per, knots, cps, ws, fit))
                else:
                    raise DwgReadError(f"hatch edge type {et} unknown")
            path["edges"] = edges
        else:
            bulges = r.b()
            closed = r.b()
            pts = []
            for _ in range(_cap(r.bl(), 1_000_000)):
                pt = r.rd2()
                b = r.bd() if bulges else 0.0
                pts.append((pt[0], pt[1], b))
            path["polyline"] = (pts, closed)
        nb = _cap(r.bl(), 1_000_000)
        nboundary += nb
        paths.append(path)
    d["paths"] = paths
    d["style"] = r.bs()
    d["pattern_type"] = r.bs()
    lines = []
    if not d["solid"]:
        d["angle"] = r.bd()
        d["scale"] = r.bd()
        d["double"] = r.b()
        for _ in range(_cap(r.bs(), 10000)):
            ang = r.bd()
            base = r.bd2()
            off = r.bd2()
            dashes = [r.bd() for _ in range(_cap(r.bs(), 10000))]
            lines.append((ang, base, off, dashes))
    d["pattern_lines"] = lines
    if any_pixel:
        r.bd()
    for _ in range(_cap(r.bl(), 1_000_000)):
        r.rd2()


def p_image(rd, r, hr, o):
    """IMAGE (§20.4.80); WIPEOUT uses the same record layout."""
    d = o.data
    d["class_version"] = r.bl()
    d["insert"] = r.bd3()
    d["u"] = r.bd3()
    d["v"] = r.bd3()
    d["size"] = r.rd2()
    d["display"] = r.bs()
    d["clipping"] = r.b()
    d["brightness"] = r.rc()
    d["contrast"] = r.rc()
    d["fade"] = r.rc()
    if rd.ver >= R2010:
        d["clip_mode"] = r.b()
    d["clip_type"] = r.bs()
    if d["clip_type"] == 1:
        d["clip"] = [r.rd2(), r.rd2()]
    else:
        d["clip"] = [r.rd2() for _ in range(_cap(r.bl(), 1_000_000))]
    d["imagedef"] = hr.href(o.handle)


def p_tolerance(rd, r, hr, o):
    """TOLERANCE (§20.4.49)."""
    d = o.data
    if rd.ver <= R14:
        r.bs(); r.bd(); r.bd()
    d["insert"] = r.bd3()
    d["x_dir"] = r.bd3()
    d["extrusion"] = r.bd3()
    d["text"] = r.tv()
    d["dimstyle"] = hr.href(o.handle)


def p_arc_dimension(rd, r, hr, o):
    """ARC_DIMENSION (§20.4.19)."""
    _dim_common(rd, r, o)
    d = o.data
    d["pt10"], d["pt13"], d["pt14"], d["pt15"] = r.bd3(), r.bd3(), r.bd3(), r.bd3()
    d["is_partial"] = r.b()
    d["arc_start"], d["arc_end"] = r.bd(), r.bd()
    d["has_leader"] = r.b()
    d["leader1"], d["leader2"] = r.bd3(), r.bd3()
    _dim_handles(hr, o)


def p_large_radial(rd, r, hr, o):
    """LARGE_RADIAL_DIMENSION (§20.4.30)."""
    _dim_common(rd, r, o)
    d = o.data
    d["pt10"], d["pt13"] = r.bd3(), r.bd3()
    r.bd()
    d["pt14"], d["pt15"] = r.bd3(), r.bd3()
    _dim_handles(hr, o)


# ====================================================================== table objects
def _table_entry_common(rd, r, o):
    d = o.data
    d["name"] = r.tv()
    d["flag64"] = r.b()
    if rd.ver < R2007:
        d["xref_index"] = r.bs() - 1
    else:
        # R2007+ files carry no "xrefindex+1" BS here: the table-specific data starts 2 bits after the
        # common object data (verified on R2010/R2013/R2018 LAYER and BLOCK_HEADER records).
        d["xref_index"] = -1
    d["xdep"] = r.b()


def p_control(rd, r, hr, o):
    n = _cap(r.bl(), 10_000_000)
    entries = [hr.href(o.handle) for _ in range(n)]
    extra = []
    try:
        while hr.remaining() >= 8:
            extra.append(hr.href(o.handle))
    except DwgReadError:
        pass
    o.data["entries"] = entries
    o.data["extra"] = extra


def p_block_header(rd, r, hr, o):
    d = o.data
    ver = rd.ver
    _table_entry_common(rd, r, o)
    d["anonymous"] = r.b()
    d["has_attdefs"] = r.b()
    d["is_xref"] = r.b()
    d["xref_overlaid"] = r.b()
    if ver >= R2000:
        d["loaded"] = r.b()
    owned = r.bl() if ver >= R2004 else 0
    d["base"] = r.bd3()
    d["xref_path"] = r.tv()
    ninserts = 0
    if ver >= R2000:
        while True:
            c = r.rc()
            if c == 0:
                break
            ninserts += 1
            if ninserts > 1_000_000:
                raise DwgReadError("insert count list does not terminate")
        d["description"] = r.tv()
        psize = _cap(r.bl(), 100_000_000)
        r.pos += psize * 8
    if ver >= R2007:
        d["insunits"] = r.bs()
        d["explodable"] = r.b()
        d["scaling"] = r.rc()
    h = o.handle
    hr.href(h)  # NULL hard pointer
    d["block_entity"] = hr.href(h)
    ents = []
    if ver <= R2000:
        if not d["is_xref"] and not d["xref_overlaid"]:
            d["first_entity"] = hr.href(h)
            d["last_entity"] = hr.href(h)
    else:
        for _ in range(_cap(owned, 10_000_000)):
            ents.append(hr.href(h))
    d["entities"] = ents
    d["endblk"] = hr.href(h)
    if ver >= R2000:
        for _ in range(ninserts):
            hr.href(h)
        d["layout"] = hr.href(h)


def p_layer(rd, r, hr, o):
    d = o.data
    _table_entry_common(rd, r, o)
    if rd.ver >= R2000:
        v = r.bs()
        d["frozen"] = bool(v & 1)
        d["off"] = bool(v & 2)
        d["frozen_new_vp"] = bool(v & 4)
        d["locked"] = bool(v & 8)
        d["plot"] = bool(v & 16)
        d["lineweight_idx"] = (v & 0x3E0) >> 5
    else:
        d["frozen"] = bool(r.b())
        d["off"] = not r.b()
        d["frozen_new_vp"] = bool(r.b())
        d["locked"] = bool(r.b())
        d["plot"] = True
        d["lineweight_idx"] = 31
    d["color"] = r.cmc()
    h = o.handle
    hr.href(h)  # xref block
    if rd.ver >= R2000:
        hr.href(h)  # plotstyle
    if rd.ver >= R2007:
        hr.href(h)  # material
    d["ltype"] = hr.href(h)


def p_style(rd, r, hr, o):
    d = o.data
    _table_entry_common(rd, r, o)
    d["vertical"] = r.b()
    d["is_shape"] = r.b()
    d["fixed_height"] = r.bd()
    d["width"] = r.bd()
    d["oblique"] = r.bd()
    d["generation"] = r.rc()
    d["last_height"] = r.bd()
    d["font"] = r.tv()
    d["bigfont"] = r.tv()


def p_ltype(rd, r, hr, o):
    d = o.data
    _table_entry_common(rd, r, o)
    d["description"] = r.tv()
    d["pattern_length"] = r.bd()
    d["align"] = r.rc()
    dashes = []
    for _ in range(r.rc()):
        length = r.bd()
        r.bs()
        r.rd(); r.rd()
        r.bd(); r.bd()
        flag = r.bs()
        dashes.append((length, flag))
    d["dashes"] = dashes


def p_named_only(rd, r, hr, o):
    _table_entry_common(rd, r, o)


def p_dictionary(rd, r, hr, o):
    d = o.data
    n = _cap(r.bl(), 10_000_000)
    if rd.ver == R14:
        r.rc()
    if rd.ver >= R2000:
        r.bs(); r.rc()
    names = [r.tv() for _ in range(n)]
    items = [hr.href(o.handle) for _ in range(n)]
    d["items"] = dict(zip(names, items))


def p_layout(rd, r, hr, o):
    d = o.data
    ver = rd.ver
    d["page_setup"] = r.tv()
    d["printer"] = r.tv()
    d["plot_flags"] = r.bs()
    d["margins"] = (r.bd(), r.bd(), r.bd(), r.bd())
    d["paper_size_mm"] = (r.bd(), r.bd())
    d["paper_name"] = r.tv()
    d["plot_origin"] = r.bd2()
    d["paper_units"] = r.bs()
    d["plot_rotation"] = r.bs()
    d["plot_type"] = r.bs()
    d["window_min"] = r.bd2()
    d["window_max"] = r.bd2()
    if ver <= R2000:
        r.t8() if ver >= R2007 else r.tv()
    d["real_units"] = r.bd()
    d["drawing_units"] = r.bd()
    d["style_sheet"] = r.tv()
    d["scale_type"] = r.bs()
    d["scale_factor"] = r.bd()
    d["paper_image_origin"] = r.bd2()
    if ver >= R2004:
        r.bs(); r.bs(); r.bs()
    d["name"] = r.tv()
    d["tab_order"] = r.bl()
    d["flags"] = r.bs()
    d["ucs_origin"] = r.bd3()
    d["limmin"] = r.rd2()
    d["limmax"] = r.rd2()
    d["insbase"] = r.bd3()
    r.bd3(); r.bd3()
    r.bd()
    r.bs()
    d["extmin"] = r.bd3()
    d["extmax"] = r.bd3()
    nvp = _cap(r.bl(), 100000) if ver >= R2004 else 0  # spec says RL; files store a BL
    h = o.handle
    if ver >= R2004:
        hr.href(h)
    if ver >= R2007:
        hr.href(h)
    d["block_record"] = hr.href(h)
    d["last_viewport"] = hr.href(h)
    hr.href(h); hr.href(h)
    d["viewports"] = [hr.href(h) for _ in range(nvp)]


_PARSERS: dict[str, Callable] = {
    "TEXT": p_text, "ATTRIB": p_attrib, "ATTDEF": p_attdef, "BLOCK": p_block, "ENDBLK": p_nothing,
    "SEQEND": p_nothing, "INSERT": p_insert, "MINSERT": p_minsert, "VERTEX_2D": p_vertex2d,
    "VERTEX_3D": p_vertex3d, "VERTEX_MESH": p_vertex3d, "VERTEX_PFACE": p_vertex3d,
    "VERTEX_PFACE_FACE": p_pface_face, "POLYLINE_2D": p_polyline2d, "POLYLINE_3D": p_polyline3d,
    "POLYLINE_PFACE": p_polyline_pface, "POLYLINE_MESH": p_polyline_mesh, "ARC": p_arc, "CIRCLE": p_circle,
    "LINE": p_line, "DIMENSION_ORDINATE": p_dim_ordinate, "DIMENSION_LINEAR": p_dim_linear,
    "DIMENSION_ALIGNED": p_dim_aligned, "DIMENSION_ANG3PT": p_dim_ang3pt, "DIMENSION_ANG2LN": p_dim_ang2ln,
    "DIMENSION_RADIUS": p_dim_radius, "DIMENSION_DIAMETER": p_dim_diameter, "POINT": p_point,
    "3DFACE": p_3dface, "SOLID": p_solid, "TRACE": p_solid, "VIEWPORT": p_viewport, "ELLIPSE": p_ellipse,
    "SPLINE": p_spline, "RAY": p_ray, "XLINE": p_ray, "MTEXT": p_mtext, "LEADER": p_leader,
    "LWPOLYLINE": p_lwpolyline, "HATCH": p_hatch,
    "BLOCK_CONTROL": p_control, "LAYER_CONTROL": p_control, "STYLE_CONTROL": p_control,
    "LTYPE_CONTROL": p_control, "DIMSTYLE_CONTROL": p_control, "APPID_CONTROL": p_control,
    "BLOCK_HEADER": p_block_header, "LAYER": p_layer, "STYLE": p_style, "LTYPE": p_ltype,
    "DIMSTYLE": p_named_only, "APPID": p_named_only, "DICTIONARY": p_dictionary,
    "DICTIONARYWDFLT": p_dictionary, "LAYOUT": p_layout, "IMAGE": p_image, "WIPEOUT": p_image,
    "TOLERANCE": p_tolerance, "ARC_DIMENSION": p_arc_dimension, "LARGE_RADIAL_DIMENSION": p_large_radial,
}
