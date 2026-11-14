# `pdfcore-words/1` — exact extraction rules

Status: **draft until BAR's parity spike passes on `fixtures/bar/`** (then frozen; only additive changes within /1).
Schema: [`schemas/pdfcore-words-1.schema.json`](../schemas/pdfcore-words-1.schema.json). Reference implementation:
[`pdfcore/words.py`](../pdfcore/words.py). Every rule below is what the reference implementation does, so a second
reader (BAR's pdf.js reader) can reproduce it. All thresholds are in PDF points or multiples of the font size;
no sort key is rounded.

## 1. Page space
- Visible box `V = CropBox ∩ MediaBox` (pdf.js's viewport box). `W, H` = its width/height.
- A point `(x, y)` in PDF user space maps to `(u, v) = (x − V.left, V.top − y)`, then by `/Rotate`:
  0 → `(u, v)`; 90 → `(H − v, u)`; 180 → `(W − u, H − v)`; 270 → `(v, W − u)`.
- `pages[].width/height` are the displayed size (`W, H`, swapped for 90/270).

## 2. Glyphs
- Source: every character of the page's text, in content-stream order, **excluding** characters synthesised by
  the extractor (PDFium "generated" spaces/line breaks; pdf.js `hasEOL`/inserted spaces) and code point 0.
  Line-break characters in the content count as whitespace.
- Box: horizontally the glyph's advance (origin to origin + advance); vertically
  `top = baseline − ascent·size`, `bottom = baseline − descent·size` (font ascent/descent; fallback 0.8 / −0.2).
  `size` = effective font size in points. Coordinates mapped as in §1; `baseline` is the mapped baseline
  (`v` for 0°/180°, `u` for 90°/270°).
- `font` = base font name with a 6-uppercase-letter subset prefix `ABCDEF+` removed.

## 3. Words
Walk glyphs in order. Start a new word when:
1. the glyph is whitespace (`str.isspace()` / JS `\s`) — the whitespace itself is dropped; or
2. `|baseline − previous.baseline| > 0.5·s`; or
3. `glyph.x0 − previous.x1 > 0.2·s`; or
4. `glyph.x1 < previous.x0` (the text jumped backwards),

where `s = max(previous.size, glyph.size, 1)`. A word's box is the union of its glyph boxes; its `size` is the max
glyph size; its `font` and `baseline` come from its first glyph. Text: expand U+FB00–U+FB06 (ff, fi, fl, ffi, ffl,
ſt→st, st) then Unicode NFC (never NFKC). Empty results are dropped.

## 4. Lines
Sort the page's words by `(baseline, x0)`. For each word, test existing lines **from the most recently created
to the oldest**; join the first line whose last word `L` satisfies
`|L.baseline − w.baseline| ≤ 0.3·s` and `0 ≤ w.x0 − L.x1 ≤ 3·s` (`s = max(L.size, w.size, 1)`).
Otherwise create a new line.

## 5. Blocks
Sort lines by `(top, x0)` (line box = union of its words). For each line, test existing blocks from most recent to
oldest; join the first block whose last line `P` satisfies `−0.5·h ≤ line.top − P.bottom ≤ 0.9·h` (`h` = the larger of
the two line heights, min 1; the negative side allows tight leading, where loose glyph boxes of adjacent lines overlap) **and** horizontal overlap `min(P.x1, line.x1) − max(P.x0, line.x0) > 0`.
Otherwise create a new block.

## 6. Block order — recursive XY-cut `cut(S)`, horizontal first
1. If `|S| ≤ 1` return `S`.
2. **Bands.** Sort `S` by `(top, x0)`. Walk it keeping `reach` = max `bottom` so far (starting with the first block's);
   start a new band when `block.top − reach > 0.5`. If there is more than one band, return the concatenation of
   `cut(band)` in order.
3. **Columns.** Sort `S` by `(x0, top)`. Walk it keeping `reach` = max `x1` so far; start a new column when
   `block.x0 − reach ≥ 12` (points). If there is more than one column, return the concatenation of `cut(column)`
   left to right.
4. Otherwise return `S` sorted by `(top, x0)`.

Effect: page regions separated by horizontal whitespace are read top to bottom; inside a region, columns separated
by a 12 pt gutter are read left to right. A table row whose cells are separated by whitespace from the next row
reads cell by cell (`Activity, Description, Qty, Rate, Amount`), then the next row. Known limit: label/value pairs
laid out as two right-aligned stacks (e.g. `DATE` / `11/05/2026`) read as all labels, then all values — use the
coordinates for those.

Within a block: lines by `(top, x0)`; within a line: words by `x0`. `block` = position in the ordered block list;
`line` = running index over lines in that order. Both 0-based per page.

## 7. Output
- `pages` / `words` cover all pages, or the selected pages in ascending order without duplicates (out-of-range
  indexes are ignored). `words` are in reading order, page by page. Numbers rounded to 0.01 (`-0` written as `0`).
- `source.hasTextLayer` = at least one word on the processed pages. `source.encrypted` = the file has an
  security handler (PDFium `FPDF_GetSecurityHandlerRevision ≠ −1`; owner-password-only files are still processed; user-password files fail with `Encrypted`).
- Serialisation: UTF-8, no BOM, LF, compact separators, keys in schema order, trailing newline — byte-identical for
  the same input and version.
- Error codes: `NotPdf` (missing, unreadable, or no `%PDF` header in the first 1024 bytes), `TooLarge` (over the
  size limit, default 1024 MB), `Encrypted` (needs a user password), `Damaged` (PDFium can't open it), `Timeout`.
