# `pdfcore-words/1` — exact extraction rules

Status: **draft until BAR's parity spike passes on `fixtures/bar/`** (then frozen; only additive changes within /1).
Schema: [`schemas/pdfcore-words-1.schema.json`](../schemas/pdfcore-words-1.schema.json). Reference implementation:
[`pdfcore/words.py`](../pdfcore/words.py). Every rule below is what the reference implementation does, so a second
reader (BAR's pdf.js reader) can reproduce it. All thresholds are in PDF points or multiples of the font size;
no sort key is rounded.

## 1. Page space and the reading frame
- Visible box `V = CropBox ∩ MediaBox` (pdf.js's viewport box), `W × H`. **Unrotated space**: a PDF user-space
  point `(x, y)` maps to `(u, v) = (x − V.left, V.top − y)` (origin top-left, `v` down).
- `rot(p, r, w, h)` turns a point of a `w × h` box clockwise by `r`: 0 → `(x, y)`; 90 → `(h − y, x)`;
  180 → `(w − x, h − y)`; 270 → `(y, w − x)`. A box is turned by turning two opposite corners and taking min/max.
- **Display space** = unrotated space turned by `/Rotate` (`rot(·, R, W, H)`). All output coordinates are in display
  space; `pages[].width/height` are the displayed size (`W, H`, swapped for 90/270).
- **Writing direction** of a glyph: its text-rendering matrix x-axis `(a, b)` in PDF space is `(a, −b)` in
  unrotated space; `θ = atan2(−b, a)` in degrees, quantised `q = round(θ / 90) mod 4`, direction `= 90·q`
  (clockwise). Its display direction is `(direction + R) mod 360`.
- **Direction groups.** Split the page's glyphs by display direction `δ ∈ {0, 90, 180, 270}` (each group keeps
  content-stream order). A group's **frame** is unrotated space turned by `F = (R − δ) mod 360` (size `W_F × H_F`,
  swapped for 90/270), so that group's text runs left to right. **§2–§5 (boxes, words, lines, blocks) run per group
  in its own frame.** Output boxes are frame boxes turned by `δ` (`rot(·, δ, W_F, H_F)`), which lands in display
  space. Drawing sheets mix directions (vertical title-block and label text), so this matters.
- **Dominant direction** `D`: the display direction shared by the most non-whitespace glyphs (ties → the smallest;
  0 for a page without text). **§6 orders all blocks of all groups together in the dominant frame**: each block's
  display box is turned by `(360 − D) mod 360` (`rot(·, ·, display W, display H)`) and the XY-cut runs on those
  boxes. Lines and words inside a block are ordered in the block's own frame.
- Examples: an ordinary page, or a landscape sheet drawn upright for its `/Rotate`, has `D = 0`, so its frame is the
  display; content that is upright before `/Rotate` has `D = R`, so the page groups exactly as if unrotated.

## 2. Glyphs
- Source: every character of the page's text, in content-stream order, **excluding** characters synthesised by
  the extractor (PDFium "generated" characters). pdf.js normalisation is not applied (BAR reads operator-list
  glyphs). Code point 0 is skipped. A character PDFium flags as a hyphen (`FPDFText_IsHyphen`, reported as U+0002)
  is `-`.
- `size` = **effective** font size: `Tf × hypot(c, d)` of the glyph's text-rendering matrix `[a b c d e f]`
  (text matrix × CTM, without `Tf`), i.e. `Tf` scaled by the matrix's y-axis length.
- Box, in the group's frame: horizontally the glyph's advance (origin to origin + advance; **excludes** `Tc`/`Tw`
  spacing); vertically `top = baseline − ascent·size`, `bottom = baseline − descent·size`, with the font's
  ascent/descent for **embedded** fonts and the fixed fallback **0.8 / −0.2 for every non-embedded font**
  (including the standard 14 — readers substitute different fonts). `baseline` = the frame `y` of the glyph origin.
- `font` = base font name with a 6-uppercase-letter subset prefix `ABCDEF+` removed.

## 3. Words
Walk glyphs in order. Start a new word when:
1. the glyph is whitespace — the Unicode `White_Space` set: U+0009–U+000D, U+0020, U+0085, U+00A0, U+1680,
   U+2000–U+200A, U+2028, U+2029, U+202F, U+205F, U+3000 (not Python `str.isspace()`, which adds U+001C–U+001F,
   nor JS `\s`, which adds U+FEFF) — the whitespace itself is dropped; or
2. `|baseline − previous.baseline| > 0.5·s`; or
3. `glyph.x0 − previous.x1 > 0.15·s`; or
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

Within a block: lines by `(top, x0)`; within a line: words by `x0` (both in the block's own frame). `block` = position in the ordered block list;
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
