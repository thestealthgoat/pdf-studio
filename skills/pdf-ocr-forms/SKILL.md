---
name: pdf-ocr-forms
description: Make scans searchable and work with PDF forms using pdfcore — OCR with deskew and auto-rotate and an invisible text layer; list, fill (with visible appearances), create, auto-detect fields on flat forms, bulk-fill from CSV, flatten, and export form data (JSON, CSV, XFDF).
---

# PDF Studio — OCR and forms

## OCR

```bash
pdfcore ocr scan.pdf                      # → scan.ocr.pdf: deskewed, auto-rotated, searchable
pdfcore ocr scan.pdf -l eng+spa           # languages (tesseract codes)
pdfcore ocr mixed.pdf --keep-original     # keep page images untouched; only add the text layer
pdfcore ocr scan.pdf --pages 3-5 --json   # per-page confidence, rotation and deskew
```

- Pages that already have real text are skipped unless `--force`.
- After OCR, check: `pdfcore search scan.ocr.pdf "<a word you can see on page 1>"` must hit.
- Mean confidence under ~80: try `--dpi 400` or `--clean`, and tell the user which pages are weak.

## Forms

```bash
pdfcore form-list form.pdf --json                     # names, types, values, options, pages
pdfcore form-fill form.pdf --data values.json         # or --set legal_name="Ridgeline Controls" --set certify=Yes
                                                      # --data also takes .csv (first row) or .xfdf
pdfcore form-fill form.pdf --data values.json --flatten
pdfcore form-detect flat.pdf --dry-run                # what fields it would create
pdfcore form-detect flat.pdf                          # → flat.fillable.pdf
pdfcore form-create f.pdf --spec fields.json          # text, multiline, checkbox, radio, dropdown, list, signature
pdfcore form-bulk form.pdf people.csv --name "{legal_name}.pdf" --out-dir filled/ [--merged] [--flatten]
pdfcore form-flatten filled.pdf
pdfcore form-export filled.pdf --format csv           # json | csv | xfdf
```

Rules:
1. Always `form-list` first and map the user's data to the exact field names; show the mapping if any name
   is a guess.
2. Checkboxes take `Yes`/`Off` (or the export value `form-list` shows); radios take one option name;
   dropdowns must use a listed option unless the field allows editing.
3. Render page 1 of the filled file and look at it: values must be visible in their boxes.
4. Flatten only when the user wants a final, non-editable copy — keep the fillable version too.
5. Forms often carry personal data: keep files local and name outputs as the user asks.
