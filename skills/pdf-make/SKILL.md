---
name: pdf-make
description: Create professional new PDFs from templates with pdfcore make (Typst) — estimates/quotes, invoices, reports, proposals, letters, memos, certificates, run sheets, production sheets, booklets, book manuscripts and drawing sheets with title blocks — in a consistent house style and per-brand colors, fonts and logos.
---

# PDF Studio — make documents

```bash
pdfcore make <template> data.yaml [--brand default] [--standard pdfa-2b] [--png-dir /tmp/look]
```

Templates: `estimate` `invoice` `report` `proposal` `letter` `memo` `certificate` `runsheet` `production`
`booklet` `manuscript` `drawing-sheet`. Start from the example data for the template:
`~/Developer/pdf-studio/templates/examples/<template>.yaml` — copy it next to where the output belongs, edit it,
then run `make`. Output is `data.pdf` beside the YAML (never overwrites; adds `-2`).

## Workflow

1. Copy the example YAML; fill it with the user's facts. Never invent prices, quantities, names, dates or
   terms — ask, or leave a visible `TBD`.
2. Prose fields (`summary`, `body`, list items) take Markdown: `# Heading`, `**bold**`, `_italic_`, `- list`,
   `1. list`, `[text](https://…)`. Write `$` normally.
3. `pdfcore make <template> data.yaml --png-dir /tmp/look`
4. The gate line must be `PASS`. Then **read every page PNG** and check: nothing cramped or overlapping,
   tables not split awkwardly, totals right, no orphaned heading, names spelled right.
5. Fix the data (shorter descriptions, fewer items per section) and re-run until it looks right.
6. Report the path, page count, and for estimates/invoices the total.

## Estimates and invoices

Money is computed exactly (Decimal) from `qty × unit_price` per item. Sections give subtotals.
`adjustments` add tax (`rate` with `applies_to: [item codes]` or `all`) or fixed amounts (payments are
negative). Alternates show as add/(deduct). Check the printed total against your own sum before reporting.

## Brands

Brands: `--brand tfc | kvntv | bar | owen | default`. Private brand files (logos, fonts) live in
`~/.config/pdf-studio/brands/` (→ the private pdf-studio-local repo); `default` ships in the public repo.
Pick by audience: church/staff → `tfc`; broadcasts/sponsors → `kvntv`; estimates, quotes, invoices for
BAR → `bar`; anything personal → `owen`. Colors are checked for 4.5:1 contrast on load; a missing brand font
fails the gate instead of silently falling back.

## House style (built into the templates — do not fight it)

- US Letter, 1 in outer margins; labels hang in a left gutter; running text in a ~66-character measure.
- Two families: Source Serif 4 (text) and Inter (headings, tables, labels). Four sizes. Tabular figures.
- Left-aligned, ragged right; only the book manuscript is justified (with hyphenation).
- Headings stay with their text; tables have no vertical rules, numbers right-aligned, units in headers,
  header rows repeat on every page.
- Title, author, language, bookmarks and tags are set automatically. `--standard pdfa-2b` for archives,
  `pdfua-1` for accessibility requirements.
- Logos as SVG; photos at ≥ 150 dpi (screen) / 300 dpi (print).

For HTML sources use `pdfcore convert page.html` (WeasyPrint). Stamping onto existing pages belongs to
pdf-organize, not here.
