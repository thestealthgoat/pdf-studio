"""AcDb:Header — drawing header variables (ODA spec chapter 9).

The section is one sequential bit stream; there is no index, so every variable before the ones we
want must be stepped over with the right bit code. We read the whole list in spec order and keep the
values useful for a DXF document (units, extents, limits, scales, table control handles, the model/paper
space block records). For R2007+ the strings and handles of the header live in separate streams
(chapter 6/7: "the data, strings and handles are separated in header and objects sections"); the caller
passes three readers (``data``, ``strings``, ``handles``) which are the same reader for R2004 and older.
"""

from __future__ import annotations

from typing import Any

from pdfcore.ops.plans.dwg.bits import R14, R2000, R2004, R2007, R2010, R2013, BitReader


def read_header(r: BitReader, s: BitReader, hh: BitReader, ver: int, maint: int = 0) -> dict[str, Any]:
    """Parse the header variables (spec §9, in order). Returns a dict keyed by DXF-style names
    (``$INSUNITS`` …) plus ``_ctrl`` (table control handles) and ``_blocks`` (model/paper space)."""
    v: dict[str, Any] = {}
    H = lambda: hh.h()[1]  # noqa: E731 - absolute handle (header handles are code 3/5 with absolute value)
    T = s.tu if ver >= R2007 else r.tv

    if ver >= R2013:
        r.bll()  # REQUIREDVERSIONS
    r.bd(); r.bd(); r.bd(); r.bd()
    T(); T(); T(); T()
    r.bl(); r.bl()
    if ver <= R14:
        r.bs()
    if ver < R2004:
        H()  # current viewport entity header
    v["$DIMASO"] = r.b()
    v["$DIMSHO"] = r.b()
    if ver <= R14:
        r.b()  # DIMSAV
    for name in ("$PLINEGEN", "$ORTHOMODE", "$REGENMODE", "$FILLMODE", "$QTEXTMODE", "$PSLTSCALE", "$LIMCHECK"):
        v[name] = r.b()
    if ver <= R14:
        r.b()  # BLIPMODE
    if ver >= R2004:
        r.b()
    for name in ("$USRTIMER", "$SKPOLY", "$ANGDIR", "$SPLFRAME"):
        v[name] = r.b()
    if ver <= R14:
        r.b(); r.b()  # ATTREQ ATTDIA
    v["$MIRRTEXT"] = r.b()
    v["$WORLDVIEW"] = r.b()
    if ver <= R14:
        r.b()  # WIREFRAME
    v["$TILEMODE"] = r.b()
    v["$PLIMCHECK"] = r.b()
    v["$VISRETAIN"] = r.b()
    if ver <= R14:
        r.b()  # DELOBJ
    v["$DISPSILH"] = r.b()
    r.b()  # PELLIPSE
    v["$PROXYGRAPHICS"] = r.bs()
    if ver <= R14:
        r.bs()  # DRAGMODE
    v["$TREEDEPTH"] = r.bs()
    v["$LUNITS"] = r.bs()
    v["$LUPREC"] = r.bs()
    v["$AUNITS"] = r.bs()
    v["$AUPREC"] = r.bs()
    if ver <= R14:
        r.bs()  # OSMODE
    v["$ATTMODE"] = r.bs()
    if ver <= R14:
        r.bs()  # COORDS
    v["$PDMODE"] = r.bs()
    if ver <= R14:
        r.bs()  # PICKSTYLE
    if ver >= R2004:
        r.bl(); r.bl(); r.bl()
    for name in ("$USERI1", "$USERI2", "$USERI3", "$USERI4", "$USERI5", "$SPLINESEGS", "$SURFU", "$SURFV",
                 "$SURFTYPE", "$SURFTAB1", "$SURFTAB2", "$SPLINETYPE", "$SHADEDGE", "$SHADEDIF", "$UNITMODE",
                 "$MAXACTVP", "$ISOLINES", "$CMLJUST", "$TEXTQLTY"):
        v[name] = r.bs()
    for name in ("$LTSCALE", "$TEXTSIZE", "$TRACEWID", "$SKETCHINC", "$FILLETRAD", "$THICKNESS", "$ANGBASE",
                 "$PDSIZE", "$PLINEWID", "$USERR1", "$USERR2", "$USERR3", "$USERR4", "$USERR5", "$CHAMFERA",
                 "$CHAMFERB", "$CHAMFERC", "$CHAMFERD", "$FACETRES", "$CMLSCALE", "$CELTSCALE"):
        v[name] = r.bd()
    if ver <= R2004:
        T()  # MENUNAME
    r.bl(); r.bl(); r.bl(); r.bl()  # TDCREATE, TDUPDATE
    if ver >= R2004:
        r.bl(); r.bl(); r.bl()
    r.bl(); r.bl(); r.bl(); r.bl()  # TDINDWG, TDUSRTIMER
    v["$CECOLOR"] = r.cmc()
    # HANDSEED lives in the data stream even for R2007+ (§9).
    v["$HANDSEED"] = r.h()[1]
    v["_clayer"] = H()
    v["_textstyle"] = H()
    v["_celtype"] = H()
    if ver >= R2007:
        H()  # CMATERIAL
    v["_dimstyle"] = H()
    H()  # CMLSTYLE
    if ver >= R2000:
        v["$PSVPSCALE"] = r.bd()
    v["$PINSBASE"] = r.bd3()
    v["$PEXTMIN"] = r.bd3()
    v["$PEXTMAX"] = r.bd3()
    v["$PLIMMIN"] = r.rd2()
    v["$PLIMMAX"] = r.rd2()
    v["$PELEVATION"] = r.bd()
    v["$PUCSORG"] = r.bd3()
    v["$PUCSXDIR"] = r.bd3()
    v["$PUCSYDIR"] = r.bd3()
    H()  # PUCSNAME
    if ver >= R2000:
        H(); r.bs(); H()
        for _ in range(6):
            r.bd3()
    v["$INSBASE"] = r.bd3()
    v["$EXTMIN"] = r.bd3()
    v["$EXTMAX"] = r.bd3()
    v["$LIMMIN"] = r.rd2()
    v["$LIMMAX"] = r.rd2()
    v["$ELEVATION"] = r.bd()
    v["$UCSORG"] = r.bd3()
    v["$UCSXDIR"] = r.bd3()
    v["$UCSYDIR"] = r.bd3()
    H()  # UCSNAME
    if ver >= R2000:
        H(); r.bs(); H()
        for _ in range(6):
            r.bd3()
    v["$DIMPOST"] = T()
    v["$DIMAPOST"] = T()
    if ver <= R14:
        for _ in range(11):
            r.b()
        r.rc(); r.rc(); r.b(); r.b(); r.rc(); r.rc(); r.rc(); r.b(); r.rc(); r.rc(); r.rc(); r.rc()
        for _ in range(6):
            r.bs()
        H()
    for name in ("$DIMSCALE", "$DIMASZ", "$DIMEXO", "$DIMDLI", "$DIMEXE", "$DIMRND", "$DIMDLE", "$DIMTP", "$DIMTM"):
        v[name] = r.bd()
    if ver >= R2007:
        r.bd(); r.bd(); r.bs(); r.cmc()
    if ver >= R2000:
        for _ in range(6):
            r.b()
        r.bs(); r.bs(); r.bs()
    if ver >= R2007:
        r.bs()
    for name in ("$DIMTXT", "$DIMCEN", "$DIMTSZ", "$DIMALTF", "$DIMLFAC", "$DIMTVP", "$DIMTFAC", "$DIMGAP"):
        v[name] = r.bd()
    if ver <= R14:
        for _ in range(5):
            r.t8()
    if ver >= R2000:
        r.bd(); r.b(); r.bs(); r.b(); r.b(); r.b(); r.b()
    r.cmc(); r.cmc(); r.cmc()
    if ver >= R2000:
        for _ in range(11):
            r.bs()
        r.b(); r.b()
        r.bs(); r.bs(); r.bs(); r.bs()
        r.b()
        r.bs()
    if ver >= R2007:
        r.b()
    if ver >= R2010:
        r.b(); r.bd(); T(); r.bd(); T()
    if ver >= R2000:
        H(); H(); H(); H(); H()
    if ver >= R2007:
        H(); H(); H()
    if ver >= R2000:
        r.bs(); r.bs()
    ctrl = {}
    for name in ("BLOCK", "LAYER", "STYLE", "LTYPE", "VIEW", "UCS", "VPORT", "APPID", "DIMSTYLE"):
        ctrl[name] = H()
    if ver <= R2000:
        ctrl["VP_ENT_HDR"] = H()
    ctrl["DICT_GROUP"] = H()
    ctrl["DICT_MLINESTYLE"] = H()
    ctrl["DICT_NAMED"] = H()
    v["_ctrl"] = ctrl
    if ver >= R2000:
        r.bs(); r.bs()  # TSTACKALIGN, TSTACKSIZE
        v["$HYPERLINKBASE"] = T()
        v["$STYLESHEET"] = T()
        ctrl["DICT_LAYOUTS"] = H()
        ctrl["DICT_PLOTSETTINGS"] = H()
        ctrl["DICT_PLOTSTYLES"] = H()
    if ver >= R2004:
        ctrl["DICT_MATERIALS"] = H()
        ctrl["DICT_COLORS"] = H()
    if ver >= R2007:
        ctrl["DICT_VISUALSTYLE"] = H()
    if ver >= R2013:
        H()
    if ver >= R2000:
        flags = r.bl()
        v["$CELWEIGHT"] = flags & 0x1F
        v["$LWDISPLAY"] = 0 if flags & 0x200 else 1
        v["$INSUNITS"] = r.bs()
        cep = r.bs()
        if cep == 3:
            H()
        v["$FINGERPRINTGUID"] = T()
        v["$VERSIONGUID"] = T()
    if ver >= R2004:
        r.rc(); r.rc(); r.rc(); r.rc(); r.rc(); r.rc()
        r.bs(); r.bs(); r.rc(); r.rc()
        v["$PROJECTNAME"] = T()
    blocks = {"PAPER_SPACE": H(), "MODEL_SPACE": H()}
    v["_blocks"] = blocks
    v["_ltype_bylayer"] = H()
    v["_ltype_byblock"] = H()
    v["_ltype_continuous"] = H()
    return v
