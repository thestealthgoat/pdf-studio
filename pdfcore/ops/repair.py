"""Repair damaged PDFs: rebuild the cross-reference table, drop broken objects, rewrite cleanly.

Strategy (first that yields a file every engine can open wins):
1. qpdf via pikepdf — reconstructs broken/missing xref tables and streams; full rewrite.
2. pypdf in non-strict mode — re-reads objects one by one and writes a fresh file.
3. PDFium via pypdfium2 — loads what it can and saves a new document.
4. Salvage — for truncated files without a trailer: keep every complete object, rebuild the page tree,
   drop pages whose content is lost (reported in ``data["pages"]``).

The repaired file must open in both pikepdf and PDFium and pass the gate's render check.
"""

from __future__ import annotations

import io
from pathlib import Path

from pdfcore.gate import GateOptions
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, output_path
from pdfcore.result import OpResult


def _opens_everywhere(data: bytes, password: str | None) -> tuple[bool, int, str]:
    import pikepdf
    import pypdfium2 as pdfium

    try:
        with pikepdf.open(io.BytesIO(data), password=password or "") as p:
            n = len(p.pages)
        doc = pdfium.PdfDocument(data, password=password)
        try:
            m = len(doc)
            for i in range(m):
                doc[i].render(scale=0.1)  # every page must rasterize
        finally:
            doc.close()
        if n != m:
            return False, n, f"page count differs (qpdf {n}, pdfium {m})"
        return n > 0, n, ""
    except Exception as exc:
        return False, 0, str(exc)[:160]


def _via_qpdf(src: Path, password: str | None) -> bytes:
    import pikepdf

    buf = io.BytesIO()
    with pikepdf.open(str(src), password=password or "", attempt_recovery=True) as pdf:
        # A plain full rewrite: qpdf has already rebuilt the xref while opening. (Extra save options
        # such as object streams can fail on objects that recovery could only partly reconstruct.)
        pdf.save(buf)
    return buf.getvalue()


def _via_pypdf(src: Path, password: str | None) -> bytes:
    from pypdf import PdfReader, PdfWriter

    r = PdfReader(str(src), strict=False)
    if r.is_encrypted:
        r.decrypt(password or "")
    w = PdfWriter()
    for page in r.pages:
        try:
            w.add_page(page)
        except Exception:
            continue
    if r.metadata:
        w.add_metadata({k: str(v) for k, v in r.metadata.items() if isinstance(k, str)})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _via_pdfium(src: Path, password: str | None) -> bytes:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(src), password=password)
    try:
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    finally:
        doc.close()


def _via_salvage(src: Path, password: str | None) -> bytes:
    """Last resort for truncated files with no usable trailer: keep every complete `N G obj … endobj`,
    then build a fresh page tree + catalog over the page objects found. Pages whose content is lost are dropped."""
    import re

    import pikepdf

    raw = src.read_bytes()
    objs: dict[int, bytes] = {}
    for m in re.finditer(rb"(?s)(?<![0-9])(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", raw):
        objs[int(m.group(1))] = m.group(3)
    if not objs:
        raise ValueError("no complete objects found")
    page_ids = [n for n, body in sorted(objs.items()) if re.search(rb"/Type\s*/Page(?![s\w])", body)]
    if not page_ids:
        raise ValueError("no page objects found")
    top = max(objs) + 1
    pages_id, cat_id = top, top + 1
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    for n, body in sorted(objs.items()):
        if n in page_ids:  # point every page at the new page tree
            body = re.sub(rb"/Parent\s+\d+\s+\d+\s+R", b"/Parent %d 0 R" % pages_id, body)
        out.write(b"%d 0 obj%send obj\n".replace(b"end obj", b"endobj") % (n, body))
    kids = b" ".join(b"%d 0 R" % n for n in page_ids)
    out.write(b"%d 0 obj\n<< /Type /Pages /Kids [%s] /Count %d >>\nendobj\n" % (pages_id, kids, len(page_ids)))
    out.write(b"%d 0 obj\n<< /Type /Catalog /Pages %d 0 R >>\nendobj\n" % (cat_id, pages_id))
    out.write(b"trailer\n<< /Root %d 0 R /Size %d >>\n%%%%EOF\n" % (cat_id, cat_id + 1))
    buf = io.BytesIO()
    with pikepdf.open(io.BytesIO(out.getvalue()), password=password or "", attempt_recovery=True) as pdf:
        keep = pikepdf.new()
        for page in pdf.pages:  # drop pages whose content streams were lost in the truncation
            try:
                c = page.obj.get("/Contents")
                streams = list(c) if isinstance(c, pikepdf.Array) else [c]
                if not streams or any(not isinstance(x, pikepdf.Stream) for x in streams):
                    continue
                if sum(len(x.read_bytes()) for x in streams) == 0:
                    continue
                keep.pages.append(page)
            except Exception:
                continue
        if not keep.pages:
            raise ValueError("no recoverable pages")
        keep.save(buf)
    return buf.getvalue()


def _drop_lost_pages(data: bytes, password: str | None) -> tuple[bytes, list[int]]:
    """Remove pages whose content streams are missing or empty (lost to damage); return 1-based numbers dropped."""
    import pikepdf

    with pikepdf.open(io.BytesIO(data), password=password or "") as pdf:
        lost = []
        for i, page in enumerate(pdf.pages):
            c = page.obj.get("/Contents")
            streams = list(c) if isinstance(c, pikepdf.Array) else ([c] if c is not None else [])
            try:
                empty = (not streams or any(not isinstance(x, pikepdf.Stream) for x in streams)
                         or sum(len(x.read_bytes()) for x in streams) == 0)
            except Exception:
                empty = True
            if empty and not page.obj.get("/Annots"):
                lost.append(i)
        if not lost or len(lost) == len(pdf.pages):
            return data, []
        for i in reversed(lost):
            del pdf.pages[i]
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue(), [i + 1 for i in lost]


def repair(src: str | Path, *, out: str | Path | None = None, password: str | None = None,
           options: GateOptions | None = None, quiet: bool = False) -> OpResult:
    src = Path(src)
    attempts: list[dict] = []
    for name, fn in (("qpdf", _via_qpdf), ("pypdf", _via_pypdf), ("pdfium", _via_pdfium), ("salvage", _via_salvage)):
        try:
            data = fn(src, password)
        except Exception as exc:
            attempts.append({"engine": name, "ok": False, "error": str(exc)[:160]})
            continue
        ok, pages, why = _opens_everywhere(data, password)
        attempts.append({"engine": name, "ok": ok, "pages": pages, "error": why})
        if ok:
            data, lost = _drop_lost_pages(data, password)
            dst = output_path(src, "repaired", out=out)
            dst.write_bytes(data)
            if lost and not quiet:
                print(f"note: page(s) {lost} had no recoverable content and were removed")
            return finalize(dst, None, options=options, password=password, quiet=quiet,
                            data={"engine": name, "pages": pages - len(lost), "lost_pages": lost, "attempts": attempts})
    msg = "; ".join(f"{a['engine']}: {a.get('error') or 'unusable'}" for a in attempts)
    raise ValueError(f"could not repair {src.name} — {msg}")


@command("repair", "Repair a damaged PDF (rebuild xref, drop broken objects, clean rewrite)", group="organize")
def _cli(p):
    p.add_argument("pdf")
    add_common(p)
    return lambda ns: repair(ns.pdf, out=ns.out, password=ns.password)
