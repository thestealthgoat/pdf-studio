# Test corpus — sources and licenses

Nothing in this repository is a private document. Generated files are rebuilt by
`python corpus/build.py` into `corpus/generated/` (git-ignored); all of their content
is original and dedicated to the public domain (CC0-1.0).

| File | Covers | Source | License |
|---|---|---|---|
| letter.pdf | business letter (vector) | generated | CC0 |
| scanned_letter.pdf | scanned letter, 1.6° skew, noise, 200 dpi | generated (rasterized letter) | CC0 |
| scanned_rotated.pdf | scan rotated 90° + skew (auto-rotate) | generated | CC0 |
| form_fillable.pdf | government-style fillable form: text, radio, checkbox, choice | generated | CC0 |
| form_flat.pdf | flat form with blank lines and boxes (field auto-detect) | generated | CC0 |
| contract_v1.pdf / contract_v2.pdf | multi-page contract with PII (fake phone, email, SSN) and two versions for compare | generated | CC0 |
| report_tables.pdf | report with headings and numeric tables | generated | CC0 |
| slides.pdf | 16:9 slide-deck export | generated | CC0 |
| signed.pdf | PAdES signature (self-signed test certificate) | generated | CC0 |
| encrypted.pdf | AES-256, user password `test` | generated | CC0 |
| broken.pdf | damaged xref and truncated trailer | generated | CC0 |
| layers.pdf | optional content groups (3 layers) | generated | CC0 |
| image_heavy.pdf | 4 pages of 300 dpi lossless images (~50 MB) | generated | CC0 |
| drawings_rev0.pdf / drawings_rev1.pdf | ARCH D electrical drawing set, 5 sheets, title blocks, callouts, two revisions | generated | CC0 |

`corpus/external/` (git-ignored) holds downloaded public documents for local testing; each one is
listed below with its URL and license before use.

| File | Source URL | License |
|---|---|---|
| flint_wpc_electrical_2023.pdf | https://www.cityofflint.com/wp-content/uploads/2023/07/Flint_WPC-Electrical-Distribution-Improvements-Drawing-Plans-July-7-2023-1.pdf (City of Flint public bid posting, 50 sheets ANSI D) | Public bid document; local testing only, not redistributed |
| flint_addendum01_plan_set.pdf | https://www.cityofflint.com/wp-content/uploads/2023/07/20230710_COF107701F_Addendum-01_Plan-Set.pdf (City of Flint public posting, 30 sheets, different project; mirrored text on sheet 1) | Public bid document; local testing only, not redistributed |

Private BAR / TFC samples belong in `pdf-studio-local/corpus/private/` only — never here.
