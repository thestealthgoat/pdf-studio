"""License audit for the installed environment (public-edition rule: permissive only).

Policy
------
* GPL / AGPL / LGPL (any version) → **fail** (LGPL is held back until Owen rules on it).
* Unknown / missing license → **fail**.
* Banned packages (PyMuPDF, Ghostscript bindings, Marker…) → **fail** regardless of metadata.
* Anything matching the permissive allow-list → pass.

``OVERRIDES`` records packages whose metadata is unclear but whose license was
checked by hand; each needs a reason. Keep this list short.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions

BANNED = {
    "pymupdf": "PyMuPDF is AGPL — local edition only",
    "pymupdfb": "PyMuPDF is AGPL — local edition only",
    "pymupdf-fonts": "PyMuPDF add-on — local edition only",
    "fitz": "PyMuPDF alias — local edition only",
    "ghostscript": "Ghostscript is AGPL — local edition only",
    "python-ghostscript": "Ghostscript is AGPL — local edition only",
    "marker-pdf": "Marker weights are not permissively licensed",
    "pdf2image": "depends on Poppler (GPL) — use pypdfium2",
    "pdftotext": "Poppler (GPL)",
    "python-poppler": "Poppler (GPL)",
    "ocrmypdf": "MPL itself but pulls Ghostscript; use pdfcore ocr",
    "cairosvg": "LGPL",
    "svglib": "LGPL",
}

PERMISSIVE = re.compile(
    r"\b(MIT|MIT-0|MIT-CMU|BSD|0BSD|BSD-2-Clause|BSD-3-Clause|Apache|Apache-2\.0|MPL|MPL-2\.0|Mozilla Public License|"
    r"ISC|PSF|PSF-2\.0|Python Software Foundation|HPND|Historical Permission|Zlib|zlib|CC0|CC0-1\.0|Unlicense|"
    r"BlueOak|Zope Public|ZPL|UPL|Matplotlib|PIL Software|CNRI|W3C|Artistic-2\.0|OFL|BSL-1\.0|Boost)\b",
    re.I,
)
COPYLEFT = re.compile(r"\b(A?GPL|LGPL|GNU (Affero |Lesser |Library )?General Public|EUPL|SSPL|CC-BY-NC|Commons Clause)\b", re.I)

OVERRIDES: dict[str, str] = {
    # name: reason (verified by hand)
    "pypdfium2": "BSD-3-Clause/Apache-2.0; bundled PDFium deps are FreeType(FTL), libjpeg-turbo, zlib, lcms2(MIT), OpenJPEG(BSD-2), libpng, ICU — all permissive",
    "pypdfium2-raw": "same as pypdfium2",
}

SELF = {"pdf-studio", "pip", "setuptools", "wheel", "uv"}


@dataclass
class Row:
    name: str
    version: str
    license: str
    verdict: str  # ok | fail | override
    reason: str = ""


def _license_text(d: Distribution) -> str:
    md = d.metadata
    expr = md.get("License-Expression") or ""
    lic = md.get("License") or ""
    if len(lic) > 200:  # full license text pasted into the field
        lic = lic[:200]
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    parts = [p for p in (expr, lic, "; ".join(classifiers)) if p and p.upper() != "UNKNOWN"]
    return " | ".join(parts)


def judge(name: str, text: str) -> tuple[str, str]:
    key = name.lower().replace("_", "-")
    if key in BANNED:
        return "fail", BANNED[key]
    if key in OVERRIDES:
        return "override", OVERRIDES[key]
    if not text:
        return "fail", "no license metadata"
    if COPYLEFT.search(text):
        # dual licenses like "GPL-3.0-or-later OR MPL-2.0" are acceptable when a permissive option exists
        if re.search(r"\bOR\b", text) and PERMISSIVE.search(text):
            return "ok", "dual-licensed; permissive option used"
        return "fail", "copyleft license"
    if PERMISSIVE.search(text):
        return "ok", ""
    return "fail", "license not recognised as permissive"


def audit() -> list[Row]:
    rows: dict[str, Row] = {}
    for d in distributions():
        name = d.metadata.get("Name") or ""
        if not name or name.lower() in SELF:
            continue
        text = _license_text(d)
        verdict, reason = judge(name, text)
        rows[name.lower()] = Row(name, d.version, text, verdict, reason)
    return sorted(rows.values(), key=lambda r: r.name.lower())


def failures(rows: list[Row] | None = None) -> list[Row]:
    return [r for r in (rows if rows is not None else audit()) if r.verdict == "fail"]


# ----------------------------------------------------------------------------- bundled native libraries
# Package metadata only describes the Python package. Wheels also ship compiled third-party libraries
# (e.g. numpy.libs/, scipy/.dylibs/, pillow/.dylibs/). Each one is checked against this table.
# verdict: ok | exception (GPL with a linking/runtime exception — free to ship) | review (needs Owen's ruling) | fail
NATIVE: list[tuple[str, str, str]] = [
    (r"^libgcc(_s)?$|^libgfortran$|^libstdc\+\+$|^libgomp$", "GPL-3.0-or-later WITH GCC-exception-3.1", "exception"),
    (r"^libquadmath$", "LGPL-2.1-or-later", "review"),
    (r"^lib(scipy_)?openblas", "BSD-3-Clause", "ok"),
    (r"^(msvcp|vcruntime|concrt)\d+", "Microsoft Visual C++ Redistributable", "ok"),
    (r"^libjpeg|^libturbojpeg", "IJG / BSD-3-Clause / Zlib (libjpeg-turbo)", "ok"),
    (r"^libqpdf$", "Apache-2.0", "ok"),
    (r"^libpdfium$|^pdfium$", "BSD-3-Clause / Apache-2.0", "ok"),
    (r"^libXau$|^libxcb", "MIT (X11)", "ok"),
    (r"^libavif$|^libopenjp2$|^libaom$|^libdav1d$", "BSD-2-Clause", "ok"),
    (r"^libbrotli", "MIT", "ok"),
    (r"^libfreetype$", "FTL (FreeType License)", "ok"),
    (r"^libharfbuzz", "MIT", "ok"),
    (r"^liblcms2$", "MIT", "ok"),
    (r"^liblzma$", "0BSD / public domain", "ok"),
    (r"^libpng", "libpng-2.0", "ok"),
    (r"^libsharpyuv$|^libwebp", "BSD-3-Clause", "ok"),
    (r"^libtiff$", "libtiff (BSD-style)", "ok"),
    (r"^libz$|^libz\.|^zlib|zlib-ng", "Zlib (zlib / zlib-ng)", "ok"),
    (r"^libzstd$", "BSD-3-Clause", "ok"),
    (r"^libproj$", "MIT", "ok"),
    (r"^libsqlite3$", "public domain", "ok"),
    (r"^libcurl$", "curl (MIT-style)", "ok"),
    (r"^lib(ssl|crypto)$", "Apache-2.0 (OpenSSL 3)", "ok"),
    (r"^libxml2$|^libxslt$|^libexslt$|^libffi$|^libyaml", "MIT", "ok"),
    (r"^libqhull", "Qhull (permissive)", "ok"),
    (r"^libfribidi$|^libraqm$", "LGPL-2.1", "fail"),
]

# Documented, package-scoped decisions for "review" libraries. Key: (package, library).
NATIVE_DECISIONS: dict[tuple[str, str], str] = {}  # none needed: macOS/Windows wheels bundle no LGPL libraries


@dataclass
class NativeRow:
    package: str
    library: str
    path: str
    license: str
    verdict: str  # ok | exception | review | fail
    reason: str = ""


def _lib_base(filename: str) -> str:
    stem = re.sub(r"\.(so(\.\d+)*|dylib|dll)$", "", filename, flags=re.I)
    stem = re.sub(r"[-_][0-9a-f]{8,}.*$", "", stem)  # auditwheel / delocate hash suffixes
    stem = re.sub(r"[-.]\d+(\.\d+)*$", "", stem)     # version suffixes
    return stem


def native_audit() -> list[NativeRow]:
    """Every compiled third-party library bundled inside installed distributions."""
    rows: list[NativeRow] = []
    for d in distributions():
        name = (d.metadata.get("Name") or "").strip()
        if not name or name.lower() in SELF:
            continue
        for f in d.files or []:
            s = str(f)
            fn = s.replace("\\", "/").split("/")[-1]
            if not re.search(r"\.(so(\.\d+)*|dylib|dll)$", fn, re.I):
                continue
            vendored = re.search(r"(^|/)([\w.-]*\.libs|\.dylibs)/", s.replace("\\", "/")) or fn.lower().startswith("lib")
            if not vendored or ".cpython-" in fn or fn.endswith(".pyd") or ".abi3." in fn:
                continue  # the package's own extension modules: covered by its own license
            base = _lib_base(fn)
            lic, verdict = "UNKNOWN", "fail"
            for rx, l, v in NATIVE:
                if re.search(rx, base, re.I):
                    lic, verdict = l, v
                    break
            reason = ""
            if verdict == "review":
                dec = NATIVE_DECISIONS.get((name.lower(), base.lower()))
                if dec:
                    reason = dec
                else:
                    verdict, reason = "fail", "LGPL library with no recorded decision for this package"
            elif verdict == "fail":
                reason = "unknown bundled library — identify its license and add it to NATIVE" if lic == "UNKNOWN" else "copyleft library bundled"
            rows.append(NativeRow(name, base, s, lic, verdict, reason))
    uniq = {(r.package.lower(), r.library.lower()): r for r in rows}
    return sorted(uniq.values(), key=lambda r: (r.package.lower(), r.library.lower()))
