"""Optimize: downsample and recompress images, re-subset fonts, deduplicate, strip extras, linearize.

Pipeline (each step optional)::

    dedupe identical streams → images (downsample to target dpi, JPEG for photos, Flate for line art)
    → font re-subsetting (fully embedded TrueType only) → strip metadata / thumbnails / private data
    → drop unreferenced resources → save with object streams + recompressed Flate (+ linearize)

Effective image dpi comes from walking the content streams with the current transformation matrix
(forms included), so an image's dpi is measured at the size it is actually drawn. Images we cannot
place with certainty (patterns, annotation appearances, Type 3 glyphs) are left at full resolution.

Size budget: ``optimize(src, target_mb=2)`` tries progressively stronger settings and keeps the
first result under budget, or the smallest one (the gate then reports FAIL with the size).
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import math
import re
import shutil
import tempfile
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pikepdf
from pikepdf import Name

from pdfcore.gate import GateOptions
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path
from pdfcore.result import OpResult

__all__ = ["Settings", "optimize", "PRESETS"]

Matrix = tuple[float, float, float, float, float, float]
_ID: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")


@dataclass
class Settings:
    dpi: float | None = 150  # target resolution; None = never downsample
    above: float = 1.5  # downsample only images above dpi × above (Acrobat-style threshold)
    quality: int = 75  # JPEG quality for photographic images
    lossless: bool = False  # never convert to JPEG (downsampled images stay Flate)
    recompress_jpeg: bool = False  # re-encode existing JPEGs even when not downsampling
    subset_fonts: bool = True
    dedupe: bool = True
    strip_metadata: bool = False  # XMP + document info (title kept)
    strip_thumbnails: bool = True
    strip_private: bool = False  # /PieceInfo application data, object-level XMP
    strip_attachments: bool = False
    linearize: bool = False
    object_streams: bool = True


PRESETS: dict[str, Settings] = {
    "screen": Settings(dpi=150, quality=70),
    "ebook": Settings(dpi=150, quality=80),
    "print": Settings(dpi=300, quality=90),
    "lossless": Settings(dpi=None, lossless=True),
}


# ------------------------------------------------------------------ matrices
def _mul(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _matrix(obj) -> Matrix:
    try:
        m = [float(v) for v in obj]
        return (m[0], m[1], m[2], m[3], m[4], m[5]) if len(m) == 6 else _ID
    except Exception:
        return _ID


def _og(obj) -> tuple[int, int] | None:
    try:
        og = obj.objgen
        return og if og != (0, 0) else None
    except Exception:
        return None


# ------------------------------------------------------------------ content scan
@dataclass
class _Scan:
    placements: dict[tuple[int, int], list[tuple[float, float]]] = field(default_factory=lambda: defaultdict(list))
    unplaced_images: set[tuple[int, int]] = field(default_factory=set)
    font_codes: dict[tuple[int, int], bytearray] = field(default_factory=lambda: defaultdict(bytearray))
    unsafe_fonts: set[tuple[int, int]] = field(default_factory=set)
    scanned: set[tuple[int, int]] = field(default_factory=set)


def _scan(pdf: pikepdf.Pdf) -> _Scan:
    s = _Scan()

    def fonts_of(res) -> Iterator[Any]:
        try:
            fd = res.get("/Font") if res is not None else None
            if isinstance(fd, pikepdf.Dictionary):
                yield from fd.values()
        except Exception:
            return

    def mark_unsafe(res) -> None:
        for f in fonts_of(res):
            og = _og(f)
            if og:
                s.unsafe_fonts.add(og)

    def walk(obj, res, ctm: Matrix | None, depth: int, stack: tuple) -> None:
        og = _og(obj)
        if og:
            s.scanned.add(og)
        if depth > 12 or (og and og in stack):
            return
        stack = stack + ((og,) if og else ())
        try:
            ops = pikepdf.parse_content_stream(obj)
        except Exception:
            mark_unsafe(res)
            return
        gs: list[tuple[Matrix | None, Any]] = []
        cur_font = None
        for operands, op in ops:
            o = str(op)
            if o == "q":
                gs.append((ctm, cur_font))
            elif o == "Q":
                if gs:
                    ctm, cur_font = gs.pop()
            elif o == "cm" and ctm is not None and len(operands) == 6:
                ctm = _mul(_matrix(operands), ctm)
            elif o == "Tf" and operands:
                try:
                    cur_font = res.Font[str(operands[0])]
                except Exception:
                    cur_font = None
            elif o in ("Tj", "'", '"', "TJ") and operands:
                fog = _og(cur_font) if cur_font is not None else None
                if fog is None:
                    continue
                arg = operands[-1]
                if isinstance(arg, pikepdf.String):
                    s.font_codes[fog] += bytes(arg)
                elif isinstance(arg, pikepdf.Array):
                    for a in arg:
                        if isinstance(a, pikepdf.String):
                            s.font_codes[fog] += bytes(a)
            elif o == "Do" and operands:
                try:
                    x = res.XObject[str(operands[0])]
                except Exception:
                    continue
                st = x.get("/Subtype")
                xog = _og(x)
                if st == "/Image" and xog:
                    if ctm is None:
                        s.unplaced_images.add(xog)
                    else:
                        a, b, c, d, _, _ = ctm
                        s.placements[xog].append((math.hypot(a, b), math.hypot(c, d)))
                elif st == "/Form":
                    m = _matrix(x.get("/Matrix", pikepdf.Array([1, 0, 0, 1, 0, 0])))
                    walk(x, x.get("/Resources") or res, _mul(m, ctm) if ctm is not None else None, depth + 1, stack)
        # Tiling patterns and Type 3 fonts are content too: scan for fonts, never trust their image placement.
        try:
            pats = res.get("/Pattern") if res is not None else None
            if isinstance(pats, pikepdf.Dictionary):
                for p in pats.values():
                    if isinstance(p, pikepdf.Stream):
                        walk(p, p.get("/Resources") or res, None, depth + 1, stack)
        except Exception:
            pass

    for page in pdf.pages:
        res = page.obj.get("/Resources")
        uu = float(page.obj.get("/UserUnit", 1) or 1)
        walk(page.obj, res, (uu, 0, 0, uu, 0, 0), 0, ())
        for annot in page.obj.get("/Annots", []) or []:
            try:
                ap = annot.get("/AP")
                if ap is None:
                    continue
                for key in ("/N", "/R", "/D"):
                    v = ap.get(key)
                    streams = [v] if isinstance(v, pikepdf.Stream) else (
                        [x for x in v.values() if isinstance(x, pikepdf.Stream)] if isinstance(v, pikepdf.Dictionary) else [])
                    for st in streams:
                        walk(st, st.get("/Resources"), None, 1, ())
            except Exception:
                continue

    # Anything with resources we did not scan (Type 3 fonts, orphan forms…) makes its fonts/images unsafe.
    for obj in pdf.objects:
        try:
            if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)) or "/Resources" not in obj:
                continue
            og = _og(obj)
            if og in s.scanned or obj.get("/Type") == "/Pages":
                continue
            res = obj.Resources
            mark_unsafe(res)
            xo = res.get("/XObject")
            if isinstance(xo, pikepdf.Dictionary):
                for x in xo.values():
                    if _og(x):
                        s.unplaced_images.add(_og(x))
        except Exception:
            continue
    try:
        dr = pdf.Root.get("/AcroForm", {}).get("/DR")
        if dr is not None:
            mark_unsafe(dr)  # form fields typeset new text at fill time
    except Exception:
        pass
    return s


# ------------------------------------------------------------------ dedupe
def _dedupe(pdf: pikepdf.Pdf) -> int:
    """Merge byte-identical image / font-file / ICC streams; returns how many duplicates were dropped."""
    dead: set[tuple[int, int]] = set()
    for _ in range(3):  # a second pass catches images whose (now merged) masks made them identical
        if not _dedupe_pass(pdf, dead):
            break
    return len(dead)


def _dedupe_pass(pdf: pikepdf.Pdf, dead: set[tuple[int, int]]) -> int:
    canon: dict[str, pikepdf.Object] = {}
    remap: dict[tuple[int, int], pikepdf.Object] = {}
    for obj in pdf.objects:
        if not isinstance(obj, pikepdf.Stream) or obj.objgen in dead:
            continue
        try:
            kind = obj.get("/Subtype")
            if not (kind == "/Image" or "/Length1" in obj or "/N" in obj or kind in ("/Type1C", "/CIDFontType0C", "/OpenType")):
                continue
            meta = repr(sorted((str(k), v.unparse() if isinstance(v, pikepdf.Object) else repr(v))
                               for k, v in obj.items() if k != "/Length"))  # unparse keeps "n 0 R" refs distinct
            h = hashlib.sha256(meta.encode() + b"\0" + obj.read_raw_bytes()).hexdigest()
        except Exception:
            continue
        if h in canon:
            remap[obj.objgen] = canon[h]
        else:
            canon[h] = obj
    if not remap:
        return 0
    dead.update(remap)

    def fix(container) -> None:
        if isinstance(container, (pikepdf.Dictionary, pikepdf.Stream)):
            for k in list(container.keys()):
                v = container[k]
                og = _og(v) if isinstance(v, (pikepdf.Stream,)) else None
                if og in remap:
                    container[k] = remap[og]
        elif isinstance(container, pikepdf.Array):
            for i, v in enumerate(container):
                og = _og(v) if isinstance(v, pikepdf.Stream) else None
                if og in remap:
                    container[i] = remap[og]

    def fix_deep(container, depth=0) -> None:
        """Direct (non-indirect) sub-dictionaries/arrays hold references too, e.g. /Resources << /XObject <<…>> >>."""
        fix(container)
        if depth > 6:
            return
        try:
            vals = container.values() if isinstance(container, (pikepdf.Dictionary, pikepdf.Stream)) else list(container)
        except Exception:
            return
        for v in vals:
            if isinstance(v, (pikepdf.Dictionary, pikepdf.Array)) and not v.is_indirect:
                fix_deep(v, depth + 1)

    for obj in list(pdf.objects):
        if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
            try:
                fix_deep(obj)
            except Exception:
                continue
    for page in pdf.pages:  # page dicts are objects too, but be explicit
        fix_deep(page.obj)
    return len(remap)


# ------------------------------------------------------------------ images
_GEN_FILTERS = {"/FlateDecode", "/LZWDecode", "/RunLengthDecode", "/ASCII85Decode", "/ASCIIHexDecode", "/Fl", "/LZW", "/RL", "/A85", "/AHx"}


def _filters(obj) -> list[str]:
    f = obj.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, pikepdf.Array) else [str(f)]


def _channels(cs) -> tuple[int, str] | None:
    """(components, kind) for colour spaces we can resample safely; kind in gray/rgb/cmyk/indexed."""
    try:
        if isinstance(cs, pikepdf.Name):
            return {"/DeviceGray": (1, "gray"), "/DeviceRGB": (3, "rgb"), "/DeviceCMYK": (4, "cmyk"),
                    "/G": (1, "gray"), "/RGB": (3, "rgb"), "/CMYK": (4, "cmyk")}.get(str(cs))
        if isinstance(cs, pikepdf.Array) and len(cs) >= 1:
            fam = str(cs[0])
            if fam == "/ICCBased":
                n = int(cs[1].get("/N", 0))
                return {1: (1, "gray"), 3: (3, "rgb"), 4: (4, "cmyk")}.get(n)
            if fam == "/CalRGB":
                return (3, "rgb")
            if fam == "/CalGray":
                return (1, "gray")
            if fam in ("/Indexed", "/I"):
                return (1, "indexed")
    except Exception:
        return None
    return None


def _is_inverted_decode(dec, n: int) -> bool:
    try:
        vals = [float(v) for v in dec]
        return len(vals) == 2 * n and all(vals[2 * i] == 1 and vals[2 * i + 1] == 0 for i in range(n))
    except Exception:
        return False


@dataclass
class _Decoded:
    arr: np.ndarray  # H×W×C uint8 (C=1 for gray/indexed)
    kind: str
    was_dct: bool
    adobe_inverted: bool  # PIL inverted Adobe CMYK on read


def _decode(obj, kind: str, n: int) -> _Decoded | None:
    from PIL import Image

    W, H = int(obj.Width), int(obj.Height)
    bpc = int(obj.get("/BitsPerComponent", 8))
    flt = _filters(obj)
    if flt and flt[-1] in ("/DCTDecode", "/DCT"):
        if len(flt) != 1:
            return None
        im = Image.open(io.BytesIO(obj.read_raw_bytes()))
        im.load()
        want = {"gray": "L", "rgb": "RGB", "cmyk": "CMYK"}.get(kind)
        if want is None or im.mode != want or im.size != (W, H):
            return None
        adobe = im.mode == "CMYK" and "adobe" in im.info
        arr = np.asarray(im)
        if arr.ndim == 2:
            arr = arr[:, :, None]
        return _Decoded(arr, kind, True, adobe)
    if any(f not in _GEN_FILTERS for f in flt):
        return None  # CCITT, JBIG2, JPX… leave alone
    raw = obj.read_bytes()
    if kind == "indexed":
        if bpc != 8:
            return None
        need = W * H
        if len(raw) < need:
            return None
        return _Decoded(np.frombuffer(raw[:need], np.uint8).reshape(H, W, 1), kind, False, False)
    if bpc == 8:
        need = W * H * n
        if len(raw) < need:
            return None
        arr = np.frombuffer(raw[:need], np.uint8).reshape(H, W, n)
    elif bpc == 16:
        need = W * H * n * 2
        if len(raw) < need:
            return None
        arr = (np.frombuffer(raw[:need], ">u2").reshape(H, W, n) >> 8).astype(np.uint8)
    else:
        return None  # 1/2/4-bit: line art / bilevel, keep lossless as is
    return _Decoded(arr, kind, False, False)


def _photo_like(arr: np.ndarray) -> bool:
    """Many distinct colours and no dominant flat colour → photograph; otherwise line art / text / flat graphics."""
    h, w, c = arr.shape
    step = max(1, int(math.sqrt(h * w / 40000)))
    s = arr[::step, ::step].reshape(-1, c)
    if c == 1:
        flat = s[:, 0].astype(np.uint32)
    else:
        flat = (s[:, 0].astype(np.uint32) << 16) | (s[:, 1].astype(np.uint32) << 8) | s[:, -1].astype(np.uint32)
    vals, counts = np.unique(flat, return_counts=True)
    if len(vals) <= 48:
        return False
    if counts.max() / len(flat) > 0.35 and len(vals) < 1024:
        return False
    return True


def _resize(arr: np.ndarray, size: tuple[int, int], method: str) -> np.ndarray:
    from PIL import Image

    resample = {"lanczos": Image.Resampling.LANCZOS, "box": Image.Resampling.BOX, "nearest": Image.Resampling.NEAREST}[method]
    c = arr.shape[2]
    if c in (1, 3):
        im = Image.fromarray(arr[:, :, 0] if c == 1 else arr, "L" if c == 1 else "RGB")
        return np.asarray(im.resize(size, resample)).reshape(size[1], size[0], c)
    # CMYK: resample channels independently (PIL resizes CMYK fine but keep it explicit)
    chans = [np.asarray(Image.fromarray(arr[:, :, i], "L").resize(size, resample)) for i in range(c)]
    return np.stack(chans, axis=-1)


def _jpeg(arr: np.ndarray, quality: int) -> bytes:
    from PIL import Image

    c = arr.shape[2]
    mode = {1: "L", 3: "RGB", 4: "CMYK"}[c]
    im = Image.fromarray(arr[:, :, 0] if c == 1 else arr, mode)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=int(quality), optimize=True, subsampling=2 if quality < 90 else 0)
    return buf.getvalue()


def _set_image(obj, data: bytes, filt: Name, w: int, h: int, decode=None, drop_decode: bool = False) -> None:
    obj.write(data, filter=filt)
    if "/DecodeParms" in obj:
        del obj["/DecodeParms"]
    obj.Width, obj.Height = w, h
    obj.BitsPerComponent = 8
    if drop_decode and "/Decode" in obj:
        del obj["/Decode"]
    if decode is not None:
        obj.Decode = decode


def _process_images(pdf: pikepdf.Pdf, scan: _Scan, st: Settings, stats: dict[str, Any]) -> None:
    skipped: Counter = Counter()
    smask_ids: set = set()
    images: dict[tuple[int, int], pikepdf.Stream] = {}
    for obj in pdf.objects:
        try:
            if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image":
                images[obj.objgen] = obj
                for k in ("/SMask", "/Mask"):
                    m = obj.get(k)
                    if isinstance(m, pikepdf.Stream):
                        smask_ids.add(m.objgen)
        except Exception:
            continue
    for og, obj in images.items():
        if og in smask_ids:
            continue  # handled with its parent
        if og not in scan.placements and og not in scan.unplaced_images:
            continue  # not drawn by any content we know of (orphan, thumbnail…): leave it
        try:
            r = _process_one(obj, og, scan, st)
        except Exception as exc:  # never let one odd image break the file
            r = f"error: {type(exc).__name__}"
        stats["images_seen"] += 1
        if r in ("jpeg", "flate", "jpeg+down", "flate+down"):
            stats["images_changed"] += 1
            if r.endswith("+down"):
                stats["images_downsampled"] += 1
            stats["jpeg" if r.startswith("jpeg") else "flate"] += 1
        else:
            skipped[r] += 1
    stats["images_left"] = dict(skipped)


def _process_one(obj: pikepdf.Stream, og, scan: _Scan, st: Settings) -> str:
    if obj.get("/ImageMask"):
        return "stencil mask"
    W, H = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
    if W < 8 or H < 8:
        return "tiny"
    ch = _channels(obj.get("/ColorSpace"))
    if ch is None:
        return "colour space not handled"
    n, kind = ch
    smask = obj.get("/SMask")
    if isinstance(smask, pikepdf.Stream) and "/Matte" in smask:
        return "pre-multiplied soft mask (Matte)"
    color_key = isinstance(obj.get("/Mask"), pikepdf.Array)
    bpc = int(obj.get("/BitsPerComponent", 8))
    if bpc < 8:
        return "bilevel / low bit depth (kept lossless)"

    # target size
    new_w, new_h = W, H
    if st.dpi and og in scan.placements and og not in scan.unplaced_images:
        sizes = scan.placements[og]
        dpi = min(min(W / (w / 72.0) if w > 1e-6 else 1e9, H / (h / 72.0) if h > 1e-6 else 1e9) for w, h in sizes)
        if dpi > st.dpi * st.above:
            f = st.dpi / dpi
            new_w, new_h = max(1, math.ceil(W * f)), max(1, math.ceil(H * f))
    down = (new_w, new_h) != (W, H)
    was_dct = _filters(obj)[-1:] in (["/DCTDecode"], ["/DCT"])
    if not down and was_dct and not st.recompress_jpeg:
        return "JPEG at target resolution"
    if not down and not was_dct and (st.lossless or kind == "indexed" or color_key) and bpc == 8:
        return "lossless, at target resolution"

    dec = _decode(obj, kind, n)
    if dec is None:
        return "encoding not handled"
    arr = dec.arr
    photo = kind != "indexed" and not color_key and (was_dct or _photo_like(arr))
    if down:
        method = "nearest" if (kind == "indexed" or color_key) else ("lanczos" if photo else "box")
        arr = _resize(arr, (new_w, new_h), method)
    old_len = len(obj.read_raw_bytes())
    dec_arr = obj.get("/Decode")

    if photo and not st.lossless:
        data = _jpeg(arr, st.quality)
        if not down and len(data) >= old_len * 0.95:
            return "already compact"
        if not down and not was_dct and len(data) >= old_len:
            return "already compact"
        decode = None
        drop = False
        if kind == "cmyk":
            # PIL writes Adobe-style inverted CMYK; PDF renderers read the stored samples, so
            # flip /Decode relative to how the source samples were stored.
            src_inv = dec_arr is not None and _is_inverted_decode(dec_arr, 4)
            stored_like_source = dec.adobe_inverted  # source stored inverted samples too
            want_inv = src_inv if stored_like_source else not src_inv
            drop = True
            decode = pikepdf.Array([1, 0] * 4) if want_inv else None
        _set_image(obj, data, Name.DCTDecode, new_w, new_h, decode=decode, drop_decode=drop)
        r = "jpeg"
    else:
        if dec.was_dct:
            if not down:
                return "JPEG at target resolution"
            data = _jpeg(arr, max(st.quality, 85))  # keep JPEG for JPEG sources
            filt = Name.DCTDecode
            if kind == "cmyk":
                return "CMYK JPEG line art (left alone)"
        else:
            data = zlib.compress(np.ascontiguousarray(arr).tobytes(), 9)
            filt = Name.FlateDecode
        if len(data) >= old_len and not down:
            return "already compact"
        _set_image(obj, data, filt, new_w, new_h)
        r = "flate" if filt == Name.FlateDecode else "jpeg"
    if down and isinstance(smask, pikepdf.Stream):
        _resize_mask(smask, new_w, new_h)
    return r + ("+down" if down else "")


def _resize_mask(m: pikepdf.Stream, w: int, h: int) -> None:
    """Downsample a soft mask to match its image (optional per spec, but saves space)."""
    try:
        if int(m.get("/BitsPerComponent", 8)) != 8 or any(f not in _GEN_FILTERS for f in _filters(m)):
            return
        mw, mh = int(m.Width), int(m.Height)
        if (mw, mh) == (w, h) or mw < w:
            return
        raw = m.read_bytes()
        if len(raw) < mw * mh:
            return
        arr = np.frombuffer(raw[: mw * mh], np.uint8).reshape(mh, mw, 1)
        arr = _resize(arr, (w, h), "box")
        _set_image(m, zlib.compress(arr.tobytes(), 9), Name.FlateDecode, w, h)
    except Exception:
        return


# ------------------------------------------------------------------ fonts
def _tag(gids: set[int]) -> str:
    h = hashlib.sha1(",".join(map(str, sorted(gids))).encode()).digest()
    return "".join(chr(65 + b % 26) for b in h[:6])


def _simple_gids(font_dict, tt, codes: set[int]) -> set[int]:
    """Glyphs a simple TrueType font may use for *codes* — the union of every mapping a reader might pick."""
    from fontTools.agl import toUnicode

    gids: set[int] = {0}
    order = tt.getGlyphOrder()
    name_to_gid = {n: i for i, n in enumerate(order)}
    cmaps = {(t.platformID, t.platEncID): t.cmap for t in tt["cmap"].tables} if "cmap" in tt else {}
    # code → glyph name via /Encoding
    names: dict[int, str] = {}
    enc = font_dict.get("/Encoding")
    base = "/WinAnsiEncoding"
    diffs = None
    if isinstance(enc, pikepdf.Name):
        base = str(enc)
    elif isinstance(enc, pikepdf.Dictionary):
        base = str(enc.get("/BaseEncoding", "/WinAnsiEncoding"))
        diffs = enc.get("/Differences")
    import codecs

    codec = {"/WinAnsiEncoding": "cp1252", "/MacRomanEncoding": "mac_roman"}.get(base, "cp1252")
    if diffs is not None:
        cur = 0
        for d in diffs:
            if isinstance(d, (int, float)) or (hasattr(d, "is_integer") and not isinstance(d, pikepdf.Name)):
                try:
                    cur = int(d)
                    continue
                except Exception:
                    pass
            if isinstance(d, pikepdf.Name):
                names[cur] = str(d)[1:]
                cur += 1
    for c in codes:
        for key in ((3, 0), (1, 0)):
            cm = cmaps.get(key)
            if cm:
                for cc in (c, 0xF000 + c, 0xF100 + c, 0xF200 + c):
                    g = cm.get(cc)
                    if g in name_to_gid:
                        gids.add(name_to_gid[g])
        uni: list[int] = []
        if c in names:
            nm = names[c]
            if nm in name_to_gid:
                gids.add(name_to_gid[nm])
            u = toUnicode(nm)
            if u:
                uni.append(ord(u[0]))
        try:
            uni.append(ord(codecs.decode(bytes([c]), codec)))
        except Exception:
            pass
        uni.append(c)
        for key in ((3, 1), (0, 3), (0, 4), (3, 10)):
            cm = cmaps.get(key)
            if cm:
                for u in uni:
                    g = cm.get(u)
                    if g in name_to_gid:
                        gids.add(name_to_gid[g])
    return gids


def _subset_fonts(pdf: pikepdf.Pdf, scan: _Scan) -> list[dict[str, Any]]:
    from fontTools import subset as fts
    from fontTools.ttLib import TTFont

    report: list[dict[str, Any]] = []
    # FontFile2 objgen → [(font dict that shows text, cid-dict or None)]
    users: dict[tuple[int, int], list[tuple[Any, Any]]] = defaultdict(list)
    files: dict[tuple[int, int], pikepdf.Stream] = {}
    for obj in pdf.objects:
        try:
            if not isinstance(obj, pikepdf.Dictionary) or obj.get("/Type") != "/Font":
                continue
            sub = obj.get("/Subtype")
            if sub == "/Type0":
                desc = (obj.get("/DescendantFonts") or [None])[0]
                if desc is None:
                    continue
                ff = desc.get("/FontDescriptor", {}).get("/FontFile2")
                if isinstance(ff, pikepdf.Stream):
                    users[ff.objgen].append((obj, desc))
                    files[ff.objgen] = ff
            elif sub == "/TrueType":
                ff = obj.get("/FontDescriptor", {}).get("/FontFile2")
                if isinstance(ff, pikepdf.Stream):
                    users[ff.objgen].append((obj, None))
                    files[ff.objgen] = ff
        except Exception:
            continue
    # CIDFontType2 dicts referenced by a Type0 are /Type /Font too, but we reach them through the Type0.

    for fog, ff in files.items():
        dicts = users[fog]
        top = dicts[0][1] if dicts[0][1] is not None else dicts[0][0]
        name = str(top.get("/BaseFont", "?")).lstrip("/")
        entry: dict[str, Any] = {"font": name}
        report.append(entry)
        try:
            if _SUBSET_TAG.match(name):
                entry["status"] = "skipped: already a subset"
                continue
            if any(_og(fd) in scan.unsafe_fonts or _og(fd) is None for fd, _ in dicts):
                entry["status"] = "skipped: used where text can change (form fields, Type 3, unscanned content)"
                continue
            gids: set[int] = {0}
            raw = ff.read_bytes()
            tt = TTFont(io.BytesIO(raw), lazy=False)
            if "glyf" not in tt:
                entry["status"] = "skipped: not a TrueType outline font"
                continue
            nglyphs = tt["maxp"].numGlyphs
            ok = True
            for fd, cid in dicts:
                codes = bytes(scan.font_codes.get(_og(fd), b""))
                if cid is not None:
                    enc = str(fd.get("/Encoding", ""))
                    if enc not in ("/Identity-H", "/Identity-V") or cid.get("/Subtype") != "/CIDFontType2":
                        ok = False
                        entry["status"] = f"skipped: CID encoding {enc or '?'} not handled"
                        break
                    if len(codes) % 2:
                        ok = False
                        entry["status"] = "skipped: odd-length CID string"
                        break
                    cids = set(np.frombuffer(codes, ">u2").tolist()) if codes else set()
                    m = cid.get("/CIDToGIDMap")
                    if m is None or (isinstance(m, pikepdf.Name) and str(m) == "/Identity"):
                        gids |= cids
                    elif isinstance(m, pikepdf.Stream):
                        table = np.frombuffer(m.read_bytes(), ">u2")
                        gids |= {int(table[c]) for c in cids if c < len(table)}
                    else:
                        ok = False
                        entry["status"] = "skipped: unusual CIDToGIDMap"
                        break
                else:
                    gids |= _simple_gids(fd, tt, set(codes))
            if not ok:
                continue
            gids = {g for g in gids if 0 <= g < nglyphs}
            opts = fts.Options()
            opts.retain_gids = True  # content streams address glyphs by id: keep the numbering
            opts.notdef_outline = True
            opts.glyph_names = True
            opts.legacy_cmap = True
            opts.symbol_cmap = True
            opts.name_IDs = ["*"]
            opts.name_languages = ["*"]
            opts.hinting = True
            opts.layout_features = []
            opts.recalc_bounds = False
            opts.recalc_timestamp = False
            opts.ignore_missing_glyphs = True
            sub = fts.Subsetter(opts)
            sub.populate(gids=sorted(gids))
            sub.subset(tt)
            buf = io.BytesIO()
            tt.save(buf)
            new = buf.getvalue()
            TTFont(io.BytesIO(new))  # must load back
            if len(new) >= len(raw) * 0.95:
                entry["status"] = "skipped: subset not smaller"
                continue
            before = len(ff.read_raw_bytes())
            ff.write(zlib.compress(new, 9), filter=Name.FlateDecode)
            ff.Length1 = len(new)
            for k in ("/Length2", "/Length3"):
                if k in ff:
                    del ff[k]
            tag = _tag(gids)
            for fd, cid in dicts:
                for d in (fd, cid):
                    if d is None:
                        continue
                    bf = str(d.get("/BaseFont", "")).lstrip("/")
                    if bf and not _SUBSET_TAG.match(bf):
                        d.BaseFont = Name("/" + tag + "+" + bf)
                    desc = d.get("/FontDescriptor")
                    if desc is not None and "/FontName" in desc and not _SUBSET_TAG.match(str(desc.FontName).lstrip("/")):
                        desc.FontName = Name("/" + tag + "+" + str(desc.FontName).lstrip("/"))
            entry.update(status="subset", glyphs=len(gids), of=nglyphs, before=before, after=len(ff.read_raw_bytes()))
        except Exception as exc:
            entry["status"] = f"skipped: {type(exc).__name__}: {str(exc)[:60]}"
    return report


# ------------------------------------------------------------------ strip
def _strip(pdf: pikepdf.Pdf, st: Settings) -> dict[str, int]:
    out: Counter = Counter()
    if st.strip_metadata:
        if "/Metadata" in pdf.Root:
            del pdf.Root["/Metadata"]
            out["xmp"] += 1
        try:
            for k in list(pdf.docinfo.keys()):
                if k != "/Title":  # keep the title: readers show it, the gate wants it
                    del pdf.docinfo[k]
                    out["docinfo"] += 1
        except Exception:
            pass
    if st.strip_thumbnails:
        for page in pdf.pages:
            if "/Thumb" in page.obj:
                del page.obj["/Thumb"]
                out["thumbnails"] += 1
    if st.strip_private:
        if "/PieceInfo" in pdf.Root:
            del pdf.Root["/PieceInfo"]
            out["pieceinfo"] += 1
        for obj in pdf.objects:
            try:
                if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                    if "/PieceInfo" in obj:
                        del obj["/PieceInfo"]
                        out["pieceinfo"] += 1
                    if "/Metadata" in obj and obj.objgen != pdf.Root.objgen and obj.get("/Type") != "/Catalog":
                        del obj["/Metadata"]
                        out["object_xmp"] += 1
            except Exception:
                continue
        for page in pdf.pages:
            if "/PieceInfo" in page.obj:
                del page.obj["/PieceInfo"]
                out["pieceinfo"] += 1
    if st.strip_attachments:
        names = pdf.Root.get("/Names")
        if names is not None and "/EmbeddedFiles" in names:
            del names["/EmbeddedFiles"]
            out["attachments"] += 1
        for page in pdf.pages:
            annots = page.obj.get("/Annots")
            if annots is not None:
                keep = [a for a in annots if a.get("/Subtype") != "/FileAttachment"]
                if len(keep) != len(annots):
                    out["attachments"] += len(annots) - len(keep)
                    page.obj.Annots = pikepdf.Array(keep)
    return dict(out)


# ------------------------------------------------------------------ one pass
def _run(src: Path, dst: Path, st: Settings, password: str | None) -> dict[str, Any]:
    stats: dict[str, Any] = {"images_seen": 0, "images_changed": 0, "images_downsampled": 0, "jpeg": 0, "flate": 0}
    with pikepdf.open(str(src), password=password or "") as pdf:
        if st.dedupe:
            stats["duplicates_merged"] = _dedupe(pdf)
        scan = _scan(pdf)
        _process_images(pdf, scan, st, stats)
        if st.subset_fonts:
            stats["fonts"] = _subset_fonts(pdf, scan)
        stats["stripped"] = _strip(pdf, st)
        try:
            pdf.remove_unreferenced_resources()
        except Exception:
            pass
        pdf.save(
            str(dst),
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate if st.object_streams else pikepdf.ObjectStreamMode.preserve,
            recompress_flate=True,
            linearize=st.linearize,
            encryption=True if pdf.is_encrypted else None,
        )
    stats["linearized"] = st.linearize
    return stats


def _ladder(st: Settings) -> list[Settings]:
    d = st.dpi or 300
    q = st.quality
    steps = [
        st,
        dataclasses.replace(st, recompress_jpeg=True, quality=min(q, 65), strip_metadata=True, strip_private=True),
        dataclasses.replace(st, dpi=min(d, 120), recompress_jpeg=True, quality=min(q, 55), strip_metadata=True, strip_private=True, lossless=False),
        dataclasses.replace(st, dpi=min(d, 100), recompress_jpeg=True, quality=min(q, 45), strip_metadata=True, strip_private=True, lossless=False),
        dataclasses.replace(st, dpi=min(d, 72), recompress_jpeg=True, quality=min(q, 35), strip_metadata=True, strip_private=True, lossless=False),
        dataclasses.replace(st, dpi=min(d, 50), recompress_jpeg=True, quality=min(q, 25), strip_metadata=True, strip_private=True, lossless=False),
    ]
    out: list[Settings] = []
    for s in steps:
        if s not in out:
            out.append(s)
    return out


# ------------------------------------------------------------------ public
def optimize(
    src: str | Path,
    *,
    dpi: float | None = 150,
    quality: int = 75,
    settings: Settings | None = None,
    target_mb: float | None = None,
    linearize: bool = False,
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Make a smaller copy. *settings* (or a :data:`PRESETS` entry) overrides *dpi*/*quality*/*linearize*.

    With *target_mb*, stronger settings are tried until the file fits; ``data["budget_met"]`` says
    whether it did, ``data["attempts"]`` lists what was tried. The gate checks image resolution
    against the dpi actually used and, with a budget, the size.
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    st = settings or Settings(dpi=dpi, quality=quality, linearize=linearize)
    if settings is not None and linearize:
        st = dataclasses.replace(st, linearize=True)
    if not 1 <= st.quality <= 100:
        raise ValueError("quality must be 1–100")
    dst = output_path(src, "optimized", out=out)
    before = src.stat().st_size
    data: dict[str, Any] = {"before_bytes": before}
    used = st
    with tempfile.TemporaryDirectory() as td:
        if target_mb is None:
            stats = _run(src, dst, st, password)
        else:
            budget = target_mb * 1e6
            attempts = []
            best: tuple[int, Path, Settings, dict] | None = None
            for k, s in enumerate(_ladder(st)):
                tmp = Path(td) / f"try{k}.pdf"
                stt = _run(src, tmp, s, password)
                size = tmp.stat().st_size
                attempts.append({"dpi": s.dpi, "quality": s.quality, "bytes": size})
                if best is None or size < best[0]:
                    best = (size, tmp, s, stt)
                if size <= budget:
                    break
            assert best is not None
            shutil.copyfile(best[1], dst)
            _, _, used, stats = best
            data.update(attempts=attempts, budget_mb=target_mb, budget_met=best[0] <= budget)
    after = dst.stat().st_size
    data.update(stats)
    data.update(after_bytes=after, saved_bytes=before - after, saved_pct=round(100 * (1 - after / before), 1) if before else 0.0,
                dpi=used.dpi, quality=used.quality)
    if any(str(f.get("status", "")).startswith("skipped") for f in data.get("fonts", [])):
        data["font_note"] = "some fonts were not re-subset; see data['fonts'][*]['status']"
    opts = dataclasses.replace(options) if options is not None else GateOptions()
    if opts.min_image_dpi is None and used.dpi:
        opts.min_image_dpi = used.dpi  # self-check: nothing we downsampled went below the target
    if opts.max_size_mb is None and target_mb is not None:
        opts.max_size_mb = target_mb
    res = finalize(dst, baseline=src, options=opts, password=password, data=data, quiet=quiet)
    if not quiet:
        print(f"  {before / 1e6:.2f} MB → {after / 1e6:.2f} MB ({data['saved_pct']:.0f}% smaller)"
              + ("" if target_mb is None else f"; budget {target_mb:g} MB {'met' if data['budget_met'] else 'NOT met'}"))
    return res


@command("optimize", "Shrink a PDF: downsample/recompress images, subset fonts, dedupe, linearize", group="optimize")
def _cli(p):
    p.add_argument("pdf")
    p.add_argument("--preset", choices=sorted(PRESETS), help="screen (150 dpi), ebook, print (300 dpi), lossless")
    p.add_argument("--dpi", type=float, default=None, help="target image resolution (default 150; 0 = no downsampling)")
    p.add_argument("--quality", type=int, default=None, help="JPEG quality 1–100 (default 75)")
    p.add_argument("--target-mb", type=float, help="size budget: try stronger settings until the file fits")
    p.add_argument("--lossless", action="store_true", help="never convert images to JPEG")
    p.add_argument("--recompress-jpeg", action="store_true", help="re-encode existing JPEGs at --quality")
    p.add_argument("--no-font-subset", action="store_true")
    p.add_argument("--strip-metadata", action="store_true", help="remove XMP and document info (title kept)")
    p.add_argument("--keep-thumbnails", action="store_true")
    p.add_argument("--strip-private", action="store_true", help="remove application private data (PieceInfo)")
    p.add_argument("--strip-attachments", action="store_true")
    p.add_argument("--linearize", "--fast-web-view", dest="linearize", action="store_true")
    add_common(p)

    def run(ns):
        base = dataclasses.replace(PRESETS[ns.preset]) if ns.preset else Settings()
        if ns.dpi is not None:
            base.dpi = ns.dpi or None
        if ns.quality is not None:
            base.quality = ns.quality
        base.lossless = base.lossless or ns.lossless
        base.recompress_jpeg = base.recompress_jpeg or ns.recompress_jpeg
        base.subset_fonts = not ns.no_font_subset
        base.strip_metadata = ns.strip_metadata
        base.strip_thumbnails = not ns.keep_thumbnails
        base.strip_private = ns.strip_private
        base.strip_attachments = ns.strip_attachments
        base.linearize = ns.linearize
        return optimize(ns.pdf, settings=base, target_mb=ns.target_mb, out=ns.out, password=ns.password,
                        options=gate_options_from(ns))

    return run
