"""Turn decoded DWG objects into an ``ezdxf`` document.

The DWG object model maps closely onto DXF (the spec gives the DXF group code for each field); the
differences handled here are: angles are radians in DWG and degrees in DXF, entity colour/linetype/
lineweight are stored as flags plus optional handles (§20.4.1/§20.4.2), block/layout ownership comes from
BLOCK_HEADER records (§20.4.52) and LAYOUT objects (§20.4.84), polyline vertices and attributes are
separate objects owned by their parent (§20.4.9, §20.4.16), and anonymous block names are stored without
their DXF number suffix (``*D`` → ``*D7``).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from pdfcore.ops.plans.dwg.bits import R2000, R2004, R2007, R2010, R2013, R2018
from pdfcore.ops.plans.dwg.objects import DwgObject

# DWG lineweight index (layer flags 0x3E0 >> 5, entity RC) -> DXF lineweight in 1/100 mm.
_LW = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]
DXFVERSION = {R2000: "R2000", R2004: "R2004", R2007: "R2007", R2010: "R2010", R2013: "R2013", R2018: "R2018"}
_DEG = 180.0 / math.pi
_SKIP_CHILDREN = {"VERTEX_2D", "VERTEX_3D", "VERTEX_MESH", "VERTEX_PFACE", "VERTEX_PFACE_FACE", "SEQEND",
                  "ATTRIB", "BLOCK", "ENDBLK"}
_DXF_NAME = {"LARGE_RADIAL_DIMENSION": "LARGE_RADIAL_DIMENSION", "POLYLINE_2D": "POLYLINE", "POLYLINE_3D": "POLYLINE", "POLYLINE_PFACE": "POLYLINE",
             "POLYLINE_MESH": "POLYLINE", "MINSERT": "INSERT"}


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(text: str, newline: str = " ") -> str:
    """DXF strings cannot contain line breaks or control characters: CR/LF become ``newline``
    (``\\P`` for MTEXT, ``^J`` for TOLERANCE), other control characters are dropped."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in text:
        text = text.replace("\n", newline)
    return _CTRL.sub("", text)


def lineweight(idx: int) -> int:
    if 0 <= idx < len(_LW):
        return _LW[idx]
    return {29: -1, 30: -2, 31: -3}.get(idx, -1)


class Builder:
    def __init__(self, objs: dict[int, DwgObject], hdr: dict, ver: int, warnings: list[str]) -> None:
        import ezdxf

        self.objs = objs
        self.hdr = hdr
        self.ver = ver
        self.warnings = warnings
        self.doc = ezdxf.new(DXFVERSION.get(ver, "R2000"), setup=False)
        self.counts: Counter[str] = Counter()
        self.skipped: Counter[str] = Counter()
        self.layer_names: dict[int, str] = {}
        self.ltype_names: dict[int, str] = {}
        self.style_names: dict[int, str] = {}
        self.dimstyle_names: dict[int, str] = {}
        self.block_names: dict[int, str] = {}   # BLOCK_HEADER handle -> ezdxf block name
        self.layouts: dict[int, Any] = {}       # BLOCK_HEADER handle -> ezdxf layout/block
        self.children: dict[int, list[int]] = {}  # owner handle -> owned entity handles (R2000 fallback)
        for h, o in objs.items():
            if o.is_entity and o.owner:
                self.children.setdefault(o.owner, []).append(h)

    # ------------------------------------------------------------------ helpers
    def _of(self, name: str) -> list[DwgObject]:
        return [o for o in self.objs.values() if o.name == name]

    def _ctrl_entries(self, key: str, name: str) -> list[DwgObject]:
        """Table records in control-object order (§20.4.51ff), falling back to a scan of the object map."""
        h = self.hdr.get("_ctrl", {}).get(key)
        ctrl = self.objs.get(h) if h else None
        out: list[DwgObject] = []
        seen = set()
        if ctrl is not None and ctrl.name.endswith("_CONTROL"):
            for eh in ctrl.data.get("entries", []) + ctrl.data.get("extra", []):
                o = self.objs.get(eh)
                if o is not None and o.name == name and eh not in seen:
                    out.append(o)
                    seen.add(eh)
        for o in self._of(name):
            if o.handle not in seen:
                out.append(o)
                seen.add(o.handle)
        return out

    def _unique(self, name: str, table) -> str:
        base = name or "UNNAMED"
        cand = base
        i = 1
        while cand in table:
            cand = f"{base}_{i}"
            i += 1
        return cand

    # ------------------------------------------------------------------ build steps
    def build(self):
        self._header()
        self._ltypes()
        self._styles()
        self._layers()
        self._dimstyles()
        self._blocks_and_layouts()
        self._fill_all()
        self._extents()
        return self.doc

    def _extents(self) -> None:
        """ezdxf rewrites $EXTMIN/$LIMMIN… from the layout entities on save, so store them there too."""
        h = self.hdr
        try:
            msp = self.doc.modelspace()
            if "$EXTMIN" in h and all(map(math.isfinite, h["$EXTMIN"] + h["$EXTMAX"])):
                msp.dxf.extmin, msp.dxf.extmax = h["$EXTMIN"], h["$EXTMAX"]
            if "$LIMMIN" in h:
                msp.dxf.limmin, msp.dxf.limmax = h["$LIMMIN"], h["$LIMMAX"]
            ps = self.layouts.get(self.ps_h)
            if ps is not None and "$PEXTMIN" in h and all(map(math.isfinite, h["$PEXTMIN"] + h["$PEXTMAX"])):
                ps.dxf.extmin, ps.dxf.extmax = h["$PEXTMIN"], h["$PEXTMAX"]
        except Exception as exc:
            self.warnings.append(f"extents: {exc}")

    def _header(self) -> None:
        h = self.hdr
        dh = self.doc.header
        for key in ("$INSUNITS", "$LTSCALE", "$PDMODE", "$PDSIZE", "$TEXTSIZE", "$DIMSCALE", "$LUNITS", "$LUPREC",
                    "$AUNITS", "$AUPREC", "$CELTSCALE", "$PSLTSCALE", "$TILEMODE", "$FILLMODE", "$MIRRTEXT",
                    "$ANGBASE", "$ANGDIR", "$LWDISPLAY", "$PLINEWID", "$PSVPSCALE"):
            if key in h:
                try:
                    val = h[key]
                    if key == "$ANGBASE":
                        val = val * _DEG
                    dh[key] = val
                except Exception:  # value out of the range ezdxf accepts
                    pass
        for key in ("$EXTMIN", "$EXTMAX", "$INSBASE", "$PEXTMIN", "$PEXTMAX", "$PINSBASE"):
            if key in h and all(map(math.isfinite, h[key])):
                dh[key] = h[key]
        for key in ("$LIMMIN", "$LIMMAX", "$PLIMMIN", "$PLIMMAX"):
            if key in h and all(map(math.isfinite, h[key])):
                dh[key] = h[key]

    def _ltypes(self) -> None:
        tbl = self.doc.linetypes
        for o in self._ctrl_entries("LTYPE", "LTYPE"):
            name = o.data.get("name", "")
            if not name:
                continue
            self.ltype_names[o.handle] = name
            if name.upper() in ("BYLAYER", "BYBLOCK", "CONTINUOUS") or name in tbl:
                continue
            dashes = [d[0] for d in o.data.get("dashes", [])]
            total = o.data.get("pattern_length", 0.0) or sum(abs(x) for x in dashes)
            try:
                if dashes and total > 0:
                    tbl.add(name, [total] + dashes, description=o.data.get("description", ""))
                else:
                    tbl.add(name, [0.0], description=o.data.get("description", ""))
            except Exception as exc:
                self.warnings.append(f"linetype {name!r}: {exc}")

    def _styles(self) -> None:
        tbl = self.doc.styles
        for o in self._ctrl_entries("STYLE", "STYLE"):
            d = o.data
            name = d.get("name", "")
            if not name and not d.get("is_shape"):
                continue
            if not name:  # shape-file entries have no name
                continue
            self.style_names[o.handle] = name
            font = d.get("font", "") or "txt"
            attrs = {"height": d.get("fixed_height", 0.0), "width": d.get("width", 1.0) or 1.0,
                     "oblique": d.get("oblique", 0.0) * _DEG, "last_height": d.get("last_height", 2.5) or 2.5,
                     "font": font}
            if d.get("bigfont"):
                attrs["bigfont"] = d["bigfont"]
            try:
                if name in tbl:
                    st = tbl.get(name)
                    st.dxf.font = font
                    for k, val in attrs.items():
                        st.dxf.set(k, val)
                else:
                    tbl.add(name, font=attrs.pop("font"), dxfattribs=attrs)
            except Exception as exc:
                self.warnings.append(f"text style {name!r}: {exc}")

    def _layers(self) -> None:
        tbl = self.doc.layers
        for o in self._ctrl_entries("LAYER", "LAYER"):
            d = o.data
            name = d.get("name", "")
            if not name:
                continue
            self.layer_names[o.handle] = name
            col = d.get("color", {})
            aci = col.get("aci", 7)
            if not 1 <= abs(aci) <= 255:
                aci = 7
            try:
                lay = tbl.get(name) if name in tbl else tbl.add(name)
                lay.dxf.color = abs(aci)
                if "rgb" in col:
                    lay.rgb = ((col["rgb"] >> 16) & 255, (col["rgb"] >> 8) & 255, col["rgb"] & 255)
                lt = self.ltype_names.get(d.get("ltype", 0))
                if lt and lt in self.doc.linetypes:
                    lay.dxf.linetype = lt
                lay.dxf.lineweight = lineweight(d.get("lineweight_idx", 31))
                if d.get("off") or aci < 0:
                    lay.off()
                if d.get("frozen"):
                    lay.freeze()
                if d.get("locked"):
                    lay.lock()
                if not d.get("plot", True):
                    lay.dxf.plot = 0
            except Exception as exc:
                self.warnings.append(f"layer {name!r}: {exc}")

    def _dimstyles(self) -> None:
        tbl = self.doc.dimstyles
        for o in self._ctrl_entries("DIMSTYLE", "DIMSTYLE"):
            name = o.data.get("name", "")
            if not name:
                continue
            self.dimstyle_names[o.handle] = name
            if name not in tbl:
                try:
                    tbl.add(name)
                except Exception as exc:
                    self.warnings.append(f"dimstyle {name!r}: {exc}")

    def _blocks_and_layouts(self) -> None:
        doc = self.doc
        hb = self.hdr.get("_blocks", {})
        ms_h = hb.get("MODEL_SPACE")
        ps_h = hb.get("PAPER_SPACE")
        headers = self._ctrl_entries("BLOCK", "BLOCK_HEADER")
        for o in headers:
            n = o.data.get("name", "").upper()
            if ms_h is None and n == "*MODEL_SPACE":
                ms_h = o.handle
            if ps_h is None and n == "*PAPER_SPACE":
                ps_h = o.handle
        self.ms_h, self.ps_h = ms_h, ps_h
        self.layouts[ms_h] = doc.modelspace()
        self.block_names[ms_h] = "*Model_Space"
        # paper-space layouts, in tab order (§20.4.84)
        layout_objs = [o for o in self._of("LAYOUT") if o.data.get("name")]
        # the active paper space (header *PAPER_SPACE) takes over ezdxf's default "Layout1"; then the rest
        layout_objs.sort(key=lambda o: (o.data.get("block_record") != ps_h, o.data.get("tab_order", 0)))
        used = {"Model"}
        tmp = "__dwg_active_layout__"
        doc.layouts.rename("Layout1", tmp)
        for lo in layout_objs:
            br = lo.data.get("block_record")
            name = lo.data["name"]
            if br == ms_h or name.upper() == "MODEL" or br not in self.objs or br in self.layouts:
                continue
            name = self._unique(name, used)
            used.add(name)
            try:
                if br == ps_h:
                    doc.layouts.rename(tmp, name)
                    layout = doc.layouts.get(name)
                else:
                    layout = doc.layouts.new(name)
                layout.dxf.taborder = max(lo.data.get("tab_order", 1), 1)
                self._page_setup(layout, lo)
                self.layouts[br] = layout
                self.block_names[br] = layout.block_record_name if hasattr(layout, "block_record_name") else name
            except Exception as exc:
                self.warnings.append(f"layout {name!r}: {exc}")
        if tmp in doc.layouts:
            doc.layouts.rename(tmp, self._unique("Layout1", used))
            if ps_h in self.objs and ps_h not in self.layouts:
                self.layouts[ps_h] = doc.layouts.get(self._unique("Layout1", used))
        # ordinary blocks
        counter = 0
        for o in headers:
            h = o.handle
            if h in self.layouts:
                continue
            d = o.data
            raw = d.get("name", "")
            if raw.upper().startswith("*PAPER_SPACE") or raw.upper() == "*MODEL_SPACE":
                continue  # layout block without a LAYOUT object: ignore
            counter += 1
            name = raw
            if raw.startswith("*") and len(raw) >= 2 and raw[1:].isalpha():
                name = f"{raw}{counter}"
            if not name:
                name = f"*U{counter}"
            name = self._unique(name, doc.blocks)
            flags = (1 if d.get("anonymous") else 0) | (2 if d.get("has_attdefs") else 0) | \
                    (4 if d.get("is_xref") else 0) | (8 if d.get("xref_overlaid") else 0) | \
                    (16 if d.get("xdep") else 0)
            try:
                blk = doc.blocks.new(name, base_point=d.get("base", (0, 0, 0)), dxfattribs={"flags": flags})
                if d.get("xref_path"):
                    blk.block.dxf.xref_path = d["xref_path"]
                self.layouts[h] = blk
                self.block_names[h] = name
            except Exception as exc:
                self.warnings.append(f"block {raw!r}: {exc}")

    def _page_setup(self, layout, lo: DwgObject) -> None:
        d = lo.data
        try:
            w, hgt = d.get("paper_size_mm", (0, 0))
            if w > 0 and hgt > 0:
                layout.dxf.paper_width = w
                layout.dxf.paper_height = hgt
            lm = d.get("margins")
            if lm:
                layout.dxf.left_margin, layout.dxf.bottom_margin, layout.dxf.right_margin, layout.dxf.top_margin = lm
            if d.get("paper_name"):
                layout.dxf.paper_size = d["paper_name"]
            layout.dxf.plot_rotation = d.get("plot_rotation", 0) & 3
            layout.dxf.limmin = d.get("limmin", (0, 0))
            layout.dxf.limmax = d.get("limmax", (420, 297))
            layout.dxf.extmin = d.get("extmin", (0, 0, 0))
            layout.dxf.extmax = d.get("extmax", (0, 0, 0))
        except Exception as exc:
            self.warnings.append(f"page setup of layout {d.get('name')!r}: {exc}")

    # ------------------------------------------------------------------ entities
    def _owned(self, bh: int) -> list[int]:
        """Entity handles of a block record, in drawing order."""
        o = self.objs.get(bh)
        if o is None:
            return []
        d = o.data
        if d.get("entities"):
            return [h for h in d["entities"] if h in self.objs]
        # R13–R2000: walk the entity chain first → last (§20.4.2 previous/next links), validated by owner.
        first, last = d.get("first_entity", 0), d.get("last_entity", 0)
        expected = set(self._owner_children(bh))
        chain: list[int] = []
        h = first
        seen: set[int] = set()
        while h and h not in seen and h in self.objs:
            seen.add(h)
            chain.append(h)
            if h == last:
                break
            e = self.objs[h]
            h = e.next if not e.nolinks else h + 1
        if expected and set(chain) >= expected and (not chain or chain[-1] == last):
            return [x for x in chain if x in expected]
        return sorted(expected)

    def _owner_children(self, bh: int) -> list[int]:
        out = list(self.children.get(bh, []))
        if bh == self.ms_h:
            out += [h for h, o in self.objs.items() if o.is_entity and o.entmode == 2]
        elif bh == self.ps_h:
            out += [h for h, o in self.objs.items() if o.is_entity and o.entmode == 1]
        return [h for h in out if self.objs[h].name not in _SKIP_CHILDREN]

    def _fill_all(self) -> None:
        for bh, layout in self.layouts.items():
            if layout is None:
                continue
            for h in self._owned(bh):
                o = self.objs.get(h)
                if o is None or o.name in _SKIP_CHILDREN:
                    continue
                self._add(layout, o)

    def _attribs(self, o: DwgObject) -> dict:
        a: dict[str, Any] = {}
        a["layer"] = self.layer_names.get(o.layer, "0")
        col = o.color
        aci = col.get("aci", 256)
        if 0 <= aci <= 257:
            a["color"] = min(aci, 256)
        if "rgb" in col:
            a["true_color"] = col["rgb"]
        if o.ltype_flags == 1:
            a["linetype"] = "BYBLOCK"
        elif o.ltype_flags == 2:
            a["linetype"] = "CONTINUOUS"
        elif o.ltype_flags == 3:
            lt = self.ltype_names.get(o.ltype)
            if lt and lt in self.doc.linetypes:
                a["linetype"] = lt
        if o.ltscale and o.ltscale != 1.0 and math.isfinite(o.ltscale):
            a["ltscale"] = o.ltscale
        lw = lineweight(o.lineweight)
        if lw != -1:
            a["lineweight"] = lw
        if o.invisible & 1:
            a["invisible"] = 1
        return a

    def _add(self, layout, o: DwgObject) -> None:
        n = o.name.lower()
        fn = getattr(self, "e_" + ("x" + n if n[:1].isdigit() else n), None)
        if fn is None or o.error:
            self.skipped[o.name] += 1
            if o.error:
                self.warnings.append(f"{o.name} {o.handle:X}: {o.error}")
            return
        try:
            fn(layout, o, self._attribs(o))
            self.counts[_DXF_NAME.get(o.name, o.name if not o.name.startswith("DIMENSION_") else "DIMENSION")] += 1
        except Exception as exc:  # keep going; one bad entity must not lose the drawing
            self.skipped[o.name] += 1
            self.warnings.append(f"{o.name} {o.handle:X}: {type(exc).__name__}: {exc}")

    # one method per DWG entity type -------------------------------------
    def e_line(self, L, o, a):
        d = o.data
        a.update(thickness=d["thickness"], extrusion=d["extrusion"])
        L.add_line(d["start"], d["end"], dxfattribs=a)

    def e_point(self, L, o, a):
        d = o.data
        a.update(thickness=d["thickness"], extrusion=d["extrusion"], angle=d["x_angle"] * _DEG)
        L.add_point(d["point"], dxfattribs=a)

    def e_circle(self, L, o, a):
        d = o.data
        a.update(thickness=d["thickness"], extrusion=d["extrusion"])
        L.add_circle(d["center"], d["radius"], dxfattribs=a)

    def e_arc(self, L, o, a):
        d = o.data
        a.update(thickness=d["thickness"], extrusion=d["extrusion"])
        L.add_arc(d["center"], d["radius"], d["start_angle"] * _DEG, d["end_angle"] * _DEG, dxfattribs=a)

    def e_ellipse(self, L, o, a):
        d = o.data
        a["extrusion"] = d["extrusion"]
        ratio = min(max(d["ratio"], 1e-6), 1.0)
        L.add_ellipse(d["center"], d["major_axis"], ratio, d["start"], d["end"], dxfattribs=a)

    def _text_attribs(self, o, a):
        d = o.data
        ins = d["insert"]
        a.update(insert=(ins[0], ins[1], d["elevation"]), height=d["height"] or 1.0,
                 rotation=d["rotation"] * _DEG, oblique=d["oblique"] * _DEG, width=d["width"] or 1.0,
                 text_generation_flag=d["generation"], halign=d["halign"], valign=d["valign"],
                 thickness=d["thickness"], extrusion=d["extrusion"])
        if d.get("align") is not None:
            al = d["align"]
            a["align_point"] = (al[0], al[1], d["elevation"])
        st = self.style_names.get(d.get("style"))
        if st:
            a["style"] = st
        return a

    def e_text(self, L, o, a):
        L.add_text(clean_text(o.data["text"]), dxfattribs=self._text_attribs(o, a))

    def e_attdef(self, L, o, a):
        d = o.data
        a = self._text_attribs(o, a)
        a["prompt"] = d.get("prompt", "")
        a["flags"] = d.get("flags", 0)
        ins = a.pop("insert")
        a["prompt"] = clean_text(a["prompt"])
        L.add_attdef(clean_text(d.get("tag", "")) or "TAG", ins, clean_text(d["text"]), dxfattribs=a)

    def e_mtext(self, L, o, a):
        d = o.data
        a.update(insert=d["insert"], char_height=d["height"] or 1.0, width=d["rect_width"],
                 attachment_point=d["attachment"] if 1 <= d["attachment"] <= 9 else 1,
                 flow_direction=d["direction"] if d["direction"] in (1, 3, 5) else 1,
                 text_direction=d["x_axis"], extrusion=d["extrusion"])
        if d.get("linespacing"):
            a["line_spacing_factor"] = min(max(d["linespacing"], 0.25), 4.0)
            a["line_spacing_style"] = d.get("linespacing_style", 1) if d.get("linespacing_style") in (1, 2) else 1
        st = self.style_names.get(d.get("style"))
        if st:
            a["style"] = st
        L.add_mtext(clean_text(d["text"], "\\P"), dxfattribs=a)

    def _insert_common(self, o, a):
        d = o.data
        name = self.block_names.get(d["block"])
        if name is None:
            raise ValueError(f"INSERT references unknown block {d['block']:X}")
        sx, sy, sz = d["scale"]
        a.update(xscale=sx, yscale=sy, zscale=sz, rotation=d["rotation"] * _DEG, extrusion=d["extrusion"])
        return name, a

    def e_insert(self, L, o, a):
        name, a = self._insert_common(o, a)
        ins = L.add_blockref(name, o.data["insert"], dxfattribs=a)
        self._add_attribs(ins, o)

    def e_minsert(self, L, o, a):
        d = o.data
        name, a = self._insert_common(o, a)
        if max(d["cols"], 1) * max(d["rows"], 1) > 100_000:
            raise ValueError(f"MINSERT array {d['cols']}x{d['rows']} too large")
        a.update(column_count=max(d["cols"], 1), row_count=max(d["rows"], 1), column_spacing=d["col_spacing"],
                 row_spacing=d["row_spacing"])
        ins = L.add_blockref(name, d["insert"], dxfattribs=a)
        self._add_attribs(ins, o)

    def _add_attribs(self, ins, o):
        d = o.data
        if not d.get("has_attribs"):
            return
        handles = d.get("attribs") or sorted(h for h in self.children.get(o.handle, [])
                                             if self.objs[h].name == "ATTRIB")
        for h in handles:
            at = self.objs.get(h)
            if at is None or at.name != "ATTRIB":
                continue
            if at.error:
                self.skipped["ATTRIB"] += 1
                self.warnings.append(f"ATTRIB {at.handle:X}: {at.error}")
                continue
            try:
                aa = self._text_attribs(at, self._attribs(at))
                aa["flags"] = at.data.get("flags", 0)
                pos = aa.pop("insert")
                ins.add_attrib(clean_text(at.data.get("tag", "")) or "TAG", clean_text(at.data["text"]), pos,
                               dxfattribs=aa)
                self.counts["ATTRIB"] += 1
            except Exception as exc:
                self.skipped["ATTRIB"] += 1
                self.warnings.append(f"ATTRIB {at.handle:X}: {exc}")

    def _vertices(self, o, names):
        d = o.data
        hs = d.get("vertices") or sorted(h for h in self.children.get(o.handle, []))
        return [self.objs[h] for h in hs if h in self.objs and self.objs[h].name in names]

    def e_polyline_2d(self, L, o, a):
        d = o.data
        verts = self._vertices(o, {"VERTEX_2D"})
        a.update(flags=d["flags"] & ~(8 | 16 | 64), elevation=(0, 0, d["elevation"]), thickness=d["thickness"],
                 extrusion=d["extrusion"], default_start_width=d["start_width"], default_end_width=d["end_width"])
        if d["curve_type"] in (5, 6, 8):
            a["smooth_type"] = d["curve_type"]
        pts = [(v.data["point"][0], v.data["point"][1], v.data["start_width"], v.data["end_width"], v.data["bulge"])
               for v in verts]
        pl = L.add_polyline2d([], dxfattribs=a)
        if pts:
            pl.append_formatted_vertices(pts, format="xyseb")
            for vx, v in zip(pl.vertices, verts):
                vx.dxf.flags = v.data["flags"]
                if v.data.get("tangent"):
                    vx.dxf.tangent = v.data["tangent"] * _DEG

    def e_polyline_3d(self, L, o, a):
        d = o.data
        verts = self._vertices(o, {"VERTEX_3D"})
        closed = bool(d["closed_flags"] & 1)
        a["flags"] = 8 | (1 if closed else 0) | (4 if d["spline_flags"] & 3 else 0)
        pl = L.add_polyline3d([v.data["point"] for v in verts], close=closed, dxfattribs=a)
        for vx, v in zip(pl.vertices, verts):
            vx.dxf.flags = v.data["flags"] | 32

    def e_polyline_pface(self, L, o, a):
        """Polyface: location vertices (flags 192) then face records (flags 128, 1-based indices,
        negative = invisible edge), kept exactly as stored (§20.4.14/§20.4.15)."""
        verts = self._vertices(o, {"VERTEX_PFACE"})
        faces = self._vertices(o, {"VERTEX_PFACE_FACE"})
        pf = L.add_polyface(dxfattribs=a)
        for v in verts:
            pf.append_vertex(v.data["point"], dxfattribs={"flags": v.data["flags"] | 192})
        for f in faces:
            fa = {"flags": 128}
            for i, idx in enumerate(f.data["indices"]):
                if idx:
                    fa[f"vtx{i}"] = idx
            pf.append_vertex((0, 0, 0), dxfattribs=fa)
        pf.dxf.m_count = len(verts)
        pf.dxf.n_count = len(faces)

    def e_polyline_mesh(self, L, o, a):
        d = o.data
        verts = self._vertices(o, {"VERTEX_MESH"})
        m, n = max(d["m"], 2), max(d["n"], 2)
        if m * n > max(len(verts), 4):
            raise ValueError(f"mesh size {m}x{n} does not match its {len(verts)} vertices")
        a["flags"] = (d["flags"] | 16) & ~8
        pm = L.add_polymesh((m, n), dxfattribs=a)
        for i, v in enumerate(verts[:m * n]):
            pm.set_mesh_vertex((i // n, i % n), v.data["point"])

    def e_lwpolyline(self, L, o, a):
        d = o.data
        pts = d["points"]
        n = len(pts)
        bulges = d["bulges"] + [0.0] * (n - len(d["bulges"]))
        widths = d["widths"] + [(d["const_width"], d["const_width"])] * (n - len(d["widths"]))
        a.update(elevation=d["elevation"], thickness=d["thickness"], extrusion=d["extrusion"],
                 const_width=d["const_width"])
        flags = d["flags"]
        a["flags"] = (1 if flags & 512 else 0) | (128 if flags & 256 else 0)  # §20.4.85 flag bits 512/256
        rows = [(p[0], p[1], w[0], w[1], b) for p, w, b in zip(pts, widths, bulges)]
        L.add_lwpolyline(rows, format="xyseb", close=bool(flags & 512), dxfattribs=a)

    def e_solid(self, L, o, a):
        d = o.data
        a.update(thickness=d["thickness"], extrusion=d["extrusion"])
        L.add_solid(d["vtx"], dxfattribs=a)

    def e_trace(self, L, o, a):
        d = o.data
        a.update(thickness=d["thickness"], extrusion=d["extrusion"])
        L.add_trace(d["vtx"], dxfattribs=a)

    def e_x3dface(self, L, o, a):
        d = o.data
        a["invisible_edges"] = d["invisible_edges"] & 15
        L.add_3dface(d["vtx"], dxfattribs=a)

    def e_ray(self, L, o, a):
        L.add_ray(o.data["point"], o.data["vector"], dxfattribs=a)

    def e_xline(self, L, o, a):
        L.add_xline(o.data["point"], o.data["vector"], dxfattribs=a)

    def e_spline(self, L, o, a):
        d = o.data
        flags = 0
        if d.get("closed") or d.get("closed_flag13"):
            flags |= 1
        if d.get("periodic"):
            flags |= 2
        if d.get("rational") or d["weights"]:
            flags |= 4
        zs = {round(p[2], 12) for p in d["ctrl"] + d["fit"]}
        if len(zs) <= 1:
            flags |= 8  # planar (DXF-only flag, derived)
        a.update(degree=d["degree"], flags=flags)
        if "knot_tol" in d:
            a.update(knot_tolerance=d["knot_tol"], control_point_tolerance=d["ctrl_tol"])
        if "fit_tol" in d:
            a["fit_tolerance"] = d["fit_tol"]
        sp = L.add_spline(degree=d["degree"], dxfattribs=a)
        if d["ctrl"]:
            sp.control_points = d["ctrl"]
            sp.knots = d["knots"]
            if d["weights"]:
                sp.weights = d["weights"]
        if d["fit"]:
            sp.fit_points = d["fit"]
            st, en = d.get("start_tangent"), d.get("end_tangent")
            if st and any(st):
                sp.dxf.start_tangent = st
            if en and any(en):
                sp.dxf.end_tangent = en

    def _dimension(self, L, o, a):
        d = o.data
        blk = self.block_names.get(d["block"])  # None: no rendered block stored (e.g. inside dynamic blocks)
        tm = d["text_mid"]
        flags = d["dimtype"] | 32
        if not d["flags1"] & 1:
            flags |= 128
        if d.get("flags2", 0) & 1:
            flags |= 64
        if blk is not None:
            a["geometry"] = blk
        a.update(dimtype=flags, text=clean_text(d["text"], "\\P"), extrusion=d["extrusion"],
                 text_midpoint=(tm[0], tm[1], d["elevation"]), defpoint=d.get("pt10", (0, 0, 0)),
                 text_rotation=d["text_rotation"] * _DEG, horizontal_direction=d["horizontal_direction"] * _DEG,
                 dimstyle=self.dimstyle_names.get(d["dimstyle"], "Standard"))
        for key, attr in (("pt13", "defpoint2"), ("pt14", "defpoint3"), ("pt15", "defpoint4")):
            if key in d:
                a[attr] = d[key]
        if "pt16" in d:
            a["defpoint5"] = (d["pt16"][0], d["pt16"][1], d["elevation"])
        if "angle" in d:
            a["angle"] = d["angle"] * _DEG
        if "oblique" in d:
            a["oblique_angle"] = d["oblique"] * _DEG
        if "attachment" in d and 1 <= d["attachment"] <= 9:
            a["attachment_point"] = d["attachment"]
        if d.get("measurement") is not None and math.isfinite(d.get("measurement", 0.0)):
            a["actual_measurement"] = d["measurement"]
        if a["dimstyle"] not in self.doc.dimstyles:
            a["dimstyle"] = "Standard"
        L.new_entity("DIMENSION", a)

    e_dimension_ordinate = e_dimension_linear = e_dimension_aligned = _dimension
    e_dimension_ang3pt = e_dimension_ang2ln = e_dimension_radius = e_dimension_diameter = _dimension

    def e_arc_dimension(self, L, o, a):
        d = o.data
        blk = self.block_names.get(d["block"])
        tm = d["text_mid"]
        a.update(dimtype=5 | 32, text=clean_text(d["text"], "\\P"), extrusion=d["extrusion"], text_midpoint=(tm[0], tm[1], d["elevation"]),
                 defpoint=d["pt10"], defpoint2=d["pt13"], defpoint3=d["pt14"], defpoint4=d["pt15"],
                 start_angle=d["arc_start"], end_angle=d["arc_end"], is_partial=d["is_partial"],
                 has_leader=d["has_leader"], leader_point1=d["leader1"], leader_point2=d["leader2"],
                 dimstyle=self.dimstyle_names.get(d["dimstyle"], "Standard"))
        if blk is not None:
            a["geometry"] = blk
        if a["dimstyle"] not in self.doc.dimstyles:
            a["dimstyle"] = "Standard"
        if 1 <= d.get("attachment", 0) <= 9:
            a["attachment_point"] = d["attachment"]
        if math.isfinite(d.get("measurement", float("nan"))):
            a["actual_measurement"] = d["measurement"]
        L.new_entity("ARC_DIMENSION", a)

    def e_large_radial_dimension(self, L, o, a):
        d = o.data
        blk = self.block_names.get(d["block"])
        tm = d["text_mid"]
        a.update(dimtype=9 | 32, text=clean_text(d["text"], "\\P"), extrusion=d["extrusion"], text_midpoint=(tm[0], tm[1], d["elevation"]),
                 defpoint=d["pt10"], chord_point=d["pt13"], override_center=d["pt14"], jog_point=d["pt15"],
                 dimstyle=self.dimstyle_names.get(d["dimstyle"], "Standard"))
        if blk is not None:
            a["geometry"] = blk
        if a["dimstyle"] not in self.doc.dimstyles:
            a["dimstyle"] = "Standard"
        L.new_entity("LARGE_RADIAL_DIMENSION", a)

    def e_tolerance(self, L, o, a):
        d = o.data
        ds = self.dimstyle_names.get(d["dimstyle"], "Standard")
        a.update(insert=d["insert"], x_axis_vector=d["x_dir"], extrusion=d["extrusion"], content=clean_text(d["text"], "^J"),
                 dimstyle=ds if ds in self.doc.dimstyles else "Standard")
        L.new_entity("TOLERANCE", a)

    def e_wipeout(self, L, o, a):
        d = o.data
        a.update(insert=d["insert"], u_pixel=d["u"], v_pixel=d["v"], image_size=d["size"],
                 flags=d["display"], clipping=d["clipping"], brightness=d["brightness"], contrast=d["contrast"],
                 fade=d["fade"], clipping_boundary_type=d["clip_type"] if d["clip_type"] in (1, 2) else 2)
        w = L.new_entity("WIPEOUT", a)
        if d["clip"]:
            w.set_boundary_path(d["clip"])

    def e_leader(self, L, o, a):
        d = o.data
        pts = d["points"]
        if len(pts) < 2:
            raise ValueError("LEADER with fewer than 2 points")
        ds = self.dimstyle_names.get(d["dimstyle"], "Standard")
        if ds not in self.doc.dimstyles:
            ds = "Standard"
        a.update(has_arrowhead=1 if d["arrowhead"] else 0, path_type=d["path_type"] & 1,
                 annotation_type=d["annot_type"] if 0 <= d["annot_type"] <= 3 else 3, normal_vector=d["extrusion"])
        L.add_leader(pts, dimstyle=ds, dxfattribs=a)

    def e_hatch(self, L, o, a):
        d = o.data
        a.update(elevation=(0, 0, d["elevation"]), extrusion=d["extrusion"], hatch_style=d["style"] if d["style"] in (0, 1, 2) else 0,
                 associative=0)
        h = L.add_hatch(color=a.pop("color", 256), dxfattribs=a)
        for p in d["paths"]:
            fl = p["flags"] & 0x1F
            if "polyline" in p:
                pts, closed = p["polyline"]
                h.paths.add_polyline_path(pts, is_closed=bool(closed), flags=fl)
                continue
            ep = h.paths.add_edge_path(flags=fl)
            for e in p["edges"]:
                kind = e[0]
                if kind == "line":
                    ep.add_line(e[1], e[2])
                elif kind == "arc":
                    _, c, r, sa, ea, ccw = e
                    ep.add_arc(c, r, sa * _DEG, ea * _DEG, ccw=bool(ccw))
                elif kind == "ellipse":
                    _, c, maj, ratio, sa, ea, ccw = e
                    ep.add_ellipse(c, maj, ratio, sa * _DEG, ea * _DEG, ccw=bool(ccw))
                elif kind == "spline":
                    _, deg, rat, per, knots, cps, ws, fit = e
                    ep.add_spline(fit_points=fit or None, control_points=cps, knot_values=knots,
                                  weights=ws or None, degree=deg, periodic=per)
        if d["solid"]:
            h.dxf.solid_fill = 1
            h.dxf.pattern_name = "SOLID"
        else:
            h.dxf.solid_fill = 0
            h.dxf.pattern_name = d["pattern_name"] or "USER"
            h.dxf.pattern_type = d["pattern_type"] if d["pattern_type"] in (0, 1, 2) else 1
            h.dxf.pattern_angle = d.get("angle", 0.0) * _DEG
            h.dxf.pattern_scale = d.get("scale", 1.0) or 1.0
            h.dxf.pattern_double = d.get("double", 0)
            lines = [[ang * _DEG, base, off, dashes] for ang, base, off, dashes in d["pattern_lines"]]
            if lines:
                h.set_pattern_definition(lines)

    def e_viewport(self, L, o, a):
        d = o.data
        if not hasattr(L, "add_viewport"):
            raise ValueError("VIEWPORT outside paper space")
        a.update(view_target_point=d.get("view_target", (0, 0, 0)), view_direction_vector=d.get("view_direction", (0, 0, 1)),
                 view_twist_angle=d.get("view_twist", 0.0) * _DEG)
        if d.get("status"):
            a["flags"] = d["status"]
        vc = d.get("view_center", (0, 0))
        L.add_viewport(d["center"], (d["width"], d["height"]), vc, d.get("view_height", d["height"]) or 1.0,
                       dxfattribs=a)
