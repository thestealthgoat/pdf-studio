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
        elif osname == "Windows":
            from pdfcore.tools import find_tool

            winget = shutil.which("winget")
            pkgs = {"tesseract": "UB-Mannheim.TesseractOCR", "soffice": "TheDocumentFoundation.LibreOffice"}
            for tool, pkg in pkgs.items():
                if find_tool(tool):
                    continue
                if winget:
                    rc |= _run([winget, "install", "--id", pkg, "-e", "--accept-source-agreements",
                                "--accept-package-agreements"] if yes else [winget, "install", "--id", pkg, "-e"])
                else:
                    print(f"Install {pkg} (winget not found): https://winget.run/pkg/{pkg.replace('.', '/')}")
            if not find_tool("verapdf"):
                print("veraPDF (PDF/A validation, optional): download the installer from https://verapdf.org/software/")
        else:
            print("Supported platforms are macOS and Windows. Install Tesseract, veraPDF and LibreOffice manually.")
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
