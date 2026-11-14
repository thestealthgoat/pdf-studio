"""R2007+ specifics: split data/string/handle streams (§6, §7, §9, §20.1) and the R2007 (AC1021)
file organisation (§5: Reed–Solomon coded system/data pages, R21 compression).
"""

from __future__ import annotations

import struct

from pdfcore.ops.plans.dwg.bits import BitReader
from pdfcore.ops.plans.dwg.errors import DwgReadError


def header_streams(c) -> tuple[BitReader, BitReader, BitReader]:
    """Header variables of R2007+ files: the data stream starts with an RL bit size (§9 "R2007: RL size
    in bits"); strings sit at the end of the data stream (located by the §20.1 string-stream rule) and the
    handle references follow it."""
    from pdfcore.ops.plans.dwg.objects import _string_stream

    if len(c.header) < 8:
        raise DwgReadError("header section too short")
    (nbits,) = struct.unpack_from("<I", c.header, 0)
    if not 32 < nbits <= len(c.header) * 8:
        raise DwgReadError(f"header bit size {nbits} out of range")
    r = BitReader(c.header, c.ver, codec=c.codec)
    s = _string_stream(r, nbits)
    if s is None:
        s = BitReader(b"", c.ver)
    hh = BitReader(c.header, c.ver, codec=c.codec, start_bit=nbits)
    r.pos = 32
    r.end = nbits
    r.strings = s
    return r, s, hh


# ====================================================================== R2007 container (§5)
def rs_data(raw: bytes, k: int, blocks: int, interleaved: bool) -> bytes:
    """Strip Reed–Solomon parity (§5.13) without error correction: (255, k) codewords, systematic, data
    bytes first. Interleaved: byte j of block i sits at ``blocks * j + i`` (§5.13.2)."""
    need = blocks * 255
    if len(raw) < need:
        raw = raw + b"\0" * (need - len(raw))
    if not interleaved:
        return raw[: blocks * k]
    out = bytearray(blocks * k)
    for i in range(blocks):
        out[i * k:(i + 1) * k] = raw[i:blocks * k:blocks][:k] if blocks == 1 else raw[i::blocks][:k]
    return bytes(out)


# §5.10.1 literal copy order: for a run of n (1..32) bytes, the list of (count, source index) sub-blocks.
_LIT_ORDER = {
    1: ((1, 0),), 2: ((1, 1), (1, 0)), 3: ((1, 2), (1, 1), (1, 0)), 4: ((1, 0), (1, 1), (1, 2), (1, 3)),
    5: ((1, 4), (4, 0)), 6: ((1, 5), (4, 1), (1, 0)), 7: ((2, 5), (4, 1), (1, 0)), 8: ((4, 0), (4, 4)),
    9: ((1, 8), (8, 0)), 10: ((1, 9), (8, 1), (1, 0)), 11: ((2, 9), (8, 1), (1, 0)), 12: ((4, 8), (8, 0)),
    13: ((1, 12), (4, 8), (8, 0)), 14: ((1, 13), (4, 9), (8, 1), (1, 0)), 15: ((2, 13), (4, 9), (8, 1), (1, 0)),
    16: ((8, 8), (8, 0)), 17: ((8, 9), (1, 8), (8, 0)), 18: ((1, 17), (16, 1), (1, 0)), 19: ((3, 16), (16, 0)),
    20: ((4, 16), (16, 0)), 21: ((1, 20), (4, 16), (16, 0)), 22: ((2, 20), (4, 16), (16, 0)),
    23: ((3, 20), (4, 16), (16, 0)), 24: ((8, 16), (16, 0)), 25: ((8, 17), (1, 16), (16, 0)),
    26: ((1, 25), (8, 17), (1, 16), (16, 0)), 27: ((2, 25), (8, 17), (1, 16), (16, 0)),
    28: ((4, 24), (8, 16), (16, 0)), 29: ((1, 28), (4, 24), (8, 16), (16, 0)),
    30: ((2, 28), (4, 24), (8, 16), (16, 0)), 31: ((1, 30), (4, 26), (8, 18), (16, 2), (2, 0)),
    32: ((16, 16), (16, 0)),
}
_PERM: dict[int, tuple[int, ...]] = {}


def _perm(n: int, recursive: bool) -> tuple[int, ...]:
    key = n if recursive else -n
    if key in _PERM:
        return _PERM[key]
    out: list[int] = []
    for cnt, idx in _LIT_ORDER[n]:
        if recursive and cnt > 1 and cnt != n:
            out.extend(idx + j for j in _perm(cnt, True))
        else:
            out.extend(range(idx, idx + cnt))
    _PERM[key] = tuple(out)
    return _PERM[key]


# Sub-blocks of a literal run are themselves copied with the order of their own size (verified: the
# non-recursive reading corrupts the R2007 section map names).
LITERAL_RECURSIVE = True


def decompress_r21(src: bytes, out_size: int) -> bytes:
    """R21 LZ77 variant (§5.10)."""
    n = len(src)
    dst = bytearray()
    si = 0

    def take(i: int) -> int:
        if i >= n:
            raise DwgReadError("R21 compressed stream truncated")
        return src[i]

    def literal(count: int) -> None:
        nonlocal si
        if si + count > n:
            raise DwgReadError("R21 literal run truncated")
        while count >= 32:
            chunk = src[si:si + 32]
            dst.extend(chunk[j] for j in _perm(32, LITERAL_RECURSIVE))
            si += 32
            count -= 32
        if count:
            chunk = src[si:si + count]
            dst.extend(chunk[j] for j in _perm(count, LITERAL_RECURSIVE))
            si += count

    def instructions(op: int) -> tuple[int, int, int]:
        nonlocal si
        hi = op >> 4
        if hi == 0:
            length = (op & 0xF) + 0x13
            off = take(si); si += 1
            op = take(si); si += 1
            length = ((op >> 3) & 0x10) + length
            off = ((op & 0x78) << 5) + 1 + off
        elif hi == 1:
            length = (op & 0xF) + 3
            off = take(si); si += 1
            op = take(si); si += 1
            off = ((op & 0xF8) << 5) + 1 + off
        elif hi == 2:
            off = take(si); si += 1
            off = ((take(si) << 8) & 0xFF00) | off; si += 1
            length = op & 7
            if not op & 8:
                op = take(si); si += 1
                length = (op & 0xF8) + length
            else:
                off += 1
                length = (take(si) << 3) + length; si += 1
                op = take(si); si += 1
                length = ((op & 0xF8) << 8) + length + 0x100
        else:
            length = op >> 4
            off = op & 15
            op = take(si); si += 1
            off = ((op & 0xF8) << 1) + off + 1
        return op, off, length

    op = take(0)
    if op >> 4 == 2:
        # "advance 2 bytes, read a length from the next byte & 7, advance another byte" — counted from
        # after the opcode byte (verified on the R2007 file header page)
        si = 4
        length = take(3) & 7
    else:
        si = 1
        length = 0
    limit = out_size + 0x10000
    while True:
        if length == 0:
            length = op + 8
            if length == 0x17:
                b = take(si); si += 1
                length += b
                if b == 0xFF:
                    while True:
                        w = take(si) | (take(si + 1) << 8)
                        si += 2
                        length += w
                        if w != 0xFFFF:
                            break
        literal(length)
        if si >= n:
            break
        op = take(si); si += 1
        op, off, length = instructions(op)
        while True:
            start = len(dst) - off
            if start < 0:
                raise DwgReadError("R21 back-reference before start of data")
            for i in range(length):
                dst.append(dst[start + i])
            if len(dst) > limit:
                raise DwgReadError("R21 decompressed data exceeds declared size")
            length = op & 7
            if length != 0 or si >= n:
                break
            op = take(si); si += 1
            if op >> 4 == 0:
                break
            if op >> 4 == 15:
                op &= 15
            op, off, length = instructions(op)
        if length == 0 and si >= n:
            break
    if len(dst) < out_size:
        dst.extend(b"\0" * (out_size - len(dst)))
    return bytes(dst[:out_size])


def _sys_page(data: bytes, pos: int, csize: int, usize: int, factor: int, what: str) -> bytes:
    """System page (§5.3): RS(255,239) interleaved, the (compressed) data repeated ``factor`` times."""
    if csize <= 0 or usize <= 0 or usize > 64 * 1024 * 1024 or factor <= 0 or factor > 1000 or csize > len(data):
        raise DwgReadError(f"R2007 {what}: implausible sizes")
    aligned = (csize + 7) & ~7
    blocks = (aligned * factor + 238) // 239
    if pos < 0 or pos + blocks * 255 > len(data) + 255:
        raise DwgReadError(f"R2007 {what} outside file")
    raw = rs_data(data[pos:pos + blocks * 255], 239, blocks, True)[:csize]
    return decompress_r21(raw, usize) if csize < usize else raw[:usize]


def open_r2007(data: bytes, vs: str, ver: int, rel: str):
    from pdfcore.ops.plans.dwg.container import (
        SENT_CLASSES, SENT_HEADER, DwgContainer, _strip_sentinel, parse_object_map)

    maint = data[0x0B]
    (codepage,) = struct.unpack_from("<H", data, 0x13)
    (security,) = struct.unpack_from("<I", data, 0x18)
    if security & 1:
        raise DwgReadError("DWG is password-protected/encrypted; not supported")
    if len(data) < 0x80 + 0x3D8:
        raise DwgReadError("R2007 file header truncated")
    dec = rs_data(data[0x80:0x80 + 0x3D8], 239, 3, True)
    _crc, _key, _ccrc, clen, _l2 = struct.unpack_from("<QQQii", dec, 0)
    if clen < 0:
        fh = dec[0x20:0x20 - clen]
    elif 0 < clen <= len(dec) - 0x20:
        fh = decompress_r21(dec[0x20:0x20 + clen], 0x110)
    else:
        raise DwgReadError("R2007 file header: bad compressed length")
    f = struct.unpack_from("<34Q", fh.ljust(0x110, b"\0"), 0)
    if f[0] != 0x70:
        raise DwgReadError("R2007 file header did not decode (bad header size)")
    pm_off, pm_csize, pm_usize, pm_factor = f[7], f[10], f[11], f[3]
    sm_csize, sm_id, sm_usize, sm_factor = f[22], f[24], f[25], f[27]
    pages = _sys_page(data, 0x480 + pm_off, pm_csize, pm_usize, pm_factor, "page map")
    page_pos: dict[int, tuple[int, int]] = {}
    off = 0
    for i in range(0, len(pages) - 15, 16):
        size, pid = struct.unpack_from("<qq", pages, i)
        if size <= 0:
            break
        page_pos[abs(pid)] = (0x480 + off, size)
        off += size
    if sm_id not in page_pos:
        raise DwgReadError("R2007 section map page not found")
    smap = _sys_page(data, page_pos[sm_id][0], sm_csize, sm_usize, sm_factor, "section map")
    sections: dict[str, tuple[int, int, int, list]] = {}
    p = 0
    while p + 64 <= len(smap):
        dsize, maxsize, enc, _hash, nlen, _unk, encoding, npages = struct.unpack_from("<QQQQQQQQ", smap, p)
        p += 64
        if nlen > 1024 or npages > 1_000_000:
            raise DwgReadError("R2007 section map entry implausible")
        # SectionNameLength is a byte count including the UTF-16 terminator (spec §5.2 lists it in chars)
        name = smap[p:p + nlen].decode("utf-16-le", "replace").split("\0", 1)[0]
        p += nlen
        plist = []
        for _ in range(npages):
            if p + 56 > len(smap):
                raise DwgReadError("R2007 section page list truncated")
            poff, psize, pid, pu, pc, _chk, _crc = struct.unpack_from("<QQQQQQQ", smap, p)
            p += 56
            plist.append((poff, psize, pid, pu, pc))
        sections[name] = (dsize, maxsize, encoding, plist, enc)

    def get(name: str) -> bytes:
        if name not in sections:
            raise DwgReadError(f"section {name} not present")
        dsize, maxsize, encoding, plist, enc = sections[name]
        if dsize > max(64 * 1024 * 1024, 64 * len(data)):
            raise DwgReadError(f"{name}: implausible section size {dsize}")
        if enc == 1:
            raise DwgReadError(f"section {name} is encrypted; not supported")
        out = bytearray()
        for poff, psize, pid, pu, pc in plist:
            if pid not in page_pos:
                raise DwgReadError(f"{name}: page {pid} missing from page map")
            pos, _sz = page_pos[pid]
            if pc > len(data) or pu > 64 * 1024 * 1024 or poff > dsize:
                raise DwgReadError(f"{name}: page {pid} has implausible sizes")
            aligned = (pc + 7) & ~7
            blocks = (aligned + 250) // 251
            raw = rs_data(data[pos:pos + blocks * 255], 251, blocks, encoding == 4)[:pc]
            page = decompress_r21(raw, pu) if pc < pu else raw[:pu]
            if poff > len(out):
                out.extend(b"\0" * (poff - len(out)))
            del out[poff:]
            out.extend(page)
        if len(out) < dsize:
            out.extend(b"\0" * (dsize - len(out)))
        return bytes(out[:dsize])

    c = DwgContainer(vs, ver, rel, maint, codepage)
    c.header = _strip_sentinel(get("AcDb:Header"), SENT_HEADER, "header", ver, maint)
    c.classes = _strip_sentinel(get("AcDb:Classes"), SENT_CLASSES, "classes", ver, maint)
    c.objects = get("AcDb:AcDbObjects")
    hnd = get("AcDb:Handles")
    c.handles = parse_object_map(hnd, 0, len(hnd), c.warnings)
    return c
