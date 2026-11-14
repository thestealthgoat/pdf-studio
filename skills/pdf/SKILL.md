---
name: pdf
description: Front door for any PDF job — read, edit, organize, stamp, redact, mark up, compare, OCR, fill forms, optimize, secure, sign, convert, make a professional document (estimate, report, proposal, invoice, letter, run sheet…) or work with drawing sets (sheet index, overlay compare, hyperlink callouts, takeoffs). Routes the request to exactly one PDF Studio skill and runs pdfcore.
---

# PDF Studio — front door

Every PDF job goes through `pdfcore` (the PDF Studio toolkit). Pick ONE skill below, load it, and follow it.
Do not call PyMuPDF, Ghostscript, pdftk, qpdf or raw Python PDF libraries directly.

## Find pdfcore

```bash
command -v pdfcore || echo ~/Developer/pdf-studio/.venv/bin/pdfcore
pdfcore doctor          # if anything is missing: pdfcore install
```

## Route

| The user wants to… | Skill |
|---|---|
| merge, split, insert, delete, reorder, rotate, crop, extract pages; bookmarks/TOC, links, metadata, attachments, page labels; headers/footers, page X of Y, Bates, watermarks, approval stamps; shrink/optimize; passwords/permissions; batch a folder (recipes); convert Office/images → PDF; PDF → images; pull text, tables (CSV/XLSX), images, vectors | **pdf-organize** |
| highlight, comment, sticky notes, shapes, clouds, callouts, stamps as annotations; comment summary; **compare two versions**; **redact** | **pdf-markup** |
| make a scan searchable (OCR); read, fill, create, auto-detect, bulk-fill, flatten or export **forms** | **pdf-ocr-forms** |
| **create** a new document: estimate/quote, invoice, report, proposal, letter, memo, certificate, run sheet, production sheet, booklet, book manuscript, drawing sheet | **pdf-make** |
| sign / certify / timestamp / validate signatures; PDF/A, PDF/UA, preflight; accessibility check | **pdf-standards** |
| drawing sets and plans: sheet index, revision overlay compare, batch link callouts, scale + measure, takeoff, slip sheets, revision clouds, issue stamps, tiling, layers, DXF ↔ PDF, schematics | **pdf-plans** |

If a request spans two skills (e.g. "OCR this scan, then redact SSNs"), run them in order, feeding each
output file to the next step.

## Rules for every job

1. **Never overwrite the input.** Every command writes a new file next to it (`name.<op>.pdf`). Say where.
2. **Read the last line.** Every command ends with `PASS` or `FAIL p.N: problem (fix: …)`. On FAIL, apply the fix
   or explain it; never hand over a failing file silently.
3. **Look at what you made.** For anything you generate or visibly change, render it and read the image:
   `pdfcore gate out.pdf --png-dir /tmp/check -v`, then open a PNG.
4. Encrypted input: add `--password …` (ask the user for it; never guess).
5. Pages on the command line are 1-based: `--pages 1-3,5,8-`, `odd`, `even`, `last`, `-1`.
6. Private documents stay local. Do not upload PDFs anywhere.

## Quick answers (no skill needed)

```bash
pdfcore text file.pdf                 # plain text in reading order → file.text.txt
pdfcore search file.pdf "term"        # hits with page + rectangles (JSON)
pdfcore meta file.pdf                 # title, author, page count, metadata
pdfcore gate file.pdf -v              # quality report on any PDF
```
