---
name: pdf-plans
description: Drawing sets and plans with pdfcore (Bluebeam-style) — read a set (one-lines, panel schedules, general notes, equipment lists to tables), sheet index/drawing log from title blocks, revision overlay compare with clouds and delta report by sheet, batch-link sheet and detail callouts, scale calibration and measurement with standard /Measure dictionaries, takeoff to XLSX, slip sheets, revision clouds, issue stamps, seals, sheet sizes and tiling, layers, DXF ↔ PDF, and electrical schematics.
---

# PDF Studio — drawings and plans

Sheets are referred to by sheet number (E-101), not page number, whenever possible.

## Read a set

```bash
pdfcore plans index set.pdf                    # sheet no., title, rev, date from title blocks → XLSX + bookmarks
pdfcore plans index set.pdf                    # also writes set.indexed.pdf (bookmarks by discipline; --no-bookmark, --no-xlsx)
pdfcore plans read set.pdf                     # finds one-lines, panel schedules, notes, equipment lists → tables
pdfcore tables set.pdf --pages 3 --format xlsx # a specific schedule
```

Always confirm the index against two or three rendered title blocks before trusting it.

## Revisions

```bash
pdfcore plans compare rev0.pdf rev1.pdf        # matches sheets by number; overlay per sheet (old red, new blue),
                                               # clouds + delta tags, delta report by sheet (PDF + XLSX)
pdfcore plans slipsheet set.pdf revised.pdf    # replace sheets by sheet number, carry markups over
pdfcore plans cloud set.pdf --sheet E-101 --rect … --delta 1
pdfcore plans issue set.pdf --stamp "ISSUED FOR CONSTRUCTION" --date 2026-11-13 [--seal seal.png]
```

Title-block date/rev changes appear on every sheet — summarize them once, not per sheet.

## Links

```bash
pdfcore plans link set.pdf                     # "SEE E-201", "3/E-501", sheet-index entries → live links
```

## Measure and takeoff

```bash
pdfcore plans scale set.pdf --sheet E-301 --scale '1/4"=1'"'"'-0"'      # writes /Measure viewports (other viewers read it)
pdfcore plans scale set.pdf --sheet E-301 --calibrate x0,y0,x1,y1=16ft  # from a known dimension
pdfcore plans scale set.pdf --sheet E-301 --calibrate bar               # from the sheet's scale bar
pdfcore plans measure set.pdf --sheet E-301 --length x0,y0,x1,y1 …      # also --area / --perimeter / --count
pdfcore plans takeoff set.pdf takeoff.yaml     # totals by type → XLSX
```

State the scale you used and where it came from (title block, scale bar, calibration) with every measurement.

## Sheets, layers, CAD

```bash
pdfcore plans sizes set.pdf                             # named sheet size of every page
pdfcore plans resize set.pdf --to ansi-b [--factor 50%]  # scale kept in /Measure; ARCH D→ANSI B fits (≈46%), not 50%
pdfcore plans tile set.pdf --paper letter --overlap 0.5in   # tiled printing with crop marks and A1, A2… labels
pdfcore plans layers set.pdf [--off "NOTES"] [--flatten]
pdfcore plans dxf2pdf drawing.dxf --size arch-d --lineweights by-layer   # .dwg too (dwg2pdf); --mono for print
pdfcore plans dxf2pdf drawing.dwg --size ansi-b --title-block --sheet M-101 --title "…" --project "…"
pdfcore plans pdf2dxf set.pdf --sheet E-101             # vector geometry for ProgeCAD
pdfcore plans schematic ladder.yaml                     # ladder/wiring diagram with wire numbers, cross-refs, BOM
#   example: pdfcore/ops/plans/examples/ladder-motor-starter.yaml  (--terminal-view strip)
pdfcore make drawing-sheet sheet.yaml                   # blank sheet with title block
```

DWG needs a converter: the local edition uses LibreDWG; elsewhere the free ODA File Converter if the user
installed it. If neither exists, ask for a DXF export instead.

## Drawing rules (for anything you produce)

- Line weights by element type; text at least 3/32 in (≈ 7 pt) at printed size, notes 1/8 in.
- Scale bar and north arrow where relevant; sheet numbers by discipline (E, M, P, …).
- One title block per project; keep the revision table current.

The reading functions live in `pdfcore.ops.plans` and can be imported on their own
(e.g. by UL 508A panel skills reading bid specs and engineer drawings).
