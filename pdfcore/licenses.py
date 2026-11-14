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
