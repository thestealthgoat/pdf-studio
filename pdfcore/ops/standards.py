"""Standards: veraPDF preflight reports, PDF/A conversion of existing files, accessibility checks.

``preflight``  run veraPDF (machine-readable JSON), list every failed rule → report PDF + JSON.
``to_pdfa``    best-effort PDF/A-1b/2b/2u/3b/3u conversion with pikepdf, then validated by veraPDF;
               ``ok`` only when veraPDF passes. Never claims conformance it cannot prove.
``a11y``       accessibility check (tags, language, title, headings, alt text, tables, reading
               order, links, form tooltips, scanned pages, text contrast) merged with veraPDF PDF/UA-1.

veraPDF is an external program (``verapdf`` on PATH). PDF/X needs Ghostscript and is available in
the local edition only.

The sRGB output-intent profile is generated here (ICC v2 matrix/TRC profile built from the
published sRGB primaries and transfer curve), so no third-party profile file is shipped.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape

import pikepdf
from pikepdf import Array, Dictionary, Name

from pdfcore.gate import GateOptions
from pdfcore.gate.checks import Finding
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path
from pdfcore.result import OpResult

__all__ = [
    "FLAVOURS", "PDFX_ERROR", "run_verapdf", "declared_standard", "preflight", "to_pdfa", "a11y", "srgb_icc",
    "write_report_pdf",
]

FLAVOURS: dict[str, str] = {
    "pdfa-1a": "1a", "pdfa-1b": "1b", "pdfa-2a": "2a", "pdfa-2b": "2b", "pdfa-2u": "2u", "pdfa-3a": "3a",
    "pdfa-3b": "3b", "pdfa-3u": "3u", "pdfa-4": "4", "pdfa-4e": "4e", "pdfa-4f": "4f", "pdfua-1": "ua1",
    "pdfua-2": "ua2", "wtpdf-1-0-reuse": "wt1r", "wtpdf-1-0-accessibility": "wt1a",
}
PDFX_ERROR = "PDF/X requires Ghostscript — available in the local edition only"


def _norm_standard(standard: str) -> str:
    s = standard.strip().lower().replace("_", "-").replace("/", "").replace(" ", "")
    if s.startswith("pdfx") or s.startswith("x-") or s == "x":
        raise ValueError(PDFX_ERROR)
    if re.fullmatch(r"[1-4][abu]?|[1-4][ef]", s):
        s = f"pdfa-{s}"
    elif re.fullmatch(r"a-?[1-4][abu]?", s):
        s = "pdfa-" + s.lstrip("a-")
    elif re.fullmatch(r"pdfa[1-4][abu]?", s):
        s = "pdfa-" + s[4:]
    elif re.fullmatch(r"(pdf)?ua-?[12]", s):
        s = "pdfua-" + s[-1]
    if s not in FLAVOURS:
        raise ValueError(f"unknown standard {standard!r}; use one of {', '.join(sorted(FLAVOURS))}")
    return s


# ====================================================================== veraPDF
@dataclass
class RuleFailure:
    specification: str
    clause: str
    test: int
    description: str
    object: str
    failed_checks: int
    samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rule_id(self) -> str:
        return f"{self.clause}-{self.test}"

    @property
    def pages(self) -> list[int]:
        return sorted({s["page"] for s in self.samples if s.get("page")})

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["rule"] = self.rule_id
        d["pages"] = self.pages
        return d


@dataclass
class VeraResult:
    standard: str
    flavour: str
    compliant: bool
    profile: str = ""
    passed_rules: int = 0
    failed_rules: int = 0
    passed_checks: int = 0
    failed_checks: int = 0
    rules: list[RuleFailure] = field(default_factory=list)
    error: str | None = None
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "rules"}
        d["rules"] = [r.to_dict() for r in self.rules]
        return d

    def summary(self) -> str:
        if self.error:
            return f"{self.standard}: veraPDF could not validate ({self.error})"
        if self.compliant:
            return f"{self.standard}: PASS ({self.passed_rules} rules, veraPDF {self.version})".replace(" veraPDF )", ")")
        first = self.rules[0] if self.rules else None
        head = f"{self.standard}: FAIL — {self.failed_rules} rule(s), {self.failed_checks} check(s)"
        if first:
            head += f"; first {first.rule_id}: {first.description[:90]}"
        return head


_PAGE_RE = re.compile(r"pages\[(\d+)\]")


def _page_from_context(ctx: str) -> int | None:
    m = _PAGE_RE.search(ctx or "")
    return int(m.group(1)) + 1 if m else None


def run_verapdf(path: str | Path, standard: str = "pdfa-2b", *, max_samples: int = 5, password: str | None = None,
                timeout: float = 600) -> VeraResult:
    """Validate with the veraPDF CLI (``--format json``) and parse every failed rule."""
    std = _norm_standard(standard)
    flav = FLAVOURS[std]
    exe = shutil.which("verapdf")
    if not exe:
        return VeraResult(std, flav, False, error="veraPDF not installed (run pdfcore install)")
    cmd = [exe, "--flavour", flav, "--format", "json", "--maxfailuresdisplayed", str(max(1, max_samples))]
    if password:
        cmd += ["--password", password]
    cmd.append(str(path))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return VeraResult(std, flav, False, error=f"timed out after {timeout:g}s")
    out = proc.stdout
    start = out.find("{")
    try:
        doc = json.loads(out[start:]) if start >= 0 else None
    except json.JSONDecodeError:
        doc = None
    if not doc:
        err = (proc.stderr or out).strip().splitlines()
        return VeraResult(std, flav, False, error=(err[-1] if err else "no output")[:200])
    rep = doc.get("report", doc)
    version = ""
    for rd in rep.get("buildInformation", {}).get("releaseDetails", []):
        if rd.get("id") == "apps":
            version = rd.get("version", "")
    jobs = rep.get("jobs") or []
    if not jobs:
        return VeraResult(std, flav, False, error="no validation job in report", version=version)
    job = jobs[0]
    vrs = job.get("validationResult")
    if isinstance(vrs, dict):
        vrs = [vrs]
    if not vrs:
        exc = job.get("taskException") or job.get("taskResult") or {}
        msg = exc.get("exceptionMessage") if isinstance(exc, dict) else str(exc)
        return VeraResult(std, flav, False, error=(msg or "validation did not run (encrypted or unreadable file?)")[:200],
                          version=version)
    vr = vrs[0]
    det = vr.get("details", {})
    res = VeraResult(std, flav, bool(vr.get("compliant")), profile=vr.get("profileName", ""),
                     passed_rules=int(det.get("passedRules", 0)), failed_rules=int(det.get("failedRules", 0)),
                     passed_checks=int(det.get("passedChecks", 0)), failed_checks=int(det.get("failedChecks", 0)),
                     version=version)
    for rs in det.get("ruleSummaries", []) or []:
        if str(rs.get("status", "")).lower() not in ("failed",) and rs.get("ruleStatus") != "FAILED":
            continue
        samples = []
        for ch in (rs.get("checks") or [])[:max_samples]:
            ctx = ch.get("context", "")
            samples.append({"context": ctx, "message": ch.get("errorMessage", ""), "page": _page_from_context(ctx)})
        res.rules.append(RuleFailure(rs.get("specification", ""), str(rs.get("clause", "")), int(rs.get("testNumber", 0)),
                                     rs.get("description", ""), rs.get("object", ""), int(rs.get("failedChecks", 0)),
                                     samples))
    return res


def declared_standard(path: str | Path, *, password: str | None = None) -> str | None:
    """Standard the file claims in its XMP (``pdfa-2b``, ``pdfua-1`` …), or None."""
    try:
        with pikepdf.open(str(path), password=password or "") as pdf:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as m:
                part = m.get("pdfaid:part")
                conf = (m.get("pdfaid:conformance") or "").lower()
                ua = m.get("pdfuaid:part")
    except Exception:
        return None
    if part:
        s = f"pdfa-{part}{conf}" if str(part) != "4" else "pdfa-4"
        return s if s in FLAVOURS else None
    if ua:
        return f"pdfua-{ua}"
    return None


# ====================================================================== report PDF
def write_report_pdf(path: Path, title: str, subtitle: str, summary: str,
                     sections: Sequence[tuple[str, Sequence[Any]]]) -> Path:
    """Simple report in the house font. Section items: str (paragraph) or list (table row; first row = header)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from pdfcore.fonts import register_reportlab

    f = register_reportlab()
    body = ParagraphStyle("body", fontName=f["sans"], fontSize=9.5, leading=12.5, textColor=colors.HexColor("#1b2129"))
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10)
    h1 = ParagraphStyle("h1", parent=body, fontName=f["sans-bold"], fontSize=17, leading=21, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=body, fontName=f["sans-semibold"], fontSize=11.5, leading=15, spaceBefore=10,
                        spaceAfter=4)
    sub = ParagraphStyle("sub", parent=body, textColor=colors.HexColor("#5a6470"))
    story: list[Any] = [Paragraph(escape(title), h1), Paragraph(escape(subtitle), sub), Spacer(1, 8),
                        Paragraph(f"<b>{escape(summary)}</b>", body)]
    width = letter[0] - 2 * inch
    for heading, items in sections:
        story.append(Paragraph(escape(heading), h2))
        rows = [it for it in items if isinstance(it, (list, tuple))]
        for it in items:
            if isinstance(it, str):
                story.append(Paragraph(escape(it), body))
        if rows:
            ncol = max(len(r) for r in rows)
            data = [[Paragraph(escape(str(c)), small) for c in list(r) + [""] * (ncol - len(r))] for r in rows]
            widths = _col_widths(rows, ncol, width)
            t = Table(data, colWidths=widths, repeatRows=1)
            t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), f["sans-bold"]),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9edf1")),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9d0d8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(t)
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=inch, rightMargin=inch, topMargin=inch,
                            bottomMargin=inch, title=title, author="PDF Studio", subject=subtitle,
                            creator="pdfcore standards", lang="en-US")
    doc.build(story)
    return path


def _col_widths(rows: Sequence[Sequence[Any]], ncol: int, total: float) -> list[float]:
    lens = [max(4, min(60, max(len(str(r[i])) if i < len(r) else 0 for r in rows))) for i in range(ncol)]
    s = sum(lens)
    return [total * x / s for x in lens]


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return path


def _with_findings(res: OpResult, findings: Iterable[Finding], quiet: bool, label: Path | None = None) -> OpResult:
    """Add compliance findings to the op's gate report and print the one-line verdict."""
    if res.gate is not None:
        for f in findings:
            (res.gate.warnings if f.severity == "warn" else res.gate.failures).append(f)
    if not quiet:
        print(f"{label or res.output}  {res.line()}")
    return res


# ====================================================================== preflight
def preflight(src: str | Path, standard: str = "pdfa-2b", *, out: str | Path | None = None,
              json_out: str | Path | None = None, max_samples: int = 5, password: str | None = None,
              quiet: bool = False) -> OpResult:
    """veraPDF preflight → report PDF (``name.preflight.pdf``) + JSON. ``ok`` only if compliant.

    *standard* ``auto`` uses the standard the file declares (default pdfa-2b if none).
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    if standard.strip().lower() == "auto":
        standard = declared_standard(src, password=password) or "pdfa-2b"
    std = _norm_standard(standard)
    vr = run_verapdf(src, std, max_samples=max_samples, password=password)
    dst = output_path(src, "preflight", ".pdf", out=out)
    jdst = Path(json_out) if json_out else output_path(src, "preflight", ".json")
    data = {"file": str(src), **vr.to_dict(), "summary": vr.summary()}
    _write_json(jdst, data)
    rows: list[list[str]] = [["Rule", "Failed", "Pages", "Requirement", "Example"]]
    for r in vr.rules:
        ex = r.samples[0]["message"] if r.samples else ""
        rows.append([r.rule_id, str(r.failed_checks), ", ".join(map(str, r.pages)) or "—", r.description, ex])
    sections: list[tuple[str, Sequence[Any]]] = []
    if vr.error:
        sections.append(("Validator error", [vr.error]))
    if vr.rules:
        sections.append(("Failed rules", rows))
        ctx = [["Rule", "Page", "Object context"]]
        for r in vr.rules:
            for smp in r.samples[:3]:
                ctx.append([r.rule_id, str(smp.get("page") or "—"), smp.get("context", "")])
        sections.append(("Sample objects", ctx))
    elif vr.compliant:
        sections.append(("Result", [f"No rule failed: {vr.passed_rules} rules and {vr.passed_checks} checks passed."]))
    write_report_pdf(dst, f"Preflight: {src.name}", f"{vr.profile or std} · veraPDF {vr.version}".strip(" ·"),
                     vr.summary(), sections)
    res = finalize(dst, None, data=data, extra=[jdst], quiet=True)
    findings = []
    if not vr.compliant:
        first = vr.rules[0] if vr.rules else None
        msg = (f"not {std}: {vr.error}" if vr.error else
               f"not {std}: {len(vr.rules)} rule(s) fail; first {first.rule_id}" if first else f"not {std}")
        findings.append(Finding("standard", msg, (first.pages[0] - 1) if first and first.pages else None,
                                "see the preflight report" + ("; pdfcore pdfa converts to PDF/A" if std.startswith("pdfa") else "")))
    _with_findings(res, findings, True)
    if not quiet:
        print(f"{src.name}: {vr.summary()}")
        print(f"{dst}  {res.line()}")
    return res


# ====================================================================== sRGB ICC (v2)
def _s15(v: float) -> bytes:
    return struct.pack(">i", int(round(v * 65536)))


def _xyz(x: float, y: float, z: float) -> bytes:
    return b"XYZ " + b"\0" * 4 + _s15(x) + _s15(y) + _s15(z)


def srgb_icc() -> bytes:
    """A small ICC v2.1 display profile for sRGB (IEC 61966-2.1 primaries, D50-adapted; sRGB TRC).

    v2 so it is valid for PDF/A-1 (which needs ICC < 3) as well as PDF/A-2/3.
    """
    desc_txt = b"sRGB IEC61966-2.1 (PDF Studio)\0"
    desc = (b"desc" + b"\0" * 4 + struct.pack(">I", len(desc_txt)) + desc_txt + struct.pack(">II", 0, 0)
            + struct.pack(">HB", 0, 0) + b"\0" * 67)
    cprt = b"text" + b"\0" * 4 + b"No copyright, use freely\0"

    def eotf(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    n = 1024
    curv = b"curv" + b"\0" * 4 + struct.pack(">I", n) + b"".join(
        struct.pack(">H", int(round(eotf(i / (n - 1)) * 65535))) for i in range(n))
    tags = [(b"desc", desc), (b"cprt", cprt), (b"wtpt", _xyz(0.9642, 1.0, 0.8249)),
            (b"rXYZ", _xyz(0.4360747, 0.2225045, 0.0139322)), (b"gXYZ", _xyz(0.3850649, 0.7168786, 0.0971045)),
            (b"bXYZ", _xyz(0.1430804, 0.0606169, 0.7141733)), (b"rTRC", curv), (b"gTRC", curv), (b"bTRC", curv)]
    table_len = 4 + 12 * len(tags)
    base = 128 + table_len
    body = b""
    entries = b""
    placed: dict[int, tuple[int, int]] = {}
    for sig, blob in tags:
        if id(blob) not in placed:
            while (base + len(body)) % 4:
                body += b"\0"
            placed[id(blob)] = (base + len(body), len(blob))
            body += blob
        off, ln = placed[id(blob)]
        entries += sig + struct.pack(">II", off, ln)
    while len(body) % 4:
        body += b"\0"
    size = base + len(body)
    hdr = (struct.pack(">I", size) + b"\0" * 4 + struct.pack(">I", 0x02100000) + b"mntrRGB XYZ "
           + struct.pack(">6H", 2026, 1, 1, 0, 0, 0) + b"acsp" + b"\0" * 24 + struct.pack(">I", 0)
           + _s15(0.9642) + _s15(1.0) + _s15(0.8249) + b"\0" * 48)
    return hdr + struct.pack(">I", len(tags)) + entries + body


# ====================================================================== PDF/A conversion
_FORBIDDEN_ACTIONS = {"/Launch", "/Sound", "/Movie", "/ResetForm", "/ImportData", "/JavaScript", "/Hide",
                      "/SetOCGState", "/Rendition", "/Trans", "/GoTo3DView", "/SetState", "/NoOp", "/RichMediaExecute"}
_ALLOWED_NAMED = {"/NextPage", "/PrevPage", "/FirstPage", "/LastPage"}
_FORBIDDEN_ANNOTS = {
    1: {"/Sound", "/Movie", "/FileAttachment", "/Screen", "/3D", "/RichMedia", "/Watermark", "/PrinterMark",
        "/TrapNet", "/Redact", "/Projection"},
    2: {"/Sound", "/Movie", "/Screen", "/3D", "/RichMedia", "/FileAttachment", "/Projection"},
    3: {"/Sound", "/Movie", "/Screen", "/3D", "/RichMedia", "/Projection"},
}


def _parse_level(level: str) -> tuple[int, str]:
    s = level.strip().lower().replace("pdfa-", "").replace("pdf/a-", "").replace("-", "")
    if s.startswith("pdfx") or s.startswith("x"):
        raise ValueError(PDFX_ERROR)
    m = re.fullmatch(r"([123])([abu])", s)
    if not m:
        raise ValueError(f"unsupported PDF/A level {level!r}; use 1b, 2b, 2u, 3b or 3u")
    part, conf = int(m.group(1)), m.group(2)
    if conf == "a":
        raise ValueError(f"PDF/A-{part}a needs a fully tagged, accessible structure; convert to {part}b, or rebuild "
                         "the document from source with pdfcore make --standard pdfa-2a")
    if part == 1 and conf == "u":
        raise ValueError("PDF/A-1 has no 'u' level; use 1b or 2u")
    return part, conf


def _resources(pdf: pikepdf.Pdf, with_owner: bool = False) -> Iterable[Any]:
    """(page_index, resource dict) for pages, their form XObjects (recursive) and annotation appearances.

    With *with_owner*: (page_index, resource dict, content owner) where the owner is the page or stream
    whose content uses those resources.
    """
    seen: set[tuple[int, int]] = set()

    def forms(res, pi, owner, depth=0):
        if depth > 12 or not isinstance(res, Dictionary):
            return
        yield (pi, res, owner) if with_owner else (pi, res)
        xo = res.get("/XObject")
        if isinstance(xo, Dictionary):
            for _, x in xo.items():
                if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == "/Form":
                    key = x.objgen
                    if key != (0, 0) and key in seen:
                        continue
                    seen.add(key)
                    r = x.get("/Resources")
                    if isinstance(r, Dictionary):
                        yield from forms(r, pi, x, depth + 1)
        for kind in ("/Pattern",):
            pat = res.get(kind)
            if isinstance(pat, Dictionary):
                for _, pt in pat.items():
                    if isinstance(pt, pikepdf.Stream) and isinstance(pt.get("/Resources"), Dictionary):
                        if pt.objgen in seen:
                            continue
                        seen.add(pt.objgen)
                        yield from forms(pt.Resources, pi, None, depth + 1)

    for pi, page in enumerate(pdf.pages):
        r = page.obj.get("/Resources")
        if isinstance(r, Dictionary):
            yield from forms(r, pi, page)
        for a in page.obj.get("/Annots", []) or []:
            if not isinstance(a, Dictionary):
                continue
            ap = a.get("/AP")
            if isinstance(ap, Dictionary):
                for _, v in ap.items():
                    streams = [v] if isinstance(v, pikepdf.Stream) else (
                        [x for _, x in v.items() if isinstance(x, pikepdf.Stream)] if isinstance(v, Dictionary) else [])
                    for st in streams:
                        if st.objgen in seen:
                            continue
                        seen.add(st.objgen)
                        rr = st.get("/Resources")
                        if isinstance(rr, Dictionary):
                            yield from forms(rr, pi, st)


def _transparency(pdf: pikepdf.Pdf) -> list[str]:
    found: list[str] = []
    for pi, res in _resources(pdf):
        gs = res.get("/ExtGState")
        if isinstance(gs, Dictionary):
            for nm, g in gs.items():
                if not isinstance(g, Dictionary):
                    continue
                for k in ("/CA", "/ca"):
                    if k in g and float(g[k]) < 1:
                        found.append(f"p.{pi + 1}: opacity {k}={float(g[k]):g}")
                sm = g.get("/SMask")
                if sm is not None and str(sm) != "/None":
                    found.append(f"p.{pi + 1}: soft mask in graphics state")
                bm = g.get("/BM")
                if bm is not None:
                    vals = [str(x) for x in bm] if isinstance(bm, Array) else [str(bm)]
                    if any(v not in ("/Normal", "/Compatible") for v in vals):
                        found.append(f"p.{pi + 1}: blend mode {vals[0]}")
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image" and "/SMask" in obj:
            found.append("image with soft mask (alpha)")
            break
    for pi, page in enumerate(pdf.pages):
        for a in page.obj.get("/Annots", []) or []:
            if isinstance(a, Dictionary) and "/CA" in a and float(a.CA) < 1:
                found.append(f"p.{pi + 1}: annotation opacity {float(a.CA):g}")
    return found


def _strip_group(obj: Dictionary, actions: list[str], where: str) -> None:
    g = obj.get("/Group")
    if isinstance(g, Dictionary) and g.get("/S") == "/Transparency":
        del obj["/Group"]
        actions.append(f"removed transparency group ({where})")


def _clean_action(holder: Dictionary, key: str, part: int, actions: list[str], where: str) -> None:
    a = holder.get(key)
    if not isinstance(a, Dictionary):
        return
    s = a.get("/S")
    bad = s in _FORBIDDEN_ACTIONS or (s == "/Named" and a.get("/N") not in _ALLOWED_NAMED)
    if bad:
        del holder[key]
        actions.append(f"removed {str(s).lstrip('/')} action ({where})")
        return
    nxt = a.get("/Next")
    if isinstance(nxt, Dictionary):
        _clean_action(a, "/Next", part, actions, where)
    elif isinstance(nxt, Array):
        keep = [x for x in nxt if not (isinstance(x, Dictionary) and x.get("/S") in _FORBIDDEN_ACTIONS)]
        if len(keep) != len(nxt):
            a.Next = Array(keep)
            actions.append(f"removed chained actions ({where})")


def _strip_scripts(pdf: pikepdf.Pdf, part: int, actions: list[str]) -> None:
    root = pdf.Root
    if "/AA" in root:
        del root["/AA"]
        actions.append("removed document additional-actions")
    _clean_action(root, "/OpenAction", part, actions, "open action")
    names = root.get("/Names")
    if isinstance(names, Dictionary) and "/JavaScript" in names:
        del names["/JavaScript"]
        actions.append("removed document JavaScript")
    for pi, page in enumerate(pdf.pages):
        if "/AA" in page.obj:
            del page.obj["/AA"]
            actions.append(f"removed page additional-actions (p.{pi + 1})")
        for a in page.obj.get("/Annots", []) or []:
            if isinstance(a, Dictionary):
                if "/AA" in a:
                    del a["/AA"]
                    actions.append(f"removed annotation additional-actions (p.{pi + 1})")
                _clean_action(a, "/A", part, actions, f"annotation p.{pi + 1}")
    af = root.get("/AcroForm")
    if isinstance(af, Dictionary):
        stack = list(af.get("/Fields", []) or [])
        seen = set()
        while stack:
            fd = stack.pop()
            if not isinstance(fd, Dictionary) or fd.objgen in seen:
                continue
            seen.add(fd.objgen)
            if "/AA" in fd:
                del fd["/AA"]
                actions.append("removed form-field additional-actions")
            _clean_action(fd, "/A", part, actions, "form field")
            stack.extend(fd.get("/Kids", []) or [])
    ol = root.get("/Outlines")
    if isinstance(ol, Dictionary):
        stack = [ol.get("/First")]
        seen = set()
        while stack:
            it = stack.pop()
            if not isinstance(it, Dictionary) or it.objgen in seen:
                continue
            seen.add(it.objgen)
            _clean_action(it, "/A", part, actions, "bookmark")
            stack += [it.get("/First"), it.get("/Next")]


def _embedded_files(pdf: pikepdf.Pdf, part: int, actions: list[str]) -> None:
    root = pdf.Root
    names = root.get("/Names")
    has_ef = isinstance(names, Dictionary) and "/EmbeddedFiles" in names
    if part in (1, 2):
        if has_ef:
            n = len(pdf.attachments)
            del names["/EmbeddedFiles"]
            actions.append(f"removed {n} embedded file(s) (PDF/A-{part} does not allow arbitrary attachments)")
        if "/AF" in root:
            del root["/AF"]
        return
    # PDF/A-3: keep attachments, add AFRelationship / Subtype / Params and catalog /AF
    import mimetypes

    af = list(root.get("/AF", []) or [])
    known = {x.objgen for x in af if isinstance(x, Dictionary)}
    for nm, spec in list(pdf.attachments.items()):
        fs = spec.obj
        if "/AFRelationship" not in fs:
            fs.AFRelationship = Name.Unspecified
            actions.append(f"attachment {nm}: AFRelationship /Unspecified")
        if "/UF" not in fs:
            fs.UF = pikepdf.String(nm)
        ef = fs.get("/EF")
        if isinstance(ef, Dictionary):
            for k in ("/F", "/UF"):
                st = ef.get(k)
                if isinstance(st, pikepdf.Stream):
                    if "/Subtype" not in st:
                        mt = mimetypes.guess_type(nm)[0] or "application/octet-stream"
                        st.Subtype = Name("/" + mt.replace("/", "#2F"))
                    params = st.get("/Params")
                    if not isinstance(params, Dictionary):
                        st.Params = Dictionary()
                        params = st.Params
                    if "/ModDate" not in params:
                        params.ModDate = pikepdf.String(_pdf_date())
        ind = fs if fs.is_indirect else pdf.make_indirect(fs)
        if ind.objgen not in known:
            af.append(ind)
            known.add(ind.objgen)
    if af:
        root.AF = Array(af)


def _pdf_date() -> str:
    import datetime as dt

    d = dt.datetime.now().astimezone()
    off = d.strftime("%z")
    return d.strftime("D:%Y%m%d%H%M%S") + (f"{off[:3]}'{off[3:]}'" if off else "Z")


def _fix_annotations(pdf: pikepdf.Pdf, part: int, actions: list[str], problems: list[str]) -> None:
    forbidden = _FORBIDDEN_ANNOTS[part]
    fixed_flags = 0
    for pi, page in enumerate(pdf.pages):
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array):
            continue
        keep = []
        for a in annots:
            if not isinstance(a, Dictionary):
                continue
            st = a.get("/Subtype")
            if st in forbidden:
                actions.append(f"removed {str(st).lstrip('/')} annotation (p.{pi + 1})")
                continue
            flags = int(a.get("/F", 0))
            nf = (flags | 4) & ~(1 | 2 | 32 | 256)
            if nf != flags or "/F" not in a:
                a.F = nf
                fixed_flags += 1
            ap = a.get("/AP")
            if isinstance(ap, Dictionary):
                for k in ("/D", "/R"):
                    if k in ap:
                        del ap[k]
            rect = [float(x) for x in a.get("/Rect", [0, 0, 0, 0])]
            zero = abs(rect[2] - rect[0]) < 1e-6 and abs(rect[3] - rect[1]) < 1e-6 if len(rect) == 4 else True
            if st not in ("/Popup", "/Link") and not zero and not (isinstance(ap, Dictionary) and "/N" in ap):
                problems.append(f"p.{pi + 1}: {str(st).lstrip('/')} annotation has no appearance stream")
            if part == 1 and "/CA" in a and float(a.CA) >= 1:
                del a["/CA"]
            keep.append(a)
        if len(keep) != len(annots):
            page.obj.Annots = Array(keep)
    if fixed_flags:
        actions.append(f"set Print flag / cleared hidden flags on {fixed_flags} annotation(s)")
    af = pdf.Root.get("/AcroForm")
    if isinstance(af, Dictionary):
        if af.get("/NeedAppearances"):
            del af["/NeedAppearances"]
            actions.append("removed AcroForm NeedAppearances")
        if "/XFA" in af:
            del af["/XFA"]
            actions.append("removed XFA form data")


def _fix_objects(pdf: pikepdf.Pdf, part: int, actions: list[str], problems: list[str]) -> None:
    n_interp = n_lzw = n_cidset = 0
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream):
            filt = obj.get("/Filter")
            fl = [str(x) for x in filt] if isinstance(filt, Array) else ([str(filt)] if filt is not None else [])
            if "/LZWDecode" in fl:
                if all(f in ("/LZWDecode", "/FlateDecode") for f in fl):
                    data = obj.read_bytes()
                    obj.write(data)  # re-encoded with Flate on save
                    n_lzw += 1
                else:
                    problems.append(f"object {obj.objgen[0]}: LZW-compressed stream that cannot be re-encoded")
            if obj.get("/Subtype") == "/Image":
                if obj.get("/Interpolate"):
                    obj.Interpolate = False
                    n_interp += 1
                for k in ("/Alternates", "/OPI"):
                    if k in obj:
                        del obj[k]
                        actions.append(f"removed image {k.lstrip('/')}")
            elif obj.get("/Subtype") == "/Form":
                for k in ("/OPI", "/PS"):
                    if k in obj:
                        del obj[k]
                        actions.append(f"removed form XObject {k.lstrip('/')}")
                if obj.get("/Subtype2") == "/PS":
                    problems.append(f"object {obj.objgen[0]}: PostScript XObject")
                if part == 1:
                    _strip_group(obj, actions, "form XObject")
            elif obj.get("/Subtype") == "/PS":
                problems.append(f"object {obj.objgen[0]}: PostScript XObject")
        elif isinstance(obj, Dictionary):
            if obj.get("/Type") == "/FontDescriptor" and part in (2, 3) and "/CIDSet" in obj:
                del obj["/CIDSet"]
                n_cidset += 1
    for pi, res in _resources(pdf):
        gs = res.get("/ExtGState")
        if isinstance(gs, Dictionary):
            for _, g in gs.items():
                if isinstance(g, Dictionary):
                    if "/TR" in g:
                        del g["/TR"]
                        actions.append("removed transfer function (TR)")
                    if "/TR2" in g and g.get("/TR2") != "/Default":
                        g.TR2 = Name.Default
                    if "/HTP" in g:
                        del g["/HTP"]
    if part == 1:
        for pi, page in enumerate(pdf.pages):
            _strip_group(page.obj, actions, f"p.{pi + 1}")
    if n_interp:
        actions.append(f"turned off image interpolation on {n_interp} image(s)")
    if n_lzw:
        actions.append(f"re-encoded {n_lzw} LZW stream(s) with Flate")
    if n_cidset:
        actions.append(f"removed {n_cidset} CIDSet entr{'y' if n_cidset == 1 else 'ies'}")


def _fix_optional_content(pdf: pikepdf.Pdf, part: int, actions: list[str]) -> None:
    oc = pdf.Root.get("/OCProperties")
    if not isinstance(oc, Dictionary):
        return
    if part == 1:
        del pdf.Root["/OCProperties"]
        actions.append("removed optional content (layers); PDF/A-1 has no layers")
        return
    confs = [oc.get("/D")] + list(oc.get("/Configs", []) or [])
    for i, c in enumerate(confs):
        if not isinstance(c, Dictionary):
            continue
        if "/Name" not in c:
            c.Name = pikepdf.String("Default" if i == 0 else f"Config {i}")
            actions.append("named optional-content configuration")
        if "/AS" in c:
            del c["/AS"]
            actions.append("removed optional-content auto-state (AS)")


def _font_problems(pdf: pikepdf.Pdf) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pi, res, owner in _resources(pdf, with_owner=True):
        fonts = res.get("/Font")
        if not isinstance(fonts, Dictionary):
            continue
        used: set[str] | None = None
        if owner is not None:
            try:
                used = {str(ops[0]) for ops, _ in pikepdf.parse_content_stream(owner, "Tf") if ops}
            except Exception:
                used = None
        for rname, ft in fonts.items():
            if not isinstance(ft, Dictionary):
                continue
            if used is not None and str(rname) not in used:
                continue  # in resources but never selected by Tf → not used for rendering
            st = ft.get("/Subtype")
            if st == "/Type3":
                continue
            fd_holder = ft
            if st == "/Type0":
                d = ft.get("/DescendantFonts")
                if isinstance(d, Array) and len(d):
                    fd_holder = d[0]
            fd = fd_holder.get("/FontDescriptor") if isinstance(fd_holder, Dictionary) else None
            emb = isinstance(fd, Dictionary) and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
            name = str(ft.get("/BaseFont", "?")).lstrip("/")
            if not emb and name not in seen:
                seen.add(name)
                out.append(f"font {name} is not embedded (first used on p.{pi + 1})")
    return out


def _output_intent(pdf: pikepdf.Pdf, actions: list[str]) -> None:
    ois = pdf.Root.get("/OutputIntents")
    lst = list(ois) if isinstance(ois, Array) else []
    for oi in lst:
        if isinstance(oi, Dictionary) and oi.get("/S") == "/GTS_PDFA1" and "/DestOutputProfile" in oi:
            return
    profile = None
    for oi in lst:
        if isinstance(oi, Dictionary) and "/DestOutputProfile" in oi:
            profile = oi.DestOutputProfile
            break
    if profile is not None:
        src_oi = next(oi for oi in lst if isinstance(oi, Dictionary) and "/DestOutputProfile" in oi)
        new = Dictionary(Type=Name.OutputIntent, S=Name.GTS_PDFA1, DestOutputProfile=profile,
                         OutputConditionIdentifier=src_oi.get("/OutputConditionIdentifier", pikepdf.String("Custom")))
        actions.append("added PDF/A output intent sharing the existing output profile")
    else:
        st = pikepdf.Stream(pdf, srgb_icc())
        st.N = 3
        new = Dictionary(Type=Name.OutputIntent, S=Name.GTS_PDFA1, DestOutputProfile=pdf.make_indirect(st),
                         OutputConditionIdentifier=pikepdf.String("sRGB IEC61966-2.1"),
                         Info=pikepdf.String("sRGB IEC61966-2.1"),
                         RegistryName=pikepdf.String("http://www.color.org"))
        actions.append("added sRGB output intent (ICC v2)")
    lst.append(pdf.make_indirect(new))
    pdf.Root.OutputIntents = Array(lst)


def _xmp(pdf: pikepdf.Pdf, part: int, conf: str, actions: list[str]) -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=True) as m:
            m.load_from_docinfo(pdf.docinfo, raise_failure=False)
            m["pdfaid:part"] = str(part)
            m["pdfaid:conformance"] = conf.upper()
    actions.append(f"wrote XMP metadata (pdfaid {part}{conf.upper()}) in sync with the Info dictionary")


def to_pdfa(src: str | Path, level: str = "2b", *, out: str | Path | None = None, password: str | None = None,
            options: GateOptions | None = None, max_samples: int = 5, quiet: bool = False) -> OpResult:
    """Best-effort conversion of an existing PDF to PDF/A (1b, 2b, 2u, 3b, 3u), validated by veraPDF.

    Adds an sRGB output intent and PDF/A XMP, removes encryption, JavaScript and forbidden actions,
    removes attachments (kept with AFRelationship for 3b/3u), fixes annotation flags and appearance
    dictionaries, re-encodes LZW, drops forbidden image/graphics-state keys. Refuses 1b when the file
    uses transparency. Fonts are never substituted: a non-embedded font is reported and the result
    will not pass. ``res.ok`` is True only when veraPDF says the output conforms; the remaining
    failures are in ``res.data["verapdf"]``.
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    part, conf = _parse_level(level)
    std = f"pdfa-{part}{conf}"
    actions: list[str] = []
    problems: list[str] = []
    try:
        pdf = pikepdf.open(str(src), password=password or "")
    except pikepdf.PasswordError as exc:
        raise ValueError(f"{src.name}: wrong or missing password") from exc
    with pdf:
        if pdf.is_encrypted:
            actions.append("removed encryption (PDF/A forbids it)")
        if part == 1:
            tr = _transparency(pdf)
            if tr:
                raise ValueError(f"PDF/A-1b forbids transparency and this file uses it ({'; '.join(tr[:3])}); "
                                 "use level 2b instead")
        _strip_scripts(pdf, part, actions)
        _embedded_files(pdf, part, actions)
        _fix_annotations(pdf, part, actions, problems)
        _fix_objects(pdf, part, actions, problems)
        _fix_optional_content(pdf, part, actions)
        problems += _font_problems(pdf)
        _output_intent(pdf, actions)
        _xmp(pdf, part, conf, actions)
        dst = output_path(src, f"pdfa{part}{conf}", out=out)
        kw: dict[str, Any] = dict(encryption=False, fix_metadata_version=True)
        if part == 1:
            kw.update(force_version="1.4", object_stream_mode=pikepdf.ObjectStreamMode.disable)
        elif float(pdf.pdf_version or "1.7") > 1.7:
            kw.update(force_version="1.7")
        pdf.save(str(dst), **kw)
    vr = run_verapdf(dst, std, max_samples=max_samples)
    data = {"standard": std, "actions": actions, "problems": problems, "verapdf": vr.to_dict(),
            "compliant": vr.compliant, "summary": vr.summary()}
    res = finalize(dst, src, options=options, data=data, quiet=True)
    findings: list[Finding] = []
    if not vr.compliant:
        first = vr.rules[0] if vr.rules else None
        pg = first.pages[0] - 1 if first and first.pages else None
        msg = (f"not {std}: {vr.error}" if vr.error else
               f"not {std}: {len(vr.rules)} rule(s) still fail; first {first.rule_id}: {first.description[:70]}"
               if first else f"not {std}")
        hint = problems[0] if problems else "run pdfcore preflight for the full rule list"
        findings.append(Finding("standard", msg, pg, hint))
    _with_findings(res, findings, True)
    if not quiet:
        for a in actions:
            print(f"  fixed  {a}")
        for p in problems:
            print(f"  issue  {p}")
        print(f"{dst}  {res.line()}")
    return res


# ====================================================================== accessibility
_STD_TYPES = {
    "Document", "Part", "Art", "Sect", "Div", "BlockQuote", "Caption", "TOC", "TOCI", "Index", "NonStruct", "Private",
    "P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "L", "LI", "Lbl", "LBody", "Table", "TR", "TH", "TD", "THead", "TBody",
    "TFoot", "Span", "Quote", "Note", "Reference", "BibEntry", "Code", "Link", "Annot", "Ruby", "RB", "RT", "RP", "Warichu",
    "WT", "WP", "Figure", "Formula", "Form", "DocumentFragment", "Aside", "Title", "FENote", "Sub", "Em", "Strong",
    "Artifact",
}


@dataclass
class _Elem:
    type: str
    raw: str
    page: int | None
    alt: str | None
    actual: str | None
    depth: int
    mcids: list[tuple[int, int]]
    scope: str | None
    obj: Any = None
    children: list["_Elem"] = field(default_factory=list)


def _struct_tree(pdf: pikepdf.Pdf, limit: int = 20000) -> list[_Elem]:
    root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(root, Dictionary):
        return []
    rolemap = root.get("/RoleMap")
    rm = {str(k).lstrip("/"): str(v).lstrip("/") for k, v in rolemap.items()} if isinstance(rolemap, Dictionary) else {}
    page_ix = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}

    def resolve(t: str) -> str:
        seen = set()
        while t in rm and t not in _STD_TYPES and t not in seen:
            seen.add(t)
            t = rm[t]
        return t

    flat: list[_Elem] = []
    visited: set[tuple[int, int]] = set()

    def pg_of(d) -> int | None:
        p = d.get("/Pg") if isinstance(d, Dictionary) else None
        return page_ix.get(p.objgen) if isinstance(p, Dictionary) else None

    def walk(node, depth: int, inherited_pg: int | None, parent: _Elem | None):
        if len(flat) >= limit or depth > 200:
            return
        if isinstance(node, Array):
            for k in node:
                walk(k, depth, inherited_pg, parent)
            return
        if isinstance(node, (int, pikepdf.Object)) and not isinstance(node, Dictionary):
            try:
                mc = int(node)
            except Exception:
                return
            if parent is not None and inherited_pg is not None:
                parent.mcids.append((inherited_pg, mc))
            return
        if not isinstance(node, Dictionary):
            return
        t = node.get("/Type")
        if t == "/MCR":
            pg = pg_of(node) if pg_of(node) is not None else inherited_pg
            if parent is not None and pg is not None and "/MCID" in node:
                parent.mcids.append((pg, int(node.MCID)))
            return
        if t == "/OBJR":
            return
        if node.is_indirect:
            if node.objgen in visited:
                return
            visited.add(node.objgen)
        if "/S" not in node:
            walk(node.get("/K"), depth, inherited_pg, parent)
            return
        raw = str(node.S).lstrip("/")
        pg = pg_of(node)
        pg = pg if pg is not None else inherited_pg
        alt = str(node.Alt) if "/Alt" in node else None
        act = str(node.ActualText) if "/ActualText" in node else None
        scope = None
        attrs = node.get("/A")
        for a in ([attrs] if isinstance(attrs, Dictionary) else list(attrs) if isinstance(attrs, Array) else []):
            if isinstance(a, Dictionary) and "/Scope" in a:
                scope = str(a.Scope).lstrip("/")
        el = _Elem(resolve(raw), raw, pg, alt, act, depth, [], scope, node)
        flat.append(el)
        if parent is not None:
            parent.children.append(el)
        walk(node.get("/K"), depth + 1, pg, el)

    walk(root.get("/K"), 0, None, None)
    return flat


def _descendants(el: _Elem) -> Iterable[_Elem]:
    for c in el.children:
        yield c
        yield from _descendants(c)


def _elem_page(el: _Elem) -> int | None:
    if el.page is not None:
        return el.page
    if el.mcids:
        return el.mcids[0][0]
    for d in _descendants(el):
        if d.page is not None or d.mcids:
            return _elem_page(d)
    return None


def _lum(rgb: Sequence[float]) -> float:
    def ch(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a: Sequence[float], b: Sequence[float]) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _contrast_page(pdfium_doc, plumber_page, pi: int, scale: float = 2.0, max_words: int = 150) -> list[dict[str, Any]]:
    import numpy as np

    page = pdfium_doc[pi]
    img = np.asarray(page.render(scale=scale).to_pil().convert("RGB")).astype(np.float32)
    H, W = img.shape[:2]
    try:
        words = plumber_page.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False)
    except Exception:
        return []
    if len(words) > max_words:
        step = len(words) / max_words
        words = [words[int(i * step)] for i in range(max_words)]
    out = []
    for w in words:
        x0, x1 = int(w["x0"] * scale), int(math.ceil(w["x1"] * scale))
        t, b = int(w["top"] * scale), int(math.ceil(w["bottom"] * scale))
        if x1 - x0 < 2 or b - t < 2 or x0 < 0 or t < 0 or x1 > W or b > H:
            continue
        pad = 3
        X0, X1, T, B = max(0, x0 - pad), min(W, x1 + pad), max(0, t - pad), min(H, b + pad)
        outer = img[T:B, X0:X1].reshape(-1, 3)
        box = img[t:b, x0:x1].reshape(-1, 3)
        mask = np.ones((B - T, X1 - X0), bool)
        mask[t - T:b - T, x0 - X0:x1 - X0] = False
        ring = img[T:B, X0:X1][mask]
        if len(ring) < 4 or len(box) < 4:
            continue
        bg = np.median(ring, axis=0)
        dist = np.linalg.norm(box - bg, axis=1)
        mx = float(dist.max())
        if mx < 1:
            continue  # nothing drawn (e.g. invisible OCR text)
        ink = box[dist >= mx * 0.8].mean(axis=0)
        ratio = _contrast(ink, bg)
        size = float(w.get("size") or 0)
        bold = "bold" in str(w.get("fontname", "")).lower()
        large = size >= 18 or (bold and size >= 14)
        need = 3.0 if large else 4.5
        if ratio < need:
            out.append({"page": pi + 1, "text": w["text"][:40], "ratio": round(ratio, 2), "required": need,
                        "ink": [int(v) for v in ink], "background": [int(v) for v in bg], "size": round(size, 1)})
    return out


def _order_agreement(seq: Sequence[int]) -> float:
    """Fraction of consecutive pairs in increasing order."""
    if len(seq) < 2:
        return 1.0
    good = sum(1 for a, b in zip(seq, seq[1:]) if b >= a)
    return good / (len(seq) - 1)


def _a11y_checks(src: Path, *, password: str | None, max_pages: int, contrast: bool, verapdf: bool,
                 max_samples: int) -> dict[str, Any]:
    import pdfplumber
    import pypdfium2 as pdfium

    findings: list[dict[str, Any]] = []
    checks: dict[str, str] = {}

    def add(check: str, sev: str, msg: str, page: int | None = None, fix: str = "") -> None:
        findings.append({"check": check, "severity": sev, "message": msg, "page": page, "fix": fix})

    with pikepdf.open(str(src), password=password or "") as pdf:
        n = len(pdf.pages)
        root = pdf.Root
        mark = root.get("/MarkInfo")
        marked = bool(isinstance(mark, Dictionary) and mark.get("/Marked"))
        has_tree = isinstance(root.get("/StructTreeRoot"), Dictionary)
        tagged = marked and has_tree
        if tagged:
            checks["tagged"] = "pass"
        else:
            checks["tagged"] = "fail"
            if has_tree and not marked:
                add("tagged", "fail", "structure tree present but MarkInfo/Marked is not true", None,
                    "pdfcore a11y --fix sets MarkInfo")
            else:
                add("tagged", "fail", "document is not tagged (no structure tree): screen readers get no reading order",
                    None, "rebuild from source with pdfcore make --standard pdfua-1, or tag in an editor")
        lang = str(root.get("/Lang", "") or "")
        if lang:
            checks["language"] = "pass"
        else:
            checks["language"] = "fail"
            add("language", "fail", "document language (/Lang) not set", None, "pdfcore a11y --fix --lang en-US")
        title = ""
        try:
            title = str(pdf.docinfo.get("/Title", "")) if pdf.docinfo is not None else ""
            if not title:
                with pdf.open_metadata(set_pikepdf_as_editor=False) as m:
                    title = str(m.get("dc:title", "") or "")
        except Exception:
            pass
        vp = root.get("/ViewerPreferences")
        ddt = bool(isinstance(vp, Dictionary) and vp.get("/DisplayDocTitle"))
        checks["title"] = "pass" if title else "fail"
        if not title:
            add("title", "fail", "document title missing", None, "pdfcore a11y --fix --title '…'")
        checks["display_doc_title"] = "pass" if ddt else "warn"
        if not ddt:
            add("display_doc_title", "warn", "viewer shows the file name, not the title (DisplayDocTitle not set)",
                None, "pdfcore a11y --fix")

        elems = _struct_tree(pdf) if has_tree else []
        # headings
        heads = [e for e in elems if re.fullmatch(r"H[1-6]?", e.type)]
        if not tagged:
            checks["headings"] = "na"
        elif not heads:
            checks["headings"] = "warn" if n > 1 else "pass"
            if n > 1:
                add("headings", "warn", "no headings tagged (H1–H6): navigation by heading is impossible")
        else:
            bad = False
            levels = [int(e.type[1]) for e in heads if len(e.type) == 2]
            if levels and levels[0] != 1:
                add("headings", "warn", f"first heading is H{levels[0]}, not H1", _p1(_elem_page(heads[0])))
                bad = True
            prev = 0
            for e in heads:
                if len(e.type) != 2:
                    continue
                lv = int(e.type[1])
                if prev and lv > prev + 1:
                    add("headings", "fail", f"heading level skips from H{prev} to H{lv}", _p1(_elem_page(e)),
                        "use consecutive heading levels")
                    bad = True
                prev = lv
            checks["headings"] = "fail" if any(f["check"] == "headings" and f["severity"] == "fail" for f in findings) \
                else ("warn" if bad else "pass")
        # figures / formulas
        figs = [e for e in elems if e.type in ("Figure", "Formula")]
        missing = [e for e in figs if not (e.alt or e.actual)]
        checks["figures_alt"] = "na" if not figs else ("fail" if missing else "pass")
        for e in missing[:50]:
            add("figures_alt", "fail", f"{e.type.lower()} without alternative text (/Alt)", _p1(_elem_page(e)),
                "add alt text describing the image")
        # tables
        tables = [e for e in elems if e.type == "Table"]
        t_bad = 0
        for e in tables:
            ths = [d for d in _descendants(e) if d.type == "TH"]
            if not ths:
                t_bad += 1
                add("tables", "fail", "table without header cells (TH)", _p1(_elem_page(e)), "tag header cells as TH")
            elif not any(d.scope for d in ths):
                add("tables", "warn", "table header cells have no Scope", _p1(_elem_page(e)))
        checks["tables"] = "na" if not tables else ("fail" if t_bad else "pass")
        # links + forms
        link_bad = link_total = link_untagged = 0
        for pi, page in enumerate(pdf.pages):
            for a in page.obj.get("/Annots", []) or []:
                if isinstance(a, Dictionary) and a.get("/Subtype") == "/Link":
                    link_total += 1
                    if not str(a.get("/Contents", "") or "").strip():
                        link_bad += 1
                        if link_bad <= 30:
                            add("links", "warn", "link without a description (/Contents)", pi + 1,
                                "describe the link target")
                    if tagged and "/StructParent" not in a:
                        link_untagged += 1
        if link_untagged:
            add("links", "warn", f"{link_untagged} link(s) not in the structure tree (no Link/OBJR)")
        checks["links"] = "na" if not link_total else ("warn" if (link_bad or link_untagged) else "pass")
        af = root.get("/AcroForm")
        f_total = f_bad = 0
        if isinstance(af, Dictionary):
            stack = [(fd, "") for fd in (af.get("/Fields", []) or [])]
            seen = set()
            while stack:
                fd, parent_name = stack.pop()
                if not isinstance(fd, Dictionary) or fd.objgen in seen:
                    continue
                seen.add(fd.objgen)
                nm = ".".join(x for x in (parent_name, str(fd.get("/T", "") or "")) if x)
                kids = [k for k in (fd.get("/Kids", []) or []) if isinstance(k, Dictionary) and "/T" in k]
                if kids:
                    stack += [(k, nm) for k in kids]
                    continue
                f_total += 1
                if not str(fd.get("/TU", "") or "").strip():
                    f_bad += 1
                    pg = None
                    p = fd.get("/P")
                    if isinstance(p, Dictionary):
                        pg = next((i + 1 for i, pp in enumerate(pdf.pages) if pp.obj.objgen == p.objgen), None)
                    if f_bad <= 50:
                        add("forms", "fail", f"form field {nm or '?'} has no tooltip (/TU)", pg,
                            "give every field an accessible name")
        checks["forms"] = "na" if not f_total else ("fail" if f_bad else "pass")
        struct_seq = [(pg, mc) for e in elems for (pg, mc) in e.mcids]

    # text-level checks: scanned pages, reading order, contrast
    scanned: list[int] = []
    order: dict[str, Any] = {"sample": [], "structure_vs_content": None, "structure_vs_visual": None}
    contrast_fails: list[dict[str, Any]] = []
    pages_checked = min(n, max_pages)
    with pdfplumber.open(str(src), password=password or "") as pl:
        doc = pdfium.PdfDocument(str(src), password=password) if contrast else None
        try:
            mcid_text: dict[tuple[int, int], str] = {}
            content_pos: dict[tuple[int, int], int] = {}
            visual_pos: dict[tuple[int, int], tuple[float, float]] = {}
            for pi in range(pages_checked):
                pg = pl.pages[pi]
                chars = pg.chars
                if not chars:
                    area = float(pg.width * pg.height) or 1.0
                    img_area = sum(max(0.0, float(im["x1"] - im["x0"])) * max(0.0, float(im["bottom"] - im["top"]))
                                   for im in pg.images)
                    if img_area / area > 0.5:
                        scanned.append(pi + 1)
                    continue
                for idx, ch in enumerate(chars):
                    mc = ch.get("mcid")
                    if mc is None:
                        continue
                    key = (pi, int(mc))
                    if key not in content_pos:
                        content_pos[key] = len(content_pos)
                        visual_pos[key] = (round(float(ch["top"]) / 4.0), float(ch["x0"]))
                    mcid_text[key] = mcid_text.get(key, "") + ch["text"]
                if doc is not None:
                    contrast_fails += _contrast_page(doc, pg, pi)
        finally:
            if doc is not None:
                doc.close()
    for p in scanned:
        add("scanned", "fail", "page looks scanned and has no text layer", p, "run pdfcore ocr")
    checks["scanned"] = "fail" if scanned else "pass"
    seq = [k for k in struct_seq if k in content_pos]
    if seq:
        c_order = [content_pos[k] for k in seq]
        v_sorted = sorted(visual_pos, key=lambda k: (k[0], visual_pos[k]))
        v_rank = {k: i for i, k in enumerate(v_sorted)}
        v_order = [v_rank[k] for k in seq]
        a_c, a_v = _order_agreement(c_order), _order_agreement(v_order)
        order.update(structure_vs_content=round(a_c, 3), structure_vs_visual=round(a_v, 3))
        order["sample"] = [{"page": k[0] + 1, "mcid": k[1], "text": mcid_text.get(k, "")[:60].strip()} for k in seq[:12]]
        checks["reading_order"] = "pass" if min(a_c, a_v) >= 0.8 else "warn"
        if a_c < 0.8:
            add("reading_order", "warn", f"structure order differs from content-stream order ({a_c:.0%} agreement)")
        if a_v < 0.8:
            add("reading_order", "warn", f"structure order differs from top-to-bottom visual order ({a_v:.0%}); "
                "fine for multi-column layouts, check it reads correctly")
    else:
        checks["reading_order"] = "na" if not tagged else "warn"
        if tagged:
            add("reading_order", "warn", "no marked content linked from the structure tree in the sampled pages")
    if contrast:
        by_page: dict[int, list[dict[str, Any]]] = {}
        for c in contrast_fails:
            by_page.setdefault(c["page"], []).append(c)
        for p, items in sorted(by_page.items()):
            worst = min(items, key=lambda c: c["ratio"])
            add("contrast", "fail", f"{len(items)} word(s) below WCAG contrast; worst {worst['ratio']}:1 "
                f"(needs {worst['required']}:1) at \"{worst['text']}\"", p, "darken the text or lighten the background")
        checks["contrast"] = "fail" if contrast_fails else "pass"
    vr = None
    if verapdf:
        vr = run_verapdf(src, "pdfua-1", max_samples=max_samples, password=password)
        if vr.error:
            checks["pdfua_verapdf"] = "na"
            add("pdfua_verapdf", "warn", f"veraPDF PDF/UA-1 not run: {vr.error}")
        else:
            checks["pdfua_verapdf"] = "pass" if vr.compliant else "fail"
            for r in vr.rules:
                add("pdfua_verapdf", "fail", f"PDF/UA-1 {r.rule_id}: {r.description[:140]} ({r.failed_checks}×)",
                    r.pages[0] if r.pages else None)
    weights = {"tagged": 20, "language": 10, "title": 8, "display_doc_title": 4, "headings": 8, "figures_alt": 12,
               "tables": 6, "reading_order": 6, "links": 4, "forms": 6, "scanned": 10, "contrast": 8,
               "pdfua_verapdf": 8}
    got = tot = 0.0
    for k, st in checks.items():
        if st == "na":
            continue
        w = weights.get(k, 5)
        tot += w
        got += w * {"pass": 1.0, "warn": 0.5, "fail": 0.0}[st]
    score = round(100 * got / tot) if tot else 0
    return {"file": str(src), "pages": n, "pages_checked": pages_checked, "score": score, "checks": checks,
            "findings": findings, "reading_order": order, "contrast_failures": contrast_fails[:200],
            "verapdf_ua1": vr.to_dict() if vr else None, "title": title, "lang": lang}


def _p1(pi: int | None) -> int | None:
    return pi + 1 if pi is not None else None


def _first_heading_text(src: Path, password: str | None) -> str | None:
    import pdfplumber

    with pikepdf.open(str(src), password=password or "") as pdf:
        heads = [e for e in _struct_tree(pdf) if re.fullmatch(r"H1?|Title", e.type)]
        keys = []
        for h in heads[:1]:
            keys = list(h.mcids) + [k for d in _descendants(h) for k in d.mcids]
    if not keys:
        return None
    pages = {k[0] for k in keys}
    txt = ""
    with pdfplumber.open(str(src), password=password or "") as pl:
        for p in sorted(pages):
            want = {k[1] for k in keys if k[0] == p}
            txt += "".join(c["text"] for c in pl.pages[p].chars if c.get("mcid") in want)
    txt = " ".join(txt.split())
    return txt[:200] or None


def _a11y_fix(src: Path, dst: Path, *, lang: str | None, title: str | None, password: str | None) -> list[str]:
    done: list[str] = []
    with pikepdf.open(str(src), password=password or "") as pdf:
        root = pdf.Root
        if lang or not str(root.get("/Lang", "") or ""):
            root.Lang = pikepdf.String(lang or "en-US")
            done.append(f"set /Lang {lang or 'en-US'}" + ("" if lang else " (default; pass --lang)"))
        cur = ""
        try:
            cur = str(pdf.docinfo.get("/Title", "")) if pdf.docinfo is not None else ""
            if not cur:
                with pdf.open_metadata(set_pikepdf_as_editor=False) as m:
                    cur = str(m.get("dc:title", "") or "")
        except Exception:
            pass
        new_title = title or cur or _first_heading_text(src, password) or src.stem.replace("_", " ").replace("-", " ")
        if new_title != cur or title:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=True) as m:
                    m["dc:title"] = new_title
            pdf.docinfo["/Title"] = pikepdf.String(new_title)
            done.append(f"set title “{new_title}”")
        vp = root.get("/ViewerPreferences")
        if not isinstance(vp, Dictionary):
            root.ViewerPreferences = Dictionary()
            vp = root.ViewerPreferences
        if not vp.get("/DisplayDocTitle"):
            vp.DisplayDocTitle = True
            done.append("set DisplayDocTitle")
        if isinstance(root.get("/StructTreeRoot"), Dictionary):
            mi = root.get("/MarkInfo")
            if not isinstance(mi, Dictionary):
                root.MarkInfo = Dictionary()
                mi = root.MarkInfo
            if not mi.get("/Marked"):
                mi.Marked = True
                done.append("set MarkInfo/Marked (structure tree present)")
        pdf.save(str(dst))
    return done


def a11y(src: str | Path, *, fix: bool = False, lang: str | None = None, title: str | None = None,
         out: str | Path | None = None, report: str | Path | None = None, max_pages: int = 20,
         contrast: bool = True, verapdf: bool = True, max_samples: int = 3, password: str | None = None,
         quiet: bool = False) -> OpResult:
    """Accessibility check of an existing PDF → report PDF + JSON with page numbers and a 0–100 score.

    With *fix*: only the safe repairs (/Lang, title, DisplayDocTitle, MarkInfo when a structure tree
    exists) are written to a new file, which is then re-checked. ``ok`` is False when any check fails.
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    target = src
    fixed: list[str] = []
    fixed_path: Path | None = None
    if fix:
        fixed_path = output_path(src, "a11y-fixed", out=out)
        fixed = _a11y_fix(src, fixed_path, lang=lang, title=title, password=password)
        target = fixed_path
    data = _a11y_checks(target, password=password, max_pages=max_pages, contrast=contrast, verapdf=verapdf,
                        max_samples=max_samples)
    data["fixed"] = fixed
    rep_path = Path(report) if report else output_path(target, "a11y", ".pdf", out=None if fix else out)
    jpath = output_path(target, "a11y", ".json")
    _write_json(jpath, data)
    fails = [f for f in data["findings"] if f["severity"] == "fail"]
    warns = [f for f in data["findings"] if f["severity"] == "warn"]
    summary = f"Accessibility score {data['score']}/100 · {len(fails)} problem(s), {len(warns)} warning(s)"
    rows = [["Check", "Result"]] + [[k.replace("_", " "), v] for k, v in data["checks"].items()]
    frows = [["Severity", "Page", "Check", "Finding", "Fix"]] + [
        [f["severity"], str(f["page"] or "—"), f["check"].replace("_", " "), f["message"], f["fix"]]
        for f in fails + warns][:400]
    sections: list[tuple[str, Sequence[Any]]] = [("Checks", rows)]
    if fixed:
        sections.append(("Repairs applied", fixed))
    if len(frows) > 1:
        sections.append(("Findings", frows))
    ro = data["reading_order"]
    if ro.get("sample"):
        sections.append(("Reading order sample (structure order)",
                         [f"Agreement with content order {ro['structure_vs_content']:.0%}, with visual order "
                          f"{ro['structure_vs_visual']:.0%}."] +
                         [["#", "Page", "Text"]] + [[str(i + 1), str(s["page"]), s["text"]] for i, s in enumerate(ro["sample"])]))
    write_report_pdf(rep_path, f"Accessibility: {src.name}",
                     f"{data['pages']} page(s), {data['pages_checked']} sampled · tags, language, title, headings, "
                     "alt text, tables, reading order, links, forms, scans, contrast, veraPDF PDF/UA-1", summary,
                     sections)
    findings = [Finding("a11y", f"{f['check'].replace('_', ' ')}: {f['message']}"[:150],
                        (f["page"] - 1) if f["page"] else None, f["fix"]) for f in fails]
    if fix and fixed_path is not None:
        res = finalize(fixed_path, src, password=password, data=data, extra=[rep_path, jpath], quiet=True)
    else:
        res = finalize(rep_path, None, data=data, extra=[jpath], quiet=True)
    _with_findings(res, findings, True)
    if not quiet:
        print(f"{src.name}: {summary}")
        for f in (fails + warns)[:12]:
            where = f"p.{f['page']}: " if f["page"] else ""
            print(f"  {f['severity']:4}  {where}{f['message']}")
        if len(fails) + len(warns) > 12:
            print(f"  … {len(fails) + len(warns) - 12} more in {rep_path.name}")
        print(f"{res.output}  {res.line()}")
    return res


# ====================================================================== CLI
@command("preflight", "veraPDF preflight: every failed PDF/A or PDF/UA rule → report PDF + JSON", group="standards")
def _cli_preflight(p):
    p.add_argument("pdf")
    p.add_argument("--standard", default="pdfa-2b",
                   help="pdfa-1b, pdfa-2b, pdfa-2u, pdfa-3b, pdfa-4, pdfua-1 … or 'auto' (what the file declares)")
    p.add_argument("--json-out", help="where to write the JSON report")
    p.add_argument("--samples", type=int, default=5, help="sample objects per failed rule")
    p.add_argument("-o", "--out", help="report PDF path")
    p.add_argument("--password", help="password to open the input")
    return lambda ns: preflight(ns.pdf, ns.standard, out=ns.out, json_out=ns.json_out, max_samples=ns.samples,
                                password=ns.password)


@command("pdfa", "Convert an existing PDF to PDF/A (1b, 2b, 2u, 3b, 3u) and validate with veraPDF", group="standards")
def _cli_pdfa(p):
    p.add_argument("pdf")
    p.add_argument("--level", default="2b", help="1b, 2b (default), 2u, 3b, 3u")
    add_common(p)
    return lambda ns: to_pdfa(ns.pdf, ns.level, out=ns.out, password=ns.password,
                              options=gate_options_from(ns) if (ns.gate_profile or ns.size_budget or ns.png_dir) else None)


@command("a11y", "Accessibility check (tags, language, alt text, headings, tables, contrast…) + PDF/UA", group="standards")
def _cli_a11y(p):
    p.add_argument("pdf")
    p.add_argument("--fix", action="store_true", help="apply safe repairs: /Lang, title, DisplayDocTitle, MarkInfo")
    p.add_argument("--lang", help="with --fix: document language, e.g. en-US")
    p.add_argument("--title", help="with --fix: document title")
    p.add_argument("--max-pages", type=int, default=20, help="pages sampled for text, order and contrast checks")
    p.add_argument("--no-contrast", action="store_true")
    p.add_argument("--no-verapdf", action="store_true")
    p.add_argument("--json", action="store_true", help="print the JSON result")
    p.add_argument("-o", "--out", help="output path (fixed PDF with --fix, else the report)")
    p.add_argument("--password", help="password to open the input")

    def run(ns):
        res = a11y(ns.pdf, fix=ns.fix, lang=ns.lang, title=ns.title, out=ns.out, max_pages=ns.max_pages,
                   contrast=not ns.no_contrast, verapdf=not ns.no_verapdf, password=ns.password, quiet=ns.json)
        if ns.json:
            print(json.dumps(res.data, indent=2, default=str))
        return res

    return run
