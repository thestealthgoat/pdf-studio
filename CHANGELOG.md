# Changelog

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
