---
name: pdf-markup
description: Mark up, compare and redact PDFs with pdfcore — highlights, underline/strike, sticky notes, free text, callouts, shapes, clouds, ink, stamps (with real appearance streams), comment lists and summary reports, text-diff and visual overlay comparison of two versions with clouded changes, and TRUE redaction (removes text, image pixels and vector paths; scrubs metadata, hidden layers, comments, attachments).
---

# PDF Studio — markup, compare, redact

Coordinates are points with a top-left origin: `x0,top,x1,bottom` (what `pdfcore search` reports).
Every command writes a new file and ends with `PASS` / `FAIL …`.

## Annotate

```bash
pdfcore annotate f.pdf --kind highlight --search "Contractor" --color yellow
pdfcore annotate f.pdf --kind note --page 2 --at 500,80 --text "Check this clause" --author "M. Lee"
pdfcore annotate f.pdf --kind cloud --page 1 --rect 300,200,450,280 --color red
pdfcore annotate f.pdf --kind callout --page 1 --rect 400,100,560,140 --target 320,180 --text "Field verify"
pdfcore annotate f.pdf --kind stamp --page 1 --rect 420,40,580,90 --text APPROVED
pdfcore annotate f.pdf --spec marks.json              # many at once
# kinds: highlight underline strikeout squiggly note freetext callout square circle line arrow
#        polygon polyline cloud ink stamp   (pages 1-based; --points x,y;x,y… for lines/polygons/ink)
pdfcore annots f.pdf                                  # list (type, page, author, date, text)
pdfcore annots-summary f.pdf                          # comment summary PDF (+ CSV) with thumbnails
pdfcore annots-flatten f.pdf                          # burn into the page
pdfcore annots-export f.pdf / annots-import f.pdf comments.json / annots-delete f.pdf --author X
```

Find target rectangles first with `pdfcore search f.pdf "phrase"`.

## Compare two versions

```bash
pdfcore compare old.pdf new.pdf            # --mode text|visual|both, --dpi 100, --align fit
```

Produces: a text-diff report (insertions, deletions, changes with page refs) and an **overlay PDF**
(old = red, new = blue, unchanged = gray) with changes clouded and numbered. Read the report's summary
counts to the user and open the overlay page with the most changes. Title-block date/revision changes on
every sheet are expected noise — say so instead of listing each.

## Redact (true redaction)

```bash
pdfcore redact f.pdf --pattern ssn --pattern phone --pattern email --dry-run   # ALWAYS dry-run first
pdfcore redact f.pdf --pattern ssn --pattern phone --pattern email
pdfcore redact f.pdf --term "Jordan Blake" --whole-word --label "(b)(6)"
pdfcore redact f.pdf --area 1:72,90,300,110                                   # page:x0,top,x1,bottom
```

Patterns: `ssn`, `phone`, `email`, `ein`, `card` (Luhn-checked). Also `--regex '…'`.

What it does — not a black box drawn on top:
- removes the glyphs under each box from the content stream (neighbours keep their positions),
- paints the covered image pixels black inside the image data, deletes vector paths under the box,
- removes annotations under boxes, strips /ActualText and alt text around removed text,
- scrubs metadata, hidden layers, comments, attachments, JavaScript; removes redacted strings from bookmarks,
  tags and form values (`--keep-metadata`, `--keep-comments`, `--keep-attachments`, `--keep-layers` to keep),
- re-extracts with two engines; the gate FAILS if any text remains in a box or any term remains anywhere.

Rules:
1. Show the dry-run list to the user and get a yes before redacting a real document.
2. Scanned pages have no text: run OCR first (pdf-ocr-forms skill), then redact the OCR output.
3. Never hand back a redacted file whose gate line is FAIL.
4. Render the redacted pages and look at them before reporting.
