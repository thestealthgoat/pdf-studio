"""``pdfcore install`` — one command to set up everything the public edition uses."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from pdfcore.doctor import run_doctor

PY_EXTRAS = ["pytesseract>=0.3.10", "pyHanko>=0.25", "pyhanko-certvalidator>=0.26", "ezdxf>=1.3", "matplotlib>=3.8", "typst>=0.12"]
BREW = ["tesseract", "typst", "verapdf"]
BREW_CASKS = ["libreoffice"]
APT = ["tesseract-ocr", "tesseract-ocr-eng", "libreoffice-core", "default-jre-headless"]


def _run(cmd: list[str]) -> int:
    print("$", " ".join(cmd))
    return subprocess.call(cmd)


def run_install(yes: bool = False, system: bool = True, fonts: bool = True) -> int:
    pip = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if shutil.which("uv") and not _has_pip():
        pip = ["uv", "pip", "install", "--python", sys.executable, "--upgrade"]
    rc = _run(pip + PY_EXTRAS)
    if system:
        osname = platform.system()
        if osname == "Darwin" and shutil.which("brew"):
            missing = [f for f in BREW if not shutil.which(f)]
            if missing:
                rc |= _run(["brew", "install", *missing])
            if not shutil.which("soffice") and not _mac_soffice():
                rc |= _run(["brew", "install", "--cask", *BREW_CASKS])
        elif osname == "Linux" and shutil.which("apt-get"):
            print("Linux: run as root →  apt-get install -y " + " ".join(APT))
            print("Typst: pip package 'typst' is installed above; veraPDF: https://docs.verapdf.org/install/")
        else:
            print("Install manually: Tesseract, Typst, veraPDF, LibreOffice (see README).")
    if fonts:
        from pdfcore.fonts import missing_fonts

        mf = missing_fonts()
        if mf:
            print("House fonts are missing from this install (they ship in the repo under templates/fonts):", ", ".join(mf))
    print()
    run_doctor()
    return rc


def _has_pip() -> bool:
    try:
        import pip  # noqa: F401

        return True
    except ImportError:
        return False


def _mac_soffice() -> bool:
    from pathlib import Path

    return Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists()
