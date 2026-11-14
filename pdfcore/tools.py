"""Find external programs on macOS and Windows (PATH first, then each tool's standard install folders).

Windows installers (Tesseract, LibreOffice, veraPDF) usually don't add themselves to PATH, so the default
locations are checked too. Supported platforms: macOS and Windows.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path


def _win_dirs() -> list[Path]:
    out = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE"):
        v = os.environ.get(var)
        if v:
            out.append(Path(v))
    return out


CANDIDATES: dict[str, list[str]] = {
    "tesseract": [
        "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
        "{win}/Tesseract-OCR/tesseract.exe", "{win}/Programs/Tesseract-OCR/tesseract.exe",
    ],
    "soffice": [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice", "/opt/homebrew/bin/soffice",
        "{win}/LibreOffice/program/soffice.exe",
    ],
    "verapdf": [
        "/opt/homebrew/bin/verapdf", "/usr/local/bin/verapdf", "/Applications/verapdf/verapdf",
        "{win}/veraPDF/verapdf.bat", "{win}/verapdf/verapdf.bat",
    ],
    "typst": ["/opt/homebrew/bin/typst", "{win}/typst/typst.exe"],
}

ALIASES = {"soffice": ["soffice", "libreoffice"], "verapdf": ["verapdf", "verapdf.bat"]}


@lru_cache(maxsize=None)
def find_tool(name: str) -> str | None:
    """Absolute path of an external program, or None."""
    for alias in ALIASES.get(name, [name]):
        p = shutil.which(alias)
        if p:
            return p
    for pattern in CANDIDATES.get(name, []):
        paths = [Path(pattern.replace("{win}", str(d))) for d in _win_dirs()] if "{win}" in pattern else [Path(pattern)]
        for p in paths:
            if p.exists():
                return str(p)
    return None


def configure_tesseract() -> str | None:
    """Point pytesseract at Tesseract when it isn't on PATH (typical on Windows). Returns the path used."""
    exe = find_tool("tesseract")
    if exe:
        try:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = exe
        except ImportError:
            pass
    return exe
