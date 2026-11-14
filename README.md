# PDF Studio

A permissively licensed (MIT) PDF toolkit — `pdfcore` — plus a set of Claude skills that cover the everyday
Acrobat Pro and Bluebeam Revu jobs: organize, stamp, redact, mark up, compare, OCR, forms, optimize, secure,
typeset professional documents, and work with drawing sets.

**Public edition rule:** every dependency is permissive (MIT / BSD / Apache / MPL-2.0 / PSF / OFL fonts).
No PyMuPDF/MuPDF, no Ghostscript, no GPL/AGPL. CI enforces this with `tools/license_audit.py`.

## Install

```bash
pip install "pdf-studio[all] @ git+https://github.com/thestealthgoat/pdf-studio"
pdfcore install      # Python extras + Tesseract, Typst, veraPDF, LibreOffice (Homebrew on macOS)
pdfcore doctor       # what is installed, what is missing
```

Python 3.11+.

## Use

```bash
pdfcore merge a.pdf b.pdf -o set.pdf
pdfcore stamp report.pdf --footer "Page {page} of {pages}" --bates BAR-{n:06d}
pdfcore redact contract.pdf --pattern ssn --pattern phone
pdfcore compare rev1.pdf rev2.pdf
pdfcore ocr scan.pdf
pdfcore gate output.pdf -v
pdfcore convert drawing.dwg --to pdf          # any format → any format (pdfcore convert --list)
pdfcore plans site-fetch --address "…"        # aerial imagery + OpenStreetMap → site plan (pip install 'pdf-studio[site]')
```

Every op writes a **new file next to the input** (never overwrites) and finishes with the quality gate:
one line, `PASS` or `FAIL p.3: text past right margin (fix: …)`.

```python
from pdfcore.ops import organize, redact
res = organize.merge(["a.pdf", "b.pdf"], out="set.pdf")
print(res.output, res.line())
```

## Layout

```
pdfcore/      engines/ (render, text adapters) · ops/ · gate/ · recipes/ · cli.py
skills/       pdf (front door) · pdf-organize · pdf-markup · pdf-ocr-forms · pdf-make · pdf-standards · pdf-plans
templates/    Typst templates, brand token files, house fonts (Inter, Source Serif 4 — OFL)
corpus/       test-PDF generator + SOURCES.md
tests/
```

## Engines and the local edition

Engines are registered through the `pdfcore.backends` entry-point group. The private `pdf-studio-local`
package adds optional backends for its owner's machine only; the public package never depends on it.

## License

MIT — see `LICENSE`. Third-party licenses: `THIRD_PARTY_LICENSES.md`.
