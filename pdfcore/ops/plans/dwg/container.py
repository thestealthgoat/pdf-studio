"""File-level DWG structure: version detection, section location, decompression, object map.

* R13–R15 (AC1012/AC1014/AC1015): file header + section-locator records (ODA spec §3.2), header and
  classes sections with sentinels (§9, §10.1), object map (§23.1); object offsets are absolute file
  addresses.
* R2004, R2010, R2013, R2018 (AC1018/AC1024/AC1027/AC1032): XOR-masked file header (§4.1), section page
  map and section map system pages (§4.3–§4.5), masked data page headers (§4.6), the LZ77 variant
  compression (§4.7). The object map (§23.2) indexes the logical AcDb:AcDbObjects section.
* R2007 (AC1021): Reed–Solomon coded pages and a different compressor (§5); see :mod:`.r2007`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from pdfcore.ops.plans.dwg.bits import R13, R14, R2000, R2004, R2007, R2010, R2013, R2018, crc8, mc_bytes
from pdfcore.ops.plans.dwg.errors import DwgReadError

VERSIONS: dict[str, tuple[int, str]] = {
    "AC1012": (R13, "R13"),
    "AC1014": (R14, "R14"),
    "AC1015": (R2000, "R2000"),
    "AC1018": (R2004, "R2004"),
    "AC1021": (R2007, "R2007"),
    "AC1024": (R2010, "R2010"),
    "AC1027": (R2013, "R2013"),
    "AC1032": (R2018, "R2018"),
}

SENT_HEADER = bytes([0xCF, 0x7B, 0x1F, 0x23, 0xFD, 0xDE, 0x38, 0xA9, 0x5F, 0x7C, 0x68, 0xB8, 0x4E, 0x6D, 0x33, 0x5F])
SENT_CLASSES = bytes([0x8D, 0xA1, 0xC4, 0xB8, 0xC4, 0xA9, 0xF8, 0xC5, 0xC0, 0xDC, 0xF4, 0x5F, 0xE7, 0xCF, 0xB6, 0x8A])

# Codepage numbers seen in the file header (§3.2.5). Unknown values fall back to cp1252.
CODEPAGES = {30: "cp1252"}


@dataclass
class DwgContainer:
    """Raw sections of a DWG file, ready for bit-level parsing."""

    version: str
    ver: int
    release: str
    maint: int
    codepage: int
    header: bytes = b""            # header variable data (after the RL size)
    classes: bytes = b""           # class data area (after the RL size)
    objects: bytes = b""           # buffer the object map offsets index into
    handles: dict[int, int] = field(default_factory=dict)  # handle -> offset (insertion order = map order)
    warnings: list[str] = field(default_factory=list)

    @property
    def codec(self) -> str:
        return CODEPAGES.get(self.codepage, "cp1252")


def peek_version(data_or_path) -> str | None:
    """Return the 6-byte version string (``AC1015`` …) or None if the input is not a DWG."""
    if isinstance(data_or_path, (bytes, bytearray, memoryview)):
        head = bytes(data_or_path[:6])
    else:
        with open(data_or_path, "rb") as fh:
            head = fh.read(6)
    try:
        s = head.decode("ascii")
    except UnicodeDecodeError:
        return None
    return s if s.startswith("AC") and len(s) == 6 else None


def open_container(data: bytes) -> DwgContainer:
    if len(data) < 0x80:
        raise DwgReadError("file too short to be a DWG")
    vs = peek_version(data)
    if vs is None:
        raise DwgReadError("not a DWG file (no ACxxxx version signature)")
    if vs not in VERSIONS:
        raise DwgReadError(f"unsupported DWG version {vs}")
    ver, rel = VERSIONS[vs]
    if ver <= R2000:
        return _open_r15(data, vs, ver, rel)
    if ver == R2007:
        from pdfcore.ops.plans.dwg.r2007 import open_r2007

        return open_r2007(data, vs, ver, rel)
    return _open_r2004(data, vs, ver, rel)


# ====================================================================== R13–R15 (§3)
def _sentinel_section(data: bytes, off: int, sent: bytes, what: str) -> bytes:
    if data[off:off + 16] != sent:
        raise DwgReadError(f"{what} section: start sentinel not found at 0x{off:x}")
    (size,) = struct.unpack_from("<I", data, off + 16)
    start = off + 20
    if start + size > len(data):
        raise DwgReadError(f"{what} section size {size} runs past end of file")
    return data[start:start + size]


def _open_r15(data: bytes, vs: str, ver: int, rel: str) -> DwgContainer:
    maint = data[0x0B]
    (codepage,) = struct.unpack_from("<H", data, 0x13)
    (nrec,) = struct.unpack_from("<i", data, 0x15)
    if not 3 <= nrec <= 16:
        raise DwgReadError(f"implausible section-locator count {nrec}")
    recs: dict[int, tuple[int, int]] = {}
    for i in range(nrec):
        o = 0x19 + 9 * i
        if o + 9 > len(data):
            raise DwgReadError("section-locator records truncated")
        num = data[o]
        seek, size = struct.unpack_from("<ii", data, o + 1)
        recs[num] = (seek, size)
    c = DwgContainer(vs, ver, rel, maint, codepage)
    for k in (0, 1, 2):
        if k not in recs:
            raise DwgReadError(f"section locator {k} missing")
        seek, size = recs[k]
        if seek < 0 or seek >= len(data):
            raise DwgReadError(f"section locator {k} points outside the file (0x{seek:x})")
    c.header = _sentinel_section(data, recs[0][0], SENT_HEADER, "header")
    c.classes = _sentinel_section(data, recs[1][0], SENT_CLASSES, "classes")
    c.objects = data
    c.handles = parse_object_map(data, recs[2][0], recs[2][0] + max(recs[2][1], 0), c.warnings)
    return c


def parse_object_map(data: bytes, off: int, end: int, warnings: list[str], base: int = 0) -> dict[int, int]:
    """Object map (§23): big-endian section sizes (counting themselves), MC handle/offset deltas, CRC."""
    out: dict[int, int] = {}
    pos = off
    h = 0
    loc = 0
    end = min(end if end > off else len(data), len(data))
    guard = 0
    while pos + 2 <= len(data):
        guard += 1
        if guard > 1_000_000:
            raise DwgReadError("object map does not terminate")
        sz = (data[pos] << 8) | data[pos + 1]
        if sz <= 2:
            break
        sec_end = pos + sz
        if sec_end + 2 > len(data):
            raise DwgReadError("object map section runs past end of data")
        p = pos + 2
        h = 0  # deltas restart in every map section
        loc = 0
        while p < sec_end:
            dh, p = mc_bytes(data, p, signed=False)
            dl, p = mc_bytes(data, p, signed=True)
            h += dh
            loc += dl
            out[h] = loc + base
        crc_stored = (data[sec_end] << 8) | data[sec_end + 1]
        if crc8(0xC0C1, data[pos:sec_end]) != crc_stored:
            warnings.append(f"object map section at 0x{pos:x}: CRC mismatch")
        pos = sec_end + 2
    if not out:
        raise DwgReadError("object map is empty")
    return out


# ====================================================================== R2004+ (§4)
def _magic(n: int) -> bytes:
    """The pseudo-random XOR sequence of §4.1."""
    out = bytearray(n)
    seed = 1
    for i in range(n):
        seed = (seed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
        out[i] = (seed >> 16) & 0xFF
    return bytes(out)


def decompress_r18(src: bytes, out_size: int) -> bytes:
    """LZ77 variant of §4.7. ``out_size`` is the expected decompressed size (output is clipped/padded)."""
    out = bytearray()
    n = len(src)
    pos = 0

    def byte() -> int:
        nonlocal pos
        if pos >= n:
            raise DwgReadError("compressed stream truncated")
        b = src[pos]
        pos += 1
        return b

    def literal_length(first: int) -> int:
        # §4.7 "Literal Length": 0x01-0x0E -> +3; 0x00 -> running 0x0F + bytes...; called with the
        # opcode/literal byte already read.
        if first == 0:
            total = 0x0F
            b = byte()
            while b == 0:
                total += 0xFF
                b = byte()
            return total + b + 3
        return first + 3

    def long_comp(first: int) -> int:
        if first:
            return first
        total = 0xFF
        b = byte()
        while b == 0:
            total += 0xFF
            b = byte()
        return total + b

    def two_byte() -> tuple[int, int]:
        b1 = byte()
        b2 = byte()
        return (b1 >> 2) | (b2 << 6), b1 & 3

    def copy_lit(cnt: int) -> None:
        nonlocal pos
        if pos + cnt > n:
            raise DwgReadError("compressed literal run truncated")
        out.extend(src[pos:pos + cnt])
        pos += cnt

    op = byte()
    if op & 0xF0 == 0:
        copy_lit(literal_length(op))
        op = byte()
    limit = out_size + 0x10000
    while True:
        if op == 0x11:
            break
        if op < 0x10:
            raise DwgReadError(f"invalid compression opcode 0x{op:02x}")
        if op == 0x10:
            cb = long_comp(byte()) + 9
            off, lit = two_byte()
            off += 0x3FFF
        elif op < 0x20:
            cb = (op & 0x0F) + 2
            off, lit = two_byte()
            off += 0x3FFF
        elif op == 0x20:
            cb = long_comp(byte()) + 0x21
            off, lit = two_byte()
        elif op < 0x40:
            cb = op - 0x1E
            off, lit = two_byte()
        else:
            cb = ((op & 0xF0) >> 4) - 1
            op2 = byte()
            off = (op2 << 2) | ((op & 0x0C) >> 2)
            lit = op & 3
        start = len(out) - off - 1
        if start < 0:
            raise DwgReadError("compression back-reference before start of data")
        if cb <= off + 1:
            out.extend(out[start:start + cb])
        else:
            for i in range(cb):
                out.append(out[start + i])
        if lit == 0:
            nxt = byte()
            if nxt & 0xF0 == 0:
                copy_lit(literal_length(nxt))
                op = byte()
            else:
                op = nxt
        else:
            copy_lit(lit)
            op = byte()
        if len(out) > limit:
            raise DwgReadError("decompressed data exceeds declared size")
    if len(out) < out_size:
        out.extend(b"\0" * (out_size - len(out)))
    return bytes(out[:out_size])


@dataclass
class _Section:
    name: str
    size: int
    max_size: int
    compressed: int
    encrypted: int
    pages: list[tuple[int, int, int]]  # (page number, data size, start offset)


def _open_r2004(data: bytes, vs: str, ver: int, rel: str) -> DwgContainer:
    maint = data[0x0B]
    (codepage,) = struct.unpack_from("<H", data, 0x13)
    (security,) = struct.unpack_from("<I", data, 0x18)
    if security & 1:
        raise DwgReadError("DWG is password-protected/encrypted (security flags 0x%x); not supported" % security)
    hdr = bytes(a ^ b for a, b in zip(data[0x80:0x80 + 0x6C], _magic(0x6C)))
    if not hdr.startswith(b"AcFssFcAJMB"):
        raise DwgReadError("R2004 file header did not decrypt (bad 'AcFssFcAJMB' id)")
    f = struct.unpack_from("<12sIIIIIIIIQQIIIIIIQIIII", hdr, 0)
    page_map_addr = f[17] + 0x100
    section_map_id = f[18]
    c = DwgContainer(vs, ver, rel, maint, codepage)

    # ---- section page map (§4.3, §4.4)
    pages = _read_system_page(data, page_map_addr, 0x41630E3B, "section page map")
    page_addr: dict[int, tuple[int, int]] = {}
    addr = 0x100
    p = 0
    while p + 8 <= len(pages):
        num, size = struct.unpack_from("<ii", pages, p)
        p += 8
        if num < 0:
            p += 16
        else:
            page_addr[num] = (addr, size)
        if size <= 0:
            break
        addr += size
    if section_map_id not in page_addr:
        raise DwgReadError("section map page not found in page map")

    # ---- section map (§4.5)
    smap = _read_system_page(data, page_addr[section_map_id][0], 0x4163003B, "section map")
    if len(smap) < 20:
        raise DwgReadError("section map too short")
    ndesc = struct.unpack_from("<i", smap, 0)[0]
    p = 20
    sections: dict[str, _Section] = {}
    for _ in range(max(0, min(ndesc, 1000))):
        if p + 0x60 > len(smap):
            raise DwgReadError("section map truncated")
        size, count, max_size, _unk, comp, sid, enc = struct.unpack_from("<QiiIiii", smap, p)
        name = smap[p + 0x20:p + 0x60].split(b"\0", 1)[0].decode("ascii", "replace")
        p += 0x60
        pl = []
        for _ in range(max(0, min(count, 100000))):
            if p + 16 > len(smap):
                raise DwgReadError("section page list truncated")
            pnum, dsize, start = struct.unpack_from("<iiQ", smap, p)
            p += 16
            pl.append((pnum, dsize, start))
        sections[name] = _Section(name, size, max_size, comp, enc, pl)

    def get(name: str) -> bytes:
        sec = sections.get(name)
        if sec is None:
            raise DwgReadError(f"section {name} not present")
        if sec.encrypted == 1:
            raise DwgReadError(f"section {name} is encrypted; not supported")
        return _read_data_section(data, sec, page_addr)

    hdr_data = get("AcDb:Header")
    cls_data = get("AcDb:Classes")
    obj_data = get("AcDb:AcDbObjects")
    hnd_data = get("AcDb:Handles")
    c.header = _strip_sentinel(hdr_data, SENT_HEADER, "header", ver, maint)
    c.classes = _strip_sentinel(cls_data, SENT_CLASSES, "classes", ver, maint)
    c.objects = obj_data
    c.handles = parse_object_map(hnd_data, 0, len(hnd_data), c.warnings)
    return c


def _strip_sentinel(sec: bytes, sent: bytes, what: str, ver: int, maint: int) -> bytes:
    if sec[:16] != sent:
        raise DwgReadError(f"{what} section: start sentinel missing")
    (size,) = struct.unpack_from("<I", sec, 16)
    start = 20
    if ver >= R2010 and (maint > 3 or ver >= R2018):
        start += 4  # §9/§10.2: unknown RL (high half of a 64-bit size)
    if size > len(sec) - start:
        raise DwgReadError(f"{what} section size {size} exceeds section length")
    return sec[start:start + size + (8 if what == "header" else 0)]


def _read_system_page(data: bytes, addr: int, kind: int, what: str) -> bytes:
    if addr + 20 > len(data):
        raise DwgReadError(f"{what} address 0x{addr:x} outside file")
    ptype, dsize, csize, ctype, _chk = struct.unpack_from("<IIIII", data, addr)
    if ptype != kind:
        raise DwgReadError(f"{what}: bad page type 0x{ptype:x}")
    if csize > len(data) - addr - 20 or dsize > 64 * 1024 * 1024:
        raise DwgReadError(f"{what}: implausible sizes")
    raw = data[addr + 20:addr + 20 + csize]
    return decompress_r18(raw, dsize) if ctype == 2 else raw[:dsize]


MAX_SECTION = 512 * 1024 * 1024


def _read_data_section(data: bytes, sec: _Section, page_addr: dict[int, tuple[int, int]]) -> bytes:
    limit = min(MAX_SECTION, max(64 * 1024 * 1024, 64 * len(data)))
    if not 0 <= sec.size <= limit or not 0 < sec.max_size <= 16 * 1024 * 1024:
        raise DwgReadError(f"{sec.name}: implausible section size {sec.size} / page size {sec.max_size}")
    out = bytearray()
    for pnum, dsize, start in sec.pages:
        if pnum not in page_addr:
            raise DwgReadError(f"{sec.name}: page {pnum} missing from page map")
        addr = page_addr[pnum][0]
        if addr + 32 > len(data):
            raise DwgReadError(f"{sec.name}: page {pnum} outside file")
        mask = 0x4164536B ^ (addr & 0xFFFFFFFF)
        words = struct.unpack_from("<8I", data, addr)
        hdr = [w ^ mask for w in words]
        if hdr[0] != 0x4163043B:
            raise DwgReadError(f"{sec.name}: page {pnum} has bad data page signature")
        csize = hdr[2]
        if start > sec.size or csize > len(data):
            raise DwgReadError(f"{sec.name}: page {pnum} has an implausible offset or size")
        if start > len(out):
            out.extend(b"\0" * (start - len(out)))  # omitted zero pages (§4.5)
        raw = data[addr + 32:addr + 32 + csize]
        if len(raw) < csize:
            raise DwgReadError(f"{sec.name}: page {pnum} truncated")
        logical = min(sec.max_size, max(sec.size - start, 0))
        page = decompress_r18(raw, sec.max_size) if sec.compressed == 2 else raw
        del out[start:]
        out.extend(page[:logical])
    if len(out) < sec.size:
        out.extend(b"\0" * (sec.size - len(out)))
    return bytes(out[:sec.size])
