"""``pdfcore doctor`` — what is installed, what is missing, and how to fix it."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pdfcore.fonts import font_dir, missing_fonts


@dataclass
class Item:
    group: str
    name: str
    status: str  # ok | missing | warn
    detail: str = ""
    required: bool = False
    fix: str = ""


PY_REQUIRED = [
    ("pypdfium2", "pypdfium2"), ("pikepdf", "pikepdf"), ("pdfplumber", "pdfplumber"), ("pypdf", "pypdf"),
    ("reportlab", "reportlab"), ("PIL", "pillow"), ("fontTools", "fonttools"), ("yaml", "pyyaml"),
    ("openpyxl", "openpyxl"), ("numpy", "numpy"),
]
PY_OPTIONAL = [
    ("pytesseract", "pytesseract", "ocr"), ("pyhanko", "pyHanko", "sign"), ("ezdxf", "ezdxf", "cad"),
    ("matplotlib", "matplotlib", "cad"), ("typst", "typst", "typeset"), ("weasyprint", "weasyprint", "html"),
    ("camelot", "camelot-py", "tables"),
]
TOOLS = [
    # exe, version args, required, purpose, brew formula
    ("tesseract", ["--version"], False, "OCR", "tesseract"),
    ("typst", ["--version"], False, "typesetting (CLI; the 'typst' Python package also works)", "typst"),
    ("verapdf", ["--version"], False, "PDF/A & PDF/UA validation", "verapdf"),
    ("soffice", ["--version"], False, "Office → PDF conversion", "--cask libreoffice"),
]


def _ver(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def _tool_version(exe: str, args: list[str]) -> str:
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=30)
        first = (r.stdout or r.stderr).strip().splitlines()
        return first[0][:60] if first else ""
    except Exception as exc:
        return f"error: {exc}"


def collect() -> list[Item]:
    items: list[Item] = []
    v = sys.version_info
    items.append(Item("python", "python", "ok" if v >= (3, 11) else "missing", f"{v.major}.{v.minor}.{v.micro}", True, "use Python 3.11+"))
    for mod, dist in PY_REQUIRED:
        try:
            import_module(mod)
            items.append(Item("python", dist, "ok", _ver(dist) or "", True))
        except Exception as exc:
            items.append(Item("python", dist, "missing", str(exc)[:60], True, f"pip install {dist}"))
    for mod, dist, extra in PY_OPTIONAL:
        try:
            import_module(mod)
            items.append(Item("extras", f"{dist} [{extra}]", "ok", _ver(dist) or ""))
        except Exception:
            items.append(Item("extras", f"{dist} [{extra}]", "missing", "", False, f"pip install 'pdf-studio[{extra}]'"))
    for exe, args, req, purpose, formula in TOOLS:
        path = shutil.which(exe)
        if exe == "soffice" and not path:
            mac = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
            path = str(mac) if mac.exists() else None
        if path:
            ver = _tool_version(path, args)
            bad = ver.startswith("error") or "Unable to locate" in ver
            items.append(Item("tools", exe, "warn" if bad else "ok", ver, req, f"reinstall: brew install {formula}" if bad else ""))
        else:
            items.append(Item("tools", exe, "missing", purpose, req, f"brew install {formula}"))
    if shutil.which("tesseract"):
        try:
            langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30).stdout.splitlines()[1:]
            langs = [ln.strip() for ln in langs if ln.strip()]
            items.append(Item("tools", "tesseract langs", "ok" if "eng" in langs else "warn", ",".join(langs[:8])))
        except Exception:
            pass
    from pdfcore import engines

    for k in engines.KINDS:
        av = engines.available(k)
        items.append(Item("engines", k, "ok" if av else "missing", ", ".join(av), True))
    mf = missing_fonts()
    items.append(Item("fonts", "house fonts", "ok" if not mf else "missing", f"{font_dir()}" if not mf else "missing: " + ", ".join(mf), False, "pdfcore install --no-system"))
    try:
        from pdfcore.licenses import failures

        bad = failures()
        local = _ver("pdf-studio-local") is not None
        if bad and local:
            items.append(Item("licenses", "dependency audit", "warn",
                              "LOCAL EDITION environment (AGPL engines present: " + ", ".join(r.name for r in bad)[:120]
                              + ") — fine on this machine, never ship it", False))
            raise StopIteration
        items.append(Item("licenses", "dependency audit", "ok" if not bad else "missing",
                          "all permissive" if not bad else "; ".join(f"{r.name} ({r.reason})" for r in bad)[:200], True,
                          "remove the listed packages from this environment"))
    except StopIteration:
        pass
    except Exception as exc:
        items.append(Item("licenses", "dependency audit", "warn", str(exc)))
    return items


def run_doctor(as_json: bool = False, strict: bool = False) -> int:
    items = collect()
    if as_json:
        print(json.dumps([asdict(i) for i in items], indent=2))
    else:
        group = None
        for i in items:
            if i.group != group:
                group = i.group
                print(f"\n{group}")
            mark = {"ok": "✓", "missing": "✗", "warn": "!"}[i.status]
            req = "" if i.status == "ok" or i.required else " (optional)"
            fix = f"   → {i.fix}" if i.status != "ok" and i.fix else ""
            print(f"  {mark} {i.name:28} {i.detail}{req}{fix}")
    bad_required = [i for i in items if i.required and i.status == "missing"]
    if not as_json:
        print("\nPASS — all required components present" if not bad_required else f"\nFAIL — {len(bad_required)} required component(s) missing")
    return 1 if (strict and bad_required) or bad_required else 0
