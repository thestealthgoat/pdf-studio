# Changelog

## v0.5.1 — words/1 parity fixes from BAR's pdf.js spike (2026-11-14)
- `size` is the effective font size (Tf × text-matrix y-scale); QuickBooks' 0.75-scaled text now reports 8 pt, not 10.66.
- Line-final hyphens (PDFium U+0002) are `-`.
- Non-embedded fonts (incl. standard 14) use fixed 0.8/−0.2 ascent/descent, so every reader gets the same boxes.
- Word gap 0.15·size (was 0.2); whitespace is the Unicode White_Space set, spelled out.
- Grouping runs per writing direction in that direction's own frame; blocks are ordered together in the dominant
  frame. `/Rotate` no longer changes words/lines/blocks, and vertical title-block text on drawing sheets reads as
  words instead of single letters.
- Spec (`docs/pdfcore-words-1.md`) and schema text updated; only `qbo-lookalike-helvetica.words.json` changed.

## v0.5.0 — BAR integration: words/1 + standalone sidecar, Windows support (2026-11-14)
- **`pdfcore-words/1`** (`pdfcore/words.py`): word-level JSON with positions for BAR Estimating. JSON Schema in
  `schemas/`, exact rules in `docs/pdfcore-words-1.md` (loose glyph boxes, CropBox-origin after /Rotate, ligatures +
  NFC, 0.2·size word gap, horizontal-first XY-cut so tables read row by row), byte-deterministic output.
- **BAR fixtures** (`fixtures/bar/`): four invented QuickBooks-style estimates with expected JSON, reproducible bytes.
- **Standalone sidecar** (`pdfcore/sidecar.py`, `tools/build_sidecar.py`, `.github/workflows/sidecar.yml`):
  `pdfcore text <in> --level words --format json -o <out>` for darwin-arm64 and win32-x64, built with Nuitka 2.8.10
  (Apache-2.0; 4.x is AGPL and is refused). Exit codes 0/1/2/3 with one JSON line on stderr, atomic output, no
  network, watchdog timeout, ~13.5 MB zipped, ~0.5 s cold start. Release assets: zips, schema, `SHA256SUMS`.
- **Windows support**: CI on windows-latest, tool discovery in Program Files, UTF-8 console and file I/O.
- **Native DWG writer** (R2000) and **PDF repair** chain (qpdf → pypdf → pdfium → raw-object salvage).

## v0.4.1 — BAR-safe site plans, deeper license audit (2026-11-13)
- Site plans run on numpy only: scikit-image and SciPy removed (their wheels bundled LGPL libquadmath).
  The `site` extra is now pyproj only. Same accuracy (building IoU 0.968, road centerlines ≤ 0.2 m median),
  ~30% faster extraction.
- License audit also scans every bundled native library (.so/.dylib/.dll) in installed wheels against a license
  table: GCC runtime exception allowed; LGPL, GPL and unknown libraries fail CI.
- macOS is the only supported platform for CI and the installer (no Linux target).

## v0.4.0 — conversions, native DWG, site plans (2026-11-13)
- **`pdfcore convert`**: one front door for every format pair — images (PNG/JPG/TIFF/GIF/BMP/WebP/AVIF/JP2/HEIC),
  SVG, DXF/DWG/PLT, Markdown, text, HTML, Office → PDF; PDF → images, SVG, DXF, DWG (with a writer), Word,
  Markdown, HTML, XLSX/CSV, text, JSON; image ↔ image; DWG ↔ DXF; batch, folders, `--merge`, `--list`.
- **Native DWG reader** (clean-room from the ODA published spec): R2000–R2018 in pure Python, no ODA or LibreDWG
  needed; matches a black-box oracle on 28 sample drawings; fuzz-tested. External converters remain as fallback.
- **Site plans from imagery** (`pip install 'pdf-studio[site]'`): `plans site-fetch` (USGS NAIP + OpenStreetMap,
  State Plane feet), `site-from-image` (your screenshots / drone photos with calibration), `site-extract`
  (buildings, roads, pavement, trees, water), `site-plan` (layered DXF + PDF at engineering scale, legend, north
  arrow, /Measure), `site-add` (proposed infrastructure + takeoff XLSX).

## v0.3.0 — Phases 3 & 4 (2026-11-13)

### Phase 3 — drawings and plans (`pdfcore plans …`)
- **Sheets**: title-block reading (vertical, rotated and mirrored text), sheet index / drawing log XLSX + bookmarks by
  discipline + page labels, set reading (one-lines, panel schedules, general notes, equipment lists → XLSX/JSON).
- **Compare by sheet number**: red/blue overlay, revision clouds + delta tags, title-block changes noted not clouded,
  delta report PDF + XLSX. **Batch link**: SEE/REFER TO references, detail callouts (3/E-501), index rows.
  **Slip sheets** by sheet number keeping markups.
- **Measure**: scale parsing and calibration (dimension or scale bar), standard /VP /Measure viewports, length / area /
  perimeter / count with measurement annotations, takeoff → XLSX with SUMIFS, sheet sizes, resize keeping scale, tiling.
  **Layers** list / on / off / flatten.
- **CAD**: DXF / DWG / HPGL → PDF (line weights, plot to scale, layouts, title block, mono), PDF → DXF for ProgeCAD,
  ladder schematics (wire numbers, cross-references, BOM, terminals, XLSX). DWG via the `dwg` engine (ODA if installed;
  LibreDWG in the private local edition).
- **Issue**: revision clouds + delta tags, issue stamps, seal placement, revision-table rows.
- Verified on a real 50-sheet public bid set: 49/50 sheet numbers read, 169 links, compare flags exactly the edited sheets.

### Phase 4 — signatures, standards, accessibility
- `sign` / `certify` / `verify` / `timestamp`: PAdES B-B → B-LTA (pyHanko), visible appearances in Inter, DocMDP.
- `preflight` (veraPDF rule report), `pdfa` (existing PDF → PDF/A best effort, honest about remaining failures),
  `a11y` (tags, language, headings, alt text, tables, reading order, contrast, PDF/UA findings, score).
- Done-when: a signed PDF/A-2b document validates in veraPDF (tested at B-T and B-LTA).

### Also
- Brand system: private brand directories, brand fonts, display faces, wordmark logos, accent contrast fallback;
  the gate fails if Typst substitutes a fallback font. Drawing-sheet text floors at 7 pt.

## v0.2.0 — Phases 1 & 2 (2026-11-13)

### Phase 1 — Acrobat / PyMuPDF parity (`pdfcore` ops)
- **Organize**: merge (per-file bookmarks), split (ranges / every N / bookmarks), insert, delete, reorder, extract,
  duplicate, rotate, crop (rotation-aware), page labels. Moving pages rebuilds bookmarks, links, fields, labels.
- **Navigate**: bookmarks list/set/auto-from-headings, links (internal, URI, named dests), autolink, metadata
  (Info + XMP + /Lang), attachments.
- **Stamp**: headers/footers (6 positions, tokens), page X of Y, Bates across files (+ metadata), watermarks
  (ExtGState opacity), approval stamps. Rotation- and CropBox-aware overlay engine.
- **Redact (true)**: glyph-level removal from content streams and form XObjects, image pixel redaction,
  vector cut/clip, annotation removal, ActualText stripping, scrubbing (metadata, hidden layers, comments,
  attachments, JS, thumbnails, strings in bookmarks/tags/fields), full rewrite. Patterns ssn/phone/email/ein/card.
  Gate re-extracts with pdfplumber and pdfium. Works on rotated pages.
- **Markup**: 15 annotation types with our own appearance streams; list/delete/flatten/export/import; comment
  summary PDF + CSV.
- **Compare**: text-diff report; red/blue overlay with revision clouds and delta tags; reusable `find_changes`.
- **OCR**: OSD auto-rotate, deskew, invisible Inter text layer, per-page confidence.
- **Forms**: list, fill with real appearances, create, auto-detect on flat forms, CSV bulk fill, flatten,
  export JSON/CSV/XFDF.
- **Optimize**: dpi-aware downsampling, JPEG vs lossless choice, dedupe, TrueType re-subsetting, size budget.
- **Security**: AES-256 encrypt with permissions, decrypt, change passwords, info.
- **Extract**: render, text (chars/words/lines/blocks/plain), search, vector paths → JSON/SVG, images, tables → CSV/XLSX.
- **Convert**: Office via LibreOffice, images → PDF, PDF → images. **Recipes**: YAML batch chains with dry-run.
- **Skills**: `pdf` front door + pdf-organize, pdf-markup, pdf-ocr-forms, pdf-make, pdf-standards, pdf-plans;
  `pdfcore route` with a 42-case routing test.

### Phase 2 — professional documents (`pdfcore make`)
- Typst house style: sidehead grid (1 in margins, ≈66-character measure), 4-size scale, Source Serif 4 + Inter,
  tabular figures, ruled tables with repeating headers, running header/footer with page X of Y, tagged PDF.
- Templates: estimate, invoice, report, proposal, letter, memo, certificate, run sheet, production sheet,
  booklet, manuscript (6×9, justified + hyphenated), drawing sheet with title block (ANSI/ARCH).
- Brand token files with WCAG contrast check; exact Decimal money; Markdown prose fields; PDF/A via `--standard`.

## v0.1.0 — Phase 0 (2026-11-13)
- `pdfcore` package skeleton: engines registry (`pdfcore.backends` entry points for the local edition), op registry, CLI.
- Builtin engines: pypdfium2 (render), pdfplumber (text, words, vector paths).
- Quality gate: render + blank pages, font embedding / Type 3 / missing glyphs (counts only fonts that draw glyphs),
  clipped text, margins and heading-at-foot (typeset profile), links/bookmarks resolve, metadata, image dpi,
  size budget, redaction re-extraction (two engines), veraPDF. Baseline comparison: problems already in the input warn, new ones fail.
- `pdfcore doctor`, `pdfcore install`, `pdfcore gate`, `pdfcore engines`.
- License audit (`tools/license_audit.py`) + CI on Linux/macOS, Python 3.11/3.12.
- House fonts bundled (Inter 4.1, Source Serif 4.005 — OFL).
- Self-generated test corpus (16 files) with `corpus/SOURCES.md`.
