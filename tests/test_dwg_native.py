"""Native DWG reader (pdfcore.ops.plans.dwg).

Unit tests (bit codes from the ODA spec's worked examples, decompression, malformed input) always run.
Sample-file tests use local AutoCAD samples that are NOT shipped with pdf-studio and skip when absent:
set ``PDFCORE_DWG_CORPUS`` or put files in ``../pdf-studio-local/corpus/dwg``. Oracle comparisons also
need LibreDWG's ``dwg2dxf`` on PATH (a local black-box test tool only — never a dependency).
"""

from __future__ import annotations

import io
import math
import os
import random
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from pdfcore.ops.plans.dwg import DwgReadError, can_read, peek_version, read_dwg  # noqa: E402
from pdfcore.ops.plans.dwg import bits  # noqa: E402
from pdfcore.ops.plans.dwg.container import decompress_r18  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(os.environ.get("PDFCORE_DWG_CORPUS", ROOT.parent / "pdf-studio-local" / "corpus" / "dwg"))
SAMPLES = ["ex2000", "sample_2000", "example_2004", "example_2007", "example_2010", "example_2013", "ex2018",
           "sample_2018"]
# more LibreDWG test-data files, saved locally as <version>_<name>.dwg (optional)
EXTRA = ["2000_TS1", "2000_Leader", "2000_Polyline", "2000_Text", "2000_Helix", "2004_Leader", "2004_Underlay",
         "2004_material", "2007_ATMOS-DC22S", "2007_Leader", "2007_Text", "2010_Ellipse", "2010_Leader",
         "2010_gh209_1", "2013_gh109_1", "2013_gh44-error", "2018_Dynblocks", "2018_Leader", "2018_LiveSection1",
         "2018_Polygon"]
DWG2DXF = shutil.which("dwg2dxf") or ("/opt/homebrew/bin/dwg2dxf" if Path("/opt/homebrew/bin/dwg2dxf").exists() else None)
REL = 1e-6


def sample(name: str) -> Path:
    p = CORPUS / f"{name}.dwg"
    if not p.exists():
        pytest.skip(f"DWG sample {p} not available (local corpus only)")
    return p


# ---------------------------------------------------------------------------------------------- bit codes
def test_bitshort_spec_example():
    # §2.2: 00 00000001 00000001 | 10 | 11 | 01 00001111 | 10
    r = bits.BitReader(bytes([0b00000000, 0b01000000, 0b01101101, 0b00001111, 0b10000000]))
    assert [r.bs() for _ in range(5)] == [257, 0, 256, 15, 0]


def test_bitlong_spec_example():
    r = bits.BitReader(bytes([0b00000000, 0b01000000, 0b01000000, 0b00000000, 0b00100100, 0b00111110]))
    assert [r.bl() for _ in range(4)] == [257, 0, 15, 0]


def test_modular_char_and_short_spec_examples():
    assert bits.mc_bytes(bytes([0b10000010, 0b00100100]), 0) == (4610, 2)
    assert bits.mc_bytes(bytes([0b11101001, 0b10010111, 0b11100110, 0b00110101]), 0)[0] == 112823273
    assert bits.mc_bytes(bytes([0b10000101, 0b01001011]), 0)[0] == -1413
    assert bits.ms_bytes(bytes([0b00110001, 0b11110100, 0b10001101, 0]), 0)[0] == 4650033


def test_handle_reference_spec_example():
    r = bits.BitReader(bytes([0b01010010, 0b00000101, 0b11100111]))
    assert r.h() == (5, 0x5E7)


def test_crc_table_matches_spec():
    bits.crc8(0, b"x")
    assert bits._CRC8[:4] == [0x0000, 0xC0C1, 0xC181, 0x0140] and bits._CRC8[255] == 0x4040


def _bits_to_bytes(bitstr: str) -> bytes:
    bitstr += "0" * (-len(bitstr) % 8)
    return int(bitstr, 2).to_bytes(len(bitstr) // 8, "big") if bitstr else b""


def test_bitdouble_and_default_double():
    import struct

    def le_bits(raw: bytes) -> str:
        return "".join(format(x, "08b") for x in raw)

    d = struct.pack("<d", 3.25)
    r = bits.BitReader(_bits_to_bytes("01" + "10" + "00" + le_bits(d)))  # BD: 1.0, 0.0, RD
    assert (r.bd(), r.bd(), r.bd()) == (1.0, 0.0, 3.25)
    # DD (§2.9): 00 default; 01 patch 4 low bytes; 10 patch bytes 5-6 then 1-4; 11 full RD
    dflt = 1234.5
    new = struct.pack("<d", -7.125)
    r = bits.BitReader(_bits_to_bytes("00" + "11" + le_bits(new)))
    assert r.dd(dflt) == dflt and r.dd(dflt) == -7.125
    raw = bytearray(struct.pack("<d", dflt))
    patch = bytes([1, 2, 3, 4])
    raw[0:4] = patch
    r = bits.BitReader(_bits_to_bytes("01" + le_bits(patch)))
    assert r.dd(dflt) == struct.unpack("<d", bytes(raw))[0]
    raw = bytearray(struct.pack("<d", dflt))
    six = bytes([9, 8, 7, 6, 5, 4])
    raw[4:6], raw[0:4] = six[0:2], six[2:6]
    r = bits.BitReader(_bits_to_bytes("10" + le_bits(six)))
    assert r.dd(dflt) == struct.unpack("<d", bytes(raw))[0]


def test_r18_decompress_literal_only():
    # literal length 0x02 -> 5 bytes, then 0x11 terminator (§4.7)
    assert decompress_r18(bytes([0x02]) + b"abcde" + bytes([0x11]), 5) == b"abcde"


def test_r18_decompress_backreference():
    # 4 literals "abcd", then opcode 0x40-0xFF form: compressedBytes=((op&0xF0)>>4)-1, offset=(op2<<2)|((op&0x0C)>>2)
    # copy 4 bytes from offset 3 (i.e. 4 back): op = 0x50 (cb=4, lit=0), op2 = 0 -> offset 0 +1 ... use offset 3
    op = 0x50 | (3 << 2)
    data = bytes([0x01]) + b"abcd" + bytes([op, 0x00, 0x11])
    assert decompress_r18(data, 8) == b"abcdabcd"


def test_bounds_checked_reads():
    r = bits.BitReader(b"\x00")
    r.bits(8)
    with pytest.raises(DwgReadError):
        r.bits(1)


# ---------------------------------------------------------------------------------------------- robustness
@pytest.mark.parametrize("blob", [b"", b"AC10", b"not a dwg at all" * 20, b"AC1015" + b"\0" * 200,
                                  b"AC1018" + bytes(range(256)) * 2, b"AC1032" + b"\xff" * 600,
                                  b"AC1021" + b"\x00" * 2000, b"AC1009" + b"\0" * 300])
def test_malformed_input_raises_dwgreaderror(tmp_path, blob):
    p = tmp_path / "bad.dwg"
    p.write_bytes(blob)
    with pytest.raises(DwgReadError):
        read_dwg(p)


def test_peek_version(tmp_path):
    p = tmp_path / "x.dwg"
    p.write_bytes(b"AC1015" + b"\0" * 10)
    assert peek_version(p) == "AC1015" and can_read(p)
    p.write_bytes(b"AC1009" + b"\0" * 10)
    assert peek_version(p) == "AC1009" and not can_read(p)
    p.write_bytes(b"%PDF-1.7")
    assert peek_version(p) is None and not can_read(p)


@pytest.mark.parametrize("name", ["ex2000", "example_2004", "ex2018"])
def test_fuzzed_samples_never_crash(tmp_path, name):
    data = bytearray(sample(name).read_bytes())
    rng = random.Random(1234)
    p = tmp_path / "fuzz.dwg"
    for i in range(25):
        buf = bytearray(data)
        if i % 5 == 0:
            buf = buf[: rng.randrange(64, len(buf))]
        else:
            for _ in range(rng.randrange(1, 64)):
                buf[rng.randrange(6, len(buf))] = rng.randrange(256)
        p.write_bytes(bytes(buf))
        try:
            doc, _ = read_dwg(p)
        except DwgReadError:
            continue
        doc.write(io.StringIO())  # whatever was read must also be exportable


# ---------------------------------------------------------------------------------------------- samples
@pytest.mark.parametrize("name", SAMPLES + EXTRA)
def test_sample_reads_and_exports(name):
    doc, rep = read_dwg(sample(name))
    assert rep.objects > 0
    buf = io.StringIO()
    doc.write(buf)
    assert "EOF" in buf.getvalue()[-20:]


@pytest.mark.parametrize("name", SAMPLES)
def test_reads_sample(name):
    doc, rep = read_dwg(sample(name))
    assert rep.version == peek_version(sample(name))
    assert sum(rep.entities.values()) > 0
    assert doc.header["$INSUNITS"] == 4
    assert "0" in rep.layers


def test_performance_ex2000():
    p = sample("ex2000")  # 570 KB
    read_dwg(p)  # warm imports
    t = time.perf_counter()
    read_dwg(p)
    assert time.perf_counter() - t < 5.0


# ---------------------------------------------------------------------------------------------- oracle
def _oracle(name: str, tmp: Path):
    if not DWG2DXF:
        pytest.skip("dwg2dxf (LibreDWG, local test oracle) not installed")
    out = tmp / f"{name}.oracle.dxf"
    subprocess.run([DWG2DXF, "-y", "-o", str(out), str(sample(name))], capture_output=True, timeout=120)
    if not out.exists():
        pytest.skip("dwg2dxf produced no output")
    from ezdxf import recover

    doc, _ = recover.readfile(str(out))
    return doc


def _spaces(doc):
    """{space key: [entities]} for model space, each paper space layout (by block record name order) and
    each named block."""
    out = {"MODEL": list(doc.modelspace())}
    ps = []
    for lay in doc.layouts:
        if lay.name != "Model":
            ps.extend(lay)
    out["PAPER"] = ps
    for b in doc.blocks:
        n = b.name
        if n.lower().startswith(("*model_space", "*paper_space")):
            continue
        key = "BLOCK:" + (n if not n.startswith("*") else n.rstrip("0123456789"))
        out.setdefault(key, []).extend(b)
    return out


def _close(a, b) -> bool:
    if isinstance(a, (tuple, list)) or hasattr(a, "__iter__") and not isinstance(a, str):
        a, b = list(a), list(b)
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if isinstance(a, str):
        return a == b
    return math.isclose(float(a), float(b), rel_tol=REL, abs_tol=1e-9 * max(1.0, abs(float(a))))


def _key(e):
    t = e.dxftype()
    d = e.dxf
    if t == "LINE":
        return (tuple(d.start), tuple(d.end))
    if t in ("CIRCLE", "ARC"):
        return (tuple(d.center), d.radius) + ((d.start_angle % 360, d.end_angle % 360) if t == "ARC" else ())
    if t in ("TEXT", "ATTDEF", "ATTRIB"):
        return (d.text, tuple(d.insert))
    if t == "INSERT":
        name = d.name if not d.name.startswith("*") else d.name.rstrip("0123456789")
        return (name, tuple(d.insert))
    if t == "MTEXT":
        return (e.text, tuple(d.insert))
    if t == "POINT":
        return (tuple(d.location),)
    if t == "LWPOLYLINE":
        return (int(e.closed), tuple(tuple(p) for p in e.get_points("xyb")))
    if t == "POLYLINE":
        return (d.flags & 1, tuple(tuple(v.dxf.location) for v in e.vertices))
    if t == "ELLIPSE":
        return (tuple(d.center), tuple(d.major_axis), d.ratio, d.start_param, d.end_param)
    if t == "SPLINE":
        return (d.degree, tuple(tuple(p) for p in e.control_points), tuple(e.knots), tuple(tuple(p) for p in e.fit_points))
    if t in ("SOLID", "3DFACE"):
        return tuple(tuple(d.get(f"vtx{i}")) for i in range(4))
    if t == "DIMENSION":
        return (tuple(d.defpoint2), tuple(d.defpoint3), tuple(d.text_midpoint))
    if t == "HATCH":
        return (d.pattern_name, d.solid_fill, len(e.paths))
    if t == "VIEWPORT":
        return (tuple(d.center), d.width, d.height)
    return None


def _compare(native, oracle, skipped: Counter):
    problems = []
    ns, os_ = _spaces(native), _spaces(oracle)
    for space in sorted(set(ns) | set(os_)):
        nc = Counter(e.dxftype() for e in ns.get(space, []))
        oc = Counter(e.dxftype() for e in os_.get(space, []))
        for t in sorted(set(nc) | set(oc)):
            if nc[t] != oc[t] and not (nc[t] < oc[t] and skipped.get(t, 0) >= oc[t] - nc[t]):
                problems.append(f"{space}: {t} native {nc[t]} oracle {oc[t]}")
        for t in ("LINE", "CIRCLE", "ARC", "TEXT", "ATTDEF", "INSERT", "MTEXT", "POINT", "LWPOLYLINE", "POLYLINE",
                  "ELLIPSE", "SPLINE", "SOLID", "3DFACE", "DIMENSION", "HATCH", "VIEWPORT"):
            nk = sorted((_key(e) for e in ns.get(space, []) if e.dxftype() == t), key=repr)
            ok = sorted((_key(e) for e in os_.get(space, []) if e.dxftype() == t), key=repr)
            if len(nk) != len(ok):
                continue  # already reported as a count mismatch
            unmatched = list(ok)
            for k in nk:
                hit = next((i for i, o in enumerate(unmatched) if _close(k, o)), None)
                if hit is None:
                    problems.append(f"{space}: {t} {k!r} not in oracle")
                else:
                    unmatched.pop(hit)
    nl = sorted(lay.dxf.name.lstrip("*").upper() for lay in native.layers)
    ol = sorted(lay.dxf.name.lstrip("*").upper() for lay in oracle.layers)
    if nl != ol:
        problems.append(f"layers native {nl} oracle {ol}")
    return problems


ORACLE_SAMPLES = SAMPLES + EXTRA


@pytest.mark.parametrize("name", ORACLE_SAMPLES)
def test_matches_libredwg_oracle(name, tmp_path):
    oracle = _oracle(name, tmp_path)
    native, rep = read_dwg(sample(name))
    problems = _compare(native, oracle, rep.skipped)
    assert not problems, "\n".join(problems[:40])


# ---------------------------------------------------------------------------------------------- engine
def test_native_engine_registered_and_preferred(tmp_path):
    from pdfcore import engines

    assert "native" in engines.available("dwg")
    eng = engines.get("dwg", "native")
    assert eng.installed()
    p = tmp_path / "v.dwg"
    p.write_bytes(b"AC1015" + b"\0" * 10)
    old = os.environ.pop("PDFCORE_DWG_ENGINE", None)
    try:
        assert engines.dwg_converter(p).name == "native"
    finally:
        if old is not None:
            os.environ["PDFCORE_DWG_ENGINE"] = old


def test_native_engine_to_dxf(tmp_path):
    from pdfcore import engines

    src = sample("ex2000")
    out = engines.get("dwg", "native").to_dxf(src, tmp_path)
    assert Path(out).exists()
    doc = ezdxf.readfile(str(out))
    assert len(doc.modelspace().query("LINE")) == 8


def test_dxf2pdf_with_native_engine(tmp_path, monkeypatch):
    from pdfcore.ops.plans import cad

    src = sample("ex2000")
    monkeypatch.setenv("PDFCORE_DWG_ENGINE", "native")
    doc, mode = cad.load_cad_ex(src)
    assert mode.startswith("dwg:")
    out = tmp_path / "ex2000.pdf"
    res = cad.dxf_to_pdf(src, out=out, quiet=True)
    assert out.exists() and out.stat().st_size > 1000
    assert res.ok, res.line()  # the output gate passed
