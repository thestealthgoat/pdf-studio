from __future__ import annotations

import csv
import json

import numpy as np
import pdfplumber
import pikepdf
import pypdfium2 as pdfium
import pytest

from pdfcore.cli import main
from pdfcore.ops import forms
from pdfcore.types import Rect

VALUES = {
    "legal_name": "Acme Widgets LLC", "business_name": "Acme", "address": "12 Main Street",
    "city_state_zip": "Springfield, IL 62701", "tin": "12-3456789", "phone": "(555) 123-4567",
    "email": "ops@acme.example", "entity": "llc", "certify": True, "state": "OK",
}


def _render(path, dpi=100, forms_on=True) -> np.ndarray:
    doc = pdfium.PdfDocument(str(path))
    try:
        if forms_on:
            doc.init_forms()
        return np.asarray(doc[0].render(scale=dpi / 72, may_draw_forms=forms_on).to_pil().convert("L"), dtype=np.int16)
    finally:
        doc.close()


def _crop(img: np.ndarray, r: Rect, dpi=100) -> np.ndarray:
    s = dpi / 72
    return img[int(r.top * s) + 2: int(r.bottom * s) - 1, int(r.x0 * s) + 2: int(r.x1 * s) - 1]


def test_list_fields(corpus):
    infos = {f.name: f for f in forms.list_fields(corpus["form_fillable"])}
    assert set(VALUES) <= set(infos)
    assert infos["legal_name"].type == "text"
    assert infos["entity"].type == "radio" and set(infos["entity"].options) == {"sole", "llc", "corp", "other"}
    assert infos["certify"].type == "checkbox"
    assert infos["state"].type == "choice" and "Combo" in infos["state"].flags and "NM" in infos["state"].options
    assert infos["legal_name"].page == 0 and infos["legal_name"].rect.width > 400
    d = infos["entity"].as_dict()
    json.dumps(d)
    assert len(d["widgets"]) == 4


def test_fill_reads_back_and_renders(work):
    src = work("form_fillable")
    res = forms.fill(src, VALUES, quiet=True)
    assert res.ok, res.line()
    assert res.data["unknown"] == []
    got = forms.read_values(res.output)
    for k, v in VALUES.items():
        want = "Yes" if v is True else v
        assert got[k] == want, k
    # value visible: pixels inside each field's rect change versus the empty form
    before, after = _render(src), _render(res.output)
    infos = {f.name: f for f in forms.list_fields(res.output)}
    for name in ("legal_name", "email", "state", "certify"):
        r = infos[name].rect
        diff = np.abs(_crop(after, r) - _crop(before, r))
        assert (diff > 60).sum() > 15, name
    llc = next(w for w in infos["entity"].widgets if w.on == "llc")
    assert (np.abs(_crop(after, llc.rect) - _crop(before, llc.rect)) > 60).sum() > 3
    # every appearance font is embedded (gate passed) and NeedAppearances is not relied on
    with pikepdf.open(res.output) as pdf:
        assert "/NeedAppearances" not in pdf.Root.AcroForm


def test_fill_then_flatten(work):
    filled = forms.fill(work("form_fillable"), VALUES, quiet=True).output
    res = forms.flatten(filled, quiet=True)
    assert res.ok, res.line()
    with pikepdf.open(res.output) as pdf:
        assert "/AcroForm" not in pdf.Root
        assert not any(a.get("/Subtype") == "/Widget" for a in pdf.pages[0].obj.get("/Annots", []) or [])
    with pdfplumber.open(res.output) as pdf:
        t = pdf.pages[0].extract_text()
    for v in ("Acme Widgets LLC", "Springfield, IL 62701", "12-3456789", "ops@acme.example", "OK"):
        assert v in t
    # flattened values render without any form support
    img = _render(res.output, forms_on=False)
    r = forms.list_fields(filled)[0].rect
    assert (_crop(img, r) < 128).sum() > 30


def test_fill_errors(work):
    src = work("form_fillable")
    with pytest.raises(ValueError):
        forms.fill(src, {"entity": "partnership"}, quiet=True)
    with pytest.raises(ValueError):
        forms.fill(src, {"state": "CA"}, quiet=True)
    with pytest.raises(KeyError):
        forms.fill(src, {"nope": "x"}, strict=True, quiet=True)
    res = forms.fill(src, {"nope": "x", "phone": "1"}, quiet=True)
    assert res.data["unknown"] == ["nope"]


def test_detect_flat_form(work):
    src = work("form_flat")
    res = forms.detect_fields(src, quiet=True)
    assert res.ok, res.line()
    found = res.data["fields"]
    texts = {f["name"] for f in found if f["type"] == "text"}
    assert {"project_name", "project_number", "visited_by", "date_of_visit", "weather"} <= texts
    checks = {f["name"] for f in found if f["type"] == "checkbox"}
    assert checks == {"safety_concerns_noted", "photos_taken", "follow_up_required"}
    assert [f["name"] for f in found if f["type"] == "multiline"] == ["work_observed"]
    infos = {f.name: f for f in forms.list_fields(res.output)}
    assert "Multiline" in infos["work_observed"].flags
    # the generated form is fillable
    f2 = forms.fill(res.output, {"project_name": "Riverside", "photos_taken": "yes", "work_observed": "Line one.\nLine two."}, quiet=True)
    assert f2.ok, f2.line()
    vals = forms.read_values(f2.output)
    assert vals["project_name"] == "Riverside" and vals["photos_taken"] == "Yes"
    # dry run writes nothing
    dry = forms.detect_fields(src, dry_run=True, quiet=True)
    assert dry.output is None and len(dry.data["fields"]) == len(found)


def test_bulk_fill_csv(work, tmp_path):
    src = work("form_fillable")
    rows = [
        {"legal_name": "Acme Widgets LLC", "tin": "11-1111111", "entity": "llc", "certify": "yes", "state": "TX"},
        {"legal_name": "Bravo Corp", "tin": "22-2222222", "entity": "corp", "certify": "no", "state": "LA"},
        {"legal_name": "Charlie Smith", "tin": "33-3333333", "entity": "sole", "certify": "yes", "state": "NM"},
    ]
    csv_path = tmp_path / "rows.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out_dir = tmp_path / "out"
    res = forms.fill_from_csv(src, csv_path, name="{legal_name}.pdf", out_dir=out_dir, merged=True, quiet=True)
    assert res.ok, res.line()
    outs = sorted(p.name for p in out_dir.glob("*.pdf") if "merged" not in p.name)
    assert outs == ["Acme Widgets LLC.pdf", "Bravo Corp.pdf", "Charlie Smith.pdf"]
    assert forms.read_values(out_dir / "Bravo Corp.pdf")["entity"] == "corp"
    assert forms.read_values(out_dir / "Bravo Corp.pdf")["certify"] == "Off"
    with pdfplumber.open(res.data["merged"]) as pdf:
        assert len(pdf.pages) == 3
        assert "Charlie Smith" in pdf.pages[2].extract_text()


def test_export_formats_roundtrip(work, tmp_path):
    filled = forms.fill(work("form_fillable"), VALUES, quiet=True).output
    j = forms.export_data(filled, fmt="json", quiet=True)
    data = json.loads(j.output.read_text())
    assert data["legal_name"] == "Acme Widgets LLC" and data["entity"] == "llc"
    c = forms.export_data(filled, fmt="csv", quiet=True)
    with c.output.open() as fh:
        row = next(csv.DictReader(fh))
    assert row["tin"] == "12-3456789"
    x = forms.export_data(filled, fmt="xfdf", quiet=True)
    assert x.output.suffix == ".xfdf"
    parsed = forms.parse_xfdf(x.output)
    assert parsed["email"] == "ops@acme.example" and parsed["state"] == "OK"
    # XFDF fills a blank copy
    again = forms.fill(work("form_fillable"), x.output, quiet=True)
    assert forms.read_values(again.output)["city_state_zip"] == "Springfield, IL 62701"


def test_create_fields(work):
    src = work("letter")
    specs = [
        {"name": "ref", "type": "text", "page": 0, "rect": [380, 60, 560, 80], "value": "PO-1182"},
        {"name": "notes", "type": "multiline", "page": 0, "rect": [72, 640, 540, 720], "font_size": 10},
        {"name": "approved", "type": "checkbox", "page": 0, "rect": [72, 730, 84, 742], "value": True},
        {"name": "priority", "type": "radio", "page": 0,
         "widgets": [{"value": "low", "rect": [300, 730, 312, 742]}, {"value": "high", "rect": [340, 730, 352, 742]}], "value": "high"},
        {"name": "region", "type": "choice", "page": 0, "rect": [380, 84, 480, 102], "options": ["North", "South"], "value": "South"},
        {"name": "dept", "type": "list", "page": 0, "rect": [480, 640, 560, 700], "options": ["Ops", "Eng", "Sales"]},
        {"name": "sig", "type": "signature", "page": 0, "rect": [380, 745, 560, 775]},
    ]
    res = forms.add_fields(src, specs, quiet=True)
    assert res.ok, res.line()
    infos = {f.name: f for f in forms.list_fields(res.output)}
    assert {i: infos[i].type for i in infos} == {
        "ref": "text", "notes": "text", "approved": "checkbox", "priority": "radio", "region": "choice",
        "dept": "choice", "sig": "signature"}
    assert infos["ref"].value == "PO-1182" and infos["priority"].value == "high" and infos["approved"].value == "Yes"
    assert infos["region"].value == "South" and "Combo" in infos["region"].flags and "Combo" not in infos["dept"].flags
    assert abs(infos["ref"].rect.x0 - 380) < 0.01 and abs(infos["ref"].rect.top - 60) < 0.01
    img = _render(res.output)
    assert (_crop(img, infos["ref"].rect) < 128).sum() > 20
    filled = forms.fill(res.output, {"notes": "Deliver to dock 4 before noon.", "dept": "Eng", "priority": "low"}, quiet=True)
    assert filled.ok, filled.line()
    assert forms.read_values(filled.output)["dept"] == "Eng"
    with pytest.raises(ValueError):
        forms.add_fields(res.output, [{"name": "ref", "type": "text", "page": 0, "rect": [0, 0, 10, 10]}], quiet=True)


def test_cli(work, capsys):
    src = work("form_fillable")
    assert main(["form-list", str(src), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(f["name"] == "certify" for f in listed)
    assert main(["form-fill", str(src), "--set", "legal_name=CLI Co", "--set", "certify=yes"]) == 0
    out = src.with_name(f"{src.stem}.filled.pdf")
    assert forms.read_values(out)["legal_name"] == "CLI Co"
    assert main(["form-export", str(out), "--format", "xfdf"]) == 0
