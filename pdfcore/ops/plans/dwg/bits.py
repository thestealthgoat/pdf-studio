"""Bit-level readers for DWG data (ODA spec chapter 2, "Bit codes and data definitions").

All multi-byte values are little-endian; bits within a byte are consumed most-significant first
(spec §2, see the worked BITSHORT example in §2.2). Every read is bounds-checked against ``end`` and
raises :class:`~pdfcore.ops.plans.dwg.errors.DwgReadError` instead of returning garbage.
"""

from __future__ import annotations

import re
import struct

from pdfcore.ops.plans.dwg.errors import DwgReadError

_unpack_d = struct.Struct("<d").unpack
_PAD = b"\0" * 16

# R2000 / R2004 / R2007 / R2010+ feature switches, set from the file version string.
R13, R14, R2000, R2004, R2007, R2010, R2013, R2018 = 13, 14, 15, 18, 21, 24, 27, 32


class BitReader:
    """Reads the DWG bit codes from a byte buffer.

    ``ver`` is one of the ``R…`` constants above; it selects version-dependent encodings
    (BE/BT in §2.8/§2.10, CMC/ENC in §2.11, object type in §2.12, TV vs TU strings).
    """

    __slots__ = ("data", "pos", "end", "ver", "codec", "strings")

    def __init__(self, data: bytes, ver: int = R2000, start_bit: int = 0, end_bit: int | None = None,
                 codec: str = "cp1252") -> None:
        self.data = bytes(data) + _PAD
        self.pos = start_bit
        self.end = len(data) * 8 if end_bit is None else min(end_bit, len(data) * 8)
        self.ver = ver
        self.codec = codec
        #: R2007+: separate reader positioned on the object's string stream (§20.1), else None
        self.strings: BitReader | None = None

    # ------------------------------------------------------------------ raw bits
    def bits(self, n: int) -> int:
        p = self.pos
        q = p + n
        if q > self.end:
            raise DwgReadError(f"read past end of bit stream (bit {q} > {self.end})")
        i = p >> 3
        j = (q + 7) >> 3
        v = int.from_bytes(self.data[i:j], "big") >> ((j << 3) - q)
        self.pos = q
        return v & ((1 << n) - 1)

    def remaining(self) -> int:
        return self.end - self.pos

    def seek(self, bit: int) -> None:
        if bit < 0 or bit > self.end:
            raise DwgReadError(f"seek outside bit stream ({bit}, end {self.end})")
        self.pos = bit

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7

    # ------------------------------------------------------------------ raw types (§2: RC RS RL RD)
    def b(self) -> int:
        return self.bits(1)

    def bb(self) -> int:
        return self.bits(2)

    def b3(self) -> int:
        """3B (§2.1): 1–3 bits, stop at the first 0 bit."""
        v = 0
        for _ in range(3):
            bit = self.bits(1)
            v = (v << 1) | bit
            if not bit:
                break
        return v

    def rc(self) -> int:
        return self.bits(8)

    def rs(self) -> int:
        v = self.bits(16)
        v = ((v & 0xFF) << 8) | (v >> 8)
        return v - 0x10000 if v & 0x8000 else v

    def rsu(self) -> int:
        v = self.bits(16)
        return ((v & 0xFF) << 8) | (v >> 8)

    def rl(self) -> int:
        v = self.bits(32)
        v = int.from_bytes(v.to_bytes(4, "big"), "little")
        return v - 0x100000000 if v & 0x80000000 else v

    def rlu(self) -> int:
        v = self.bits(32)
        return int.from_bytes(v.to_bytes(4, "big"), "little")

    def rd(self) -> float:
        return _unpack_d(self.bits(64).to_bytes(8, "big"))[0]

    def raw(self, n: int) -> bytes:
        """``n`` raw bytes at the current (possibly unaligned) bit position."""
        if n <= 0:
            return b""
        if n * 8 > self.remaining():
            raise DwgReadError(f"raw read of {n} bytes past end of stream")
        if not self.pos & 7:
            i = self.pos >> 3
            self.pos += n * 8
            return self.data[i:i + n]
        return self.bits(n * 8).to_bytes(n, "big")

    # ------------------------------------------------------------------ compressed types (§2.2–§2.5)
    def bs(self) -> int:
        """BITSHORT (§2.2)."""
        c = self.bits(2)
        if c == 0:
            return self.rs()
        if c == 1:
            return self.bits(8)
        return 0 if c == 2 else 256

    def bl(self) -> int:
        """BITLONG (§2.3)."""
        c = self.bits(2)
        if c == 0:
            return self.rl()
        if c == 1:
            return self.bits(8)
        if c == 2:
            return 0
        raise DwgReadError("invalid BITLONG code 11")

    def bll(self) -> int:
        """BITLONGLONG (§2.4): byte count then little-endian bytes. Files store the count as a plain
        3-bit field (verified on R2013/R2018 headers), not as the variable-length 3B of §2.1."""
        n = self.bits(3)
        v = 0
        for i in range(n):
            v |= self.bits(8) << (8 * i)
        return v

    def bd(self) -> float:
        """BITDOUBLE (§2.5)."""
        c = self.bits(2)
        if c == 0:
            return self.rd()
        if c == 1:
            return 1.0
        if c == 2:
            return 0.0
        raise DwgReadError("invalid BITDOUBLE code 11")

    def dd(self, default: float) -> float:
        """BitDouble with default (§2.9)."""
        c = self.bits(2)
        if c == 0:
            return default
        if c == 3:
            return self.rd()
        raw = bytearray(struct.pack("<d", default))
        if c == 1:
            raw[0:4] = self.raw(4)
        else:
            b = self.raw(6)
            raw[4:6] = b[0:2]
            raw[0:4] = b[2:6]
        return _unpack_d(bytes(raw))[0]

    def be(self) -> tuple[float, float, float]:
        """BitExtrusion (§2.8)."""
        if self.ver >= R2000 and self.bits(1):
            return (0.0, 0.0, 1.0)
        return (self.bd(), self.bd(), self.bd())

    def bt(self) -> float:
        """BitThickness (§2.10)."""
        if self.ver >= R2000 and self.bits(1):
            return 0.0
        return self.bd()

    def mc(self) -> int:
        """Modular char (§2.6), signed."""
        v = 0
        shift = 0
        for _ in range(8):
            byte = self.bits(8)
            if byte & 0x80:
                v |= (byte & 0x7F) << shift
                shift += 7
                continue
            neg = byte & 0x40
            v |= (byte & 0x3F) << shift
            return -v if neg else v
        raise DwgReadError("modular char too long")

    def umc(self) -> int:
        """Modular char without the sign bit (handle-stream size in R2010+ objects, §20.1)."""
        v = 0
        shift = 0
        for _ in range(10):
            byte = self.bits(8)
            v |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return v
        raise DwgReadError("modular char too long")

    def ms(self) -> int:
        """Modular short (§2.7)."""
        v = 0
        shift = 0
        for _ in range(4):
            w = self.rsu()
            v |= (w & 0x7FFF) << shift
            shift += 15
            if not w & 0x8000:
                return v
        raise DwgReadError("modular short too long")

    # ------------------------------------------------------------------ points
    def rd2(self) -> tuple[float, float]:
        return (self.rd(), self.rd())

    def rd3(self) -> tuple[float, float, float]:
        return (self.rd(), self.rd(), self.rd())

    def bd2(self) -> tuple[float, float]:
        return (self.bd(), self.bd())

    def bd3(self) -> tuple[float, float, float]:
        return (self.bd(), self.bd(), self.bd())

    def dd3(self, d: tuple[float, float, float]) -> tuple[float, float, float]:
        return (self.dd(d[0]), self.dd(d[1]), self.dd(d[2]))

    # ------------------------------------------------------------------ handles (§2.13)
    def h(self) -> tuple[int, int]:
        """Handle reference: returns ``(code, value)`` without applying offset codes."""
        code = self.bits(4)
        n = self.bits(4)
        v = 0
        for _ in range(n):
            v = (v << 8) | self.bits(8)
        return code, v

    def href(self, ref: int) -> int:
        """Handle reference resolved against ``ref`` (the owning object's handle) per §2.13."""
        code, v = self.h()
        if code == 6:
            return ref + 1
        if code == 8:
            return ref - 1
        if code == 0xA:
            return ref + v
        if code == 0xC:
            return ref - v
        return v

    # ------------------------------------------------------------------ text
    def tv(self) -> str:
        """TV: 8-bit text (R13–R2004) or, for R2007+, UTF-16 text from the string stream (§2, §20.1)."""
        if self.ver >= R2007:
            src = self.strings
            if src is None:
                return ""
            return src.tu()
        n = self.bs()
        if n <= 0:
            return ""
        raw = self.raw(n)
        return decode_8bit(raw.split(b"\0", 1)[0], self.codec)

    def tu(self) -> str:
        """TU: bitshort character count then UTF-16LE (§2)."""
        n = self.bs()
        if n <= 0:
            return ""
        raw = self.raw(n * 2)
        txt = raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
        if "\\U+" in txt:
            txt = _UNI_S.sub(lambda m: chr(int(m.group(1), 16)), txt)
        return txt

    def t8(self) -> str:
        """T: 8-bit text regardless of version (used for a few R2010+ header strings)."""
        n = self.bs()
        if n <= 0:
            return ""
        return self.raw(n).split(b"\0", 1)[0].decode(self.codec, errors="replace")

    # ------------------------------------------------------------------ colors (§2.11)
    def cmc(self) -> dict:
        """CMC color. R15-: BS index. R2004+: BS index, BL RGB, RC flags (+ names)."""
        if self.ver < R2004:
            return {"aci": self.bs()}
        idx = self.bs()
        rgb = self.bl() & 0xFFFFFFFF
        flags = self.rc()
        out: dict = {"aci": idx, "rgb_raw": rgb}
        if flags & 1:
            out["name"] = self.tv()
        if flags & 2:
            out["book"] = self.tv()
        return _decode_rgb(out)

    def enc(self) -> dict:
        """Entity color (ENC, §2.11): R15- plain BS index; R2004+ flags + optional RGB/handle/transparency."""
        if self.ver < R2004:
            return {"aci": self.bs()}
        v = self.bs()
        flags = v & 0xFF00
        out: dict = {"aci": v & 0x1FF}
        if flags & 0x8000:
            rgb = self.bl() & 0xFFFFFFFF
            out["rgb_raw"] = rgb
            _decode_rgb(out)
        if flags & 0x4000:
            out["has_book_handle"] = True
        if flags & 0x2000:
            out["transparency"] = self.bl() & 0xFFFFFFFF
        return out


_UNI = re.compile(rb"\\U\+([0-9A-Fa-f]{4})")
_UNI_S = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def decode_8bit(raw: bytes, codec: str) -> str:
    """8-bit DWG text: code-page bytes, with characters outside the code page written as ``\\U+XXXX``
    (the same escape DXF uses); the escapes are turned back into Unicode."""
    if b"\\U+" not in raw:
        return raw.decode(codec, errors="replace")
    out = []
    pos = 0
    for m in _UNI.finditer(raw):
        out.append(raw[pos:m.start()].decode(codec, errors="replace"))
        out.append(chr(int(m.group(1), 16)))
        pos = m.end()
    out.append(raw[pos:].decode(codec, errors="replace"))
    return "".join(out)


def _decode_rgb(out: dict) -> dict:
    """Interpret the 32-bit color word: high byte 0xC2 = true color, 0xC3 = ACI in the low byte,
    0xC0/0xC1 = BYLAYER/BYBLOCK (the DXF group 420 / AcCmColor method byte)."""
    rgb = out.get("rgb_raw")
    if rgb is None:
        return out
    method = rgb >> 24
    if method == 0xC2:
        out["rgb"] = rgb & 0xFFFFFF
    elif method == 0xC3:
        out["aci"] = rgb & 0xFF
    elif method == 0xC0:
        out["aci"] = 256
    elif method == 0xC1:
        out["aci"] = 0
    elif method == 0 and rgb and not out.get("aci"):
        out["rgb"] = rgb & 0xFFFFFF
    return out


# ---------------------------------------------------------------------- byte helpers
def mc_bytes(data: bytes, pos: int, signed: bool = True) -> tuple[int, int]:
    """Modular char from a byte buffer (object map, §23). Returns (value, new_pos)."""
    v = 0
    shift = 0
    n = len(data)
    while True:
        if pos >= n:
            raise DwgReadError("modular char runs past end of data")
        byte = data[pos]
        pos += 1
        if byte & 0x80:
            v |= (byte & 0x7F) << shift
            shift += 7
            if shift > 63:
                raise DwgReadError("modular char too long")
            continue
        if signed and byte & 0x40:
            v |= (byte & 0x3F) << shift
            return -v, pos
        v |= (byte & (0x3F if signed else 0x7F)) << shift
        return v, pos


def ms_bytes(data: bytes, pos: int) -> tuple[int, int]:
    """Modular short (§2.7) from a byte buffer. Returns (value, new_pos)."""
    v = 0
    shift = 0
    for _ in range(4):
        if pos + 2 > len(data):
            raise DwgReadError("modular short runs past end of data")
        w = data[pos] | (data[pos + 1] << 8)
        pos += 2
        v |= (w & 0x7FFF) << shift
        shift += 15
        if not w & 0x8000:
            return v, pos
    raise DwgReadError("modular short too long")


_CRC8 = None


def crc8(seed: int, data: bytes) -> int:
    """The 16-bit table CRC of §2.14.1."""
    global _CRC8
    if _CRC8 is None:
        tbl = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
            tbl.append(c)
        _CRC8 = tbl
    t = _CRC8
    dx = seed & 0xFFFF
    for byte in data:
        dx = (dx >> 8) ^ t[(byte ^ dx) & 0xFF]
    return dx & 0xFFFF
