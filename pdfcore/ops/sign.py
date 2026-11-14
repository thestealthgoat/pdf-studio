"""Digital signatures with pyHanko: sign, certify, timestamp, verify (PAdES B-B … B-LTA).

Levels (ETSI EN 319 142 / PAdES baseline)::

    b-b    signature only (CAdES detached, signing-certificate-v2 attribute)
    b-t    + signature timestamp from a time-stamping authority (TSA)
    b-lt   + revocation info (certificates, CRLs, OCSP) in the document security store (DSS)
    b-lta  + document timestamp over the DSS (archive timestamp)

Every signature is written as an **incremental update**: the bytes of the input are kept
unchanged and the signature revision is appended, so earlier signatures stay valid. The quality
gate only reads the output, it never rewrites it.

Secrets: key passphrases come from the ``passphrase`` argument or an environment variable
(``PDFCORE_SIGN_PASSPHRASE`` by default). They are never printed or stored in results.

Visible appearances are drawn with ReportLab in the house font (Inter, embedded) and imported as
a static appearance stream; they contain no transparency, so PDF/A inputs stay PDF/A.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Sequence

from pdfcore.gate import GateOptions, GateReport
from pdfcore.gate.checks import Finding
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path, parse_pages
from pdfcore.result import OpResult
from pdfcore.types import Rect

__all__ = [
    "LEVELS", "DOCMDP", "PASSPHRASE_ENV", "load_signer", "sign", "certify", "timestamp", "verify",
    "SignatureReport",
]

# pyHanko logs validation tracebacks (e.g. an untrusted root) at ERROR; results carry that information.
for _lg in ("pyhanko", "pyhanko_certvalidator"):
    logging.getLogger(_lg).addHandler(logging.NullHandler())

LEVELS = ("b-b", "b-t", "b-lt", "b-lta")
DOCMDP = {"no-changes": 1, "form-fill": 2, "annotate": 3}
PASSPHRASE_ENV = "PDFCORE_SIGN_PASSPHRASE"
DEFAULT_BOX = (200.0, 60.0)  # width, height of a default visible signature box (pt)


# ====================================================================== helpers
def _level(level: str) -> str:
    v = level.strip().lower().replace("_", "-").replace("pades-", "")
    v = {"bb": "b-b", "bt": "b-t", "blt": "b-lt", "blta": "b-lta", "b": "b-b", "t": "b-t", "lt": "b-lt",
         "lta": "b-lta"}.get(v, v)
    if v not in LEVELS:
        raise ValueError(f"unknown PAdES level {level!r}; use {', '.join(LEVELS)}")
    return v


def _docmdp(docmdp: str):
    from pyhanko.sign.fields import MDPPerm

    k = docmdp.strip().lower().replace("_", "-")
    k = {"none": "no-changes", "nochanges": "no-changes", "forms": "form-fill", "fill-forms": "form-fill",
         "form-filling": "form-fill", "annotations": "annotate", "comment": "annotate"}.get(k, k)
    if k not in DOCMDP:
        raise ValueError(f"unknown DocMDP permission {docmdp!r}; use {', '.join(DOCMDP)}")
    return MDPPerm(DOCMDP[k]), k


def _passphrase(passphrase: str | bytes | None, env: str | None) -> bytes | None:
    if passphrase is None and env:
        passphrase = os.environ.get(env)
    if passphrase is None or passphrase == "":
        return None
    return passphrase.encode("utf-8") if isinstance(passphrase, str) else passphrase


def _load_certs(items: Iterable[Any] | None) -> list:
    """Certificates from paths (PEM bundles or DER) or asn1crypto/cryptography objects."""
    from asn1crypto import x509 as ax509
    from pyhanko.keys import load_certs_from_pemder

    out: list = []
    for it in items or ():
        if isinstance(it, ax509.Certificate):
            out.append(it)
        elif hasattr(it, "public_bytes"):  # cryptography certificate
            from cryptography.hazmat.primitives.serialization import Encoding

            out.append(ax509.Certificate.load(it.public_bytes(Encoding.DER)))
        else:
            p = Path(it)
            if not p.exists():
                raise FileNotFoundError(f"certificate file not found: {p}")
            out.extend(load_certs_from_pemder([str(p)]))
    return out


def _load_der_or_pem(items: Iterable[Any] | None, kind: str) -> list:
    from asn1crypto import crl as acrl, ocsp as aocsp, pem

    cls = acrl.CertificateList if kind == "crl" else aocsp.OCSPResponse
    out: list = []
    for it in items or ():
        if isinstance(it, cls):
            out.append(it)
            continue
        data = it if isinstance(it, (bytes, bytearray)) else Path(it).read_bytes()
        if pem.detect(data):
            for _, _, der in pem.unarmor(bytes(data), multiple=True):
                out.append(cls.load(der))
        else:
            out.append(cls.load(bytes(data)))
    return out


def _validation_context(trust_roots: Any = None, *, other_certs: Iterable[Any] = (), crls: Iterable[Any] = (),
                        ocsps: Iterable[Any] = (), allow_fetching: bool = False, revocation_mode: str = "soft-fail",
                        dss=None, **extra):
    """A pyhanko-certvalidator ValidationContext. ``trust_roots`` None/"system" = OS trust store."""
    from pyhanko_certvalidator import ValidationContext

    kw: dict[str, Any] = dict(other_certs=_load_certs(other_certs), crls=_load_der_or_pem(crls, "crl"),
                              ocsps=_load_der_or_pem(ocsps, "ocsp"), allow_fetching=allow_fetching,
                              revocation_mode=revocation_mode, **extra)
    if trust_roots is not None and not (isinstance(trust_roots, str) and trust_roots == "system"):
        kw["trust_roots"] = _load_certs([trust_roots] if isinstance(trust_roots, (str, Path)) else trust_roots)
    if dss is not None:
        return dss.as_validation_context(kw)
    return ValidationContext(**kw)


def load_signer(*, key: str | Path | None = None, cert: str | Path | None = None, pkcs12: str | Path | None = None,
                chain: Sequence[str | Path] = (), passphrase: str | bytes | None = None,
                passphrase_env: str | None = PASSPHRASE_ENV):
    """Load a pyHanko SimpleSigner from PEM/DER key + certificate, or a PKCS#12 bundle."""
    from pyhanko.sign import signers

    pw = _passphrase(passphrase, passphrase_env)
    chain_files = [str(c) for c in chain]
    for f in [p for p in (key, cert, pkcs12) if p is not None] + chain_files:
        if not Path(f).exists():
            raise FileNotFoundError(f"signing material not found: {f}")
    if pkcs12 is not None:
        s = signers.SimpleSigner.load_pkcs12(str(pkcs12), ca_chain_files=chain_files or None, passphrase=pw)
        what = "PKCS#12 file"
    elif key is not None and cert is not None:
        s = signers.SimpleSigner.load(str(key), str(cert), ca_chain_files=chain_files or None, key_passphrase=pw)
        what = "key/certificate"
    else:
        raise ValueError("signing needs --key and --cert (PEM/DER) or --pkcs12")
    if s is None:
        hint = "missing or wrong passphrase?" if pw is None else "wrong passphrase or mismatched key/certificate?"
        raise ValueError(f"could not load the {what} ({hint} set it with --passphrase-env / {PASSPHRASE_ENV})")
    return s


def _timestamper(tsa_url: str | None, timestamper: Any):
    if timestamper is not None:
        return timestamper
    if tsa_url:
        from pyhanko.sign.timestamps import HTTPTimeStamper

        return HTTPTimeStamper(tsa_url, timeout=20)
    return None


def _cn(cert) -> str:
    try:
        nat = cert.subject.native
        return str(nat.get("common_name") or nat.get("organization_name") or cert.subject.human_friendly)
    except Exception:
        return "unknown signer"


def _read_input(src: Path) -> bytes:
    """Input bytes, with an EOL appended after a final ``%%EOF`` that lacks one.

    PDF/A (ISO 19005 6.1.9) needs every ``obj`` keyword on its own line; some writers end the file
    right after ``%%EOF``, so the appended revision would start on the same line. The original
    bytes stay an exact prefix of the output.
    """
    data = src.read_bytes()
    if not data.endswith((b"\n", b"\r")):
        data += b"\n"
    return data


def _page_geometry(data: bytes, password: str | None) -> tuple[list[tuple[float, float, float, float]], list[tuple[int, int]]]:
    """(llx, lly, urx, ury) of each page's crop box, and each page's (objnum, gen)."""
    import pikepdf

    try:
        pdf = pikepdf.open(io.BytesIO(data), password=password or "")
    except pikepdf.PasswordError as exc:
        raise ValueError("the input is encrypted: pass --password (the owner password to allow signing)") from exc
    with pdf:
        out, ids = [], []
        for pg in pdf.pages:
            box = pg.obj.get("/CropBox", None) or pg.mediabox
            v = [float(x) for x in box]
            out.append((min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3])))
            ids.append(pg.obj.objgen)
        return out, ids


def _box_from_rect(rect: Rect | Sequence[float], page_box: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """Top-left-origin Rect (x0, top, x1, bottom) in points → PDF user-space box for pyHanko."""
    r = rect if isinstance(rect, Rect) else Rect(*[float(v) for v in rect])
    llx, lly, urx, ury = page_box
    h = ury - lly
    x0, y0, x1, y1 = r.to_pdf(h)
    return (round(llx + x0), round(lly + y0), round(llx + x1), round(lly + y1))


def _default_box(page_box: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    llx, lly, urx, ury = page_box
    w, h = DEFAULT_BOX
    m = 54.0
    return (round(urx - m - w), round(lly + m), round(urx - m), round(lly + m + h))


# ---------------------------------------------------------------------- appearance
def _fit(text: str, font: str, size: float, width: float) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    while size > 4 and stringWidth(text, font, size) > width:
        size -= 0.25
    return size


def render_appearance(path: Path, width: float, height: float, *, signer_name: str, when: _dt.datetime,
                      reason: str | None = None, location: str | None = None, image: str | Path | None = None,
                      heading: str = "Digitally signed by") -> Path:
    """Draw the visible signature box (one page, *width* × *height* pt) with ReportLab in Inter."""
    from reportlab.pdfgen import canvas

    from pdfcore.fonts import register_reportlab

    f = register_reportlab()
    sans, bold = f["sans"], f["sans-bold"]
    # initialFontName: otherwise ReportLab lists an unembedded Helvetica in the page resources
    c = canvas.Canvas(str(path), pagesize=(width, height), pageCompression=1, initialFontName=sans)
    c.setTitle("Signature appearance")
    pad = max(2.0, min(width, height) * 0.06)
    c.setStrokeGray(0.35)
    c.setLineWidth(0.75)
    c.rect(0.5, 0.5, width - 1, height - 1, stroke=1, fill=0)
    x0 = pad
    if image is not None:
        from PIL import Image
        from reportlab.lib.utils import ImageReader

        im = Image.open(str(image))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])  # flatten: no soft mask → no transparency (PDF/A-1 safe)
            im = bg
        else:
            im = im.convert("RGB")
        ih = height - 2 * pad
        iw = min(width * 0.4, ih * im.width / max(1, im.height))
        ih = min(ih, iw * im.height / max(1, im.width))
        c.drawImage(ImageReader(im), x0, (height - ih) / 2, iw, ih)
        x0 += iw + pad
    tw = width - x0 - pad
    stamp = when.strftime("%Y-%m-%d %H:%M %z")
    if len(stamp) > 5 and stamp[-5] in "+-":
        stamp = stamp[:-2] + ":" + stamp[-2:]
    lines: list[tuple[str, str, float]] = [(heading, sans, 0.75), (signer_name, bold, 1.0), (f"Date: {stamp}", sans, 0.75)]
    if reason:
        lines.append((f"Reason: {reason}", sans, 0.75))
    if location:
        lines.append((f"Location: {location}", sans, 0.75))
    base = min(12.0, (height - 2 * pad) / (sum(r for _, _, r in lines) * 1.25))
    sizes = [_fit(t, fn, base * r, tw) for t, fn, r in lines]
    total = sum(s * 1.25 for s in sizes)
    y = (height + total) / 2
    c.setFillGray(0.1)
    for (t, fn, _), s in zip(lines, sizes):
        y -= s * 1.25
        c.setFont(fn, s)
        c.drawString(x0, y + s * 0.25, t)
    c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------------- fields
def _existing_fields(w) -> list[tuple[str, bool, Any]]:
    from pyhanko.sign.fields import enumerate_sig_fields

    out = []
    for name, value, ref in enumerate_sig_fields(w.prev):
        out.append((name, value is not None, ref))
    return out


def _field_widget_geometry(ref, page_ids: list[tuple[int, int]]) -> tuple[int | None, tuple[float, ...] | None]:
    """Page index and /Rect of a signature field's (first) widget."""
    fld = ref.get_object()
    widget = fld
    if "/Rect" not in fld and "/Kids" in fld:
        widget = fld["/Kids"][0].get_object()
    rect = tuple(float(v) for v in widget.get("/Rect", [0, 0, 0, 0]))
    page_ix = None
    try:
        pref = widget.raw_get("/P")
        key = (pref.idnum, pref.generation)
        page_ix = page_ids.index(key) if key in page_ids else None
    except Exception:
        page_ix = None
    return page_ix, rect


def _declared_pdfa(path: Path, password: str | None = None) -> str | None:
    try:
        from pdfcore.ops.standards import declared_standard

        std = declared_standard(path, password=password)
        return std if std and std.startswith("pdfa") else None
    except Exception:
        return None


# ====================================================================== sign / certify
def _do_sign(src: str | Path, *, key=None, cert=None, pkcs12=None, chain=(), passphrase=None,
             passphrase_env: str | None = PASSPHRASE_ENV, signer=None, field: str | None = None, page: int | None = None,
             rect: Rect | Sequence[float] | None = None, visible: bool | None = None, image: str | Path | None = None,
             name: str | None = None, reason: str | None = None, location: str | None = None,
             contact: str | None = None, level: str = "b-b", tsa_url: str | None = None, timestamper=None,
             trust_roots: Any = None, other_certs: Iterable[Any] = (), crls: Iterable[Any] = (),
             ocsps: Iterable[Any] = (), allow_fetching: bool = True, md_algorithm: str = "sha256",
             certify_perm: str | None = None, out: str | Path | None = None, password: str | None = None,
             options: GateOptions | None = None, quiet: bool = False) -> OpResult:
    from pyhanko.pdf_utils import layout
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    from pyhanko.stamp import StaticStampStyle

    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    lv = _level(level)
    ts = _timestamper(tsa_url, timestamper)
    if lv != "b-b" and ts is None:
        raise ValueError(f"level {lv} needs a time-stamping authority: pass --tsa URL")
    s = signer or load_signer(key=key, cert=cert, pkcs12=pkcs12, chain=chain, passphrase=passphrase,
                              passphrase_env=passphrase_env)
    data = _read_input(src)
    geom, objgens = _page_geometry(data, password)
    w = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
    if w.prev.encrypted:
        if not password:
            raise ValueError(f"{src.name} is encrypted: pass --password")
        try:
            w.encrypt(password)
        except Exception as exc:
            raise ValueError(f"{src.name}: wrong password") from exc

    existing = _existing_fields(w)
    perm = None
    mdp_name = None
    if certify_perm is not None:
        perm, mdp_name = _docmdp(certify_perm)
        if any(filled for _, filled, _ in existing):
            raise ValueError("a certification signature must be the first signature; this file is already signed")

    # which field
    new_field = True
    page_ix = page
    box: tuple[float, ...] | None = None
    if field is not None:
        match = [e for e in existing if e[0] == field]
        if match:
            if match[0][1]:
                raise ValueError(f"signature field {field!r} is already signed")
            new_field = False
    else:
        empty = [e for e in existing if not e[1]]
        if empty and rect is None:
            field, new_field = empty[0][0], False
        else:
            taken = {e[0] for e in existing}
            k = 1
            while f"Signature{k}" in taken:
                k += 1
            field = f"Signature{k}"
    if not new_field:
        ref = next(e[2] for e in existing if e[0] == field)
        pix, frect = _field_widget_geometry(ref, objgens)
        page_ix = pix if pix is not None else (page_ix or 0)
        if frect and abs(frect[2] - frect[0]) > 1 and abs(frect[3] - frect[1]) > 1:
            box = frect
        if visible is None:
            visible = box is not None
        if visible and box is None:
            raise ValueError(f"field {field!r} has no visible area; sign invisibly or pick another field")
    else:
        n = len(geom)
        if page_ix is None:
            page_ix = n - 1 if (visible or rect is not None) else 0
        if page_ix < 0:
            page_ix += n
        if not (0 <= page_ix < n):
            raise ValueError(f"page {page_ix + 1} out of range 1–{n}")
        if visible is None:
            visible = rect is not None
        if visible:
            box = _box_from_rect(rect, geom[page_ix]) if rect is not None else _default_box(geom[page_ix])

    signer_name = name or _cn(s.signing_cert)
    when = _dt.datetime.now().astimezone()
    vc = None
    if lv in ("b-lt", "b-lta"):
        vc = _validation_context(trust_roots, other_certs=other_certs, crls=crls, ocsps=ocsps,
                                 allow_fetching=allow_fetching)
    meta_kw: dict[str, Any] = dict(
        field_name=field, md_algorithm=md_algorithm, reason=reason, location=location, contact_info=contact,
        subfilter=fields.SigSeedSubFilter.PADES, embed_validation_info=vc is not None,
        use_pades_lta=lv == "b-lta", validation_context=vc,
    )
    if name:
        meta_kw["name"] = name
    if perm is not None:
        meta_kw.update(certify=True, docmdp_permissions=perm)
    meta = signers.PdfSignatureMetadata(**meta_kw)

    tag = "certified" if perm is not None else "signed"
    dst = output_path(src, tag, out=out)
    with tempfile.TemporaryDirectory(prefix="pdfcore-sign-") as tmp:
        style = None
        if visible and box is not None:
            bw, bh = abs(box[2] - box[0]), abs(box[3] - box[1])
            ap = render_appearance(Path(tmp) / "appearance.pdf", bw, bh, signer_name=signer_name, when=when,
                                   reason=reason, location=location, image=image,
                                   heading="Certified by" if perm is not None else "Digitally signed by")
            style = StaticStampStyle.from_pdf_file(
                str(ap), border_width=0,
                background_layout=layout.SimpleBoxLayoutRule(
                    x_align=layout.AxisAlignment.ALIGN_MID, y_align=layout.AxisAlignment.ALIGN_MID,
                    margins=layout.Margins.uniform(0), inner_content_scaling=layout.InnerScaling.STRETCH_FILL),
            )
        spec = None
        if new_field:
            ibox = tuple(int(round(v)) for v in box) if (visible and box is not None) else None
            spec = fields.SigFieldSpec(sig_field_name=field, on_page=page_ix, box=ibox)
        pdf_signer = signers.PdfSigner(meta, signer=s, timestamper=ts, stamp_style=style, new_field_spec=spec)
        buf = io.BytesIO()
        try:
            pdf_signer.sign_pdf(w, output=buf)
        except Exception as exc:
            msg = str(exc) or exc.__class__.__name__
            if lv in ("b-lt", "b-lta"):
                msg += " (for b-lt/b-lta the signer chain must validate: pass --trust-root and --crl/--ocsp, or allow fetching)"
            raise ValueError(f"signing failed: {msg}") from exc
    dst.parent.mkdir(parents=True, exist_ok=True)
    out_bytes = buf.getvalue()
    if not out_bytes.startswith(data):
        raise RuntimeError("internal error: signed output is not an incremental update of the input")
    dst.write_bytes(out_bytes)

    info: dict[str, Any] = {
        "field": field, "level": lv, "signer": signer_name, "page": (page_ix + 1) if page_ix is not None else None,
        "visible": bool(visible), "box": list(box) if (visible and box is not None) else None,
        "new_field": new_field, "reason": reason, "location": location, "incremental": True,
        "timestamped": ts is not None,
    }
    if perm is not None:
        info["docmdp"] = mdp_name
    warnings: list[str] = []
    if lv in ("b-lt", "b-lta"):
        rev = _dss_counts(out_bytes, password)
        info["dss"] = rev
        if not (rev.get("crls") or rev.get("ocsps")):
            warnings.append("no revocation info (CRL/OCSP) could be collected; the file is not LTV-enabled")
    if warnings:
        info["warnings"] = warnings
    opts = options
    if opts is None:
        opts = GateOptions(standard=_declared_pdfa(src, password))
    res = finalize(dst, src, options=opts, password=password, data=info, quiet=quiet)
    if warnings and not quiet:
        for wmsg in warnings:
            print(f"  warn  {wmsg}")
    return res


def _dss_counts(data: bytes, password: str | None) -> dict[str, int]:
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import DocumentSecurityStore
    from pyhanko.sign.validation.errors import NoDSSFoundError

    r = PdfFileReader(io.BytesIO(data), strict=False)
    if r.encrypted and password:
        r.decrypt(password)
    try:
        dss = DocumentSecurityStore.read_dss(r)
    except NoDSSFoundError:
        return {"certs": 0, "crls": 0, "ocsps": 0}
    return {"certs": len(dss.certs), "crls": len(dss.crls), "ocsps": len(dss.ocsps)}


def sign(src: str | Path, *, key: str | Path | None = None, cert: str | Path | None = None,
         pkcs12: str | Path | None = None, chain: Sequence[str | Path] = (), passphrase: str | bytes | None = None,
         passphrase_env: str | None = PASSPHRASE_ENV, signer=None, field: str | None = None, page: int | None = None,
         rect: Rect | Sequence[float] | None = None, visible: bool | None = None, image: str | Path | None = None,
         name: str | None = None, reason: str | None = None, location: str | None = None,
         contact: str | None = None, level: str = "b-b", tsa_url: str | None = None, timestamper=None,
         trust_roots: Any = None, other_certs: Iterable[Any] = (), crls: Iterable[Any] = (),
         ocsps: Iterable[Any] = (), allow_fetching: bool = True, md_algorithm: str = "sha256",
         out: str | Path | None = None, password: str | None = None, options: GateOptions | None = None,
         quiet: bool = False) -> OpResult:
    """Sign a copy of *src* (PAdES, incremental update).

    Identity: *key* + *cert* (PEM/DER, optional *chain*), *pkcs12*, or a ready pyHanko *signer*.
    *field*: existing empty signature field to fill, or name for a new one (default: the first empty
    field, else ``SignatureN``). *page* is 0-based (default: last page for visible, first for
    invisible). *rect* is ``(x0, top, x1, bottom)`` in points from the page's top-left corner;
    giving it makes the signature visible (box with signer name, date, reason, optional *image*).
    *level*: b-b | b-t | b-lt | b-lta. b-t and up need *tsa_url* (or a pyHanko *timestamper*); b-lt
    and b-lta embed revocation info, validating the signer against *trust_roots* (paths/certs, or
    None = system store) using *crls*/*ocsps* supplied here and, if *allow_fetching*, the network.
    """
    return _do_sign(src, key=key, cert=cert, pkcs12=pkcs12, chain=chain, passphrase=passphrase,
                    passphrase_env=passphrase_env, signer=signer, field=field, page=page, rect=rect, visible=visible,
                    image=image, name=name, reason=reason, location=location, contact=contact, level=level,
                    tsa_url=tsa_url, timestamper=timestamper, trust_roots=trust_roots, other_certs=other_certs,
                    crls=crls, ocsps=ocsps, allow_fetching=allow_fetching, md_algorithm=md_algorithm, out=out,
                    password=password, options=options, quiet=quiet)


def certify(src: str | Path, *, docmdp: str = "form-fill", key: str | Path | None = None,
            cert: str | Path | None = None, pkcs12: str | Path | None = None, chain: Sequence[str | Path] = (),
            passphrase: str | bytes | None = None, passphrase_env: str | None = PASSPHRASE_ENV, signer=None,
            field: str | None = None, page: int | None = None, rect: Rect | Sequence[float] | None = None,
            visible: bool | None = None, image: str | Path | None = None, name: str | None = None,
            reason: str | None = None, location: str | None = None, contact: str | None = None,
            level: str = "b-b", tsa_url: str | None = None, timestamper=None, trust_roots: Any = None,
            other_certs: Iterable[Any] = (), crls: Iterable[Any] = (), ocsps: Iterable[Any] = (),
            allow_fetching: bool = True, md_algorithm: str = "sha256", out: str | Path | None = None,
            password: str | None = None, options: GateOptions | None = None, quiet: bool = False) -> OpResult:
    """Certification (author) signature with DocMDP: *docmdp* = no-changes | form-fill | annotate.

    Must be the first signature in the document. Same options as :func:`sign`.
    """
    return _do_sign(src, key=key, cert=cert, pkcs12=pkcs12, chain=chain, passphrase=passphrase,
                    passphrase_env=passphrase_env, signer=signer, field=field, page=page, rect=rect, visible=visible,
                    image=image, name=name, reason=reason, location=location, contact=contact, level=level,
                    tsa_url=tsa_url, timestamper=timestamper, trust_roots=trust_roots, other_certs=other_certs,
                    crls=crls, ocsps=ocsps, allow_fetching=allow_fetching, md_algorithm=md_algorithm,
                    certify_perm=docmdp, out=out, password=password, options=options, quiet=quiet)


# ====================================================================== timestamp
def timestamp(src: str | Path, tsa_url: str | None = None, *, timestamper=None, field: str | None = None,
              embed_validation: bool = False, trust_roots: Any = None, other_certs: Iterable[Any] = (),
              crls: Iterable[Any] = (), ocsps: Iterable[Any] = (), allow_fetching: bool = True,
              md_algorithm: str = "sha256", out: str | Path | None = None, password: str | None = None,
              options: GateOptions | None = None, quiet: bool = False) -> OpResult:
    """Add a document timestamp (/DocTimeStamp, RFC 3161) as an incremental update.

    With *embed_validation*, the TSA's chain and revocation info go into the DSS first.
    """
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    ts = _timestamper(tsa_url, timestamper)
    if ts is None:
        raise ValueError("a document timestamp needs a time-stamping authority: pass --tsa URL")
    data = _read_input(src)
    w = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
    if w.prev.encrypted:
        if not password:
            raise ValueError(f"{src.name} is encrypted: pass --password")
        w.encrypt(password)
    vc = None
    if embed_validation:
        vc = _validation_context(trust_roots, other_certs=other_certs, crls=crls, ocsps=ocsps,
                                 allow_fetching=allow_fetching)
    dst = output_path(src, "timestamped", out=out)
    buf = io.BytesIO()
    try:
        signers.PdfTimeStamper(ts, field_name=field).timestamp_pdf(w, md_algorithm, validation_context=vc, output=buf)
    except Exception as exc:
        raise ValueError(f"timestamping failed: {exc or exc.__class__.__name__}") from exc
    out_bytes = buf.getvalue()
    if not out_bytes.startswith(data):
        raise RuntimeError("internal error: timestamped output is not an incremental update of the input")
    dst.write_bytes(out_bytes)
    opts = options if options is not None else GateOptions(standard=_declared_pdfa(src, password))
    return finalize(dst, src, options=opts, password=password,
                    data={"tsa": tsa_url or type(ts).__name__, "incremental": True}, quiet=quiet)


# ====================================================================== verify
@dataclass
class SignatureReport:
    field: str
    kind: str  # approval | certification | timestamp
    signer: str
    subfilter: str
    intact: bool
    valid: bool
    trusted: bool
    revoked: bool
    trust_problem: str | None
    signing_time: str | None
    timestamp: dict[str, Any] | None
    coverage: str
    covers_whole_file: bool
    modification_level: str | None
    changes_after: str
    docmdp: str | None
    docmdp_ok: bool | None
    pades_level: str
    ltv: bool
    ltv_reason: str
    revision: int | None  # 1-based revision the signature covers
    revisions_total: int | None
    verdict: str
    ok: bool
    errors: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def lines(self) -> list[str]:
        head = f"{self.field} ({self.kind}, {self.pades_level}): {self.verdict}"
        rows = [head]
        if self.kind == "timestamp":
            bits = [f"TSA: {self.signer}", f"time {self.signing_time}"]
        else:
            bits = [f"signer: {self.signer}"]
            if self.signing_time:
                bits.append(f"signed {self.signing_time}")
        if self.timestamp and self.kind != "timestamp":
            t = self.timestamp
            bits.append(f"timestamp {t.get('time')} by {t.get('tsa')} ({'trusted' if t.get('trusted') else 'untrusted'})")
        rows.append("  " + " · ".join(bits))
        cov = ("entire document" if self.covers_whole_file
               else f"revision {self.revision} of {self.revisions_total}; later: {self.changes_after}")
        extra = f" · DocMDP: {self.docmdp}" if self.docmdp else ""
        rows.append(f"  covers: {cov} · LTV: {'yes' if self.ltv else 'no (' + self.ltv_reason + ')'}{extra}")
        if not self.trusted and self.trust_problem:
            rows.append(f"  trust: {self.trust_problem}")
        for e in self.errors:
            rows.append(f"  error: {e}")
        return rows


_TRUST_WHY = {
    "NO_CERTIFICATE_CHAIN_FOUND": "no chain to a trusted root (self-signed or unknown CA; pass --trust-root)",
    "CERTIFICATE_CHAIN_GENERAL_FAILURE": "certificate chain could not be validated",
    "REVOKED_NO_POE": "signer certificate revoked",
    "REVOKED_CA_NO_POE": "a CA certificate in the chain is revoked",
    "OUT_OF_BOUNDS_NO_POE": "certificate expired or not yet valid at validation time",
    "OUT_OF_BOUNDS_NOT_REVOKED": "certificate expired (not revoked)",
    "EXPIRED": "certificate expired",
    "NOT_YET_VALID": "certificate not yet valid",
    "TRY_LATER": "revocation status unavailable (try again, or verify with --online)",
    "REVOCATION_OUT_OF_BOUNDS_NO_POE": "revocation info is stale",
    "CHAIN_CONSTRAINTS_FAILURE": "certificate chain violates constraints (key usage, policy)",
    "SIG_CONSTRAINTS_FAILURE": "signer certificate not allowed for signing (key usage)",
    "CRYPTO_CONSTRAINTS_FAILURE": "weak algorithm",
}

_CHANGES = {
    "NONE": "no changes",
    "LTA_UPDATES": "signature maintenance only (validation data / timestamps)",
    "FORM_FILLING": "form filling or further signatures",
    "ANNOTATIONS": "comments / annotations added",
    "OTHER": "content changed after signing",
}


def _fmt_dt(d: _dt.datetime | None) -> str | None:
    if d is None:
        return None
    return d.isoformat(timespec="seconds")


def _ltv_check(sig, trust_roots, other_certs, dss, moment: _dt.datetime | None) -> tuple[bool, str]:
    """Can the signature be validated using only what is embedded (hard-fail revocation)?"""
    from pyhanko.sign.validation import validate_pdf_signature, validate_pdf_timestamp

    if dss is None:
        return False, "no DSS (document security store)"
    if not (dss.crls or dss.ocsps):
        return False, "DSS has no revocation info"
    try:
        extra: dict[str, Any] = {}
        if moment is not None:
            extra.update(moment=moment, retroactive_revinfo=True)
        vc = _validation_context(trust_roots, other_certs=other_certs, allow_fetching=False,
                                 revocation_mode="hard-fail", dss=dss, **extra)
        if sig.sig_object_type == "/DocTimeStamp":
            st = validate_pdf_timestamp(sig, vc, skip_diff=True)
        else:
            st = validate_pdf_signature(sig, vc, skip_diff=True)
    except Exception as exc:
        return False, f"offline validation failed: {exc}"[:160]
    if not st.trusted:
        why = st.trust_problem_indic.name if st.trust_problem_indic else "untrusted"
        return False, _TRUST_WHY.get(why, why.lower().replace("_", " "))
    return True, "revocation info embedded"


def verify(src: str | Path, *, trust_roots: Any = "system", other_certs: Iterable[Any] = (),
           crls: Iterable[Any] = (), ocsps: Iterable[Any] = (), online: bool = False, require_trust: bool = False,
           password: str | None = None, as_json: bool = False, quiet: bool = False) -> OpResult:
    """Validate every signature and document timestamp in *src*.

    Per signature: field, signer, signing time, integrity, coverage / changes after signing
    (pyHanko difference analysis), trust chain (and why not), timestamp, detected PAdES level and
    LTV status. ``res.ok`` is False if any signature is broken or the document was modified in a
    disallowed way (with *require_trust*, also if any chain is untrusted). Offline by default:
    revocation uses embedded info plus *crls*/*ocsps*; *online* allows fetching.
    """
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import DocumentSecurityStore, validate_pdf_signature, validate_pdf_timestamp
    from pyhanko.sign.validation.errors import NoDSSFoundError

    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    r = PdfFileReader(io.BytesIO(src.read_bytes()), strict=False)
    if r.encrypted:
        if not password:
            raise ValueError(f"{src.name} is encrypted: pass --password")
        r.decrypt(password)
    try:
        dss = DocumentSecurityStore.read_dss(r)
    except NoDSSFoundError:
        dss = None
    except Exception:
        dss = None
    sigs = list(r.embedded_signatures)
    total_revs = r.xrefs.total_revisions
    doc_ts_revs = [s.signed_revision for s in sigs if s.sig_object_type == "/DocTimeStamp"]
    reports: list[SignatureReport] = []
    for sig in sigs:
        errors: list[str] = []
        is_ts = sig.sig_object_type == "/DocTimeStamp"
        subf = str(sig.sig_object.get("/SubFilter", ""))
        docmdp = sig.docmdp_level
        kind = "timestamp" if is_ts else ("certification" if docmdp is not None and _is_certification(sig) else "approval")
        try:
            vc = _validation_context(trust_roots, other_certs=other_certs, crls=crls, ocsps=ocsps,
                                     allow_fetching=online, dss=dss)
        except Exception as exc:  # e.g. system trust store unavailable
            errors.append(f"trust store: {exc}")
            vc = _validation_context([], other_certs=other_certs, crls=crls, ocsps=ocsps, allow_fetching=online, dss=dss)
        try:
            st = validate_pdf_timestamp(sig, vc) if is_ts else validate_pdf_signature(sig, vc)
        except Exception as exc:
            reports.append(SignatureReport(
                field=str(sig.field_name), kind=kind, signer="?", subfilter=subf, intact=False, valid=False,
                trusted=False, revoked=False, trust_problem=None, signing_time=None, timestamp=None,
                coverage="UNCLEAR", covers_whole_file=False, modification_level=None, changes_after="unknown",
                docmdp=None, docmdp_ok=None, pades_level="unknown", ltv=False, ltv_reason="validation error",
                revision=None, revisions_total=total_revs, verdict=f"could not be validated: {exc}", ok=False, errors=[str(exc)]))
            continue
        signer_cn = _cn(st.signing_cert)
        tp = st.trust_problem_indic.name if st.trust_problem_indic is not None else None
        trust_problem = _TRUST_WHY.get(tp, tp.lower().replace("_", " ")) if tp else None
        if not st.trusted and not trust_problem:
            trust_problem = "chain not trusted"
        tsinfo = None
        moment = None
        if is_ts:
            moment = st.timestamp
            tsinfo = {"time": _fmt_dt(st.timestamp), "tsa": signer_cn, "trusted": bool(st.trusted)}
            signing_time = _fmt_dt(st.timestamp)
        else:
            signing_time = _fmt_dt(st.signer_reported_dt)
            tv = st.timestamp_validity
            if tv is not None:
                moment = tv.timestamp
                tsinfo = {"time": _fmt_dt(tv.timestamp), "tsa": _cn(tv.signing_cert), "trusted": bool(tv.trusted),
                          "intact": bool(tv.intact and tv.valid)}
        cov = st.coverage.name if st.coverage is not None else "UNCLEAR"
        whole = cov == "ENTIRE_FILE"
        ml = st.modification_level.name if st.modification_level is not None else None
        diff = st.diff_result
        changes = "none" if whole else _CHANGES.get(ml or "", "unknown")
        if diff is not None and diff.__class__.__name__ == "SuspiciousModification":
            changes = f"suspicious modification: {diff}"
            errors.append(str(diff))
        elif diff is not None and getattr(diff, "changed_form_fields", None):
            changes += f" (fields: {', '.join(sorted(diff.changed_form_fields))})"
        ltv, ltv_reason = _ltv_check(sig, trust_roots, other_certs, dss, moment)
        # PAdES level
        if is_ts:
            level = "document timestamp"
        elif subf == "/ETSI.CAdES.detached":
            level = "PAdES B-B"
            if tsinfo is not None:
                level = "PAdES B-T"
                # structural: revocation info in the DSS (whether it validates is reported by `ltv`)
                if ltv or (dss is not None and (dss.crls or dss.ocsps)):
                    level = "PAdES B-LT"
                    later_ts = [rv for rv in doc_ts_revs if rv > sig.signed_revision]
                    if later_ts:
                        level = "PAdES B-LTA"
        else:
            level = f"CMS {subf.lstrip('/')}" + (" + timestamp" if tsinfo else "")
        intact = bool(st.intact and st.valid)
        disallowed = st.docmdp_ok is False or ml == "OTHER" or bool(errors and "suspicious" in changes)
        revoked = bool(getattr(st, "revoked", False))
        if not intact:
            verdict, ok = "BROKEN: signature does not match the signed bytes", False
        elif disallowed:
            verdict, ok = "modified after signing", False
        elif revoked:
            verdict, ok = "valid but certificate revoked", False
        elif st.trusted:
            verdict, ok = "valid and trusted", True
        else:
            verdict, ok = "valid but untrusted root", not require_trust
        reports.append(SignatureReport(
            field=str(sig.field_name), kind=kind, signer=signer_cn, subfilter=subf, intact=intact,
            valid=bool(st.valid), trusted=bool(st.trusted), revoked=revoked, trust_problem=trust_problem,
            signing_time=signing_time, timestamp=tsinfo, coverage=cov, covers_whole_file=whole,
            modification_level=ml, changes_after=changes,
            docmdp=_mdp_name(docmdp) if kind == "certification" else None,
            docmdp_ok=st.docmdp_ok, pades_level=level, ltv=ltv, ltv_reason=ltv_reason,
            revision=sig.signed_revision + 1, revisions_total=total_revs, verdict=verdict, ok=ok, errors=errors))

    rep = GateReport(src)
    if not reports:
        rep.failures.append(Finding("signature", "no signatures found", None, "sign it first (pdfcore sign)"))
    for sr in reports:
        if not sr.ok:
            rep.failures.append(Finding("signature", f"{sr.field}: {sr.verdict}", None,
                                        "re-sign the final version" if "modified" in sr.verdict else ""))
        elif not sr.trusted:
            rep.warnings.append(Finding("signature", f"{sr.field}: {sr.verdict}", None, "", "warn"))
    data = {"file": str(src), "revisions": total_revs, "signatures": [s.to_dict() for s in reports],
            "dss": ({"certs": len(dss.certs), "crls": len(dss.crls), "ocsps": len(dss.ocsps)} if dss else None),
            "all_intact": bool(reports) and all(s.intact for s in reports), "ok": rep.passed}
    if not quiet:
        if as_json:
            print(json.dumps(data, indent=2, default=str))
        else:
            print(f"{src}: {len(reports)} signature(s), {total_revs} revision(s)")
            for sr in reports:
                for ln in sr.lines():
                    print("  " + ln)
            print(f"{src}  {rep.line()}")
    return OpResult(None, rep, [], data)


def _is_certification(sig) -> bool:
    try:
        for ref in sig.sig_object.get("/Reference", []):
            if str(ref.get_object().get("/TransformMethod", "")) == "/DocMDP":
                return True
    except Exception:
        pass
    return False


def _mdp_name(perm) -> str | None:
    if perm is None:
        return None
    inv = {v: k for k, v in DOCMDP.items()}
    return inv.get(int(perm.value), str(perm))


# ====================================================================== CLI
def _rect_arg(s: str | None) -> Rect | None:
    if not s:
        return None
    import re

    v = [float(x) for x in re.split(r"[,\s]+", s.strip()) if x]
    if len(v) != 4:
        raise ValueError("--rect needs x0,top,x1,bottom (points, top-left origin)")
    return Rect(*v)


def _identity_args(p) -> None:
    p.add_argument("--key", help="private key (PEM/DER)")
    p.add_argument("--cert", help="signer certificate (PEM/DER)")
    p.add_argument("--chain", action="append", default=[], help="intermediate CA certificate(s) to embed")
    p.add_argument("--pkcs12", "--p12", dest="pkcs12", help="PKCS#12 bundle (.p12/.pfx) instead of --key/--cert")
    p.add_argument("--passphrase-env", default=PASSPHRASE_ENV, metavar="VAR",
                   help=f"environment variable holding the key passphrase (default {PASSPHRASE_ENV})")
    p.add_argument("--ask-passphrase", action="store_true", help="prompt for the key passphrase (not echoed)")
    p.add_argument("--field", help="signature field to fill or create")
    p.add_argument("--page", help="page for a new visible field (1-based; default last)")
    p.add_argument("--rect", help="visible box x0,top,x1,bottom in points from the top-left")
    vis = p.add_mutually_exclusive_group()
    vis.add_argument("--visible", dest="visible", action="store_true", default=None, help="visible box (default when --rect)")
    vis.add_argument("--invisible", dest="visible", action="store_false", help="no visible appearance")
    p.add_argument("--image", help="image (PNG/JPEG) drawn at the left of the visible box")
    p.add_argument("--name", help="signer name shown in the box (default: certificate CN)")
    p.add_argument("--reason")
    p.add_argument("--location")
    p.add_argument("--contact")
    p.add_argument("--level", default="b-b", choices=list(LEVELS), help="PAdES level (default b-b)")
    p.add_argument("--tsa", help="time-stamping authority URL (needed for b-t and up)")
    p.add_argument("--trust-root", action="append", default=[], help="trust anchor for b-lt/b-lta validation")
    p.add_argument("--crl", action="append", default=[], help="CRL file to embed / use (b-lt)")
    p.add_argument("--ocsp", action="append", default=[], help="OCSP response file to embed / use (b-lt)")
    p.add_argument("--offline", action="store_true", help="b-lt: do not fetch revocation info from the network")
    add_common(p)


def _identity_kw(ns) -> dict[str, Any]:
    pw = None
    if ns.ask_passphrase:
        import getpass

        pw = getpass.getpass("Key passphrase: ")
    page = None
    if ns.page:
        import pikepdf

        with pikepdf.open(ns.pdf, password=ns.password or "") as pdf:
            page = parse_pages(ns.page, len(pdf.pages))[0]
    return dict(key=ns.key, cert=ns.cert, pkcs12=ns.pkcs12, chain=ns.chain, passphrase=pw,
                passphrase_env=ns.passphrase_env, field=ns.field, page=page, rect=_rect_arg(ns.rect),
                visible=ns.visible, image=ns.image, name=ns.name, reason=ns.reason, location=ns.location,
                contact=ns.contact, level=ns.level, tsa_url=ns.tsa, trust_roots=ns.trust_root or None,
                crls=ns.crl, ocsps=ns.ocsp, allow_fetching=not ns.offline, out=ns.out, password=ns.password,
                options=_gate_opts(ns))


def _gate_opts(ns) -> GateOptions | None:
    if any(getattr(ns, k, None) for k in ("gate_profile", "size_budget", "png_dir")):
        return gate_options_from(ns, standard=_declared_pdfa(Path(ns.pdf), ns.password))
    return None


@command("sign", "Digitally sign a PDF (PAdES B-B … B-LTA, visible or invisible)", group="standards")
def _cli_sign(p):
    p.add_argument("pdf")
    _identity_args(p)
    return lambda ns: sign(ns.pdf, **_identity_kw(ns))


@command("certify", "Certification signature with DocMDP permissions (no-changes, form-fill, annotate)", group="standards")
def _cli_certify(p):
    p.add_argument("pdf")
    p.add_argument("--docmdp", "--allow", dest="docmdp", default="form-fill",
                   choices=["no-changes", "form-fill", "annotate"], help="changes allowed after certifying")
    _identity_args(p)
    return lambda ns: certify(ns.pdf, docmdp=ns.docmdp, **_identity_kw(ns))


@command("verify", "Verify signatures: intact? trusted? whole document covered? PAdES level, LTV", group="standards")
def _cli_verify(p):
    p.add_argument("pdf")
    p.add_argument("--trust-root", action="append", default=[], help="trust anchor certificate (default: system store)")
    p.add_argument("--cert", dest="other_certs", action="append", default=[], help="extra intermediate certificates")
    p.add_argument("--crl", action="append", default=[])
    p.add_argument("--ocsp", action="append", default=[])
    p.add_argument("--online", action="store_true", help="fetch revocation info (CRL/OCSP) from the network")
    p.add_argument("--require-trust", action="store_true", help="exit 1 if any chain is untrusted")
    p.add_argument("--json", action="store_true")
    p.add_argument("--password", help="password to open the input")
    return lambda ns: verify(ns.pdf, trust_roots=ns.trust_root or "system", other_certs=ns.other_certs, crls=ns.crl,
                             ocsps=ns.ocsp, online=ns.online, require_trust=ns.require_trust, password=ns.password,
                             as_json=ns.json)


@command("timestamp", "Add an RFC 3161 document timestamp (archive timestamp for B-LTA refresh)", group="standards")
def _cli_timestamp(p):
    p.add_argument("pdf")
    p.add_argument("--tsa", required=True, help="time-stamping authority URL")
    p.add_argument("--field")
    p.add_argument("--embed-validation", action="store_true", help="embed the TSA chain/revocation info first")
    p.add_argument("--trust-root", action="append", default=[])
    add_common(p)
    return lambda ns: timestamp(ns.pdf, ns.tsa, field=ns.field, embed_validation=ns.embed_validation,
                                trust_roots=ns.trust_root or None, out=ns.out, password=ns.password,
                                options=_gate_opts(ns))
