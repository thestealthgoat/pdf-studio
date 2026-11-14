"""Native DWG writer (pdfcore.ops.plans.dwg.writer): ezdxf document → R2000 (AC1015) DWG.

Always-run tests: bit codes against the reader, file structure (sentinels, CRCs, section locators, object
map sections), round trips of documents built here through our own reader, robustness (empty documents,
text outside code page 1252, huge/non-finite coordinates), engine wiring and PDF → DXF → DWG → PDF.

Local-only tests skip when their inputs are absent: the AutoCAD sample corpus
(``../pdf-studio-local/corpus/dwg`` or ``$PDFCORE_DWG_CORPUS``) and LibreDWG's ``dwgread``/``dwg2dxf``
binaries, which are used purely as black-box validators (GPL tools, never a dependency).
"""

from __future__ import annotations

import math
import os
import random
import shutil
import struct
import subprocess
from collections import Counter
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from pdfcore.ops.plans.dwg import read_dwg  # noqa: E402
from pdfcore.ops.plans.dwg.bits import BitReader, crc8, mc_bytes, ms_bytes  # noqa: E402
from pdfcore.ops.plans.dwg.bitwriter import BitWriter, encode_8bit, mc_encode, ms_encode  # noqa: E402
from pdfcore.ops.plans.dwg.container import SENT_CLASSES, SENT_HEADER, open_container, parse_object_map  # noqa: E402
from pdfcore.ops.plans.dwg.writer import MAP_SECTION_MAX, write_dwg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(os.environ.get("PDFCORE_DWG_CORPUS", ROOT.parent / "pdf-studio-local" / "corpus" / "dwg"))
DWGREAD = shutil.which("dwgread") or ("/opt/homebrew/bin/dwgread" if Path("/opt/homebrew/bin/dwgread").exists() else None)
DWG2DXF = shutil.which("dwg2dxf") or ("/opt/homebrew/bin/dwg2dxf" if Path("/opt/homebrew/bin/dwg2dxf").exists() else None)
TOL = 1e-9


# ====================================================================== bit codes: writer ↔ reader
def _roundtrip(write, read, values):
    w = BitWriter()
    for v in values:
        write(w, v)
    r = BitReader(w.getvalue())
    return [read(r) for _ in values]


def test_bitshort_bitlong_round_trip_and_spec_example():
    vals = [0, 1, 15, 255, 256, 257, 4096, 32767, -1, -32768]
    assert _roundtrip(BitWriter.bs, BitReader.bs, vals) == vals
    lvals = [0, 1, 255, 256, 70000, 2**31 - 1, -5]
    assert _roundtrip(BitWriter.bl, BitReader.bl, lvals) == lvals
    # §2.2 worked example: 257, 0, 256, 15, 0 encode to these 34 bits
    w = BitWriter()
    for v in (257, 0, 256, 15, 0):
        w.bs(v)
    assert w.getvalue() == bytes([0b00000000, 0b01000000, 0b01101101, 0b00001111, 0b10000000])


def test_doubles_round_trip_bitwise():
    rnd = random.Random(7)
    vals = [0.0, 1.0, -0.0, 2.5, 1e300, -1e-300, 5e-324, math.pi] + [rnd.uniform(-1e9, 1e9) for _ in range(50)]
    got = _roundtrip(BitWriter.bd, BitReader.bd, vals)
    assert [struct.pack("<d", v) for v in got] == [struct.pack("<d", v) for v in vals]
    assert _roundtrip(BitWriter.rd, BitReader.rd, vals) == vals


def test_bitdouble_with_default_all_four_forms():
    pairs = [(5.0, 5.0), (5.000000000001, 5.0), (5.5, 5.0), (-123456.75, 7.0), (1.0, 1.0), (0.0, 1.0)]
    w = BitWriter()
    for v, d in pairs:
        w.dd(v, d)
    r = BitReader(w.getvalue())
    assert [r.dd(d) for _v, d in pairs] == [v for v, _d in pairs]


def test_raw_types_extrusion_thickness_handles_text():
    w = BitWriter()
    w.b(1)
    w.bb(2)
    w.rc(0xAB)
    w.rs(-2)
    w.rl(0x12345678)
    w.be((0.0, 0.0, 1.0))
    w.be((0.0, 1.0, 0.0))
    w.bt(0.0)
    w.bt(3.5)
    w.h(5, 0x5E7)
    w.h(4, 0)
    w.tv("Grüße 東")
    r = BitReader(w.getvalue())
    assert (r.b(), r.bb(), r.rc(), r.rs(), r.rlu()) == (1, 2, 0xAB, -2, 0x12345678)
    assert r.be() == (0.0, 0.0, 1.0) and r.be() == (0.0, 1.0, 0.0)
    assert r.bt() == 0.0 and r.bt() == 3.5
    assert r.h() == (5, 0x5E7) and r.h() == (4, 0)
    assert r.tv() == "Grüße 東"  # \U+6771 escape decoded by the reader
    # §2.13 worked example: 5.2.05E7 = 01010010 00000101 11100111
    w = BitWriter()
    w.h(5, 0x5E7)
    assert w.getvalue() == bytes([0b01010010, 0b00000101, 0b11100111])


def test_modular_char_and_short():
    for v in (0, 1, 63, 64, 127, 4610, 112823273, 2**40):
        assert mc_bytes(mc_encode(v, signed=False), 0, signed=False)[0] == v
    for v in (0, 1, -1, -1413, 63, -64, 112823273, -2**33):
        assert mc_bytes(mc_encode(v), 0)[0] == v
    assert mc_encode(-1413) == bytes([0b10000101, 0b01001011])  # §2.6 example
    for v in (0, 1, 0x7FFF, 0x8000, 4650033, 2**40):
        assert ms_bytes(ms_encode(v), 0)[0] == v


def test_encode_8bit_reports_substitutions():
    st: Counter = Counter()
    assert encode_8bit("ok €", "cp1252", st) == "ok €".encode("cp1252")
    assert encode_8bit("π😀", "cp1252", st) == b"\\U+03C0?"
    assert st == Counter(escaped=1, replaced=1)


# ====================================================================== documents built here
def _doc():
    doc = ezdxf.new("R2000", setup=True)
    doc.header["$INSUNITS"] = 6
    doc.header["$LTSCALE"] = 2.5
    doc.layers.add("WALLS", color=1, linetype="DASHED", lineweight=50)
    doc.layers.add("HIDDEN_L", color=3, linetype="HIDDEN")
    doc.layers.add("OFF", color=4).off()
    doc.layers.add("FROZEN", color=5).freeze()
    doc.layers.add("LOCKED", color=6).lock()
    doc.styles.add("ROMANS", font="romans.shx")
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 5), dxfattribs={"layer": "WALLS"})
    msp.add_line((0.1, 0.2, 1.5), (10.25, -5.5, 2.0), dxfattribs={"color": 2, "linetype": "CENTER", "lineweight": 35})
    msp.add_circle((5, 5), 3, dxfattribs={"layer": "OFF"})
    msp.add_arc((1, 2), 4, 30, 200)
    msp.add_ellipse((3, 3), (4, 1), 0.5, 0.2, 5.0)
    msp.add_point((7, 7, 1))
    msp.add_solid([(0, 0), (1, 0), (0, 1), (1, 1)])
    msp.add_lwpolyline([(0, 0, 0, 0, 0.5), (5, 0, 0, 0, 0), (5, 5, 0, 0, -0.3)], format="xyseb", close=True,
                       dxfattribs={"layer": "HIDDEN_L"})
    msp.add_lwpolyline([(0, 0), (1, 1), (2, 0)], dxfattribs={"const_width": 0.2})
    msp.add_polyline2d([(0, 0), (2, 1), (3, 4)], dxfattribs={"layer": "WALLS"})
    msp.add_polyline3d([(0, 0, 0), (2, 1, 1), (3, 4, 2)])
    msp.add_spline([(0, 0), (2, 3), (5, 1), (8, 4)])
    msp.add_spline(fit_points=[(0, 0), (2, 3), (5, 1), (8, 4)])
    msp.add_text("Hello TEXT", height=2.5, dxfattribs={"rotation": 30, "style": "ROMANS"}).set_placement((1, 1))
    msp.add_text("Centered", height=1).set_placement((10, 10), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)
    msp.add_mtext("Line one\\Psecond line", dxfattribs={"char_height": 1.5, "insert": (20, 20), "width": 30})
    h = msp.add_hatch(color=2)
    h.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True)
    h2 = msp.add_hatch(color=3)
    ep = h2.paths.add_edge_path()
    ep.add_line((0, 0), (10, 0))
    ep.add_arc((10, 5), 5, -90, 90)
    ep.add_line((10, 10), (0, 10))
    ep.add_ellipse((0, 5), (0, 5), 0.5, 90, 270)
    h3 = msp.add_hatch()
    h3.set_pattern_fill("ANSI31", scale=0.5)
    h3.paths.add_polyline_path([(20, 0), (30, 0), (30, 10)], is_closed=True)
    blk = doc.blocks.new("DOOR")
    blk.add_line((0, 0), (1, 0))
    blk.add_arc((0, 0), 1, 0, 90)
    blk.add_attdef("TAG1", (0, 1), dxfattribs={"height": 0.25, "prompt": "Door no."})
    ins = msp.add_blockref("DOOR", (50, 50), dxfattribs={"xscale": 2, "yscale": 2, "rotation": 45})
    ins.add_auto_attribs({"TAG1": "D-101"})
    msp.add_blockref("DOOR", (60, 50), dxfattribs={"xscale": 1.5, "yscale": -2, "zscale": 3})
    msp.add_linear_dim(base=(0, 20), p1=(0, 15), p2=(10, 15)).render()
    msp.add_radius_dim(center=(40, 40), radius=5, angle=45).render()
    msp.add_xline((0, 0), (1, 1))
    msp.add_ray((0, 0), (1, -1))
    msp.add_3dface([(0, 0, 0), (1, 0, 0), (1, 1, 1), (0, 1, 1)])
    pf = msp.add_polyface()
    pf.append_face([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
    pm = msp.add_polymesh((3, 3))
    for i in range(3):
        for j in range(3):
            pm.set_mesh_vertex((i, j), (i, j, (i * j) % 2))
    psp = doc.layouts.get("Layout1")
    psp.add_line((0, 0), (100, 100))
    psp.add_viewport((100, 100), (150, 100), (5, 5), 20)
    doc.layouts.new("Second").add_circle((10, 10), 5)
    return doc


def _close(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= TOL * max(1.0, abs(a), abs(b))
    la, lb = list(a), list(b)
    return len(la) == len(lb) and all(_close(x, y) for x, y in zip(la, lb))


def _flat(seq) -> list[float]:
    out: list[float] = []
    for p in seq:
        out.extend(p if hasattr(p, "__len__") else [p])
    return out


_ATTRS = ["start", "end", "center", "radius", "major_axis", "ratio", "start_param", "end_param", "location",
          "insert", "height", "text", "char_height", "width", "xscale", "yscale", "zscale", "name", "defpoint",
          "defpoint2", "defpoint3", "defpoint4", "text_midpoint", "layer", "color", "lineweight", "halign",
          "valign", "tag", "vtx0", "vtx1", "vtx2", "vtx3", "unit_vector", "view_height", "view_center_point",
          "style", "const_width"]
_ANGLES = ["start_angle", "end_angle", "rotation", "angle"]


def _check_entity(a, b) -> list[str]:
    bad = []
    t = a.dxftype()
    for k in _ATTRS + _ANGLES:
        if not a.dxf.is_supported(k) or not a.dxf.hasattr(k):
            continue
        va, vb = a.dxf.get(k), b.dxf.get(k)
        if k == "style" and va == "Standard" and vb is None:
            continue
        if k in _ANGLES and vb is not None and abs(((va - vb + 180) % 360) - 180) < 1e-9:
            continue
        if isinstance(va, str) or isinstance(vb, str) or va is None or vb is None:
            ok = va == vb
        else:
            ok = _close(va if not isinstance(va, (int, float)) else [va], vb if not isinstance(vb, (int, float)) else [vb])
        if not ok:
            bad.append(f"{t}.{k}: {va!r} != {vb!r}")
    if t == "LINE" and a.dxf.linetype.upper() != "BYLAYER" and a.dxf.linetype != b.dxf.linetype:
        bad.append(f"LINE linetype {a.dxf.linetype} != {b.dxf.linetype}")
    if t == "LWPOLYLINE":
        pa, pb = a.get_points("xyb"), b.get_points("xyb")
        if not _close(_flat(pa), _flat(pb)) or a.closed != b.closed:
            bad.append(f"LWPOLYLINE points {pa} != {pb}")
    if t == "SPLINE":
        for attr in ("control_points", "knots", "fit_points", "weights"):
            if not _close(_flat(getattr(a, attr)), _flat(getattr(b, attr))):
                bad.append(f"SPLINE {attr}")
    if t == "POLYLINE":
        va = [tuple(v.dxf.location) for v in a.vertices]
        vb = [tuple(v.dxf.location) for v in b.vertices]
        if a.get_mode() == "AcDb2dPolyline":
            va, vb = [p[:2] for p in va], [p[:2] for p in vb]
        if not _close(_flat(va), _flat(vb)) or a.get_mode() != b.get_mode():
            bad.append(f"POLYLINE vertices {va} != {vb}")
    if t == "MTEXT" and a.text != b.text:
        bad.append(f"MTEXT {a.text!r} != {b.text!r}")
    if t == "INSERT":
        ta = [(x.dxf.tag, x.dxf.text) for x in a.attribs]
        tb = [(x.dxf.tag, x.dxf.text) for x in b.attribs]
        if ta != tb:
            bad.append(f"INSERT attribs {ta} != {tb}")
    if t == "HATCH":
        if len(a.paths) != len(b.paths) or a.dxf.solid_fill != b.dxf.solid_fill:
            bad.append("HATCH paths/fill")
        for pa, pb in zip(a.paths, b.paths):
            if hasattr(pa, "vertices") and not _close(_flat(pa.vertices), _flat(pb.vertices)):
                bad.append("HATCH polyline path")
            if hasattr(pa, "edges") and [e.type for e in pa.edges] != [e.type for e in pb.edges]:
                bad.append("HATCH edges")
    return bad


def _compare(src, dst) -> list[str]:
    bad: list[str] = []
    for lay in src.layouts:
        other = dst.layouts.get(lay.name)
        ea, eb = list(lay), list(other)
        if [e.dxftype() for e in ea] != [e.dxftype() for e in eb]:
            bad.append(f"layout {lay.name}: {[e.dxftype() for e in ea]} != {[e.dxftype() for e in eb]}")
            continue
        for a, b in zip(ea, eb):
            bad += _check_entity(a, b)
    for blk in src.blocks:
        if blk.is_any_layout or blk.name.startswith("*"):
            continue
        assert blk.name in dst.blocks
        for a, b in zip(blk, dst.blocks.get(blk.name)):
            bad += _check_entity(a, b)
    for lay in src.layers:
        m = dst.layers.get(lay.dxf.name)
        if lay.dxf.hasattr("color") and lay.dxf.color != m.dxf.color:
            bad.append(f"layer {lay.dxf.name} color")
        if lay.dxf.hasattr("lineweight") and lay.dxf.lineweight != m.dxf.lineweight:
            bad.append(f"layer {lay.dxf.name} lineweight")
        if (lay.is_off(), lay.is_frozen(), lay.is_locked()) != (m.is_off(), m.is_frozen(), m.is_locked()):
            bad.append(f"layer {lay.dxf.name} state")
    return bad


def test_round_trip_through_native_reader(tmp_path):
    src = _doc()
    out = tmp_path / "rt.dwg"
    rep = write_dwg(src, out)
    assert out.read_bytes()[:6] == b"AC1015"
    assert rep.exploded == Counter() and rep.skipped == Counter()
    dst, rrep = read_dwg(out)
    assert rrep.version == "AC1015" and not rrep.warnings and not rrep.skipped
    assert _compare(src, dst) == []
    # tables
    assert {"WALLS", "HIDDEN_L", "OFF", "FROZEN", "LOCKED"} <= set(rrep.layers)
    assert dst.layers.get("WALLS").dxf.linetype == "DASHED"
    assert dst.layers.get("HIDDEN_L").dxf.linetype == "HIDDEN"   # standard definition added
    assert {"DASHED", "CENTER", "HIDDEN"} <= {lt.dxf.name for lt in dst.linetypes}
    assert "ROMANS" in dst.styles and dst.styles.get("ROMANS").dxf.font == "romans.shx"
    assert "DOOR" in rrep.blocks
    assert rrep.layouts == ["Model", "Layout1", "Second"]
    assert dst.header["$INSUNITS"] == 6 and dst.header["$LTSCALE"] == 2.5
    # dimensions keep their rendered geometry block
    for d in dst.modelspace().query("DIMENSION"):
        assert d.dxf.geometry in dst.blocks and len(dst.blocks.get(d.dxf.geometry)) > 0
    assert rep.entities["DIMENSION"] == 2 and rep.entities["VIEWPORT"] == 1


def test_extents_and_report(tmp_path):
    doc = ezdxf.new("R2000")
    doc.modelspace().add_line((-5, 2), (30, 40))
    out = tmp_path / "ext.dwg"
    rep = write_dwg(doc, out)
    dst, _ = read_dwg(out)
    assert _close(dst.header["$EXTMIN"], (-5, 2, 0)) and _close(dst.header["$EXTMAX"], (30, 40, 0))
    assert rep.entities == Counter(LINE=1) and "R2000" in rep.summary()
    with pytest.raises(ValueError, match="R2000"):
        write_dwg(doc, tmp_path / "x.dwg", version="R2018")


def test_file_structure_crcs_and_object_map_sections(tmp_path):
    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    for i in range(3000):
        msp.add_line((i, 0), (i, 1))
    out = tmp_path / "many.dwg"
    write_dwg(doc, out)
    data = out.read_bytes()
    n = struct.unpack_from("<i", data, 0x15)[0]
    assert n == 6
    end = 0x19 + 9 * n
    assert struct.unpack_from("<H", data, end)[0] == crc8(0, data[:end]) ^ 0x8461  # §3.2.6
    recs = {data[0x19 + 9 * i]: struct.unpack_from("<ii", data, 0x19 + 9 * i + 1) for i in range(n)}
    for k, sent in ((0, SENT_HEADER), (1, SENT_CLASSES)):
        off, size = recs[k]
        assert data[off:off + 16] == sent
        dsize = struct.unpack_from("<I", data, off + 16)[0]
        assert size == dsize + 38
        assert struct.unpack_from("<H", data, off + 20 + dsize)[0] == crc8(0xC0C1, data[off + 16:off + 20 + dsize])
    # object map: several sections, each ≤ 2032 bytes with a valid big-endian CRC, deltas per section
    off, size = recs[2]
    pos, sections = off, []
    while True:
        sz = (data[pos] << 8) | data[pos + 1]
        assert (data[pos + sz] << 8 | data[pos + sz + 1]) == crc8(0xC0C1, data[pos:pos + sz])
        if sz == 2:
            break
        assert sz <= MAP_SECTION_MAX
        sections.append(sz)
        pos += sz + 2
    assert len(sections) >= 2 and pos + 4 == off + size
    warnings: list[str] = []
    handles = parse_object_map(data, off, off + size, warnings)
    assert not warnings and len(handles) > 3000
    # every object's CRC (§20.1: the CRC includes the size bytes)
    for h, o in handles.items():
        osize, p = ms_bytes(data, o)
        assert struct.unpack_from("<H", data, p + osize)[0] == crc8(0xC0C1, data[o:p + osize]), hex(h)
    c = open_container(data)
    assert c.version == "AC1015" and not c.warnings
    # second header (chapter 26) between the map and the template, ending with its sentinel
    assert data.rfind(bytes([0xD4, 0x7B, 0x21, 0xCE])) > off
    tmpl = recs[4]
    assert tmpl[1] == 4 and tmpl[0] + 4 == len(data)
    dst, rr = read_dwg(out)
    assert rr.entities["LINE"] == 3000


def test_empty_document(tmp_path):
    out = tmp_path / "empty.dwg"
    rep = write_dwg(ezdxf.new("R2000"), out)
    dst, rr = read_dwg(out)
    assert rep.entities == Counter() and sum(rr.entities.values()) == 0 and not rr.warnings
    assert rr.layouts == ["Model", "Layout1"] and "0" in rr.layers


def test_document_without_paper_space_and_r12_source(tmp_path):
    doc = ezdxf.new("R12")
    doc.modelspace().add_line((0, 0), (1, 1))
    out = tmp_path / "r12.dwg"
    write_dwg(doc, out)
    dst, rr = read_dwg(out)
    assert rr.entities["LINE"] == 1


def test_unicode_text_outside_cp1252(tmp_path):
    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    msp.add_text("Grüße €100 — Ω≈π 東京", height=1)
    msp.add_mtext("emoji 😀 end")
    layer = doc.layers.add("Ärger_Ω")
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": layer.dxf.name})
    out = tmp_path / "uni.dwg"
    rep = write_dwg(doc, out)
    assert rep.text["escaped"] == 6 and rep.text["replaced"] == 1
    assert any("1252" in w for w in rep.warnings)
    dst, _ = read_dwg(out)
    texts = [e.dxf.text for e in dst.modelspace().query("TEXT")]
    assert texts == ["Grüße €100 — Ω≈π 東京"]
    assert dst.modelspace().query("MTEXT")[0].text == "emoji ? end"
    assert "Ärger_Ω" in dst.layers


def test_huge_tiny_and_non_finite_coordinates(tmp_path):
    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    msp.add_line((1e15, -1e15, 0), (1e300, -1e-300, 5e-324))
    msp.add_circle((1e18, -1e18), 1e17)
    bad = msp.add_line((0, 0), (1, 1))
    bad.dxf.start = (float("nan"), 0, 0)
    msp.add_mtext("x" * 40000)
    out = tmp_path / "huge.dwg"
    rep = write_dwg(doc, out)
    assert any("non-finite" in w for w in rep.warnings) and rep.text["truncated"] == 40000 - 32767
    dst, rr = read_dwg(out)
    lines = list(dst.modelspace().query("LINE"))
    assert tuple(lines[0].dxf.start) == (1e15, -1e15, 0) and tuple(lines[0].dxf.end) == (1e300, -1e-300, 5e-324)
    assert tuple(lines[1].dxf.start) == (0, 0, 0)
    assert dst.modelspace().query("CIRCLE")[0].dxf.radius == 1e17


def test_unsupported_entities_are_exploded_or_reported(tmp_path):
    doc = ezdxf.new("R2000", setup=True)
    msp = doc.modelspace()
    msp.add_leader([(0, 0), (5, 5), (8, 5)])
    msp.add_mline([(0, 0), (10, 0), (10, 10)])
    img_def = doc.add_image_def("x.png", size_in_pixel=(10, 10))
    msp.add_image(img_def, insert=(0, 0), size_in_units=(1, 1))
    out = tmp_path / "x.dwg"
    rep = write_dwg(doc, out)
    assert rep.exploded["LEADER"] == 1 and rep.exploded["MLINE"] == 1 and rep.skipped["IMAGE"] == 1
    _dst, rr = read_dwg(out)
    assert rr.entities["LINE"] >= 2


def test_dimension_without_geometry_block(tmp_path):
    doc = ezdxf.new("R2000", setup=True)
    dim = doc.modelspace().add_linear_dim(base=(0, 5), p1=(0, 0), p2=(10, 0))
    dim.dimension.dxf.discard("geometry")
    out = tmp_path / "d.dwg"
    rep = write_dwg(doc, out)
    assert rep.entities["DIMENSION"] == 1 and any("NULL block" in w for w in rep.warnings)
    _dst, rr = read_dwg(out)
    assert rr.entities["DIMENSION"] == 1


# ====================================================================== engine wiring and conversions
def test_native_engine_from_dxf_and_writer_preference(tmp_path):
    from pdfcore import engines
    from pdfcore.ops import convert

    w = convert.dwg_writer()
    assert w.name == "native" and w.installed()
    dxf = tmp_path / "plan.dxf"
    _doc().saveas(dxf)
    out = engines.get("dwg", "native").from_dxf(dxf, tmp_path)
    assert out == tmp_path / "plan.dwg" and out.read_bytes()[:6] == b"AC1015"
    res = convert.convert(dxf, "dwg", out=tmp_path / "via_convert.dwg", quiet=True)
    assert res.data["converter"] == "native"
    _d, rr = read_dwg(res.output)
    assert rr.entities["LINE"] >= 2


def test_pdf_to_dxf_to_dwg_to_pdf_passes_gate(corpus, tmp_path):
    from pdfcore.ops import convert

    src = tmp_path / "drawings_rev0.pdf"
    shutil.copy(corpus["drawings_rev0"], src)
    dwg = convert.convert(src, "dwg", pages="2", out=tmp_path / "p2.dwg", quiet=True).output
    assert dwg.read_bytes()[:6] == b"AC1015"
    doc, rr = read_dwg(dwg)
    assert sum(rr.entities.values()) > 10 and not rr.warnings
    back = convert.convert(dwg, "pdf", out=tmp_path / "p2.pdf", quiet=True)
    assert back.gate is not None and back.gate.passed and back.ok


# ====================================================================== local-only: corpus and validators
def _corpus_files() -> list[Path]:
    return sorted(CORPUS.glob("*.dwg")) if CORPUS.is_dir() else []


@pytest.mark.skipif(not _corpus_files(), reason="local DWG corpus not available")
def test_corpus_dxf_conversions_written_back(tmp_path):
    done = 0
    for f in _corpus_files():
        try:
            doc, _ = read_dwg(f)
        except Exception:
            continue  # R13/R14 samples: not readable natively
        dxf = tmp_path / (f.stem + ".dxf")
        doc.saveas(dxf)
        src = ezdxf.readfile(dxf)
        out = tmp_path / (f.stem + ".dwg")
        rep = write_dwg(src, out)
        dst, rr = read_dwg(out)
        assert not rr.warnings, (f.name, rr.warnings[:3])
        for lay in src.layouts:
            a = Counter(e.dxftype() for e in lay)
            b = Counter(e.dxftype() for e in dst.layouts.get(lay.name))
            for t in set(rep.exploded) | set(rep.skipped):
                a.pop(t, None)
            # exploded entities come back as their primitives
            assert all(b[t] >= n for t, n in a.items()), (f.name, lay.name, a, b)
        done += 1
    assert done >= 20


def _dwgread_ok(path: Path) -> tuple[bool, str]:
    p = subprocess.run([DWGREAD, str(path)], capture_output=True, text=True, timeout=300)
    text = p.stdout + p.stderr
    errors = [ln for ln in text.splitlines() if "ERROR" in ln.upper() and "0x0" not in ln]
    return p.returncode == 0 and not errors, text[-2000:]


@pytest.mark.skipif(DWGREAD is None, reason="LibreDWG dwgread not installed (local black-box validator)")
def test_dwgread_opens_written_files(tmp_path):
    files = []
    out = tmp_path / "doc.dwg"
    write_dwg(_doc(), out)
    files.append(out)
    empty = tmp_path / "empty.dwg"
    write_dwg(ezdxf.new("R2000"), empty)
    files.append(empty)
    for f in _corpus_files()[:40]:
        try:
            doc, _ = read_dwg(f)
        except Exception:
            continue
        o = tmp_path / ("c_" + f.stem + ".dwg")
        write_dwg(doc, o)
        files.append(o)
    for f in files:
        ok, text = _dwgread_ok(f)
        assert ok, (f.name, text)


@pytest.mark.skipif(DWG2DXF is None, reason="LibreDWG dwg2dxf not installed (local black-box validator)")
def test_dwg2dxf_converts_written_file(tmp_path):
    src = _doc()
    out = tmp_path / "doc.dwg"
    write_dwg(src, out)
    dxf = tmp_path / "lib.dxf"
    p = subprocess.run([DWG2DXF, "-y", str(out), "-o", str(dxf)], capture_output=True, text=True, timeout=300)
    assert p.returncode == 0 and dxf.exists(), p.stderr[-2000:]
    lib = ezdxf.readfile(dxf)
    for lay in src.layouts:
        a = Counter(e.dxftype() for e in lay)
        b = Counter(e.dxftype() for e in lib.layouts.get(lay.name))
        assert a == b, (lay.name, a, b)
    layers = {lay.dxf.name: lay for lay in lib.layers}
    assert layers["WALLS"].dxf.color == 1 and layers["WALLS"].dxf.linetype == "DASHED"
    assert layers["OFF"].is_off() and layers["FROZEN"].is_frozen() and layers["LOCKED"].is_locked()
    texts = sorted(e.dxf.text for e in lib.modelspace().query("TEXT"))
    assert texts == ["Centered", "Hello TEXT"]
    ins = [e for e in lib.modelspace().query("INSERT") if e.attribs]
    assert [(a.dxf.tag, a.dxf.text) for a in ins[0].attribs] == [("TAG1", "D-101")]
