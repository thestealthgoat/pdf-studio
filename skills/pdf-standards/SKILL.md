---
name: pdf-standards
description: Signatures and standards with pdfcore — sign, certify, timestamp and validate PDF signatures (PAdES B-B to B-LTA via pyHanko), produce and validate PDF/A and PDF/UA (veraPDF), preflight reports, and accessibility checks of existing PDFs (tags, reading order, alt text).
---

# PDF Studio — signatures, PDF/A, PDF/UA, accessibility

## Standards for new documents

Make them compliant at creation time (pdf-make): `pdfcore make report data.yaml --standard pdfa-2b`
(or `pdfa-3b` to embed source files, `pdfua-1` for accessibility). Then validate:

```bash
pdfcore gate out.pdf --standard pdfa-2b -v           # runs veraPDF; FAIL lists the first rule broken
pdfcore preflight out.pdf --standard pdfa-2b         # full rule report (PDF + JSON)
```

Converting an *existing* PDF to PDF/A: `pdfcore pdfa in.pdf [--level 2b]` (adds output intent and XMP, removes
forbidden features; it does NOT embed missing fonts — it reports them). If veraPDF still fails, say which rules
and why — do not claim compliance. PDF/X needs Ghostscript and
is local-edition only.

## Signatures

```bash
pdfcore sign f.pdf --key me.key.pem --cert me.cert.pem [--field Sig1 --page 1 --rect 72,650,272,700]
pdfcore sign f.pdf --pkcs12 me.p12 --level b-lta --tsa https://timestamp.example   # timestamped, long-term
pdfcore certify f.pdf --pkcs12 me.p12 --allow form-fill                              # MDP certification
pdfcore verify f.pdf                                    # each signature: intact? trusted? covers whole file?
```

Levels: `b-b` (basic) → `b-t` (timestamp) → `b-lt` (+ revocation info) → `b-lta` (+ archive timestamp).

Rules:
1. The key/certificate and any passwords come from the user. Never create a signing identity for them
   without being asked; never print private keys or passwords.
2. Sign last: any change after signing breaks the signature. Stamp, OCR, PDF/A first, then sign.
3. Report verification plainly: "valid and trusted", "valid but untrusted root", or "modified after signing".

## Accessibility check (existing PDFs)

```bash
pdfcore a11y f.pdf                 # tagged?, language, title, reading order sample, images without alt text,
                                   # headings structure, table headers, contrast of text on its background
```

Report the findings as a short list with page numbers. Automatic repair is limited: offer to rebuild the
document from source with `pdfcore make` when the source is available.
