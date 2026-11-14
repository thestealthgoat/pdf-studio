"""Bit-level writer for DWG data — the inverse of :class:`~pdfcore.ops.plans.dwg.bits.BitReader`.

Implements the bit codes of ODA spec chapter 2 ("Bit codes and data definitions"): B, BB, BS (§2.2),
BL (§2.3), BD (§2.5), MC/MS (§2.6/§2.7, byte-oriented helpers), BE (§2.8), DD (§2.9), BT (§2.10), CMC for
R15 and earlier (§2.11: a plain BS colour index), object type (§2.12, BS before R2010), handle references
(§2.13) and 8-bit TV text. Bits are emitted most-significant first and multi-byte raw values little-endian,
exactly as the reader consumes them; every encoder here is checked against the reader in the tests.

Text: R13–R2004 strings are 8-bit in the drawing code page. Characters the code page cannot represent are
written as the ``\\U+XXXX`` escape (the form DXF and DWG share, and which the reader turns back into
Unicode); characters outside the Basic Multilingual Plane, which that escape cannot express, become ``?``.
Every substitution is counted in :attr:`BitWriter.text_stats`.
"""

from __future__ import annotations

import math
import struct
from collections import Counter

from pdfcore.ops.plans.dwg.bits import crc8

_pack_d = struct.Struct("<d").pack
_ONE = _pack_d(1.0)
_ZERO = _pack_d(0.0)


class BitWriter:
    """Accumulates DWG bit codes. ``tell()`` is the bit length so far; ``getvalue()`` pads to a byte."""

    __slots__ = ("buf", "acc", "nacc", "codec", "text_stats")

    def __init__(self, codec: str = "cp1252", text_stats: Counter | None = None) -> None:
        self.buf = bytearray()
        self.acc = 0
        self.nacc = 0
        self.codec = codec
        #: ``escaped`` / ``replaced`` / ``truncated`` character counts from :meth:`tv`
        self.text_stats: Counter = text_stats if text_stats is not None else Counter()

    # ------------------------------------------------------------------ raw bits
    def bits(self, value: int, n: int) -> None:
        if n <= 0:
            return
        self.acc = (self.acc << n) | (value & ((1 << n) - 1))
        self.nacc += n
        if self.nacc >= 64:
            k = self.nacc >> 3
            rem = self.nacc & 7
            self.buf += (self.acc >> rem).to_bytes(k, "big")
            self.acc &= (1 << rem) - 1
            self.nacc = rem

    def tell(self) -> int:
        return len(self.buf) * 8 + self.nacc

    def getvalue(self) -> bytes:
        """The bits written so far, zero-padded to the next byte boundary (§2.14: "padding bits")."""
        out = bytearray(self.buf)
        if self.nacc:
            pad = -self.nacc % 8
            out += (self.acc << pad).to_bytes((self.nacc + pad) // 8, "big")
        return bytes(out)

    def append(self, other: "BitWriter") -> None:
        """Append another writer's bits (not padded)."""
        data = other.buf
        for i in range(0, len(data), 8):
            chunk = data[i:i + 8]
            self.bits(int.from_bytes(chunk, "big"), len(chunk) * 8)
        self.bits(other.acc, other.nacc)

    def raw(self, data: bytes) -> None:
        """Raw bytes at the current (possibly unaligned) bit position."""
        for i in range(0, len(data), 8):
            chunk = data[i:i + 8]
            self.bits(int.from_bytes(chunk, "big"), len(chunk) * 8)

    def align(self) -> None:
        self.bits(0, -self.tell() % 8)

    # ------------------------------------------------------------------ raw types (§2)
    def b(self, v: int | bool) -> None:
        self.bits(1 if v else 0, 1)

    def bb(self, v: int) -> None:
        self.bits(v & 3, 2)

    def rc(self, v: int) -> None:
        self.bits(v & 0xFF, 8)

    def rs(self, v: int) -> None:
        self.bits(int.from_bytes(struct.pack("<H", v & 0xFFFF), "big"), 16)

    def rl(self, v: int) -> None:
        self.bits(int.from_bytes(struct.pack("<I", v & 0xFFFFFFFF), "big"), 32)

    def rd(self, v: float) -> None:
        self.bits(int.from_bytes(_pack_d(float(v)), "big"), 64)

    # ------------------------------------------------------------------ compressed types (§2.2–§2.5)
    def bs(self, v: int) -> None:
        """BITSHORT (§2.2): 10 = 0, 11 = 256, 01 + byte for 0..255, else 00 + RS."""
        v = int(v)
        if v == 0:
            self.bits(0b10, 2)
        elif v == 256:
            self.bits(0b11, 2)
        elif 0 < v < 256:
            self.bits(0b01, 2)
            self.bits(v, 8)
        else:
            if not -32768 <= v <= 65535:
                raise ValueError(f"BITSHORT value {v} out of range")
            self.bits(0b00, 2)
            self.rs(v)

    def bl(self, v: int) -> None:
        """BITLONG (§2.3): 10 = 0, 01 + byte for 1..255, else 00 + RL (code 11 is not used)."""
        v = int(v)
        if v == 0:
            self.bits(0b10, 2)
        elif 0 < v < 256:
            self.bits(0b01, 2)
            self.bits(v, 8)
        else:
            if not -(1 << 31) <= v < (1 << 32):
                raise ValueError(f"BITLONG value {v} out of range")
            self.bits(0b00, 2)
            self.rl(v)

    def bd(self, v: float) -> None:
        """BITDOUBLE (§2.5): 01 = 1.0, 10 = 0.0, 00 + RD otherwise (-0.0 and non-finite stored raw)."""
        raw = _pack_d(float(v))
        if raw == _ONE:
            self.bits(0b01, 2)
        elif raw == _ZERO:
            self.bits(0b10, 2)
        else:
            self.bits(0b00, 2)
            self.bits(int.from_bytes(raw, "big"), 64)

    def dd(self, v: float, default: float) -> None:
        """BitDouble with default (§2.9): patch the default's low 4 or 6 bytes, or store a full RD."""
        raw = _pack_d(float(v))
        d = _pack_d(float(default))
        if raw == d:
            self.bits(0b00, 2)
        elif raw[4:8] == d[4:8]:
            self.bits(0b01, 2)
            self.raw(raw[0:4])
        elif raw[6:8] == d[6:8]:
            self.bits(0b10, 2)
            self.raw(raw[4:6] + raw[0:4])
        else:
            self.bits(0b11, 2)
            self.rd(v)

    def be(self, v) -> None:
        """BitExtrusion (§2.8, R2000+): 1 = (0,0,1), else 0 + 3BD."""
        x, y, z = (float(c) for c in v)
        if _pack_d(x) == _ZERO and _pack_d(y) == _ZERO and _pack_d(z) == _ONE:
            self.b(1)
        else:
            self.b(0)
            self.bd(x)
            self.bd(y)
            self.bd(z)

    def bt(self, v: float) -> None:
        """BitThickness (§2.10, R2000+): 1 = 0.0, else 0 + BD."""
        if _pack_d(float(v)) == _ZERO:
            self.b(1)
        else:
            self.b(0)
            self.bd(v)

    # ------------------------------------------------------------------ points
    def rd2(self, p) -> None:
        self.rd(p[0])
        self.rd(p[1])

    def rd3(self, p) -> None:
        self.rd(p[0])
        self.rd(p[1])
        self.rd(p[2])

    def bd2(self, p) -> None:
        self.bd(p[0])
        self.bd(p[1])

    def bd3(self, p) -> None:
        self.bd(p[0])
        self.bd(p[1])
        self.bd(p[2])

    # ------------------------------------------------------------------ handles (§2.13)
    def h(self, code: int, value: int) -> None:
        """Handle reference ``CODE.COUNTER.BYTES`` with the handle bytes most-significant first."""
        value = int(value)
        if value < 0:
            raise ValueError("negative handle")
        n = (value.bit_length() + 7) // 8
        self.bits(code & 0xF, 4)
        self.bits(n, 4)
        if n:
            self.bits(value, 8 * n)

    # ------------------------------------------------------------------ text / colour
    def tv(self, s: str | None, limit: int = 32767) -> None:
        """TV for R13–R2004 (§2): BS byte count, then 8-bit code-page text without a terminator."""
        raw = encode_8bit(s or "", self.codec, self.text_stats)
        if len(raw) > limit:
            self.text_stats["truncated"] += len(raw) - limit
            raw = raw[:limit]
        self.bs(len(raw))
        self.raw(raw)

    def cmc(self, aci: int) -> None:
        """CmColor for R15 and earlier (§2.11): a BS colour index (256 = BYLAYER, 0 = BYBLOCK)."""
        self.bs(aci)


def encode_8bit(text: str, codec: str = "cp1252", stats: Counter | None = None) -> bytes:
    """Encode for an 8-bit DWG string; unrepresentable BMP characters become ``\\U+XXXX`` (counted as
    ``escaped``), astral characters and NUL become ``?`` (counted as ``replaced``)."""
    try:
        if "\0" not in text:
            return text.encode(codec)
    except UnicodeEncodeError:
        pass
    out = bytearray()
    for ch in text:
        try:
            if ch == "\0":
                raise UnicodeEncodeError(codec, ch, 0, 1, "nul")
            out += ch.encode(codec)
        except UnicodeEncodeError:
            cp = ord(ch)
            if 0 < cp <= 0xFFFF:
                out += f"\\U+{cp:04X}".encode("ascii")
                if stats is not None:
                    stats["escaped"] += 1
            else:
                out += b"?"
                if stats is not None:
                    stats["replaced"] += 1
    return bytes(out)


# ---------------------------------------------------------------------- byte-oriented helpers
def ms_encode(n: int) -> bytes:
    """Modular short (§2.7): 15-bit little-endian words, high bit = "more follows"."""
    if n < 0:
        raise ValueError("negative modular short")
    out = bytearray()
    while True:
        w = n & 0x7FFF
        n >>= 15
        if n:
            out += struct.pack("<H", w | 0x8000)
        else:
            out += struct.pack("<H", w)
            return bytes(out)


def mc_encode(n: int, signed: bool = True) -> bytes:
    """Modular char (§2.6): 7-bit groups, high bit = "more follows"; when ``signed`` the last byte's
    0x40 bit is the sign (the object-map location deltas, §23)."""
    neg = signed and n < 0
    v = -n if neg else n
    if v < 0:
        raise ValueError("negative unsigned modular char")
    out = bytearray()
    lim = 0x3F if signed else 0x7F
    while v > lim:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    out.append(v | (0x40 if neg else 0))
    return bytes(out)


def set_bits(buf: bytearray, bitpos: int, value: int, n: int) -> None:
    """Overwrite ``n`` bits at ``bitpos`` (MSB-first) in ``buf`` — used to patch the RL object size."""
    for i in range(n):
        bit = (value >> (n - 1 - i)) & 1
        p = bitpos + i
        byte, off = divmod(p, 8)
        mask = 0x80 >> off
        if bit:
            buf[byte] |= mask
        else:
            buf[byte] &= ~mask & 0xFF


def rl_bits(v: int) -> int:
    """The 32-bit pattern :meth:`BitWriter.rl` emits for ``v``."""
    return int.from_bytes(struct.pack("<I", v & 0xFFFFFFFF), "big")


def crc_le(seed: int, data: bytes) -> bytes:
    """§2.14.1 CRC of ``data`` as the two little-endian bytes stored after objects and sections."""
    return struct.pack("<H", crc8(seed, data))


def finite(v: float, default: float = 0.0) -> float:
    """Replace NaN/inf (which no CAD program accepts) with ``default``."""
    v = float(v)
    return v if math.isfinite(v) else default
