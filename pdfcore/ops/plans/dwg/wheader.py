"""AcDb:Header writer — the R2000 (AC1015) header variables of ODA spec chapter 9, in spec order.

The section is one bit stream with no index (§9), so every variable is written, including the ones a
DXF document cannot express (unknown doubles/strings at the start, the trailing four unknown BS values):
those get the defaults the spec lists or the values AutoCAD-saved R2000 samples carry. Values that exist
in the ezdxf header (``$LTSCALE``, ``$INSUNITS``, ``$EXTMIN`` …) are taken from the document; handles come
from the writer's table of fixed objects (``refs``). The order below mirrors
:func:`pdfcore.ops.plans.dwg.header.read_header` restricted to R2000, which is checked by reading every
written file back.
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any, Callable

from pdfcore.ops.plans.dwg.bitwriter import BitWriter

#: header variables we take from the ezdxf document when present: name -> (bit code, default)
_DEFAULTS: dict[str, Any] = {
    "$DIMASO": 1, "$DIMSHO": 1, "$PLINEGEN": 0, "$ORTHOMODE": 0, "$REGENMODE": 1, "$FILLMODE": 1,
    "$QTEXTMODE": 0, "$PSLTSCALE": 1, "$LIMCHECK": 0, "$USRTIMER": 1, "$SKPOLY": 0, "$ANGDIR": 0,
    "$SPLFRAME": 0, "$MIRRTEXT": 0, "$WORLDVIEW": 1, "$TILEMODE": 1, "$PLIMCHECK": 0, "$VISRETAIN": 1,
    "$DISPSILH": 0, "$PELLIPSE": 0, "$PROXYGRAPHICS": 1, "$TREEDEPTH": 3020, "$LUNITS": 2, "$LUPREC": 4,
    "$AUNITS": 0, "$AUPREC": 0, "$ATTMODE": 1, "$PDMODE": 0, "$USERI1": 0, "$USERI2": 0, "$USERI3": 0,
    "$USERI4": 0, "$USERI5": 0, "$SPLINESEGS": 8, "$SURFU": 6, "$SURFV": 6, "$SURFTYPE": 6, "$SURFTAB1": 6,
    "$SURFTAB2": 6, "$SPLINETYPE": 6, "$SHADEDGE": 3, "$SHADEDIF": 70, "$UNITMODE": 0, "$MAXACTVP": 64,
    "$ISOLINES": 4, "$CMLJUST": 0, "$TEXTQLTY": 50, "$LTSCALE": 1.0, "$TEXTSIZE": 2.5, "$TRACEWID": 1.0,
    "$SKETCHINC": 1.0, "$FILLETRAD": 0.0, "$THICKNESS": 0.0, "$ANGBASE": 0.0, "$PDSIZE": 0.0,
    "$PLINEWID": 0.0, "$USERR1": 0.0, "$USERR2": 0.0, "$USERR3": 0.0, "$USERR4": 0.0, "$USERR5": 0.0,
    "$CHAMFERA": 0.0, "$CHAMFERB": 0.0, "$CHAMFERC": 0.0, "$CHAMFERD": 0.0, "$FACETRES": 0.5,
    "$CMLSCALE": 20.0, "$CELTSCALE": 1.0, "$PSVPSCALE": 0.0, "$CECOLOR": 256,
    "$PINSBASE": (0.0, 0.0, 0.0), "$PEXTMIN": (0.0, 0.0, 0.0), "$PEXTMAX": (0.0, 0.0, 0.0),
    "$PLIMMIN": (0.0, 0.0), "$PLIMMAX": (12.0, 9.0), "$PELEVATION": 0.0, "$PUCSORG": (0.0, 0.0, 0.0),
    "$PUCSXDIR": (1.0, 0.0, 0.0), "$PUCSYDIR": (0.0, 1.0, 0.0),
    "$INSBASE": (0.0, 0.0, 0.0), "$EXTMIN": (0.0, 0.0, 0.0), "$EXTMAX": (0.0, 0.0, 0.0),
    "$LIMMIN": (0.0, 0.0), "$LIMMAX": (420.0, 297.0), "$ELEVATION": 0.0, "$UCSORG": (0.0, 0.0, 0.0),
    "$UCSXDIR": (1.0, 0.0, 0.0), "$UCSYDIR": (0.0, 1.0, 0.0),
    "$DIMPOST": "", "$DIMAPOST": "", "$DIMSCALE": 1.0, "$DIMASZ": 2.5, "$DIMEXO": 0.625, "$DIMDLI": 3.75,
    "$DIMEXE": 1.25, "$DIMRND": 0.0, "$DIMDLE": 0.0, "$DIMTP": 0.0, "$DIMTM": 0.0, "$DIMTOL": 0, "$DIMLIM": 0,
    "$DIMTIH": 0, "$DIMTOH": 0, "$DIMSE1": 0, "$DIMSE2": 0, "$DIMTAD": 1, "$DIMZIN": 8, "$DIMAZIN": 0,
    "$DIMTXT": 2.5, "$DIMCEN": 2.5, "$DIMTSZ": 0.0, "$DIMALTF": 0.0393701, "$DIMLFAC": 1.0, "$DIMTVP": 0.0,
    "$DIMTFAC": 1.0, "$DIMGAP": 0.625, "$DIMALTRND": 0.0, "$DIMALT": 0, "$DIMALTD": 3, "$DIMTOFL": 1,
    "$DIMSAH": 0, "$DIMTIX": 0, "$DIMSOXD": 0, "$DIMCLRD": 0, "$DIMCLRE": 0, "$DIMCLRT": 0, "$DIMADEC": 0,
    "$DIMDEC": 2, "$DIMTDEC": 2, "$DIMALTU": 2, "$DIMALTTD": 3, "$DIMAUNIT": 0, "$DIMFRAC": 0, "$DIMLUNIT": 2,
    "$DIMDSEP": 46, "$DIMTMOVE": 0, "$DIMJUST": 0, "$DIMSD1": 0, "$DIMSD2": 0, "$DIMTOLJ": 0, "$DIMTZIN": 8,
    "$DIMALTZ": 0, "$DIMALTTZ": 0, "$DIMUPT": 0, "$DIMATFIT": 3, "$DIMLWD": -2, "$DIMLWE": -2,
    "$TSTACKALIGN": 1, "$TSTACKSIZE": 70, "$HYPERLINKBASE": "", "$STYLESHEET": "", "$INSUNITS": 0,
    "$CELWEIGHT": -1, "$LWDISPLAY": 0, "$PSTYLEMODE": 1, "$EXTNAMES": 1, "$XEDIT": 1, "$OLESTARTUP": 0,
    "$ENDCAPS": 0, "$JOINSTYLE": 0, "$MENU": ".",
}

#: DXF lineweight (1/100 mm) -> DWG lineweight index (§20.4.1 "Lineweight RC", layer flags 0x3E0)
LW_VALUES = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]


def lineweight_index(lw: int) -> int:
    """DXF lineweight → DWG index: -1 BYLAYER → 29, -2 BYBLOCK → 30, -3 DEFAULT → 31, else the nearest
    standard weight's position in :data:`LW_VALUES`."""
    lw = int(lw)
    if lw in (-1, -2, -3):
        return {-1: 29, -2: 30, -3: 31}[lw]
    if lw < 0:
        return 29
    best = min(range(len(LW_VALUES)), key=lambda i: abs(LW_VALUES[i] - lw))
    return best


def julian(t: float | None = None) -> tuple[int, int]:
    """(Julian day number, milliseconds into the day) — the TIMEBLL pair of §9 (TDCREATE …)."""
    t = time.time() if t is None else t
    jd = t / 86400.0 + 2440587.5
    day = int(jd)
    ms = int(round((jd - day) * 86400000.0))
    if ms >= 86400000:
        day, ms = day + 1, ms - 86400000
    return day, ms


def write_header(hv: Callable[[str], Any], refs: dict[str, int], handseed: int, codec: str,
                 stats) -> bytes:
    """Encode the header variables (R2000 path of §9) and return the padded data bytes.

    ``hv(name)`` returns the document value of a header variable or the R2000 default; ``refs`` holds the
    handles of the fixed objects (``CLAYER``, ``BLOCK_CONTROL`` …, 0 = NULL)."""
    w = BitWriter(codec, stats)

    def H(code: int, key: str) -> None:
        w.h(code, refs.get(key, 0))

    def B(name: str) -> None:
        w.b(int(bool(hv(name))))

    def BS(name: str) -> None:
        w.bs(int(hv(name)))

    def BD(name: str) -> None:
        w.bd(_fin(hv(name)))

    def P3(name: str) -> None:
        w.bd3([_fin(c) for c in _pt(hv(name), 3)])

    def P2(name: str) -> None:
        w.rd2([_fin(c) for c in _pt(hv(name), 2)])

    # §9 "Common": unknowns with their documented defaults (the strings carry unit names in samples)
    w.bd(412148564080.0)
    w.bd(1.0)
    w.bd(1.0)
    w.bd(1.0)
    w.tv("m")
    w.tv("")
    w.tv("")
    w.tv("")
    w.bl(24)
    w.bl(0)
    H(5, "VP_ENT_HDR_CURRENT")  # pre-2004: current viewport entity header (hard pointer)
    for n in ("$DIMASO", "$DIMSHO", "$PLINEGEN", "$ORTHOMODE", "$REGENMODE", "$FILLMODE", "$QTEXTMODE",
              "$PSLTSCALE", "$LIMCHECK", "$USRTIMER", "$SKPOLY", "$ANGDIR", "$SPLFRAME", "$MIRRTEXT",
              "$WORLDVIEW", "$TILEMODE", "$PLIMCHECK", "$VISRETAIN", "$DISPSILH", "$PELLIPSE"):
        B(n)
    for n in ("$PROXYGRAPHICS", "$TREEDEPTH", "$LUNITS", "$LUPREC", "$AUNITS", "$AUPREC", "$ATTMODE", "$PDMODE",
              "$USERI1", "$USERI2", "$USERI3", "$USERI4", "$USERI5", "$SPLINESEGS", "$SURFU", "$SURFV",
              "$SURFTYPE", "$SURFTAB1", "$SURFTAB2", "$SPLINETYPE", "$SHADEDGE", "$SHADEDIF", "$UNITMODE",
              "$MAXACTVP", "$ISOLINES", "$CMLJUST", "$TEXTQLTY"):
        BS(n)
    for n in ("$LTSCALE", "$TEXTSIZE", "$TRACEWID", "$SKETCHINC", "$FILLETRAD", "$THICKNESS"):
        BD(n)
    w.bd(math.radians(_fin(hv("$ANGBASE"))))  # stored in radians (DXF: degrees)
    for n in ("$PDSIZE", "$PLINEWID", "$USERR1", "$USERR2", "$USERR3", "$USERR4", "$USERR5", "$CHAMFERA",
              "$CHAMFERB", "$CHAMFERC", "$CHAMFERD", "$FACETRES", "$CMLSCALE", "$CELTSCALE"):
        BD(n)
    w.tv(str(hv("$MENU") or "."))  # MENUNAME (R13–R2004)
    day, ms = julian()
    for v in (day, ms, day, ms, 0, 0, 0, 0):  # TDCREATE, TDUPDATE, TDINDWG, TDUSRTIMER
        w.bl(v)
    w.cmc(int(hv("$CECOLOR")))
    w.h(0, handseed)  # HANDSEED: code 0, in the data stream
    H(5, "CLAYER")
    H(5, "TEXTSTYLE")
    H(5, "CELTYPE")
    H(5, "DIMSTYLE")
    H(5, "CMLSTYLE")
    BD("$PSVPSCALE")
    for n in ("$PINSBASE", "$PEXTMIN", "$PEXTMAX"):
        P3(n)
    P2("$PLIMMIN")
    P2("$PLIMMAX")
    BD("$PELEVATION")
    for n in ("$PUCSORG", "$PUCSXDIR", "$PUCSYDIR"):
        P3(n)
    w.h(5, 0)  # PUCSNAME
    w.h(5, 0)  # PUCSORTHOREF
    w.bs(0)    # PUCSORTHOVIEW
    w.h(5, 0)  # PUCSBASE
    for _ in range(6):  # PUCSORGTOP … PUCSORGBACK
        w.bd3((0.0, 0.0, 0.0))
    for n in ("$INSBASE", "$EXTMIN", "$EXTMAX"):
        P3(n)
    P2("$LIMMIN")
    P2("$LIMMAX")
    BD("$ELEVATION")
    for n in ("$UCSORG", "$UCSXDIR", "$UCSYDIR"):
        P3(n)
    w.h(5, 0)  # UCSNAME
    w.h(5, 0)  # UCSORTHOREF
    w.bs(0)
    w.h(5, 0)  # UCSBASE
    for _ in range(6):
        w.bd3((0.0, 0.0, 0.0))
    w.tv(str(hv("$DIMPOST") or ""))
    w.tv(str(hv("$DIMAPOST") or ""))
    for n in ("$DIMSCALE", "$DIMASZ", "$DIMEXO", "$DIMDLI", "$DIMEXE", "$DIMRND", "$DIMDLE", "$DIMTP", "$DIMTM"):
        BD(n)
    for n in ("$DIMTOL", "$DIMLIM", "$DIMTIH", "$DIMTOH", "$DIMSE1", "$DIMSE2"):
        B(n)
    for n in ("$DIMTAD", "$DIMZIN", "$DIMAZIN"):
        BS(n)
    for n in ("$DIMTXT", "$DIMCEN", "$DIMTSZ", "$DIMALTF", "$DIMLFAC", "$DIMTVP", "$DIMTFAC", "$DIMGAP",
              "$DIMALTRND"):
        BD(n)
    B("$DIMALT")
    BS("$DIMALTD")
    for n in ("$DIMTOFL", "$DIMSAH", "$DIMTIX", "$DIMSOXD"):
        B(n)
    for n in ("$DIMCLRD", "$DIMCLRE", "$DIMCLRT"):
        w.cmc(int(hv(n)))
    for n in ("$DIMADEC", "$DIMDEC", "$DIMTDEC", "$DIMALTU", "$DIMALTTD", "$DIMAUNIT", "$DIMFRAC", "$DIMLUNIT",
              "$DIMDSEP", "$DIMTMOVE", "$DIMJUST"):
        BS(n)
    B("$DIMSD1")
    B("$DIMSD2")
    for n in ("$DIMTOLJ", "$DIMTZIN", "$DIMALTZ", "$DIMALTTZ"):
        BS(n)
    B("$DIMUPT")
    BS("$DIMATFIT")
    H(5, "DIMTXSTY")
    for _ in range(4):  # DIMLDRBLK, DIMBLK, DIMBLK1, DIMBLK2: default arrowheads
        w.h(5, 0)
    BS("$DIMLWD")
    BS("$DIMLWE")
    for key in ("BLOCK_CONTROL", "LAYER_CONTROL", "STYLE_CONTROL", "LTYPE_CONTROL", "VIEW_CONTROL",
                "UCS_CONTROL", "VPORT_CONTROL", "APPID_CONTROL", "DIMSTYLE_CONTROL", "VP_ENT_HDR_CONTROL"):
        H(3, key)
    H(5, "DICT_GROUP")
    H(5, "DICT_MLINESTYLE")
    H(3, "DICT_NAMED")
    BS("$TSTACKALIGN")
    BS("$TSTACKSIZE")
    w.tv(str(hv("$HYPERLINKBASE") or ""))
    w.tv(str(hv("$STYLESHEET") or ""))
    H(5, "DICT_LAYOUTS")
    H(5, "DICT_PLOTSETTINGS")
    H(5, "DICT_PLOTSTYLES")
    flags = lineweight_index(int(hv("$CELWEIGHT"))) & 0x1F
    flags |= (int(hv("$ENDCAPS")) & 3) << 5
    flags |= (int(hv("$JOINSTYLE")) & 3) << 7
    if not hv("$LWDISPLAY"):
        flags |= 0x200
    if not hv("$XEDIT"):
        flags |= 0x400
    if hv("$EXTNAMES"):
        flags |= 0x800
    if hv("$PSTYLEMODE"):
        flags |= 0x2000
    if hv("$OLESTARTUP"):
        flags |= 0x4000
    w.bl(flags)
    BS("$INSUNITS")
    w.bs(0)  # CEPSNTYPE: by layer (so no CPSNID handle)
    w.tv(_guid(hv("$FINGERPRINTGUID")))
    w.tv(_guid(hv("$VERSIONGUID")))
    H(5, "PAPER_SPACE")
    H(5, "MODEL_SPACE")
    H(5, "LTYPE_BYLAYER")
    H(5, "LTYPE_BYBLOCK")
    H(5, "LTYPE_CONTINUOUS")
    for _ in range(4):  # §9 "R14+: BS unknown short (type 5/6 only)"
        w.bs(0)
    return w.getvalue()


def _guid(v: Any) -> str:
    s = str(v or "")
    if len(s) == 38 and s.startswith("{") and s.endswith("}"):
        return s
    return "{" + str(uuid.uuid4()).upper() + "}"


def _fin(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _pt(v: Any, n: int) -> tuple[float, ...]:
    try:
        seq = list(v)
    except TypeError:
        seq = []
    seq = [_fin(c) for c in seq[:n]]
    while len(seq) < n:
        seq.append(0.0)
    return tuple(seq)


def header_getter(doc, overrides: dict[str, Any]) -> Callable[[str], Any]:
    """``hv(name)``: ``overrides`` first (computed extents …), then the ezdxf header, then R2000 defaults."""
    hdr = doc.header

    def hv(name: str) -> Any:
        if name in overrides:
            return overrides[name]
        try:
            if name in hdr:
                return hdr.get(name)
        except Exception:  # a malformed value in the source DXF
            pass
        return _DEFAULTS.get(name, 0)

    return hv
