---
name: pdf-organize
description: Organize and finish existing PDFs with pdfcore — merge/split/insert/delete/reorder/rotate/crop/extract pages, page labels, bookmarks (auto TOC), links, metadata, attachments, headers/footers, page X of Y, Bates numbering, watermarks, approval stamps, optimize/shrink, passwords and permissions, batch recipes, Office/image conversion, and extracting text, tables (CSV/XLSX), images and vector paths.
---

# PDF Studio — organize, finish, extract

All commands write a new file next to the input and end with `PASS` / `FAIL p.N: … (fix: …)`.
Run `pdfcore <command> -h` for every option.

## Pages

```bash
pdfcore merge a.pdf b.pdf c.pdf -o set.pdf          # one bookmark per file (--flat-bookmarks to merge outlines)
pdfcore split big.pdf --ranges "1-3,4-10,11-"        # or --every 5  or --bookmarks
pdfcore insert main.pdf addendum.pdf --after 4
pdfcore delete-pages f.pdf "2,7-9"
pdfcore reorder f.pdf "3,1,2,4-"                      # or: reverse
pdfcore extract-pages f.pdf "5-8"
pdfcore rotate f.pdf 90 --pages even
pdfcore crop f.pdf --margins 0.5in                   # t,r,b,l also accepted; --box x0,y0,x1,y1; --trim
pdfcore page-labels f.pdf --range 1-4:roman --range 5-:decimal:E-:1
```

## Navigation and metadata

```bash
pdfcore bookmarks f.pdf --auto [--levels 2]          # TOC from headings; --list, --json, --set outline.yaml, --clear
pdfcore autolink f.pdf                               # bare URLs and e-mails become live links
pdfcore link f.pdf --page 1 --rect 72,90,200,104 --to-page 5      # or --uri https://… / --dest name
pdfcore meta f.pdf --title "…" --author "…" --subject "…" --lang en-US
pdfcore attach f.pdf --add takeoff.xlsx   |  --list  |  --extract outdir/  |  --remove name
```

## Stamping

```bash
pdfcore stamp f.pdf --footer-center "Page {page} of {pages}" --header-right "{title}"
pdfcore stamp f.pdf --page-numbers                    # "Page X of Y" at footer-center
#   tokens: {page} {pages} {date} {title} {file} {label}; positions header-/footer- left/center/right
pdfcore bates f.pdf more.pdf --prefix BAR- --start 1 --digits 6        # continues across files
pdfcore watermark f.pdf "DRAFT" --opacity 0.12                         # --image logo.png; --behind; --angle horizontal
pdfcore approve f.pdf "ISSUED FOR CONSTRUCTION" --name "M. Lee" --date --pages 1 --position top-right
```

## Size and security

```bash
pdfcore optimize f.pdf --preset screen               # 150 dpi; print = 300 dpi; ebook; lossless
pdfcore optimize f.pdf --target-mb 10                # tries stronger settings until under 10 MB
pdfcore encrypt f.pdf --user-password … --owner-password … --deny copy,modify   (AES-256)
pdfcore decrypt f.pdf --password …
pdfcore security f.pdf                               # what is protected and how
```

Ask the user for passwords; never invent or store them.

## Extract

```bash
pdfcore render f.pdf --dpi 200 --pages 1-3           # PNGs in f_pages/
pdfcore text f.pdf [--level words|lines|blocks|chars]   # JSON with positions (default plain text)
pdfcore search f.pdf "Bid 26-114" [--regex --case --whole-word]
pdfcore tables f.pdf --format xlsx                   # one sheet per table, numbers as numbers
pdfcore extract-images f.pdf
pdfcore vectors f.pdf --pages 2                      # lines/curves/rects → JSON + SVG
```

Check extracted tables against the page image before trusting totals.

## Convert

```bash
pdfcore convert spec.docx                            # Office/text via LibreOffice
pdfcore images-to-pdf scan1.jpg scan2.jpg -o scans.pdf
pdfcore pdf-to-images f.pdf --dpi 150
```

## Batch (Action Wizard equivalent)

A recipe is YAML: `steps:` run in order on every file; only the final file is written.

```yaml
name: bid-set-prep
input: "*.pdf"
steps:
  - {op: organize.rotate, pages: "2", degrees: 90}
  - {op: stamp.stamp, texts: {footer-center: "Page {page} of {pages}"}}
  - {op: navigate.auto_bookmarks}
```

```bash
pdfcore recipe --list                                # bundled examples
pdfcore recipe bid-set-prep folder/ --dry-run        # always dry-run first
pdfcore recipe my.yaml folder/ --out-dir done/
```

## Done

Report the output path(s) and the PASS/FAIL line. For visible changes (stamps, rotation, crop), render page 1
and look at it before saying it is finished.
