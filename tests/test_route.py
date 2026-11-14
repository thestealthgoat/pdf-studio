"""The front door must send each kind of request to the right skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdfcore.route import route

CASES = [
    ("Combine these three PDFs into one bid package", "pdf-organize"),
    ("split the spec book into one file per section", "pdf-organize"),
    ("rotate pages 4 and 5 so they read the right way", "pdf-organize"),
    ("delete the blank pages at the end", "pdf-organize"),
    ("add page X of Y footers to the submittal", "pdf-organize"),
    ("Bates number all the discovery documents starting at BAR-000100", "pdf-organize"),
    ("put a DRAFT watermark on every page", "pdf-organize"),
    ("this file is 80 MB, make it small enough to email", "pdf-organize"),
    ("password protect the contract so they can't copy text", "pdf-organize"),
    ("build bookmarks from the headings", "pdf-organize"),
    ("pull the tables out of this report into Excel", "pdf-organize"),
    ("convert this Word doc to PDF", "pdf-organize"),
    ("do the same cleanup on every PDF in this folder", "pdf-organize"),
    ("set the title and author metadata", "pdf-organize"),
    ("attach the takeoff spreadsheet to the PDF", "pdf-organize"),
    ("highlight every mention of liquidated damages", "pdf-markup"),
    ("add a sticky note on page 3 asking about the warranty", "pdf-markup"),
    ("compare rev 2 of the contract with rev 3 and tell me what changed", "pdf-markup"),
    ("redact all social security numbers and phone numbers", "pdf-markup"),
    ("black out the client's name before we send it", "pdf-markup"),
    ("export a summary of all the comments", "pdf-markup"),
    ("make this scanned letter searchable", "pdf-ocr-forms"),
    ("OCR the old permit scans", "pdf-ocr-forms"),
    ("fill out the W-9 with our company info", "pdf-ocr-forms"),
    ("turn this flat site visit report into a fillable form", "pdf-ocr-forms"),
    ("fill the registration form once per row of this CSV", "pdf-ocr-forms"),
    ("make an estimate for the Riverside pump station panel", "pdf-make"),
    ("create an invoice for progress billing 2", "pdf-make"),
    ("write a proposal for the control panel upgrade", "pdf-make"),
    ("draft a letter to the engineer acknowledging addendum 1", "pdf-make"),
    ("build the run sheet for Sunday night", "pdf-make"),
    ("make a certificate of completion for Jordan", "pdf-make"),
    ("digitally sign the submittal with my certificate", "pdf-standards"),
    ("convert the closeout manual to PDF/A for archiving", "pdf-standards"),
    ("check whether this PDF is accessible to screen readers", "pdf-standards"),
    ("verify the signature on the signed contract", "pdf-standards"),
    ("make a sheet index from the title blocks of this drawing set", "pdf-plans"),
    ("overlay compare rev 0 and rev 1 of the electrical drawings", "pdf-plans"),
    ("hyperlink all the detail callouts like 3/E-501 across the set", "pdf-plans"),
    ("measure the conduit run on E-301 at 1/4 inch scale", "pdf-plans"),
    ("pull the panel schedules off the drawings into a spreadsheet", "pdf-plans"),
    ("convert this DXF to PDF with line weights", "pdf-plans"),
]


@pytest.mark.parametrize("request_text,expected", CASES)
def test_route(request_text, expected):
    assert route(request_text) == expected


def test_skills_exist_and_are_short():
    root = Path(__file__).resolve().parents[1] / "skills"
    names = {"pdf", "pdf-organize", "pdf-markup", "pdf-ocr-forms", "pdf-make", "pdf-standards", "pdf-plans"}
    for n in names:
        p = root / n / "SKILL.md"
        text = p.read_text()
        assert text.startswith("---\nname: " + n + "\n"), n
        assert len(text.splitlines()) < 200, n
        assert "pdfcore" in text
        for banned in ("import fitz", "import pymupdf", "gs -sdevice", "pdftk cat"):
            assert banned not in text.lower()
