from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from pdfcore import engines
from pdfcore.cli import main
from pdfcore.gate import GateOptions, run_gate
from pdfcore.licenses import judge
from pdfcore.ops._common import parse_pages
from pdfcore.result import output_path
from pdfcore.types import Rect


def test_output_path_never_overwrites(tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-1.4")
    p1 = output_path(src, "merged")
    assert p1.name == "a.merged.pdf"
    p1.write_bytes(b"x")
    assert output_path(src, "merged").name == "a.merged-2.pdf"
    with pytest.raises(ValueError):
        output_path(src, "x", out=src)


def test_parse_pages():
    assert parse_pages("1-3,5", 6) == [0, 1, 2, 4]
    assert parse_pages("odd", 5) == [0, 2, 4]
    assert parse_pages("-1", 4) == [3]
    assert parse_pages("4-", 6) == [3, 4, 5]
    assert parse_pages("3-1", 3) == [2, 1, 0]
    with pytest.raises(ValueError):
        parse_pages("9", 3)


def test_rect_roundtrip():
    r = Rect(10, 20, 110, 70)
    assert Rect.from_pdf(*r.to_pdf(792), page_height=792) == r
    assert r.overlap_fraction(Rect(10, 20, 60, 70)) == 1.0


def test_engines_registered():
    assert "pdfium" in engines.available("render")
    assert "pdfplumber" in engines.available("text")
    assert engines.get("render").name == "pdfium"


def test_license_judge():
    assert judge("foo", "MIT License")[0] == "ok"
    assert judge("foo", "GNU General Public License v3")[0] == "fail"
    assert judge("foo", "AGPL-3.0")[0] == "fail"
    assert judge("foo", "LGPL-2.1")[0] == "fail"
    assert judge("foo", "GPL-3.0-or-later OR MPL-2.0")[0] == "ok"
    assert judge("PyMuPDF", "MIT")[0] == "fail"  # banned by name, whatever the metadata says
    assert judge("foo", "")[0] == "fail"


def test_installed_environment_is_permissive():
    from pdfcore.licenses import failures

    assert failures() == []


def _bad_pdf(path: Path):
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("bad")
    c.setFont("Helvetica", 12)
    c.drawString(560, 400, "This line runs off the right edge of the page")
    c.showPage()
    c.showPage()  # blank page
    c.save()


def test_gate_catches_problems(tmp_path):
    bad = tmp_path / "bad.pdf"
    _bad_pdf(bad)
    rep = run_gate(bad)
    msgs = {f.message for f in rep.failures}
    assert any("clipped past right" in m for m in msgs)
    assert "blank page" in msgs
    assert any("not embedded" in m for m in msgs)
    assert rep.line().startswith("FAIL p.")


def test_gate_baseline_excuses_inherited(tmp_path):
    bad = tmp_path / "bad.pdf"
    _bad_pdf(bad)
    rep = run_gate(bad, baseline=bad)
    assert rep.passed, rep.details()
    assert rep.warnings


def test_gate_corpus(corpus):
    assert run_gate(corpus["letter"]).passed
    assert run_gate(corpus["encrypted"], password="test").passed
    assert not run_gate(corpus["broken"]).passed


def test_gate_typeset_margin(tmp_path):
    p = tmp_path / "m.pdf"
    c = canvas.Canvas(str(p), pagesize=letter)
    c.setFont("Helvetica", 11)
    c.drawString(10, 400, "too close to the left edge")
    c.save()
    rep = run_gate(p, options=GateOptions(profile="typeset", margins_in=0.75))
    assert any("left margin" in f.message for f in rep.failures)


def test_gate_pngs(tmp_path, corpus):
    rep = run_gate(corpus["slides"], options=GateOptions(png_dir=tmp_path / "png"))
    assert len(rep.pngs) == 6 and all(p.exists() for p in rep.pngs)


def test_cli_doctor_and_gate(corpus, capsys):
    assert main(["doctor"]) == 0
    assert main(["gate", str(corpus["letter"])]) == 0
    assert "PASS" in capsys.readouterr().out


def test_native_library_names_and_verdicts():
    from pdfcore.licenses import NATIVE, _lib_base
    import re

    assert _lib_base("libgfortran-83c28eba-468e71e5.so.5.0.0") == "libgfortran"
    assert _lib_base("libquadmath.0.dylib") == "libquadmath"
    assert _lib_base("libscipy_openblas64_-ed4f167a5330424524f45258e7ca2c8d.dll").startswith("libscipy_openblas")

    def verdict(name):
        return next((v for rx, _, v in NATIVE if re.search(rx, name, re.I)), "unknown")

    assert verdict("libgfortran") == "exception"
    assert verdict("libquadmath") == "review"
    assert verdict("libfribidi") == "fail"
    assert verdict("libz.1.3.1.zlib-ng") == "ok"
    assert verdict("libsomethingnew") == "unknown"


def test_no_bundled_copyleft_native_libraries():
    """Wheels' bundled .so/.dylib/.dll files: no LGPL/GPL (except GCC runtime exception) or unknown libraries."""
    from pdfcore.licenses import native_audit

    bad = [r for r in native_audit() if r.verdict == "fail"]
    assert not bad, [(r.package, r.library, r.reason) for r in bad]


def test_gate_cropbox_clipping(tmp_path):
    """Text outside the visible CropBox is clipped; an offset MediaBox alone is not."""
    import io

    import pikepdf

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(register_reportlab_font(), 12)
    c.drawString(300, 400, "visible")
    c.drawString(40, 750, "outside the crop")
    c.save()
    p = pikepdf.open(io.BytesIO(buf.getvalue()))
    p.pages[0].MediaBox = [-200, -200, 812, 992]
    p.pages[0].CropBox = [100, 100, 512, 692]
    out = tmp_path / "crop.pdf"
    p.save(out)
    msgs = [f.message for f in run_gate(out).failures]
    assert any("clipped past" in m for m in msgs)


def test_gate_samples_long_drawing_sets():
    from pdfcore.gate.checks import GateOptions, deep_sample

    assert deep_sample(10, GateOptions()) is None
    s = deep_sample(50, GateOptions())
    assert s[0] == 0 and s[-1] == 49 and len(s) == 24
    assert deep_sample(50, GateOptions(profile="typeset")) is None


def register_reportlab_font() -> str:
    from pdfcore.fonts import register_reportlab

    return register_reportlab()["sans"]
