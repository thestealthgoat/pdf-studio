"""Native, clean-room DWG reader (MIT) — DWG → ``ezdxf`` document, no external converter.

Implementation basis
--------------------
Written only from the publicly published **Open Design Specification for .dwg files, version 5.4.1**
(Open Design Alliance, opendesign.com guest download) and ezdxf's public documentation for the target
object model. No other DWG implementation's source code was consulted. Section numbers in docstrings refer
to that spec version. LibreDWG's ``dwg2dxf`` binary was used only as a black-box test oracle on sample
files during development; it is not a dependency.

Layout
------
* :mod:`.bits`      — bit codes (§2): B, BB, 3B, BS, BL, BLL, BD, DD, RC, RS, RL, RD, MC, MS, H, TV/TU, CMC/ENC, BE, BT
* :mod:`.container` — file header, section locators (§3), R2004+ page/section maps + LZ77 (§4), object map (§23)
* :mod:`.r2007`     — R2007 (AC1021) Reed–Solomon pages and compressor (§5)
* :mod:`.header`    — header variables (§9)
* :mod:`.objects`   — classes (§10) and object/entity prescriptions (§20)
* :mod:`.build`     — mapping onto an ezdxf ``Drawing``

Supported versions: R2000 (AC1015), R2004 (AC1018), R2007 (AC1021), R2010 (AC1024), R2013 (AC1027),
R2018 (AC1032). R13/R14 are recognised but rejected with :class:`DwgReadError`.

Where real files differ from the spec text
------------------------------------------
Found by bit-level inspection of sample files (each is commented at the code that handles it):

* §23 object map: handle and offset deltas restart from 0 in every map section (not once per map).
* §20.4.1: from R2004 on there is no "Nolinks" bit at all (the spec lists it with "always 1").
* §20.4.54ff table records, R2007+: no "xrefindex+1" BS between the 64-flag and Xdep bits.
* §20.4.84 LAYOUT: the R2004+ viewport count is a BL, not an RL.
* §2.4 BLL: the byte count is a fixed 3-bit field, not the variable-length 3B.
* §9/§10 R2007+: header and class data start with an RL bit size; strings sit at its end.
* §5.10 R21 compression: a leading 0x2X opcode skips 3 bytes after the opcode and takes the length from
  the 4th; literal sub-blocks are themselves reordered by their own size. §5.2 section-name length is in
  bytes. §20.4.47 LEADER: the tail after the end-point projection differs in R2007+ (read best-effort).

Usage::

    from pdfcore.ops.plans.dwg import read_dwg
    doc, report = read_dwg("plan.dwg")
    doc.saveas("plan.dxf")
"""

from __future__ import annotations

import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pdfcore.ops.plans.dwg.bits import R2007, BitReader
from pdfcore.ops.plans.dwg.container import VERSIONS, peek_version
from pdfcore.ops.plans.dwg.errors import DwgReadError

SPEC_VERSION = "Open Design Specification for .dwg files v5.4.1"
#: version strings :func:`read_dwg` can read
SUPPORTED_VERSIONS = ("AC1015", "AC1018", "AC1021", "AC1024", "AC1027", "AC1032")

__all__ = ["read_dwg", "ReadReport", "DwgReadError", "peek_version", "can_read", "SUPPORTED_VERSIONS", "SPEC_VERSION"]


@dataclass
class ReadReport:
    """What :func:`read_dwg` found. ``entities`` counts converted entities by DXF type (all layouts and
    blocks, attributes included); ``skipped`` counts entities that were not converted, by DWG type."""

    path: str
    version: str
    release: str
    objects: int = 0
    entities: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    layers: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    layouts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        ents = ", ".join(f"{k} {v}" for k, v in sorted(self.entities.items()))
        skip = ", ".join(f"{k} {v}" for k, v in sorted(self.skipped.items())) or "none"
        return (f"{Path(self.path).name}: {self.release} ({self.version}), {self.objects} objects, "
                f"{sum(self.entities.values())} entities [{ents}]; skipped: {skip}; "
                f"{len(self.warnings)} warnings; {self.seconds:.2f}s")


def can_read(path: str | Path) -> bool:
    """True when the file looks like a DWG of a version this reader supports (peeks 6 bytes)."""
    try:
        return peek_version(Path(path)) in SUPPORTED_VERSIONS
    except OSError:
        return False


def read_dwg(path: str | Path):
    """Read a DWG file into an ezdxf document. Returns ``(doc, ReadReport)``.

    Raises :class:`DwgReadError` for anything that is not a readable DWG (wrong signature, unsupported
    version, encryption, corrupt section structure). Individual objects that fail to decode are skipped
    and listed in ``report.warnings``.
    """
    t0 = time.perf_counter()
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise DwgReadError(f"cannot read {p}: {exc}") from exc
    try:
        return _read(data, str(p), t0)
    except DwgReadError:
        raise
    except (IndexError, ValueError, KeyError, OverflowError, MemoryError, RecursionError, ZeroDivisionError,
            TypeError, AttributeError, struct.error, UnicodeError) as exc:
        raise DwgReadError(f"{p.name}: malformed DWG data ({type(exc).__name__}: {exc})") from exc


def _read(data: bytes, name: str, t0: float):
    from pdfcore.ops.plans.dwg.build import Builder
    from pdfcore.ops.plans.dwg.container import open_container
    from pdfcore.ops.plans.dwg.objects import ObjectReader, read_classes

    vs = peek_version(data)
    if vs is not None and vs in VERSIONS and vs not in SUPPORTED_VERSIONS:
        raise DwgReadError(f"DWG version {vs} ({VERSIONS[vs][1]}) is not supported by the native reader")
    c = open_container(data)
    hdr = _header(c)
    rep = ReadReport(name, c.version, c.release, warnings=list(c.warnings))
    classes = read_classes(c.classes, c.ver, c.maint, c.codec)
    rd = ObjectReader(c.objects, c.ver, c.codec, classes)
    objs = {}
    bad = 0
    undecoded: Counter = Counter()
    for h, off in c.handles.items():
        try:
            o = rd.read(off)
        except DwgReadError as exc:
            bad += 1
            if getattr(exc, "is_entity", False):
                undecoded[exc.obj_name] += 1  # type: ignore[attr-defined]
            if bad <= 20:
                rep.warnings.append(f"object {h:X} at 0x{off:x}: {exc}")
            continue
        objs[h] = o
    if bad > 20:
        rep.warnings.append(f"... {bad - 20} more objects failed to decode")
    if not objs:
        raise DwgReadError("no objects could be decoded")
    rep.objects = len(objs)
    b = Builder(objs, hdr, c.ver, rep.warnings)
    doc = b.build()
    rep.entities = b.counts
    rep.skipped = b.skipped + undecoded
    rep.layers = [lay.dxf.name for lay in doc.layers]
    rep.blocks = [blk.name for blk in doc.blocks if not blk.name.lower().startswith(("*model_space", "*paper_space"))]
    rep.layouts = [lay.name for lay in doc.layouts]
    rep.seconds = time.perf_counter() - t0
    return doc, rep


def _header(c) -> dict:
    from pdfcore.ops.plans.dwg.header import read_header

    r = BitReader(c.header, c.ver, codec=c.codec)
    try:
        if c.ver >= R2007:
            from pdfcore.ops.plans.dwg.r2007 import header_streams

            r, s, hh = header_streams(c)
            return read_header(r, s, hh, c.ver, c.maint)
        return read_header(r, r, r, c.ver, c.maint)
    except DwgReadError as exc:
        c.warnings.append(f"header variables: {exc}")
        return {}
