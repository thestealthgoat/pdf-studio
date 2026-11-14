"""House fonts (SIL OFL 1.1, unmodified, shipped with their license files).

Sans: Inter 4.1 · Serif: Source Serif 4.005. Two families max (house style).
"""

from __future__ import annotations

from pathlib import Path

SANS = "Inter"
SERIF = "Source Serif 4"

FILES = [
    "Inter-Regular.ttf", "Inter-Italic.ttf", "Inter-SemiBold.ttf", "Inter-SemiBoldItalic.ttf", "Inter-Bold.ttf",
    "Inter-BoldItalic.ttf", "SourceSerif4-Regular.ttf", "SourceSerif4-It.ttf", "SourceSerif4-Semibold.ttf",
    "SourceSerif4-SemiboldIt.ttf", "SourceSerif4-Bold.ttf", "SourceSerif4-BoldIt.ttf",
]


def templates_dir() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here / "_templates", here.parent / "templates"):
        if cand.is_dir():
            return cand
    return here.parent / "templates"


def font_dir() -> Path:
    return templates_dir() / "fonts"


def missing_fonts() -> list[str]:
    d = font_dir()
    return [f for f in FILES if not (d / f).exists()]


def font_path(name: str) -> Path:
    return font_dir() / name


_registered = False


def register_reportlab() -> dict[str, str]:
    """Register house fonts with ReportLab; returns {role: reportlab_font_name}."""
    global _registered
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    names = {
        "sans": "Inter", "sans-bold": "Inter-Bold", "sans-semibold": "Inter-SemiBold", "sans-italic": "Inter-Italic",
        "serif": "SourceSerif4", "serif-bold": "SourceSerif4-Bold", "serif-italic": "SourceSerif4-It",
    }
    if _registered:
        return names
    files = {
        "Inter": "Inter-Regular.ttf", "Inter-Bold": "Inter-Bold.ttf", "Inter-SemiBold": "Inter-SemiBold.ttf",
        "Inter-Italic": "Inter-Italic.ttf", "SourceSerif4": "SourceSerif4-Regular.ttf",
        "SourceSerif4-Bold": "SourceSerif4-Bold.ttf", "SourceSerif4-It": "SourceSerif4-It.ttf",
    }
    for rl_name, fn in files.items():
        p = font_path(fn)
        if p.exists():
            pdfmetrics.registerFont(TTFont(rl_name, str(p)))
    from reportlab.lib.fonts import addMapping

    if font_path("Inter-Regular.ttf").exists():
        addMapping("Inter", 0, 0, "Inter")
        addMapping("Inter", 1, 0, "Inter-Bold")
        addMapping("Inter", 0, 1, "Inter-Italic")
        addMapping("Inter", 1, 1, "Inter-Bold")
    _registered = True
    return names
