"""Standards: veraPDF preflight, PDF/A conversion, accessibility checks."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pikepdf
import pytest

from pdfcore.ops import standards as st

ROOT = Path(__file__).resolve().parents[1]
needs_vera = pytest.mark.skipif(shutil.which("verapdf") is None, reason="veraPDF not installed")


def _rl_pdf(path: Path, draw) -> Path:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    from pdfcore.fonts import register_reportlab

    f = register_reportlab()
    c = canvas.Canvas(str(path), pagesize=letter, initialFontName=f["sans"])
    c.setTitle("Test document")
    draw(c, f)
    c.showPage()
    c.save()
    return path


@pytest.fixture(scope="module")
def typst_pdfa(tmp_path_factory) -> Path:
    pytest.importorskip("typst")
    from pdfcore.ops import typeset

    d = tmp_path_factory.mktemp("typst")
    r = typeset.make("letter", ROOT / "templates/examples/letter.yaml", standard="pdfa-2b", out=d / "letter.pdf",
                     quiet=True)
    return r.output


# ---------------------------------------------------------------- ICC profile
def test_srgb_icc_is_valid_v2_profile():
    from PIL import Image, ImageCms

    data = st.srgb_icc()
    assert data[36:40] == b"acsp" and data[12:16] == b"mntr" and data[8] == 2
    prof = ImageCms.ImageCmsProfile(io.BytesIO(data))
    assert "sRGB" in prof.profile.profile_description
    t = ImageCms.buildTransform(prof, ImageCms.createProfile("sRGB"), "RGB", "RGB")
    px = ImageCms.applyTransform(Image.new("RGB", (2, 2), (200, 30, 90)), t).getpixel((0, 0))
    assert all(abs(a - b) <= 2 for a, b in zip(px, (200, 30, 90)))


def test_standard_names():
    assert st._norm_standard("2b") == "pdfa-2b"
    assert st._norm_standard("PDF/A-1b") == "pdfa-1b"
    assert st._norm_standard("ua1") == "pdfua-1"
    with pytest.raises(ValueError, match="Ghostscript"):
        st._norm_standard("pdfx-4")
    with pytest.raises(ValueError, match="Ghostscript"):
        st.to_pdfa(ROOT / "corpus/generated/letter.pdf", "pdfx-1a")
    with pytest.raises(ValueError, match="2a"):
        st.to_pdfa(ROOT / "corpus/generated/letter.pdf", "2a")


# ---------------------------------------------------------------- veraPDF / preflight
@needs_vera
def test_run_verapdf_parses_failed_rules(corpus):
    vr = st.run_verapdf(corpus["letter"], "pdfa-2b")
    assert not vr.compliant and vr.error is None
    assert vr.failed_rules == len(vr.rules) > 0
    r = vr.rules[0]
    assert r.clause and r.test and r.description and r.failed_checks >= 1
    assert r.samples and r.samples[0]["context"]
    assert any(rule.pages == [1] for rule in vr.rules)  # page parsed from the object context


@needs_vera
def test_preflight_report_and_json(work):
    src = work("letter")
    res = st.preflight(src, "pdfa-2b", quiet=True)
    assert not res.ok and "not pdfa-2b" in res.line()
    assert res.output.name == "letter.preflight.pdf" and res.output.exists()
    js = res.extra_outputs[0]
    data = json.loads(js.read_text())
    assert data["compliant"] is False and data["rules"][0]["rule"]
    with pikepdf.open(res.output) as pdf:
        assert len(pdf.pages) >= 1
    # never overwrites: a second run gets a new name
    again = st.preflight(src, "pdfa-2b", quiet=True)
    assert again.output != res.output


@needs_vera
def test_preflight_passes_typeset_pdfa(typst_pdfa, tmp_path):
    src = tmp_path / "doc.pdf"
    shutil.copy(typst_pdfa, src)
    res = st.preflight(src, "auto", quiet=True)
    assert res.ok, res.line()
    assert res.data["standard"] == "pdfa-2b" and res.data["compliant"]


@needs_vera
def test_gate_standard_cli(typst_pdfa, corpus):
    from pdfcore.cli import main

    assert main(["gate", str(typst_pdfa), "--standard", "pdfa-2b"]) == 0
    assert main(["gate", str(corpus["letter"]), "--standard", "pdfa-2b"]) == 1


# ---------------------------------------------------------------- PDF/A conversion
@needs_vera
@pytest.mark.parametrize("level", ["2b", "1b", "3b", "2u"])
def test_to_pdfa_levels(work, level):
    src = work("letter")
    before = src.read_bytes()
    res = st.to_pdfa(src, level, quiet=True)
    assert res.ok, (res.line(), res.data["verapdf"]["rules"][:2])
    assert res.output.name == f"letter.pdfa{level}.pdf" and src.read_bytes() == before
    assert st.declared_standard(res.output) == f"pdfa-{level}"
    with pikepdf.open(res.output) as pdf:
        oi = pdf.Root.OutputIntents[0]
        assert oi.S == "/GTS_PDFA1" and oi.DestOutputProfile.read_bytes()[8] == 2
        if level == "1b":
            assert pdf.pdf_version == "1.4"


@needs_vera
def test_to_pdfa_removes_encryption_and_javascript(work, tmp_path):
    src = work("letter")
    js = tmp_path / "js.pdf"
    with pikepdf.open(src) as pdf:
        pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert('hi')"))
        pdf.save(js, encryption=pikepdf.Encryption(user="u-test", owner="o-test", R=6))
    res = st.to_pdfa(js, "2b", password="u-test", quiet=True)
    assert res.ok, res.line()
    acts = " ".join(res.data["actions"])
    assert "encryption" in acts and "JavaScript" in acts
    with pikepdf.open(res.output) as pdf:
        assert not pdf.is_encrypted and "/OpenAction" not in pdf.Root


@needs_vera
def test_to_pdfa_attachments_2b_removed_3b_kept(work, tmp_path):
    src = work("letter")
    att = tmp_path / "att.pdf"
    with pikepdf.open(src) as pdf:
        pdf.attachments["data.csv"] = pikepdf.AttachedFileSpec(pdf, b"a,b\n1,2\n", mime_type="text/csv")
        pdf.save(att)
    r2 = st.to_pdfa(att, "2b", quiet=True)
    assert r2.ok, r2.line()
    with pikepdf.open(r2.output) as pdf:
        assert len(pdf.attachments) == 0
    r3 = st.to_pdfa(att, "3b", quiet=True)
    assert r3.ok, (r3.line(), r3.data["verapdf"]["rules"][:2])
    with pikepdf.open(r3.output) as pdf:
        assert "data.csv" in pdf.attachments
        assert pdf.attachments["data.csv"].obj.AFRelationship == "/Unspecified"
        assert len(pdf.Root.AF) == 1


def test_to_pdfa_refuses_1b_with_transparency(tmp_path):
    def draw(c, f):
        c.setFillColorRGB(0.2, 0.3, 0.8, alpha=0.5)
        c.rect(100, 100, 200, 200, fill=1, stroke=0)
        c.setFont(f["sans"], 12)
        c.drawString(72, 700, "Half transparent box")

    src = _rl_pdf(tmp_path / "alpha.pdf", draw)
    with pytest.raises(ValueError, match="transparency"):
        st.to_pdfa(src, "1b", quiet=True)


@needs_vera
def test_to_pdfa_transparency_fine_for_2b(tmp_path):
    def draw(c, f):
        c.setFillColorRGB(0.2, 0.3, 0.8, alpha=0.5)
        c.rect(100, 100, 200, 200, fill=1, stroke=0)
        c.setFont(f["sans"], 12)
        c.drawString(72, 700, "Half transparent box")

    res = st.to_pdfa(_rl_pdf(tmp_path / "alpha.pdf", draw), "2b", quiet=True)
    assert res.ok, res.line()


@needs_vera
def test_to_pdfa_unembedded_font_is_not_claimed(work):
    res = st.to_pdfa(work("form_fillable"), "2b", quiet=True)
    assert not res.ok
    assert any("not embedded" in p for p in res.data["problems"])
    assert res.data["verapdf"]["rules"] and not res.data["compliant"]
    assert "not pdfa-2b" in res.line()


@needs_vera
def test_to_pdfa_annotation_flags(work, tmp_path):
    src = work("letter")
    an = tmp_path / "annot.pdf"
    with pikepdf.open(src) as pdf:
        pg = pdf.pages[0]
        ap = pikepdf.Stream(pdf, b"0 0 1 rg 0 0 20 20 re f")
        ap.Type, ap.Subtype, ap.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, [0, 0, 20, 20]
        a = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Square, Rect=[72, 72, 92, 92], F=2,
                               AP=pikepdf.Dictionary(N=ap, D=ap))
        pg.obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(a)]))
        pdf.save(an)
    res = st.to_pdfa(an, "2b", quiet=True)
    assert res.ok, res.line()
    with pikepdf.open(res.output) as pdf:
        a = pdf.pages[0].Annots[0]
        assert int(a.F) == 4 and "/D" not in a.AP


@needs_vera
def test_cli_pdfa_and_preflight(work):
    from pdfcore.cli import main

    src = work("letter")
    assert main(["pdfa", str(src), "--level", "2b"]) == 0
    assert (src.parent / "letter.pdfa2b.pdf").exists()
    assert main(["preflight", str(src.parent / "letter.pdfa2b.pdf"), "--standard", "auto"]) == 0
    assert main(["preflight", str(src)]) == 1


# ---------------------------------------------------------------- accessibility
def test_a11y_untagged(work):
    res = st.a11y(work("letter"), verapdf=False, quiet=True)
    assert not res.ok
    d = res.data
    assert d["checks"]["tagged"] == "fail" and d["checks"]["language"] == "fail"
    assert d["checks"]["title"] == "pass"
    assert 0 <= d["score"] < 70
    assert res.output.name == "letter.a11y.pdf" and res.extra_outputs[0].suffix == ".json"


def test_a11y_tagged_typst(typst_pdfa):
    res = st.a11y(typst_pdfa, verapdf=False, quiet=True)
    d = res.data
    assert d["checks"]["tagged"] == "pass" and d["checks"]["language"] == "pass"
    assert d["checks"]["contrast"] == "pass" and d["checks"]["scanned"] == "pass"
    ro = d["reading_order"]
    assert ro["sample"] and ro["structure_vs_content"] is not None
    assert d["score"] >= 60


@needs_vera
def test_a11y_merges_verapdf_ua(work):
    res = st.a11y(work("letter"), contrast=False, quiet=True)
    assert res.data["checks"]["pdfua_verapdf"] == "fail"
    assert any(f["check"] == "pdfua_verapdf" and "PDF/UA-1" in f["message"] for f in res.data["findings"])
    assert res.data["verapdf_ua1"]["rules"]


def test_a11y_contrast(tmp_path):
    def draw(c, f):
        c.setFont(f["sans"], 11)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(72, 700, "Readable black body text on white paper")
        c.setFillColorRGB(0.82, 0.82, 0.82)
        c.drawString(72, 650, "Very pale grey text that fails contrast")

    res = st.a11y(_rl_pdf(tmp_path / "pale.pdf", draw), verapdf=False, quiet=True)
    assert res.data["checks"]["contrast"] == "fail"
    bad = res.data["contrast_failures"]
    assert bad and all(c["page"] == 1 for c in bad)
    words = {c["text"] for c in bad}
    assert words & {"Very", "pale", "grey", "text", "fails", "contrast"}
    assert not words & {"Readable", "black", "paper"}
    assert all(c["ratio"] < 4.5 for c in bad)


def test_a11y_scanned_page(corpus):
    res = st.a11y(corpus["scanned_letter"], verapdf=False, contrast=False, quiet=True)
    assert res.data["checks"]["scanned"] == "fail"
    assert any(f["check"] == "scanned" and f["page"] == 1 for f in res.data["findings"])


def test_a11y_form_tooltips(corpus):
    res = st.a11y(corpus["form_fillable"], verapdf=False, contrast=False, quiet=True)
    assert res.data["checks"]["forms"] == "fail"
    assert any("tooltip" in f["message"] for f in res.data["findings"])


def test_a11y_fix(work):
    src = work("letter")
    before = src.read_bytes()
    res = st.a11y(src, fix=True, lang="en-GB", title="Quote letter", verapdf=False, contrast=False, quiet=True)
    assert src.read_bytes() == before
    assert res.output.name == "letter.a11y-fixed.pdf"
    with pikepdf.open(res.output) as pdf:
        assert str(pdf.Root.Lang) == "en-GB"
        assert pdf.Root.ViewerPreferences.DisplayDocTitle
        assert str(pdf.docinfo.Title) == "Quote letter"
        assert "/MarkInfo" not in pdf.Root  # untagged: Marked would be a lie
    assert res.data["checks"]["language"] == "pass" and res.data["checks"]["display_doc_title"] == "pass"
    assert any(p.suffix == ".pdf" and "a11y" in p.name for p in res.extra_outputs)


def test_a11y_fix_marks_tagged(tmp_path, typst_pdfa):
    src = tmp_path / "t.pdf"
    with pikepdf.open(typst_pdfa) as pdf:
        del pdf.Root["/MarkInfo"]
        pdf.save(src)
    before = st.a11y(src, verapdf=False, contrast=False, quiet=True)
    assert before.data["checks"]["tagged"] == "fail"
    res = st.a11y(src, fix=True, verapdf=False, contrast=False, quiet=True)
    assert res.data["checks"]["tagged"] == "pass"
    assert any("MarkInfo" in x for x in res.data["fixed"])


def test_cli_registered_and_a11y_cli(work, capsys):
    from pdfcore.cli import main
    from pdfcore.ops import COMMANDS, load_all

    load_all()
    for c in ("preflight", "pdfa", "a11y"):
        assert c in COMMANDS
    assert main(["a11y", str(work("letter")), "--no-verapdf", "--json"]) == 1
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{"):])["checks"]["tagged"] == "fail"
