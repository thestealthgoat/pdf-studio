from __future__ import annotations

import pdfplumber
import pytest

from pdfcore.ops.repair import repair


def test_repairs_corrupt_xref(work):
    src = work("broken")
    res = repair(src, quiet=True)
    assert res.ok, res.gate.details()
    with pdfplumber.open(res.output) as pdf:
        assert len(pdf.pages) == 3 and "Quarterly Operations Report" in pdf.pages[0].extract_text()


def test_salvages_file_missing_trailer(work, tmp_path):
    raw = work("broken").read_bytes()
    cut = tmp_path / "notrailer.pdf"
    cut.write_bytes(raw[: raw.rfind(b"xref") - 50])  # lose the xref, trailer and the end of the last object
    res = repair(cut, quiet=True)
    assert res.ok, res.gate.details()
    assert res.data["lost_pages"] == [3]
    with pdfplumber.open(res.output) as pdf:
        assert len(pdf.pages) == 2 and "Quarterly" in pdf.pages[0].extract_text()


def test_unrecoverable_raises(tmp_path):
    bad = tmp_path / "junk.pdf"
    bad.write_bytes(b"%PDF-1.7\nthis is not a pdf at all\n")
    with pytest.raises(ValueError, match="could not repair"):
        repair(bad, quiet=True)


def test_healthy_file_round_trips(work):
    res = repair(work("letter"), quiet=True)
    assert res.ok and res.data["lost_pages"] == []
