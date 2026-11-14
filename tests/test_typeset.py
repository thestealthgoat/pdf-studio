from __future__ import annotations

import shutil
from pathlib import Path

import pdfplumber
import pytest

from pdfcore.fonts import templates_dir
from pdfcore.ops.typeset import contrast, load_brand, make, md_to_typst

EX = templates_dir() / "examples"
CORE = ["estimate", "invoice", "report", "proposal", "letter", "memo"]


@pytest.mark.parametrize("name", CORE)
def test_template_renders_and_passes_gate(tmp_path, name):
    data = tmp_path / f"{name}.yaml"
    shutil.copy(EX / f"{name}.yaml", data)
    res = make(name, data, quiet=True)
    assert res.ok, res.gate.details()
    assert res.output == tmp_path / f"{name}.pdf"
    with pdfplumber.open(res.output) as pdf:
        assert pdf.metadata.get("Title")
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    assert "Page 1 of" in text


def test_estimate_totals_exact(tmp_path):
    data = tmp_path / "e.yaml"
    shutil.copy(EX / "estimate.yaml", data)
    res = make("estimate", data, quiet=True)
    # 64,367.00 subtotal + 8.25% of 44,430.00 materials = 68,032.48
    assert res.data["total"] == "68032.48"
    with pdfplumber.open(res.output) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    assert "$68,032.48" in text and "$64,367.00" in text and "(2,600.00)" in text


def test_never_overwrites(tmp_path):
    data = tmp_path / "memo.yaml"
    shutil.copy(EX / "memo.yaml", data)
    a = make("memo", data, quiet=True).output
    b = make("memo", data, quiet=True).output
    assert a != b and a.exists() and b.exists()


def test_house_style_measure_and_leading(tmp_path):
    """Body text: 60–75 characters per line, line height 1.3–1.45, fonts embedded (gate)."""
    data = tmp_path / "report.yaml"
    shutil.copy(EX / "report.yaml", data)
    res = make("report", data, quiet=True)
    with pdfplumber.open(res.output) as pdf:
        lines = []
        for page in pdf.pages:
            words = [w for w in page.extract_words(extra_attrs=["size", "fontname"]) if "Serif" in w["fontname"] and abs(w["size"] - 11) < 0.2]
            rows: dict[float, list] = {}
            for w in words:
                rows.setdefault(round(w["bottom"]), []).append(w)
            lines += [(b, " ".join(x["text"] for x in sorted(r, key=lambda x: x["x0"]))) for b, r in sorted(rows.items())]
    full = [t for _, t in lines if len(t) > 50]  # full lines (not paragraph ends)
    avg = sum(len(t) for t in full) / len(full)
    assert 60 <= avg <= 75, avg
    gaps = [b2 - b1 for (b1, _), (b2, _) in zip(lines, lines[1:]) if 0 < b2 - b1 < 20]
    pitch = sorted(gaps)[len(gaps) // 2] / 11
    assert 1.3 <= pitch <= 1.45, pitch


def test_brand_contrast_enforced(tmp_path):
    b = load_brand("default")
    assert contrast(b["colors"]["ink"], b["colors"]["paper"]) >= 4.5
    bad = tmp_path / "bad.toml"
    bad.write_text((templates_dir() / "brands" / "default.toml").read_text().replace('muted = "#55606e"', 'muted = "#bbbbbb"'))
    with pytest.raises(ValueError, match="muted"):
        load_brand(bad)


def test_markdown_conversion():
    t = md_to_typst("# Head\n**bold** and _it_\n* item\n1. one\n[site](https://x.org) costs $5 @ me")
    assert "= Head" in t and "*bold*" in t and "- item" in t and "+ one" in t
    assert '#link("https://x.org")[site]' in t and "\\$5" in t and "\\@" in t


def test_pdfa(tmp_path):
    if not shutil.which("verapdf"):
        pytest.skip("veraPDF not installed")
    data = tmp_path / "letter.yaml"
    shutil.copy(EX / "letter.yaml", data)
    res = make("letter", data, standard="pdfa-2b", quiet=True)
    assert res.ok, res.gate.details()
